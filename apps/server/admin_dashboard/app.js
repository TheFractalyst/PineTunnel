const API = "/api/dashboard";
const POLL_INTERVAL = 10000;
const FETCH_TIMEOUT = 10000;
const RETRY_DELAY = 2000;
let pollTimers = [];
let currentRoute = "overview";
let lastSetupStatus = null;
let loginVisible = false;
let connectionLostVisible = false;

const CSRF_HEADER = "X-Admin-CSRF";

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function adminHeaders(extra = {}) {
  const h = Object.assign({}, extra);
  h[CSRF_HEADER] = "1";
  return h;
}

function jsonHeaders(withCsrf = false) {
  const h = { "Content-Type": "application/json" };
  if (withCsrf) h[CSRF_HEADER] = "1";
  return h;
}
let routeTimer = null;
let domCache = { content: null, actions: null, sidebar: null };
let overviewRendered = false;
let overviewSig = null;
let visibilityPolling = true;

const SVG_ATTRS = ' viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false"';
const ICONS = {
  overview: '<svg' + SVG_ATTRS + '><path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/></svg>',
  signals: '<svg' + SVG_ATTRS + '><path d="M2 12h4l3-9 6 18 3-9h4"/></svg>',
  settings: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  setup: '<svg' + SVG_ATTRS + '><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>',
  check: '<svg' + SVG_ATTRS + '><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/></svg>',
  x: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
  alert: '<svg' + SVG_ATTRS + ' role="img" aria-label="Alert"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  copy: '<svg' + SVG_ATTRS + '><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  external: '<svg' + SVG_ATTRS + '><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
  arrow: '<svg' + SVG_ATTRS + '><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
};

const cache = {};

