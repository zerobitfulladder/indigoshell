"""What Flux (the browser) says to the shell and to the agent.

Pure Python: imported by the browser (Qt) and by the agent's side
(control.py, hands.py), so it imports no toolkit.

The shell. Flux is an indigoshell app: each window connects to the
shell's app socket and speaks the app bar protocol
(indigoshell/applink/PROTOCOL.md; its names and builders in
indigoshell/applink/bar.py): it announces itself as a "flux" window,
its own id for it and its X window (`announce`), then sends its section
of the top bar whole whenever it changes (`bar`, made of the window's
state by bar.py: its tabs, the page's host (a `fixed` text), the
`address`, `new_tab` and `find` inputs, the `actions` menu). A tab names
its page's favicon by id (`image`), or gives its host's first letter
while the page has none; the favicon itself goes before the first bar
that names it, once, as an `image` message: a PNG at most 32 px a side
(qt/favicon.py), its id the PNG's hash, sent again only after a bar
went without it or on a new connection (qt/link.py; PROTOCOL.md,
"Images"). The icon goes with its site: kept across a site's pages,
dropped for a page on another host (qt/tabs.py). It answers the queries
of its inputs (`answer`: its places, addresses.py; find's count) and
acts on the bar's events, which name its tabs by id. A main window's keys
(Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M) are the chords of its inputs and menu,
which the shell takes from it; a single window (a page's sign-in popup,
the inspector) sends one tab and no inputs, so its keys stay its own.
With no shell, or one that cannot show an input (`fallback`), those keys
reach the window and it answers them itself: a dialog for an address,
its own find over the page. The agent's own window ("Alone", below)
asks the shell to `place` it before it is first shown, and its bar
starts with the agent's name in her colour.

Single instance: the running browser also listens at
`instance_socket_path()`, guarded by an flock on `instance_lock_path()`,
one of each per profile (profiles.py: a named profile's browser is
another single instance, beside the default one).
A second invocation sends {"type": "open", "urls": [url, ...]} (an
empty list asks for a new window) and waits for {"type": "ok"}; a
request it cannot take is answered {"type": "error", "error": str}.
A line with no `id` is such a hand-off: answered once, then the line is
closed (also, without an answer, for a type that is not `open`).

Control: the same socket, one per browser process, is how the agent
drives the browser (flux/control.py is the client, flux/qt/
control.py the browser's side). A connection stays open for as many
requests as it likes. Every request carries an `id`, an int the client
picks, unique among its requests in flight on that connection; several
may be in flight, and each is answered once, in any order:

    {"type": <request>, "id": N, ...}
    {"type": "result", "id": N, "ok": true, ...}
    {"type": "result", "id": N, "ok": false, "error": str}

    cancel     {"type": "cancel", "id": N}
               Stops request N. If it was still running it is answered
               ok:false, error "cancelled"; otherwise nothing is.

A type the browser does not know is answered ok:false. Times are
seconds. Tabs and windows are named by ids the browser gives them, ints
that are never reused within one browser process; a tab's id is its
page's, so it stays the same when the page moves to another window.
Windows are listed most recently used first.

TAB, as several results give it:
    {"tab": id, "window": id, "index": <0-based, in its window>,
     "title": str, "url": str, "loading": bool, "current": bool}
    `current`: the tab its window shows.

**Showing.** The agent works only on what the user could see: every
request that reads, shows or acts on a tab (close, go, wait, read,
screenshot) refuses a tab that is not the current one of its window,
ok:false with the error NOT_SHOWING ("tab N is not showing in its
window; switch to it first"); a wait whose tab stops showing ends so.
A window on another workspace still counts as showing: its pages stay
live. `switch` makes a tab current without raising its window, taking
focus or changing the workspace; `open` and `tabs` work on any.

    tabs       {}
               -> {"windows": [{"window": id, "xid": <X window, or null>,
                                "single": bool, "focused": bool,
                                "tabs": [TAB, ...]}, ...],
                   "focused": <the focused window's id, or null>,
                   "headless": bool, "profile": <its name, or null for
                                                 the default one>}
               `single`: a page's popup or the inspector (one page, takes
               no tabs). `xid` is the window manager's id for the window
               on X11; null on Wayland, where a client has no such id.
               `headless`: Flux runs with no screen ("Headless", below).
    open       {"url": str, "window"?: id | "agent", "background"?: bool}
               A new tab at the end of that window (a main one), or of
               the most recently used main window of the user's (one is
               made when there is none), or with "agent" of the agent's
               own window, made (and placed) first when there is none
               ("Alone"). It becomes the current tab unless `background`.
               -> TAB. Answered once the load is asked for; `wait` for it.
               `url` is anything the address line takes (a search too).
    switch     {"tab": id}                         -> TAB
    close      {"tab": id}                         -> {"tab": id, "window_closed": bool}
               The window closes with its last tab, and the browser
               with its last window.
    go         {"tab": id, "url": str}
               {"tab": id, "action": "back" | "forward" | "reload"}
               -> TAB, once the navigation is asked for.
    wait       {"tab": id, "timeout"?: s}                 until it has loaded
               {"tab": id, "selector": str, "timeout"?: s}  ... until the CSS
                                                          selector matches
               {"tab": id, "text": str, "timeout"?: s}    ... until the text
                                                          is on the page (as
                                                          rendered, case kept)
               -> TAB + {"waited": s}; ok:false "timed out after N s ..."
               when not by `timeout` (default WAIT_DEFAULT_S, at most
               WAIT_MAX_S). A load asked for a moment ago and not yet
               begun counts as loading.
    read       {"tab": id, "what": one of READS, "offset"?: int,
                "length"?: int, "hidden"?: bool}
               -> TAB + {"what", "offset", "total", "more": bool,
                         "next": <offset of the rest, or null>,
                         "frames": [FRAME, ...], ...}
               The page and the frames in it ("Frames", below), visible
               elements only unless `hidden` (links, forms, outline,
               article, and which frames are read).
               text      {"text": str}: the text as rendered, characters
                         [offset, offset + length) of `total`
                         (length: TEXT_LENGTH, at most TEXT_MAX); each
                         frame's text after the page's, between the lines
                         "[frame f2: <its element> -> <address>]" and
                         "[end of frame f2]"
               article   {"article": str, "found": str}: the page's main
                         content as Markdown (headings, paragraphs, lists,
                         links as [text](absolute URL), emphasis, code,
                         block quotes, simple tables, images as
                         ![alt](URL)), without navigation, sidebars,
                         footers, forms, fixed banners (cookie notices);
                         characters [offset, offset + length) as text.
                         `found`: where the content was found: "the page's
                         <article>", "the page's <main>", "the densest
                         block (<tag#id.class>)" or "the whole page". A
                         frame inside the content is in it where it sits,
                         between "[frame f2 -> <address>]" and "[end of
                         frame f2]" lines.
               links     {"links": [{"ref", "text", "url" (absolute)}]},
                         links [offset, offset + length) of `total`
                         (LINKS_LENGTH, at most LINKS_MAX)
               forms     {"forms": [{"ref" (null for the fields outside
                         any form), "name", "action", "method",
                         "fields": [{"ref", "label", "type", "name",
                         "value", "required": bool, "disabled": bool,
                         "checked"?: bool, "options"?: [{"value", "text",
                         "selected"}]}]}]}, forms [offset, ...) of `total`
               outline   {"outline": [str, ...]}: the page's meaningful
                         elements, one line each, indented under the
                         landmark that holds them:
                             navigation "Main"
                               [14] link "Orders" -> /orders
                             heading 2 "Your account"
                             [12] button "Sign in"
                             [13] textbox "Email" = "a@b.example"
                         lines [offset, offset + length) of `total`
                         (OUTLINE_LENGTH, at most OUTLINE_MAX)
               A password field's value is never read: it reads
               PASSWORD_VALUE when it holds one. `[N]` and "ref" are
               REFs for elements, the same for an element in every read
               until its document is replaced; they are kept where the
               page cannot see them (an isolated script world).
    screenshot {"tab": id, "full"?: bool}
               -> TAB + {"png": <base64>, "width", "height" (as sent),
                         "source_width", "source_height" (as rendered),
                         "viewport_width", "viewport_height" (CSS px)}
               The visible part of the page, after a frame painted since
               the request came; its long side scaled down to at most
               SHOT_MAX_SIDE.
               With `full`: the page from its top, as viewport-high
               TILES, at most SHOT_TILES_MAX, scrolled to and grabbed in
               turn, the page's scroll put back after:
               -> TAB + {"tiles": [{"png", "width", "height",
                                    "source_width", "source_height",
                                    "x", "y"}, ...],
                         "page_width", "page_height", "viewport_width",
                         "viewport_height" (CSS px), "more": bool}
               A tile's `x`, `y`: the page's scroll when it was grabbed
               (CSS px), so its pixel (px, py) is the page's point
               (x + px * viewport_width / width, y + py * viewport_height
               / height); the last tile may overlap the one before (the
               page scrolls no further). `more`: the page goes on below
               the last tile. Elements fixed or sticky on the page (a
               header that stays, a cookie banner) are hidden from the
               second tile on, by a style sheet adopted from Flux's
               isolated world, taken away after: no element, attribute or
               class of the page changes and no mutation is seen; only a
               page listing document.adoptedStyleSheets meanwhile would
               see it (Qt has no user style sheet to put it in). An inner
               box that scrolls by itself is shown as it stands.
    look       {"window"?: id, "pid"?: int, "xid"?: int, "screenshot"?: bool,
                "image"?: bool}
               What the user is looking at in the browser, changing
               nothing they see: the focused window, or with none the one
               that had focus last (or the window named). `pid`: the
               process the named `window` is in, as the shell knows it
               (on Wayland the call names the window it saw focused by
               its app bar id and its connection's process): a window of
               another Flux's is not this one's to look at, and the look
               goes on as if none was named. `xid` (X11): the X window
               the call saw focused; on Wayland it fails (no X window).
               -> TAB + {"focused": bool, "unfocused_for": <s, or null
                         while focused>, "selection": str,
                         "selection_in": "page" | "field" | "frame" | null,
                         "screenshot"?: {"png", "width", "height",
                                         "source_width", "source_height"},
                         "image"?: null | {"png", "width", "height",
                                   "source_width", "source_height",
                                   "under": "selection" | "pointer",
                                   "url": str, "alt": str,
                                   "from": "page" | "screen"}}
               `selection`: the text the user selected (at most 200,000
               characters), in the page, a text field (never a password
               field) or a frame (where Qt has frames, 6.8 on); "" for
               none. `image`: the image in the selection, else the one
               under the mouse pointer: its own pixels where the page may
               read them ("page"; a cross-origin image without CORS it
               may not), else the part of the screen it covers ("screen";
               a selected image is drawn tinted there).
               Headless, the window is the one named, else the most
               recently used, never focused, and the result says
               {"headless": true}; `xid` fails (no X window), and `image`
               is only ever the selection's (no mouse pointer).

**Headless.** `--headless` (flux/qt/__main__.py) runs Flux with no
screen: Qt's offscreen platform, drawn in software. Its windows exist and
render (screenshots, reads and acting work as on screen), but no one
sees them: no line to the shell (no `announce`, no top bar, no fallback
line, no placing; the agent's own window shows at once), no window has
focus, no window has an X window (`xid` null), the page effects are
never made, audio is muted, and what would ask the user is answered at
once: a permission request denied, a page's dialog outside the agent's
session dismissed (an alert accepted; a confirm, a prompt and the
question before leaving a page answered no), a file chooser outside an
upload given nothing, no address dialog, fullscreen refused. Its last
window closing does not end it (a service's browser waits for the next
`open`); SIGTERM does. `read` reads the page's frames too (iframes, frames, an
<object>'s page; same-origin and cross-origin, nested), through Qt's
QWebEngineFrame (6.8 on; with an older Qt, frames are not read): each
in its own isolated world, as the page is. A frame's document is named
"f<N>", a number Flux gives it in its tab and never gives another; a
frame that loads another document is named anew (an old "f2:14" then
says there is no frame f2: read again, as after the page's own load).
An element in a frame has the REF "f<N>:<M>" (the main page's stay plain
numbers), in every read and in every acting request, which finds it in
its frame and sends real input to it there: the frame's point plus its
element's place in each frame above it (scrolled into view there when it
is not, refused when something covers it). A point (`x`, `y`) over a
frame is acted on in the frame (what is there, refused fields). In the
outline the frame's lines sit under its element's line, which ends
"(frame f2)", or "(not read: why)"; text, links and forms put each
frame's after the page's; article puts a frame inside the content where
it sits. Every read says each frame it met (and `frames_more`, a count,
when there were more than it looks at):
    FRAME      {"frame": "f2" | null (it did not answer), "in": "f1" |
                null (the main page), "url": str (cut to the site for a
                kept one), "read": bool, "why"?: str}
A frame is matched to its element by identity, not by guessing: the
frame's own script world finds which of its parent's windows is its own
(`parent[i] === window`, allowed across origins), the parent's which
element holds that window (`window[i] === el.contentWindow`). A frame
whose element is not found is read after its parent's content, said so.
Not read: a frame at a host the user keeps from the agent ("Sites kept
from the agent": only its place is asked, nothing of its content), one
that does not answer within a moment, more than FRAMES_MAX in all or
deeper than FRAMES_DEPTH. `wait` and `js` work in the main page only.

**Acting.** On a tab that is showing, by element REF (from `read`: the
outline's [N], a link's or a field's "ref"; refs of the document shown
now, gone with it; "f2:14" in frame f2, "Frames") or by viewport POINT:
`x`, `y` in CSS pixels of the visible viewport, (0, 0) its top left. A
screenshot's pixel (px, py) is
the point (px * viewport_width / width, py * viewport_height / height)
(`screenshot` gives both sizes; they differ when it was scaled down, or
the page is zoomed). The input is real: Qt mouse, key and wheel events
sent to the page's view, which the page sees as trusted, the same as
the user's. Every acting request answers
    TAB + {"navigated": bool, "opened": [TAB, ...], "dialog": DIALOG | null}
once the page settled: a navigation it caused began (`navigated`; then
`wait`), a dialog opened, or SETTLE_S passed quietly. `opened`: tabs the
action opened (a link to a new tab, a popup window).

    click      {"tab", "ref" | "x", "y", "button"?: "left" | "middle",
                "double"?: bool}
               -> + {"x", "y", "what": str}: where it clicked, and what
               is there (a ref, its role and name, as the outline says).
               A ref is scrolled into view first; the click goes to the
               middle of its visible part, and only when the element (or
               something inside it, or its label) is what is there: else
               ok:false naming what covers it. No right clicks (a context
               menu would take the user's pointer). Refused: a file field
               (`upload` gives it files), a <select> (`select` it), a date,
               time or colour field (their pickers take the pointer too;
               `fill` them).
    type       {"tab", "text", "ref"? | "x"?, "y"?, "clear"?: bool,
                "submit"?: bool}
               Into the field `ref` (focused, the caret at its end), at
               the point (clicked first), or into what has focus. `clear`
               empties it first (Ctrl+A, Backspace); `submit` presses
               Enter after. A newline is Enter and a tab Tab, as typed.
               Up to TYPE_KEYS_MAX characters go as key presses (each
               with its own keydown, as typed); a longer text as one input
               method commit between them (measured: 2100 characters in
               2.4 s as keys, 0.05 s as a commit).
               -> + {"typed": n, "via": "keys" | "input method"}
    key        {"tab", "key": "Enter" | "ctrl+a" | "shift+Tab" | "PageDown"
                | "a" ..., "count"?: 1..KEY_COUNT_MAX}
               To what has focus in the page; KEY_NAMES has the names.
    select     {"tab", "ref", "value"? | "label"?}
               An option of a <select> (a list for a multiple one), set
               in the agent's world with input and change events after:
               never its popup, which would take the user's pointer.
               -> + {"value", "label"}
    scroll     {"tab", "dy"?, "dx"?: px, "ref"? | "x"?, "y"?}
               Real wheel turns over the element, the point, or the
               middle of the page, by about that many pixels (down,
               right positive);
               {"tab", "to": ref}  that element scrolled into view.
               -> + {"scroll_x", "scroll_y"} (the page's, after)
    fill       {"tab", "fields": [{"ref", "value"}, ...]}
               Each in turn: a text field emptied and typed into (as
               `type`), a checkbox or radio clicked to `value` (bool), a
               <select> as `select`, a date, time, colour or range field
               set in the agent's world with input and change events.
               -> + {"fields": [{"ref", "ok": bool, "error"?: str}]}
    hover      {"tab", "ref" | "x", "y"}
               The pointer moved there (menus that open on hover).
               -> + {"x", "y", "what"}
    dialog     {"tab", "accept": bool, "text"?: str}
               Answers the page's dialog held for the agent (below);
               `text` is a prompt's answer.
    release    {"tab"?}
               The agent lets go of the tab (all its tabs with none).
               -> {"released": [tab, ...]}

**Watch.** An acting request starts the agent's SESSION on its tab, or
goes on with it: the tab's input is the agent's while the user watches.
The user's mouse and keys to that tab are swallowed (the agent's own
events go on; the user's are the window system's, spontaneous, and they
come through the window, where they are stopped); a frame and a mark
in the agent's colour (indigoshell/core/persona.py) sit over the page
(not in any screenshot). Other
requests on the tab (read, screenshot, wait, go) keep a session going
but never start one. A session ends QUIET_S after the last request on
its tab ended (not while a dialog is held), on `release`, when its tab
closes, or when its window shows another tab.

TAKE-OVER: a key the user presses in that window, or a click on the
mark, ends the session at once and PAUSES the agent on the tab; the key
reaches nothing. Every request on a paused tab, and every one that would
change what its window shows (`switch` to another tab there, `open`
there not in the background), fails with TOOK_OVER, "the user took over
tab N". The pause ends when the user hands the tab back ("Let Lilac
drive again" in the window's actions on the shell's top bar) or PAUSE_S
after their last key,
click or wheel turn in that tab. Other clicks on the locked page are
swallowed, and the mark flashes.

DIALOG: {"type": one of DIALOG_TYPES, "message": str, "default": str}.
While a session drives the tab, a dialog the page opens (alert, confirm,
prompt, and leaving a page that asks first: "beforeunload") is held for
the agent instead of shown: every result naming the tab says it (TAB's
`dialog`), requests that need the page refuse until `dialog` answers it,
and one left DIALOG_S unanswered is dismissed. On a take-over or a
`release` it becomes the user's own dialog, shown as without the agent.
A file chooser the page opens while the agent drives is refused, but
the one `upload` asked for ("Uploads"). Outside a session the page's
dialogs are the user's, as ever.

TAB, in every result since acting came, also says {"agent": null |
"driving" | "paused", "dialog": DIALOG | null}.

**Alone.** The agent's own window, a main window like the user's, in
their sight only when they go and look. `open` with "window": "agent"
and `borrow` make it when there is none (one at most). Before it is
first shown Flux asks the shell to put it on an empty workspace no
screen shows (applink `place`), so it never appears where the user is
looking and their view never moves (on niri, where nothing can be
placed before it maps, the shell moves it there as niri lists it, and
docs/niri.kdl's window rule keeps it from taking the focus meanwhile);
with no shell (or one too old to answer within applink's PLACE_WAIT_S)
it shows where the window manager puts it. Flux never raises it or
gives it focus. Its title is "<her name> · Flux" and its page's, its
WM_WINDOW_ROLE AGENT_ROLE, its class Flux's own; its bar starts with
her name in her colour. The showing rule, the
Watch's lock and take-over hold in it as in any window. A popup a page
in it opens goes to the same workspace, and the tabs such a popup asks
for to the agent's window.

    borrow     {"tab": id}
               A tab of the user's moves into the agent's window (made if
               need be) as its current tab: its page and everything in it
               (a half-filled form, the scroll, the page's scripts and
               cookies: the same page, in a new view). Any tab of the
               user's main windows, showing or not; not one the user took
               over, one whose page has a dialog open, a popup's, or one
               in an agent's window already. A window's last tab leaves a
               blank tab behind (a window holds one at least), closed
               again when the tab comes back.
               -> TAB + {"from": {"window": id, "index": int}}
    return     {"tab": id, "show"?: bool}
               A tab in the agent's window goes to the user: a borrowed
               one back to its window at its old place; one whose window
               is gone, and one the agent opened itself, to the end of the
               user's most recently used window. In the background there
               unless `show` (then that window's current tab, the window
               neither raised nor focused, the workspace kept); refused
               when the user has no window. The agent's session on it
               ends; its window closes with its last tab.
               -> TAB + {"to": "its window" | "another window"}

While away, a borrowed tab is gone from its window's tab list (and the
top bar's): nothing stands in its place. When the agent's window closes
(the user, the window manager), its borrowed tabs go back as `return`
sends them (to a new window of the user's when none is left); the tabs
it opened itself close with it. TAB also says {"borrowed": null |
{"window": id, "index": int}}: where a tab in the agent's window came
from. Each window in `tabs` also says {"agent": bool}: whether it is
the agent's own.

**Show.**
    show       {"path": str}
               A local file (an absolute path, or a file:// URL): an HTML
               report with what it links beside it (images, stylesheets),
               a PDF, an image. A new tab at the end of the user's most
               recently used window, its current tab (in the background
               when the user has taken over the tab that window shows),
               the window neither raised nor focused, the workspace kept;
               a new window when the user has none. Anything that is not
               a file is refused.
               -> TAB + {"background": bool}

**Downloads.** Flux takes every download into the profile's download
folder, no question asked.
    downloads  {} -> {"downloads": [DOWNLOAD, ...]}  newest first, this
               run's, at most DOWNLOADS_KEPT
    wait       {"download": id, "timeout"?: s}
               until it has finished (done, failed or cancelled)
               -> DOWNLOAD + {"waited": s}; ok:false "timed out ..." as
               the other waits.
    DOWNLOAD   {"download": id, "name": str, "path": str, "state": one of
                DOWNLOAD_LIST_STATES, "percent": <0..100, or null>,
                "received": <bytes>, "total": <bytes, or null>, "host": str
                (where from), "tab": <the tab it started in, or null>,
                "error"?: str}
An acting request's answer lists the downloads it started, when it did
(`downloads`: [DOWNLOAD, ...]).

**Quit.**
    quit       {} -> {"quitting": true}
               Flux ends right after this answer, as on SIGTERM: its
               windows closed, the profile released, the socket gone; its
               log says "ending: a quit request". For a process that
               started a Flux of its own (a hub's headless one) and is done
               with it. Not one of the agent's hands: the agent never
               closes the user's browser.

**Uploads.**
    upload     {"tab", "ref", "paths": [str, ...]}
               An acting request: the files reach the page as from its
               file chooser, with no dialog. `ref` is a file field, or
               what opens one (a button, a drop zone's "browse"): Flux
               arms the answer to the next chooser the page opens with the
               paths, then clicks with real input (the field, or its label
               when the field is hidden; a hidden field with no label gets
               a key press the page can open a chooser after, then a click
               in the agent's world). Every path is checked first: a
               readable file, under the home or /tmp, in no place secrets
               are kept (flux/uploads.py has the list); several paths
               for a field that takes one are refused too.
               -> + {"files": [{"name", "size"}], "field": <what got them,
                     as the outline names it, or null when the page's own
                     chooser took them>}
               ok:false (NO_CHOOSER) when the page opened no chooser.

**Scripts.**
    js         {"tab", "code": str, "timeout"?: s}
               An acting request: the agent's JavaScript, run in the
               page's main frame in an isolated world of its own (not the
               page's, not the reader's), as the body of an async
               function (`return` its answer; `await` works) or as one
               expression (code of one line is tried as one first). A
               promise it returns is awaited, by polling,
               up to `timeout` (JS_TIMEOUT_S, at most JS_TIMEOUT_MAX_S).
               -> + {"result": <its answer as JSON text, or null>,
                     "result_type": <typeof the answer, "array" and
                     "null" told apart>, "length": <characters of
                     JSON>, "cut": bool (at JS_RESULT_MAX),
                     "threw"?: str, "pending"?: true, "withheld"?: true}
               `threw`: what it threw ("TypeError: ..."); `pending`: no
               answer by `timeout` (or a dialog opened first, or the page
               went on to another document first), the code may still be
               running; `withheld`: it answered, but the page stopped
               answering (it left for another document) before the answer
               could be checked for passwords, so it is not given. Nodes
               come back as "<tag#id.class>", functions as "[function
               name]".
               THE GUARD, in that world only, before the code: reading or
               setting document.cookie, localStorage, sessionStorage,
               indexedDB, caches, cookieStore, navigator.credentials (and
               navigator.storage, navigator.serviceWorker), window.open,
               opener, Worker, SharedWorker, and a frame element's
               contentWindow, contentDocument and getSVGDocument throw; a
               password field's value reads PASSWORD_VALUE (as `read`
               says it), FormData gives PASSWORD_VALUE for one, and a
               password field's type cannot be changed from there. Every
               frame's world of the same number is guarded first. The
               answer then passes through the reader's world of each
               frame holding a password field, where every such field's
               value (JS_SCRUB_MIN characters or more, as typed and as
               JSON writes it) is replaced by PASSWORD_VALUE. A guard
               against mistakes and casual misuse, NOT a sandbox: the code
               reads the page's DOM, runs with the page's origin (fetch
               sends the page's cookies; what comes back is the code's),
               can click and submit, and a frame made after the guard ran
               is reachable as window[N] unguarded. A loop that never ends
               hangs the page. The page's own scripts keep all of it.

**Sites kept from the agent.** sites.json (flux/qt/identity.py) may
name hosts the agent stays out of: {"agent": {"<host or *.domain>":
"never"}}. Every request on a tab at such a host, and `open`, `go` and
`show` to one, fails with KEPT ("the user keeps <host> to themselves");
`release` and `return` still work. `tabs` lists such a tab with no
title and its address cut to the site ({"kept": true}); `look` there
fails the same. No host is "never" unless the user writes it.
"""

