// Jack's page reader: what the agent reads of a page, and the numbers
// (refs) it names elements by. Run by reader.py in the agent's isolated
// world only, never the page's own: the page's scripts can see neither
// this code nor the refs. Reading writes nothing to the page (no
// attribute, no focus, no scroll, no selection change); only the acting
// part at the end does, for the agent's acting requests, what the user's
// hands would: it scrolls an element into view, focuses a field, and
// sets what no key can (a <select>'s option, a date). The clicks, keys
// and wheel turns themselves are real input, sent by Jack as Qt events.
// Every request sends this file again; it defines `window.__jack` once
// per document (the world starts empty with each new document, so the
// refs last as long as the page load does).
//
// Frames (frames.py). Each frame's document has its own world and its own
// `__jack`: Jack reads and acts in it the same way. The first call gives
// the document its name (`__jack.frameName`, "f2"; "" in the main page),
// which it keeps while it lasts, and the refs it hands out carry it
// ("f2:14"). The reads say where each
// frame element sits in what they read and which of the document's windows
// it holds (`frameIndex`), and a frame says which of its parent's windows
// it is (`whoami`): that pair is how Jack matches an element to its frame.
//
// Kept to what Chromium 122 has (Qt 6.8's QtWebEngine).
(() => {
"use strict";
if (window.__jack) return;

const NAME_MAX = 100;            // characters of an element's name
const VALUE_MAX = 80;            // ... of a field's value in the outline
const URL_MAX = 160;
const WALK_MAX = 20000;          // outline lines gathered, at most
const OPTIONS_MAX = 100;
const SELECTION_MAX = 200000;
const PASSWORD = "(password)";
const HOLE = "\u0001";          // marks where a frame's content goes (frames.py)

// ── refs ────────────────────────────────────────────────────────────────
const refs = new Map();          // ref -> WeakRef(element)
const numbers = new WeakMap();   // element -> ref
let next = 1;

function ref(el) {
  let n = numbers.get(el);
  if (n === undefined) {
    n = next++;
    numbers.set(el, n);
    refs.set(n, new WeakRef(el));
  }
  return n;
}

// A ref as the agent names it: the number, or "f2:14" in frame f2.
function refOf(el) {
  const n = ref(el);
  const f = window.__jack && window.__jack.frameName;
  return f ? `${f}:${n}` : n;
}

function refName(n) {
  const f = window.__jack && window.__jack.frameName;
  return f ? `${f}:${n}` : String(n);
}

function element(n) {           // the element a ref names, while it is in the page
  const held = refs.get(n);
  const el = held && held.deref();
  if (!el) { refs.delete(n); return null; }
  return el.isConnected ? el : null;
}

// ── helpers ─────────────────────────────────────────────────────────────
function clean(s, max) {
  s = String(s || "").replace(/\s+/g, " ").trim();
  return max && s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function quote(s) { return JSON.stringify(s); }

function box(el) {
  for (const r of el.getClientRects()) if (r.width > 0 && r.height > 0) return true;
  return false;
}

// Rendered: not display:none (itself or above), not visibility:hidden,
// with a box of some size. Opacity is not looked at: styled checkboxes
// are transparent inputs under their labels.
function visible(el) {
  if (!el.isConnected) return false;
  if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true,
                                                   checkVisibilityCSS: true})) return false;
  return box(el);
}

// A field counts as visible when it is, or a label of it is.
function fieldVisible(el) {
  if (visible(el)) return true;
  return !!(el.labels && Array.from(el.labels).some(visible));
}

// A subtree nothing of which renders: display:none (but not
// display:contents, whose children render) or aria-hidden.
function prunable(el) {
  if (el.getAttribute("aria-hidden") === "true") return true;
  if (!el.checkVisibility || el.checkVisibility()) return false;
  const display = getComputedStyle(el).display;
  return display !== "contents";
}

function* deep(root, selector) {
  // querySelectorAll into open shadow roots too.
  yield* root.querySelectorAll(selector);
  for (const host of root.querySelectorAll("*"))
    if (host.shadowRoot) yield* deep(host.shadowRoot, selector);
}

function where(url) {
  // A link's address as the outline shows it: the path on this site,
  // whole elsewhere.
  try {
    const u = new URL(url, location.href);
    if (u.protocol === "javascript:") return "(script)";
    const s = u.origin === location.origin ? u.pathname + u.search + u.hash : u.href;
    return clean(s, URL_MAX);
  } catch (e) {
    return clean(url, URL_MAX);
  }
}

function labelText(label) {
  const copy = label.cloneNode(true);
  for (const c of copy.querySelectorAll("input, select, textarea, button")) c.remove();
  return copy.textContent;
}

const TEXT_INPUTS = new Set(["text", "search", "email", "tel", "url", "password", "number",
                             "date", "datetime-local", "month", "week", "time", "color"]);

function isPassword(el) {
  return el.tagName === "INPUT" && el.type === "password";
}

function fieldValue(el) {
  if (isPassword(el)) return el.value ? PASSWORD : "";
  if (el.tagName === "SELECT")
    return Array.from(el.selectedOptions).map(o => clean(o.text)).join(", ");
  if (el.isContentEditable && !("value" in el)) return el.innerText;
  return el.value == null ? "" : String(el.value);
}

function name(el) {
  const by = el.getAttribute("aria-labelledby");
  if (by) {
    const t = clean(by.split(/\s+/).map(id => {
      const e = document.getElementById(id);
      return e ? (e.innerText || e.textContent) : "";
    }).join(" "), NAME_MAX);
    if (t) return t;
  }
  const label = el.getAttribute("aria-label");
  if (label && label.trim()) return clean(label, NAME_MAX);
  if (el.isContentEditable && el.getAttribute("contenteditable") !== null)
    return clean(el.getAttribute("aria-placeholder") || el.getAttribute("title"), NAME_MAX);
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    if (tag === "INPUT" && ["submit", "button", "reset"].includes(el.type))
      return clean(el.value, NAME_MAX) || (el.type === "submit" ? "Submit" :
                                          el.type === "reset" ? "Reset" : "");
    if (tag === "INPUT" && el.type === "image") return clean(el.alt || el.value || "Submit", NAME_MAX);
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map(labelText).join(" "), NAME_MAX);
      if (t) return t;
    }
    return clean(el.getAttribute("placeholder") || el.getAttribute("title") || "", NAME_MAX);
  }
  if (tag === "IMG" || tag === "AREA") return clean(el.getAttribute("alt") || el.getAttribute("title"), NAME_MAX);
  if (tag === "IFRAME" || tag === "FRAME" || tag === "OBJECT" || tag === "EMBED")
    return clean(el.getAttribute("title") || el.getAttribute("name"), NAME_MAX);
  let t = clean(el.innerText !== undefined ? el.innerText : el.textContent, NAME_MAX);
  if (!t) {
    const img = el.querySelector("img[alt]");
    if (img) t = clean(img.getAttribute("alt"), NAME_MAX);
  }
  if (!t) {
    const title = el.querySelector("svg title");
    if (title) t = clean(title.textContent, NAME_MAX);
  }
  return t || clean(el.getAttribute("title"), NAME_MAX);
}

// ── roles ───────────────────────────────────────────────────────────────
const INPUT_ROLES = {button: "button", submit: "button", reset: "button", image: "button",
                     file: "button", checkbox: "checkbox", radio: "radio", range: "slider",
                     number: "spinbutton", search: "searchbox", hidden: null};
const ACTIVE = new Set(["link", "button", "checkbox", "radio", "textbox", "searchbox",
                        "combobox", "listbox", "option", "slider", "spinbutton", "switch",
                        "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem",
                        "clickable", "iframe"]);
