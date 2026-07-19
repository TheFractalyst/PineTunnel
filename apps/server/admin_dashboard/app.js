const API = "/api/dashboard";

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  });
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  return r;
}

function showLogin() {
  document.getElementById("login-screen").classList.remove("hidden");
  document.getElementById("main-screen").classList.add("hidden");
}

function showMain() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("main-screen").classList.remove("hidden");
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2500);
}

async function login() {
  const code = document.getElementById("login-code").value.trim();
  const userId = parseInt(document.getElementById("login-user-id").value, 10);
  const err = document.getElementById("login-error");
  err.textContent = "";
  try {
    const r = await fetch(API + "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, user_id: userId }),
    });
    if (r.ok) { showMain(); loadOverview(); }
    else { err.textContent = "Invalid code or user ID"; }
  } catch (e) { err.textContent = "Connection failed"; }
}

async function logout() {
  await api("/logout", { method: "POST" });
  showLogin();
}

async function loadOverview() {
  const content = document.getElementById("content");
  content.innerHTML = `<div class="panel"><h2>Overview</h2><p>Loading...</p></div>`;
  try {
    const status = await (await api("/setup-status")).json();
    content.innerHTML = `
      <div class="panel"><h2>Overview</h2>
        <div class="stat-row">
          <div class="stat ${status.telegram_configured ? 'ok' : 'bad'}"><div class="v">${status.telegram_configured ? 'Yes' : 'No'}</div><div class="l">Telegram</div></div>
          <div class="stat ${status.cloudflare_configured ? 'ok' : 'warn'}"><div class="v">${status.cloudflare_configured ? 'Yes' : 'No'}</div><div class="l">Cloudflare</div></div>
          <div class="stat ${status.initialized ? 'ok' : 'warn'}"><div class="v">${status.initialized ? 'Yes' : 'No'}</div><div class="l">Initialized</div></div>
        </div>
      </div>`;
  } catch (e) { content.innerHTML = `<div class="panel"><h2>Overview</h2><p>Failed to load.</p></div>`; }
}

async function loadSettings() {
  const content = document.getElementById("content");
  content.innerHTML = `<div class="panel"><h2>Settings</h2><p>Loading...</p></div>`;
  try {
    const cfg = await (await api("/config")).json();
    const rows = Object.entries(cfg).map(([k, v]) =>
      `<div class="config-row"><label>${k}</label><input value="${v}" disabled></div>`).join("");
    content.innerHTML = `<div class="panel"><h2>Settings (.env - read only in v1)</h2>${rows}</div>`;
  } catch (e) { content.innerHTML = `<div class="panel"><h2>Settings</h2><p>Failed to load.</p></div>`; }
}

async function loadSetup() {
  const content = document.getElementById("content");
  content.innerHTML = `<div class="panel"><h2>Setup Wizard</h2><p>Step 1: Configure Telegram bot (coming in Phase 1 update)</p><p>Step 2: Connect Cloudflare tunnel (Phase 2)</p><p>Step 3: Install EA (Phase 3)</p></div>`;
}

function route() {
  const hash = window.location.hash.slice(1) || "overview";
  document.querySelectorAll(".nav-link").forEach(a => a.classList.remove("active"));
  const link = document.querySelector(`.nav-link[href="#${hash}"]`);
  if (link) link.classList.add("active");
  if (hash === "settings") loadSettings();
  else if (hash === "setup") loadSetup();
  else loadOverview();
}

document.getElementById("login-btn").addEventListener("click", login);
document.getElementById("logout-btn").addEventListener("click", logout);
document.querySelectorAll(".nav-link").forEach(a => a.addEventListener("click", () => route()));
window.addEventListener("hashchange", route);

(async function init() {
  try {
    const r = await fetch(API + "/setup-status");
    if (r.status === 200) { showMain(); route(); }
    else { showLogin(); }
  } catch (e) { showLogin(); }
})();