import os
import re

from indigoshell.applink.protocol import (  # noqa: F401  (part of this module's API)
    ANNOUNCE, ATTACHED, RESTARTING, SOCKET_ENV, TYPE, decode, encode, message, socket_path)
from flux import profiles
from indigoshell.core.naming import APP, env
from indigoshell.core.paths import runtime_dir
from indigoshell.core import persona


# ── where ───────────────────────────────────────────────────────────────
INSTANCE_SOCKET_ENV = env("BROWSER_SOCKET")


def instance_socket_path(profile: str | None = None) -> str:
    """Where the running browser takes URLs from later invocations; a
    named profile's browser (profiles.py) at the same with "-NAME"
    before ".sock"."""
    default = os.environ.get(INSTANCE_SOCKET_ENV) or os.path.join(
        runtime_dir(), f"{APP}-browser-{os.getuid()}.sock")
    return profiles.socket_path(default, profile)


def instance_lock_path(profile: str | None = None) -> str:
    return instance_socket_path(profile) + ".lock"


def control_socket_path(profile: str | None = None) -> str:
    """Where the running browser takes the agent's requests: the same
    socket (one per browser process)."""
    return instance_socket_path(profile)


# ── names ───────────────────────────────────────────────────────────────
KIND = "flux"                   # the app Flux announces
TAB_ID = "tab"                  # a tab's id, in the agent's results
OPEN = "open"
URLS = "urls"
OK = "ok"
ERROR = "error"

