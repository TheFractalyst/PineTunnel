const API = "/api/dashboard";
const POLL_INTERVAL = 10000;
let pollTimers = [];
let currentRoute = "overview";
let lastSetupStatus = null;

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
  arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
};

const cache = {};
async function useFetch(path, opts = {}) {
  const cached = cache[path];
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

let healthState = { data: null, error: null, stale: false };
let healthTimer = null;
let healthActive = false;

function formatUptime(sec) {
  if (sec == null || isNaN(sec)) return "--";
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`;
  return `${Math.floor(sec / 86400)}d ${Math.round((sec % 86400) / 3600)}h`;
}

function loadColor(pct) {
  if (pct == null || isNaN(pct)) return "";
  return pct < 50 ? "ok" : pct <= 80 ? "warn" : "bad";
}

function eaColor(n) {
  return n > 0 ? "ok" : "warn";
}

async function fetchHealth() {
  const prev = healthState.data;
  try {
    const [hRes, cRes] = await Promise.all([
      fetch("/api/system/health", { headers: { "Content-Type": "application/json" } }),
      fetch("/api/connections", { headers: { "Content-Type": "application/json" } }),
    ]);
    if (!hRes.ok) throw new Error(`health ${hRes.status}`);
    const health = await hRes.json();
    let connections = null;
    if (cRes.ok) connections = await cRes.json();
    healthState = { data: { health, connections }, error: null, stale: false };
  } catch (e) {
    healthState = { data: prev, error: e.message, stale: true };
  }
  if (healthActive) updateHealthCard();
}

function startHealthPolling() {
  healthActive = true;
  if (healthTimer) return;
  fetchHealth();
  healthTimer = setInterval(fetchHealth, POLL_INTERVAL);
}

function stopHealthPolling() {
  healthActive = false;
  if (healthTimer) {
    clearInterval(healthTimer);
    healthTimer = null;
  }
}

function setTile(id, value, cls) {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.className = `stat ${cls}`;
  const v = tile.querySelector(".value");
  if (v) v.textContent = value;
}

function updateHealthCard() {
  const card = document.getElementById("health-card");
  if (!card) return;
  const { data, error, stale } = healthState;
  card.classList.toggle("stale", stale);
  const titleEl = card.querySelector(".card-title");
  const existingBadge = titleEl.querySelector(".stale-badge");
  if (stale && !existingBadge) {
    const b = document.createElement("span");
    b.className = "stale-badge";
    b.textContent = "stale";
    titleEl.appendChild(b);
  } else if (!stale && existingBadge) {
    existingBadge.remove();
  }
  if (!data && error) {
    const grid = document.getElementById("health-grid");
    if (grid) {
      grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="msg">Health unavailable</div><div class="sub">${error}</div></div>`;
    }
    return;
  }
  if (!data) return;
  const h = data.health;
  const c = data.connections;
  const uptimeSec = h.uptime_seconds;
  const cpu = h.system ? h.system.cpu_percent : null;
  const mem = h.system ? h.system.memory_percent : null;
  let eaCount = 0;
  if (c) {
    const http = c.http_polling_connections || 0;
    const ws = (c.websocket && c.websocket.websocket_connections) || 0;
    eaCount = http + ws;
  } else if (h.connections) {
    eaCount = h.connections.total_clients || 0;
  }
  setTile("tile-uptime", formatUptime(uptimeSec), "ok");
  setTile("tile-cpu", cpu != null ? `${cpu.toFixed(1)}%` : "--", loadColor(cpu));
  setTile("tile-mem", mem != null ? `${mem.toFixed(1)}%` : "--", loadColor(mem));
  setTile("tile-ea", String(eaCount), eaColor(eaCount));
}

