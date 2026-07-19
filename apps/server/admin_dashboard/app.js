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

const LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="190 48 475 48" preserveAspectRatio="xMidYMid meet" class="brand-logo" aria-label="PineTunnel"><g fill="currentColor"><g transform="translate(196,91.6)"><path d="M32.25-37.94c1.38 0 2.66.34 3.81 1.03 1.16.68 2.07 1.6 2.75 2.75.68 1.15 1.02 2.42 1.02 3.81v7.58c0 1.39-.34 2.66-1.02 3.81-.68 1.15-1.6 2.07-2.75 2.75-1.15.68-2.42 1.02-3.81 1.02H9.48v15.17H1.89v-37.94zM9.48-22.77h22.77v-7.58H9.48z"/></g><g transform="translate(243,91.6)"><path d="M39.83 0H1.89v-7.59h15.19v-22.75H1.89v-7.59h37.94v7.59H24.66v22.75h15.17z"/></g><g transform="translate(290,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(338,91.6)"><path d="M39.83-37.94v7.59H9.48v7.58h30.35v7.59H9.48v7.58h30.35V0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-22.75c0-1.39.34-2.66 1.02-3.81.68-1.16 1.6-2.07 2.75-2.75 1.16-.69 2.44-1.03 3.83-1.03z"/></g><g transform="translate(385,91.6)"><path d="M39.83-37.94v7.59H24.66V0h-7.58v-30.35H1.89v-7.59z"/></g><g transform="translate(432,91.6)"><path d="M32.25 0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-30.35h7.59v30.35H32.25v-30.35h7.58v30.35c0 1.39-.34 2.66-1.02 3.81-.68 1.16-1.6 2.07-2.75 2.75-1.15.69-2.43 1.03-3.81 1.03z"/></g><g transform="translate(479,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(526,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(574,91.6)"><path d="M39.83-37.94v7.59H9.48v7.58h30.35v7.59H9.48v7.58h30.35V0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-22.75c0-1.39.34-2.66 1.02-3.81.68-1.16 1.6-2.07 2.75-2.75 1.16-.69 2.44-1.03 3.83-1.03z"/></g><g transform="translate(621,91.6)"><path d="M39.83 0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-30.35h7.59v30.35h30.35z"/></g></g></svg>';

const SVG_ATTRS = ' viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false"';
const ICONS = {
  overview: '<svg' + SVG_ATTRS + '><path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/></svg>',
  signals: '<svg' + SVG_ATTRS + '><path d="M2 12h4l3-9 6 18 3-9h4"/></svg>',
  feed: '<svg' + SVG_ATTRS + '><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="14" y2="18"/></svg>',
  map: '<svg' + SVG_ATTRS + '><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/></svg>',
  analytics: '<svg' + SVG_ATTRS + '><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg>',
  pipeline: '<svg' + SVG_ATTRS + '><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/><line x1="7" y1="12" x2="10" y2="12"/><line x1="14" y1="12" x2="17" y2="12"/></svg>',
  settings: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  setup: '<svg' + SVG_ATTRS + '><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>',
  check: '<svg' + SVG_ATTRS + '><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/></svg>',
  x: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
  alert: '<svg' + SVG_ATTRS + ' role="img" aria-label="Alert"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  copy: '<svg' + SVG_ATTRS + '><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  external: '<svg' + SVG_ATTRS + '><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>',
  arrow: '<svg' + SVG_ATTRS + '><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>',
  pause: '<svg' + SVG_ATTRS + '><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>',
  play: '<svg' + SVG_ATTRS + '><polygon points="5 3 19 12 5 21 5 3"/></svg>',
  refresh: '<svg' + SVG_ATTRS + '><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  chevron: '<svg' + SVG_ATTRS + '><polyline points="6 9 12 15 18 9"/></svg>',
  health: '<svg' + SVG_ATTRS + '><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  webhook: '<svg' + SVG_ATTRS + '><path d="M18 16.16v-1.6a2 2 0 0 0-1.1-1.8L13 11V7a1.5 1.5 0 0 0-3 0v9l-2.5-1.5a1.5 1.5 0 0 0-1.6 2.5L9 19"/></svg>',
  risk: '<svg' + SVG_ATTRS + '><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  errors: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
  database: '<svg' + SVG_ATTRS + '><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  metrics: '<svg' + SVG_ATTRS + '><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
  diag: '<svg' + SVG_ATTRS + '><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
  bot: '<svg' + SVG_ATTRS + '><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>',
  license: '<svg' + SVG_ATTRS + '><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
  security: '<svg' + SVG_ATTRS + '><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  audit: '<svg' + SVG_ATTRS + '><path d="M3 3v18h18"/><path d="M7 14l4-4 4 4 5-5"/><line x1="16" y1="9" x2="20" y2="9"/></svg>',
  trash: '<svg' + SVG_ATTRS + '><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  ban: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  power: '<svg' + SVG_ATTRS + '><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>',
  edit: '<svg' + SVG_ATTRS + '><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  plus: '<svg' + SVG_ATTRS + '><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
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
        if (path.startsWith("/api/dashboard/")) {
          showLogin();
        }
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

function loadColorHex(pct, diskMode = false) {
  if (pct == null || isNaN(pct)) return "#82828b";
  if (diskMode) {
    return pct < 70 ? "#22c55e" : pct <= 90 ? "#f59e0b" : "#ef4444";
  }
  return pct < 50 ? "#22c55e" : pct <= 80 ? "#f59e0b" : "#ef4444";
}

function svgGauge(value, label, opts = {}) {
  const size = opts.size || 120;
  const stroke = opts.stroke || 8;
  const r = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = 2 * Math.PI * r;
  const pct = (value != null && !isNaN(value)) ? Math.max(0, Math.min(100, value)) : null;
  const diskMode = !!opts.diskMode;
  const color = loadColorHex(pct, diskMode);
  const display = (pct != null) ? `${pct.toFixed(0)}%` : "--";
  const dash = (pct != null) ? (pct / 100) * circumference : 0;
  return `<div class="gauge-wrap">
    <svg class="gauge" viewBox="0 0 ${size} ${size}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${label}: ${display}">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="${stroke}"/>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
        stroke-dasharray="${dash} ${circumference}" stroke-linecap="round"
        transform="rotate(-90 ${cx} ${cy})" class="gauge-arc" style="transition: stroke-dasharray 0.6s ease, stroke 0.3s ease"/>
      <text x="${cx}" y="${cy - 2}" text-anchor="middle" class="gauge-value" fill="${color}">${display}</text>
      <text x="${cx}" y="${cy + 16}" text-anchor="middle" class="gauge-label" fill="#9a9aa3">${label}</text>
    </svg>
  </div>`;
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
  let diskPct = null;
  if (h.system && h.system.disk_percent != null) diskPct = h.system.disk_percent;
  else if (h.disk && h.disk.used_percent != null) diskPct = h.disk.used_percent;
  let eaCount = 0;
  if (c) {
    const httpCount = c.http_polling_connections || 0;
    const ws = (c.websocket && c.websocket.websocket_connections) || 0;
    eaCount = httpCount + ws;
  } else if (h.connections) {
    eaCount = h.connections.total_clients || 0;
  }
  const cpuCell = document.getElementById("gauge-cpu");
  const memCell = document.getElementById("gauge-mem");
  const diskCell = document.getElementById("gauge-disk");
  if (cpuCell) cpuCell.innerHTML = svgGauge(cpu, "CPU");
  if (memCell) memCell.innerHTML = svgGauge(mem, "RAM");
  if (diskCell) diskCell.innerHTML = svgGauge(diskPct, "Disk", { diskMode: true });
  setTile("tile-uptime", formatUptime(uptimeSec), "ok");
  setTile("tile-ea", String(eaCount), eaColor(eaCount));
  const sig = h.signals_today != null ? h.signals_today : (data.signalsToday != null ? data.signalsToday : null);
  const fill = h.fill_rate != null ? h.fill_rate : (data.fillRate != null ? data.fillRate : null);
  setTile("tile-signals", sig != null ? String(sig) : "--", sig != null && sig > 0 ? "ok" : "info");
  setTile("tile-fill", fill != null ? `${fill.toFixed(1)}%` : "--", fill != null ? (fill >= 80 ? "ok" : fill >= 50 ? "warn" : "bad") : "info");
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
  const monitorNav = [
    { id: "overview", label: "Overview", icon: ICONS.overview },
    { id: "signals", label: "Live Signals", icon: ICONS.signals },
    { id: "ea-map", label: "EA Connections", icon: ICONS.map },
    { id: "analytics", label: "Trade Analytics", icon: ICONS.analytics },
    { id: "pipeline", label: "Pipeline", icon: ICONS.pipeline },
  ];
  const manageNav = [
    { id: "setup", label: "Setup Wizard", icon: ICONS.setup },
    { id: "licenses", label: "License Manager", icon: ICONS.license },
    { id: "security", label: "Security Center", icon: ICONS.security },
    { id: "audit", label: "Audit Log", icon: ICONS.audit },
    { id: "settings", label: "Settings", icon: ICONS.settings },
  ];
  const systemNav = [
    { id: "sys-health", label: "System Health", icon: ICONS.health },
    { id: "sys-webhooks", label: "Webhook Logs", icon: ICONS.webhook },
    { id: "sys-risk", label: "Risk Monitor", icon: ICONS.risk },
    { id: "sys-errors", label: "Error Logs", icon: ICONS.errors },
    { id: "sys-database", label: "Database", icon: ICONS.database },
    { id: "sys-metrics", label: "Metrics", icon: ICONS.metrics },
    { id: "sys-diag", label: "Diagnostics", icon: ICONS.diag },
    { id: "sys-bot", label: "Bot Status", icon: ICONS.bot },
  ];
  const navHtml = (items) => items.map(n => `<a class="nav-item" href="#${n.id}" data-route="${n.id}" role="button" tabindex="0">${n.icon}<span>${n.label}</span></a>`).join("");
  const allNav = monitorNav.concat(manageNav, systemNav);
  const groups = [
    { id: "monitor", label: "Monitor", items: monitorNav },
    { id: "manage", label: "Manage", items: manageNav },
    { id: "system", label: "System", items: systemNav },
  ];
  const groupState = (gid) => {
    try { return sessionStorage.getItem("nav-group-" + gid) !== "collapsed"; }
    catch { return true; }
  };
  const groupHtml = groups.map(g => {
    const expanded = groupState(g.id);
    return `<div class="nav-group${expanded ? "" : " collapsed"}" data-group="${g.id}">
      <button class="nav-group-header" data-toggle="${g.id}" aria-expanded="${expanded}" aria-controls="nav-items-${g.id}" type="button">
        <span class="nav-group-label">${g.label}</span>
        <svg class="nav-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
      </button>
      <div class="nav-group-items" id="nav-items-${g.id}">${navHtml(g.items)}</div>
    </div>`;
  }).join("");
  const mobilePrimary = ["overview", "signals", "ea-map", "setup", "settings"];
  const mobilePrimaryItems = mobilePrimary.map(id => {
    const n = allNav.find(x => x.id === id);
    return n ? `<a class="tab" href="#${n.id}" data-route="${n.id}" role="button" tabindex="0">${n.icon}<span>${n.label.split(" ")[0]}</span></a>` : "";
  }).join("");
  const mobileSheetItems = groups.map(g => {
    const items = g.items.map(n => `<a class="mobile-sheet-item" href="#${n.id}" data-route="${n.id}" role="button" tabindex="0">${n.icon}<span>${n.label}</span></a>`).join("");
    return `<div class="mobile-sheet-group"><div class="mobile-sheet-label">${g.label}</div>${items}</div>`;
  }).join("");
  app.innerHTML = `
    <a class="skip-link" href="#content">Skip to main content</a>
    <div class="layout">
      <nav class="sidebar" aria-label="Main navigation">
        <div class="brand">${LOGO_SVG}</div>
        <div class="nav-scroll">${groupHtml}</div>
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
      ${mobilePrimaryItems}
      <button class="tab mobile-more" data-action="mobile-more" type="button" aria-haspopup="dialog" tabindex="0">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>
        <span>More</span>
      </button>
    </nav>
    <div class="mobile-sheet hidden" id="mobile-sheet" role="dialog" aria-modal="true" aria-label="All panels">
      <div class="mobile-sheet-backdrop" data-action="close-sheet"></div>
      <div class="mobile-sheet-card">
        <div class="mobile-sheet-head">
          <span class="mobile-sheet-title">All Panels</span>
          <button class="btn ghost sm" data-action="close-sheet" type="button" aria-label="Close">${ICONS.x}</button>
        </div>
        <div class="mobile-sheet-body">${mobileSheetItems}</div>
      </div>
    </div>
  `;
  document.querySelectorAll("[data-route]").forEach(el => {
    el.addEventListener("click", e => { e.preventDefault(); route(el.dataset.route); closeMobileSheet(); });
    el.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); route(el.dataset.route); closeMobileSheet(); }
    });
  });
  document.querySelectorAll("[data-toggle]").forEach(el => {
    el.addEventListener("click", e => {
      e.preventDefault();
      const gid = el.dataset.toggle;
      const group = document.querySelector(`.nav-group[data-group="${gid}"]`);
      if (!group) return;
      const collapsed = group.classList.toggle("collapsed");
      el.setAttribute("aria-expanded", String(!collapsed));
      try { sessionStorage.setItem("nav-group-" + gid, collapsed ? "collapsed" : "expanded"); } catch {}
    });
  });
  const moreBtn = document.querySelector("[data-action='mobile-more']");
  if (moreBtn) moreBtn.addEventListener("click", e => { e.preventDefault(); openMobileSheet(); });
  document.querySelectorAll("[data-action='close-sheet']").forEach(el => {
    el.addEventListener("click", e => { e.preventDefault(); closeMobileSheet(); });
  });
}

function openMobileSheet() {
  const sheet = document.getElementById("mobile-sheet");
  if (sheet) sheet.classList.remove("hidden");
}