AGENT = "agent"
AGENT_STATES = ("driving", "paused")


def agent(state_: str) -> dict:
    """A tab's agent hold, as the window's state and the bar carry it."""
    return {"state": state_}


def open_urls(urls: list[str]) -> dict:
    return message(OPEN, **{URLS: list(urls)})


# ── control (the agent's requests) ──────────────────────────────────────
ID = "id"
RESULT = "result"
CANCEL = "cancel"
CANCELLED = "cancelled"
ACTS = ("click", "type", "key", "select", "scroll", "fill", "hover", "dialog")
UPLOAD = "upload"               # acting too, with files
ALONE = ("borrow", "return", "show", "downloads")
JS = "js"                       # acting too: the agent's own script
QUIT = "quit"
REQUESTS = ("tabs", "open", "switch", "close", "go", "wait", "read", "screenshot", "look",
            *ACTS, "release", UPLOAD, *ALONE, JS, QUIT)
GO_ACTIONS = ("back", "forward", "reload")
READS = ("text", "links", "forms", "outline", "article")

NOT_SHOWING = "tab {tab} is not showing in its window; switch to it first"
PASSWORD_VALUE = "(password)"

WAIT_DEFAULT_S = 30.0
WAIT_MAX_S = 600.0
TEXT_LENGTH, TEXT_MAX = 20000, 100000          # characters
LINKS_LENGTH, LINKS_MAX = 200, 1000
FORMS_LENGTH, FORMS_MAX = 20, 100
OUTLINE_LENGTH, OUTLINE_MAX = 400, 2000        # lines
ARTICLE_LENGTH, ARTICLE_MAX = 20000, 100000    # characters
SHOT_MAX_SIDE = 1568                           # px, the long side
SHOT_TILES_MAX = 6                             # a full-page screenshot's tiles