const LANDMARKS = new Set(["banner", "navigation", "main", "complementary", "contentinfo",
                           "region", "form", "search", "dialog", "alertdialog", "tablist",
                           "menu", "menubar", "tree", "toolbar"]);
const OTHER = new Set(["heading", "img", "alert", "status"]);
// Whose insides the outline does not list again: the line says it.
const WHOLE = new Set(["link", "button", "checkbox", "radio", "textbox", "searchbox",
                       "combobox", "option", "slider", "spinbutton", "switch", "tab",
                       "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem", "heading",
                       "img", "iframe"]);

function sectioned(el) {
  return !!el.parentElement && !!el.parentElement.closest("article, aside, main, nav, section");
}

function named(el) {
  return !!(el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") ||
            el.getAttribute("title"));
}

function role(el) {
  const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
  if (explicit && !["presentation", "none", "generic"].includes(explicit)) return explicit;
  switch (el.tagName) {
    case "A": case "AREA": return el.hasAttribute("href") ? "link" : null;
    case "BUTTON": case "SUMMARY": return "button";
    case "INPUT": return el.type in INPUT_ROLES ? INPUT_ROLES[el.type] : "textbox";
    case "TEXTAREA": return "textbox";
    case "SELECT": return el.multiple || el.size > 1 ? "listbox" : "combobox";
    case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return "heading";
    case "NAV": return "navigation";
    case "MAIN": return "main";
    case "ASIDE": return "complementary";
    case "HEADER": return sectioned(el) ? null : "banner";
    case "FOOTER": return sectioned(el) ? null : "contentinfo";
    case "FORM": return named(el) ? "form" : null;
    case "SECTION": return named(el) ? "region" : null;
    case "SEARCH": return "search";
    case "DIALOG": return "dialog";
    case "IMG": return el.getAttribute("alt") ? "img" : null;
    case "IFRAME": case "FRAME": return "iframe";
    case "OBJECT": return frameIndex(el) !== null ? "iframe" : null;
  }
  const editable = el.getAttribute("contenteditable");
  if (editable !== null && editable !== "false" &&
      !(el.parentElement && el.parentElement.isContentEditable)) return "textbox";
  if (el.hasAttribute("onclick") ||
      (el.hasAttribute("tabindex") && el.tabIndex >= 0 && el !== document.body)) return "clickable";
  return null;
}

function level(el) {
  const aria = parseInt(el.getAttribute("aria-level"), 10);
  if (aria > 0) return aria;
  const m = /^H(\d)$/.exec(el.tagName);
  return m ? +m[1] : 2;
}

function states(el, r) {
  const out = [];
  const aria = a => el.getAttribute(a);
  if (el.tagName === "INPUT" && !["text", "search", "password", "checkbox", "radio",
                                   "submit", "button", "reset", "image"].includes(el.type))
    out.push(el.type);
  if (r === "checkbox" || r === "radio" || r === "switch" || r === "menuitemcheckbox" ||
      r === "menuitemradio") {
    const checked = el.tagName === "INPUT" ? el.checked : aria("aria-checked") === "true";
    out.push(checked ? "checked" : "not checked");
  }
  if (el.disabled || aria("aria-disabled") === "true") out.push("disabled");
  if (el.required || aria("aria-required") === "true") out.push("required");
  if (aria("aria-expanded") === "true") out.push("expanded");
  if (aria("aria-expanded") === "false") out.push("collapsed");
  if (aria("aria-selected") === "true") out.push("selected");
  if (aria("aria-pressed") === "true") out.push("pressed");
  if (el.tagName === "DETAILS" || (el.tagName === "SUMMARY" && el.parentElement &&
                                   el.parentElement.tagName === "DETAILS"))
    out.push(el.parentElement.open ? "expanded" : "collapsed");
  if (document.activeElement === el) out.push("focused");
  return out.length ? " (" + out.join(", ") + ")" : "";
}

function line(el, r) {
  if (r === "heading") return `heading ${level(el)} ${quote(name(el))}`;
  if (LANDMARKS.has(r)) return named(el) ? `${r} ${quote(name(el))}` : r;
  if (r === "img") return `img ${quote(name(el))}`;
  if (r === "alert" || r === "status") {
    const t = clean(el.innerText, NAME_MAX);
    return t ? `${r} ${quote(t)}` : null;
  }
  const n = name(el);
  let s = `[${refOf(el)}] ${r} ${quote(n)}`;
  if (r === "link" && el.hasAttribute("href")) s += " -> " + where(el.getAttribute("href"));
  if (r === "iframe") {
    // Jack says what it read of the frame here (frames.py), by its window.
    const src = el.src || el.data || "";
    const index = frameIndex(el);
    s += (src ? " -> " + where(src) : "") + HOLE + (index === null ? "x" : index) + HOLE;
  }
  s += states(el, r);
  if (["textbox", "searchbox", "combobox", "spinbutton", "slider"].includes(r) ||
      (r === "listbox" && el.tagName === "SELECT")) {
    const v = fieldValue(el);
    if (v === PASSWORD) s += " = " + PASSWORD;
    else if (v) s += " = " + quote(clean(v, VALUE_MAX));
  }
  return s;
}

// ── what the agent reads ────────────────────────────────────────────────
function page(items, offset, length) {
  offset = Math.max(0, offset | 0);
  const slice = items.slice(offset, offset + length);
  const more = offset + slice.length < items.length;
  return {offset, total: items.length, more, next: more ? offset + slice.length : null, slice};
}

function text(o) {
  const body = document.body || document.documentElement;
  const all = body ? body.innerText : "";
  const offset = Math.max(0, o.offset | 0);
  const t = all.slice(offset, offset + o.length);
  const more = offset + t.length < all.length;
  return {text: t, offset, total: all.length, more, next: more ? offset + t.length : null,
          ...frameMarks(o.hidden, all.length)};
}

function links(o) {
  const items = [];
  for (const a of deep(document, "a[href], area[href]")) {
    if (!o.hidden && !visible(a)) continue;
    items.push({ref: refOf(a), text: name(a), url: a.href});
  }
  const p = page(items, o.offset, o.length);
  return {links: p.slice, offset: p.offset, total: p.total, more: p.more, next: p.next,
          ...frameMarks(o.hidden, items.length)};
}

function field(el) {
  const type = el.tagName === "INPUT" || el.tagName === "BUTTON" ? el.type
                                                                 : el.tagName.toLowerCase();
  const f = {ref: refOf(el), label: name(el), type, name: el.name || "",
             value: fieldValue(el), required: !!el.required, disabled: !!el.disabled};
  if (type === "checkbox" || type === "radio") f.checked = el.checked;
  if (el.tagName === "SELECT") {
    f.options = Array.from(el.options).slice(0, OPTIONS_MAX).map(
      o => ({value: o.value, text: clean(o.text, NAME_MAX), selected: o.selected}));
    if (el.options.length > OPTIONS_MAX) f.options_total = el.options.length;
  }
  return f;
}