function closeMobileSheet() {
  const sheet = document.getElementById("mobile-sheet");
  if (sheet) sheet.classList.add("hidden");
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    visibilityPolling = false;
    clearPolls();
  } else {
    visibilityPolling = true;
    route(currentRoute);
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
    const titles = {
      overview: "Overview",
      setup: "Setup Wizard",
      settings: "Settings",
      signals: "Live Signal Feed",
      "ea-map": "EA Connections Map",
      analytics: "Trade Analytics",
      pipeline: "Signal Pipeline Monitor",
      "sys-health": "System Health",
      "sys-webhooks": "Webhook Logs",
      "sys-risk": "Risk Monitor",
      "sys-errors": "Error Log Viewer",
      "sys-database": "Database Manager",
      "sys-metrics": "Performance Metrics",
      "sys-diag": "Diagnostics",
      "sys-bot": "Telegram Bot Status",
      licenses: "License Manager",
      security: "Security Center",
      audit: "Audit Log Timeline",
    };
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
    else if (id === "signals") renderSignalFeed(content, actions);
    else if (id === "ea-map") renderEaMap(content, actions);
    else if (id === "analytics") renderTradeAnalytics(content, actions);
    else if (id === "pipeline") renderPipelineMonitor(content, actions);
    else if (id === "sys-health") renderSystemHealth(content);
    else if (id === "sys-webhooks") renderWebhookLogs(content);
    else if (id === "sys-risk") renderRiskMonitor(content);
    else if (id === "sys-errors") renderErrorLogs(content);
    else if (id === "sys-database") renderDatabaseManager(content);
    else if (id === "sys-metrics") renderMetrics(content);
    else if (id === "sys-diag") renderDiagnostics(content);
    else if (id === "sys-bot") renderBotStatus(content);
    else if (id === "licenses") renderLicenses(content, actions);
    else if (id === "security") renderSecurity(content, actions);
    else if (id === "audit") renderAuditTimeline(content, actions);
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
        <button class="btn primary full-sm" id="copy-webhook" data-action="copy-webhook">${ICONS.copy}Copy URL</button>
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
      <div class="gauge-row" id="health-grid">
        <div class="gauge-cell" id="gauge-cpu">${svgGauge(null, "CPU")}</div>
        <div class="gauge-cell" id="gauge-mem">${svgGauge(null, "RAM")}</div>
        <div class="gauge-cell" id="gauge-disk">${svgGauge(null, "Disk", { diskMode: true })}</div>
        <div class="stat-pair">
          <div class="stat" id="tile-uptime"><div class="value skeleton line"></div><div class="label">Uptime</div></div>
          <div class="stat" id="tile-ea"><div class="value skeleton line"></div><div class="label">EA Connections</div></div>
          <div class="stat" id="tile-signals"><div class="value skeleton line"></div><div class="label">Signals Today</div></div>
          <div class="stat" id="tile-fill"><div class="value skeleton line"></div><div class="label">Fill Rate</div></div>
        </div>
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
        <button class="btn primary mt full-sm" data-action="advance-2">${ICONS.arrow}Continue to Step 2</button>
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
        <button class="btn primary full-sm" id="save-tg" data-action="save-telegram">${ICONS.check}Save and Verify</button>
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
        <button class="btn primary mt full-sm" data-action="advance-3">${ICONS.arrow}Continue to Step 3</button>
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
        <button class="btn primary full-sm" id="copy-step3" data-action="copy-step3" ${cf ? "" : "disabled"}>${ICONS.copy}Copy URL</button>
      </div>
      <div class="hint mt">In TradingView: Chart -> Alert -> Notifications -> Webhook URL</div>
    </div>
    <button class="btn outline full-sm" data-action="goto-overview">${ICONS.check}Back to Overview</button>
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
    if (r.needs_restart) {
      result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Restart the server for the bot to pick up the new token.</div>`;
      toast("Telegram saved - restart required", "ok");
    } else {
      result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Send /login to your bot to test.</div>`;
      toast("Telegram configured", "ok");
    }
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

function maskKey(k) {
  if (!k) return "--";
  const s = String(k);
  if (s.length <= 10) return s.slice(0, 4) + "...";
  return s.slice(0, 8) + "...";
}

function relativeTime(iso) {
  if (!iso) return "--";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "--";
  const diff = (Date.now() - t) / 1000;
  if (diff < 0) return "now";
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function statusClassFor(status) {
  if (!status) return "";
  const s = String(status).toLowerCase();
  if (s === "success" || s === "executed" || s === "ok") return "ok";
  if (s === "pending" || s === "queued" || s === "delivered") return "warn";
  if (s === "failed" || s === "error") return "bad";
  if (s === "duplicate" || s === "rejected" || s === "skipped") return "muted";
  return "info";
}

let signalFeedState = {
  rows: [],
  paused: false,
  filterLicense: "",
  filterSymbol: "",
  filterStatus: "",
  seenIds: new Set(),
};

function renderSignalFeed(content, actions) {
  signalFeedState = {
    rows: [],
    paused: false,
    filterLicense: "",
    filterSymbol: "",
    filterStatus: "",
    seenIds: new Set(),
  };
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Live Signal Feed</div>
      <div class="card-desc">Real-time webhook signals - polling every 5s</div>
      <div class="feed-toolbar">
        <div class="filter-bar">
          <select class="input filter-sel" id="feed-filter-license" aria-label="Filter by license"><option value="">All licenses</option></select>
          <input class="input filter-txt" id="feed-filter-symbol" placeholder="Symbol filter" aria-label="Filter by symbol">
          <select class="input filter-sel" id="feed-filter-status" aria-label="Filter by status">
            <option value="">All status</option>
            <option value="success">Executed</option>
            <option value="pending">Pending</option>
            <option value="failed">Failed</option>
            <option value="duplicate">Duplicate</option>
          </select>
        </div>
        <button class="btn outline sm" id="feed-pause-btn" data-action="feed-pause">${ICONS.pause}Pause</button>
      </div>
      <div class="feed-scroll" id="feed-scroll">
        <table class="feed-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>License</th>
              <th>Action</th>
              <th>Symbol</th>
              <th>Lots</th>
              <th>Status</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody id="feed-body">
            <tr><td colspan="7" class="feed-empty">Loading...</td></tr>
          </tbody>
        </table>
      </div>
      <div class="feed-footer">
        <span id="feed-count">0 signals</span>
        <span class="feed-hint">Hover to pause auto-scroll</span>
      </div>
    </div>
  `;
  const licenseSel = content.querySelector("#feed-filter-license");
  const symbolInput = content.querySelector("#feed-filter-symbol");
  const statusSel = content.querySelector("#feed-filter-status");
  const pauseBtn = content.querySelector("[data-action='feed-pause']");
  const scrollEl = content.querySelector("#feed-scroll");
  licenseSel.addEventListener("change", () => { signalFeedState.filterLicense = licenseSel.value; renderFeedRows(); });
  symbolInput.addEventListener("input", () => { signalFeedState.filterSymbol = symbolInput.value.trim().toUpperCase(); renderFeedRows(); });
  statusSel.addEventListener("change", () => { signalFeedState.filterStatus = statusSel.value; renderFeedRows(); });
  pauseBtn.addEventListener("click", e => {
    e.preventDefault();
    signalFeedState.paused = !signalFeedState.paused;
    pauseBtn.innerHTML = signalFeedState.paused ? `${ICONS.play}Resume` : `${ICONS.pause}Pause`;
  });
  scrollEl.addEventListener("mouseenter", () => { signalFeedState.paused = true; });
  scrollEl.addEventListener("mouseleave", () => {
    if (pauseBtn.innerHTML.indexOf("Resume") === -1) signalFeedState.paused = false;
  });
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 5s</span>`;
  pollSignalFeed();
  const t = setInterval(pollSignalFeed, 5000);
  pollTimers.push(t);
}

async function pollSignalFeed() {
  if (currentRoute !== "signals" || !visibilityPolling) return;
  const { data, error } = await useFetch("/api/webhooks/recent?limit=50");
  if (error && !data) {
    const body = document.getElementById("feed-body");
    if (body) body.innerHTML = `<tr><td colspan="7" class="feed-empty feed-error">Failed to load: ${escapeHtml(error)}<br><button class="btn outline sm mt" data-action="retry-signals">Retry</button></td></tr>`;
    bindRetry(content, "retry-signals", () => route("signals"));
    return;
  }
  if (!data || !data.webhooks) return;
  const incoming = data.webhooks;
  let newRows = [];
  for (const w of incoming) {
    const id = w.id;
    if (signalFeedState.seenIds.has(id)) continue;
    newRows.push(w);
    signalFeedState.seenIds.add(id);
  }
  if (newRows.length > 0) {
    signalFeedState.rows = newRows.concat(signalFeedState.rows).slice(0, 100);
  }
  if (signalFeedState.rows.length === 0 && incoming.length > 0) {
    signalFeedState.rows = incoming.slice(0, 100);
    incoming.forEach(w => signalFeedState.seenIds.add(w.id));
  }
  const licenseSet = new Set();
  for (const r of signalFeedState.rows) {
    const lk = r.payload && r.payload.license_key ? r.payload.license_key : (r.ip_address || "");
    if (lk) licenseSet.add(maskKey(lk));
  }
  const licenseSel = document.getElementById("feed-filter-license");
  if (licenseSel) {
    const cur = licenseSel.value;
    const opts = ['<option value="">All licenses</option>'].concat(
      Array.from(licenseSet).sort().map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`)
    );
    const newHtml = opts.join("");
    if (licenseSel.innerHTML !== newHtml) {
      licenseSel.innerHTML = newHtml;
      licenseSel.value = cur;
    }
  }
  renderFeedRows();
}

function renderFeedRows() {
  const body = document.getElementById("feed-body");
  if (!body) return;
  const { rows, filterLicense, filterSymbol, filterStatus } = signalFeedState;
  let filtered = rows;
  if (filterLicense) filtered = filtered.filter(r => {
    const lk = r.payload && r.payload.license_key ? maskKey(r.payload.license_key) : (r.ip_address || "");
    return lk === filterLicense;
  });
  if (filterSymbol) filtered = filtered.filter(r => (r.symbol || "").toUpperCase().includes(filterSymbol));
  if (filterStatus) filtered = filtered.filter(r => {
    const cls = statusClassFor(r.status);
    if (filterStatus === "success") return cls === "ok";
    if (filterStatus === "pending") return cls === "warn";
    if (filterStatus === "failed") return cls === "bad";
    if (filterStatus === "duplicate") return cls === "muted";
    return false;
  });
  const countEl = document.getElementById("feed-count");
  if (countEl) countEl.textContent = `${filtered.length} signals`;
  if (filtered.length === 0) {
    body.innerHTML = `<tr><td colspan="7" class="feed-empty">No signals${rows.length > 0 ? " match filters" : ""}</td></tr>`;
    return;
  }
  body.innerHTML = filtered.map(r => {
    const ts = r.timestamp ? new Date(r.timestamp).toLocaleTimeString() : "--";
    const lk = r.payload && r.payload.license_key ? maskKey(r.payload.license_key) : (r.ip_address || "--");
    const action = r.action || "--";
    const sym = r.symbol || "--";
    const lots = r.volume != null ? r.volume : "--";
    const status = r.status || "--";
    const cls = statusClassFor(status);
    const lat = r.execution_time_ms != null ? `${r.execution_time_ms}ms` : "--";
    const actionCls = action === "buy" ? "act-buy" : action === "sell" ? "act-sell" : action === "close" || action === "close_all" ? "act-close" : "act-other";
    return `<tr class="row-${cls}">
      <td class="td-time">${escapeHtml(ts)}</td>
      <td class="td-key">${escapeHtml(lk)}</td>
      <td><span class="action-tag ${actionCls}">${escapeHtml(action)}</span></td>
      <td>${escapeHtml(sym)}</td>
      <td>${escapeHtml(String(lots))}</td>
      <td><span class="status-tag ${cls}">${escapeHtml(status)}</span></td>
      <td class="td-lat">${escapeHtml(lat)}</td>
    </tr>`;
  }).join("");
  if (!signalFeedState.paused) {
    const scroll = document.getElementById("feed-scroll");
    if (scroll) scroll.scrollTop = 0;
  }
}

let eaMapState = { expanded: null };

function renderEaMap(content, actions) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">EA Connections</div>
      <div class="card-desc">Connected EAs with live telemetry - polling every 10s</div>
      <div class="ea-grid" id="ea-grid">
        <div class="ea-empty">Loading connections...</div>
      </div>
    </div>
    <div id="ea-expand-container"></div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  pollEaMap();
  const t = setInterval(pollEaMap, 10000);
  pollTimers.push(t);
}

async function pollEaMap() {
  if (currentRoute !== "ea-map" || !visibilityPolling) return;
  const [overviewRes, eaCheckRes] = await Promise.all([
    useFetch("/api/ea/ws-telemetry/overview").catch(() => ({ data: null, error: "telemetry" })),
    useFetch("/health/ea-check").catch(() => ({ data: null, error: "ea-check" })),
  ]);
  const overview = overviewRes.data;
  const eaCheck = eaCheckRes.data;
  const grid = document.getElementById("ea-grid");
  if (!grid) return;
  if (!overview && !eaCheck) {
    grid.innerHTML = `<div class="ea-empty">Failed to load EA data<br><button class="btn outline sm mt" data-action="retry-ea">Retry</button></div>`;
    const retryBtn = grid.querySelector("[data-action='retry-ea']");
    if (retryBtn) retryBtn.addEventListener("click", e => { e.preventDefault(); pollEaMap(); });
    return;
  }
  const licenses = (overview && overview.licenses) ? overview.licenses : [];
  const dbConns = (eaCheck && eaCheck.db_connections) ? eaCheck.db_connections : [];
  const merged = [];
  const seen = new Set();
  for (const lic of licenses) {
    const key = lic.license_key;
    merged.push({
      license_key: key,
      masked: maskKey(key),
      account: lic.account || null,
      health: lic.health || null,
      open_position_count: lic.open_position_count || 0,
      connType: "WS",
      lastSeen: lic.health && lic.health.timestamp ? lic.health.timestamp : (lic.account && lic.account.timestamp ? lic.account.timestamp : null),
      latency: lic.health && lic.health.ws_latency_ms != null ? lic.health.ws_latency_ms : null,
    });
    seen.add(key);
  }
  for (const c of dbConns) {
    const maskedKey = c.license_key || "--";
    if (seen.has(maskedKey)) continue;
    merged.push({
      license_key: maskedKey,
      masked: maskedKey,
      account: null,
      health: null,
      open_position_count: 0,
      connType: c.type || "HTTP",
      lastSeen: c.last_seen || null,
      latency: null,
    });
    seen.add(maskedKey);
  }
  if (merged.length === 0) {
    grid.innerHTML = `<div class="ea-empty">No EA connections</div>`;
    return;
  }
  const now = Date.now();
  grid.innerHTML = merged.map(ea => {
    const lastSeenMs = ea.lastSeen ? new Date(ea.lastSeen).getTime() : 0;
    const ageSec = ea.lastSeen ? (now - lastSeenMs) / 1000 : 999;
    const statusCls = ageSec < 30 ? "ea-ok" : ageSec < 120 ? "ea-warn" : "ea-bad";
    const statusLabel = ageSec < 30 ? "Connected" : ageSec < 120 ? "Stale" : "Disconnected";
    const acc = ea.account || {};
    const balance = acc.balance != null ? acc.balance.toFixed(2) : "--";
    const equity = acc.equity != null ? acc.equity.toFixed(2) : "--";
    const marginLevel = acc.margin_level != null ? `${acc.margin_level.toFixed(1)}%` : "--";
    const broker = acc.company || acc.server || "--";
    const positions = ea.open_position_count || 0;
    const lat = ea.latency != null ? `${ea.latency}ms` : "--";
    const connBadge = ea.connType === "WS" ? '<span class="conn-badge ws">WS</span>' : '<span class="conn-badge http">HTTP</span>';
    return `<div class="ea-card ${statusCls}" data-key="${escapeHtml(ea.license_key)}" data-action="ea-expand" tabindex="0" role="button">
      <div class="ea-card-head">
        <span class="ea-key">${escapeHtml(ea.masked)}</span>
        ${connBadge}
        <span class="ea-status ${statusCls}">${statusLabel}</span>
      </div>
      <div class="ea-card-body">
        <div class="ea-row"><span>Broker</span><span>${escapeHtml(broker)}</span></div>
        <div class="ea-row"><span>Balance</span><span class="ea-num">${escapeHtml(String(balance))}</span></div>
        <div class="ea-row"><span>Equity</span><span class="ea-num">${escapeHtml(String(equity))}</span></div>
        <div class="ea-row"><span>Margin Lvl</span><span class="ea-num">${escapeHtml(String(marginLevel))}</span></div>
        <div class="ea-row"><span>Positions</span><span class="ea-num">${positions}</span></div>
        <div class="ea-row"><span>Last Seen</span><span>${escapeHtml(relativeTime(ea.lastSeen))}</span></div>
        <div class="ea-row"><span>Latency</span><span class="ea-num">${escapeHtml(lat)}</span></div>
      </div>
    </div>`;
  }).join("");
  grid.querySelectorAll("[data-action='ea-expand']").forEach(card => {
    card.addEventListener("click", e => {
      e.preventDefault();
      const key = card.dataset.key;
      eaMapState.expanded = eaMapState.expanded === key ? null : key;
      document.querySelectorAll(".ea-card").forEach(c => c.classList.remove("expanded"));
      const expandContainer = document.getElementById("ea-expand-container");
      if (eaMapState.expanded) {
        card.classList.add("expanded");
        loadEaTrades(key, expandContainer);
      } else {
        expandContainer.innerHTML = "";
      }
    });
    card.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); card.click(); }
    });
  });
}

async function loadEaTrades(key, container) {
  container.innerHTML = `<div class="card"><div class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</div><div class="card-desc">Loading trade history...</div></div>`;
  try {
    const r = await http(`/api/ea/ws-telemetry/trade-history/${encodeURIComponent(key)}`);
    const data = await r.json();
    const trades = data.deals || data.trades || [];
    if (trades.length === 0) {
      container.innerHTML = `<div class="card"><div class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</div><div class="ea-empty">No trade history</div></div>`;
      return;
    }
    container.innerHTML = `<div class="card">
      <div class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</div>
      <div class="card-desc">${trades.length} recent trades</div>
      <div class="feed-scroll">
        <table class="feed-table">
          <thead><tr><th>Time</th><th>Symbol</th><th>Type</th><th>Lots</th><th>Ticket</th><th>Profit</th></tr></thead>
          <tbody>
            ${trades.map(t => {
              const ts = t.close_time || t.open_time || t.timestamp;
              const tsStr = ts ? new Date(ts).toLocaleString() : "--";
              const sym = t.symbol || "--";
              const type = t.type || t.cmd || (t.entry ? (t.entry === 1 ? "sell" : "buy") : "--");
              const lots = t.volume != null ? t.volume : "--";
              const ticket = t.ticket || t.deal_id || "--";
              const profit = t.profit != null ? t.profit : null;
              const cls = profit != null ? (profit >= 0 ? "ok" : "bad") : "info";
              const pStr = profit != null ? (profit >= 0 ? "+" : "") + profit.toFixed(2) : "--";
              return `<tr class="row-${cls}"><td class="td-time">${escapeHtml(tsStr)}</td><td>${escapeHtml(sym)}</td><td>${escapeHtml(String(type))}</td><td>${escapeHtml(String(lots))}</td><td>${escapeHtml(String(ticket))}</td><td class="ea-num">${escapeHtml(pStr)}</td></tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
  } catch (e) {
    container.innerHTML = `<div class="card"><div class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</div><div class="ea-empty">Failed to load: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderTradeAnalytics(content, actions) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Trade Analytics</div>
      <div class="card-desc">Performance overview - polling every 15s</div>
      <div class="grid grid-4" id="analytics-stats">
        <div class="stat" id="stat-total"><div class="value skeleton line"></div><div class="label">Total Trades</div></div>
        <div class="stat" id="stat-winrate"><div class="value skeleton line"></div><div class="label">Win Rate</div></div>
        <div class="stat" id="stat-latency"><div class="value skeleton line"></div><div class="label">Avg Latency</div></div>
        <div class="stat" id="stat-pf"><div class="value skeleton line"></div><div class="label">Profit Factor</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Trades by Hour (last 24h)</div>
      <div class="card-desc">Hourly trade volume distribution</div>
      <div id="bar-chart-wrap" class="chart-wrap"></div>
    </div>
    <div class="card">
      <div class="card-title">7-Day Success Rate Trend</div>
      <div class="card-desc">Daily success rate over the past week</div>
      <div id="line-chart-wrap" class="chart-wrap"></div>
    </div>
    <div class="card">
      <div class="card-title">Top Symbols by Volume</div>
      <div class="card-desc">Top 5 symbols by trade volume</div>
      <div id="donut-chart-wrap" class="chart-wrap"></div>
    </div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 15s</span>`;
  pollTradeAnalytics();
  const t = setInterval(pollTradeAnalytics, 15000);
  pollTimers.push(t);
}

async function pollTradeAnalytics() {
  if (currentRoute !== "analytics" || !visibilityPolling) return;
  const [statsRes, dashRes] = await Promise.all([
    useFetch("/api/statistics?days=7").catch(() => ({ data: null, error: "stats" })),
    useFetch("/api/trades/admin/dashboard").catch(() => ({ data: null, error: "dashboard" })),
  ]);
  const stats = statsRes.data;
  const dash = dashRes.data;
  if (!stats && !dash) {
    const content = domCache.content || document.getElementById("content");
    if (content) {
      content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load analytics</div><div class="sub">Statistics and dashboard endpoints unavailable</div><button class="btn outline sm mt" data-action="retry-analytics">Retry</button></div>`;
      bindRetry(content, "retry-analytics", () => route("analytics"));
    }
    return;
  }
  const overall = (stats && stats.overall) || {};
  const daily = (stats && stats.daily) || [];
  const alerts = (stats && stats.alerts) || {};
  const setStat = (id, val, cls) => setTile(id, val, cls);
  const totalTrades = overall.total_trades || (dash && dash.overview && dash.overview.total_trades) || 0;
  const successRate = overall.success_rate != null ? overall.success_rate : (dash && dash.overview && dash.overview.success_rate) || 0;
  const avgLatency = alerts.avg_response_time != null ? alerts.avg_response_time : (overall.avg_latency != null ? overall.avg_latency : null);
  const profitFactor = overall.profit_factor != null ? overall.profit_factor : null;
  setStat("stat-total", String(totalTrades), totalTrades > 0 ? "ok" : "info");
  setStat("stat-winrate", `${successRate.toFixed(1)}%`, successRate >= 60 ? "ok" : successRate >= 40 ? "warn" : "bad");
  setStat("stat-latency", avgLatency != null ? `${avgLatency.toFixed(0)}ms` : "--", avgLatency != null && avgLatency < 100 ? "ok" : avgLatency != null && avgLatency < 500 ? "warn" : "info");
  setStat("stat-pf", profitFactor != null ? profitFactor.toFixed(2) : "--", profitFactor != null && profitFactor >= 1.5 ? "ok" : profitFactor != null && profitFactor >= 1 ? "warn" : "info");
  drawBarChart(daily, dash);
  drawLineChart(daily);
  drawDonutChart(stats, dash);
}

function drawBarChart(daily, dash) {
  const wrap = document.getElementById("bar-chart-wrap");
  if (!wrap) return;
  const hours = Array.from({ length: 24 }, (_, i) => ({ hour: i, count: 0 }));
  const now = new Date();
  const todayTrades = (dash && dash.recent_activity) || [];
  for (const t of todayTrades) {
    const ts = t.received_at || t.timestamp;
    if (!ts) continue;
    const d = new Date(ts);
    if (d.toDateString() === now.toDateString()) {
      hours[d.getHours()].count++;
    }
  }
  for (const d of daily) {
    if (!d.date) continue;
    const dd = new Date(d.date);
    if (dd.toDateString() === now.toDateString() && d.total_trades != null) {
      const spread = Math.max(1, d.total_trades);
      hours.forEach(h => { if (h.count === 0) h.count = Math.round(spread / 24); });
    }
  }
  const maxVal = Math.max(1, ...hours.map(h => h.count));
  const W = 600, H = 200, pad = 30, barW = (W - pad * 2) / 24;
  const bars = hours.map((h, i) => {
    const bh = (h.count / maxVal) * (H - pad * 2);
    const x = pad + i * barW;
    const y = H - pad - bh;
    return `<rect x="${x + 1}" y="${y}" width="${barW - 2}" height="${Math.max(0, bh)}" fill="#5e6ad2" rx="2" class="bar-rect">
      <title>${h.hour}:00 - ${h.count} trades</title>
    </rect>`;
  }).join("");
  const labels = hours.filter((_, i) => i % 3 === 0).map(h => {
    const x = pad + h.hour * barW + barW / 2;
    return `<text x="${x}" y="${H - pad + 14}" text-anchor="middle" fill="#82828b" font-size="10">${h.hour}</text>`;
  }).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    ${bars}${labels}
  </svg>`;
}

function drawLineChart(daily) {
  const wrap = document.getElementById("line-chart-wrap");
  if (!wrap) return;
  const points = (daily || []).slice(-7).map(d => ({
    date: d.date || "",
    rate: d.success_rate != null ? d.success_rate : (d.total_trades > 0 ? (d.successful_trades / d.total_trades * 100) : 0),
  }));
  while (points.length < 7) points.unshift({ date: "", rate: 0 });
  const W = 600, H = 200, pad = 30;
  const xStep = (W - pad * 2) / Math.max(1, points.length - 1);
  const coords = points.map((p, i) => ({
    x: pad + i * xStep,
    y: H - pad - (p.rate / 100) * (H - pad * 2),
  }));
  const pathD = coords.map((c, i) => `${i === 0 ? "M" : "L"} ${c.x} ${c.y}`).join(" ");
  const areaD = `${pathD} L ${coords[coords.length - 1].x} ${H - pad} L ${coords[0].x} ${H - pad} Z`;
  const dots = coords.map((c, i) => `<circle cx="${c.x}" cy="${c.y}" r="3" fill="#22c55e"><title>${points[i].date}: ${points[i].rate.toFixed(1)}%</title></circle>`).join("");
  const xLabels = points.map((p, i) => {
    if (i % 2 !== 0) return "";
    const d = p.date ? new Date(p.date) : null;
    const lbl = d ? `${d.getMonth() + 1}/${d.getDate()}` : "";
    return `<text x="${coords[i].x}" y="${H - pad + 14}" text-anchor="middle" fill="#82828b" font-size="10">${lbl}</text>`;
  }).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
    <defs><linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#22c55e" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="#22c55e" stop-opacity="0"/>
    </linearGradient></defs>
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    <path d="${areaD}" fill="url(#lineGrad)"/>
    <path d="${pathD}" fill="none" stroke="#22c55e" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}${xLabels}
  </svg>`;
}

function drawDonutChart(stats, dash) {
  const wrap = document.getElementById("donut-chart-wrap");
  if (!wrap) return;
  const symPerf = (stats && stats.symbol_performance) || [];
  let entries = symPerf.map(s => ({ name: s.symbol || s.name || "--", vol: s.total_trades || s.volume || 0 }))
    .filter(s => s.vol > 0)
    .sort((a, b) => b.vol - a.vol)
    .slice(0, 5);
  if (entries.length === 0 && dash && dash.top_symbols) {
    entries = Object.entries(dash.top_symbols).map(([name, vol]) => ({ name, vol }))
      .sort((a, b) => b.vol - a.vol).slice(0, 5);
  }
  if (entries.length === 0) {
    wrap.innerHTML = `<div class="ea-empty">No symbol data</div>`;
    return;
  }
  const total = entries.reduce((s, e) => s + e.vol, 0);
  const colors = ["#5e6ad2", "#22c55e", "#f59e0b", "#ef4444", "#82828b"];
  const W = 300, H = 200, cx = 100, cy = 100, r = 70, innerR = 45;
  let angle = -Math.PI / 2;
  const slices = entries.map((e, i) => {
    const frac = e.vol / total;
    const startA = angle;
    const endA = angle + frac * Math.PI * 2;
    angle = endA;
    const x1 = cx + r * Math.cos(startA), y1 = cy + r * Math.sin(startA);
    const x2 = cx + r * Math.cos(endA), y2 = cy + r * Math.sin(endA);
    const xi1 = cx + innerR * Math.cos(endA), yi1 = cy + innerR * Math.sin(endA);
    const xi2 = cx + innerR * Math.cos(startA), yi2 = cy + innerR * Math.sin(startA);
    const large = frac > 0.5 ? 1 : 0;
    const path = `M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} L ${xi1} ${yi1} A ${innerR} ${innerR} 0 ${large} 0 ${xi2} ${yi2} Z`;
    const pct = (frac * 100).toFixed(1);
    return `<path d="${path}" fill="${colors[i]}" stroke="#131318" stroke-width="1"><title>${e.name}: ${e.vol} (${pct}%)</title></path>`;
  }).join("");
  const legend = entries.map((e, i) => {
    const pct = ((e.vol / total) * 100).toFixed(1);
    return `<div class="legend-item"><span class="legend-dot" style="background:${colors[i]}"></span><span class="legend-name">${escapeHtml(e.name)}</span><span class="legend-val">${e.vol} (${pct}%)</span></div>`;
  }).join("");
  wrap.innerHTML = `<div class="donut-wrap">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg donut-svg">
      ${slices}
      <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="#e4e4e7" font-size="16" font-weight="700">${total}</text>
      <text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="#9a9aa3" font-size="10">trades</text>
    </svg>
    <div class="legend">${legend}</div>
  </div>`;
}

function renderPipelineMonitor(content, actions) {
  const stages = ["Receive", "Queue", "Validate", "Deliver", "Ack"];
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Signal Pipeline</div>
      <div class="card-desc">5-stage signal processing pipeline - polling every 5s</div>
      <div class="pipeline-viz" id="pipeline-viz">
        ${stages.map((s, i) => {
          const arrow = i < stages.length - 1 ? '<div class="pipe-arrow"><div class="flow-dot"></div></div>' : "";
          return `<div class="pipe-stage" id="pipe-stage-${i}"><div class="pipe-name">${s}</div><div class="pipe-count" id="pipe-count-${i}">--</div></div>${arrow}`;
        }).join("")}
      </div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-title">Queue Depth</div>
        <div class="card-desc">Current pending signals in queue</div>
        <div id="queue-gauge-wrap" class="gauge-center"></div>
      </div>
      <div class="card">
        <div class="card-title">Throughput</div>
        <div class="card-desc">Signals per minute (rolling 60s)</div>
        <div class="stat big-stat" id="throughput-stat"><div class="value">--</div><div class="label">signals/min</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Delivery Latency Histogram</div>
      <div class="card-desc">Latency distribution across buckets</div>
      <div id="histogram-wrap" class="chart-wrap"></div>
    </div>
    <div class="grid grid-2">
      <div class="stat" id="stat-dupes"><div class="value skeleton line"></div><div class="label">Duplicate Rejections</div></div>
      <div class="stat" id="stat-retries"><div class="value skeleton line"></div><div class="label">Retries</div></div>
    </div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 5s</span>`;
  pipelineState.signalHistory = [];
  pipelineState.lastDelivered = 0;
  pollPipeline();
  const t = setInterval(pollPipeline, 5000);
  pollTimers.push(t);
}

let pipelineState = {
  signalHistory: [],
  lastDelivered: 0,
  latencies: [],
};

async function pollPipeline() {
  if (currentRoute !== "pipeline" || !visibilityPolling) return;
  const [statusRes, metricsText] = await Promise.all([
    useFetch("/api/status").catch(() => ({ data: null, error: "status" })),
    fetchMetrics(),
  ]);
  const status = statusRes.data;
  const m = metricsText;
  if (!status && !m) {
    const content = domCache.content || document.getElementById("content");
    if (content) {
      content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load pipeline data</div><div class="sub">Status and metrics endpoints unavailable</div><button class="btn outline sm mt" data-action="retry-pipeline">Retry</button></div>`;
      bindRetry(content, "retry-pipeline", () => route("pipeline"));
    }
    return;
  }
  const webhookTotal = parsePromMetric(m, "pinetunnel_webhook_signals_total");
  const queueDepth = parsePromMetric(m, "pinetunnel_signal_queue_depth");
  const wsConn = parsePromMetric(m, "pinetunnel_websocket_connections");
  const wsDelivered = parsePromMetric(m, "pinetunnel_websocket_signals_delivered_total");
  const dupes = parsePromMetricLabeled(m, "pinetunnel_webhook_signals_total", { result: "duplicate" });
  const retries = parsePromMetricLabeled(m, "pinetunnel_webhook_signals_total", { result: "retry" });
  const httpDur = parseHistogram(m, "pinetunnel_http_request_duration_seconds");
  const received = webhookTotal;
  const delivered = wsDelivered || (status && status.connections && status.connections.total_connections) || 0;
  const validated = received;
  const acked = delivered;
  const counts = [received, queueDepth || 0, validated, delivered, acked];
  for (let i = 0; i < 5; i++) {
    const el = document.getElementById(`pipe-count-${i}`);
    if (el) el.textContent = counts[i] != null ? String(counts[i]) : "--";
  }
  const queueWrap = document.getElementById("queue-gauge-wrap");
  if (queueWrap) {
    const qd = queueDepth || 0;
    const qPct = Math.min(100, (qd / 50) * 100);
    queueWrap.innerHTML = svgGauge(qPct, "Queue", { size: 120, diskMode: false });
    const valEl = queueWrap.querySelector(".gauge-value");
    if (valEl) valEl.textContent = String(qd);
  }
  if (delivered > pipelineState.lastDelivered) {
    const diff = delivered - pipelineState.lastDelivered;
    pipelineState.signalHistory.push({ t: Date.now(), n: diff });
    if (pipelineState.lastDelivered > 0 && httpDur && httpDur.avg != null) {
      pipelineState.latencies.push(httpDur.avg * 1000);
      if (pipelineState.latencies.length > 100) pipelineState.latencies = pipelineState.latencies.slice(-50);
    }
    pipelineState.lastDelivered = delivered;
  }
  pipelineState.signalHistory = pipelineState.signalHistory.filter(h => Date.now() - h.t < 60000);
  const perMin = pipelineState.signalHistory.reduce((s, h) => s + h.n, 0);
  const tpEl = document.getElementById("throughput-stat");
  if (tpEl) {
    const v = tpEl.querySelector(".value");
    if (v) v.textContent = String(perMin);
    tpEl.className = `stat big-stat ${perMin > 10 ? "ok" : perMin > 0 ? "info" : "warn"}`;
  }
  setTile("stat-dupes", String(dupes || 0), dupes > 0 ? "warn" : "ok");
  setTile("stat-retries", String(retries || 0), retries > 0 ? "warn" : "ok");
  drawHistogram();
}

function drawHistogram() {
  const wrap = document.getElementById("histogram-wrap");
  if (!wrap) return;
  const lats = pipelineState.latencies;
  const buckets = [
    { label: "0-10ms", max: 10, count: 0 },
    { label: "10-50ms", max: 50, count: 0 },
    { label: "50-100ms", max: 100, count: 0 },
    { label: "100-500ms", max: 500, count: 0 },
    { label: "500ms+", max: Infinity, count: 0 },
  ];
  for (const l of lats) {
    for (const b of buckets) {
      if (l <= b.max) { b.count++; break; }
    }
  }
  const maxC = Math.max(1, ...buckets.map(b => b.count));
  const W = 600, H = 200, pad = 30, barW = (W - pad * 2) / buckets.length;
  const bars = buckets.map((b, i) => {
    const bh = (b.count / maxC) * (H - pad * 2);
    const x = pad + i * barW;
    const y = H - pad - bh;
    const color = i === 0 ? "#22c55e" : i === 1 ? "#22c55e" : i === 2 ? "#f59e0b" : i === 3 ? "#f59e0b" : "#ef4444";
    return `<rect x="${x + 8}" y="${y}" width="${barW - 16}" height="${Math.max(2, bh)}" fill="${color}" rx="3"><title>${b.label}: ${b.count}</title></rect>
      <text x="${x + barW / 2}" y="${H - pad + 14}" text-anchor="middle" fill="#82828b" font-size="10">${b.label}</text>
      <text x="${x + barW / 2}" y="${y - 6}" text-anchor="middle" fill="#9a9aa3" font-size="10">${b.count}</text>`;
  }).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="rgba(255,255,255,0.08)"/>
    ${bars}
  </svg>`;
}

async function fetchMetrics() {
  try {
    const r = await http("/metrics");
    const text = await r.text();
    return text;
  } catch {
    return "";
  }
}

function parsePromMetric(text, name) {
  if (!text) return 0;
  const re = new RegExp(`^${name}\\{[^}]*\\}\\s+(\\d+(?:\\.\\d+)?)`, "m");
  const match = text.match(re);
  if (match) return parseFloat(match[1]);
  const re2 = new RegExp(`^${name}\\s+(\\d+(?:\\.\\d+)?)`, "m");
  const match2 = text.match(re2);
  return match2 ? parseFloat(match2[1]) : 0;
}

function parsePromMetricLabeled(text, name, labels) {
  if (!text) return 0;
  const labelStr = Object.entries(labels).map(([k, v]) => `${k}="${v}"`).join(",");
  const re = new RegExp(`^${name}\\{[^}]*${labelStr}[^}]*\\}\\s+(\\d+(?:\\.\\d+)?)`, "m");
  const match = text.match(re);
  return match ? parseInt(match[1], 10) : 0;
}

function parseHistogram(text, name) {
  if (!text) return null;
  const countRe = new RegExp(`^${name}_count\\{[^}]*\\}\\s+(\\d+(?:\\.\\d+)?)`, "m");
  const sumRe = new RegExp(`^${name}_sum\\{[^}]*\\}\\s+(\\d+(?:\\.\\d+)?)`, "m");
  const cMatch = text.match(countRe);
  const sMatch = text.match(sumRe);
  if (!cMatch) return null;
  const count = parseInt(cMatch[1], 10);
  const sum = parseFloat(sMatch ? sMatch[1] : 0);
  return { count, sum, avg: count > 0 ? sum / count : 0 };
}

function svgLineChart(values, opts = {}) {
  const W = opts.width || 600;
  const H = opts.height || 160;
  const pad = opts.pad || 24;
  const color = opts.color || "#22c55e";
  const max = opts.max != null ? opts.max : Math.max(100, ...values.map(v => v || 0));
  const min = opts.min || 0;
  const range = max - min || 1;
  const n = values.length;
  if (n === 0) return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="none"></svg>`;
  const pts = values.map((v, i) => {
    const x = pad + (i / Math.max(1, n - 1)) * (W - pad * 2);
    const y = H - pad - ((v - min) / range) * (H - pad * 2);
    return [x, y];
  });
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)} ${H - pad} L${pts[0][0].toFixed(1)} ${H - pad} Z`;
  const gid = "grad-" + Math.random().toString(36).slice(2, 8);
  const gridLines = [0.25, 0.5, 0.75].map(g => {
    const y = pad + g * (H - pad * 2);
    return `<line x1="${pad}" y1="${y}" x2="${W - pad}" y2="${y}" stroke="rgba(255,255,255,0.05)"/>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="none" role="img" aria-label="${opts.label || "chart"}">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${gridLines}
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function svgSparkline(values, opts = {}) {
  const W = 60, H = 20;
  const color = opts.color || "#22c55e";
  const n = values.length;
  if (n === 0) return `<svg viewBox="0 0 ${W} ${H}" class="sparkline"></svg>`;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / Math.max(1, n - 1)) * W;
    const y = H - ((v - min) / range) * H;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" class="sparkline" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "--";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function fmtNum(n, digits = 0) {
  if (n == null || isNaN(n)) return "--";
  return Number(n).toFixed(digits);
}