# ── frames, scripts ─────────────────────────────────────────────────────
FRAMES_MAX = 50                 # frames read in one request, at most
FRAMES_DEPTH = 8                # frames inside frames, this deep at most
JS_TIMEOUT_S = 10.0             # what `js` waits for a promise, by default
JS_TIMEOUT_MAX_S = 60.0
JS_CODE_MAX = 100000            # characters of code
JS_RESULT_MAX = 100000          # characters of the answer's JSON
JS_SCRUB_MIN = 4                # a password this long or longer is taken out of answers

_FRAME_REF = re.compile(r"f([1-9][0-9]*):([0-9]+)")


def parse_ref(value) -> tuple[int | None, int] | None:
    """A REF as (frame number, the element's number in it): 14 is
    (None, 14), "f2:14" is (2, 14); a number as a string goes too. None
    for what is no ref."""
    if is_id(value):
        return None, value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return None, int(text)
        m = _FRAME_REF.fullmatch(text)
        if m:
            return int(m[1]), int(m[2])
    return None


def is_ref(value) -> bool:
    return parse_ref(value) is not None


def ref_name(frame: int | None, n: int) -> str:
    """"14", or "f2:14" in frame f2."""
    return f"f{frame}:{n}" if frame else str(n)


def frame_name(frame: int) -> str:
    return f"f{frame}"

