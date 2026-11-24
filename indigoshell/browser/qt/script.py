"""The agent's own scripts in a page (`js`, protocol.py "Scripts").

They run in a script world of their own, `WORLD`: not the page's (its
scripts see none of their variables, and they none of the page's), not
the reader's (reader.WORLD, whose refs and reads they cannot touch).
Before any of their code a document gets the guard (guard.js): cookies,
storage, credentials, new windows and a frame element's way into its
frame's realm throw there; a password field's value reads as Jack's
reads say it, so does FormData, and the field keeps its type. Every
frame's world of the same number is guarded too (frames.py
`guard_all`), so reaching a frame by window[N] finds the guard there;
one made after that does not (guard.js says what the guard is not: a
sandbox).

How the code runs. Eval would be the plain way, and it is not used: an
isolated world with no Content-Security-Policy of its own falls under the
page's, so on a page that forbids eval it is refused, and the refusal is
the page's to see (its securitypolicyviolation event, its report-uri).
Instead the code is written into the script Jack runs, as the body of an
async arrow function: first as one expression (`async () => (code)`, so
`document.title` answers itself), then, when that does not compile, as
statements (`async () => { code }`, `return` answering). A script that
does not compile answers nothing at all (runJavaScript's empty answer),
which is how the two are told apart, and how a syntax error is known.
The guard runs as a script of its own just before (call 1); the code's
script (call 2) begins by checking, with nothing but syntax, that the
guard is there (`typeof window.__jackJs`), and runs none of the code
when it is not: a function the code declares is hoisted above anything
written before it in the same script, so the guard could not be
trusted to come first in it, and a document that changed between the
two calls (a navigation) has a fresh world with no guard.

The answer: the function's promise ends in the world's table (guard.js
`start`), which Jack polls (`poll`) every POLL_S until it is there, the
page opens a dialog (then it waits for the agent's answer, and so does
the code), the user takes the tab over, or the time is up; then the
run is dropped (`drop`: a later end is not kept). Its JSON, cut at
protocol.JS_RESULT_MAX characters and a margin, passes through the
reader's world of every document holding a password field (frames.py
`scrub`), where each such value (protocol.JS_SCRUB_MIN characters or
more; as typed, and as JSON writes it, once and twice) is replaced by
protocol.PASSWORD_VALUE before it leaves the page; only then is it cut to
JS_RESULT_MAX. So a password is out of the answer twice over: the
code's own world reads none, and what it answers is scrubbed of them.

A script that never yields (a loop that does not end) holds the page's
renderer: Jack answers when its time is up, and the page stays stuck
until the user closes the tab. The hand says so.
"""

from pathlib import Path

from indigoshell.browser import protocol

WORLD = 4               # reader.py lists the worlds
POLL_S = 0.05

GUARD = (Path(__file__).with_name("guard.js").read_text(encoding="utf-8")
         .replace("__RESULT_MAX__", str(protocol.JS_RESULT_MAX)))
CHECK = 'if (typeof window.__jackJs !== "object") throw 0;\n'


def source(run: int, code: str, *, expression: bool) -> str:
    """The code's script (call 2): `code` as one expression, or as the
    statements of an async function's body."""
    if expression:
        body = code.strip()
        while body.endswith(";"):
            body = body[:-1].rstrip()
        fn = "async () => (" + body + "\n)"
    else:
        fn = "async () => {" + code + "\n}"
    return CHECK + "window.__jackJs.start(" + str(int(run)) + ", " + fn + ");"


def poll(run: int) -> str:
    return ('typeof window.__jackJs === "object" ? window.__jackJs.take(' + str(int(run))
            + ') : \'{"gone": true}\'')


def drop(run: int) -> str:
    return ('typeof window.__jackJs === "object" ? window.__jackJs.drop(' + str(int(run))
            + ') : "true"')