function statusColor(code) {
  if (code === 200) return "ok";
  if (code === 202) return "info";
  if (code >= 200 && code < 300) return "ok";
  if (code >= 300 && code < 400) return "info";
  if (code === 400) return "warn";
  if (code === 401 || code === 403) return "bad";
  if (code >= 500) return "bad";
  return "warn";
}

let sysHealthState = { cpu: [], mem: [], disk: null, lastHealth: null, lastStats: null };

async function pollSystemHealth() {
  if (currentRoute !== "sys-health" || !visibilityPolling) return;
  const [hRes, sRes] = await Promise.all([
    useFetch("/api/system/health").catch(() => ({ data: null })),
    useFetch("/api/system/stats").catch(() => ({ data: null })),
  ]);
  const h = hRes.data;
  const s = sRes.data;
  if (h) {
    const cpu = h.system ? h.system.cpu_percent : null;
    const mem = h.system ? h.system.memory_percent : null;
    if (cpu != null) {
      sysHealthState.cpu.push(cpu);
      if (sysHealthState.cpu.length > 60) sysHealthState.cpu.shift();
    }
    if (mem != null) {
      sysHealthState.mem.push(mem);
      if (sysHealthState.mem.length > 60) sysHealthState.mem.shift();
    }
    sysHealthState.lastHealth = h;
  }
  if (s) sysHealthState.lastStats = s;
  updateSystemHealthUI();
}

