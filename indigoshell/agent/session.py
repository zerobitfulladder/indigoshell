"""The agent on the other end of the call — Claude, through the Agent SDK.

This module owns the conversation and knows nothing about drawing; the
call overlay (`agent/ui/`) reads the state kept here and listens
for changes. One `Agent` lives for the daemon's lifetime, listed in the
config's SERVICES.

**Whose account.** The SDK runs the Claude Code binary, which signs in
from its config directory. That directory is `config_dir` here —
`~/.config/indigoshell/claude` by default — and is handed to the
binary as CLAUDE_CONFIG_DIR, so the shell's agent has its own login,
settings and session history, separate from a `claude` in a terminal
(which keeps reading `~/.claude`). Sign it in once with

    CLAUDE_CONFIG_DIR=~/.config/indigoshell/claude claude auth login

and it uses that account's subscription. The shell never handles the
login itself: it only checks the credentials file is there.

An API key or cloud-provider switch in the daemon's environment would
outrank that login and bill elsewhere, and `env` can only add to the
inherited environment, not remove from it — so the agent refuses to
dial while one is set rather than silently paying with it.

**One task owns the client.** `ClaudeSDKClient` keeps an anyio task
group alive from `connect()` to `disconnect()` and must be connected and
disconnected from the same task. `_run()` is that task; the UI only
calls the plain methods below, which queue work for it.

**Turns.** One prompt at a time: a line typed while the agent is busy
waits in `queued` and goes out when the turn ends. Sending it mid-turn
would let the CLI fold it into the running turn, and the turn count
would no longer match the results that come back.

**Hanging up.** The agent gets one tool of ours, `hang_up`. It calls it
before long work with one line about what it is doing; the overlay
closes and the work goes on. A turn that ends while the call is closed
rings — `ringing` is set — and answering shows the result.

**Progress.** `jobs` hears how far the agent's long commands have got
(agent/progress.py): its Claude Code process is started with the
socket's path and the shims on its PATH, and each job is matched to the
step that started it, so the overlay can draw its bar under that step.
"""

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import time
from collections import deque
from typing import Any, Callable

from ..core import paths, state
from .activity import (RESULT_KEEP, Activity, Approval, approval_detail,
                       describe_tool, home_path, result_text, tool_detail)
from .permissions import ASK_FIRST
from .progress import Job, Jobs, runs_command
from .prompts import CALL_PROTOCOL, DEFAULT_PERSONA
from .seed import seed

log = logging.getLogger(__name__)

# What Ctrl+M and Ctrl+T cycle through in the call. Aliases: Claude Code
# resolves each to the newest model of its line. Fable can bill to usage
# credits rather than the plan's included limits, depending on the plan.
MODELS = ("opus", "sonnet", "haiku", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Claude Code's own default for the current models, when none is set.
DEFAULT_EFFORT = "medium"

# Environment that outranks a subscription login (Claude Code's
# authentication precedence), so dialing with any of it set would bill
# something other than the plan the user signed in with.
_OVERRIDES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)

# A conversation older than this starts fresh on the next dial instead of
# resuming — yesterday's context is more likely to confuse than help.
RESUME_WITHIN_S = 6 * 3600
# Hang up the CLI process after this long with no call open and no turn
# running. It holds a few hundred MB; the next dial resumes the session.
IDLE_DISCONNECT_S = 15 * 60
# The longest single line we accept from the CLI's stdout. The SDK's
# default is 1MB, and one message over it ends the stream for good: the
# turn dies and the CLI is hung up. Reading a phone photo crosses it —
# the CLI downscales it to ~660KB of base64, then sends that twice in
# one message (the tool result, and `tool_use_result` beside it). This
# is only a ceiling; nothing is allocated up front.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# Everything the agent did this turn, so the overlay can expand it.
ACTIVITY_KEEP = 80


