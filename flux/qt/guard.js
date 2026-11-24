// Flux's guard on the agent's own scripts (script.py, the `js` request),
// run in their world (script.WORLD) of a document before any of their
// code, and in that world only: the page's own scripts and Flux's reader
// (other worlds, other copies of every prototype) keep all of it.
//
// What it does: storage, cookies and sign-ins throw when touched; frames
// and new windows (a realm of their own, where none of this would be
// guarded) give no way in through their elements; a password field's
// value reads "(password)", as Flux's reads say it, a form's FormData
// says the same, and a password field keeps its type. Fixed in place (not
// configurable), and what it checks with taken before any of the agent's
// code ran (a global the code replaces later fools nothing here).
//
// What it is not: a sandbox. The code still reads and changes the page's
// DOM, runs with the page's origin (fetch and XHR carry the page's
// cookies, and the code reads what comes back), clicks and submits; a
// frame made after the guard ran is reachable as window[N], unguarded (a
// window's indexed frames cannot be redefined); a loop that never ends
// hangs the page. It keeps mistakes and casual use from reaching what the
// user keeps: cookies, storage, credentials, passwords.
//
// Kept to what Chromium 122 has (Qt 6.8's QtWebEngine).
(() => {
"use strict";
if (typeof window.__fluxJs === "object") return;

const PASSWORD = "(password)";
const RESULT_MAX = __RESULT_MAX__;      // characters of an answer's JSON kept (script.py)
const has = Object.prototype.hasOwnProperty;
const define = Object.defineProperty;
const describe = Object.getOwnPropertyDescriptor;
const protoOf = Object.getPrototypeOf;
const stringify = JSON.stringify;
const toList = Array.from;
const Fault = Error;

function refuse(what) {
  return function () { throw new Fault(`${what} is kept from the agent's scripts (Flux's guard)`); };
}

// `name` on `obj` and on every prototype above it that has it: a getter
// and a setter that throw, fixed. `only`: none put where there was none.
function block(obj, name, what, only) {
  const stop = refuse(what || name);
  let found = false;
  for (let o = obj; o; o = protoOf(o)) {
    if (!has.call(o, name)) continue;
    found = true;
    try { define(o, name, {get: stop, set: stop, enumerable: false, configurable: false}); }
    catch (e) { /* not configurable already */ }
  }
  if (!found && !only) {
    try { define(obj, name, {get: stop, set: stop, enumerable: false, configurable: false}); }
    catch (e) { /* a name the window will not take */ }
  }
}

// What a thing is, by asking a getter of the kind: it throws for any other.
function brand(getter) {
  return x => {
    if (x === null || typeof x !== "object") return false;
    try { getter.call(x); return true; } catch (e) { return false; }
  };
}

// ── cookies, storage, sign-ins, new realms ──────────────────────────────
block(document, "cookie", "document.cookie");
for (const name of ["localStorage", "sessionStorage", "indexedDB", "caches", "cookieStore",
                    "opener", "Worker", "SharedWorker", "webkitRequestFileSystem",
                    "webkitResolveLocalFileSystemURL", "showOpenFilePicker", "showSaveFilePicker",
                    "showDirectoryPicker"])
  block(window, name);
block(window, "open", "window.open");
for (const name of ["credentials", "storage", "serviceWorker", "webkitPersistentStorage",
                    "webkitTemporaryStorage"])
  block(navigator, name, "navigator." + name);
for (const kind of ["HTMLIFrameElement", "HTMLFrameElement", "HTMLObjectElement", "HTMLEmbedElement"]) {
  const proto = window[kind] && window[kind].prototype;
  if (proto)
    for (const name of ["contentWindow", "contentDocument", "getSVGDocument"])
      block(proto, name, `a frame's ${name}`, true);
}

// ── password fields ─────────────────────────────────────────────────────
const input = HTMLInputElement.prototype;
const value = describe(input, "value");
const type = describe(input, "type");
const ownerOf = describe(Attr.prototype, "ownerElement").get;
const nameOf = describe(Attr.prototype, "localName").get;
const attributesOf = describe(Element.prototype, "attributes").get;
const inputs = Document.prototype.querySelectorAll.bind(document, "input");
const isInput = brand(type.get);
const isAttr = brand(ownerOf);
const isPassword = el => isInput(el) && type.get.call(el) === "password";
const staysPassword = () => { throw new Fault("a password field keeps its type (Flux's guard)"); };
const local = qualified => String(qualified).split(":").pop().toLowerCase();
const isMap = brand(describe(NamedNodeMap.prototype, "length").get);

define(input, "value", {
  get() { const v = value.get.call(this); return isPassword(this) && v ? PASSWORD : v; },
  set(v) { value.set.call(this, v); }, enumerable: true, configurable: false});
define(input, "type", {
  get() { return type.get.call(this); },
  set(v) {
    if (isPassword(this) && String(v).toLowerCase() !== "password") staysPassword();
    type.set.call(this, v);
  }, enumerable: true, configurable: false});

// The type attribute, every way it is set or taken: not a password field's.
function wrapMethod(proto, name, check) {
  const original = proto[name];
  if (typeof original !== "function") return;
  define(proto, name, {value: {[name](...args) {
    check(this, args);
    return original.apply(this, args);
  }}[name], writable: false, enumerable: true, configurable: false});
}

const element = Element.prototype;
for (const [name, at, valueAt] of [["setAttribute", 0, 1], ["setAttributeNS", 1, 2],
                                   ["removeAttribute", 0, -1], ["removeAttributeNS", 1, -1],
                                   ["toggleAttribute", 0, -1]])
  wrapMethod(element, name, (el, args) => {
    if (isPassword(el) && local(args[at]) === "type" &&
        !(valueAt >= 0 && String(args[valueAt]).toLowerCase() === "password")) staysPassword();
  });
for (const name of ["setAttributeNode", "setAttributeNodeNS", "removeAttributeNode"])
  wrapMethod(element, name, (el, args) => {
    if (isPassword(el) && isAttr(args[0]) && nameOf.call(args[0]) === "type") staysPassword();
  });
const fieldOfMap = map => toList(inputs()).find(el => attributesOf.call(el) === map) || null;
for (const name of ["setNamedItem", "setNamedItemNS", "removeNamedItem", "removeNamedItemNS"])
  wrapMethod(NamedNodeMap.prototype, name, (map, args) => {
    const last = args[args.length - 1];
    const named = isAttr(last) ? nameOf.call(last) : local(last);
    if (named === "type" && isMap(map) && isPassword(fieldOfMap(map))) staysPassword();
  });
for (const name of ["value", "nodeValue", "textContent"]) {
  let d = null;
  for (let o = Attr.prototype; o && !d; o = protoOf(o)) d = describe(o, name);
  if (!d || !d.set) continue;
  const {get, set} = d;
  define(Attr.prototype, name, {get() { return get.call(this); }, set(v) {
    if (isAttr(this) && nameOf.call(this) === "type" && isPassword(ownerOf.call(this)) &&
        String(v).toLowerCase() !== "password") staysPassword();
    set.call(this, v);
  }, enumerable: true, configurable: false});
}

// FormData of a form: its password fields' values as "(password)".
const FormDataOf = FormData;
const fd = FormDataOf.prototype;
const [getAll, remove, append] = [fd.getAll, fd.delete, fd.append];
const elementsOf = describe(HTMLFormElement.prototype, "elements").get;
const isForm = brand(elementsOf);
function GuardedFormData(form, submitter) {
  const data = arguments.length === 0 ? new FormDataOf()
    : arguments.length === 1 ? new FormDataOf(form) : new FormDataOf(form, submitter);
  if (isForm(form)) {
    const names = new Set();
    for (const el of toList(elementsOf.call(form))) if (isPassword(el) && el.name) names.add(el.name);
    for (const name of names) {
      const n = getAll.call(data, name).length;
      remove.call(data, name);
      for (let i = 0; i < n; i++) append.call(data, name, PASSWORD);
    }
  }
  return data;
}
GuardedFormData.prototype = fd;
define(fd, "constructor", {value: GuardedFormData, writable: false, enumerable: false,
                           configurable: false});
define(window, "FormData", {value: GuardedFormData, writable: false, enumerable: false,
                            configurable: false});

// ── answers ─────────────────────────────────────────────────────────────
// The code's answer as JSON: nodes as "<tag#id.class>", a window as
// "[window]", functions by name, Maps and Sets and node lists as lists, a
// value met again (a cycle) as "[seen]"; at most RESULT_MAX characters and
// a margin (Flux takes the passwords out first, then cuts).
const [NodeOf, WindowOf, MapOf, SetOf, ListOf, CollectionOf, WeakSetOf] =
  [Node, Window, Map, Set, NodeList, HTMLCollection, WeakSet];
const resolve = Promise.resolve.bind(Promise), reject = Promise.reject.bind(Promise);
const assign = Object.assign;

function nodeName(n) {
  if (n.nodeType === 1) {
    const classes = typeof n.className === "string" ? n.className.trim().split(/\s+/).filter(Boolean) : [];
    return `<${n.localName}${n.id ? "#" + n.id : ""}${classes.slice(0, 3).map(c => "." + c).join("")}>`;
  }
  if (n.nodeType === 3) return n.data;
  if (n.nodeType === 9) return "[document]";
  return `[${n.nodeName}]`;
}

function encode(v) {
  const seen = new WeakSetOf();
  const json = stringify(v, function (key, x) {
    if (typeof x === "bigint") return x.toString();
    if (typeof x === "function") return x.name ? `[function ${x.name}]` : "[function]";
    if (typeof x === "symbol") return String(x);
    if (x && typeof x === "object") {
      if (x instanceof NodeOf) return nodeName(x);
      if (x === window || x instanceof WindowOf) return "[window]";
      if (x instanceof Fault) return `${x.name}: ${x.message}`;
      if (x instanceof MapOf) return toList(x.entries());
      if (x instanceof SetOf || x instanceof ListOf || x instanceof CollectionOf) return toList(x);
      if (seen.has(x)) return "[seen]";
      seen.add(x);
    }
    return x;
  });
  const text = json === undefined ? "null" : json;
  return {type: v === null ? "null" : Array.isArray(v) ? "array" : typeof v, length: text.length,
          json: text.length > RESULT_MAX + 1000 ? text.slice(0, RESULT_MAX + 1000) : text};
}

function said(e) {
  if (e instanceof Fault) return `${e.name}: ${e.message}`;
  try { return `a thrown ${typeof e}: ${String(e)}`; } catch (x) { return "a thrown value"; }
}

const runs = new MapOf();         // id -> {done, ...the answer}

function start(id, fn) {
  const run = {done: false, dropped: false};
  runs.set(id, run);
  const end = answer => { if (!run.dropped) assign(run, answer, {done: true}); };
  let promise;
  try { promise = resolve(fn()); } catch (e) { promise = reject(e); }
  promise.then(v => {
    try { end({ok: true, ...encode(v)}); }
    catch (e) { end({ok: false, error: "its answer could not be made JSON: " + said(e)}); }
  }, e => end({ok: false, error: said(e)}));
  return stringify({started: true});
}

function take(id) {
  const run = runs.get(id);
  if (!run) return stringify({gone: true});
  if (!run.done) return stringify({done: false});
  runs.delete(id);
  return stringify(run);
}

function drop(id) {
  const run = runs.get(id);
  if (run) { run.dropped = true; runs.delete(id); }
  return "true";
}

define(window, "__fluxJs", {value: Object.freeze({start, take, drop}), writable: false,
                            enumerable: false, configurable: false});
})();