function updateSystemHealthUI() {
  const { lastHealth: h, lastStats: s, cpu, mem } = sysHealthState;
  if (!h) return;
  const cpuEl = document.getElementById("sh-cpu-chart");
  const memEl = document.getElementById("sh-mem-chart");
  if (cpuEl) cpuEl.innerHTML = svgLineChart(cpu, { color: "#22c55e", label: "CPU %", max: 100 });
  if (memEl) memEl.innerHTML = svgLineChart(mem, { color: "#3b82f6", label: "Memory %", max: 100 });
  const cpuVal = h.system ? h.system.cpu_percent : null;
  const memVal = h.system ? h.system.memory_percent : null;
  setTile("sh-cpu-val", cpuVal != null ? `${cpuVal.toFixed(1)}%` : "--", loadColor(cpuVal));
  setTile("sh-mem-val", memVal != null ? `${memVal.toFixed(1)}%` : "--", loadColor(memVal));
  setTile("sh-threads", h.process ? String(h.process.threads) : "--", "info");
  setTile("sh-proc-mem", h.process ? `${h.process.memory_mb.toFixed(1)} MB` : "--", "info");
  const diskWrap = document.getElementById("sh-disk-gauge");
  if (diskWrap && s) {
    const diskPct = s.disk ? s.disk.percent : null;
    diskWrap.innerHTML = svgGauge(diskPct, "Disk", { size: 140, diskMode: true });
  }
  const netEl = document.getElementById("sh-net");
  if (netEl && s && s.network) {
    netEl.innerHTML = `
      <div class="row"><span class="k">Bytes sent</span><span class="v">${fmtBytes(s.network.bytes_sent)}</span></div>
      <div class="row"><span class="k">Bytes recv</span><span class="v">${fmtBytes(s.network.bytes_recv)}</span></div>
      <div class="row"><span class="k">Packets sent</span><span class="v">${(s.network.packets_sent || 0).toLocaleString()}</span></div>
      <div class="row"><span class="k">Packets recv</span><span class="v">${(s.network.packets_recv || 0).toLocaleString()}</span></div>`;
  }
  const poolEl = document.getElementById("sh-db-pool");
  if (poolEl && h.db_pool) {
    const p = h.db_pool;
    const inUse = p.in_use || p.used || 0;
    const avail = p.available || p.free || 0;
    const overflow = p.overflow || 0;
    const total = inUse + avail + overflow || 1;
    const iW = (inUse / total) * 100;
    const aW = (avail / total) * 100;
    const oW = (overflow / total) * 100;
    poolEl.innerHTML = `
      <div class="stacked-bar">
        <div class="seg ok" style="width:${iW}%" title="In use: ${inUse}"></div>
        <div class="seg info" style="width:${aW}%" title="Available: ${avail}"></div>
        <div class="seg warn" style="width:${oW}%" title="Overflow: ${overflow}"></div>
      </div>
      <div class="stacked-legend">
        <span class="lg ok"><span class="dot"></span>In use ${inUse}</span>
        <span class="lg info"><span class="dot"></span>Available ${avail}</span>
        <span class="lg warn"><span class="dot"></span>Overflow ${overflow}</span>
      </div>`;
  }
  const redisEl = document.getElementById("sh-redis");
  if (redisEl) {
    if (h.redis_info && Object.keys(h.redis_info).length) {
      const ri = h.redis_info;
      redisEl.innerHTML = `
        <div class="row"><span class="k">Used memory</span><span class="v">${ri.used_memory_mb} MB</span></div>
        <div class="row"><span class="k">Connected clients</span><span class="v">${ri.connected_clients}</span></div>
        <div class="row"><span class="k">Keyspace hits</span><span class="v">${(ri.keyspace_hits || 0).toLocaleString()}</span></div>
        <div class="row"><span class="k">Keyspace misses</span><span class="v">${(ri.keyspace_misses || 0).toLocaleString()}</span></div>`;
    } else {
      redisEl.innerHTML = `<div class="empty small"><div class="msg">Redis not configured</div></div>`;
    }
  }
}

