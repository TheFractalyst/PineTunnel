const API = "/api/dashboard";
const POLL_INTERVAL = 10000;
let pollTimers = [];

const ICONS = {
  overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/></svg>',
  signals: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12h4l3-9 6 18 3-9h4"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  setup: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  external: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
};

const cache = {};
async function useFetch(path, opts = {}) {
  const cached = cache[path];
  const interval = opts.interval || POLL_INTERVAL;
  try {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" } });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    cache[path] = data;
    return { data, error: null, loading: false };
  } catch (e) {
    return { data: cached || null, error: e.message, loading: false };
  }
}

function clearPolls() {
  pollTimers.forEach(t => clearInterval(t));
  pollTimers = [];
}

function toast(msg, type = "ok") {
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast("Copied to clipboard"));
}

function render() {
  clearPolls();
  const app = document.getElementById("app");
  const nav = [
    { id: "overview", label: "Overview", icon: ICONS.overview },
    { id: "setup", label: "Setup", icon: ICONS.setup },
    { id: "settings", label: "Settings", icon: ICONS.settings },
  ];
  app.innerHTML = `
    <div class="layout">
      <nav class="sidebar">
        <div class="brand"><img src="/admin/assets/banner.svg" alt="PineTunnel" class="brand-logo"></div>
        ${nav.map(n => `<a class="nav-item" data-route="${n.id}">${n.icon}<span>${n.label}</span></a>`).join("")}
        <div class="spacer"></div>
        <div class="footer"><span class="pulse-dot"></span><span>System Online - v1.0</span></div>
      </nav>
      <div class="main-area">
        <header class="header">
          <div class="title" id="page-title">Overview</div>
          <div class="actions" id="header-actions"></div>
        </header>
        <main class="content" id="content"></main>
      </div>
    </div>
    <nav class="mobile-nav">
      ${nav.map(n => `<a class="tab" data-route="${n.id}">${n.icon}<span>${n.label}</span></a>`).join("")}
    </nav>
  `;
  document.querySelectorAll("[data-route]").forEach(el => {
    el.addEventListener("click", e => {
      e.preventDefault();
      route(el.dataset.route);
    });
  });
}

function route(id) {
  document.querySelectorAll("[data-route]").forEach(el => {
    el.classList.toggle("active", el.dataset.route === id);
  });
  const titles = { overview: "Overview", setup: "Setup Wizard", settings: "Settings" };
  document.getElementById("page-title").textContent = titles[id] || id;
  const content = document.getElementById("content");
  const actions = document.getElementById("header-actions");
  actions.innerHTML = "";
  if (id === "overview") renderOverview(content, actions);
  else if (id === "setup") renderSetup(content);
  else if (id === "settings") renderSettings(content);
}

function skeletonCard(cols = 3) {
  return `<div class="card"><div class="grid grid-${cols}">${Array(cols).fill('<div><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}</div></div>`;
}

function badge(state, text, pulse = false) {
  const cls = state === "ok" ? "ok" : state === "bad" ? "bad" : state === "warn" ? "warn" : "info";
  return `<span class="badge ${cls} ${pulse ? "pulse" : ""}"><span class="dot"></span>${text}</span>`;
}

