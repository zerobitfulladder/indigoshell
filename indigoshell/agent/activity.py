"""The agent's steps and questions, as the overlay shows them.

An `Activity` is one step — a tool call, a line of narration between
calls, a notice — kept apart from what the agent *says*. An `Approval` is
a tool call parked on the user's yes or no. The functions here turn a
tool call into the one line, and the full detail, the overlay shows."""

import itertools
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any
import asyncio


# A tool's output is kept for the expanded view, trimmed to this.
RESULT_KEEP = 4000

_ids = itertools.count(1)


@dataclass
class Activity:
    """One line of "what the agent is doing" — kept apart from what it
    says, so the overlay can show the two separately.

    `text` is the collapsed one-liner; `detail` is everything (the whole
    command, the whole note) and `result` the tool's output once it has
    come back, for the overlay's expanded view."""

    kind: str            # "tool" | "note" | "info"
    text: str
    detail: str = ""
    result: str | None = None
    failed: bool = False
    sub: bool = False    # inside a subagent
    tool_id: str = ""
    tool: str = ""       # the tool's name, for a tool call
    command: str = ""    # a Bash call's command, as the agent wrote it
    summary: str = ""    # what its progress came to: "51.0 MB in 0:16"
    id: int = field(default_factory=lambda: next(_ids))
    at: float = field(default_factory=time.monotonic)


@dataclass
class Approval:
    """A tool call waiting on the user. The SDK's permission callback is
    parked on `future` until the overlay resolves it."""

    tool: str
    title: str
    detail: str
    suggestions: list
    future: asyncio.Future


def describe_tool(name: str, args: dict) -> str:
    """One activity line for a tool call — the part a person would want
    to see, not the whole input."""
    def first_line(value: Any) -> str:
        return str(value or "").strip().splitlines()[0] if value else ""

    if name == "Bash":
        cmd = first_line(args.get("command"))
        return f"$ {cmd}" if cmd else "shell"
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = args.get("file_path") or args.get("notebook_path") or ""
        verb = {"Read": "read", "Write": "write"}.get(name, "edit")
        return f"{verb} {home_path(path)}"
    if name in ("Glob", "Grep"):
        where = args.get("path")
        what = args.get("pattern", "")
        return f"{name.lower()} {what}" + (f" in {home_path(where)}" if where else "")
    if name == "WebSearch":
        return f"search {args.get('query', '')}"
    if name == "WebFetch":
        return f"fetch {args.get('url', '')}"
    if name in ("Task", "Agent"):
        return f"agent: {first_line(args.get('description')) or 'subtask'}"
    if name == "TodoWrite":
        return "planning"
    if name.startswith("mcp__shell__"):
        # The desktop tools: the tool and its one telling argument.
        what = name.removeprefix("mcp__shell__").replace("_", " ")
        for key in ("target", "action", "url", "name", "uri", "query",
                    "title", "summary", "window_id", "image"):
            if args.get(key):
                return f"{what} {first_line(args[key])}"
        return what
    if name.startswith("mcp__"):
        return name.split("__")[-1].replace("_", " ")
    return name


def tool_detail(name: str, args: dict) -> str:
    """The whole of a tool call, for the expanded view."""
    if name == "Bash":
        cmd = str(args.get("command", "")).expandtabs(4).strip()
        why = str(args.get("description", "")).strip()
        return f"{cmd}\n# {why}" if why else cmd
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        return home_path(str(args.get("file_path") or args.get("notebook_path")
                         or ""))
    if name in ("Task", "Agent"):
        return str(args.get("prompt") or args.get("description") or "")
    try:
        return json.dumps(args, indent=1, ensure_ascii=False)[:RESULT_KEEP]
    except (TypeError, ValueError):
        return str(args)[:RESULT_KEEP]


def result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def home_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else path


def approval_detail(name: str, args: dict) -> str:
    if name == "Bash":
        return str(args.get("command", "")).strip()
    if name in ("Write", "Edit", "MultiEdit", "Read"):
        return home_path(str(args.get("file_path", "")))
    if name == "WebFetch":
        return str(args.get("url", ""))
    return describe_tool(name, args)
