// The one thing that needs a person: Firefox grants the user-scripts
// permission (the agent's JavaScript tool) only from a click.
const state = document.getElementById("state");
const allow = document.getElementById("allow");

async function refresh() {
  const on = await browser.permissions.contains({permissions: ["userScripts"]});
  state.textContent = on ? "JavaScript is allowed." : "JavaScript is not allowed yet.";
  allow.hidden = on;
}

allow.addEventListener("click", async () => {
  await browser.permissions.request({permissions: ["userScripts"]});
  refresh();
});

refresh();
