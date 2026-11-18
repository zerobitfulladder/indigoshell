// indigoshell bridge — the helpers it injects into a page.
//
// Runs in the extension's isolated world: the page's own scripts cannot
// see `__indigo`, and the elements numbered by the last read() live here
// until the page is read again or goes away.
(() => {
  if (window.__indigo) return;

  let refs = [];

  // ── reading ──────────────────────────────────────────────────────────
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG",
    "CANVAS", "HEAD", "META", "LINK", "OBJECT"]);
  const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "MAIN", "HEADER",
    "FOOTER", "NAV", "ASIDE", "UL", "OL", "TABLE", "THEAD", "TBODY", "TR",
    "FORM", "FIELDSET", "PRE", "BLOCKQUOTE", "DL", "DT", "DD", "FIGURE",
    "FIGCAPTION", "BR", "HR", "DETAILS", "SUMMARY", "DIALOG", "ADDRESS"]);

  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const text = (el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const q = (s) => JSON.stringify(clip(s, 80));
  const shown = (el) => {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden") return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    return el.getClientRects().length > 0 || st.display === "contents";
  };
  const labelOf = (el) => {
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map(text).join(" ");
      if (t) return t;
    }
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    if (el.labels && el.labels.length) return text(el.labels[0]);
    return el.getAttribute("placeholder") || el.getAttribute("title")
      || el.getAttribute("name") || el.id || "";
  };
  const ref = (el) => { refs.push(el); return refs.length - 1; };

  // An upload box: {12 file "Attach" accept image/*} — what
  // browser_upload takes.
  const fileMarker = (el) => {
    const accept = el.getAttribute("accept");
    return `{${ref(el)} file ${q(labelOf(el) || "upload")}`
      + (accept ? ` accept ${clip(accept, 60)}` : "")
      + (el.multiple ? " multiple" : "") + "}";
  };

  // A brace marker for what can be acted on, or null for anything else.
  const marker = (el) => {
    const tag = el.tagName, role = el.getAttribute("role");
    if (tag === "A" && el.hasAttribute("href")) {
      let href = el.getAttribute("href");
      try {
        const u = new URL(el.href);
        href = u.origin === location.origin ? u.pathname + u.search + u.hash : u.href;
      } catch (e) { /* keep the attribute */ }
      return `{${ref(el)} link ${q(text(el) || labelOf(el))} → ${clip(href, 120)}}`;
    }
    if (tag === "BUTTON" || role === "button" || role === "tab" || role === "menuitem"
        || (tag === "INPUT" && ["submit", "button", "reset", "image"].includes(el.type))) {
      const kind = role === "tab" ? "tab" : role === "menuitem" ? "menuitem" : "button";
      return `{${ref(el)} ${kind} ${q(text(el) || el.value || labelOf(el))}${el.disabled ? " disabled" : ""}}`;
    }
    if (tag === "INPUT" && el.type === "file") {
      return fileMarker(el);
    }
    if (tag === "INPUT") {
      if (el.type === "hidden") return "";
      if (el.type === "checkbox" || el.type === "radio") {
        return `{${ref(el)} ${el.type} ${q(labelOf(el))} ${el.checked ? "[x]" : "[ ]"}}`;
      }
      // A password is never read back: the model sees how long it is.
      const v = el.type === "password" ? "•".repeat(Math.min(el.value.length, 8)) : el.value;
      return `{${ref(el)} input:${el.type} ${q(labelOf(el))} = ${q(v)}${el.disabled ? " disabled" : ""}}`;
    }
    if (tag === "TEXTAREA") return `{${ref(el)} textarea ${q(labelOf(el))} = ${q(el.value)}}`;
    if (tag === "SELECT") {
      const opts = [...el.options].map((o) => o.text.trim());
      const cur = el.selectedOptions.length ? el.selectedOptions[0].text.trim() : "";
      const list = opts.length <= 12 ? " of " + opts.map((o) => q(o)).join(", ") : ` (${opts.length} options)`;
      return `{${ref(el)} select ${q(labelOf(el))} = ${q(cur)}${list}}`;
    }
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) {
      return `{${ref(el)} editor ${q(labelOf(el))} = ${q(text(el))}}`;
    }
    if (role === "checkbox" || role === "switch") {
      return `{${ref(el)} ${role} ${q(labelOf(el) || text(el))} ${el.getAttribute("aria-checked") === "true" ? "[x]" : "[ ]"}}`;
    }
    return null;
  };

  function read({maxChars = 12000, selector = ""} = {}) {
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) throw new Error("nothing on the page matches " + selector);
    refs = [];
    const out = [];
    let size = 0, cut = false, line = "", frames = 0;
    const push = (s) => {
      if (cut) return;
      if (size + s.length > maxChars) { cut = true; return; }
      out.push(s); size += s.length + 1;
    };
    const flush = () => {
      const t = line.replace(/[ \t\r\n ]+/g, " ").trim();
      if (t) push(t);
      line = "";
    };
    const walk = (node) => {
      if (cut) return;
      if (node.nodeType === 3) { line += node.nodeValue; return; }
      if (node.nodeType !== 1) return;
      const el = node, tag = el.tagName;
      if (SKIP.has(tag)) return;
      if (tag === "IFRAME" || tag === "FRAME") { frames++; return; }
      if (!shown(el)) return;
      const m = marker(el);
      if (m !== null) { line += " " + m + " "; return; }
      if (/^H[1-6]$/.test(tag)) {
        flush(); push("#".repeat(+tag[1]) + " " + clip(text(el), 200)); return;
      }
      if (tag === "LI") { flush(); line = "- "; }
      else if (tag === "TD" || tag === "TH") { line += " | "; }
      else if (tag === "IMG") {
        const alt = el.getAttribute("alt");
        if (alt) line += ` [image: ${clip(alt, 80)}] `;
        return;
      }
      else if (BLOCK.has(tag)) flush();
      for (const c of el.childNodes) walk(c);
      if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walk(c);
      if (BLOCK.has(tag) || tag === "LI") flush();
    };
    walk(root);
    flush();
    // Apps hide their real upload boxes behind a button of their own; list
    // those too, since they are what an upload goes into.
    const hidden = [...root.querySelectorAll("input[type=file]")]
      .filter((el) => !refs.includes(el) && !el.disabled);
    if (hidden.length) {
      const wasCut = cut;
      cut = false;        // room for this line even after a long page
      push("Upload boxes: " + hidden.map(fileMarker).join(" "));
      cut = cut || wasCut;
    }
    return {title: document.title, url: location.href, text: out.join("\n"),
            cut, frames, refs: refs.length};
  }

  // ── finding ──────────────────────────────────────────────────────────
  function find({ref: n, selector, text: words} = {}) {
    let el = null;
    if (n !== undefined && n !== null) {
      if (!refs.length) throw new Error("read the page first");
      el = refs[n];
      if (!el) throw new Error(`no {${n}} on the last read`);
      if (!el.isConnected) throw new Error(`{${n}} is gone — the page changed; read it again`);
    } else if (selector) {
      el = document.querySelector(selector);
      if (!el) throw new Error("nothing matches " + selector);
    } else if (words) {
      const want = words.trim().toLowerCase();
      const pool = document.querySelectorAll(
        "a, button, [role=button], [role=tab], [role=menuitem], input[type=submit], "
        + "input[type=button], label, summary, [onclick]");
      for (const c of pool) {
        const t = (c.innerText || c.value || c.getAttribute("aria-label") || "").trim().toLowerCase();
        if (!t || !c.getClientRects().length) continue;
        if (t === want) { el = c; break; }
        if (!el && t.includes(want)) el = c;
      }
      if (!el) throw new Error(`nothing clickable says "${words}"`);
    } else {
      throw new Error("say which element: ref, selector or text");
    }
    el.scrollIntoView({block: "center", inline: "center"});
    return el;
  }

  // ── acting ───────────────────────────────────────────────────────────
  // A click as a page sees one: pointer and mouse events at the element's
  // centre, then click(), which also runs the default action (following
  // a link, toggling a box, submitting a form).
  function press(el, double = false) {
    const r = el.getBoundingClientRect();
    const at = {bubbles: true, cancelable: true, composed: true,
                clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0};
    const times = double ? 2 : 1;
    for (let i = 0; i < times; i++) {
      el.dispatchEvent(new PointerEvent("pointerdown", {...at, pointerType: "mouse", isPrimary: true}));
      el.dispatchEvent(new MouseEvent("mousedown", {...at, detail: i + 1}));
      if (typeof el.focus === "function") el.focus({preventScroll: true});
      el.dispatchEvent(new PointerEvent("pointerup", {...at, pointerType: "mouse", isPrimary: true}));
      el.dispatchEvent(new MouseEvent("mouseup", {...at, detail: i + 1}));
      el.click();
    }
    if (double) el.dispatchEvent(new MouseEvent("dblclick", {...at, detail: 2}));
  }

  function click(a) {
    press(find(a), !!a.double);
    return true;
  }

  // Replace a field's text the way typing would: execCommand produces
  // real input events, which framework-controlled fields listen for. If
  // the page refuses it, set the value through the native setter (which
  // skips any wrapper a framework put on the element) and announce it.
  function setText(el, value) {
    el.focus({preventScroll: true});
    if (el.isContentEditable) {
      document.execCommand("selectAll");
      if (!document.execCommand("insertText", false, value)) el.textContent = value;
      return;
    }
    if (typeof el.select === "function") el.select();
    const ok = value === "" ? document.execCommand("delete")
      : document.execCommand("insertText", false, value);
    if (!ok || el.value !== value) {
      el.value = value;
      el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
    }
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function chooseOption(el, want) {
    const w = String(want).trim().toLowerCase();
    const opt = [...el.options].find((o) => o.text.trim().toLowerCase() === w || o.value.toLowerCase() === w)
      || [...el.options].find((o) => o.text.trim().toLowerCase().includes(w));
    if (!opt) {
      throw new Error("no option " + JSON.stringify(want) + " in "
        + [...el.options].map((o) => o.text.trim()).join(", "));
    }
    el.value = opt.value;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function checked(el) {
    if (el.tagName === "INPUT") return el.checked;
    return el.getAttribute("aria-checked") === "true";
  }

  function submit(el) {
    const form = el.form || el.closest("form");
    if (!form) { key(el, "Enter"); return "enter"; }
    const btn = form.querySelector("button[type=submit], input[type=submit], button:not([type])")
      || [...document.querySelectorAll("button[form], input[type=submit][form]")].find((b) => b.form === form);
    if (btn && !btn.disabled && btn.getClientRects().length) { press(btn); return "button"; }
    form.requestSubmit();
    return "form";
  }

  function fill({fields = [], submit: andSubmit = false} = {}) {
    let last = null;
    const done = [];
    for (const f of fields) {
      const el = find(f);
      const v = f.value;
      const role = el.getAttribute("role");
      if (el.tagName === "SELECT") chooseOption(el, v);
      else if ((el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "radio"))
               || role === "checkbox" || role === "switch") {
        const want = !["", "0", "false", "no", "off"].includes(String(v).trim().toLowerCase());
        if (checked(el) !== want) press(el);
      } else {
        setText(el, String(v ?? ""));
      }
      done.push(f.ref !== undefined && f.ref !== null ? `{${f.ref}}` : (f.selector || f.text));
      last = el;
    }
    return {done, submitted: andSubmit && last ? submit(last) : null};
  }

  // ── keys ─────────────────────────────────────────────────────────────
  const NAMED = {enter: "Enter", return: "Enter", tab: "Tab", escape: "Escape",
    esc: "Escape", backspace: "Backspace", delete: "Delete", space: " ",
    up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft", right: "ArrowRight",
    arrowup: "ArrowUp", arrowdown: "ArrowDown", arrowleft: "ArrowLeft",
    arrowright: "ArrowRight", home: "Home", end: "End", pageup: "PageUp",
    pagedown: "PageDown"};
  for (let i = 1; i <= 12; i++) NAMED["f" + i] = "F" + i;
  const MODS = {control: "ctrlKey", ctrl: "ctrlKey", shift: "shiftKey",
    alt: "altKey", meta: "metaKey", super: "metaKey"};

  const focusables = () => [...document.querySelectorAll(
    "a[href], button, input:not([type=hidden]), select, textarea, [tabindex], [contenteditable=true]")]
    .filter((e) => !e.disabled && e.tabIndex >= 0 && e.getClientRects().length);

  function key(target, item) {
    const parts = item.length > 1 && item.includes("+") ? item.split("+") : [item];
    const mods = {ctrlKey: false, shiftKey: false, altKey: false, metaKey: false};
    let k = parts[parts.length - 1];
    for (const p of parts.slice(0, -1)) {
      const m = MODS[p.trim().toLowerCase()];
      if (!m) throw new Error("no modifier called " + p);
      mods[m] = true;
    }
    if (k.length > 1) {
      const named = NAMED[k.trim().toLowerCase()];
      if (!named) throw new Error("no key called " + k);
      k = named;
    }
    const init = {key: k, bubbles: true, cancelable: true, composed: true, ...mods};
    const go = target.dispatchEvent(new KeyboardEvent("keydown", init));
    if (k.length === 1 && !mods.ctrlKey && !mods.altKey && !mods.metaKey) {
      target.dispatchEvent(new KeyboardEvent("keypress", init));
    }
    if (go) {
      // What the browser would have done with a real key, where a page
      // script can do it too.
      const editable = target.isContentEditable || ["INPUT", "TEXTAREA"].includes(target.tagName);
      if (mods.ctrlKey && k.toLowerCase() === "a") document.execCommand("selectAll");
      else if (k.length === 1 && !mods.ctrlKey && !mods.altKey && !mods.metaKey && editable) {
        document.execCommand("insertText", false, k);
      } else if (k === "Backspace" && editable) document.execCommand("delete");
      else if (k === "Delete" && editable) document.execCommand("forwardDelete");
      else if (k === "Enter") {
        if (target.tagName === "TEXTAREA" || target.isContentEditable) {
          document.execCommand("insertParagraph");
        } else if (target.form) {
          target.form.requestSubmit();
        } else if (target.tagName === "A" || target.tagName === "BUTTON") {
          target.click();
        }
      } else if (k === "Tab") {
        const list = focusables();
        const i = list.indexOf(document.activeElement);
        const next = list[(i + (mods.shiftKey ? -1 : 1) + list.length) % list.length];
        if (next) next.focus();
      } else if (["PageDown", "PageUp", "Home", "End"].includes(k) && !editable) {
        const h = innerHeight * 0.9;
        if (k === "PageDown") scrollBy(0, h);
        else if (k === "PageUp") scrollBy(0, -h);
        else scrollTo(0, k === "Home" ? 0 : document.body.scrollHeight);
      }
    }
    target.dispatchEvent(new KeyboardEvent("keyup", init));
  }

  function keys({keys: list = []} = {}) {
    for (const item of list) {
      key(document.activeElement || document.body, String(item));
    }
    const a = document.activeElement;
    return a && a !== document.body ? (a.tagName.toLowerCase() + (a.id ? "#" + a.id : "")) : "";
  }

  // ── uploading ────────────────────────────────────────────────────────
  // How well an <input type=file accept=...> takes a file: 2 when a token
  // names its type or extension, 1 when it takes anything, 0 when not.
  function fits(el, name, type) {
    const accept = (el.getAttribute("accept") || "").trim();
    if (!accept || accept === "*" || accept === "*/*") return 1;
    const ext = "." + (name.split(".").pop() || "").toLowerCase();
    for (let t of accept.split(",")) {
      t = t.trim().toLowerCase();
      if (!t) continue;
      if (t === ext || t === type) return 2;
      if (t.endsWith("/*") && type.startsWith(t.slice(0, -1))) return 2;
      if (t === "*" || t === "*/*") return 1;
    }
    return 0;
  }

  function uploadTarget(a, name, type) {
    if (a.ref !== undefined && a.ref !== null || a.selector) {
      const el = find(a);
      return el;
    }
    const boxes = [...document.querySelectorAll("input[type=file]")].filter((e) => !e.disabled);
    let best = null, score = 0;
    for (const e of boxes) {
      const f = fits(e, name, type);
      if (f > score) { best = e; score = f; }
    }
    if (!best) {
      throw new Error(boxes.length
        ? `no upload box here takes ${name} (${type || "unknown type"})`
        : "no upload box on this page — open its attach menu first (the paperclip or + button), then try again");
    }
    return best;
  }

  function upload({b64, name, type = "", ref, selector} = {}) {
    const el = uploadTarget({ref, selector}, name, type);
    if (!(el.tagName === "INPUT" && el.type === "file")) {
      throw new Error("that is not an upload box; to drop the file onto it, use drop");
    }
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, {type, lastModified: Date.now()}));
    // As the file picker leaves it: the box holds the file, and the page
    // hears input and change.
    el.files = dt.files;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
    return {how: "box", into: (el.getAttribute("accept") || "any file")};
  }

  // A drop has to be built in the page's own world: Firefox does not hand
  // the page the files of a drop made here. So this only finds the
  // target and marks it for that step (background.js, dropInPage).
  function markDrop({ref, selector} = {}) {
    if ((ref === undefined || ref === null) && !selector) {
      throw new Error("say where to drop it: ref or selector");
    }
    const el = find({ref, selector});
    const token = Math.random().toString(36).slice(2);
    el.setAttribute("data-indigo-drop", token);
    return token;
  }

  function scroll({pages = 1} = {}) {
    scrollBy(0, pages * innerHeight * 0.9);
    return Math.round(scrollY);
  }

  function where() {
    return {title: document.title, url: location.href, ready: document.readyState};
  }

  window.__indigo = {read, click, fill, keys, scroll, where, upload, markDrop};
})();