function fields(list, hidden) {
  const out = [];
  for (const el of list) {
    if (!["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName)) continue;
    if (!hidden && (el.type === "hidden" || !fieldVisible(el))) continue;
    out.push(field(el));
  }
  return out;
}

function forms(o) {
  const items = [];
  for (const form of deep(document, "form")) {
    const fs = fields(Array.from(form.elements), o.hidden);
    if (!fs.length && !o.hidden) continue;
    items.push({ref: refOf(form),
                name: clean(form.getAttribute("aria-label") || form.name || form.id, NAME_MAX),
                action: form.action, method: (form.method || "get").toLowerCase(), fields: fs});
  }
  const loose = fields(Array.from(deep(document, "input, select, textarea, button"))
                         .filter(el => !el.form), o.hidden);
  if (loose.length)
    items.push({ref: null, name: "(fields outside any form)", action: "", method: "",
                fields: loose});
  const p = page(items, o.offset, o.length);
  return {forms: p.slice, offset: p.offset, total: p.total, more: p.more, next: p.next,
          ...frameMarks(o.hidden, items.length)};
}

function outline(o) {
  const lines = [];
  const marks = [];               // where each frame's lines go: after its element's
  let cut = false;
  const walk = (parent, depth) => {
    for (let el = parent.firstElementChild; el; el = el.nextElementSibling) {
      if (lines.length >= WALK_MAX) { cut = true; return; }
      const tag = el.tagName;
      if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK"].includes(tag)) continue;
      if (!o.hidden && prunable(el)) continue;
      const r = role(el);
      let inner = depth;
      if (r && (ACTIVE.has(r) || LANDMARKS.has(r) || OTHER.has(r))) {
        const shown = o.hidden || (ACTIVE.has(r) ? fieldVisible(el) : visible(el));
        const s = shown ? line(el, r) : null;
        if (s) {
          lines.push("  ".repeat(depth) + s);
          if (LANDMARKS.has(r)) inner = depth + 1;
          if (r === "iframe") {
            const index = frameIndex(el);
            if (index !== null) marks.push({at: lines.length, index, depth: depth + 1});
          }
        }
        if (WHOLE.has(r)) continue;
      }
      walk(el, inner);
      if (el.shadowRoot) walk(el.shadowRoot, inner);
    }
  };
  const root = document.body || document.documentElement;
  if (root) walk(root, 0);
  const p = page(lines, o.offset, o.length);
  return {outline: p.slice, offset: p.offset, total: p.total, more: p.more, next: p.next,
          cut, frames: marks, known: knownFrames(), length: window.length};
}

function has(o) {
  if (o.selector) return {found: document.querySelector(o.selector) !== null};
  const body = document.body || document.documentElement;
  return {found: !!body && body.innerText.includes(o.text)};
}

function info() {
  return {url: location.href, title: document.title, ready: document.readyState,
          width: innerWidth, height: innerHeight};
}

// ── look: the user's selection, the image they mean ─────────────────────
function rectOf(el) {
  const r = el.getBoundingClientRect();
  const x = Math.max(0, r.left), y = Math.max(0, r.top);
  const w = Math.min(innerWidth, r.right) - x, h = Math.min(innerHeight, r.bottom) - y;
  return w > 1 && h > 1 ? {x, y, w, h} : null;
}

// The image's own pixels, as a PNG data URL, through a canvas that is
// never put in the page: an <img> at its natural size (at most
// PIXELS_MAX on its long side), a <video>'s frame, a <canvas>. null when
// the page's origin may not read it (a cross-origin image without CORS
// taints the canvas) or it has none yet.
const PIXELS_MAX = 4096;
function pixels(el) {
  try {
    const tag = el.tagName.toLowerCase();
    let w = 0, h = 0;
    if (tag === "img") {
      if (!el.complete) return null;
      w = el.naturalWidth; h = el.naturalHeight;
    } else if (tag === "video") {
      if (el.readyState < 2) return null;
      w = el.videoWidth; h = el.videoHeight;
    } else if (tag === "canvas") {
      w = el.width; h = el.height;
    } else {
      return null;
    }
    if (!w || !h) return null;
    const scale = Math.min(1, PIXELS_MAX / Math.max(w, h));
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * scale));
    c.height = Math.max(1, Math.round(h * scale));
    c.getContext("2d").drawImage(el, 0, 0, c.width, c.height);
    return c.toDataURL("image/png");
  } catch (e) {
    return null;
  }
}

function imageInfo(el, under) {
  const rect = rectOf(el);
  if (!rect) return null;
  let url = "";
  if (el.tagName === "IMG") url = el.currentSrc || el.src;
  else if (el.tagName === "VIDEO") url = el.currentSrc || el.poster || "";
  else if (el.tagName !== "CANVAS" && el.tagName.toLowerCase() !== "svg") {
    const m = /url\(["']?([^"')]+)/.exec(getComputedStyle(el).backgroundImage || "");
    url = m ? new URL(m[1], location.href).href : "";
  }
  return {rect, under, url, alt: clean(el.getAttribute("alt") || el.getAttribute("title") ||
                                       el.getAttribute("aria-label"), NAME_MAX),
          width: innerWidth, height: innerHeight, pixels: pixels(el)};
}

const IMAGES = new Set(["img", "svg", "canvas", "video"]);

// An image under the pointer: an image element, or a box drawn with a
// background image.
function pictured(el) {
  const tag = el.tagName.toLowerCase();
  if (IMAGES.has(tag)) return true;
  if (tag === "picture") return false;
  const bg = getComputedStyle(el).backgroundImage;
  return !!bg && bg.includes("url(");
}

function selection(o) {
  o = o || {};
  const active = document.activeElement;
  const tag = active ? active.tagName : "";
  const focused = document.hasFocus();
  if (tag === "IFRAME" || tag === "FRAME") return {text: "", in: "frame", image: null, focused};
  if (active && isPassword(active)) return {text: "", in: null, image: null, focused};
  const sel = getSelection();
  let text = sel ? String(sel) : "";
  let where_ = text ? "page" : null;
  if ((tag === "INPUT" || tag === "TEXTAREA") && typeof active.selectionStart === "number" &&
      active.selectionEnd > active.selectionStart) {
    text = active.value.slice(active.selectionStart, active.selectionEnd);
    where_ = "field";
  }
  let image = null;
  if (o.image && sel && sel.rangeCount && !sel.isCollapsed) {
    outer:
    for (let i = 0; i < sel.rangeCount; i++) {
      const range = sel.getRangeAt(i);
      const container = range.commonAncestorContainer;
      const start = container.nodeType === 1 ? container : container.parentElement;
      if (!start) continue;
      const candidates = [start, ...start.querySelectorAll("img, svg, canvas, video")];
      for (const el of candidates) {
        if (IMAGES.has(el.tagName.toLowerCase()) && range.intersectsNode(el) && visible(el)) {
          image = imageInfo(el, "selection");
          if (image) break outer;
        }
      }
    }
  }
  return {text: text.length > SELECTION_MAX ? text.slice(0, SELECTION_MAX) : text,
          cut: text.length > SELECTION_MAX, in: where_, image, focused};
}

function imageAt(x, y) {
  for (const el of document.elementsFromPoint(x, y)) {
    if (el === document.body || el === document.documentElement) break;
    if (pictured(el)) {
      const image = imageInfo(el, "pointer");
      if (image) return image;
    }
  }
  return null;
}

// Two animation frames from now: a frame painted after everything the
// page had done by the time this ran.
let painted = 0;
function frame() {
  const n = ++painted;
  window.__jack.painted = 0;
  requestAnimationFrame(() => requestAnimationFrame(() => { window.__jack.painted = n; }));
  return n;
}

// ── acting (the acting requests only) ───────────────────────────────────
// What a click must not reach: a file field (its chooser is a dialog of
// the browser's), a <select> and the date, time and colour fields (their
// popups take the user's pointer and keyboard).
const PICKERS = new Set(["date", "datetime-local", "month", "week", "time", "color"]);
const TYPABLE = new Set(["text", "search", "email", "tel", "url", "password", "number"]);

function target(n) {
  const el = element(n);
  if (!el) throw new Error(`no element [${refName(n)}] on this page (a ref is gone with the page ` +
                           "that gave it: read the page again)");
  return el;
}

// An element as the outline names it, with a ref: what was acted on, or
// what is in the way.
function describe(el) {
  if (!el) return "nothing";
  const r = role(el);
  const n = name(el);
  return `[${refOf(el)}] ${r || el.tagName.toLowerCase()}` + (n ? " " + quote(n) : "");
}

// The field a click on `el` works: itself, the <select> it is in, or the
// field of the label it is in.
function controlOf(el) {
  if (!el || !el.closest) return null;
  if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName)) return el;
  const select = el.closest("select");
  if (select) return select;
  const label = el.closest("label");
  return label ? label.control : null;
}