class Agent:
    """The conversation, its state and its CLI process.

    Widgets read the public attributes and `subscribe()` for a callback
    on every change; they never touch the SDK.
    """

    def __init__(self, *, name: str = "INDIGO",
                 persona: str = DEFAULT_PERSONA,
                 model: str | None = None,
                 effort: str | None = None,
                 config_dir: str | None = None,
                 workspace: str | None = None,
                 cli_path: str | None = None,
                 permission_mode: str = "auto",
                 ask: tuple[str, ...] = ASK_FIRST,
                 tools=None) -> None:
        self.name = name
        # Extra tools for the agent — `ShellTools`, the desktop. Anything
        # with `build(agent) -> [SdkMcpTool]` and a `prompt` string.
        self.tools = tools
        self.persona = persona
        self.model = model
        self.effort = effort
        # "auto": Claude Code's classifier approves routine tool calls
        # and blocks risky ones (telling the model why); `ask` rules still
        # come to the overlay. "default" asks for anything that changes
        # the system. Auto mode needs a recent model; where it is not
        # available Claude Code falls back to asking.
        self.permission_mode = permission_mode
        self.ask = tuple(ask)
        self.config_dir = config_dir or os.path.join(paths.config_dir(),
                                                     "claude")
        self.workspace = workspace or os.path.join(paths.config_dir(),
                                                   "assistant")
        self.cli_path = cli_path

        # ── what the overlay shows ──────────────────────────────────
        self.phase = "offline"   # offline dialing idle thinking working
                                 # speaking asking error
        self.busy = False        # a turn is in flight
        self.heard = ""          # the user's line this turn answers
        self.queued: list[str] = []
        self.message = ""        # what the agent is saying now / said last
        self.activity: deque[Activity] = deque(maxlen=ACTIVITY_KEEP)
        self.thought = ""        # latest thinking summary, if any
        self.approval: Approval | None = None
        self.error: str | None = None
        self.hint: str | None = None   # what to do about `error`
        self.hangup_line: str | None = None
        self.turn_started = 0.0
        self.call_open = False
        self.ringing: str | None = None     # "done" | "ask" | "error"
        self.ring_started = 0.0
        # How far its long commands have got, for the bars.
        self.jobs = Jobs()
        self.jobs.claim = self._claim_job
        self.jobs.subscribe(self._on_job)

        self._listeners: list[Callable[[str], None]] = []
        self._client = None
        self._worker: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._session: str | None = None
        self._resume: str | None = None
        # The reply so far this turn: one entry per text block, so text the
        # agent writes between tool calls stays part of what it says. With
        # each, the id of the last step before it (0: none), so the
        # overlay can show text and steps in the order they happened.
        self._segments: list[str] = []
        self._seg_after: list[int] = []
        self._restart_after_turn = False
        self._interrupted = False
        self._redial = False
        self._idle_handle: asyncio.TimerHandle | None = None

    # ── service hooks ───────────────────────────────────────────────────
    def start(self) -> None:
        """Listen for progress; the CLI itself spawns on the first dial."""
        self.jobs.start()
        saved = state.get("agent.session")
        if isinstance(saved, dict) and saved.get("id") and not self._stale():
            self._resume = saved["id"]
            # Show where it left off, so a call after a restart does not
            # open blank on a conversation that is in fact continuing.
            self.heard, self.message = self._last_exchange(saved["id"])
        # A model or effort picked on a call outlives reloads.
        if state.get("agent.model") in MODELS:
            self.model = state.get("agent.model")
        if state.get("agent.effort") in EFFORTS:
            self.effort = state.get("agent.effort")

    def stop(self) -> None:
        self.jobs.stop()
        if self._stop is not None:
            self._stop.set()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    # ── listeners ───────────────────────────────────────────────────────
    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners \
            else None

    def _emit(self, event: str = "change") -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                log.exception("agent listener failed on %r", event)

    # ── what the UI calls ───────────────────────────────────────────────
    def set_call_open(self, value: bool) -> None:
        """The overlay opened or closed. Opening answers a ring."""
        if value == self.call_open:
            return
        self.call_open = value
        if value:
            self.ringing = None
            self.dial()
        elif self.approval is not None:
            # Hung up on a question: it rings until answered, since the
            # task is parked on it.
            self._ring("ask")
        self._schedule_idle()
        self._emit()

    def dial(self) -> None:
        """Make sure the CLI is up. Cheap when it already is."""
        if self._worker is not None and not self._worker.done():
            return
        problem = self._preflight()
        if problem is not None:
            self._fail(*problem)
            return
        if self.session_id and not self.busy and self._stale():
            # A conversation quiet for longer than RESUME_WITHIN_S starts
            # over, however long the shell has been running.
            self._session = self._resume = None
            self._clear_said()
            self.heard = self.thought = ""
            self.activity.clear()
            self.activity.append(Activity(
                "info", "a fresh line — the last one was quiet for over "
                f"{RESUME_WITHIN_S // 3600} hours (Ctrl+H to go back)"))
        self.error = self.hint = None
        self.phase = "dialing"
        self._worker = asyncio.get_running_loop().create_task(self._run())

    def _stale(self) -> bool:
        """Whether the current conversation's last turn ended longer
        ago than RESUME_WITHIN_S."""
        saved = state.get("agent.session")
        at = saved.get("at", 0) if isinstance(saved, dict) else 0
        return time.time() - at > RESUME_WITHIN_S

    def say(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if self.busy or self._client is None:
            # Mid-turn, or still dialing: it goes out when the line is
            # free. Only an absent CLI needs dialing — calling dial()
            # mid-turn could only fail a preflight and wreck the turn.
            self.queued.append(line)
            if self._client is None:
                self.dial()
            self._emit()
            return
        self._begin_turn(line)

    def interrupt(self) -> None:
        if self.approval is not None:
            self._resolve(self.approval, "deny", interrupt=True)
        self.queued.clear()
        if self.busy and self._client is not None:
            self._interrupted = True
            asyncio.get_running_loop().create_task(self._safe_interrupt())
        self._emit()

    def acknowledge(self) -> None:
        """The user is looking: stop calling for attention. A question
        keeps ringing until it is answered."""
        if self.ringing in ("done", "error"):
            self.ringing = None
            self._emit()

    def answer(self, decision: str) -> None:
        """Resolve the pending approval: "allow", "always" or "deny"."""
        if self.approval is not None:
            self._resolve(self.approval, decision)

    def new_conversation(self) -> bool:
        """Drop the context and start the next call fresh."""
        if self.busy:
            return False
        self._session = self._resume = None
        state.delete("agent.session")
        self._clear_said()
        self.heard = self.thought = ""
        self.hangup_line = self.error = self.hint = None
        self.activity.clear()
        self.activity.append(Activity("info", "new line — context cleared"))
        if self._stop is not None and self._client is not None:
            # The next dial starts a new session; with the call open that
            # is straight away, once this CLI has gone.
            self._redial = True
            self._stop.set()
        self._emit()
        return True

    # ── model and effort ────────────────────────────────────────────────
    def cycle_model(self, step: int = 1) -> None:
        """Next model (Ctrl+M on the call). Takes effect at once, on the
        live conversation; it keeps its context."""
        current = self.model if self.model in MODELS else MODELS[0]
        self.model = MODELS[(MODELS.index(current) + step) % len(MODELS)]
        state.set("agent.model", self.model)
        note = f"model: {self.model}"
        if self.model == "fable":
            note += " — may draw on usage credits"
        self.activity.append(Activity("info", note))
        if self._client is not None:
            asyncio.get_running_loop().create_task(self._apply_model())
        self._emit()

    async def _apply_model(self) -> None:
        try:
            if self._client is not None:
                await self._client.set_model(self.model)
        except Exception:
            log.exception("could not switch the model")

    def cycle_effort(self, step: int = 1) -> None:
        """Next effort level (Ctrl+T on the call). Effort is fixed when
        Claude Code starts, so it reconnects, resuming the conversation —
        straight away, or once the running task is done."""
        current = self.effort if self.effort in EFFORTS else DEFAULT_EFFORT
        self.effort = EFFORTS[(EFFORTS.index(current) + step) % len(EFFORTS)]
        state.set("agent.effort", self.effort)
        self.activity.append(Activity(
            "info", f"effort: {self.effort}"
            + (" — from the next task" if self.busy else "")))
        self._restart_for_options()
        self._emit()

    def _restart_for_options(self) -> None:
        if self._client is None or self._stop is None:
            return                      # the next dial starts with them
        if self.busy:
            self._restart_after_turn = True
            return
        self._redial = True
        self._stop.set()

    # ── past conversations ──────────────────────────────────────────────
    @property
    def session_id(self) -> str | None:
        return self._session or self._resume

    @contextlib.contextmanager
    def _own_config(self):
        """Point the SDK's session readers at this agent's config dir.

        They read CLAUDE_CONFIG_DIR from *this* process's environment and
        fall back to ~/.claude — which is a different account's history.
        The readers are synchronous, so nothing else runs on the loop
        while the variable is swapped.
        """
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.config_dir
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old

    def lines(self, limit: int = 40) -> list:
        """Past conversations in the workspace, newest first
        (`SDKSessionInfo`s: session_id, summary, last_modified ms)."""
        try:
            from claude_agent_sdk import list_sessions
            with self._own_config():
                return list_sessions(directory=self.workspace, limit=limit,
                                     include_worktrees=False)
        except Exception:
            log.exception("cannot list past conversations")
            return []

    def resume_line(self, session_id: str) -> bool:
        """Go back to a past conversation: its context comes back on the
        next turn, and its last exchange is shown in the meantime."""
        if self.busy:
            return False
        if session_id == self.session_id:
            return True
        heard, said = self._last_exchange(session_id)
        self._clear_said()
        self.message, self.heard, self.thought = said, heard, ""
        self.hangup_line = self.error = self.hint = None
        self.activity.clear()
        self.activity.append(Activity("info", "back on an earlier line"))
        self._session = session_id
        state.set("agent.session", {"id": session_id, "at": time.time()})
        if self._stop is not None and self._client is not None:
            self._redial = True
            self._stop.set()         # the CLI comes back resuming it
        else:
            self._resume = session_id
            if self.call_open:
                self.dial()
        self._emit()
        return True

    def _last_exchange(self, session_id: str) -> tuple[str, str]:
        """The last thing the user said and the last reply, as text."""
        try:
            from claude_agent_sdk import get_session_messages
            with self._own_config():
                msgs = get_session_messages(session_id,
                                            directory=self.workspace)
        except Exception:
            log.debug("cannot read session %s", session_id, exc_info=True)
            return "", ""
        heard = said = ""
        for m in msgs:
            content = (m.message or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            else:
                texts = [b.get("text", "") for b in content or []
                         if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(t for t in texts if t).strip()
            if not joined:
                continue
            if m.type == "user":
                heard, said = joined, ""
            elif m.type == "assistant":
                said = joined
        return heard, said

    @property
    def login_command(self) -> str:
        return f"CLAUDE_CONFIG_DIR={home_path(self.config_dir)} claude auth login"

    # ── turn bookkeeping ────────────────────────────────────────────────
    def _begin_turn(self, line: str) -> None:
        self.busy = True
        self.ringing = None
        self.heard = line
        self._clear_said()
        self.thought = ""
        self.hangup_line = None
        self.activity.clear()
        self.error = self.hint = None
        self._interrupted = False
        self.turn_started = time.monotonic()
        self.phase = "thinking"
        self._schedule_idle()
        asyncio.get_running_loop().create_task(self._send(line))
        self._emit()

    def _end_turn(self, result: Any) -> None:
        self.busy = False
        if self.approval is not None:       # a turn cannot end mid-ask,
            self._resolve(self.approval, "deny")   # but never leave one
        if getattr(result, "is_error", False):
            text = (result.result or "").strip() or "the call dropped"
            if not self.error:
                self._set_error(text)
        else:
            final = self._said() or (result.result or "").strip()
            if final:
                self.message = final
            self.phase = "idle"
        if result is not None and getattr(result, "session_id", None):
            self._session = result.session_id
            state.set("agent.session", {"id": self._session,
                                        "at": time.time()})
        if self.queued:
            self._begin_turn(self.queued.pop(0))
            return
        if self._restart_after_turn:
            self._restart_after_turn = False
            self._restart_for_options()
        if not self.call_open and not self._interrupted:
            # Only a closed call rings. With the call open the user is in
            # it — the overlay blinks its chip briefly instead — and a ring
            # left over from then would come back as a call request the
            # moment they hung up.
            self._ring("error" if self.phase == "error" else "done")
        self._schedule_idle()
        self._emit()

    def _ring(self, why: str) -> None:
        self.ringing = why
        self.ring_started = time.monotonic()
        self._emit("ring")

    def _set_error(self, text: str) -> None:
        self.phase = "error"
        self.error = text
        low = text.lower()
        if "not logged in" in low or "/login" in low or "oauth" in low:
            self.error = "Not signed in on the shell's account."
            self.hint = self.login_command

    def _fail(self, error: str, hint: str | None = None) -> None:
        log.warning("agent: %s", error)
        self.phase = "error"
        self.error, self.hint = error, hint
        self.busy = False
        self._emit()

    def _preflight(self) -> tuple[str, str | None] | None:
        set_vars = [v for v in _OVERRIDES if os.environ.get(v)]
        if set_vars:
            return (f"{set_vars[0]} is set in the shell's environment and "
                    "would bill it instead of your plan.",
                    "unset it where indigoshell starts, then reload")
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return ("The Claude Agent SDK is not installed.",
                    "uv pip install --python .venv/bin/python claude-agent-sdk")
        if not os.path.exists(os.path.join(self.config_dir,
                                           ".credentials.json")):
            return ("Not signed in on the shell's account.",
                    self.login_command)
        return None

    def _schedule_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self.busy or self.call_open or self._client is None:
            return
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(IDLE_DISCONNECT_S,
                                            self._idle_hangup)

    def _idle_hangup(self) -> None:
        self._idle_handle = None
        if not self.busy and not self.call_open and self._stop is not None:
            log.info("agent idle for %ds; closing its CLI", IDLE_DISCONNECT_S)
            self._stop.set()

    # ── the worker ──────────────────────────────────────────────────────
    async def _run(self) -> None:
        import warnings

        from claude_agent_sdk import ClaudeSDKClient, CanUseToolShadowedWarning
        # Our own hang_up tool is pre-approved on purpose.
        warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

        self._stop = stop = asyncio.Event()
        self.phase = "dialing"
        self._emit()
        seed(self.workspace)
        self.jobs.refresh()

        client = ClaudeSDKClient(self._options())
        t0 = time.monotonic()
        try:
            await client.connect()
        except Exception as e:
            log.exception("agent connect failed")
            self._fail(f"could not start Claude Code: {e}")
            await client.disconnect()
            return
        log.info("agent on the line in %.2fs%s", time.monotonic() - t0,
                 f" (resuming {self._resume[:8]})" if self._resume else "")
        self._client = client
        self._resume = None
        if not self.busy:
            self.phase = "idle"
        self._schedule_idle()
        if self.queued and not self.busy:
            self._begin_turn(self.queued.pop(0))
        else:
            self._emit()

        reader = asyncio.get_running_loop().create_task(self._read(client))
        stopper = asyncio.get_running_loop().create_task(stop.wait())
        try:
            await asyncio.wait({reader, stopper},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            self._client = None
            if self._session:
                self._resume = self._session
            try:
                await client.disconnect()
            except Exception:
                log.debug("agent disconnect failed", exc_info=True)
            reader.cancel()
            if self.busy:
                self.busy = False
                self._set_error("the line dropped mid-task")
            elif self.phase != "error":
                self.phase = "offline"
            self._emit()
            log.info("agent hung up its CLI")
            if self._redial:
                self._redial = False
                if self.call_open:
                    asyncio.get_running_loop().call_soon(self.dial)

    async def _read(self, client) -> None:
        try:
            async for msg in client.receive_messages():
                try:
                    self._on_message(msg)
                except Exception:
                    log.exception("agent message handler failed")
        except Exception as e:
            log.exception("agent stream failed")
            self._set_error(f"the line dropped: {e}")
            self._emit()

    async def _send(self, line: str) -> None:
        client = self._client
        if client is None:
            self.queued.insert(0, line)
            self.busy = False
            self.dial()
            return
        try:
            await client.query(line)
        except Exception as e:
            log.exception("agent send failed")
            self.busy = False
            self._set_error(f"could not reach Claude Code: {e}")
            self._emit()

    async def _safe_interrupt(self) -> None:
        try:
            if self._client is not None:
                await self._client.interrupt()
        except Exception:
            log.debug("interrupt failed", exc_info=True)

    def _options(self):
        from claude_agent_sdk import (ClaudeAgentOptions,
                                      create_sdk_mcp_server, tool)

        @tool("hang_up",
              "End the call and keep working. Call this before work that "
              "takes more than a few seconds of tool use, with one short "
              "spoken line about what you are about to do. The overlay "
              "closes; when your turn ends the user is rung back and reads "
              "your final message. Do not use it for quick answers.",
              {"message": str})
        async def hang_up(args):
            line = str(args.get("message", "")).strip()
            self.hangup_line = line or "on it, I'll call you back"
            self._new_segment(self.hangup_line)
            self.message = self._said()
            self._emit("hangup")
            return {"content": [{"type": "text", "text": (
                "Call ended. Keep working; the user is rung back with "
                "your final message when you finish.")}]}

        prompt = CALL_PROTOCOL.format(name=self.name)
        servers = {"call": create_sdk_mcp_server("call", tools=[hang_up])}
        allowed = ["mcp__call__hang_up"]
        if self.tools is not None:
            servers["shell"] = create_sdk_mcp_server(
                "shell", tools=self.tools.build(self))
            allowed.append("mcp__shell")        # the whole server
            prompt += "\n\n" + self.tools.prompt
        if self.persona:
            prompt += "\n\n" + self.persona
        return ClaudeAgentOptions(
            cwd=self.workspace,
            env={"CLAUDE_CONFIG_DIR": self.config_dir, **self.jobs.env()},
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": prompt},
            mcp_servers=servers,
            allowed_tools=allowed,
            # Tools that need a dialog the call does not have: a multiple-
            # choice question, and plan mode's approve-the-plan step.
            # Without them the agent asks in its reply and the user types.
            disallowed_tools=["AskUserQuestion", "EnterPlanMode",
                              "ExitPlanMode"],
            can_use_tool=self._can_use_tool,
            permission_mode=self.permission_mode,
            # The workspace's skills (the report skill, and any the agent
            # or the user adds) are on, with the Skill tool allowed.
            skills="all",
            # The flag-settings layer: above the workspace's own
            # .claude/settings.json, which can still add rules of its own.
            settings=json.dumps({"permissions": {"ask": list(self.ask)}}),
            include_partial_messages=True,
            max_buffer_size=MAX_MESSAGE_BYTES,
            thinking={"type": "adaptive", "display": "summarized"},
            model=self.model,
            effort=self.effort,
            resume=self._resume,
            cli_path=self.cli_path,
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
        )

    # ── permission prompts ──────────────────────────────────────────────
    async def _can_use_tool(self, name: str, args: dict, ctx) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        loop = asyncio.get_running_loop()
        approval = Approval(
            tool=name,
            title=getattr(ctx, "title", None) or f"use {name}",
            detail=approval_detail(name, args),
            suggestions=list(getattr(ctx, "suggestions", None) or []),
            future=loop.create_future(),
        )
        self.approval = approval
        prev_phase, self.phase = self.phase, "asking"
        if not self.call_open:
            self._ring("ask")
        self._emit()
        try:
            decision = await approval.future
        finally:
            if self.approval is approval:
                self.approval = None
            if self.phase == "asking":
                self.phase = prev_phase if prev_phase != "asking" else "working"
            self._emit()
        verdict, interrupt = decision
        if verdict in ("allow", "always"):
            return PermissionResultAllow(
                updated_permissions=approval.suggestions
                if verdict == "always" and approval.suggestions else None)
        return PermissionResultDeny(
            message="The user declined this over the call.",
            interrupt=interrupt)

    def _resolve(self, approval: Approval, verdict: str,
                 interrupt: bool = False) -> None:
        if not approval.future.done():
            approval.future.set_result((verdict, interrupt))
        if self.ringing == "ask":
            self.ringing = None

    # ── the message stream ──────────────────────────────────────────────
    def _on_message(self, msg: Any) -> None:
        from claude_agent_sdk import (AssistantMessage, RateLimitEvent,
                                      ResultMessage, StreamEvent,
                                      SystemMessage, ToolResultBlock,
                                      ToolUseBlock, UserMessage)

        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id is None:
                self._on_stream(msg.event)
        elif isinstance(msg, AssistantMessage):
            if msg.error is not None:
                text = " ".join(getattr(b, "text", "") for b in msg.content)
                self._set_error(text.strip() or msg.error.replace("_", " "))
                self._emit()
                return
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    self._on_tool(block, sub=msg.parent_tool_use_id is not None)
        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        self._on_result(block)
        elif isinstance(msg, ResultMessage):
            self._end_turn(msg)
        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                sid = msg.data.get("session_id")
                if sid:
                    self._session = sid
                log.debug("agent init: model %s, %s", msg.data.get("model"),
                          msg.data.get("apiKeySource"))
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if getattr(info, "status", "") in ("allowed_warning", "rejected"):
                pct = getattr(info, "utilization", None)
                note = "plan limit reached" if info.status == "rejected" \
                    else "nearing the plan limit"
                if pct is not None:
                    note += f" ({pct:.0%})"
                self.activity.append(Activity("info", note))
                self._emit()

    def _on_stream(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            btype = block.get("type")
            if btype == "thinking":
                self.phase = "thinking"
                self.thought = ""
                self._emit()
            elif btype == "text":
                # A new text block, after a tool call or not: it adds to
                # the reply, it does not replace what came before it.
                self._new_segment("")
                self.phase = "speaking"
                self._emit()
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                if not self._segments:
                    self._new_segment("")
                self._segments[-1] += delta.get("text", "")
                self.message = self._said()
                self.phase = "speaking"
                self._emit()
            elif dtype == "thinking_delta":
                self.thought += delta.get("thinking", "")
                self._emit()

    def _on_tool(self, block: Any, sub: bool) -> None:
        if block.name == "mcp__call__hang_up":
            return
        self.thought = ""
        args = block.input or {}
        self.activity.append(Activity(
            "tool", ("↳ " if sub else "") + describe_tool(block.name, args),
            detail=tool_detail(block.name, args), sub=sub,
            tool_id=block.id, tool=block.name,
            command=str(args.get("command") or "")
            if block.name == "Bash" else ""))
        if self.phase != "asking":
            self.phase = "working"
        self._emit()

    def _on_result(self, block: Any) -> None:
        for act in reversed(self.activity):
            if act.tool_id and act.tool_id == block.tool_use_id:
                # Tabs have no glyph on the overlay's grid; columns of
                # `du` or `ls -l` output still line up at 8.
                out = result_text(block.content).expandtabs(8).strip()
                if len(out) > RESULT_KEEP:
                    out = out[:RESULT_KEEP] + "\n…"
                act.result = out
                act.failed = bool(block.is_error)
                self._emit()
                return

    # ── progress ────────────────────────────────────────────────────────
    def _claim_job(self, job: Job) -> str:
        """The step a job that just started reporting belongs to: the
        Bash call whose command its shell runs (one in the background
        too), or, for a helper of the shell's, the shell tool running."""
        steps = [a for a in self.activity if a.kind == "tool" and a.tool_id]
        if job.under == "cli":
            for act in reversed(steps):
                if act.tool == "Bash" and runs_command(job.cmdline,
                                                       act.command):
                    return act.tool_id
            running = [a for a in steps if a.tool == "Bash"
                       and a.result is None]
            return running[0].tool_id if len(running) == 1 else ""
        if job.under == "shell":
            running = [a for a in steps if a.result is None
                       and a.tool.startswith("mcp__shell__")]
            return running[-1].tool_id if running else ""
        return ""

    def _on_job(self, event: str, job: Job) -> None:
        if event == "end" and job.step and job.visible():
            # Its bar goes; what it came to stays on its step.
            for act in self.activity:
                if act.tool_id == job.step:
                    act.summary = job.summary()
        self._emit("progress")

    def job_of(self, act: Activity) -> Job | None:
        """The running job a step's bar shows, if it has one on show."""
        job = self.jobs.by_step(act.tool_id)
        return job if job is not None and job.live and job.visible() \
            else None

    def loose_jobs(self) -> list[Job]:
        """Jobs on show with no step of this turn to sit under: a
        background command from an earlier turn, or one nothing claimed."""
        steps = {a.tool_id for a in self.activity if a.tool_id}
        return [j for j in self.jobs.shown() if j.step not in steps]

    def _said(self) -> str:
        """The reply so far: every text block this turn, in order."""
        return "\n\n".join(s.strip() for s in self._segments if s.strip())

    def _new_segment(self, value: str) -> None:
        self._segments.append(value)
        self._seg_after.append(self.activity[-1].id if self.activity else 0)

    def _clear_said(self) -> None:
        self.message = ""
        self._segments = []
        self._seg_after = []

    def said_parts(self) -> list[tuple[str, float, str]]:
        """The reply as it was said, for showing between the steps: one
        (key, after, text) per text block, `after` being the id of the
        last step before it. A reply with no blocks behind it (a resumed
        conversation's last answer, a result given only at the end) is
        one part after every step."""
        parts = [(f"m{i}_", float(after), s.strip())
                 for i, (s, after) in enumerate(zip(self._segments,
                                                    self._seg_after))
                 if s.strip()]
        if not parts and self.message.strip():
            parts = [("m", float("inf"), self.message.strip())]
        return parts
