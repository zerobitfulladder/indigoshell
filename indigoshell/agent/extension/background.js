// indigoshell bridge — the extension's side of the call agent's browser
// tools. It connects to the shell through a native messaging host (see
// indigoshell/agent/browser_host.py) and answers requests:
//
//   {id, op, args}  ->  {id, ok: true, result} | {id, ok: false, error}
//
// Ops act on tabs here, or inject page.js into a tab and call one of its
// helpers. Nothing listens on a port: only this extension can start the
// host, and only the shell can talk to the host.

const HOST = "indigoshell";
const LOAD_MS = 8000;

let port = null;
// Pings to the shell, by id. The host keeps the port open while the
// shell is down, so an open port alone does not say the shell is there:
// asking it does.
const pings = new Map();
let pingCount = 0;

function pingShell(ms = 1000) {
  if (!port) return Promise.resolve(false);
  const id = "ping" + (++pingCount);
  return new Promise((resolve) => {
    const timer = setTimeout(() => { pings.delete(id); resolve(false); }, ms);
    pings.set(id, () => { clearTimeout(timer); resolve(true); });
    port.postMessage({id, ping: true});
  });
}
// Files on their way into a page, by upload id: Firefox caps a message
// from the host at 1 MB, so the shell sends them in base64 pieces.
const uploads = new Map();

function connect() {
  if (port) return;
  try {
    port = browser.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    return;
  }
  port.onMessage.addListener(onMessage);
  port.onDisconnect.addListener(() => { port = null; });
}

// The host keeps the port open while the shell restarts, so this only
// matters when Firefox starts, or the host itself was stopped: then the
// alarm brings the connection back within a minute, and any tab event
// sooner.
browser.alarms.create("indigoshell-connect", {periodInMinutes: 1});
browser.alarms.onAlarm.addListener(connect);
browser.runtime.onStartup.addListener(connect);
browser.tabs.onActivated.addListener(connect);
connect();

async function onMessage(msg) {
  if (msg.pong) {                 // the shell answering a ping
    const done = pings.get(msg.id);
    pings.delete(msg.id);
    if (done) done();
    return;
  }
  let reply;
  try {
    reply = {id: msg.id, ok: true, result: await handle(msg.op, msg.args || {})};
  } catch (e) {
    reply = {id: msg.id, ok: false, error: String((e && e.message) || e)};
  }
  if (port) port.postMessage(reply);
}

// The toolbar popup and the options page ask how the bridge is doing.
browser.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "status") {
    return pingShell().then((shell) => ({
      port: !!port, shell, version: browser.runtime.getManifest().version}));
  }
  return undefined;
});

// ── tabs ───────────────────────────────────────────────────────────────
function info(t, focusedWindow) {
  return {id: t.id, window: t.windowId, index: t.index, title: t.title || "",
          url: t.url || "", active: t.active, status: t.status,
          focused: t.active && t.windowId === focusedWindow};
}

async function focusedWindow() {
  try {
    return (await browser.windows.getLastFocused()).id;
  } catch (e) {
    return -1;
  }
}