function refused(el) {
  const c = controlOf(el);
  if (!c) return null;
  if (c.tagName === "INPUT" && c.type === "file") return "upload";
  if (c.tagName === "SELECT") return "select";
  if (c.tagName === "INPUT" && PICKERS.has(c.type)) return "picker";
  return null;
}

function visibleRect(el) {
  for (const r of el.getClientRects()) {
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    const w = Math.min(innerWidth, r.right) - x, h = Math.min(innerHeight, r.bottom) - y;
    if (w >= 1 && h >= 1) return {x, y, w, h};
  }
  return null;
}

function intoView(el) {
  const r = el.getBoundingClientRect();
  if (r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth)
    el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
}

// What is at a point, into open shadow roots.
function hitAt(x, y) {
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  return hit;
}

// Whether a click on `hit` is a click on `el`: it is el, inside it
// (through shadow roots too), or a label of it.
function reaches(el, hit) {
  for (let n = hit; n; ) {
    if (n === el) return true;
    if (n.parentNode) n = n.parentNode;
    else n = n instanceof ShadowRoot ? n.host : null;
  }
  return !!(hit && el.labels && Array.from(el.labels).some(l => l === hit || l.contains(hit)));
}

// Where to put the pointer: o.ref (scrolled into view, the middle of its
// visible part, or of a label's when its own is covered or has no box)
// or the point o.x, o.y. o.refuse: refuse what a click must not reach;
// o.cover: say what covers the element when something does.
function point(o) {
  if (o.ref !== undefined && o.ref !== null) {
    const el = target(o.ref);
    const why = o.refuse ? refused(el) : null;
    if (why) return {refused: why, what: describe(el)};
    if (o.refuse && el.disabled) throw new Error(`[${refName(o.ref)}] is disabled`);
    const aims = [el, ...(el.labels ? Array.from(el.labels) : [])].filter(visible);
    if (!aims.length) throw new Error(`[${refName(o.ref)}] is not visible on the page`);
    let first = null;
    for (const aim of aims) {
      intoView(aim);
      const r = visibleRect(aim);
      if (!r) continue;
      const x = Math.round(r.x + r.w / 2), y = Math.round(r.y + r.h / 2);
      const hit = hitAt(x, y);
      if (!o.cover || reaches(el, hit))
        return {x, y, what: describe(el), width: innerWidth, height: innerHeight};
      first = first || {covered: describe(hit), x, y, what: describe(el)};
    }
    if (first) return first;
    throw new Error(`[${refName(o.ref)}] is outside the visible page, even scrolled to`);
  }
  const x = Number(o.x), y = Number(o.y);
  if (!(x >= 0 && y >= 0 && x < innerWidth && y < innerHeight))
    throw new Error(`(${o.x}, ${o.y}) is outside the visible page (${innerWidth}x${innerHeight} ` +
                    "CSS px)");
  const hit = hitAt(x, y);
  if (hit && FRAME_TAGS.has(hit.tagName)) {
    // Over a frame's page: Jack asks the frame what is there (frames.py).
    const index = frameIndex(hit);
    if (index !== null) {
      const b = frameBox(hit);
      const fx = (x - b.left) / b.sx, fy = (y - b.top) / b.sy;
      if (fx >= 0 && fy >= 0 && fx < b.width && fy < b.height)
        return {frame: {index, x: fx, y: fy}, x, y, what: describe(hit),
                width: innerWidth, height: innerHeight};
    }
  }
  const why = o.refuse && hit ? refused(hit) : null;
  if (why) return {refused: why, what: describe(hit), x, y};
  return {x, y, what: describe(hit), width: innerWidth, height: innerHeight};
}

function typable(el) {
  if (!el) return false;
  if (el.tagName === "TEXTAREA") return true;
  if (el.tagName === "INPUT") return TYPABLE.has(el.type);
  return !!el.isContentEditable;
}

function deepActive() {
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return a;
}

function caretToEnd(el) {
  try {
    if (typeof el.setSelectionRange === "function" && typeof el.value === "string") {
      el.setSelectionRange(el.value.length, el.value.length);
      return;
    }
  } catch (e) { /* email and number fields have no caret to set */ }
  if (el.isContentEditable) {
    const r = document.createRange();
    r.selectNodeContents(el);
    r.collapse(false);
    const s = getSelection();
    s.removeAllRanges();
    s.addRange(r);
  }
}

// A text field made the one keys go to, its caret at the end.
function focusField(n) {
  const el = target(n);
  const why = refused(el);
  if (why) return {refused: why, what: describe(el)};
  if (!typable(el))
    throw new Error(`[${refName(n)}] is not a text field (${describe(el)}): click it, then type with ` +
                    "no ref");
  if (el.disabled || el.readOnly)
    throw new Error(`[${refName(n)}] is ${el.disabled ? "disabled" : "read-only"}`);
  intoView(el);
  el.focus({preventScroll: true});
  const active = deepActive();
  if (active !== el && !el.contains(active)) throw new Error(`[${refName(n)}] did not take the focus`);
  caretToEnd(el);
  return {what: describe(el), ...middle(el)};
}

// The middle of an element's visible part (or of its box), for Jack to
// bring the frame it is in into view in the frames above.
function middle(el) {
  const r = visibleRect(el);
  if (r) return {x: Math.round(r.x + r.w / 2), y: Math.round(r.y + r.h / 2)};
  const b = el.getBoundingClientRect();
  return {x: Math.round(Math.min(Math.max(b.left + b.width / 2, 0), innerWidth - 1)),
          y: Math.round(Math.min(Math.max(b.top + b.height / 2, 0), innerHeight - 1))};
}

function focused() {
  const a = deepActive();
  const none = !a || a === document.body || a === document.documentElement;
  return {what: none ? null : describe(a), editable: !none && typable(a)};
}

function fieldKind(n) {
  const el = target(n);
  let kind = "other";
  if (el.tagName === "SELECT") kind = "select";
  else if (el.tagName === "TEXTAREA" || (el.tagName !== "INPUT" && el.isContentEditable)) kind = "text";
  else if (el.tagName === "INPUT") {
    if (el.type === "file") kind = "file";
    else if (el.type === "checkbox" || el.type === "radio") kind = el.type;
    else if (TYPABLE.has(el.type)) kind = "text";
    else if (PICKERS.has(el.type) || el.type === "range") kind = "value";
  }
  return {kind, checked: !!el.checked, disabled: !!el.disabled, what: describe(el)};
}

function changed(el) {
  el.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
}

