// The one thing that needs a person: Firefox grants the user-scripts
// permission (the agent's JavaScript tool) only from a click.
const link = document.getElementById("link");
const state = document.getElementById("state");
const allow = document.getElementById("allow");

async function refresh() {
  let s = {port: false, shell: false, version: ""};
  try { s = await browser.runtime.sendMessage({type: "status"}); } catch (e) { /* no answer */ }
  link.textContent = s.shell ? "● Connected to indigoshell."
    : s.port ? "○ Waiting for indigoshell — is the shell running?"
    : "○ Not connected: the native host is not installed or did not start.";
  link.className = s.shell ? "on" : "off";
  const on = await browser.permissions.contains({permissions: ["userScripts"]});
  state.textContent = on ? "JavaScript is allowed." : "JavaScript is not allowed yet.";
  allow.hidden = on;
}

allow.addEventListener("click", async () => {
  await browser.permissions.request({permissions: ["userScripts"]});
  refresh();
});

refresh();
setInterval(refresh, 2000);