async function waitLoad(tabId, ms = LOAD_MS) {
  const end = Date.now() + ms;
  await new Promise((r) => setTimeout(r, 300));
  while (Date.now() < end) {
    let t;
    try { t = await browser.tabs.get(tabId); } catch (e) { return; }
    if (t.status === "complete") return;
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function show(tabId) {
  const t = await browser.tabs.update(tabId, {active: true});
  await browser.windows.update(t.windowId, {focused: true});
  return t;
}

// ── a page's helpers ───────────────────────────────────────────────────
async function inPage(tabId, fn, args) {
  const target = {tabId};
  await browser.scripting.executeScript({target, files: ["page.js"]});
  const [res] = await browser.scripting.executeScript({
    target, func: (name, a) => window.__indigo[name](a), args: [fn, args]});
  if (!res) throw new Error("the page gave no answer");
  if (res.error) throw new Error(res.error.message || String(res.error));
  return res.result;
}

// Runs in the page's own world (see page.js, markDrop): the file and the
// drag events are the page's own objects, so its drop handler can read
// them.
function dropInPage(b64, name, type, token) {
  const el = document.querySelector(`[data-indigo-drop="${token}"]`);
  if (!el) throw new Error("the drop target went away");
  el.removeAttribute("data-indigo-drop");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([bytes], name, {type, lastModified: Date.now()}));
  el.scrollIntoView({block: "center"});
  const r = el.getBoundingClientRect();
  const at = {bubbles: true, cancelable: true, composed: true, dataTransfer: dt,
              clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
  for (const kind of ["dragenter", "dragover", "drop"]) {
    el.dispatchEvent(new DragEvent(kind, at));
  }
  return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "");
}

async function handle(op, a) {
  switch (op) {
    case "hello":
      return {version: browser.runtime.getManifest().version,
              javascript: !!browser.userScripts};

    case "tabs": {
      const fw = await focusedWindow();
      return (await browser.tabs.query({})).map((t) => info(t, fw));
    }

    case "open": {
      let t;
      if (a.tab === undefined || a.tab === null) {
        t = await browser.tabs.create({url: a.url, active: true});
      } else {
        t = await browser.tabs.update(a.tab, {url: a.url, active: true});
      }
      await browser.windows.update(t.windowId, {focused: true});
      await waitLoad(t.id);
      return info(await browser.tabs.get(t.id), t.windowId);
    }

    case "show":
      return info(await show(a.tab), (await browser.tabs.get(a.tab)).windowId);

    case "close":
      await browser.tabs.remove(a.tab);
      return true;

    case "go": {
      const to = String(a.to || "").trim();
      const low = to.toLowerCase();
      if (low === "back") await browser.tabs.goBack(a.tab);
      else if (low === "forward") await browser.tabs.goForward(a.tab);
      else if (low === "reload") await browser.tabs.reload(a.tab);
      else await browser.tabs.update(a.tab, {url: to});
      await waitLoad(a.tab);
      return info(await browser.tabs.get(a.tab), -1);
    }

    case "page": {
      const out = await inPage(a.tab, a.fn, a.args || {});
      if (a.wait) await waitLoad(a.tab, a.wait);
      return out;
    }

    case "where": {
      const t = await browser.tabs.get(a.tab);
      return {title: t.title || "", url: t.url || "", status: t.status};
    }

    case "js": {
      if (!browser.userScripts) {
        throw new Error("JavaScript is not allowed yet: open the INDIGO "
          + "bridge's options (about:addons) and press Allow JavaScript");
      }
      const results = await browser.userScripts.execute({
        target: {tabId: a.tab}, js: [{code: a.code}], world: "MAIN",
        injectImmediately: true});
      const r = results && results[0];
      if (!r) throw new Error("the page gave no answer");
      if (r.error) throw new Error(String(r.error.message || r.error));
      return r.result === undefined ? null : r.result;
    }

    case "shot":
      return await browser.tabs.captureTab(a.tab, {format: "png"});

    case "upload_part": {
      const u = uploads.get(a.id) || {parts: []};
      u.parts[a.index] = a.data;
      uploads.set(a.id, u);
      return a.index;
    }

    case "upload": {
      const u = uploads.get(a.id);
      uploads.delete(a.id);
      if (!u || u.parts.length !== a.parts || u.parts.some((p) => p === undefined)) {
        throw new Error("the file did not arrive whole; try again");
      }
      const b64 = u.parts.join("");
      if (!a.drop) {
        return await inPage(a.tab, "upload", {
          b64, name: a.name, type: a.type, ref: a.ref, selector: a.selector});
      }
      const token = await inPage(a.tab, "markDrop", {ref: a.ref, selector: a.selector});
      const [res] = await browser.scripting.executeScript({
        target: {tabId: a.tab}, world: "MAIN", func: dropInPage,
        args: [b64, a.name, a.type, token]});
      if (!res) throw new Error("the page gave no answer");
      if (res.error) throw new Error(res.error.message || String(res.error));
      return {how: "drop", into: res.result};
    }

    case "options":                 // where Allow JavaScript lives
      await browser.runtime.openOptionsPage();
      return true;

    default:
      throw new Error("unknown op " + op);
  }
}