// A <select>'s options by value or by label (o.by), never its popup.
function choose(n, o) {
  const el = target(n);
  if (el.tagName !== "SELECT") throw new Error(`[${refName(n)}] is not a <select> (${describe(el)})`);
  if (el.disabled) throw new Error(`[${refName(n)}] is disabled`);
  const options = Array.from(el.options);
  const fold = s => clean(s).toLowerCase();
  const found = o.values.map(w => {
    const opt = o.by === "value" ? options.find(p => p.value === w)
      : options.find(p => clean(p.text) === clean(w)) || options.find(p => fold(p.text) === fold(w));
    if (!opt) throw new Error(`[${refName(n)}] has no option with the ${o.by} ${quote(w)}`);
    if (opt.disabled) throw new Error(`[${refName(n)}]'s option ${quote(clean(opt.text))} is disabled`);
    return opt;
  });
  if (found.length !== 1 && !el.multiple) throw new Error(`[${refName(n)}] takes one option`);
  el.focus({preventScroll: true});
  if (el.multiple) for (const p of options) p.selected = found.includes(p);
  else found[0].selected = true;
  changed(el);
  const chosen = Array.from(el.selectedOptions);
  return {value: el.multiple ? chosen.map(p => p.value) : el.value,
          label: el.multiple ? chosen.map(p => clean(p.text)) : clean(chosen.length ? chosen[0].text : "")};
}

// A date, time, colour or range field's value, as typing cannot set it.
function setValue(n, value) {
  const el = target(n);
  if (el.disabled || el.readOnly)
    throw new Error(`[${refName(n)}] is ${el.disabled ? "disabled" : "read-only"}`);
  el.focus({preventScroll: true});
  el.value = String(value);
  if (String(value) !== "" && el.value === "")
    throw new Error(`[${refName(n)}] does not take ${quote(String(value))} (a ${el.type} field)`);
  changed(el);
  return {value: el.value};
}

function scrollTo(n) {
  const el = target(n);
  el.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});
  return {what: describe(el), ...middle(el)};
}

function scrolled() {
  return {x: scrollX, y: scrollY};
}

// ── uploads ─────────────────────────────────────────────────────────────
function fileField(el) {
  if (el.tagName === "INPUT" && el.type === "file") return el;
  const c = el.tagName === "LABEL" ? el.control : null;
  return c && c.tagName === "INPUT" && c.type === "file" ? c : null;
}

// What `upload` clicks for ref n (Jack answers the chooser that opens):
// aim "element", n itself (an opener: a button, a drop zone, a label; or
// the field, shown), "label" (a hidden field's shown label, its ref in
// `label`), or "script" (a hidden field with no label shown: Jack lets
// the page open a chooser with a key press, then clicks it here).
function uploadTarget(n) {
  const el = target(n);
  const input = fileField(el);
  if (input && input.disabled) throw new Error(`[${refName(n)}] is disabled`);
  const out = {what: describe(el), field: input ? describe(input) : null,
               multiple: input ? !!input.multiple : null,
               folder: input ? !!input.webkitdirectory : false, aim: "element"};
  if (!input || input !== el) return out;
  if (visible(input) && visibleRect(input)) return out;
  const label = Array.from(input.labels || []).find(l => visible(l) && visibleRect(l));
  if (label) {
    out.aim = "label";
    out.label = refOf(label);
  } else {
    out.aim = "script";
  }
  return out;
}

function fileClick(n) {
  const input = fileField(target(n));
  if (!input) throw new Error(`[${refName(n)}] is not a file field`);
  input.click();
  return navigator.userActivation ? navigator.userActivation.isActive : null;
}

// The files a field holds now: names and sizes (never their contents).
function fileNames(n) {
  const input = fileField(target(n));
  return input ? Array.from(input.files || []).map(f => ({name: f.name, size: f.size})) : null;
}

// ── frames (frames.py) ──────────────────────────────────────────────────
// Which of this document's windows (window[i]) an element's frame is:
// comparing windows is allowed across origins, so it holds for any frame.
// The order of window[i] is the frames' making, not the page's order.
const FRAME_TAGS = new Set(["IFRAME", "FRAME", "OBJECT"]);

function frameIndex(el) {
  let win = null;
  try { win = el.contentWindow; } catch (e) { return null; }
  if (!win) return null;
  for (let i = 0; i < window.length; i++) if (window[i] === win) return i;
  return null;
}

function frameElements() {
  return Array.from(deep(document, "iframe, frame, object"));
}

// The windows every frame element of the document holds, shown or not.
function knownFrames() {
  return frameElements().map(frameIndex).filter(i => i !== null);
}

// The frames a read that lists (text, links, forms) puts after its own,
// in the page's order: the shown ones, all of them with `hidden`.
function frameMarks(hidden, at) {
  const frames = [];
  for (const el of frameElements()) {
    const index = frameIndex(el);
    if (index === null || (!hidden && !visible(el))) continue;
    frames.push({at, index, name: `${el.tagName.toLowerCase()} ${quote(name(el))}`});
  }
  return {frames, known: knownFrames(), length: window.length};
}

// This frame's place among its parent's windows, from its own side.
function whoami() {
  let index = null, count = null;
  try {
    if (window.parent !== window) {
      count = parent.length;
      for (let i = 0; i < count; i++) if (parent[i] === window) { index = i; break; }
    }
  } catch (e) { /* no parent to ask */ }
  return {index, count, url: location.href, name: window.__jack.frameName || ""};
}

function survey(what, o) {
  return {me: whoami(), read: window.__jack[what](o)};
}

// The frame's page in its element: where its (0, 0) is in this
// document's viewport, its scale (a CSS transform), its size.
function frameBox(el) {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const sx = el.offsetWidth ? r.width / el.offsetWidth : 1;
  const sy = el.offsetHeight ? r.height / el.offsetHeight : 1;
  const pl = parseFloat(cs.paddingLeft) || 0, pt = parseFloat(cs.paddingTop) || 0;
  const pr = parseFloat(cs.paddingRight) || 0, pb = parseFloat(cs.paddingBottom) || 0;
  return {left: r.left + (el.clientLeft + pl) * sx, top: r.top + (el.clientTop + pt) * sy, sx, sy,
          width: el.clientWidth - pl - pr, height: el.clientHeight - pt - pb};
}

// The point (o.x, o.y) of window o.index's page, in this document's
// viewport: its element scrolled into view first when the point is not
// in sight; with o.cover, what covers it there when its element is not
// what is at the point.
function framePoint(o) {
  const el = frameElements().find(e => frameIndex(e) === o.index);
  if (!el) throw new Error("the frame's element is gone from the page");
  const at = () => {
    const b = frameBox(el);
    return {x: b.left + o.x * b.sx, y: b.top + o.y * b.sy};
  };
  const inside = p => p.x >= 0 && p.y >= 0 && p.x < innerWidth && p.y < innerHeight;
  let p = at();
  if (!inside(p)) {
    el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    p = at();
  }
  if (!inside(p)) {
    // A frame taller than the view: the page scrolled on to the point.
    window.scrollBy({left: p.x - innerWidth / 2, top: p.y - innerHeight / 2, behavior: "instant"});
    p = at();
  }
  const x = Math.round(p.x), y = Math.round(p.y);
  if (!inside({x, y})) throw new Error("the frame is outside the visible page, even scrolled to");
  if (o.cover) {
    const hit = hitAt(x, y);
    if (hit !== el) return {covered: describe(hit), x, y};
  }
  return {x, y};
}