function renderSystemHealth(content) {
  content.innerHTML = `
    <div class="grid grid-2">
      <div class="card">
        <div class="card-title">CPU Usage</div>
        <div class="card-desc">60s rolling - updates every 5s</div>
        <div class="stat" id="sh-cpu-val"><div class="value skeleton line"></div><div class="label">Current</div></div>
        <div class="chart-wrap" id="sh-cpu-chart"></div>
      </div>
      <div class="card">
        <div class="card-title">Memory Usage</div>
        <div class="card-desc">60s rolling - updates every 5s</div>
        <div class="stat" id="sh-mem-val"><div class="value skeleton line"></div><div class="label">Current</div></div>
        <div class="chart-wrap" id="sh-mem-chart"></div>
      </div>
    </div>
    <div class="grid grid-3">
      <div class="card">
        <div class="card-title">Disk Usage</div>
        <div class="card-desc">Updates every 60s</div>
        <div class="gauge-center" id="sh-disk-gauge"></div>
      </div>
      <div class="card">
        <div class="card-title">Network I/O</div>
        <div class="card-desc">Cumulative counters</div>
        <div id="sh-net"></div>
      </div>
      <div class="card">
        <div class="card-title">DB Pool</div>
        <div class="card-desc">Connection pool stats</div>
        <div id="sh-db-pool"></div>
      </div>
    </div>
    <div class="grid grid-3">
      <div class="card">
        <div class="card-title">Redis Info</div>
        <div class="card-desc">Cache server stats</div>
        <div id="sh-redis"></div>
      </div>
      <div class="stat" id="sh-threads"><div class="value skeleton line"></div><div class="label">Thread Count</div></div>
      <div class="stat" id="sh-proc-mem"><div class="value skeleton line"></div><div class="label">Process Memory</div></div>
    </div>
  `;
  pollSystemHealth();
  const t = setInterval(pollSystemHealth, 5000);
  pollTimers.push(t);
}

let webhookLogState = { rows: [], page: 0, filter: { range: "today", status: "", symbol: "", license: "" } };

async function pollWebhookLogs() {
  if (currentRoute !== "sys-webhooks" || !visibilityPolling) return;
  const { data } = await useFetch("/api/webhooks/recent?limit=50");
  if (!data || !data.webhooks) return;
  webhookLogState.rows = data.webhooks;
  renderWebhookTable();
}

function renderWebhookLogs(content) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Webhook Logs</div>
      <div class="card-desc">Recent webhook requests - polling every 10s</div>
      <div class="filter-bar">
        <select class="input filter-sel" id="wl-range">
          <option value="today">Today</option>
          <option value="7d">7 days</option>
          <option value="30d">30 days</option>
        </select>
        <select class="input filter-sel" id="wl-status">
          <option value="">All status</option>
          <option value="200">200</option>
          <option value="202">202</option>
          <option value="400">400</option>
          <option value="401">401</option>
        </select>
        <input class="input filter-input" id="wl-symbol" placeholder="Symbol filter">
        <input class="input filter-input" id="wl-license" placeholder="License filter">
      </div>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table class="data-table" id="wl-table">
          <thead>
            <tr>
              <th>Timestamp</th><th>Source IP</th><th>Action</th><th>Symbol</th>
              <th>Volume</th><th>Status</th><th>Resp ms</th><th>Payload</th>
            </tr>
          </thead>
          <tbody id="wl-body"></tbody>
        </table>
      </div>
      <div class="table-footer">
        <span id="wl-count">0 rows</span>
        <button class="btn outline sm" id="wl-load-more" data-action="wl-load-more">Load More</button>
      </div>
    </div>
  `;
  const rangeEl = document.getElementById("wl-range");
  const statusEl = document.getElementById("wl-status");
  const symEl = document.getElementById("wl-symbol");
  const licEl = document.getElementById("wl-license");
  if (rangeEl) rangeEl.addEventListener("change", () => { webhookLogState.filter.range = rangeEl.value; renderWebhookTable(); });
  if (statusEl) statusEl.addEventListener("change", () => { webhookLogState.filter.status = statusEl.value; renderWebhookTable(); });
  if (symEl) symEl.addEventListener("input", () => { webhookLogState.filter.symbol = symEl.value.trim().toUpperCase(); renderWebhookTable(); });
  if (licEl) licEl.addEventListener("input", () => { webhookLogState.filter.license = licEl.value.trim(); renderWebhookTable(); });
  const loadMore = content.querySelector("[data-action='wl-load-more']");
  if (loadMore) loadMore.addEventListener("click", e => { e.preventDefault(); webhookLogState.page++; renderWebhookTable(); });
  pollWebhookLogs();
  const t = setInterval(pollWebhookLogs, 10000);
  pollTimers.push(t);
}

function renderWebhookTable() {
  const tbody = document.getElementById("wl-body");
  if (!tbody) return;
  const f = webhookLogState.filter;
  let rows = webhookLogState.rows;
  if (f.status) rows = rows.filter(r => String(r.response_code) === f.status);
  if (f.symbol) rows = rows.filter(r => (r.symbol || "").toUpperCase().includes(f.symbol));
  if (f.license) rows = rows.filter(r => (JSON.stringify(r.payload || "")).includes(f.license));
  const perPage = 50;
  const shown = rows.slice(0, (webhookLogState.page + 1) * perPage);
  if (shown.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty small"><div class="msg">No webhook logs</div></td></tr>`;
  } else {
    tbody.innerHTML = shown.map(r => {
      const cls = statusColor(r.response_code);
      const preview = r.payload ? String(r.payload).slice(0, 60) : "";
      const payloadStr = r.payload ? escapeHtml(JSON.stringify(r.payload)) : "";
      return `<tr class="row-expandable" data-payload="${payloadStr}">
        <td class="mono">${escapeHtml(String(r.timestamp || "").slice(0, 19))}</td>
        <td class="mono">${escapeHtml(r.ip_address || "--")}</td>
        <td>${escapeHtml(r.action || "--")}</td>
        <td class="mono">${escapeHtml(r.symbol || "--")}</td>
        <td class="mono">${escapeHtml(String(r.volume || "--"))}</td>
        <td><span class="badge ${cls}"><span class="dot"></span>${r.response_code || "--"}</span></td>
        <td class="mono">${escapeHtml(String(r.execution_time_ms || "--"))}</td>
        <td class="mono trunc">${escapeHtml(preview)}</td>
      </tr>`;
    }).join("");
  }
  const countEl = document.getElementById("wl-count");
  if (countEl) countEl.textContent = `${shown.length} of ${rows.length} rows`;
  tbody.querySelectorAll(".row-expandable").forEach(tr => {
    tr.addEventListener("click", () => {
      const existing = tr.nextElementSibling && tr.nextElementSibling.classList.contains("row-expanded");
      if (existing) { tr.nextElementSibling.remove(); tr.classList.remove("expanded"); return; }
      const payload = tr.dataset.payload || "(no payload)";
      const exp = document.createElement("tr");
      exp.className = "row-expanded";
      exp.innerHTML = `<td colspan="8"><pre class="payload-pre">${payload}</pre></td>`;
      tr.after(exp);
      tr.classList.add("expanded");
    });
  });
}

async function pollRiskMonitor() {
  if (currentRoute !== "sys-risk" || !visibilityPolling) return;
  const { data, error } = await useFetch("/api/risk-status");
  if (error || !data) {
    const card = document.getElementById("risk-status-card");
    if (card) card.innerHTML = `<div class="empty small"><div class="msg">Risk data unavailable</div><div class="sub">${escapeHtml(error || "")}</div></div>`;
    return;
  }
  updateRiskUI(data);
}

function updateRiskUI(data) {
  const card = document.getElementById("risk-status-card");
  if (card) {
    const ok = data.can_trade;
    card.className = `stat big-stat ${ok ? "ok" : "bad"}`;
    const v = card.querySelector(".value");
    if (v) v.textContent = ok ? "CAN TRADE" : "BLOCKED";
    const l = card.querySelector(".label");
    if (l) l.textContent = data.reason || (ok ? "All checks passed" : "Trading disabled");
  }
  const rm = data.risk_metrics || {};
  const acc = data.account || {};
  setTile("risk-daily-pnl", rm.daily_pnl != null ? `$${Number(rm.daily_pnl).toFixed(2)}` : "--", rm.daily_pnl >= 0 ? "ok" : "bad");
  setTile("risk-max-dd", rm.max_drawdown != null ? `${Number(rm.max_drawdown).toFixed(1)}%` : "--", "warn");
  setTile("risk-mode", rm.position_sizing_mode || "--", "info");
  setTile("risk-pct", rm.risk_per_trade_pct != null ? `${Number(rm.risk_per_trade_pct).toFixed(1)}%` : "--", "info");
  setTile("risk-balance", acc.balance != null ? `$${Number(acc.balance).toFixed(2)}` : "--", "ok");
  setTile("risk-equity", acc.equity != null ? `$${Number(acc.equity).toFixed(2)}` : "--", "ok");
  setTile("risk-margin", acc.margin_level != null ? `${Number(acc.margin_level).toFixed(1)}%` : "--", loadColor(acc.margin_level));
  const alertsEl = document.getElementById("risk-alerts");
  if (alertsEl) {
    const alerts = [];
    if (rm.daily_pnl != null && rm.daily_loss_limit != null && rm.daily_pnl < 0 && Math.abs(rm.daily_pnl) > rm.daily_loss_limit * 0.8) {
      alerts.push({ cls: "warn", msg: "Approaching daily loss limit" });
    }
    if (acc.margin_level != null && acc.margin_level < 120) {
      alerts.push({ cls: "bad", msg: "Margin call warning - margin level below 120%" });
    }
    if (!data.can_trade) {
      alerts.push({ cls: "bad", msg: `Trading blocked: ${data.reason || "unknown"}` });
    }
    alertsEl.innerHTML = alerts.length ? alerts.map(a => `<div class="inline-${a.cls === "bad" ? "error" : "ok"}">${escapeHtml(a.msg)}</div>`).join("") : `<div class="inline-ok">No active alerts</div>`;
  }
}

