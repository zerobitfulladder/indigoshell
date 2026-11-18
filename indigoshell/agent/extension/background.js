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
  let reply;
  try {
    reply = {id: msg.id, ok: true, result: await handle(msg.op, msg.args || {})};
  } catch (e) {
    reply = {id: msg.id, ok: false, error: String((e && e.message) || e)};
  }
  if (port) port.postMessage(reply);
}

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

    case "options":                 // where Allow JavaScript lives
      await browser.runtime.openOptionsPage();
      return true;

    default:
      throw new Error("unknown op " + op);
  }
}
