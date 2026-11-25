"""The app bar protocol's vocabulary (indigoshell/applink/bar.py), pure.

    .venv/bin/python tests/appbar.py

1. The builders: every message and piece the protocol names, in the
   documented shape (none of the keys that say nothing).
2. The worked examples in PROTOCOL.md section 7 (read from the document
   itself) pass `bar_problems`; the chords they take.
3. What breaks a bar's shape, each reported: an item with no id, ids
   repeated, `current` naming no tab, `opens` naming nothing, a chord
   that is none or opens two things, a row's submenu or action naming
   nothing, a colour that is no role, the bounds (tabs, items, strings);
   what does not: kinds, keys and icons it does not know.
4. Answers: good ones and broken ones; chords parsed.
5. Pure: imports with xcffib, skia and PySide6 blocked.

No display, no processes but one; a second or so.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from indigoshell.applink import bar as B      # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed += not ok


print("1. builders")
t = B.tab(41, "std - Rust", busy=True, progress=62)
check("a tab: id and title, and only what says something",
      t == {"id": 41, "title": "std - Rust", "busy": True, "progress": 62}
      and B.tab(1, "x") == {"id": 1, "title": "x"}, f"{t}")
tb = B.tabs("tabs", [t], current=41, can=["close", "move"])
check("tabs: kind, id, tabs, current, can",
      tb == {"kind": "tabs", "id": "tabs", "tabs": [t], "current": 41, "can": ["close", "move"]})
host = B.text("host", spans=[B.span(icon="bookmark"), B.span(icon="unlocked", color="error"),
                             B.span("doc.rust-lang.org")])
check("text with spans; a span is text or an icon, with a colour role",
      host == {"kind": "text", "id": "host", "spans": [
          {"icon": "bookmark"}, {"icon": "unlocked", "color": "error"},
          {"text": "doc.rust-lang.org"}]}, f"{host}")
check("button: label, opens; enabled only when false",
      B.button("file", label="File", opens="file") == {"kind": "button", "id": "file",
                                                        "label": "File", "opens": "file"}
      and B.button("x", icon="play", enabled=False)["enabled"] is False)
addr = B.input_(chords=["ctrl+l"], icon="search", placeholder="address or search",
                text="http://doc.rust-lang.org/std/", select=True, query=True,
                actions={"remove": B.action("shift+delete", drops=True)})
check("an input", addr == {"chords": ["ctrl+l"], "icon": "search",
                           "placeholder": "address or search",
                           "text": "http://doc.rust-lang.org/std/", "select": True,
                           "query": True,
                           "actions": {"remove": {"chord": "shift+delete", "drops": True}}},
      f"{addr}")
r = B.row("add", "Add bookmark", icon="bookmark", detail="doc.rust-lang.org", enabled=False)
check("a row; a menu", r == {"id": "add", "title": "Add bookmark", "detail": "doc.rust-lang.org",
                             "icon": "bookmark", "enabled": False}
      and B.menu([r], chords=["ctrl+m"]) == {"rows": [r], "chords": ["ctrl+m"]})
whole = B.bar([tb], [host], {"address": addr}, {"actions": B.menu([r])})
check("a whole bar: type, start, end, inputs, menus", list(whole) == [
    "type", "start", "end", "inputs", "menus"] and whole["type"] == "bar"
      and B.bar_problems(whole) == [], f"{B.bar_problems(whole)}")
a = B.answer("address", 3, [B.row("typed", "yo", tag="SEARCH")], selected="typed", total=5321)
check("an answer", a == {"type": "answer", "input": "address", "seq": 3,
                         "rows": [{"id": "typed", "title": "yo", "tag": "SEARCH"}],
                         "selected": "typed", "total": 5321} and B.answer_problems(a) == [])
check("an announce: app, the app's id for the window, its handle",
      B.announce("flux", 3, 0x400001) == {"type": "announce", "app": "flux", "window": 3,
                                          "handle": {"x11": 0x400001}}
      and B.announce("flux", 3)["handle"] == {})
check("open, events", B.open_input("address") == {"type": "open", "input": "address"}
      and B.open_menu("actions") == {"type": "open", "menu": "actions"}
      and B.event("move", item="tabs", tab=44, before=40) == {
          "type": "move", "item": "tabs", "tab": 44, "before": 40})

print("2. the worked examples in PROTOCOL.md")
doc = (REPO / "indigoshell" / "applink" / "PROTOCOL.md").read_text()
section7 = doc[doc.index("## 7."):doc.index("## 8.")]
examples = [json.loads(m) for m in re.findall(r"```json\n(.*?)```", section7, re.S)]
check("two bars in section 7, each read whole", len(examples) == 2
      and all(e.get("type") == "bar" for e in examples), f"{len(examples)}")
for e in examples:
    probs = B.bar_problems(e)
    check(f"... {e['start'][0]['id']!r}'s has no problem", probs == [], f"{probs}")
flux_, editor = examples
check("Flux's chords: Ctrl+L, Ctrl+T, Ctrl+F, Ctrl+M",
      B.chords(flux_) == {"ctrl+l": ("input", "address"), "ctrl+t": ("input", "new_tab"),
                          "ctrl+f": ("input", "find"), "ctrl+m": ("menu", "actions")},
      f"{B.chords(flux_)}")
check("the editor's: Ctrl+P, Ctrl+Shift+P, Ctrl+F, Ctrl+H, Alt+F",
      set(B.chords(editor)) == {"ctrl+p", "ctrl+shift+p", "ctrl+f", "ctrl+h", "alt+f"})
block = re.findall(r"```json\n(.*?)```", doc[doc.index("## 4."):doc.index("## 5.")], re.S)[0]
lines, at = [], 0
while block[at:].strip():
    at += len(block[at:]) - len(block[at:].lstrip())
    value, at = json.JSONDecoder().raw_decode(block, at)
    lines.append(value)
answers = [x for x in lines if x.get("type") == "answer"]
check("section 4's answer has no problem", len(answers) == 1
      and B.answer_problems(answers[0]) == [], f"{answers}")


print("3. what breaks a bar, and what does not")


def good():
    return json.loads(json.dumps(flux_))


def broken(change) -> list[str]:
    b = good()
    change(b)
    return B.bar_problems(b)


cases = {
    "an item with no id": lambda b: b["end"][0].pop("id"),
    "an item id repeated": lambda b: b["end"][0].update(id="tabs"),
    "a tab with no id": lambda b: b["start"][0]["tabs"][0].pop("id"),
    "tab ids repeated": lambda b: b["start"][0]["tabs"][1].update(id=40),
    "current naming no tab": lambda b: b["start"][0].update(current=99),
    "opens naming nothing": lambda b: b["end"][0].update(opens="nothing"),
    "a bool for an id": lambda b: b["end"][0].update(id=True),
    "an id too long": lambda b: b["end"][0].update(id="x" * 129),
    "a chord that is none": lambda b: b["inputs"]["find"].update(chords=["ctrl+ff"]),
    "a chord with a named key (not taken from a window)":
        lambda b: b["inputs"]["find"].update(chords=["ctrl+delete"]),
    "a chord opening two things": lambda b: b["menus"]["actions"].update(chords=["ctrl+l"]),
    "a colour that is no role": lambda b: b["end"][0]["spans"][1].update(color="#ff0000"),
    "a row's submenu naming nothing": lambda b: b["menus"]["actions"]["rows"][0].update(
        menu="none"),
    "a row's action the input has not": lambda b: b["inputs"]["address"].update(rows=[
        {"id": 1, "title": "x", "actions": ["nope"]}], query=False),
    "both query and rows": lambda b: b["inputs"]["address"].update(rows=[]),
    "progress past 100": lambda b: b["start"][0]["tabs"][1].update(progress=101),
    "an agent in no state": lambda b: b["start"][0]["tabs"][2]["agent"].update(state="flying"),
    "a string too long": lambda b: b["end"][0]["spans"][2].update(text="x" * 1001),
    "too many tabs": lambda b: b["start"][0].update(
        tabs=[{"id": i, "title": ""} for i in range(501)], current=None),
    "too many items": lambda b: b["end"].extend(
        {"kind": "text", "id": f"t{i}", "text": ""} for i in range(51)),
    "not a list": lambda b: b.update(start={}),
}
missed = {name: probs for name, change in cases.items() if not (probs := broken(change))}
check(f"each of {len(cases)} breakages reported", not missed, f"missed: {list(missed)}")
said = broken(cases["current naming no tab"])
check("... saying where and what", said == ["start 0: current 99 names no tab"], f"{said}")
fine = {
    "a kind it does not know": lambda b: b["end"].append({"kind": "gauge", "id": "g", "v": 3}),
    "keys it does not know": lambda b: (b["start"][0].update(future=1), b.update(later=[1])),
    "an icon it does not know": lambda b: b["end"][0]["spans"][0].update(icon="unicorn"),
    "a voice's colour": lambda b: b["end"][0]["spans"][2].update(color="voice:lilac"),
    "no current tab": lambda b: b["start"][0].update(current=None),
    "nothing at all": lambda b: [b.pop(k) for k in ("start", "end", "inputs", "menus")],
}
wrong = {name: probs for name, change in fine.items() if (probs := broken(change))}
check(f"and {len(fine)} that are fine", not wrong, f"{wrong}")
check("not even an object", B.bar_problems([1]) == ["not an object"])
long_text = broken(lambda b: b["inputs"]["address"].update(text="https://x.org/?" + "a" * 2000))
too_long = broken(lambda b: b["inputs"]["address"].update(text="x" * (B.INPUT_TEXT_MAX + 1)))
check("an input's text may be an address of thousands of characters (up to INPUT_TEXT_MAX); "
      "past that, refused", long_text == [] and too_long, f"{long_text} {too_long}")

print("4. answers, chords")
bad_answers = [{"input": "a"}, {"input": "a", "seq": -1}, {"input": "a", "seq": 1, "total": "x"},
               {"input": "a", "seq": 1, "rows": [{"title": "no id"}]},
               {"input": "a", "seq": 1, "count": {"text": 3}},
               {"input": "a", "seq": 1, "rows": [{"id": i, "title": ""} for i in range(201)]},
               {"seq": 1}]
check("broken answers reported", all(B.answer_problems(x) for x in bad_answers),
      f"{[x for x in bad_answers if not B.answer_problems(x)]}")
check("a count only (find's) is fine", B.answer_problems(
    B.answer("find", 2, count=B.count("no matches", "error"))) == [])
check("chords parsed: modifiers and a key, lower-cased; named keys only where the shell has "
      "the keyboard", B.parse_chord("Ctrl+Shift+P") == (frozenset({"ctrl", "shift"}), "p")
      and B.parse_chord("shift+delete") == (frozenset({"shift"}), "delete")
      and B.parse_chord("shift+delete", grab=True) is None
      and B.parse_chord("n") == (frozenset(), "n")
      and B.parse_chord("ctrl+ctrl+a") is None and B.parse_chord("hyper+a") is None
      and B.parse_chord("ctrl+") is None and B.parse_chord(3) is None)

print("5. pure")
probe = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("xcffib", "skia", "PySide6", "dbus_fast", "psutil"):
            raise ImportError(name)
sys.meta_path.insert(0, Block())
import indigoshell.applink.bar
print(" ".join(sorted(m for m in sys.modules if m.startswith("indigoshell"))))
"""
run = subprocess.run([sys.executable, "-B", "-c", probe], cwd=REPO, capture_output=True,
                     text=True, timeout=30)
check("imports with the toolkits blocked, loading nothing else of the shell's",
      run.returncode == 0 and set(run.stdout.split()) == {
          "indigoshell", "indigoshell.applink", "indigoshell.applink.bar"},
      run.stderr.strip()[-200:] or run.stdout)

print(f"\n{'all ok' if not failed else f'{failed} failed'}")
sys.exit(1 if failed else 0)