function renderRiskMonitor(content) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Trading Status</div>
      <div class="card-desc">Current risk gate - polling every 10s</div>
      <div class="stat big-stat" id="risk-status-card"><div class="value skeleton line"></div><div class="label">Loading...</div></div>
    </div>
    <div class="card">
      <div class="card-title">Risk Metrics</div>
      <div class="grid grid-4">
        <div class="stat" id="risk-daily-pnl"><div class="value skeleton line"></div><div class="label">Daily P&L</div></div>
        <div class="stat" id="risk-max-dd"><div class="value skeleton line"></div><div class="label">Max Drawdown</div></div>
        <div class="stat" id="risk-mode"><div class="value skeleton line"></div><div class="label">Position Sizing</div></div>
        <div class="stat" id="risk-pct"><div class="value skeleton line"></div><div class="label">Risk / Trade</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Account</div>
      <div class="grid grid-3">
        <div class="stat" id="risk-balance"><div class="value skeleton line"></div><div class="label">Balance</div></div>
        <div class="stat" id="risk-equity"><div class="value skeleton line"></div><div class="label">Equity</div></div>
        <div class="stat" id="risk-margin"><div class="value skeleton line"></div><div class="label">Margin Level</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Alerts</div>
      <div id="risk-alerts"></div>
    </div>
  `;
  pollRiskMonitor();
  const t = setInterval(pollRiskMonitor, 10000);
  pollTimers.push(t);
}

let errorLogState = { entries: [], paused: false, filter: "ALL", search: "" };

async function pollErrorLogs() {
  if (currentRoute !== "sys-errors" || !visibilityPolling) return;
  const { data } = await useFetch("/api/logs/errors?limit=100");
  if (!data || !data.errors) return;
  const newEntries = data.errors.map(e => {
    const text = typeof e === "string" ? e : (e.message || JSON.stringify(e));
    const level = typeof e === "object" && e.level ? e.level : (text.includes("ERROR") ? "ERROR" : text.includes("WARNING") ? "WARN" : "INFO");
    const ts = typeof e === "object" && e.timestamp ? e.timestamp : "";
    return { timestamp: ts, level, message: text, full: text };
  });
  const existingIds = new Set(errorLogState.entries.map(e => e.message));
  for (const e of newEntries.reverse()) {
    if (!existingIds.has(e.message)) {
      errorLogState.entries.push(e);
      existingIds.add(e.message);
    }
  }
  if (errorLogState.entries.length > 200) errorLogState.entries = errorLogState.entries.slice(-200);
  renderErrorLogList();
}

function renderErrorLogList() {
  const wrap = document.getElementById("el-list");
  if (!wrap) return;
  let entries = errorLogState.entries;
  if (errorLogState.filter !== "ALL") entries = entries.filter(e => e.level === errorLogState.filter);
  if (errorLogState.search) {
    const q = errorLogState.search.toLowerCase();
    entries = entries.filter(e => e.message.toLowerCase().includes(q));
  }
  if (entries.length === 0) {
    wrap.innerHTML = `<div class="empty small"><div class="msg">No log entries</div></div>`;
    return;
  }
  wrap.innerHTML = entries.slice().reverse().map((e, i) => {
    const cls = e.level === "ERROR" ? "bad" : e.level === "WARN" ? "warn" : "info";
    const full = escapeHtml(e.full || e.message);
    return `<div class="log-entry ${cls}" data-idx="${i}" data-full="${full}">
      <span class="log-ts mono">${escapeHtml(String(e.timestamp || "").slice(0, 19))}</span>
      <span class="log-level badge ${cls}">${e.level}</span>
      <span class="log-msg trunc">${escapeHtml(e.message)}</span>
    </div>`;
  }).join("");
  wrap.querySelectorAll(".log-entry").forEach(el => {
    el.addEventListener("click", () => {
      const existing = el.nextElementSibling && el.nextElementSibling.classList.contains("log-expanded");
      if (existing) { el.nextElementSibling.remove(); el.classList.remove("expanded"); return; }
      const full = el.dataset.full || "";
      const exp = document.createElement("div");
      exp.className = "log-expanded";
      exp.innerHTML = `<pre class="payload-pre">${full}</pre>`;
      el.after(exp);
      el.classList.add("expanded");
    });
  });
  if (!errorLogState.paused) {
    const wrap2 = document.getElementById("el-list");
    if (wrap2 && wrap2.parentElement) {
      const scroller = wrap2.closest(".log-scroll-wrap");
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }
  }
}

function renderErrorLogs(content) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Error Log Viewer</div>
      <div class="card-desc">Real-time log tail - polling every 10s - max 200 entries</div>
      <div class="filter-bar">
        <select class="input filter-sel" id="el-filter">
          <option value="ALL">All levels</option>
          <option value="ERROR">ERROR</option>
          <option value="WARN">WARN</option>
        </select>
        <input class="input filter-input" id="el-search" placeholder="Search text...">
        <button class="btn outline sm" id="el-pause" data-action="el-pause">${ICONS.pause}Pause</button>
      </div>
      <div class="log-scroll-wrap" id="el-scroll">
        <div id="el-list"></div>
      </div>
    </div>
  `;
  const filterEl = document.getElementById("el-filter");
  const searchEl = document.getElementById("el-search");
  const pauseBtn = content.querySelector("[data-action='el-pause']");
  if (filterEl) filterEl.addEventListener("change", () => { errorLogState.filter = filterEl.value; renderErrorLogList(); });
  if (searchEl) searchEl.addEventListener("input", () => { errorLogState.search = searchEl.value.trim(); renderErrorLogList(); });
  if (pauseBtn) pauseBtn.addEventListener("click", e => {
    e.preventDefault();
    errorLogState.paused = !errorLogState.paused;
    pauseBtn.innerHTML = errorLogState.paused ? `${ICONS.play}Resume` : `${ICONS.pause}Pause`;
  });
  const scrollWrap = document.getElementById("el-scroll");
  if (scrollWrap) {
    scrollWrap.addEventListener("mouseenter", () => { errorLogState.paused = true; });
    scrollWrap.addEventListener("mouseleave", () => {
      if (pauseBtn && pauseBtn.textContent.includes("Pause")) errorLogState.paused = false;
    });
  }
  pollErrorLogs();
  const t = setInterval(pollErrorLogs, 10000);
  pollTimers.push(t);
}

async function pollDatabaseManager() {
  if (currentRoute !== "sys-database" || !visibilityPolling) return;
  const { data } = await useFetch("/api/database/stats");
  if (!data) return;
  updateDatabaseUI(data);
}

function updateDatabaseUI(data) {
  const tables = data.tables || {};
  const expected = ["trades", "alert_history", "signal_queue", "ea_connections", "ws_signal_log", "ws_account_stats", "ws_open_positions", "ws_trade_history", "ws_health"];
  const body = document.getElementById("db-tables");
  if (body) {
    body.innerHTML = expected.map(t => {
      const cnt = tables[t] != null ? Number(tables[t]).toLocaleString() : "0";
      const cls = tables[t] > 0 ? "info" : "warn";
      return `<div class="row"><span class="k mono">${t}</span><span class="v mono stat-${cls}">${cnt}</span></div>`;
    }).join("");
  }
  setTile("db-size", data.size_mb != null ? `${Number(data.size_mb).toFixed(2)} MB` : "--", "info");
  setTile("db-total", data.total_records != null ? Number(data.total_records).toLocaleString() : "--", "info");
  const typeEl = document.getElementById("db-type");
  if (typeEl) {
    const v = typeEl.querySelector(".value");
    const isPg = data.size_mb != null && data.size_mb > 0;
    if (v) v.textContent = isPg ? "PostgreSQL" : "SQLite";
  }
}

function renderDatabaseManager(content) {
  content.innerHTML = `
    <div class="grid grid-3">
      <div class="stat" id="db-size"><div class="value skeleton line"></div><div class="label">DB Size</div></div>
      <div class="stat" id="db-total"><div class="value skeleton line"></div><div class="label">Total Records</div></div>
      <div class="stat" id="db-type"><div class="value skeleton line"></div><div class="label">DB Type</div></div>
    </div>
    <div class="card">
      <div class="card-title">Table Row Counts</div>
      <div class="card-desc">Records per table - polling every 30s</div>
      <div id="db-tables"></div>
    </div>
    <div class="card">
      <div class="card-title">Cleanup Tool</div>
      <div class="card-desc">Remove old records to free space</div>
      <div class="cleanup-row">
        <label for="db-days">Delete records older than</label>
        <input class="input days-input" id="db-days" type="number" value="90" min="1" max="3650">
        <span>days</span>
        <button class="btn red sm" id="db-cleanup" data-action="db-cleanup">Delete</button>
      </div>
      <div id="db-cleanup-result" aria-live="polite"></div>
    </div>
    <div class="card">
      <div class="card-title">Migration Status</div>
      <div class="card-desc">Database schema migrations</div>
      <div id="db-migrations" class="empty small"><div class="msg">Migration info not available via API</div></div>
    </div>
  `;
  const btn = content.querySelector("[data-action='db-cleanup']");
  if (btn) btn.addEventListener("click", e => { e.preventDefault(); runDbCleanup(); });
  pollDatabaseManager();
  const t = setInterval(pollDatabaseManager, 30000);
  pollTimers.push(t);
}

async function runDbCleanup() {
  const days = parseInt(document.getElementById("db-days").value, 10);
  const result = document.getElementById("db-cleanup-result");
  const btn = document.getElementById("db-cleanup");
  if (!days || days < 1) { result.innerHTML = `<div class="inline-error">Enter a valid number of days</div>`; return; }
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span>Deleting...`;
  try {
    const r = await http(`/api/database/cleanup?days_to_keep=${days}`, { method: "POST", headers: jsonHeaders(true) });
    const data = await r.json();
    result.innerHTML = `<div class="inline-ok">${ICONS.check}Cleanup complete: ${escapeHtml(JSON.stringify(data.result || data))}</div>`;
    toast("Database cleanup complete", "ok");
    pollDatabaseManager();
  } catch (e) {
    result.innerHTML = `<div class="inline-error">${ICONS.x}Cleanup failed: ${escapeHtml(e.message)}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = original;
}

let metricsState = { history: {}, lastValues: {} };

async function pollMetrics() {
  if (currentRoute !== "sys-metrics" || !visibilityPolling) return;
  const text = await fetchMetrics();
  if (!text) return;
  const specs = [
    { key: "webhook_total", name: "pinetunnel_webhook_signals_total", label: "Webhook Signals Total", color: "#22c55e" },
    { key: "ws_delivered", name: "pinetunnel_websocket_signals_delivered_total", label: "WS Signals Delivered", color: "#3b82f6" },
    { key: "queue_depth", name: "pinetunnel_signal_queue_depth", label: "Signal Queue Depth", color: "#f59e0b" },
    { key: "redis_ops", name: "pinetunnel_redis_operations_total", label: "Redis Ops Total", color: "#5e6ad2" },
  ];
  for (const s of specs) {
    let val = parsePromMetric(text, s.name);
    if (s.key === "redis_ops" || s.key === "webhook_total") {
      val = parsePromMetricSum(text, s.name);
    }
    metricsState.lastValues[s.key] = val;
    if (!metricsState.history[s.key]) metricsState.history[s.key] = [];
    metricsState.history[s.key].push(val);
    if (metricsState.history[s.key].length > 20) metricsState.history[s.key].shift();
  }
  const wsPushAvg = parseHistogram(text, "pinetunnel_http_request_duration_seconds");
  const pushVal = wsPushAvg && wsPushAvg.avg != null ? wsPushAvg.avg * 1000 : 0;
  metricsState.lastValues.ws_push_avg = pushVal;
  if (!metricsState.history.ws_push_avg) metricsState.history.ws_push_avg = [];
  metricsState.history.ws_push_avg.push(pushVal);
  if (metricsState.history.ws_push_avg.length > 20) metricsState.history.ws_push_avg.shift();
  updateMetricsUI();
}

function parsePromMetricSum(text, name) {
  if (!text) return 0;
  const re = new RegExp(`^${name}\\{[^}]*\\}\\s+(\\d+(?:\\.\\d+)?)`, "gm");
  let sum = 0;
  let match;
  while ((match = re.exec(text)) !== null) sum += parseFloat(match[1]);
  return sum;
}

function updateMetricsUI() {
  const specs = [
    { key: "webhook_total", label: "Webhook Signals Total", color: "#22c55e", digits: 0 },
    { key: "ws_delivered", label: "WS Signals Delivered", color: "#3b82f6", digits: 0 },
    { key: "queue_depth", label: "Signal Queue Depth", color: "#f59e0b", digits: 0 },
    { key: "redis_ops", label: "Redis Ops Total", color: "#5e6ad2", digits: 0 },
    { key: "ws_push_avg", label: "WS Push Avg (ms)", color: "#22c55e", digits: 1 },
  ];
  const grid = document.getElementById("metrics-grid");
  if (!grid) return;
  grid.innerHTML = specs.map(s => {
    const val = metricsState.lastValues[s.key];
    const hist = metricsState.history[s.key] || [];
    return `<div class="card metric-card">
      <div class="metric-header">
        <span class="metric-name">${s.label}</span>
        ${svgSparkline(hist, { color: s.color })}
      </div>
      <div class="metric-value" style="color:${s.color}">${val != null ? fmtNum(val, s.digits) : "--"}</div>
    </div>`;
  }).join("");
}