// ── article: the page's main content, as Markdown ───────────────────────
// Where the content is: the page's own word for it first (one outermost
// <article> holding a good part of the text, else one <main> or
// role=main), else the block with the most paragraph text in it,
// Readability's way in short (a paragraph scores its parent, and less its
// grandparent, by its length and commas; a class or id that says content
// adds, one that says sidebar or comment takes away; links weigh it down),
// with the siblings that score nearly as well. Then Markdown, leaving out
// what is no content: navigation, sidebars, footers, forms and buttons,
// dialogs, fixed boxes (a cookie banner, a chat bubble), text for screen
// readers only, and boxes whose class or id says ads, sharing, comments,
// newsletters and the like. A frame inside the content leaves a HOLE
// where it sits, for Jack to fill (frames.py).
const ARTICLE_MIN = 200;          // characters an <article> or <main> needs to count
const words = list => new RegExp(list.join("|"), "i");
const UNLIKELY = words(["-ad-", "ai2html", "banner", "breadcrumb", "combx", "comment", "community",
                        "cookie", "consent", "cover-wrap", "disqus", "extra", "footer", "gdpr",
                        "legends", "menu", "newsletter", "related", "remark", "replies", "rss",
                        "share", "shoutbox", "sidebar", "skyscraper", "social", "sponsor",
                        "subscribe", "supplemental", "ad-break", "agegate", "pagination", "pager",
                        "popup", "promo", "yom-remote"]);
const MAYBE = words(["and", "article", "body", "column", "content", "main", "shadow"]);
const POSITIVE = words(["article", "body", "content", "entry", "hentry", "h-entry", "main", "page",
                        "post", "text", "blog", "story", "prose", "markdown"]);
const NEGATIVE = words(["-ad-", "hidden", "^hid$", " hid$", " hid ", "^hid ", "banner", "combx",
                        "comment", "com-", "contact", "foot", "footer", "footnote", "gdpr",
                        "masthead", "media", "meta", "outbrain", "promo", "related", "scroll",
                        "share", "shoutbox", "sidebar", "skyscraper", "sponsor", "shopping", "tags",
                        "tool", "widget"]);
const READERS_ONLY = new RegExp("(^|\\s)(sr-only|visually-hidden|visuallyhidden|screen-reader-text|" +
                                "screenreader-only|a11y-hidden|skip-link|skip-to-content)(\\s|$)", "i");
const NOT_CONTENT = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "SVG",
                             "CANVAS", "AUDIO", "VIDEO", "NAV", "ASIDE", "FOOTER", "FORM", "INPUT",
                             "SELECT", "TEXTAREA", "BUTTON", "DIALOG", "MENU", "DATALIST", "OPTION",
                             "OUTPUT", "PROGRESS", "METER", "MAP", "EMBED"]);
const NOT_CONTENT_ROLES = new Set(["navigation", "complementary", "contentinfo", "banner", "search",
                                   "dialog", "alertdialog", "menu", "menubar", "toolbar", "tablist",
                                   "form"]);
const IND = "\u0002", QUO = "\u0003";     // an indent space and a "> ", put in at the end

function sizeOf(el) {
  return clean(el.innerText !== undefined ? el.innerText : el.textContent).length;
}

function outermost(list) {
  return list.filter(el => !list.some(other => other !== el && other.contains(el)));
}

function selectorOf(el) {
  const classes = typeof el.className === "string" ? el.className.trim().split(/\s+/).filter(Boolean) : [];
  return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
         classes.slice(0, 2).map(c => "." + c).join("");
}

function weight(el) {
  let w = 0;
  for (const said of [typeof el.className === "string" ? el.className : "", el.id]) {
    if (!said) continue;
    if (NEGATIVE.test(said)) w -= 25;
    if (POSITIVE.test(said)) w += 25;
  }
  return w;
}

function linkDensity(el) {
  const all = (el.textContent || "").length;
  if (!all) return 0;
  let links = 0;
  for (const a of el.querySelectorAll("a")) links += (a.textContent || "").length;
  return Math.min(1, links / all);
}

const TAG_SCORES = {DIV: 5, PRE: 3, TD: 3, BLOCKQUOTE: 3, ADDRESS: -3, OL: -3, UL: -3, DL: -3, DD: -3,
                    DT: -3, LI: -3, FORM: -3, H1: -5, H2: -5, H3: -5, H4: -5, H5: -5, H6: -5, TH: -5};
const BLOCK_INSIDE = "p, div, section, article, table, ul, ol, pre, blockquote, h1, h2, h3, h4, h5, h6";

// The densest block: [roots], or null when nothing scores.
function densest(body, hidden) {
  const scores = new Map();
  const add = (el, s) => {
    if (!el || el.nodeType !== 1 || el === document.documentElement) return;
    scores.set(el, (scores.has(el) ? scores.get(el) : (TAG_SCORES[el.tagName] || 0) + weight(el)) + s);
  };
  for (const p of body.querySelectorAll("p, pre, td, blockquote, div, section")) {
    if ((p.tagName === "DIV" || p.tagName === "SECTION") && p.querySelector(BLOCK_INSIDE)) continue;
    if (!hidden && prunable(p)) continue;
    const t = clean(p.textContent);
    if (t.length < 25) continue;
    const s = 1 + (t.match(/[,،、，]/g) || []).length + Math.min(Math.floor(t.length / 100), 3);
    let up = p.parentElement;
    for (let level = 0; up && level < 3; level++, up = up.parentElement)
      add(up, level === 0 ? s : level === 1 ? s / 2 : s / 6);
  }
  const ranked = Array.from(scores).sort((a, b) => b[1] - a[1]).slice(0, 12)
    .map(([el, s]) => [el, s * (1 - linkDensity(el))]).sort((a, b) => b[1] - a[1]);
  if (!ranked.length || ranked[0][1] < 20) return null;   // nothing that reads as prose
  const [best, top] = ranked[0];
  const roots = [];
  const parent = best.parentElement;
  for (const sib of parent ? parent.children : [best]) {
    if (sib === best) { roots.push(sib); continue; }
    const s = scores.get(sib);
    if (s !== undefined && s * (1 - linkDensity(sib)) >= Math.max(10, top * 0.2)) roots.push(sib);
    else if (sib.tagName === "P" && clean(sib.textContent).length > 80 && linkDensity(sib) < 0.25)
      roots.push(sib);
  }
  return {roots, best};
}

function contentRoots(o) {
  const body = document.body || document.documentElement;
  const shown = el => o.hidden || visible(el);
  const total = Math.max(1, sizeOf(body));
  const enough = el => {
    const n = sizeOf(el);
    return n >= Math.min(ARTICLE_MIN, total / 2) && n >= 0.2 * total;
  };
  const articles = outermost(Array.from(deep(document, "article, [role=article]")).filter(shown));
  if (articles.length === 1 && enough(articles[0]))
    return {roots: articles, found: "the page's <article>"};
  const mains = outermost(Array.from(deep(document, "main, [role=main]")).filter(shown));
  if (mains.length === 1 && enough(mains[0])) return {roots: mains, found: "the page's <main>"};
  const dense = densest(body, o.hidden);
  if (dense) return {roots: dense.roots, found: `the densest block (<${selectorOf(dense.best)}>)`};
  return {roots: [body], found: "the whole page"};
}

function junk(el, st, ctx) {
  const tag = el.tagName.toUpperCase();
  if (NOT_CONTENT.has(tag)) return true;
  if (tag === "HEADER" && !sectioned(el)) return true;
  if (NOT_CONTENT_ROLES.has((el.getAttribute("role") || "").trim().split(/\s+/)[0])) return true;
  if (!ctx.hidden && (el.getAttribute("aria-hidden") === "true" || st.display === "none" ||
                      (el.checkVisibility && !el.checkVisibility() && st.display !== "contents")))
    return true;
  if (st.position === "fixed") return true;
  const cls = typeof el.className === "string" ? el.className : (el.getAttribute("class") || "");
  if (READERS_ONLY.test(cls)) return true;
  const said = (cls + " " + el.id).trim();
  return !!said && UNLIKELY.test(said) && !MAYBE.test(said) && !el.closest("table, pre, code");
}