async function http(path, opts = {}) {
  const attempt = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
    try {
      const r = await fetch(path, {
        ...opts,
        headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
        signal: controller.signal,
      });
      if (r.status === 401) {
        showLogin();
        const e = new Error("Unauthorized");
        e.status = 401;
        e.transient = false;
        throw e;
      }
      if (!r.ok) {
        const e = new Error(`HTTP ${r.status}`);
        e.status = r.status;
        e.transient = r.status >= 500;
        throw e;
      }
      return r;
    } catch (e) {
      if (e.name === "AbortError") {
        e.message = "Request timed out";
        e.transient = true;
        e.status = 0;
      } else if (e instanceof TypeError) {
        e.transient = true;
        e.status = 0;
      } else if (e.transient === undefined) {
        e.transient = false;
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  };
  try {
    return await attempt();
  } catch (e) {
    if (e.transient && !opts._retried) {
      await new Promise(r => setTimeout(r, RETRY_DELAY));
      return await http(path, { ...opts, _retried: true });
    }
    throw e;
  }
}

async function useFetch(path, opts = {}) {
  try {
    const r = await http(path, opts);
    const data = await r.json();
    cache[path] = { data, ts: Date.now() };
    hideConnectionLost();
    return { data, error: null, loading: false, stale: false };
  } catch (e) {
    if (cache[path]) {
      return { data: cache[path].data, error: e.message, loading: false, stale: true };
    }
    if (e.status !== 401) showConnectionLost(e.message);
    return { data: null, error: e.message, loading: false, stale: false };
  }
}

function showLogin() {
  if (loginVisible) return;
  loginVisible = true;
  clearPolls();
  const app = document.getElementById("app");
  if (!app) return;
  app.innerHTML = `
    <div class="welcome">
      <div class="logo">P</div>
      <h1>PineTunnel Login</h1>
      <p>Send /login to your Telegram bot to get a one-time code</p>
      <div class="login-form">
        <input class="input" id="login-code" placeholder="Login code" autocomplete="off">
        <input class="input" id="login-uid" type="number" placeholder="Telegram user ID">
        <button class="btn primary lg" id="login-submit" data-action="do-login">Login</button>
        <div id="login-error"></div>
      </div>
    </div>`;
  const btn = app.querySelector("[data-action='do-login']");
  if (btn) btn.addEventListener("click", e => { e.preventDefault(); doLogin(); });
}

async function doLogin() {
  const code = document.getElementById("login-code").value.trim();
  const uid = parseInt(document.getElementById("login-uid").value.trim(), 10);
  const err = document.getElementById("login-error");
  const btn = document.getElementById("login-submit");
  if (!code || !uid) {
    err.innerHTML = `<div class="inline-error">Code and user ID are required</div>`;
    return;
  }
  btn.disabled = true;
  btn.textContent = "Logging in...";
  err.innerHTML = "";
  try {
    const r = await http(`${API}/login`, { method: "POST", headers: jsonHeaders(true), body: JSON.stringify({ code, user_id: uid }) });
    if (r.ok) {
      loginVisible = false;
      toast("Logged in", "ok");
      render();
      route(currentRoute);
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Login";
    err.innerHTML = `<div class="inline-error">${escapeHtml(e.message)}</div>`;
  }
}

function showConnectionLost(msg) {
  if (connectionLostVisible) return;
  connectionLostVisible = true;
  let overlay = document.getElementById("conn-lost");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "conn-lost";
    overlay.className = "conn-overlay";
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = `<div class="conn-card"><div class="conn-icon">${ICONS.alert}</div><div class="conn-msg">Connection lost</div><div class="conn-sub">${escapeHtml(msg || "The server is unreachable. Retrying automatically.")}</div><button class="btn outline sm" data-action="retry-conn">Retry</button></div>`;
  const btn = overlay.querySelector("[data-action='retry-conn']");
  if (btn) btn.addEventListener("click", e => { e.preventDefault(); retryLastRoute(); });
}

function hideConnectionLost() {
  if (!connectionLostVisible) return;
  connectionLostVisible = false;
  const overlay = document.getElementById("conn-lost");
  if (overlay) overlay.remove();
}

function retryLastRoute() {
  hideConnectionLost();
  route(currentRoute);
}

function staleBanner() {
  return `<div class="stale-banner">${ICONS.alert}<span>Showing last known data - connection issue</span></div>`;
}

window.addEventListener("unhandledrejection", e => {
  if (e.reason && (e.reason instanceof TypeError || e.reason.name === "AbortError")) {
    showConnectionLost(e.reason.message || "Network error");
    e.preventDefault();
  }
});
window.addEventListener("error", e => {
  const msg = e.message || "Runtime error";
  if (/fetch|network|abort|timeout/i.test(msg)) showConnectionLost(msg);
});

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
    const hRes = await http("/api/system/health");
    const health = await hRes.json();
    let connections = null;
    try {
      const cRes = await http("/api/connections");
      connections = await cRes.json();
    } catch {}
    healthState = { data: { health, connections }, error: null, stale: false };
    hideConnectionLost();
  } catch (e) {
    healthState = { data: prev, error: e.message, stale: true };
    if (!prev && e.status !== 401) showConnectionLost(e.message);
  }
  if (healthActive) updateHealthCard();
}