# ── alone, downloads, uploads, kept sites ───────────────────────────────
AGENT_WINDOW = "agent"          # `open`'s window: the agent's own
AGENT_ROLE = f"{APP}-agent"     # its WM_WINDOW_ROLE
AGENT_TITLE = "{name} · Flux"
DOWNLOAD_LIST_STATES = ("running", "done", "failed", "cancelled")
DOWNLOADS_KEPT = 50
UPLOAD_PATHS_MAX = 20
UPLOAD_WAIT_S = 3.0             # for the chooser the click should open
KEPT = "the user keeps {host} to themselves"
AGENT_POLICIES = ("never",)
FILE_FIELD = "{what} is a file field: give it files with upload"
FILES_REFUSED = "the page asked for files and got none: only upload gives a page files"
NO_CHOOSER = ("{what} opened no file chooser: give upload the file field's ref (read with "
              "hidden finds a hidden one) or what opens its chooser")


def kept(host: str) -> str:
    return KEPT.format(host=host or "this site")


def agent_title() -> str:
    return AGENT_TITLE.format(name=persona.name())


# ── acting, and the Watch ───────────────────────────────────────────────
TOOK_OVER = "the user took over tab {tab}"
DIALOG_TYPES = ("alert", "confirm", "prompt", "beforeunload")
QUIET_S = 10.0          # a session with no request on its tab this long ends
PAUSE_S = 120.0         # a take-over's pause, after the user's last input in the tab
DIALOG_S = 60.0         # a dialog held for the agent this long unanswered: dismissed
SETTLE_S = 0.4          # after acting: this long quiet, and the page counts as settled
TYPE_KEYS_MAX = 200     # characters typed as key presses; a longer text is committed
KEY_COUNT_MAX = 50      # `key`'s count
BUTTONS = ("left", "middle")
# The keys `key` names (case does not matter), besides one character:
KEY_NAMES = ("Enter", "Escape", "Tab", "Backspace", "Delete", "Space", "Insert",
             "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
             "PageUp", "PageDown", *(f"F{n}" for n in range(1, 13)))
KEY_MODIFIERS = ("ctrl", "shift", "alt", "meta")

def took_over(tab: int) -> str:
    return TOOK_OVER.format(tab=tab)


def dialog(type_: str, message_: str, default: str = "") -> dict:
    return {"type": type_, "message": message_, "default": default}


def is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def request(type_: str, id_: int, **args) -> dict:
    return message(type_, **{ID: id_}, **args)


def result(id_, **fields) -> dict:
    return message(RESULT, **{ID: id_, "ok": True}, **fields)


def failure(id_, error: str) -> dict:
    return message(RESULT, **{ID: id_, "ok": False, "error": str(error)})


def not_showing(tab: int) -> str:
    return NOT_SHOWING.format(tab=tab)


def insecure_scheme(scheme: str) -> bool:
    """Whether a page reached over this scheme travelled in the clear.
    Local schemes (about:, file:, chrome:, data:) count as secure: there
    is no connection to doubt."""
    return scheme.lower() in ("http", "ws", "ftp")
