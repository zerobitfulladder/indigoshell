// Jack's page reader: what the agent reads of a page, and the numbers
// (refs) it names elements by. Run by reader.py in the agent's isolated
// world only, never the page's own: the page's scripts can see neither
// this code nor the refs, and nothing here writes to the page (no
// attribute, no focus, no scroll, no selection change). Every request
// sends this file again; it defines `window.__jack` once per document
// (the world starts empty with each new document, so the refs last as
// long as the page load does).
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

function element(n) {           // for acting on a ref (not yet used)
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

window.__jack = {text, links, forms, outline, has, info, selection, imageAt, frame,
                 element, painted: 0};
})();