function startHealthPolling() {
  if (!visibilityPolling) return;
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
      grid.innerHTML = `<div class="empty grid-span-all"><div class="msg">Health unavailable</div><div class="sub">${escapeHtml(error)}</div></div>`;
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
  t.setAttribute("role", "status");
  t.setAttribute("aria-live", "polite");
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
    <a class="skip-link" href="#content">Skip to main content</a>
    <div class="layout">
      <nav class="sidebar" aria-label="Main navigation">
        <div class="brand"><img src="/admin/assets/logo.svg" alt="PineTunnel" class="brand-logo"></div>
        ${nav.map(n => `<a class="nav-item" href="#${n.id}" data-route="${n.id}" role="button">${n.icon}<span>${n.label}</span></a>`).join("")}
        <div class="spacer"></div>
        <div class="footer"><span class="pulse-dot" aria-hidden="true"></span><span>System Online - v1.0</span></div>
      </nav>
      <div class="main-area">
        <header class="header" role="banner">
          <div class="title" id="page-title">Overview</div>
          <div class="actions" id="header-actions"></div>
        </header>
        <main class="content" id="content" tabindex="-1"></main>
      </div>
    </div>
    <nav class="mobile-nav" aria-label="Mobile navigation">
      ${nav.map(n => `<a class="tab" href="#${n.id}" data-route="${n.id}" role="button">${n.icon}<span>${n.label}</span></a>`).join("")}
    </nav>
  `;
  document.querySelectorAll("[data-route]").forEach(el => {
    el.addEventListener("click", e => {
      e.preventDefault();
      route(el.dataset.route);
    });
    el.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        route(el.dataset.route);
      }
    });
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    visibilityPolling = false;
    clearPolls();
  } else {
    visibilityPolling = true;
    if (currentRoute === "overview") {
      route("overview");
    }
  }
});

function route(id) {
  if (routeTimer) clearTimeout(routeTimer);
  routeTimer = setTimeout(() => {
    currentRoute = id;
    clearPolls();
    document.querySelectorAll("[data-route]").forEach(el => {
      const isActive = el.dataset.route === id;
      el.classList.toggle("active", isActive);
      if (isActive) el.setAttribute("aria-current", "page");
      else el.removeAttribute("aria-current");
    });
    const titles = { overview: "Overview", setup: "Setup Wizard", settings: "Settings" };
    document.getElementById("page-title").textContent = titles[id] || id;
    const content = domCache.content || document.getElementById("content");
    const actions = domCache.actions || document.getElementById("header-actions");
    actions.innerHTML = "";
    if (id !== "overview") overviewRendered = false;
    if (id === "overview") {
      if (overviewRendered && overviewSig === lastSetupStatus) {
        startOverviewPoll();
        startHealthPolling();
        if (healthState.data || healthState.error) updateHealthCard();
      } else {
        renderOverview(content, actions);
      }
    } else if (id === "setup") renderSetup(content);
    else if (id === "settings") renderSettings(content);
    if (content) content.focus({ preventScroll: true });
  }, 100);
}

function skeletonCard(cols = 3) {
  return `<div class="card"><div class="grid grid-${cols}">${Array(cols).fill('<div><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}</div></div>`;
}

function badge(state, text, pulse = false) {
  const cls = state === "ok" ? "ok" : state === "bad" ? "bad" : state === "warn" ? "warn" : "info";
  return `<span class="badge ${cls} ${pulse ? "pulse" : ""}"><span class="dot"></span>${text}</span>`;
}

function startOverviewPoll() {
  if (!visibilityPolling) return;
  const t = setInterval(async () => {
    if (currentRoute !== "overview" || !visibilityPolling) return;
    const { data } = await useFetch(`${API}/setup-status`);
    if (!data) return;
    const sig = JSON.stringify(data);
    if (sig !== lastSetupStatus) {
      lastSetupStatus = sig;
      const content = domCache.content || document.getElementById("content");
      const actions = domCache.actions || document.getElementById("header-actions");
      if (content && actions) renderOverview(content, actions);
    }
  }, POLL_INTERVAL);
  pollTimers.push(t);
}

async function renderOverview(content, actions) {
  content.innerHTML = skeletonCard(3);
  const { data, error, stale } = await useFetch(`${API}/setup-status`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load status</div><div class="sub">${escapeHtml(error)}</div><button class="btn outline sm mt" data-action="retry-overview">Retry</button></div>`;
    bindRetry(content, "retry-overview", () => route("overview"));
    return;
  }
  const staleBannerHtml = stale ? staleBanner() : "";
  const tg = data.telegram_configured;
  const cf = data.cloudflare_configured;
  const init = data.initialized;
  const allDone = tg && cf && init;
  lastSetupStatus = JSON.stringify(data);
  const small = window.innerWidth < 375;
  actions.innerHTML = badge(allDone ? "ok" : "info", allDone ? (small ? "OK" : "All Ready") : (small ? "Hi" : "Welcome"), !allDone);

  const tgHint = tg ? "" : '<div class="stat-hint">Click Setup to configure</div>';
  const cfHint = cf ? "" : '<div class="stat-hint">Click Setup to configure</div>';
  const initHint = init ? "" : '<div class="stat-hint">Complete steps 1 and 2</div>';

  const webhookBlock = allDone ? `
    <div class="card webhook-card">
      <div class="card-title">Your TradingView Webhook URL</div>
      <div class="card-desc">Paste this into TradingView: Alert -> Notifications -> Webhook URL</div>
      <div class="webhook-display">
        <code class="webhook-url">${escapeHtml(data.server_url || "http://127.0.0.1:8000")}</code>
        <button class="btn primary lg" id="copy-webhook" data-action="copy-webhook">${ICONS.copy}Copy URL</button>
      </div>
      <div class="hint mt">Only ports 80 and 443 are accepted by TradingView. Use the Cloudflare tunnel URL.</div>
      <div class="webhook-test-section mt">
        <button class="btn outline" id="ov-test-toggle" data-action="toggle-test">${ICONS.external}Test Webhook</button>
        <div id="ov-test-form" class="webhook-test-form hidden">
          <div class="grid grid-3">
            <div class="field">
              <label for="ov-test-symbol">Symbol</label>
              <input class="input" id="ov-test-symbol" value="EURUSD" placeholder="EURUSD">
            </div>
            <div class="field">
              <label for="ov-test-action">Action</label>
              <select class="input" id="ov-test-action">
                <option value="buy">buy</option>
                <option value="sell">sell</option>
                <option value="close">close</option>
                <option value="close_all">close_all</option>
              </select>
            </div>
            <div class="field">
              <label for="ov-test-lots">Lots</label>
              <input class="input" id="ov-test-lots" value="0.10" placeholder="0.10">
            </div>
          </div>
          <button class="btn primary" id="ov-test-send" data-action="send-test">${ICONS.check}Send Test Signal</button>
          <div id="ov-test-result" aria-live="polite"></div>
        </div>
      </div>
    </div>` : "";

  const setupBlock = !allDone ? `
    <div class="card">
      <div class="card-title">Get Started</div>
      <div class="card-desc">3 quick steps to start receiving TradingView webhooks</div>
      <div class="steps">
        <div class="step ${tg ? "done" : ""}">
          <div class="num">${tg ? ICONS.check : "1"}</div>
          <div class="body">
            <div class="t">Configure Telegram Bot</div>
            <div class="d">${tg ? "Done" : "Required for login and trade alerts"}</div>
            <div class="action"><button class="btn outline sm" data-action="goto-setup">Go to Setup</button></div>
          </div>
        </div>
        <div class="step ${cf ? "done" : ""}">
          <div class="num">${cf ? ICONS.check : "2"}</div>
          <div class="body">
            <div class="t">Connect Cloudflare Tunnel</div>
            <div class="d">${cf ? "Done" : "Provides public HTTPS URL for TradingView webhooks"}</div>
            <div class="action"><button class="btn outline sm" data-action="goto-setup">Go to Setup</button></div>
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
    ${staleBannerHtml}
    ${webhookBlock}
    <div class="card">
      <div class="card-title">System Status</div>
      <div class="card-desc">Current configuration state</div>
      <div class="grid grid-3">
        <div class="stat ${tg ? "ok" : "info"} clickable" data-action="goto-setup" tabindex="0" role="button" aria-label="Telegram Bot status - go to Setup">
          <div class="value">${tg ? "Connected" : "Pending"}</div>
          <div class="label">Telegram Bot</div>
          ${tgHint}
        </div>
        <div class="stat ${cf ? "ok" : "info"} clickable" data-action="goto-setup" tabindex="0" role="button" aria-label="Cloudflare Tunnel status - go to Setup">
          <div class="value">${cf ? "Active" : "Pending"}</div>
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
  bindOverviewActions(content);
  startOverviewPoll();
  startHealthPolling();
  if (healthState.data || healthState.error) updateHealthCard();
}

function bindRetry(scope, action, fn) {
  const el = scope.querySelector(`[data-action="${action}"]`);
  if (el) el.addEventListener("click", e => { e.preventDefault(); fn(); });
}

function bindOverviewActions(scope) {
  scope.querySelectorAll("[data-action='goto-setup']").forEach(el => {
    const handler = e => { e.preventDefault(); route("setup"); };
    el.addEventListener("click", handler);
    if (el.tagName === "DIV") {
      el.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); route("setup"); }
      });
    }
  });
  const copyBtn = scope.querySelector("[data-action='copy-webhook']");
  if (copyBtn) copyBtn.addEventListener("click", e => { e.preventDefault(); copyWebhook(); });
  const toggleBtn = scope.querySelector("[data-action='toggle-test']");
  if (toggleBtn) toggleBtn.addEventListener("click", e => { e.preventDefault(); toggleTestForm(); });
  const sendBtn = scope.querySelector("[data-action='send-test']");
  if (sendBtn) sendBtn.addEventListener("click", e => { e.preventDefault(); sendTestWebhook(); });
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
  const { data, error, stale } = await useFetch(`${API}/setup-status`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load setup status</div><button class="btn outline sm mt" data-action="retry-setup">Retry</button></div>`;
    bindRetry(content, "retry-setup", () => route("setup"));
    return;
  }
  const staleBannerHtml = stale ? staleBanner() : "";
  const state = setupStepState(data);
  renderSetupStep(content, state.step, data, staleBannerHtml);
}

function renderSetupStep(content, step, data, staleBannerHtml = "") {
  const total = 3;
  const dots = Array.from({ length: total }, (_, i) =>
    `<div class="prog-dot ${i + 1 < step ? "done" : i + 1 === step ? "active" : ""}">${i + 1 < step ? ICONS.check : i + 1}</div>`
  ).join('<div class="prog-line"></div>');

  content.innerHTML = `
    ${staleBannerHtml}
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
        <button class="btn primary mt" data-action="advance-2">${ICONS.arrow}Continue to Step 2</button>
      ` : `
        <div class="field">
          <label>1. Create a bot via @BotFather on Telegram</label>
          <div class="hint">Open Telegram, message @BotFather, send /newbot, follow prompts</div>
        </div>
        <div class="field">
          <label for="tg-token">2. Paste bot token</label>
          <input class="input" id="tg-token" type="password" placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" aria-describedby="tg-token-hint">
        </div>
        <div class="field">
          <label for="tg-uid">3. Get your Telegram user ID</label>
          <div class="hint">Message @userinfobot on Telegram, it replies with your numeric ID</div>
          <input class="input" id="tg-uid" type="number" placeholder="123456789">
        </div>
        <button class="btn primary" id="save-tg" data-action="save-telegram">${ICONS.check}Save and Verify</button>
        <div id="tg-result" aria-live="polite"></div>
      `}
    </div>
  `;
  const adv = body.querySelector("[data-action='advance-2']");
  if (adv) adv.addEventListener("click", e => { e.preventDefault(); advanceStep(2); });
  const saveBtn = body.querySelector("[data-action='save-telegram']");
  if (saveBtn) saveBtn.addEventListener("click", e => { e.preventDefault(); saveTelegram(); });
}