function absolute(url) {
  try { return new URL(url, document.baseURI).href; } catch (e) { return url; }
}

function dest(url) {
  return /[\s<>()]/.test(url) ? "<" + url.replace(/[<>]/g, encodeURIComponent) + ">" : url;
}

function oneLine(s) {
  return s.replace(/[\u0002\u0003]/g, "").replace(/\s*\n\s*/g, " ");
}

function block(s, ctx) {
  if (ctx.link || ctx.cell) return " " + s + " ";
  s = s.trim();
  return s ? "\n\n" + s + "\n\n" : "";
}

// Bold or emphasis around inline text, its outer spaces left outside.
function wrap(s, d) {
  if (s.includes("\n\n")) return s;
  const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(s);
  return m[2] ? m[1] + d + m[2] + d + m[3] : s;
}

function kids(el, ctx) {
  let nodes = (el.shadowRoot || el).childNodes;
  if (el.tagName === "SLOT") {
    const given = el.assignedNodes({flatten: true});
    if (given.length) nodes = given;
  }
  let s = "";
  for (const n of nodes) s += md(n, ctx);
  return s;
}

function image(img, ctx) {
  if (!ctx.hidden) {
    if (!visible(img)) return "";
    const r = img.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2) return "";       // a tracking pixel
  }
  const own = img.currentSrc || img.src || "";
  const src = own && !own.startsWith("data:") ? own
    : img.getAttribute("data-src") || img.getAttribute("data-lazy-src") || own;
  const alt = clean(img.getAttribute("alt") || img.getAttribute("title"), 300).replace(/[\[\]]/g, "\\$&");
  if (!src) return alt;
  return `![${alt}](${src.startsWith("data:") ? "" : dest(absolute(src))})`;
}

function link(el, ctx) {
  if (ctx.link) return kids(el, ctx);
  const inner = oneLine(kids(el, {...ctx, link: true})).trim();
  const href = el.hasAttribute("href") ? el.href : "";
  if (!href || /^javascript:/i.test(href)) return inner;
  return inner ? `[${inner}](${dest(href)})` : "";
}