async function renderOverview(content, actions) {
  content.innerHTML = skeletonCard(3);
  const { data, error } = await useFetch(`${API}/setup-status`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load status</div><div class="sub">${error}</div></div>`;
    return;
  }
  const tg = data.telegram_configured;
  const cf = data.cloudflare_configured;
  const init = data.initialized;
  const allDone = tg && cf && init;
  actions.innerHTML = badge(allDone ? "ok" : "warn", allDone ? "All Ready" : "Setup Needed", !allDone);

  content.innerHTML = `
    <div class="card">
      <div class="card-title">System Status</div>
      <div class="card-desc">Current configuration state</div>
      <div class="grid grid-3">
        <div class="stat ${tg ? "ok" : "bad"}">
          <div class="value">${tg ? "Connected" : "Not Set"}</div>
          <div class="label">Telegram Bot</div>
        </div>
        <div class="stat ${cf ? "ok" : "warn"}">
          <div class="value">${cf ? "Active" : "Not Set"}</div>
          <div class="label">Cloudflare Tunnel</div>
        </div>
        <div class="stat ${init ? "ok" : "warn"}">
          <div class="value">${init ? "Yes" : "No"}</div>
          <div class="label">Initialized</div>
        </div>
      </div>
    </div>
    ${!allDone ? `
    <div class="card">
      <div class="card-title">${ICONS.alert}Setup Incomplete</div>
      <div class="card-desc">Complete these steps to start receiving TradingView webhooks</div>
      <div class="steps">
        <div class="step ${tg ? "done" : ""}">
          <div class="num">${tg ? ICONS.check : "1"}</div>
          <div class="body">
            <div class="t">Configure Telegram Bot</div>
            <div class="d">${tg ? "Done" : "Required for login and trade alerts"}</div>
            <div class="action"><button class="btn outline sm" onclick="route('setup')">Go to Setup</button></div>
          </div>
        </div>
        <div class="step ${cf ? "done" : ""}">
          <div class="num">${cf ? ICONS.check : "2"}</div>
          <div class="body">
            <div class="t">Connect Cloudflare Tunnel</div>
            <div class="d">${cf ? "Done" : "Provides public HTTPS URL for TradingView webhooks"}</div>
            <div class="action"><button class="btn outline sm" onclick="route('setup')">Go to Setup</button></div>
          </div>
        </div>
        <div class="step ${init ? "done" : ""}">
          <div class="num">${init ? ICONS.check : "3"}</div>
          <div class="body">
            <div class="t">Get Your Webhook URL</div>
            <div class="d">${init ? "Done" : "Copy to TradingView after Cloudflare is connected"}</div>
          </div>
        </div>
      </div>
    </div>` : `
    <div class="card">
      <div class="card-title">TradingView Webhook URL</div>
      <div class="card-desc">Paste this into TradingView Alert -> Notifications -> Webhook URL</div>
      <div class="field">
        <input class="input" value="${data.server_url || "http://127.0.0.1:8000"}" readonly>
        <div class="hint">Only ports 80 and 443 are accepted by TradingView. Use the Cloudflare tunnel URL.</div>
      </div>
      <button class="btn primary sm" onclick="copy(this.previousElementSibling.previousElementSibling.value)">${ICONS.copy}Copy URL</button>
    </div>`}
  `;
}

async function renderSetup(content) {
  content.innerHTML = skeletonCard(1);
  const { data } = await useFetch(`${API}/setup-status`);
  const tg = data?.telegram_configured;
  const cf = data?.cloudflare_configured;

  content.innerHTML = `
    <div class="card">
      <div class="card-title">Step 1: Telegram Bot</div>
      <div class="card-desc">Required for dashboard login and trade alerts</div>
      ${tg ? `
        <div class="row"><span class="k">Status</span><span class="v">${badge("ok", "Configured")}</span></div>
        <div class="row"><span class="k">Bot Token</span><span class="v">**** (set)</span></div>
      ` : `
        <div class="field">
          <label>1. Create a bot via @BotFather on Telegram</label>
          <div class="hint">Open Telegram, message @BotFather, send /newbot, follow prompts</div>
        </div>
        <div class="field">
          <label>2. Paste bot token</label>
          <input class="input" id="tg-token" type="password" placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11">
        </div>
        <div class="field">
          <label>3. Get your Telegram user ID</label>
          <div class="hint">Message @userinfobot on Telegram, it replies with your numeric ID</div>
          <input class="input" id="tg-uid" type="number" placeholder="123456789">
        </div>
        <button class="btn primary" id="save-tg" onclick="saveTelegram()">${ICONS.check}Save and Verify</button>
        <div id="tg-result"></div>
      `}
    </div>

    <div class="card">
      <div class="card-title">Step 2: Cloudflare Tunnel</div>
      <div class="card-desc">Provides public HTTPS URL for TradingView webhooks</div>
      ${cf ? `
        <div class="row"><span class="k">Status</span><span class="v">${badge("ok", "Connected")}</span></div>
        <div class="row"><span class="k">URL</span><span class="v">${data?.server_url || "https://..."}</span></div>
      ` : `
        <div class="field">
          <label>Option A: I have a Cloudflare domain (recommended)</label>
          <div class="hint">Dashboard will create the tunnel for you. Coming in Phase 2.</div>
        </div>
        <div class="field">
          <label>Option B: I have a tunnel token already</label>
          <input class="input" id="cf-token" placeholder="eyJ..." disabled>
          <input class="input mt" id="cf-url" placeholder="https://pinetunnel.example.com" disabled>
        </div>
        <button class="btn outline" disabled>${ICONS.external}Connect (Phase 2)</button>
      `}
    </div>

    <div class="card">
      <div class="card-title">Step 3: TradingView Webhook</div>
      <div class="card-desc">After Cloudflare is connected, paste this URL into TradingView</div>
      <div class="field">
        <input class="input" value="${cf ? (data?.server_url || "") : "Complete Step 2 first"}" readonly>
      </div>
      <button class="btn outline sm" onclick="copy(this.previousElementSibling.previousElementSibling.value)" ${cf ? "" : "disabled"}>${ICONS.copy}Copy</button>
      <div class="hint mt">In TradingView: Chart -> Alert -> Notifications -> Webhook URL</div>
    </div>
  `;
}

async function saveTelegram() {
  const btn = document.getElementById("save-tg");
  const token = document.getElementById("tg-token").value.trim();
  const uid = document.getElementById("tg-uid").value.trim();
  const result = document.getElementById("tg-result");
  if (!token || !uid) {
    result.innerHTML = `<div class="inline-error">Both token and user ID are required</div>`;
    return;
  }
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>Verifying...`;
  try {
    const vr = await fetch(`${API}/validate-telegram`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, user_id: uid }),
    });
    const vdata = await vr.json();
    if (!vr.ok || !vdata.valid) {
      btn.disabled = false;
      btn.innerHTML = `${ICONS.check}Save and Verify`;
      result.innerHTML = `<div class="inline-error">Invalid bot token${vdata.error ? ": " + vdata.error : ""}</div>`;
      return;
    }
    result.innerHTML = `<div class="inline-ok">Connected as ${vdata.bot_username}</div>`;
    btn.innerHTML = `<span class="spin"></span>Saving...`;
    const r = await fetch(`${API}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates: { TELEGRAM_BOT_TOKEN: token, TELEGRAM_ADMIN_IDS: uid } }),
    });
    if (r.ok) {
      btn.innerHTML = `${ICONS.check}Saved`;
      result.innerHTML = `<div class="inline-ok">Connected as ${vdata.bot_username} - ${vdata.bot_name} saved.</div>`;
      toast(`Telegram configured - ${vdata.bot_name}`, "ok");
      setTimeout(() => route("overview"), 2000);
    } else {
      throw new Error(r.status);
    }
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = `${ICONS.check}Save and Verify`;
    result.innerHTML = `<div class="inline-error">Failed: ${e.message}</div>`;
  }
}

async function renderSettings(content) {
  content.innerHTML = skeletonCard(1);
  const { data, error } = await useFetch(`${API}/config`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load settings</div></div>`;
    return;
  }
  const entries = Object.entries(data).filter(([k]) => !k.startsWith("#") && k);
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Configuration</div>
      <div class="card-desc">Environment variables (secrets are redacted)</div>
      ${entries.map(([k, v]) => `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("")}
    </div>
  `;
}

window.route = route;
window.saveTelegram = saveTelegram;
window.copy = copy;

(async function init() {
  await useFetch(`${API}/setup-status`);
  render();
  route("overview");
})();