function renderStep2(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">Cloudflare Tunnel</div>
      <div class="card-desc">Provides public HTTPS URL for TradingView webhooks</div>
      ${cf ? `
        <div class="row"><span class="k">Status</span><span class="v">${badge("ok", "Connected")}</span></div>
        <div class="row"><span class="k">URL</span><span class="v">${escapeHtml(data?.server_url || "https://...")}</span></div>
        <button class="btn primary mt" data-action="advance-3">${ICONS.arrow}Continue to Step 3</button>
      ` : `
        <div class="field">
          <label>Option A: I have a Cloudflare domain (recommended)</label>
          <div class="hint">Dashboard will create the tunnel for you. Coming in Phase 2.</div>
        </div>
        <div class="field">
          <label for="cf-token">Option B: I have a tunnel token already</label>
          <input class="input" id="cf-token" placeholder="eyJ..." disabled>
          <label for="cf-url" class="sr-only">Tunnel URL</label>
          <input class="input mt" id="cf-url" placeholder="https://pinetunnel.example.com" disabled>
        </div>
        <button class="btn outline" disabled>${ICONS.external}Connect (Phase 2)</button>
        <div id="cf-result" aria-live="polite"></div>
      `}
    </div>
  `;
  const adv = body.querySelector("[data-action='advance-3']");
  if (adv) adv.addEventListener("click", e => { e.preventDefault(); advanceStep(3); });
}

async function renderStep3(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">TradingView Webhook</div>
      <div class="card-desc">Copy this URL and paste it into TradingView</div>
      <div class="webhook-display">
        <code class="webhook-url" id="step3-webhook-url">${cf ? "Loading..." : "Complete Step 2 first"}</code>
        <button class="btn primary lg" id="copy-step3" data-action="copy-step3" ${cf ? "" : "disabled"}>${ICONS.copy}Copy URL</button>
      </div>
      <div class="hint mt">In TradingView: Chart -> Alert -> Notifications -> Webhook URL</div>
    </div>
    <button class="btn outline" data-action="goto-overview">${ICONS.check}Back to Overview</button>
  `;
  const copyBtn = body.querySelector("[data-action='copy-step3']");
  if (copyBtn) copyBtn.addEventListener("click", e => { e.preventDefault(); copyWebhookStep3(); });
  const backBtn = body.querySelector("[data-action='goto-overview']");
  if (backBtn) backBtn.addEventListener("click", e => { e.preventDefault(); route("overview"); });
  if (!cf) return;
  try {
    const r = await http(`${API}/webhook-url`);
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
    const r = await http(`${API}/config`, {
      method: "PUT",
      headers: jsonHeaders(true),
      body: JSON.stringify({ updates: { TELEGRAM_BOT_TOKEN: token, TELEGRAM_ADMIN_IDS: uid } }),
    });
    btn.innerHTML = `${ICONS.check}Saved`;
    btn.classList.add("btn-success");
    result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Send /login to your bot to test.</div>`;
    toast("Telegram configured", "ok");
    setTimeout(() => advanceStep(2), 2000);
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = original;
    btn.classList.remove("btn-success");
    result.innerHTML = `<div class="inline-error">${ICONS.x}Failed: ${escapeHtml(e.message)}. <a href="#" data-action="retry-save" class="retry-link">Retry</a></div>`;
    const retry = result.querySelector("[data-action='retry-save']");
    if (retry) retry.addEventListener("click", ev => { ev.preventDefault(); saveTelegram(); });
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
  form.classList.toggle("hidden");
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
    const r = await http(`${API}/test-webhook`, {
      method: "POST",
      headers: jsonHeaders(true),
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
    result.innerHTML = `<div class="inline-error">${ICONS.x}Request failed: ${escapeHtml(e.message)}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = original;
}

async function renderSettings(content) {
  content.innerHTML = skeletonCard(1);
  const { data, error, stale } = await useFetch(`${API}/config`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load settings</div><button class="btn outline sm mt" data-action="retry-settings">Retry</button></div>`;
    bindRetry(content, "retry-settings", () => route("settings"));
    return;
  }
  const entries = Object.entries(data).filter(([k]) => !k.startsWith("#") && k);
  content.innerHTML = `
    ${stale ? staleBanner() : ""}
    <div class="card">
      <div class="card-title">Configuration</div>
      <div class="card-desc">Environment variables (secrets are redacted)</div>
      ${entries.map(([k, v]) => `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span></div>`).join("")}
    </div>
  `;
}

window.route = route;

(async function init() {
  await useFetch(`${API}/setup-status`);
  render();
  route("overview");
})();

let resizeTimer = null;
let lastWidth = window.innerWidth;
window.addEventListener("resize", () => {
  const w = window.innerWidth;
  if ((w < 375) !== (lastWidth < 375)) {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      overviewRendered = false;
      route(currentRoute);
    }, 200);
  }
  lastWidth = w;
});