function code(el) {
  const t = (el.textContent || "").replace(/\n/g, " ");
  if (!t.trim()) return "";
  const longest = Math.max(0, ...(t.match(/`+/g) || []).map(r => r.length));
  const tick = "`".repeat(longest + 1);
  const pad = t.startsWith("`") || t.endsWith("`") ? " " : "";
  return tick + pad + t + pad + tick;
}

function pre(el, ctx) {
  const t = (el.innerText !== undefined ? el.innerText : el.textContent).replace(/\n+$/, "");
  if (ctx.link || ctx.cell) return " " + code({textContent: t}) + " ";
  if (!t.trim()) return "";
  const inner = el.querySelector("code");
  const m = /(?:^|\s)lang(?:uage)?-([\w#+.-]+)/.exec(`${el.className} ${inner ? inner.className : ""}`);
  const longest = Math.max(2, ...(t.match(/`+/g) || []).map(r => r.length));
  const fence = "`".repeat(longest + 1);
  return "\n\n" + fence + (m ? m[1] : "") + "\n" + t + "\n" + fence + "\n\n";
}

function list(el, ctx) {
  if (ctx.link || ctx.cell) return " " + kids(el, ctx) + " ";
  const ordered = el.tagName === "OL";
  let n = ordered ? (el.start || 1) : 0;
  const items = [];
  for (const li of el.children) {
    if (li.tagName !== "LI") {
      const s = md(li, ctx).trim();
      if (s) items.push(s);
      continue;
    }
    const st = getComputedStyle(li);
    if (junk(li, st, ctx)) continue;
    const marker = ordered ? `${n++}. ` : "- ";
    const body = kids(li, ctx).trim().replace(/\n\n(?=[\u0002\u0003]*(?:[-*+]|\d+\.) )/g, "\n");
    if (!body) continue;
    const lines = body.split("\n");
    items.push(marker + lines[0] +
               lines.slice(1).map(l => "\n" + (l ? IND.repeat(marker.length) + l : "")).join(""));
  }
  return items.length ? "\n\n" + items.join("\n") + "\n\n" : "";
}

function quoted(el, ctx) {
  if (ctx.link || ctx.cell) return kids(el, ctx);
  const body = kids(el, ctx).trim();
  return body ? "\n\n" + body.split("\n").map(l => QUO + l).join("\n") + "\n\n" : "";
}

function table(el, ctx) {
  const rows = Array.from(el.rows).filter(r => ctx.hidden || !prunable(r));
  let width = 0;
  for (const r of rows) {
    let w = 0;
    for (const c of r.cells) w += c.colSpan || 1;
    width = Math.max(width, w);
  }
  const data = width >= 2 && !el.querySelector("table") && (rows.length >= 2 || !!el.querySelector("th"));
  if (!data || ctx.link || ctx.cell) return block(kids(el, ctx), ctx);
  const grid = rows.map(r => {
    const cells = [];
    for (const c of r.cells) {
      cells.push(oneLine(kids(c, {...ctx, cell: true})).trim().replace(/\|/g, "\\|"));
      for (let k = 1; k < (c.colSpan || 1); k++) cells.push("");
    }
    return cells;
  });
  const row = cells => "| " + Array.from({length: width}, (_, i) => cells[i] || "").join(" | ") + " |";
  const caption = el.caption ? oneLine(kids(el.caption, {...ctx, cell: true})).trim() : "";
  return "\n\n" + (caption ? `*${caption}*\n\n` : "") +
         [row(grid[0]), "|" + " --- |".repeat(width), ...grid.slice(1).map(row)].join("\n") + "\n\n";
}

const ESCAPE_START = /^(#{1,6}\s|[-+*]\s|\d+[.)]\s|>)/;

function md(node, ctx) {
  if (node.nodeType === 3) {
    if (ctx.pre) return node.data;
    const t = node.data.replace(/\s+/g, " ");
    return ctx.link ? t.replace(/[\[\]]/g, "\\$&") : t;
  }
  if (node.nodeType !== 1) return "";
  const el = node;
  const st = getComputedStyle(el);
  if (!ctx.roots.has(el) && junk(el, st, ctx)) return "";
  const tag = el.tagName.toUpperCase();
  if (FRAME_TAGS.has(tag)) {
    const index = frameIndex(el);
    if (index === null || ctx.link || ctx.cell || (!ctx.hidden && !visible(el))) return "";
    return "\n\n" + HOLE + index + HOLE + "\n\n";
  }
  switch (tag) {
    case "BR": return ctx.link || ctx.cell ? " " : "\n";
    case "HR": return block("---", ctx);
    case "IMG": return image(el, ctx);
    case "PICTURE": { const img = el.querySelector("img"); return img ? image(img, ctx) : ""; }
    case "PRE": return pre(el, ctx);
    case "CODE": case "KBD": case "SAMP": case "TT": return code(el);
    case "A": { const s = link(el, ctx); return st.display.startsWith("inline") ? s : block(s, ctx); }
    case "EM": case "I": case "CITE": case "DFN": return wrap(kids(el, ctx), "*");
    case "STRONG": case "B": return wrap(kids(el, ctx), "**");
    case "DEL": case "S": case "STRIKE": return wrap(kids(el, ctx), "~~");
    case "Q": return "“" + kids(el, ctx) + "”";
    case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": {
      const t = oneLine(kids(el, ctx)).trim();
      if (!t) return "";
      return ctx.link || ctx.cell ? " " + t + " " : block("#".repeat(+tag[1]) + " " + t, ctx);
    }
    case "UL": case "OL": return list(el, ctx);
    case "BLOCKQUOTE": return quoted(el, ctx);
    case "TABLE": return table(el, ctx);
    case "DT": case "SUMMARY": return block(wrap(oneLine(kids(el, ctx)), "**"), ctx);
    case "FIGCAPTION": return block(wrap(oneLine(kids(el, ctx)), "*"), ctx);
  }
  const inner = kids(el, ctx);
  const d = st.display;
  if (d.startsWith("inline") || d === "contents" || d === "ruby") return inner;
  if (tag === "P" && !ctx.link && !ctx.cell) {
    const t = inner.trim();
    return block(ESCAPE_START.test(t) ? "\\" + t : t, ctx);
  }
  return block(inner, ctx);
}

// Lines trimmed and blank runs made one, outside code; the indents and
// quote marks put in.
function tidy(text) {
  const out = [];
  let fence = null, blank = true;
  for (const line of text.split("\n")) {
    const lead = /^[\u0002\u0003]*/.exec(line)[0];
    const rest = line.slice(lead.length);
    const prefix = lead.replace(/\u0002/g, " ").replace(/\u0003/g, "> ");
    if (fence) {
      out.push(prefix + rest);
      if (rest.trim() === fence) fence = null;
      blank = false;
      continue;
    }
    const body = rest.trim();
    if (!body) {
      if (!blank) out.push(prefix.trimEnd());
      blank = true;
      continue;
    }
    out.push(prefix + body);
    blank = false;
    const open = /^(`{3,})/.exec(body);
    if (open) fence = open[1];
  }
  return out.join("\n").trim();
}

// The frames' HOLEs taken out: where each was.
function holes(text) {
  const marks = [];
  let out = "", last = 0;
  const re = /\u0001(\d+)\u0001(\n\n)?/g;
  for (let m; (m = re.exec(text)); ) {
    out += text.slice(last, m.index);
    marks.push({at: out.length, index: +m[1]});
    last = m.index + m[0].length;
  }
  return {text: out + text.slice(last), marks};
}

// The page's title over its content, when the content has no <h1> of its
// own: the first shown <h1>, else the document's title. None for a frame's
// document, whose title is seldom meant to be read.
function articleTitle(roots) {
  if (window.parent !== window) return "";
  if (roots.some(r => r.tagName === "H1" || r.querySelector("h1"))) return "";
  for (const h of deep(document, "h1")) {
    if (!visible(h)) continue;
    const t = clean(h.innerText, 300);
    if (t) return t;
  }
  return clean(document.title, 300);
}

function article(o) {
  const {roots, found} = contentRoots(o);
  const ctx = {hidden: !!o.hidden, roots: new Set(roots), link: false, cell: false, pre: false};
  let text = roots.map(r => md(r, ctx)).join("\n\n");
  const title = articleTitle(roots);
  if (title) text = "# " + title + "\n\n" + text;
  const done = holes(tidy(text));
  const offset = Math.max(0, o.offset | 0);
  const t = done.text.slice(offset, offset + o.length);
  const more = offset + t.length < done.text.length;
  return {article: t, offset, total: done.text.length, more, next: more ? offset + t.length : null,
          found, frames: done.marks, known: knownFrames(), length: window.length};
}

// ── full-page screenshots (control.py `screenshot`, full) ───────────────
// The page scrolled tile by tile. From the second tile on, what is fixed
// or sticky (a header that stays, a cookie banner) is hidden by a sheet
// adopted into the document (and the shadow roots holding such
// elements), its rules naming them by their place in the tree: no
// element, attribute or class of the page changes, and the sheet goes
// again after.
const FIXED_MAX = 300;
let hiding = [];                  // [{root, sheet, selectors}]

function pageSize() {
  const s = document.scrollingElement || document.documentElement;
  return {width: Math.max(s ? s.scrollWidth : 0, innerWidth),
          height: Math.max(s ? s.scrollHeight : 0, innerHeight),
          x: scrollX, y: scrollY, vw: innerWidth, vh: innerHeight};
}

function tile(o) {
  window.scrollTo({left: o.x, top: o.y, behavior: "instant"});
  if (o.hide) hideFixed();
  return {x: scrollX, y: scrollY, height: pageSize().height, hidden: hiding.reduce(
    (n, h) => n + h.selectors.size, 0)};
}

function placeOf(el) {
  const parts = [];
  for (let n = el; n && n !== document.documentElement; ) {
    const parent = n.parentNode;
    if (!parent) return null;
    const nth = Array.prototype.indexOf.call(parent.children, n) + 1;
    parts.unshift(`${CSS.escape(n.localName)}:nth-child(${nth})`);
    if (parent instanceof ShadowRoot) return ":host > " + parts.join(" > ");
    n = parent;
  }
  return ":root > " + parts.join(" > ");
}

function hideFixed() {
  const todo = [document.documentElement];
  let n = 0;
  while (todo.length && n < FIXED_MAX) {
    const root = todo.pop();
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {acceptNode(el) {
      const st = getComputedStyle(el);
      if (st.display === "none") return NodeFilter.FILTER_REJECT;
      if (el.shadowRoot) todo.push(el.shadowRoot);
      if ((st.position === "fixed" || st.position === "sticky") && n < FIXED_MAX) {
        const place = placeOf(el);
        if (place) {
          const into = el.getRootNode();
          let entry = hiding.find(h => h.root === into);
          if (!entry) {
            entry = {root: into, sheet: new CSSStyleSheet(), selectors: new Set()};
            into.adoptedStyleSheets = [...into.adoptedStyleSheets, entry.sheet];
            hiding.push(entry);
          }
          if (!entry.selectors.has(place)) { entry.selectors.add(place); n++; }
        }
        return NodeFilter.FILTER_REJECT;    // what is inside goes with it
      }
      return NodeFilter.FILTER_ACCEPT;
    }});
    while (walker.nextNode()) { /* acceptNode gathers */ }
  }
  for (const entry of hiding)
    entry.sheet.replaceSync(Array.from(entry.selectors).map(s => `${s} { opacity: 0 !important; ` +
      "visibility: hidden !important; transition: none !important; animation: none !important; }")
      .join("\n"));
}

function endTiles(o) {
  for (const {root, sheet} of hiding)
    root.adoptedStyleSheets = root.adoptedStyleSheets.filter(s => s !== sheet);
  hiding = [];
  window.scrollTo({left: o.x, top: o.y, behavior: "instant"});
  return {x: scrollX, y: scrollY};
}

// ── the agent's scripts' answers (script.py) ────────────────────────────
// The password fields' values in this document (as long as `min` or
// longer), each as typed and as JSON writes it, once and twice: never
// sent anywhere, only taken out of an answer here.
function secrets(min) {
  const values = new Set();
  for (const el of deep(document, "input"))
    if (isPassword(el) && el.value && el.value.length >= min) values.add(el.value);
  const forms = new Set();
  for (const v of values) {
    const once = JSON.stringify(v).slice(1, -1);
    forms.add(v).add(once).add(JSON.stringify(once).slice(1, -1));
  }
  return Array.from(forms).sort((a, b) => b.length - a.length);
}

function hasSecrets(min) {
  return secrets(min).length > 0;
}

function scrub(text, min) {
  for (const v of secrets(min)) text = text.split(v).join(PASSWORD);
  return text;
}

window.__jack = {text, links, forms, outline, article, has, info, selection, imageAt, frame,
                 element, painted: 0, point, focusField, focused, fieldKind, choose, setValue,
                 scrollTo, scrolled, uploadTarget, fileClick, fileNames, whoami, survey,
                 framePoint, pageSize, tile, endTiles, hasSecrets, scrub, frameName: ""};
})();