function renderMetrics(content) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Performance Metrics</div>
      <div class="card-desc">Prometheus metrics - polling every 10s - sparklines show last 20 samples</div>
    </div>
    <div class="grid grid-3" id="metrics-grid">
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
    </div>
  `;
  pollMetrics();
  const t = setInterval(pollMetrics, 10000);
  pollTimers.push(t);
}

async function pollDiagnostics() {
  if (currentRoute !== "sys-diag" || !visibilityPolling) return;
  const { data, error } = await useFetch("/api/diagnostics");
  if (error || !data) {
    const overall = document.getElementById("diag-overall");
    if (overall) overall.innerHTML = `<div class="empty small"><div class="msg">Diagnostics unavailable</div><div class="sub">${escapeHtml(error || "")}</div></div>`;
    return;
  }
  updateDiagnosticsUI(data);
}

function updateDiagnosticsUI(data) {
  const overall = document.getElementById("diag-overall");
  if (overall) {
    const status = data.overall_status;
    const issueCount = (data.probes || []).filter(p => p.status !== "ok").length;
    const cls = status === "ok" ? "ok" : status === "degraded" ? "warn" : "bad";
    const msg = status === "ok" ? "All Systems Operational" : `${issueCount} issue${issueCount !== 1 ? "s" : ""} detected`;
    overall.className = `stat big-stat ${cls}`;
    const v = overall.querySelector(".value");
    if (v) v.textContent = msg;
  }
  const grid = document.getElementById("diag-grid");
  if (grid) {
    const probes = data.probes || [];
    const wanted = ["database", "redis", "disk", "memory", "websocket_hub", "signal_queue", "rate_limiter", "client_manager"];
    const probeMap = {};
    for (const p of probes) probeMap[p.name] = p;
    grid.innerHTML = wanted.map(name => {
      const p = probeMap[name];
      if (!p) return `<div class="card probe-card warn"><div class="probe-name">${name}</div><div class="badge warn"><span class="dot"></span>unknown</div><div class="probe-latency">-- ms</div><div class="probe-detail">No probe data</div></div>`;
      const cls = p.status === "ok" ? "ok" : p.status === "fail" ? "bad" : "warn";
      const badgeCls = cls === "ok" ? "ok" : cls === "bad" ? "bad" : "warn";
      return `<div class="card probe-card ${cls}">
        <div class="probe-header">
          <span class="probe-name">${escapeHtml(p.name)}</span>
          <span class="badge ${badgeCls}"><span class="dot"></span>${escapeHtml(p.status)}</span>
        </div>
        <div class="probe-latency mono">${p.latency_ms != null ? p.latency_ms.toFixed(2) + " ms" : "-- ms"}</div>
        <div class="probe-detail trunc">${escapeHtml(p.detail || "")}</div>
      </div>`;
    }).join("");
  }
}

function renderDiagnostics(content) {
  content.innerHTML = `
    <div class="card">
      <div class="card-title">Overall Status</div>
      <div class="stat big-stat" id="diag-overall"><div class="value skeleton line"></div><div class="label">Running diagnostics...</div></div>
    </div>
    <div class="grid grid-4" id="diag-grid">
      ${Array(8).fill('<div class="card probe-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}
    </div>
  `;
  pollDiagnostics();
  const t = setInterval(pollDiagnostics, 15000);
  pollTimers.push(t);
}

async function pollBotStatus() {
  if (currentRoute !== "sys-bot" || !visibilityPolling) return;
  const { data, error } = await useFetch("/health/bot");
  if (error || !data) {
    const card = document.getElementById("bot-status-card");
    if (card) card.innerHTML = `<div class="empty small"><div class="msg">Bot status unavailable</div><div class="sub">${escapeHtml(error || "")}</div></div>`;
    return;
  }
  updateBotUI(data);
}

function updateBotUI(data) {
  const started = data.started;
  const hasApp = data.has_app;
  const tokenSet = data.token_set;
  const updaterRunning = data.updater_running;
  const appRunning = data.app_running;
  const card = document.getElementById("bot-status-card");
  if (card) {
    const ok = started && updaterRunning;
    card.className = `stat big-stat ${ok ? "ok" : "bad"}`;
    const v = card.querySelector(".value");
    if (v) v.textContent = ok ? "RUNNING" : "STOPPED";
    const l = card.querySelector(".label");
    if (l) l.textContent = ok ? "Bot is online" : "Bot is offline";
  }
  setTile("bot-started", started ? "Yes" : "No", started ? "ok" : "bad");
  setTile("bot-app", hasApp ? "Yes" : "No", hasApp ? "ok" : "warn");
  setTile("bot-token", tokenSet ? "Set" : "Missing", tokenSet ? "ok" : "bad");
  setTile("bot-updater", updaterRunning ? "Running" : "Stopped", updaterRunning ? "ok" : "warn");
  const handlerEl = document.getElementById("bot-handlers");
  if (handlerEl) {
    const v = handlerEl.querySelector(".value");
    if (v) v.textContent = data.handler_count != null ? String(data.handler_count) : "--";
  }
  const adminEl = document.getElementById("bot-admins");
  if (adminEl) {
    const env = data.env || {};
    const admins = env.TELEGRAM_ADMIN_IDS || "";
    adminEl.innerHTML = `<div class="row"><span class="k">Admin IDs</span><span class="v mono">${escapeHtml(admins || "not set")}</span></div>
      <div class="row"><span class="k">Token len</span><span class="v mono">${env.TELEGRAM_BOT_TOKEN_len || 0}</span></div>`;
  }
  const usernameEl = document.getElementById("bot-username");
  if (usernameEl) {
    if (data.bot && data.bot.username) {
      usernameEl.querySelector(".value").textContent = "@" + data.bot.username;
    } else {
      usernameEl.querySelector(".value").textContent = "unavailable";
    }
  }
}

function renderBotStatus(content) {
  content.innerHTML = `
    <div class="grid grid-2">
      <div class="card">
        <div class="card-title">Bot Status</div>
        <div class="card-desc">Telegram bot health - polling every 15s</div>
        <div class="stat big-stat" id="bot-status-card"><div class="value skeleton line"></div><div class="label">Loading...</div></div>
      </div>
      <div class="card">
        <div class="card-title">Bot Info</div>
        <div class="stat" id="bot-username"><div class="value skeleton line"></div><div class="label">Username</div></div>
        <div class="stat" id="bot-handlers"><div class="value skeleton line"></div><div class="label">Handler Count</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Status Flags</div>
      <div class="grid grid-4">
        <div class="stat" id="bot-started"><div class="value skeleton line"></div><div class="label">Started</div></div>
        <div class="stat" id="bot-app"><div class="value skeleton line"></div><div class="label">App Exists</div></div>
        <div class="stat" id="bot-token"><div class="value skeleton line"></div><div class="label">Token</div></div>
        <div class="stat" id="bot-updater"><div class="value skeleton line"></div><div class="label">Updater</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Admin Configuration</div>
      <div id="bot-admins"></div>
    </div>
    <div class="card">
      <div class="card-title">Alerts</div>
      <div class="card-desc">Trade alert notifications</div>
      <div class="row">
        <span class="k">Alerts enabled</span>
        <span class="v"><label class="toggle"><input type="checkbox" checked disabled><span class="toggle-slider"></span></label></span>
      </div>
      <button class="btn primary sm mt" id="bot-test-msg" data-action="bot-test-msg">Send Test Message</button>
      <div id="bot-test-result" aria-live="polite"></div>
    </div>
  `;
  const testBtn = content.querySelector("[data-action='bot-test-msg']");
  if (testBtn) testBtn.addEventListener("click", e => { e.preventDefault(); sendBotTestMessage(); });
  pollBotStatus();
  const t = setInterval(pollBotStatus, 15000);
  pollTimers.push(t);
}

async function sendBotTestMessage() {
  const btn = document.getElementById("bot-test-msg");
  const result = document.getElementById("bot-test-result");
  if (!btn || !result) return;
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span>Sending...`;
  result.innerHTML = "";
  try {
    const r = await http("/debug/telegram-test", { method: "GET", headers: adminHeaders() });
    const data = await r.json();
    result.innerHTML = `<div class="inline-ok">${ICONS.check}Test message sent: ${escapeHtml(JSON.stringify(data))}</div>`;
    toast("Test message sent", "ok");
  } catch (e) {
    result.innerHTML = `<div class="inline-error">${ICONS.x}Failed: ${escapeHtml(e.message)}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = original;
}

let licenseState = { rows: [], search: "" };

async function renderLicenses(content, actions) {
  content.innerHTML = skeletonCard(1);
  actions.innerHTML = `<button class="btn primary sm" id="add-license-btn" data-action="add-license">${ICONS.plus}Add License</button>`;
  const addBtn = actions.querySelector("[data-action='add-license']");
  if (addBtn) addBtn.addEventListener("click", e => { e.preventDefault(); openLicenseModal(); });
  const { data, error, stale } = await useFetch(`${API}/users`);
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load licenses</div><div class="sub">${escapeHtml(error)}</div><button class="btn outline sm mt" data-action="retry-licenses">Retry</button></div>`;
    bindRetry(content, "retry-licenses", () => route("licenses"));
    return;
  }
  licenseState.rows = data ? data.users : [];
  const staleBannerHtml = stale ? staleBanner() : "";
  const total = data ? data.total_users : 0;
  const totalEAs = licenseState.rows.reduce((n, u) => n + (u.stats && u.stats.connected_eas || 0), 0);
  content.innerHTML = `
    ${staleBannerHtml}
    <div class="panel-toolbar">
      <input class="input search-input" id="lic-search" placeholder="Search by key, name, or email" value="${escapeHtml(licenseState.search)}" aria-label="Search licenses">
      <span class="badge info">${total} users</span>
      <span class="badge ok">${totalEAs} EAs connected</span>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table class="data-table mgr-table">
          <thead>
            <tr>
              <th>License Key</th>
              <th>Name</th>
              <th>Email</th>
              <th>Status</th>
              <th>Secret</th>
              <th>Expires</th>
              <th class="td-num">EAs</th>
              <th class="td-num">Trades</th>
              <th>Last Activity</th>
              <th class="td-actions">Actions</th>
            </tr>
          </thead>
          <tbody id="lic-body"></tbody>
        </table>
      </div>
    </div>
  `;
  const searchEl = content.querySelector("#lic-search");
  if (searchEl) searchEl.addEventListener("input", () => {
    licenseState.search = searchEl.value.trim().toLowerCase();
    renderLicenseRows();
  });
  renderLicenseRows();
  pollLicenses();
  const t = setInterval(pollLicenses, 15000);
  pollTimers.push(t);
}

function pollLicenses() {
  if (currentRoute !== "licenses" || !visibilityPolling) return;
  useFetch(`${API}/users`).then(({ data }) => {
    if (!data) return;
    licenseState.rows = data.users;
    renderLicenseRows();
  });
}

function renderLicenseRows() {
  const body = document.getElementById("lic-body");
  if (!body) return;
  const q = licenseState.search;
  let rows = licenseState.rows;
  if (q) {
    rows = rows.filter(u => {
      const hay = `${u.email || ""} ${u.name || ""} ` + (u.licenses || []).map(l => l.license_key || "").join(" ");
      return hay.toLowerCase().includes(q);
    });
  }
  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="10" class="empty small"><div class="msg">${licenseState.rows.length === 0 ? "No licenses yet" : "No matches"}</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map(u => {
    const lic = (u.licenses && u.licenses[0]) || {};
    const stats = u.stats || {};
    const status = lic.status || "active";
    const enabled = lic.enabled !== false;
    let pillCls = "ok";
    let pillLabel = "Active";
    if (!enabled || status === "disabled") { pillCls = "bad"; pillLabel = "Disabled"; }
    else if (status === "expired") { pillCls = "warn"; pillLabel = "Expired"; }
    const expires = lic.expires_at ? new Date(lic.expires_at).toLocaleDateString() : "--";
    const lastAct = lic.last_activity ? relativeTime(lic.last_activity) : (stats.total_trades > 0 ? "prior" : "never");
    return `<tr>
      <td class="td-key" title="${escapeHtml(lic.license_key || "")}">${escapeHtml(maskKey(lic.license_key))}</td>
      <td>${escapeHtml(u.name || "--")}</td>
      <td class="td-email" title="${escapeHtml(u.email || "")}">${escapeHtml(u.email || "--")}</td>
      <td><span class="status-pill ${pillCls}"><span class="dot"></span>${pillLabel}</span></td>
      <td class="secret-cell">****</td>
      <td>${escapeHtml(expires)}</td>
      <td class="td-num">${stats.connected_eas || 0}</td>
      <td class="td-num">${stats.total_trades || 0}</td>
      <td>${escapeHtml(lastAct)}</td>
      <td class="td-actions">
        <button class="btn ghost sm" data-action="lic-edit" data-key="${escapeHtml(lic.license_key || "")}" title="Edit">${ICONS.edit}</button>
        <button class="btn ghost sm" data-action="lic-extend" data-key="${escapeHtml(lic.license_key || "")}" title="Extend +30d">+30d</button>
        <button class="btn ghost sm" data-action="lic-toggle" data-key="${escapeHtml(lic.license_key || "")}" data-enabled="${enabled ? "1" : "0"}" title="${enabled ? "Disable" : "Enable"}">${enabled ? ICONS.ban : ICONS.power}</button>
        <button class="btn ghost sm" data-action="lic-disconnect" data-key="${escapeHtml(lic.license_key || "")}" title="Force disconnect">${ICONS.power}</button>
        <button class="btn ghost sm" data-action="lic-delete" data-key="${escapeHtml(lic.license_key || "")}" data-name="${escapeHtml(u.email || u.name || "")}" title="Delete">${ICONS.trash}</button>
      </td>
    </tr>`;
  }).join("");
  body.querySelectorAll("[data-action='lic-edit']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); comingSoon("License editing"); }));
  body.querySelectorAll("[data-action='lic-extend']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); comingSoon("License extension"); }));
  body.querySelectorAll("[data-action='lic-toggle']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); comingSoon("Enable/disable license"); }));
  body.querySelectorAll("[data-action='lic-disconnect']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); comingSoon("Force disconnect"); }));
  body.querySelectorAll("[data-action='lic-delete']").forEach(b => b.addEventListener("click", e => {
    e.preventDefault();
    const key = b.dataset.key;
    const name = b.dataset.name;
    openConfirmModal("Delete license", `Delete license for ${escapeHtml(name)}?`, () => comingSoon("License deletion"));
  }));
}

function comingSoon(feature) {
  toast(`${feature} - coming soon (Phase 3)`, "bad");
}

function genKey(prefix) {
  const seg = () => Math.random().toString(36).slice(2, 6).toUpperCase();
  return `${prefix}-${seg()}-${seg()}-${seg()}-${seg()}`;
}

function openLicenseModal() {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `<div class="modal-card">
    <button class="modal-close" data-action="modal-close" aria-label="Close">${ICONS.x}</button>
    <div class="modal-title">Add License</div>
    <div class="modal-desc">Create a new license key. CRUD endpoints arrive in Phase 3.</div>
    <div class="modal-body">
      <div class="field">
        <label for="lic-modal-key">License Key</label>
        <div class="gen-row">
          <input class="input" id="lic-modal-key" value="${genKey("PT")}" readonly>
          <button class="btn outline sm" data-action="regen-key">${ICONS.refresh}Regenerate</button>
        </div>
      </div>
      <div class="field">
        <label for="lic-modal-name">Name</label>
        <input class="input" id="lic-modal-name" placeholder="Client name">
      </div>
      <div class="field">
        <label for="lic-modal-email">Email</label>
        <input class="input" id="lic-modal-email" type="email" placeholder="client@example.com">
      </div>
      <div class="field">
        <label for="lic-modal-secret">Secret</label>
        <div class="gen-row">
          <input class="input" id="lic-modal-secret" value="${genKey("SEC")}" readonly>
          <button class="btn outline sm" data-action="regen-secret">${ICONS.refresh}Regenerate</button>
        </div>
      </div>
      <div class="field">
        <label for="lic-modal-expires">Expires At</label>
        <input class="input" id="lic-modal-expires" type="date">
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn outline" data-action="modal-cancel">Cancel</button>
      <button class="btn primary" data-action="modal-save">${ICONS.check}Create (Phase 3)</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  overlay.querySelector("[data-action='modal-close']").addEventListener("click", close);
  overlay.querySelector("[data-action='modal-cancel']").addEventListener("click", close);
  overlay.querySelector("[data-action='regen-key']").addEventListener("click", e => { e.preventDefault(); overlay.querySelector("#lic-modal-key").value = genKey("PT"); });
  overlay.querySelector("[data-action='regen-secret']").addEventListener("click", e => { e.preventDefault(); overlay.querySelector("#lic-modal-secret").value = genKey("SEC"); });
  overlay.querySelector("[data-action='modal-save']").addEventListener("click", e => {
    e.preventDefault();
    close();
    comingSoon("License creation");
  });
}

function openConfirmModal(title, msg, onConfirm) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `<div class="modal-card">
    <div class="modal-title">${escapeHtml(title)}</div>
    <div class="modal-desc">${msg}</div>
    <div class="modal-footer">
      <button class="btn outline" data-action="confirm-cancel">Cancel</button>
      <button class="btn red" data-action="confirm-ok">${ICONS.trash}Delete</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  overlay.querySelector("[data-action='confirm-cancel']").addEventListener("click", close);
  overlay.querySelector("[data-action='confirm-ok']").addEventListener("click", e => { e.preventDefault(); close(); onConfirm(); });
}

let securityState = { data: null, headers: null };

async function renderSecurity(content, actions) {
  content.innerHTML = skeletonCard(2);
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  const [rlRes, hdrRes] = await Promise.all([
    useFetch(`${API}/rate-limits`),
    useFetch(`${API}/security-headers`),
  ]);
  if (rlRes.error && !rlRes.data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load security data</div><div class="sub">${escapeHtml(rlRes.error)}</div><button class="btn outline sm mt" data-action="retry-security">Retry</button></div>`;
    bindRetry(content, "retry-security", () => route("security"));
    return;
  }
  securityState.data = rlRes.data;
  securityState.headers = hdrRes.data;
  renderSecurityContent(content);
  pollSecurity();
  const t = setInterval(pollSecurity, 10000);
  pollTimers.push(t);
}

function pollSecurity() {
  if (currentRoute !== "security" || !visibilityPolling) return;
  Promise.all([useFetch(`${API}/rate-limits`), useFetch(`${API}/security-headers`)]).then(([rl, hdr]) => {
    if (rl.data) securityState.data = rl.data;
    if (hdr.data) securityState.headers = hdr.data;
    const content = domCache.content || document.getElementById("content");
    if (content && currentRoute === "security") renderSecurityContent(content);
  });
}

function renderSecurityContent(content) {
  const d = securityState.data || {};
  const hdr = securityState.headers || {};
  const blocked = d.blocked_ips || [];
  const blockedCount = blocked.length;
  const failed24h = d.blocked_requests || 0;
  const rateHits = d.rate_limited_requests || 0;
  const headersActive = hdr.headers ? Object.keys(hdr.headers).length : 0;
  const totalHeaders = 5;
  const headersCls = headersActive >= totalHeaders ? "ok" : headersActive > 0 ? "warn" : "bad";

  const headers = hdr.headers || {};
  const headerList = [
    { name: "X-Frame-Options", val: headers.x_frame_options },
    { name: "Content-Security-Policy", val: headers.content_security_policy },
    { name: "X-Content-Type-Options", val: headers.x_content_type_options },
    { name: "Referrer-Policy", val: headers.referrer_policy },
    { name: "Strict-Transport-Security", val: headers.hsts },
  ];

  const tvAllow = hdr.tradingview_ip_allowlist;
  const tvIps = hdr.tradingview_ips || [];

  const blockedRows = blocked.length === 0
    ? `<tr><td colspan="5" class="empty small"><div class="msg">No blocked IPs</div></td></tr>`
    : blocked.map(b => `<tr>
        <td class="td-key">${escapeHtml(b.ip)}</td>
        <td>${escapeHtml(relativeTime(null))}</td>
        <td>Rate limit exceeded</td>
        <td class="td-num">${b.remaining_seconds || 0}s</td>
        <td class="td-actions"><button class="btn ghost sm" data-action="unblock-ip" data-ip="${escapeHtml(b.ip)}">Unblock</button></td>
      </tr>`).join("");

  content.innerHTML = `
    <div class="stat-grid-4">
      <div class="sec-stat ${blockedCount > 0 ? "bad" : "ok"}">
        <div class="value">${blockedCount}</div>
        <div class="label">Blocked IPs</div>
      </div>
      <div class="sec-stat ${failed24h > 10 ? "bad" : failed24h > 0 ? "warn" : "ok"}">
        <div class="value">${failed24h}</div>
        <div class="label">Failed Attempts</div>
      </div>
      <div class="sec-stat ${rateHits > 50 ? "warn" : "ok"}">
        <div class="value">${rateHits}</div>
        <div class="label">Rate Limit Hits</div>
      </div>
      <div class="sec-stat ${headersCls}">
        <div class="value">${headersActive}/${totalHeaders}</div>
        <div class="label">Security Headers</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Blocked IPs</div>
      <div class="card-desc">Currently blocked by rate limiter</div>
      <div class="table-wrap">
        <table class="data-table mgr-table">
          <thead><tr><th>IP</th><th>Blocked At</th><th>Reason</th><th class="td-num">Remaining</th><th class="td-actions">Action</th></tr></thead>
          <tbody>${blockedRows}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Security Headers</div>
      <div class="card-desc">HTTP security response headers</div>
      <div class="headers-checklist">
        ${headerList.map(h => {
          const ok = !!h.val;
          return `<div class="header-item">
            <span class="header-mark ${ok ? "ok" : "bad"}">${ok ? ICONS.check : ICONS.x}</span>
            <span class="h-name">${escapeHtml(h.name)}</span>
            <span class="h-val" title="${escapeHtml(h.val || "")}">${escapeHtml(h.val || "missing")}</span>
          </div>`;
        }).join("")}
      </div>
    </div>
    <div class="card">
      <div class="card-title">TradingView IP Allowlist</div>
      <div class="card-desc">Webhook requests restricted to known TradingView egress IPs</div>
      <div class="allowlist-status">
        <div>
          <div class="label">Status: <strong style="color:${tvAllow ? "var(--green)" : "var(--muted-2)"}">${tvAllow ? "Enabled" : "Disabled"}</strong></div>
          ${tvIps.length > 0 ? `<div class="ips">${tvIps.map(escapeHtml).join(", ")}</div>` : ""}
        </div>
        <span class="status-pill ${tvAllow ? "ok" : "muted"}"><span class="dot"></span>${tvAllow ? "ON" : "OFF"}</span>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Recent 401/403 Responses</div>
      <div class="card-desc">Authentication and authorization failures</div>
      <div class="empty small"><div class="msg">Requires audit log filtering by action - available when audit endpoints support status filtering</div></div>
    </div>
  `;
  content.querySelectorAll("[data-action='unblock-ip']").forEach(b => b.addEventListener("click", async e => {
    e.preventDefault();
    const ip = b.dataset.ip;
    comingSoon("IP unblock (Phase 3)");
  }));
}

let auditState = { rows: [], filterAction: "", filterAdmin: "", filterFrom: "", filterTo: "", search: "", loading: false, hasMore: true, limit: 50 };

async function renderAuditTimeline(content, actions) {
  content.innerHTML = skeletonCard(1);
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  auditState = { rows: [], filterAction: "", filterAdmin: "", filterFrom: "", filterTo: "", search: "", loading: false, hasMore: true, limit: 50 };
  await loadAuditPage(true);
  renderAuditContent(content);
  pollAudit();
  const t = setInterval(pollAudit, 10000);
  pollTimers.push(t);
}

async function loadAuditPage(isInitial) {
  if (auditState.loading) return;
  auditState.loading = true;
  const { data, error } = await useFetch(`${API}/audit-actions?limit=${auditState.limit}`);
  auditState.loading = false;
  if (error && !data) return;
  if (!data || !data.actions) return;
  auditState.rows = data.actions;
  auditState.hasMore = data.actions.length >= auditState.limit;
}

function pollAudit() {
  if (currentRoute !== "audit" || !visibilityPolling) return;
  loadAuditPage(false).then(() => {
    const content = domCache.content || document.getElementById("content");
    if (content && currentRoute === "audit") renderAuditContent(content);
  });
}

function auditSeverityClass(action) {
  const a = (action || "").toLowerCase();
  if (a.includes("delete") || a.includes("disable") || a.includes("force") || a.includes("restart") || a.includes("revoke")) return "bad";
  if (a.includes("edit") || a.includes("update") || a.includes("config") || a.includes("extend")) return "warn";
  if (a.includes("add") || a.includes("create") || a.includes("enable") || a.includes("login") || a.includes("success")) return "ok";
  return "info";
}

function sourceBadge(user) {
  const u = (user || "").toLowerCase();
  if (u.includes("telegram") || u.startsWith("tg")) return '<span class="src-badge tg">Telegram</span>';
  if (u.includes("dashboard") || u.includes("admin")) return '<span class="src-badge dash">Dashboard</span>';
  if (u.includes("api")) return '<span class="src-badge api">API</span>';
  return "";
}

function renderAuditContent(content) {
  const actions = new Set();
  const admins = new Set();
  for (const a of auditState.rows) {
    if (a.action) actions.add(a.action);
    if (a.user) admins.add(a.user);
  }
  let filtered = auditState.rows;
  if (auditState.filterAction) filtered = filtered.filter(a => a.action === auditState.filterAction);
  if (auditState.filterAdmin) filtered = filtered.filter(a => a.user === auditState.filterAdmin);
  if (auditState.filterFrom) {
    const from = new Date(auditState.filterFrom).getTime();
    filtered = filtered.filter(a => new Date(a.timestamp).getTime() >= from);
  }
  if (auditState.filterTo) {
    const to = new Date(auditState.filterTo).getTime();
    filtered = filtered.filter(a => new Date(a.timestamp).getTime() <= to);
  }
  if (auditState.search) {
    const q = auditState.search.toLowerCase();
    filtered = filtered.filter(a => {
      const hay = `${a.action || ""} ${a.user || ""} ${a.ip_address || ""} ${a.details || ""}`;
      return hay.toLowerCase().includes(q);
    });
  }

  content.innerHTML = `
    <div class="panel-toolbar">
      <select class="input filter-sel" id="audit-filter-action" aria-label="Filter by action">
        <option value="">All actions</option>
        ${Array.from(actions).sort().map(a => `<option value="${escapeHtml(a)}" ${auditState.filterAction === a ? "selected" : ""}>${escapeHtml(a)}</option>`).join("")}
      </select>
      <select class="input filter-sel" id="audit-filter-admin" aria-label="Filter by admin">
        <option value="">All admins</option>
        ${Array.from(admins).sort().map(a => `<option value="${escapeHtml(a)}" ${auditState.filterAdmin === a ? "selected" : ""}>${escapeHtml(a)}</option>`).join("")}
      </select>
      <input class="input filter-input" id="audit-filter-from" type="date" value="${escapeHtml(auditState.filterFrom)}" aria-label="From date">
      <input class="input filter-input" id="audit-filter-to" type="date" value="${escapeHtml(auditState.filterTo)}" aria-label="To date">
      <input class="input search-input" id="audit-search" placeholder="Search details" value="${escapeHtml(auditState.search)}" aria-label="Search">
    </div>
    <div class="card">
      <div class="card-title">Admin Activity Timeline</div>
      <div class="card-desc">${filtered.length} entries - polling every 10s</div>
      <div class="timeline" id="audit-timeline"></div>
      ${auditState.hasMore ? `<div class="load-more-row" id="audit-load-more">Showing ${auditState.rows.length} - increase limit for more history</div>` : `<div class="load-more-row">End of log</div>`}
    </div>
  `;
  const tl = content.querySelector("#audit-timeline");
  if (filtered.length === 0) {
    tl.innerHTML = `<div class="empty small"><div class="msg">${auditState.rows.length === 0 ? "No audit entries" : "No matches"}</div></div>`;
  } else {
    tl.innerHTML = filtered.map(a => {
      const sev = auditSeverityClass(a.action);
      const ts = a.timestamp ? new Date(a.timestamp).toLocaleString() : "--";
      const user = a.user || "unknown";
      const src = sourceBadge(a.user);
      const details = a.details ? (typeof a.details === "string" ? a.details : JSON.stringify(a.details, null, 2)) : "";
      const target = a.ip_address || "";
      return `<div class="tl-entry ${sev}">
        <div class="tl-head">
          <span class="tl-action">${escapeHtml(a.action || "--")}</span>
          <span class="tl-time">${escapeHtml(ts)}</span>
          ${src}
        </div>
        <div class="tl-meta">
          <span class="tl-user">by ${escapeHtml(user)}</span>
          ${target ? `<span class="tl-target">IP: ${escapeHtml(target)}</span>` : ""}
        </div>
        ${details ? `<div class="tl-details">${escapeHtml(details)}</div>` : ""}
      </div>`;
    }).join("");
  }
  const actionSel = content.querySelector("#audit-filter-action");
  const adminSel = content.querySelector("#audit-filter-admin");
  const fromEl = content.querySelector("#audit-filter-from");
  const toEl = content.querySelector("#audit-filter-to");
  const searchEl = content.querySelector("#audit-search");
  if (actionSel) actionSel.addEventListener("change", () => { auditState.filterAction = actionSel.value; renderAuditContent(content); });
  if (adminSel) adminSel.addEventListener("change", () => { auditState.filterAdmin = adminSel.value; renderAuditContent(content); });
  if (fromEl) fromEl.addEventListener("change", () => { auditState.filterFrom = fromEl.value; renderAuditContent(content); });
  if (toEl) toEl.addEventListener("change", () => { auditState.filterTo = toEl.value; renderAuditContent(content); });
  if (searchEl) searchEl.addEventListener("input", () => { auditState.search = searchEl.value.trim(); renderAuditContent(content); });
}

window.route = route;
window.doLogin = doLogin;
window.retryLastRoute = retryLastRoute;
window.saveTelegram = saveTelegram;
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
