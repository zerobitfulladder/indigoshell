# The app bar protocol

How an indigoshell app tells the shell what to show in the top bar while
one of its windows has focus, and how the shell sends the user's
interactions back. The reference: change it first, then the shell and
the apps. It starts
from `applink/` (the line, the service, the bar's app section), removes
`applink/flux.py` (~1300 lines of Flux in the shell) and turns the
launcher into a part of the bar.

```
 top bar, a Flux window focused:
 | [1 Inbox] [2 std - Rust] [3 Weather]            doc.rust-lang.org | own widgets | AgentChip |
 the same after Ctrl+L, "yo" typed:
 | ⌕ yo▏                                                     12/5321 | own widgets | AgentChip |
 |   SEARCH yo                                                       |
 | ▸ YouTube                                            youtube.com  |   <- the list, flush under
 |   ...                                                             |      the section
```

## 1. Principles

- **The app describes, the shell renders.** An app sends items of a few
  kinds (section 2) with their content and state. The shell owns the
  kinds: their look, fonts, colours, motion, hit testing, drag, typing
  and keyboard handling. A new app needs no shell code.
- **One component, no launcher.** Clicking a tab, typing an address and
  picking from a menu are all the bar's: an input turns the app's section
  into a text box, suggestions dropping flush from the bar's bottom edge
  as if the bar extended down; a menu drops the same way. An app sees no
  launcher, panel or chip. (The shell's MOD+D app picker becomes the same
  component, fed by the app catalog in-process: not this protocol's.)
- **Nothing app-specific in the shell.** `AppService(kinds=...)`,
  `AppWindow` subclasses, `FluxWindow` and the shell's reading of Flux's
  history and bookmarks (`services/addresses.py`) go. No per-kind
  defaults either: Flux's main and single windows differ only in what
  each sends, and a bar follows the announce at once, leaving no gap for
  a default to fill. The announce's `app` stays, for log lines.
- **An app fills its own section only**, only while one of its windows
  (or a dialog transient for one) has focus. Section 6 has the bounds.
- **Identity by id, never by position.** Every event names the app's
  ids, so a tab opened or closed between a click and its event changes
  nothing of what the click meant.
- **Whole state, sent when it changes**; the shell diffs by id for motion.
- **No platform in it.** Windows are named by the app's own ids, chords
  by name ("ctrl+t", never keycodes), and where anything shows is the
  shell's, never coordinates from an app. The X window id is only the
  announce's platform handle, for the shell to follow focus and place
  windows on X11 ("On Wayland", section 3).
- **Any language.** Plain JSON over the existing apps socket, one object
  per line, each with its `type`; a side ignores types, kinds and keys it
  does not know. No version fields and no compatibility branches: the
  shell and its apps ship from one checkout and restart together.

## 2. The vocabulary

A `bar` message holds two runs of visible items, `start` (from the left)
and `end` (against the section's right), and two maps of things shown
only when opened, `inputs` and `menus`. Ids are strings or ints (not
bools), at most 128 characters, unique within one bar, echoed back
unchanged. An item that keeps its id keeps its widget (hover, a drag in
progress, motion); one whose kind changes is made anew.

**Visible items** (`{"kind": ..., "id": ...}` plus):

| kind | fields | the shell draws | events back |
|---|---|---|---|
| `tabs` | `tabs`: [TAB], `current`: tab id or null, `can`: some of `"close"`, `"move"`, `"detach"` | Flux's box tabs: equal slots, numbered, the current one lit, hover, load trace and comet, agent marks, drag lift, tear-out | `select`, `close`, `move`, `detach` |
| `text` | `text` + `color`, or `spans`: [SPAN]; `press`: bool; `opens`: input or menu id | plain text, ellipsized at its end when short of room; hover when it reacts | `press`, or it opens what it names |
| `button` | `label` and/or `icon`, `color`, `on`, `enabled`, `opens` | a plain label or icon; lit while `on` or while what it opens shows; muted when not enabled | `press`, or it opens what it names |

- TAB: `{"id", "title", "busy"?: bool, "progress"?: 0..100, "badge"?:
  {"icon", "color"}, "agent"?: {"voice", "state"}}`. On the current tab
  `progress` lights its outline round, as now; a tab behind that is busy
  runs the comet. `badge` sits at the box's right end (the editor's
  unsaved dot). `agent`: section 5.
- SPAN: `{"text"?: str, "icon"?: name, "color"?: role}`, text or icon.
  Flux's host is three: a bookmark, an open padlock in `error`, the host.
- No `progress` kind (both consumers show load on a tab), no group or
  spacer kind (two runs with the shell's spacing; nothing needs nesting).

**INPUT** (in `inputs`, by id), what the app's section turns into when
opened, and **MENU** (in `menus`), a list of actions dropping from the
bar with no text box:

```
INPUT {"chords"?: [chord], "icon"?: name, "placeholder"?: str,
       "text"?: str, "select"?: bool,         the text it opens with, selected or not
       "query"?: bool | "rows"?: [ROW],       app suggestions by query | static rows
       "keep"?: bool,                         Enter leaves it open (the editor's replace)
       "steps"?: bool,                        Enter turns it to stepping: n, N (find)
       "buttons"?: [{"id", "icon", "on", "chord"?}],   toggles in the box (case, regex)
       "actions"?: {id: {"chord", "drops"?: bool}}}    what a row may do besides Enter
MENU  {"chords"?: [chord], "rows": [ROW]}
```

**ROW**, in suggestions, static rows and menus: `{"id", "title",
"detail"?: str, "tag"?: str, "icon"?: name, "chord"?: str, "enabled"?:
bool, "on"?: bool, "menu"?: menu id, "actions"?: [action id], "match"?:
[int]}`. `detail` is the muted text at the right, `tag` a word before
the title (`OPEN`, `SEARCH`), `chord` a key hint ("ctrl+s"), `on` a
checked mark, `menu` a submenu, `actions` which of the input's actions
this row allows, `match` (in answers) the title's matched characters.
`enabled: false` dims a row, and picking it flashes with no round trip.

**Colours are roles**, never hex from an app; the shell maps them to
`theme.py`, so a theme change recolours every app.

| role | theme | e.g. |
|---|---|---|
| `fg` | `FG` | the host, plain text (the default) |
| `strong` | `FG_STRONG` | emphasis |
| `muted` | `BASE_MUTED` | line:col, mode |
| `info` | `CYAN_BRIGHT` | a count (the default for an answer's) |
| `accent` | `FG_ACCENT` | the unsaved dot |
| `warn` | `YELLOW_MID` | warnings |
| `error` | `ERROR` | insecure padlock, "no matches", errors |
| `ok` | `LIME_BRIGHT` | a passing check |
| `voice:<id>` | that voice's colour | the agent's name (section 5) |

**Icons are names** the shell maps to glyphs, a table grown as apps need:
`bookmark`, `unlocked`, `search`, `replace`, `play`, `history`, `unsaved`,
`branch`, `error`, `warning`, `info`, `case`, `regex`, `file`, `folder`.
An unknown name draws nothing (one debug line); an unknown role draws `fg`.

## 3. Messages

**App -> shell**

| type | fields | when |
|---|---|---|
| `announce` | `app`, `window`: the app's id for it, `handle`: {`x11`: X window id} | first on each connection; `app` no longer picks a class |
| `place` | `with`?: another of its windows, by the app's id | as now, but no X id in it |
| `bar` | `start`, `end`, `inputs`, `menus` | right after the announce, then whenever it differs; replaces the last whole |
| `answer` | `input`, `seq`, `rows`?, `selected`?: row id, `total`?: int, `count`?: {`text`, `color`} | to a `query` |
| `open` | `input` or `menu` | open one now, no key pressed (Flux's new window with no address) |

**Shell -> app**

| type | fields | means |
|---|---|---|
| `attached` | `voices`: {id: name} | the shell has the window; the voices' names (section 5) |
| `restarting`, `placed` | | unchanged |
| `select`, `close` | `item`, `tab` | a left or right press on a tab; a middle click |
| `move` | `item`, `tab`, `before`: tab id or null | dropped before that tab (null: the end) |
| `detach` | `item`, `tab` | torn off the bar: a window of its own, wherever the window manager puts it (today's `x`, `y` hint goes; qtile ignores it) |
| `press` | `item`, `input`? | a `text` or `button` that opens nothing; a toggle in an open input |
| `query` | `input`, `seq`, `text` | an input with `query` has new text |
| `submit` | `input`, `text`, `row`: id or null, `shift`: bool | Enter (the selected row) or Shift+Enter (`row` null: the text itself) |
| `act` | `input`, `action`, `row`, `text` | a row action's chord (Flux: Shift+Delete) |
| `pick` | `menu`, `row` | a menu row chosen |
| `closed` | `input` or `menu` | it left the screen (after a submit or pick too, or Escape, a click outside, focus lost) |
| `fallback` | `input` or `menu` | the shell took the key (or the `open`) but cannot show it: do what you do with no shell |

The shell never sends an app-specific command: Flux's `load`, `bookmark`,
`resume`, `prompt` and `tab`/`close`/`move`/`detach` with an index become
the events above, which Flux interprets.

**Rate.** At most one `bar` per turn of the app's event loop, only when
it differs from the last sent (Flux's `link.push` does this already):
one per progress step during a load, none while idle. 30 tabs are about
2.3 KB, decoded in about 15 µs; the shell applies the newest at its next
frame. Patches are not worth their failure modes at this size.

**Validation.** A `bar` that breaks its shape (a missing or repeated id,
`current` naming no tab, `opens` naming nothing) is ignored whole, the
last good one kept, a warning once per connection per problem. Bounds:
`LINE_LIMIT` (1 MiB) a line, 1000 characters a string, 500 tabs, 50
other items, 2000 static rows, 200 rows an answer.

**Windows.** One connection per window and one bar per connection, as
now, so no message after the announce names its window. Several Flux
windows each send their own; focus swaps which shows (`AppContext`). A
dialog transient for an app window shows that window's bar. A single
window sends what fits it: one tab, `can: []`, no chords.

**Either side restarting.** The shell keeps nothing. An app that
connects (first time, after a shell restart, or when its platform window
is replaced) sends its `bar` whole after the announce, without waiting
for `attached`. When a connection closes the shell drops that window's bar,
its key grabs, an input or menu open for it, and any query pending.
`restarting` is sent as now, so an app waits longer before showing its
own fallback (Flux's line over the page).

**On Wayland** only the announce's `handle` and the shell's own side
change. The handle becomes the toplevel's (an xdg-foreign export, or
nothing at all once the shell is the compositor and knows its clients'
surfaces); focus comes from the compositor instead of
`_NET_ACTIVE_WINDOW`; chords are taken before the client sees them
instead of by X grabs; the drop-down is a popup of the bar's layer
surface. Messages, kinds and events stay as they are.

## 4. Inputs and menus

**Chords.** The chords of a bar's inputs and menus are the keys the shell
takes from that window, a chord being `ctrl`, `shift`, `alt`, `super` and
one letter or digit, as now. How is the shell's: on X11 a passive grab
on the window as `grab_keys` does now (every lock combination, by the
keymap, made again on MappingNotify and when the chords change; a
BadAccess said once and the key left to the app). A key the window
manager binds wins (X checks grabs from the root down), so an app cannot
take qtile's keys.

**Opening an input.** On the press the shell takes the keyboard (on X11
`hold_keyboard`, then a grab of its own for as long as the input is
open), and at the next frame the app's section becomes the text box: the
icon, the input's `text` (selected when `select`, so the first key
replaces it), the caret, the count, the toggles. Keys typed before the
shell's own grab (on X11 they arrive on the app's window) go into the
box in order (`on_typed`, today's type-ahead). The shell edits the text
itself (`TextInput`'s keys: words, selection, paste), so every key shows
at once with no round trip. Escape, or a click outside the bar and its
list, closes it, and the section shows the app's items again. It also
closes when its window loses focus or goes.

**Suggestions.** With `query`, the shell sends the text as it changes,
one query in flight per input: newer text waits, and only the newest goes
when the answer comes. No timer: a fast app sees every keystroke, a slow
one the latest text. `seq` rises per connection; an answer for a closed
input or an older seq is dropped. The list opens with the first answer;
until then, and between answers, the box echoes typing and the rows stay
as they were. Static `rows` (a command palette) the shell filters itself,
as `Choices` does now: the app's order with nothing typed.

```json
{"type": "query", "input": "address", "seq": 3, "text": "yo"}
{"type": "answer", "input": "address", "seq": 3, "selected": "https://www.youtube.com/",
 "total": 5321, "rows": [{"id": "typed", "tag": "SEARCH", "title": "yo"},
  {"id": "https://www.youtube.com/", "title": "YouTube", "detail": "youtube.com",
   "icon": "history", "match": [0, 1]}]}
```

- Latency. Flux answers from its own places: local SQLite, a few ms for
  a typical history, at most 40 ms at 10,000 pages (the budget
  `tests/browser_launcher.py` holds the shell to today); the socket adds
  well under a millisecond. The search leaves the shell's frames: a slow
  one delays the rows, never the typing. Flux may answer from a thread.
- Enter with the latest text unanswered waits for its answer (at most
  0.25 s) and submits the row selected then; with none by then, the text
  alone (`row: null`). Never a row from an older answer.
- The count at the box's right end is the answer's `count` (find's
  "3/12"), else the rows without a `tag` over `total`.
- A row action's chord on a row that lists it sends `act`; with `drops`
  the row leaves at once (Shift+Delete on a bookmark, as now) and the
  text is queried again: the answer is the truth (it may come back as
  history).
- With `keep`, Enter submits and the input stays (the editor's replace:
  Enter the next match, Shift+Enter, `shift: true`, all).
- With `steps` (find, vim-like, the user's design), Enter submits and
  turns the box to stepping: the text stays shown, typing stops, and `n`
  and `N` send `submit` again (`shift: false` / `true`: the next match,
  the one before). The keyboard stays the shell's, so `n` and `N` never
  reach the page. Enter, Escape or a click ends it (`closed`).

**Menus.** A menu drops under the item that opens it, flush with the
bar; opened by a chord, under the item that opens it if there is one (so
Alt+F and a click on File agree), else at the section's left edge. Up
and Down move, Return or a click picks (`pick`, then `closed`), Right or
Return on a row with `menu` opens the submenu beside it, Left or Escape
goes back. Typing filters with the shell's fuzzy match, the filter line
shown at the menu's top once something is typed: a short menu reads as
a menu, a long one is found by name. Key hints (`chord`) are drawn, not
taken: the app handles its own Ctrl+S.

**Opening without a key.** `open` is honoured only while that window or
its dialog has focus; otherwise, or with no top bar over the window, the
shell answers `fallback` and gives the keyboard back.

**The drop-down.** The bar is a dock of fixed height, so the list is a
second window against its bottom edge (`window.Placement`, as the
launcher drops from the bar now), on the bar's background with no edge
between: the section's width for an input, its own for a menu. The
launcher's rows, filter and steps move into it; its spawn effect becomes
the list unrolling from the bar.

## 5. The agent

The agent is the shell's: its voices, names and colours. `VOICES` moves
from `flux/protocol.py` to a pure `indigoshell/core/voices.py` (Flux
imports it for its in-page frame). An app names the agent by voice id:

- a tab it drives: `"agent": {"voice": "lilac", "state": "driving"}`:
  number, brackets and outline in the voice's colour; `paused` (the user
  took it over) dimmed a third of the way to the bar's background, as now.
- its name: a span in `"voice:lilac"` (the agent's own Flux window starts
  its bar so). In a row ("Let Lilac drive again") the app writes the
  label from `attached`'s `voices`, so any language can. Colours never
  travel; an unknown voice gets the default voice's.

## 6. Strict separation

- **Two sections.** The top bar stays `Row([AppContext(apps), *widgets,
  AgentChip()])`. Its own section is the user's: the widgets their config
  passes to `top_bar()`, then the AgentChip, a widget `top_bar()` always
  ends with. They take their natural width first; the app section gets
  what is left. An app cannot squeeze, cover or reach them, and they stay
  as they are while an input or menu is open.
- **Inside the app section** `end` takes at most a third (today's
  `HOST_CAP`), its leftmost text giving way first; `start` the rest,
  `tabs` taking what its other items leave. All is clipped to the
  section; an open input takes all of it, its list drops under it.
- **When.** Only while the window, or a dialog of it, has focus.
- **What an app cannot do:** draw pixels; pick fonts, sizes, hex colours,
  positions or animations; take focus or grab keys on any window but its
  own (or ones the window manager holds); open an input or menu while not
  focused; reach the bar's own section or the shell's panels. The socket
  is the user's alone (0600, as now): these bounds keep the bar coherent;
  they are not a sandbox.

## 7. Worked examples

**Flux**, three tabs, the second current and loading, the third driven by
the agent; the page a bookmark reached over http:

```json
{"type": "bar",
 "start": [{"kind": "tabs", "id": "tabs", "current": 41, "can": ["close", "move", "detach"],
            "tabs": [{"id": 40, "title": "Inbox"},
                     {"id": 41, "title": "std - Rust", "busy": true, "progress": 62},
                     {"id": 44, "title": "Weather", "agent": {"voice": "lilac", "state": "driving"}}]}],
 "end": [{"kind": "text", "id": "host", "spans": [{"icon": "bookmark"},
          {"icon": "unlocked", "color": "error"}, {"text": "doc.rust-lang.org"}]}],
 "inputs": {
   "address": {"chords": ["ctrl+l"], "icon": "search", "placeholder": "address or search",
               "text": "http://doc.rust-lang.org/std/", "select": true, "query": true,
               "actions": {"remove": {"chord": "shift+delete", "drops": true}}},
   "new_tab": {"chords": ["ctrl+t"], "icon": "search", "placeholder": "address or search",
               "query": true, "actions": {"remove": {"chord": "shift+delete", "drops": true}}},
   "find": {"chords": ["ctrl+f"], "icon": "search", "placeholder": "find", "query": true, "steps": true}},
 "menus": {
   "actions": {"chords": ["ctrl+m"], "rows": [
     {"id": "add", "title": "Add bookmark", "icon": "bookmark", "detail": "doc.rust-lang.org", "enabled": false},
     {"id": "remove", "title": "Remove bookmark", "icon": "bookmark", "detail": "doc.rust-lang.org"}]}}}
```

When the user takes tab 44 over, its agent state turns `paused` and
`actions` gains `{"id": "resume:44", "title": "Let Lilac drive again",
"icon": "play", "detail": "tab 3 · example.org"}` first. A bookmark's
row in an answer says `"actions": ["remove"]`.

The mouse: `{"type": "select", "item": "tabs", "tab": 44}`; a middle
click `{"type": "close", "item": "tabs", "tab": 40}`; a drag `{"type":
"move", "item": "tabs", "tab": 44, "before": 40}`; torn off `{"type":
"detach", "item": "tabs", "tab": 44}`.

Ctrl+L, then "yo" typed fast, then Return:

1. X11 hands the press to the shell, which holds the keyboard; the section
   becomes the box, the address selected. `query` seq 1, the address.
2. "y" (on Flux's window, before the shell's grab) replaces the
   selection; "o" comes through the grab. Both show at once; seq 1 is
   still out, so "yo" waits.
3. Flux answers seq 1 and the list drops. `query` seq 2 "yo"; Flux
   answers the typed row and YouTube, selected.
4. Return: `{"type": "submit", "input": "address", "text": "yo", "row":
   "https://www.youtube.com/", "shift": false}`, then `closed`. Flux
   loads it in the current tab (from `new_tab` it would open a tab).
5. Shift+Delete on a bookmark's row instead: `{"type": "act", "input":
   "address", "action": "remove", "row": "<its address>", "text": "yo"}`,
   the row gone at once, `query` seq 3 "yo" again.

Find: Flux answers each query with a count only, `"count": {"text":
"3/12"}` (`"no matches"` in `error`), while it highlights the page
(smartcase, as now). Enter turns the box to stepping; `n` and `N` step
(`submit`, `shift` false/true) and the page scrolls; Enter, Escape or a
click sends `closed` and Flux ends the find.

**An editor**, a Rust file unsaved, two errors:

```json
{"type": "bar",
 "start": [{"kind": "button", "id": "file", "label": "File", "opens": "file"},
           {"kind": "tabs", "id": "files", "current": "src/main.rs", "can": ["close", "move"],
            "tabs": [{"id": "src/main.rs", "title": "main.rs", "badge": {"icon": "unsaved", "color": "accent"}},
                     {"id": "Cargo.toml", "title": "Cargo.toml"}]}],
 "end": [{"kind": "text", "id": "diag", "spans": [{"icon": "error", "color": "error"}, {"text": "2", "color": "error"}], "press": true},
         {"kind": "text", "id": "branch", "spans": [{"icon": "branch", "color": "muted"}, {"text": "main"}]},
         {"kind": "text", "id": "pos", "text": "12:4", "color": "muted"},
         {"kind": "text", "id": "mode", "text": "rust", "color": "muted", "opens": "modes"}],
 "inputs": {
   "files": {"chords": ["ctrl+p"], "icon": "file", "placeholder": "go to file", "query": true},
   "palette": {"chords": ["ctrl+shift+p"], "placeholder": "command",
               "rows": [{"id": "fmt", "title": "Format Document", "chord": "ctrl+shift+i"}]},
   "find": {"chords": ["ctrl+f"], "icon": "search", "query": true, "keep": true,
            "buttons": [{"id": "case", "icon": "case", "on": false, "chord": "alt+c"},
                        {"id": "regex", "icon": "regex", "on": false, "chord": "alt+r"}]},
   "replace": {"chords": ["ctrl+h"], "icon": "replace", "placeholder": "replace with", "keep": true}},
 "menus": {
   "file": {"chords": ["alt+f"], "rows": [{"id": "open", "title": "Open…", "chord": "ctrl+o"},
            {"id": "recent", "title": "Open Recent", "menu": "recent"},
            {"id": "save", "title": "Save", "chord": "ctrl+s"}]},
   "recent": {"rows": [{"id": "/notes.md", "title": "notes.md", "detail": "~/notes"}]},
   "modes": {"rows": [{"id": "rust", "title": "Rust", "on": true}, {"id": "toml", "title": "TOML"}]}}}
```

A click on the error count sends `press` for `diag`; one on `rust` drops
`modes` under it. In `replace`, Enter replaces the next match and
Shift+Enter all, both `submit`. Ctrl+S never reaches the shell.

## 8. Migration

| today | becomes |
|---|---|
| `TabList`, `Glide`, `fit_slots`, `paint_*` (applink/flux.py) | the `tabs` kind, `applink/kinds/tabs.py`: any app's ids, `can`, `badge`, `busy`/`progress` per tab |
| `Host`, `Owner` | the `text` kind (spans) |
| `fit_find`, `paint_find` | the open input's box, `applink/kinds/input.py`, on `TextInput` |
| `FluxContent` | `applink/view.py`, `BarView`: widgets kept by id, `start`/`end` laid out, swapped for the open input |
| the launcher window and its list (`widgets/launcher.py`), `Mode`/`Choices` | the drop-down window under the bar: rows, fuzzy filter, steps, row actions; sources: static rows, an app's answers, the app catalog (MOD+D) |
| `FluxWindow._show`, `actions()` | `flux/bar.py`, pure: Flux's state to its `bar` |
| `FluxWindow.on_key`, `_open`, `placement` | the generic `AppWindow` and the input's opening in service.py |
| `services/addresses.py` (`AddressMode`) | `flux/addresses.py`, pure: Flux's answers to `query`; the shell no longer imports `flux.places` |
| Flux's own find keys in the page | the `find` input |
| `VOICES`, `voice_*` (flux/protocol.py) | `indigoshell/core/voices.py` |
| Flux's `state`, `launcher`, `download`; the shell's commands to Flux | `bar`, `open`, `answer`; the events (`download`, only logged today, goes) |
| the announce's `wid`, `place`'s X `with`, `detach`'s `x`, `y` | `window` (Flux's window ids, as its control socket gives them) and `handle`; `with` by window id; no coordinates |
| `AppService(kinds={"flux": FluxWindow})` | `AppService()` |

Steps, each one working:

1. **The vocabulary.** `applink/bar.py`, pure (apps import it): names,
   builders, `bar_problems`. Tests of its own; nothing else changes.
2. **The kinds.** The tabs' and host's drawing and mouse code moves into
   `applink/kinds/` behind `BarView`; `FluxWindow` turns Flux's `state`
   into a bar dict in the shell for now. Same pixels: `tests/topbar.py`
   4 and 4b build bar dicts instead of Flux states against the same
   oracles; the mouse tests record events with ids and `before`.
3. **The input and the drop-down**, made from the launcher's parts; Flux's
   Ctrl+L/T/M go through them, `AddressMode` still in the shell as a
   stand-in source. `tests/browser_launcher.py` 3 and 4 drive them with
   KeyEvents (echo, type-ahead, one query in flight, stale answers
   dropped, Enter waiting, a dropping action, menus filtered and
   stepped); its first-frame check becomes the box within a frame.
4. **The switch**, one commit on both sides: the generic `AppWindow`
   (`bar`, `open` in; events out; grabs from the chords); Flux sends what
   `flux/bar.py` makes of `window.state()`, answers with
   `flux/addresses.py` and its find; `FluxWindow`, `kinds`, the stand-ins
   and Flux's old messages go. `tests/topbar.py` 5 and 5b drive a fake
   app over the socket (a bar shown and replaced, a bad one keeping the
   last good, chords regrabbed when they change, all dropped with the
   connection); `tests/browser_launcher.py` 2 and 5 test
   `flux/addresses.py` and `flux/bar.py`, pure; `tests/browser.py`
   follows; `tests/flux_imports.py` allows `indigoshell.core.voices`.
5. **MOD+D** onto the same component, the app catalog as its source: the
   focused monitor's top bar app section becomes the app search box (an
   app's bar, if one shows, gives way until it closes), its list dropping
   under it like any input's.
6. **Docs**: applink's and flux's package docstrings, the README.

## 9. Decisions (the user's, 2026-11-25)

1. **Find is a bar input, vim-like:** type (live count), Enter, then `n`
   / `N` step and never reach the page; Enter, Escape or a click ends it
   (`steps`, section 4).
2. **Typing in a menu filters it**, its line shown once something is typed.
3. **The input's list is the app section's width**: the bar growing down.
4. **The app section gets what the bar's own widgets leave.**
5. **One tab look for every app**; another is a shell change, not an
   app's choice.
6. **Icons by name**, the shell's table grown as apps need.
7. **MOD+D opens in the top bar** too: the focused monitor's app section
   becomes the app search box (step 5).
