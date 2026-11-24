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
  if (tag === "IFRAME" || tag === "FRAME") return clean(el.getAttribute("title") || el.getAttribute("name"), NAME_MAX);
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
  let s = `[${ref(el)}] ${r} ${quote(n)}`;
  if (r === "link" && el.hasAttribute("href")) s += " -> " + where(el.getAttribute("href"));
  if (r === "iframe") s += (el.src ? " -> " + where(el.src) : "") + " (not read)";
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
  return {text: t, offset, total: all.length, more, next: more ? offset + t.length : null};
}

function links(o) {
  const items = [];
  for (const a of deep(document, "a[href], area[href]")) {
    if (!o.hidden && !visible(a)) continue;
    items.push({ref: ref(a), text: name(a), url: a.href});
  }
  const p = page(items, o.offset, o.length);
  return {links: p.slice, offset: p.offset, total: p.total, more: p.more, next: p.next};
}

function field(el) {
  const type = el.tagName === "INPUT" || el.tagName === "BUTTON" ? el.type
                                                                 : el.tagName.toLowerCase();
  const f = {ref: ref(el), label: name(el), type, name: el.name || "",
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
    items.push({ref: ref(form),
                name: clean(form.getAttribute("aria-label") || form.name || form.id, NAME_MAX),
                action: form.action, method: (form.method || "get").toLowerCase(), fields: fs});
  }
  const loose = fields(Array.from(deep(document, "input, select, textarea, button"))
                         .filter(el => !el.form), o.hidden);
  if (loose.length)
    items.push({ref: null, name: "(fields outside any form)", action: "", method: "",
                fields: loose});
  const p = page(items, o.offset, o.length);
  return {forms: p.slice, offset: p.offset, total: p.total, more: p.more, next: p.next};
}

function outline(o) {
  const lines = [];
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
          cut};
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
  if (!el) throw new Error(`no element [${n}] on this page (a ref is gone with the page ` +
                           "that gave it: read the page again)");
  return el;
}

// An element as the outline names it, with a ref: what was acted on, or
// what is in the way.
function describe(el) {
  if (!el) return "nothing";
  const r = role(el);
  const n = name(el);
  return `[${ref(el)}] ${r || el.tagName.toLowerCase()}` + (n ? " " + quote(n) : "");
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
    if (o.refuse && el.disabled) throw new Error(`[${o.ref}] is disabled`);
    const aims = [el, ...(el.labels ? Array.from(el.labels) : [])].filter(visible);
    if (!aims.length) throw new Error(`[${o.ref}] is not visible on the page`);
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
    throw new Error(`[${o.ref}] is outside the visible page, even scrolled to`);
  }
  const x = Number(o.x), y = Number(o.y);
  if (!(x >= 0 && y >= 0 && x < innerWidth && y < innerHeight))
    throw new Error(`(${o.x}, ${o.y}) is outside the visible page (${innerWidth}x${innerHeight} ` +
                    "CSS px)");
  const hit = hitAt(x, y);
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
    throw new Error(`[${n}] is not a text field (${describe(el)}): click it, then type with ` +
                    "no ref");
  if (el.disabled || el.readOnly) throw new Error(`[${n}] is ${el.disabled ? "disabled" : "read-only"}`);
  intoView(el);
  el.focus({preventScroll: true});
  const active = deepActive();
  if (active !== el && !el.contains(active)) throw new Error(`[${n}] did not take the focus`);
  caretToEnd(el);
  return {what: describe(el)};
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
  if (el.tagName !== "SELECT") throw new Error(`[${n}] is not a <select> (${describe(el)})`);
  if (el.disabled) throw new Error(`[${n}] is disabled`);
  const options = Array.from(el.options);
  const fold = s => clean(s).toLowerCase();
  const found = o.values.map(w => {
    const opt = o.by === "value" ? options.find(p => p.value === w)
      : options.find(p => clean(p.text) === clean(w)) || options.find(p => fold(p.text) === fold(w));
    if (!opt) throw new Error(`[${n}] has no option with the ${o.by} ${quote(w)}`);
    if (opt.disabled) throw new Error(`[${n}]'s option ${quote(clean(opt.text))} is disabled`);
    return opt;
  });
  if (found.length !== 1 && !el.multiple) throw new Error(`[${n}] takes one option`);
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
  if (el.disabled || el.readOnly) throw new Error(`[${n}] is ${el.disabled ? "disabled" : "read-only"}`);
  el.focus({preventScroll: true});
  el.value = String(value);
  if (String(value) !== "" && el.value === "")
    throw new Error(`[${n}] does not take ${quote(String(value))} (a ${el.type} field)`);
  changed(el);
  return {value: el.value};
}

function scrollTo(n) {
  const el = target(n);
  el.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});
  return {what: describe(el)};
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
  if (input && input.disabled) throw new Error(`[${n}] is disabled`);
  const out = {what: describe(el), field: input ? describe(input) : null,
               multiple: input ? !!input.multiple : null,
               folder: input ? !!input.webkitdirectory : false, aim: "element"};
  if (!input || input !== el) return out;
  if (visible(input) && visibleRect(input)) return out;
  const label = Array.from(input.labels || []).find(l => visible(l) && visibleRect(l));
  if (label) {
    out.aim = "label";
    out.label = ref(label);
  } else {
    out.aim = "script";
  }
  return out;
}

function fileClick(n) {
  const input = fileField(target(n));
  if (!input) throw new Error(`[${n}] is not a file field`);
  input.click();
  return navigator.userActivation ? navigator.userActivation.isActive : null;
}

// The files a field holds now: names and sizes (never their contents).
function fileNames(n) {
  const input = fileField(target(n));
  return input ? Array.from(input.files || []).map(f => ({name: f.name, size: f.size})) : null;
}

window.__jack = {text, links, forms, outline, has, info, selection, imageAt, frame,
                 element, painted: 0, point, focusField, focused, fieldKind, choose, setValue,
                 scrollTo, scrolled, uploadTarget, fileClick, fileNames};
})();