function clearPolls() {
  pollTimers.forEach(t => clearInterval(t));
  pollTimers = [];
  stopHealthPolling();
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
        <div class="brand"><img src="/admin/assets/logo.svg" alt="PineTunnel" class="brand-logo"></div>
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
  currentRoute = id;
  clearPolls();
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

function startOverviewPoll() {
  const t = setInterval(async () => {
    if (currentRoute !== "overview") return;
    const { data } = await useFetch(`${API}/setup-status`);
    if (!data) return;
    const sig = JSON.stringify(data);
    if (sig !== lastSetupStatus) {
      lastSetupStatus = sig;
      const content = document.getElementById("content");
      const actions = document.getElementById("header-actions");
      if (content && actions) renderOverview(content, actions);
    }
  }, POLL_INTERVAL);
  pollTimers.push(t);
}

async function renderOverview(content, actions) {
  content.innerHTML = skeletonCard(3);
  const { data, error } = await useFetch(`${API}/setup-status`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load status</div><div class="sub">${error}</div><button class="btn outline sm mt" onclick="route('overview')">Retry</button></div>`;
    return;
  }
  const tg = data.telegram_configured;
  const cf = data.cloudflare_configured;
  const init = data.initialized;
  const allDone = tg && cf && init;
  lastSetupStatus = JSON.stringify(data);
  actions.innerHTML = badge(allDone ? "ok" : "warn", allDone ? "All Ready" : "Setup Needed", !allDone);

  const tgHint = tg ? "" : '<div class="stat-hint">Click Setup to configure</div>';
  const cfHint = cf ? "" : '<div class="stat-hint">Click Setup to configure</div>';
  const initHint = init ? "" : '<div class="stat-hint">Complete steps 1 and 2</div>';

  const webhookBlock = allDone ? `
    <div class="card webhook-card">
      <div class="card-title">Your TradingView Webhook URL</div>
      <div class="card-desc">Paste this into TradingView: Alert -> Notifications -> Webhook URL</div>
      <div class="webhook-display">
        <code class="webhook-url">${data.server_url || "http://127.0.0.1:8000"}</code>
        <button class="btn primary lg" id="copy-webhook" onclick="copyWebhook()">${ICONS.copy}Copy URL</button>
      </div>
      <div class="hint mt">Only ports 80 and 443 are accepted by TradingView. Use the Cloudflare tunnel URL.</div>
      <div class="webhook-test-section mt">
        <button class="btn outline" id="ov-test-toggle" onclick="toggleTestForm()">${ICONS.external}Test Webhook</button>
        <div id="ov-test-form" class="webhook-test-form" style="display:none">
          <div class="grid grid-3">
            <div class="field">
              <label>Symbol</label>
              <input class="input" id="ov-test-symbol" value="EURUSD" placeholder="EURUSD">
            </div>
            <div class="field">
              <label>Action</label>
              <select class="input" id="ov-test-action">
                <option value="buy">buy</option>
                <option value="sell">sell</option>
                <option value="close">close</option>
                <option value="close_all">close_all</option>
              </select>
            </div>
            <div class="field">
              <label>Lots</label>
              <input class="input" id="ov-test-lots" value="0.10" placeholder="0.10">
            </div>
          </div>
          <button class="btn primary" id="ov-test-send" onclick="sendTestWebhook()">${ICONS.check}Send Test Signal</button>
          <div id="ov-test-result"></div>
        </div>
      </div>
    </div>` : "";

  const setupBlock = !allDone ? `
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
    </div>` : "";

  content.innerHTML = `
    ${webhookBlock}
    <div class="card">
      <div class="card-title">System Status</div>
      <div class="card-desc">Current configuration state</div>
      <div class="grid grid-3">
        <div class="stat ${tg ? "ok" : "bad"} clickable" onclick="route('setup')">
          <div class="value">${tg ? "Connected" : "Not Set"}</div>
          <div class="label">Telegram Bot</div>
          ${tgHint}
        </div>
        <div class="stat ${cf ? "ok" : "warn"} clickable" onclick="route('setup')">
          <div class="value">${cf ? "Active" : "Not Set"}</div>
          <div class="label">Cloudflare Tunnel</div>
          ${cfHint}
        </div>
        <div class="stat ${init ? "ok" : "warn"}">
          <div class="value">${init ? "Yes" : "No"}</div>
          <div class="label">Initialized</div>
          ${initHint}
        </div>
      </div>
    </div>
    <div class="card" id="health-card">
      <div class="card-title">Server Health</div>
      <div class="card-desc">Live system metrics - refreshes every 10s</div>
      <div class="grid grid-4" id="health-grid">
        <div class="stat" id="tile-uptime"><div class="value skeleton line"></div><div class="label">Uptime</div></div>
        <div class="stat" id="tile-cpu"><div class="value skeleton line"></div><div class="label">CPU</div></div>
        <div class="stat" id="tile-mem"><div class="value skeleton line"></div><div class="label">Memory</div></div>
        <div class="stat" id="tile-ea"><div class="value skeleton line"></div><div class="label">EA Connections</div></div>
      </div>
    </div>
    ${setupBlock}
  `;
  startOverviewPoll();
  startHealthPolling();
  if (healthState.data || healthState.error) updateHealthCard();
}

function setupStepState(data) {
  const tg = data?.telegram_configured;
  const cf = data?.cloudflare_configured;
  if (!tg) return { step: 1, tg, cf };
  if (!cf) return { step: 2, tg, cf };
  return { step: 3, tg, cf };
}

async function renderSetup(content) {
  content.innerHTML = skeletonCard(1);
  const { data, error } = await useFetch(`${API}/setup-status`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load setup status</div><button class="btn outline sm mt" onclick="route('setup')">Retry</button></div>`;
    return;
  }
  const state = setupStepState(data);
  renderSetupStep(content, state.step, data);
}

function renderSetupStep(content, step, data) {
  const total = 3;
  const dots = Array.from({ length: total }, (_, i) =>
    `<div class="prog-dot ${i + 1 < step ? "done" : i + 1 === step ? "active" : ""}">${i + 1 < step ? ICONS.check : i + 1}</div>`
  ).join('<div class="prog-line"></div>');

  content.innerHTML = `
    <div class="card setup-prog-card">
      <div class="prog-header">
        <div class="prog-title">Step ${step} of ${total}</div>
        <div class="prog-dots">${dots}</div>
      </div>
    </div>
    <div id="step-body"></div>
  `;
  const body = document.getElementById("step-body");
  if (step === 1) renderStep1(body, data);
  else if (step === 2) renderStep2(body, data);
  else if (step === 3) renderStep3(body, data);
}

function renderStep1(body, data) {
  const tg = data?.telegram_configured;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">Telegram Bot</div>
      <div class="card-desc">Required for dashboard login and trade alerts</div>
      ${tg ? `
        <div class="row"><span class="k">Status</span><span class="v">${badge("ok", "Configured")}</span></div>
        <div class="row"><span class="k">Bot Token</span><span class="v">**** (set)</span></div>
        <button class="btn primary mt" onclick="advanceStep(2)">${ICONS.arrow}Continue to Step 2</button>
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
  `;
}

function renderStep2(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">Cloudflare Tunnel</div>
      <div class="card-desc">Provides public HTTPS URL for TradingView webhooks</div>
      ${cf ? `
        <div class="row"><span class="k">Status</span><span class="v">${badge("ok", "Connected")}</span></div>
        <div class="row"><span class="k">URL</span><span class="v">${data?.server_url || "https://..."}</span></div>
        <button class="btn primary mt" onclick="advanceStep(3)">${ICONS.arrow}Continue to Step 3</button>
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
        <div id="cf-result"></div>
      `}
    </div>
  `;
}

async function renderStep3(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">TradingView Webhook</div>
      <div class="card-desc">Copy this URL and paste it into TradingView</div>
      <div class="webhook-display">
        <code class="webhook-url" id="step3-webhook-url">${cf ? "Loading..." : "Complete Step 2 first"}</code>
        <button class="btn primary lg" id="copy-step3" onclick="copyWebhookStep3()" ${cf ? "" : "disabled"}>${ICONS.copy}Copy URL</button>
      </div>
      <div class="hint mt">In TradingView: Chart -> Alert -> Notifications -> Webhook URL</div>
    </div>
    <button class="btn outline" onclick="route('overview')">${ICONS.check}Back to Overview</button>
  `;
  if (!cf) return;
  try {
    const r = await fetch(`${API}/webhook-url`, { headers: { "Content-Type": "application/json" } });
    if (!r.ok) throw new Error(r.status);
    const info = await r.json();
    const urlEl = document.getElementById("step3-webhook-url");
    if (urlEl) urlEl.textContent = info.url || "";
  } catch (e) {
    const urlEl = document.getElementById("step3-webhook-url");
    if (urlEl) urlEl.textContent = data?.server_url || "";
  }
}

async function advanceStep(step) {
  const content = document.getElementById("content");
  content.innerHTML = skeletonCard(1);
  const { data } = await useFetch(`${API}/setup-status`);
  renderSetupStep(content, step, data);
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
  const original = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span>Saving...`;
  result.innerHTML = "";
  try {
    const r = await fetch(`${API}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates: { TELEGRAM_BOT_TOKEN: token, TELEGRAM_ADMIN_IDS: uid } }),
    });
    if (r.ok) {
      btn.innerHTML = `${ICONS.check}Saved`;
      btn.classList.add("btn-success");
      result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Send /login to your bot to test.</div>`;
      toast("Telegram configured", "ok");
      setTimeout(() => advanceStep(2), 2000);
    } else {
      throw new Error(r.status);
    }
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = original;
    btn.classList.remove("btn-success");
    result.innerHTML = `<div class="inline-error">${ICONS.x}Failed: ${e.message}. <a href="#" onclick="saveTelegram();return false;" class="retry-link">Retry</a></div>`;
  }
}

function copyWebhook() {
  const el = document.getElementById("copy-webhook");
  const url = document.querySelector(".webhook-card .webhook-url").textContent;
  const original = el.innerHTML;
  navigator.clipboard.writeText(url).then(() => {
    el.innerHTML = `${ICONS.check}Copied!`;
    el.classList.add("btn-success");
    toast("Webhook URL copied", "ok");
    setTimeout(() => {
      el.innerHTML = original;
      el.classList.remove("btn-success");
    }, 2000);
  });
}

function copyWebhookStep3() {
  const el = document.getElementById("copy-step3");
  const url = document.querySelector("#step-body .webhook-url").textContent;
  const original = el.innerHTML;
  navigator.clipboard.writeText(url).then(() => {
    el.innerHTML = `${ICONS.check}Copied!`;
    el.classList.add("btn-success");
    toast("Webhook URL copied", "ok");
    setTimeout(() => {
      el.innerHTML = original;
      el.classList.remove("btn-success");
    }, 2000);
  });
}

function toggleTestForm() {
  const form = document.getElementById("ov-test-form");
  if (!form) return;
  form.style.display = form.style.display === "none" ? "block" : "none";
}

async function sendTestWebhook() {
  const btn = document.getElementById("ov-test-send");
  const result = document.getElementById("ov-test-result");
  if (!btn || !result) return;
  const symbol = document.getElementById("ov-test-symbol").value.trim() || "EURUSD";
  const action = document.getElementById("ov-test-action").value;
  const lots = document.getElementById("ov-test-lots").value.trim() || "0.10";
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span>Sending...`;
  result.innerHTML = "";
  try {
    const r = await fetch(`${API}/test-webhook`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, action, lots }),
    });
    const data = await r.json();
    if (data.status === "sent") {
      const ok = data.response_code >= 200 && data.response_code < 300;
      result.innerHTML = `<div class="inline-${ok ? "ok" : "error"}">${ok ? ICONS.check : ICONS.x}HTTP ${data.response_code} - ${ok ? "Signal delivered" : "Webhook returned error"}</div><div class="hint mt">Response: ${data.response_body || ""}</div>`;
    } else {
      result.innerHTML = `<div class="inline-error">${ICONS.x}${data.message || "Test failed"}</div>`;
    }
  } catch (e) {
    result.innerHTML = `<div class="inline-error">${ICONS.x}Request failed: ${e.message}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = original;
}

async function renderSettings(content) {
  content.innerHTML = skeletonCard(1);
  const { data, error } = await useFetch(`${API}/config`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load settings</div><button class="btn outline sm mt" onclick="route('settings')">Retry</button></div>`;
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
window.copyWebhook = copyWebhook;
window.copyWebhookStep3 = copyWebhookStep3;
window.advanceStep = advanceStep;
window.toggleTestForm = toggleTestForm;
window.sendTestWebhook = sendTestWebhook;

(async function init() {
  await useFetch(`${API}/setup-status`);
  render();
  route("overview");
})();
