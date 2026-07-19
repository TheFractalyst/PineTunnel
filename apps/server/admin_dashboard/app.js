(function() {
"use strict";
const API = "/api/dashboard";
const POLL_INTERVAL = 10000;
const FETCH_TIMEOUT = 10000;
const RETRY_DELAY = 2000;
const SKELETON_MIN_MS = 200;
const CACHE_FRESH_MS = 5000;
const CACHE_TTL_MS = 30000;
let pollTimers = [];
let requestGen = 0;
let renderToken = 0;
const inflight = new Map();
let currentRoute = "overview";
let pendingRouteAfterLogin = "overview";
let lastSetupStatus = null;
let loginVisible = false;
let connectionLostVisible = false;
let connBackoff = 5000;
let connRetryTimer = null;
let toastStack = [];
const TOAST_MAX = 3;

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

let analyticsChartsAnimated = false;

const panelStates = {};
function getPanelState(id) {
  if (!panelStates[id]) {
    panelStates[id] = { data: null, error: null, loading: false, filters: {}, pollTimer: null, sig: null };
  }
  return panelStates[id];
}
function setPanelState(id, patch) {
  const s = getPanelState(id);
  if (patch && patch.filters) { Object.assign(s.filters, patch.filters); delete patch.filters; }
  if (patch) Object.assign(s, patch);
}
function cleanupPanel(id) {
  const s = panelStates[id];
  if (s && s.pollTimer) { clearInterval(s.pollTimer); s.pollTimer = null; }
}
function resetPanelData(id) {
  const s = getPanelState(id);
  s.data = null; s.error = null; s.loading = false; s.sig = null;
}
function addPoll(timer) {
  const s = getPanelState(currentRoute);
  if (s.pollTimer) clearInterval(s.pollTimer);
  s.pollTimer = timer;
  pollTimers.push(timer);
}
function startPoll(fn, ms) {
  if (!visibilityPolling) return;
  fn();
  addPoll(setInterval(fn, ms));
}
function staleRender(token) { return token !== renderToken; }

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

let setupDirty = false;
window.addEventListener("beforeunload", e => {
  if (setupDirty) {
    e.preventDefault();
    e.returnValue = "You have unsaved changes in the Setup Wizard.";
    return e.returnValue;
  }
});

const VALIDATORS = {
  tgToken: (v) => {
    if (!v) return "Bot token is required";
    if (!/^\d+:[A-Za-z0-9_-]{35}$/.test(v)) return "Token must match 123456:ABC... (35 char secret)";
    return "";
  },
  tgUid: (v) => {
    if (!v) return "Telegram user ID is required";
    if (!/^\d+$/.test(v)) return "User ID must be numeric";
    if (parseInt(v, 10) <= 0) return "User ID must be greater than 0";
    return "";
  },
  cfToken: (v) => {
    if (!v) return "Tunnel token is required";
    if (!v.startsWith("eyJ")) return "Token must start with eyJ";
    return "";
  },
  cfUrl: (v) => {
    if (!v) return "Tunnel URL is required";
    if (!v.startsWith("https://")) return "URL must start with https://";
    return "";
  },
  days: (v) => {
    const n = parseInt(v, 10);
    if (isNaN(n)) return "Enter a whole number";
    if (n < 1 || n > 3650) return "Must be between 1 and 3650";
    return "";
  },
  symbol: (v) => {
    if (!v) return "Symbol is required";
    if (!/^[A-Z]+$/.test(v)) return "Uppercase letters only";
    return "";
  },
  lots: (v) => {
    const n = parseFloat(v);
    if (isNaN(n)) return "Enter a number";
    if (n <= 0) return "Must be positive";
    return "";
  },
  email: (v) => {
    if (!v) return "";
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) return "Enter a valid email";
    return "";
  },
  loginCode: (v) => v ? "" : "Login code is required",
  loginUid: (v) => {
    if (!v) return "Telegram user ID is required";
    if (!/^\d+$/.test(v)) return "User ID must be numeric";
    return "";
  },
};

function validateInput(input, key) {
  const v = input.value.trim();
  const err = VALIDATORS[key] ? VALIDATORS[key](v) : "";
  const wrap = input.closest(".field") || input.parentElement;
  setFieldState(input, wrap, err);
  return !err;
}

function setFieldState(input, wrap, errMsg) {
  if (!wrap) wrap = input.parentElement;
  let msgEl = wrap.querySelector(".field-error");
  if (errMsg) {
    input.classList.add("input-error");
    input.classList.remove("input-ok");
    input.setAttribute("aria-invalid", "true");
    if (!msgEl) {
      msgEl = document.createElement("div");
      msgEl.className = "field-error";
      msgEl.setAttribute("role", "alert");
      wrap.appendChild(msgEl);
    }
    msgEl.textContent = errMsg;
    input.setAttribute("aria-describedby", msgEl.id || (msgEl.id = input.id + "-err"));
  } else {
    input.classList.remove("input-error");
    if (input.value.trim()) input.classList.add("input-ok");
    else input.classList.remove("input-ok");
    input.removeAttribute("aria-invalid");
    if (msgEl) msgEl.remove();
  }
}

function attachValidator(input, key) {
  if (!input) return;
  input.addEventListener("blur", () => validateInput(input, key));
  input.addEventListener("input", () => {
    if (input.classList.contains("input-error")) validateInput(input, key);
  });
}

function addPasswordToggle(input) {
  if (!input || input.dataset.toggleAdded) return;
  input.dataset.toggleAdded = "1";
  const wrap = input.closest(".field") || input.parentElement;
  if (!wrap) return;
  wrap.classList.add("has-toggle");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pwd-toggle";
  btn.setAttribute("aria-label", "Show value");
  btn.innerHTML = ICONS.eye;
  btn.addEventListener("click", e => {
    e.preventDefault();
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.innerHTML = show ? ICONS.eyeOff : ICONS.eye;
    btn.setAttribute("aria-label", show ? "Hide value" : "Show value");
    input.focus();
  });
  wrap.appendChild(btn);
}

function setBtnLoading(btn, text) {
  if (!btn) return;
  btn.dataset.origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span>${escapeHtml(text)}`;
}

function setBtnSuccess(btn, text, restoreMs = 2000) {
  if (!btn) return;
  btn.innerHTML = `${ICONS.check}${escapeHtml(text)}`;
  btn.classList.add("btn-success");
  setTimeout(() => {
    btn.classList.remove("btn-success");
    btn.disabled = false;
    btn.innerHTML = btn.dataset.origHtml || btn.innerHTML;
  }, restoreMs);
}

function setBtnError(btn, text) {
  if (!btn) return;
  btn.classList.add("btn-error");
  btn.innerHTML = `${ICONS.x}${escapeHtml(text)}`;
  setTimeout(() => {
    btn.classList.remove("btn-error");
    btn.disabled = false;
    btn.innerHTML = btn.dataset.origHtml || btn.innerHTML;
  }, 2500);
}

function autofocusFirst(scope) {
  if (!scope) return;
  const el = scope.querySelector("input:not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled])");
  if (el) setTimeout(() => el.focus(), 50);
}

function closeOnEscape(overlay, closeFn) {
  if (!overlay) return;
  const handler = e => {
    if (e.key === "Escape") { e.preventDefault(); closeFn(); document.removeEventListener("keydown", handler); }
  };
  document.addEventListener("keydown", handler);
}

function submitOnEnter(scope, submitFn) {
  if (!scope) return;
  scope.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.tagName !== "TEXTAREA" && e.target.tagName !== "SELECT") {
      e.preventDefault();
      submitFn();
    }
  });
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

function friendlyMsg(e) {
  if (!e) return "Something went wrong. Please try again.";
  if (e.friendly) return e.friendly;
  const status = e.status;
  const raw = e.message || String(e);
  if (status === 401) return "Your session expired. Please log in again.";
  if (status === 403) return "You do not have permission to do this.";
  if (status === 404) return "The resource was not found on the server.";
  if (status === 429) return "Too many requests. Wait a moment and retry.";
  if (status >= 500) return `Server error (${status}). The server may be overloaded - try again in a moment.`;
  if (status === 0) {
    if (/timeout/i.test(raw)) return "The server took too long to respond. Check if it is running.";
    return "Connection failed. The server is unreachable.";
  }
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return "Connection failed. The server is unreachable.";
  }
  return raw;
}

function errorBlock(opts) {
  const title = opts.title || "Could not load data";
  const detail = opts.detail ? `<div class="sub">${escapeHtml(opts.detail)}</div>` : "";
  const hint = opts.hint ? `<div class="sub">${escapeHtml(opts.hint)}</div>` : "";
  const retryAction = opts.retryAction || "retry-generic";
  const retryLabel = opts.retryLabel || "Retry";
  return `<div class="empty error-state">
    <div class="icon">${ICONS.alert}</div>
    <div class="msg">${escapeHtml(title)}</div>
    ${detail}${hint}
    <button class="btn outline sm mt" data-action="${escapeHtml(retryAction)}">${ICONS.refresh}${escapeHtml(retryLabel)}</button>
  </div>`;
}

function partialErrorBadge(label) {
  return `<div class="partial-badge">${ICONS.alert}<span>${escapeHtml(label)}</span></div>`;
}

const PALETTE = {
  green: "#4cb782",
  red: "#eb5757",
  amber: "#f2994a",
  blue: "#5e6ad2",
  text: "#e4e4e7",
  muted: "#9a9aa3",
  muted2: "#82828b",
  card: "#131318",
  grid: "rgba(255,255,255,0.08)",
  gridFaint: "rgba(255,255,255,0.05)",
  gridTrack: "rgba(255,255,255,0.06)",
  amberBg: "rgba(242,153,74,0.1)",
};

function emptyState(icon, msg, actionLabel, actionName) {
  const iconHtml = typeof icon === "string" && icon.indexOf("<svg") === 0 ? icon : (ICONS[icon] || ICONS.check);
  const btn = actionLabel ? `<button class="btn outline sm mt" data-action="${escapeHtml(actionName || "empty-action")}">${escapeHtml(actionLabel)}</button>` : "";
  return `<div class="empty empty-illustrated">
    <div class="icon">${iconHtml}</div>
    <div class="msg">${escapeHtml(msg)}</div>
    ${btn}
  </div>`;
}

function withMinDisplayTime(promise) {
  const start = Date.now();
  return promise.then(result => {
    const elapsed = Date.now() - start;
    if (elapsed < SKELETON_MIN_MS) {
      return new Promise(resolve => setTimeout(() => resolve(result), SKELETON_MIN_MS - elapsed));
    }
    return result;
  });
}

function bindRetry(scope, action, fn) {
  const el = scope.querySelector(`[data-action="${action}"]`);
  if (el) el.addEventListener("click", e => { e.preventDefault(); fn(); });
}
let routeTimer = null;
let overviewRendered = false;
let overviewSig = null;
let visibilityPolling = true;

const LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="190 48 475 48" preserveAspectRatio="xMidYMid meet" class="brand-logo" aria-label="PineTunnel"><g fill="currentColor"><g transform="translate(196,91.6)"><path d="M32.25-37.94c1.38 0 2.66.34 3.81 1.03 1.16.68 2.07 1.6 2.75 2.75.68 1.15 1.02 2.42 1.02 3.81v7.58c0 1.39-.34 2.66-1.02 3.81-.68 1.15-1.6 2.07-2.75 2.75-1.15.68-2.42 1.02-3.81 1.02H9.48v15.17H1.89v-37.94zM9.48-22.77h22.77v-7.58H9.48z"/></g><g transform="translate(243,91.6)"><path d="M39.83 0H1.89v-7.59h15.19v-22.75H1.89v-7.59h37.94v7.59H24.66v22.75h15.17z"/></g><g transform="translate(290,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(338,91.6)"><path d="M39.83-37.94v7.59H9.48v7.58h30.35v7.59H9.48v7.58h30.35V0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-22.75c0-1.39.34-2.66 1.02-3.81.68-1.16 1.6-2.07 2.75-2.75 1.16-.69 2.44-1.03 3.83-1.03z"/></g><g transform="translate(385,91.6)"><path d="M39.83-37.94v7.59H24.66V0h-7.58v-30.35H1.89v-7.59z"/></g><g transform="translate(432,91.6)"><path d="M32.25 0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-30.35h7.59v30.35H32.25v-30.35h7.58v30.35c0 1.39-.34 2.66-1.02 3.81-.68 1.16-1.6 2.07-2.75 2.75-1.15.69-2.43 1.03-3.81 1.03z"/></g><g transform="translate(479,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(526,91.6)"><path d="M9.48 0H1.89v-34.14c0-1.04.37-1.93 1.11-2.67.75-.75 1.64-1.12 2.69-1.12 1.04 0 1.94.38 2.7 1.14L32.25-12.95V-37.94h7.58v34.14c0 1.04-.37 1.94-1.11 2.69-.74.74-1.63 1.11-2.67 1.11-1.05 0-1.95-.38-2.7-1.14L9.48-24.98z"/></g><g transform="translate(574,91.6)"><path d="M39.83-37.94v7.59H9.48v7.58h30.35v7.59H9.48v7.58h30.35V0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-22.75c0-1.39.34-2.66 1.02-3.81.68-1.16 1.6-2.07 2.75-2.75 1.16-.69 2.44-1.03 3.83-1.03z"/></g><g transform="translate(621,91.6)"><path d="M39.83 0H9.48c-1.39 0-2.67-.34-3.83-1.02-1.15-.69-2.07-1.6-2.75-2.75-.68-1.16-1.02-2.43-1.02-3.82v-30.35h7.59v30.35h30.35z"/></g></g></svg>';

const SVG_ATTRS = ' viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';
const ICONS = {
  overview: '<svg' + SVG_ATTRS + '><path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/></svg>',
  signals: '<svg' + SVG_ATTRS + '><path d="M2 12h4l3-9 6 18 3-9h4"/></svg>',
  feed: '<svg' + SVG_ATTRS + '><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="14" y2="18"/></svg>',
  map: '<svg' + SVG_ATTRS + '><path d="M9 2v6"/><path d="M15 2v6"/><path d="M5 8h14l-1.5 9a2 2 0 0 1-2 1.7H8.5a2 2 0 0 1-2-1.7z"/><path d="M12 19v3"/></svg>',
  analytics: '<svg' + SVG_ATTRS + '><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg>',
  pipeline: '<svg' + SVG_ATTRS + '><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
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
  health: '<svg' + SVG_ATTRS + '><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/></svg>',
  webhook: '<svg' + SVG_ATTRS + '><path d="M18 16.16v-1.6a2 2 0 0 0-1.1-1.8L13 11V7a1.5 1.5 0 0 0-3 0v9l-2.5-1.5a1.5 1.5 0 0 0-1.6 2.5L9 19"/></svg>',
  risk: '<svg' + SVG_ATTRS + '><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  errors: '<svg' + SVG_ATTRS + '><path d="M8 2l1.88 1.88M14.12 3.88L16 2M9 7.13v-1a3 3 0 0 1 .59-1.82A4 4 0 0 1 12 4a4 4 0 0 1 2.41.31A3 3 0 0 1 15 6.13v1"/><path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/><path d="M12 20v-9"/><path d="M6.53 9C4.6 8.8 3 7.1 3 5M6 13H2M3 21c0-2.1 1.7-3.9 3.8-4M20.97 5c0 2.1-1.6 3.8-3.5 4M22 13h-4M17.2 17c2.1.1 3.8 1.9 3.8 4"/></svg>',
  database: '<svg' + SVG_ATTRS + '><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  metrics: '<svg' + SVG_ATTRS + '><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
  diag: '<svg' + SVG_ATTRS + '><path d="M4.8 2.3A.3.3 0 1 0 5 2H4a2 2 0 0 0-2 2v5a6 6 0 0 0 6 6v0a6 6 0 0 0 6-6V4a2 2 0 0 0-2-2h-1a.3.3 0 0 0 .2.3"/><path d="M8 15v1a6 6 0 0 0 6 6v0a6 6 0 0 0 6-6v-4"/><circle cx="20" cy="10" r="2"/></svg>',
  bot: '<svg' + SVG_ATTRS + '><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  license: '<svg' + SVG_ATTRS + '><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 2.5l3 3L16 8l1.5 1.5L14 13"/></svg>',
  security: '<svg' + SVG_ATTRS + '><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  audit: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  eye: '<svg' + SVG_ATTRS + '><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeOff: '<svg' + SVG_ATTRS + '><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
  trash: '<svg' + SVG_ATTRS + '><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  ban: '<svg' + SVG_ATTRS + '><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
  power: '<svg' + SVG_ATTRS + '><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>',
  edit: '<svg' + SVG_ATTRS + '><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  plus: '<svg' + SVG_ATTRS + '><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  minus: '<svg' + SVG_ATTRS + '><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  search: '<svg' + SVG_ATTRS + '><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  filter: '<svg' + SVG_ATTRS + '><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
  eye: '<svg' + SVG_ATTRS + '><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  "eye-off": '<svg' + SVG_ATTRS + '><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
  "chevron-up": '<svg' + SVG_ATTRS + '><polyline points="18 15 12 9 6 15"/></svg>',
  "chevron-down": '<svg' + SVG_ATTRS + '><polyline points="6 9 12 15 18 9"/></svg>',
  "chevron-right": '<svg' + SVG_ATTRS + '><polyline points="9 18 15 12 9 6"/></svg>',
  stop: '<svg' + SVG_ATTRS + '><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
  download: '<svg' + SVG_ATTRS + '><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  upload: '<svg' + SVG_ATTRS + '><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  logout: '<svg' + SVG_ATTRS + '><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
};

const cache = {};
function getCached(path) {
  const e = cache[path];
  if (!e) return null;
  if (Date.now() - e.ts > CACHE_TTL_MS) { delete cache[path]; return null; }
  return e.data;
}
function setCached(path, data) { cache[path] = { data, ts: Date.now() }; }
function invalidateCache(path) { delete cache[path]; }
function clearCache() { Object.keys(cache).forEach(k => delete cache[k]); }
function forceRefresh(routeId) {
  clearCache();
  route(routeId || currentRoute);
}

async function http(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const isGet = method === "GET";
  const dedupKey = isGet ? method + " " + path : null;
  if (dedupKey && inflight.has(dedupKey)) {
    return inflight.get(dedupKey);
  }
  const run = (async () => {
    const attempt = async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
      try {
        const mergedHeaders = { "Content-Type": "application/json", ...(opts.headers || {}) };
        if (!isGet && !mergedHeaders[CSRF_HEADER]) {
          mergedHeaders[CSRF_HEADER] = "1";
        }
        const r = await fetch(path, {
          ...opts,
          headers: mergedHeaders,
          signal: controller.signal,
        });
        if (r.status === 401) {
          if (path.startsWith("/api/dashboard/")) {
            pendingRouteAfterLogin = currentRoute;
            showLogin(true);
          }
          const e = new Error("Your session expired. Please log in again.");
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
          e.message = "Request timed out. Check if the server is running.";
          e.transient = true;
          e.status = 0;
        } else if (e instanceof TypeError) {
          e.message = "Connection failed. The server is unreachable.";
          e.transient = true;
          e.status = 0;
        } else if (e.transient === undefined) {
          e.transient = false;
        }
        e.friendly = friendlyMsg(e);
        throw e;
      } finally {
        clearTimeout(timer);
      }
    };
    try {
      return await attempt();
    } catch (e) {
      if (e.transient && !opts._retried) {
        const gen = requestGen;
        await new Promise(r => setTimeout(r, RETRY_DELAY));
        if (gen !== requestGen) {
          const navErr = new Error("Request cancelled - you navigated away.");
          navErr.status = 0;
          navErr.transient = false;
          navErr.friendly = navErr.message;
          throw navErr;
        }
        return await http(path, { ...opts, _retried: true });
      }
      throw e;
    }
  })();
  if (dedupKey) {
    inflight.set(dedupKey, run);
    run.finally(() => inflight.delete(dedupKey));
  }
  return run;
}

async function parseResponse(r) {
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("text/plain")) return await r.text();
  if (ct.includes("application/json")) {
    const text = await r.text();
    if (!text) return null;
    return JSON.parse(text);
  }
  const text = await r.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

async function useFetch(path, opts = {}) {
  const cached = cache[path];
  if (cached && (Date.now() - cached.ts) < CACHE_FRESH_MS) {
    return { data: cached.data, error: null, loading: false, stale: false };
  }
  try {
    const r = await http(path, opts);
    const data = await parseResponse(r);
    setCached(path, data);
    hideConnectionLost();
    return { data, error: null, loading: false, stale: false };
  } catch (e) {
    const msg = friendlyMsg(e);
    const staleData = getCached(path);
    if (staleData) {
      return { data: staleData, error: msg, loading: false, stale: true };
    }
    if (e.status !== 401) showConnectionLost(msg);
    return { data: null, error: msg, loading: false, stale: false };
  }
}

function showLogin(sessionExpired) {
  if (loginVisible) return;
  loginVisible = true;
  clearPolls();
  const app = document.getElementById("app");
  if (!app) return;
  const notice = sessionExpired
    ? `<div class="inline-error session-notice">${ICONS.alert}<span>Your session expired. Please log in again.</span></div>`
    : "";
  app.innerHTML = `
    <div class="welcome">
      <div class="logo">P</div>
      <h1>PineTunnel Login</h1>
      <p>Send /login to your Telegram bot to get a one-time code</p>
      ${notice}
      <div class="login-form" id="login-form">
        <div class="field">
          <label for="login-code">Login code <span class="req">*</span></label>
          <input class="input" id="login-code" placeholder="Login code" autocomplete="one-time-code" spellcheck="false" inputmode="numeric" aria-required="true">
          <div class="hint">One-time code from your Telegram bot</div>
        </div>
        <div class="field">
          <label for="login-uid">Telegram user ID <span class="req">*</span></label>
          <input class="input" id="login-uid" type="number" placeholder="123456789" autocomplete="off" inputmode="numeric" aria-required="true">
          <div class="hint">Message @userinfobot to get your ID</div>
        </div>
        <button class="btn primary lg" id="login-submit" data-action="do-login">Login</button>
        <div id="login-error" aria-live="polite"></div>
      </div>
    </div>`;
  const btn = app.querySelector("[data-action='do-login']");
  const form = app.querySelector("#login-form");
  const codeInput = app.querySelector("#login-code");
  const uidInput = app.querySelector("#login-uid");
  if (btn) btn.addEventListener("click", e => { e.preventDefault(); doLogin(); });
  attachValidator(codeInput, "loginCode");
  attachValidator(uidInput, "loginUid");
  submitOnEnter(form, doLogin);
  autofocusFirst(form);
}

async function doLogin() {
  const codeEl = document.getElementById("login-code");
  const uidEl = document.getElementById("login-uid");
  const err = document.getElementById("login-error");
  const btn = document.getElementById("login-submit");
  const okCode = validateInput(codeEl, "loginCode");
  const okUid = validateInput(uidEl, "loginUid");
  if (!okCode || !okUid) return;
  setBtnLoading(btn, "Logging in...");
  err.innerHTML = "";
  try {
    const r = await http(`${API}/login`, { method: "POST", headers: jsonHeaders(true), body: JSON.stringify({ code: codeEl.value.trim(), user_id: parseInt(uidEl.value.trim(), 10) }) });
    if (r.ok) {
      loginVisible = false;
      toast("Logged in", "ok");
      const dest = pendingRouteAfterLogin || currentRoute || "overview";
      render();
      route(dest);
    }
  } catch (e) {
    setBtnError(btn, "Login");
    err.innerHTML = `<div class="inline-error">${escapeHtml(friendlyMsg(e))}</div>`;
  }
}

function showConnectionLost(msg) {
  if (loginVisible) return;
  if (!connectionLostVisible) {
    connectionLostVisible = true;
    let banner = document.getElementById("conn-lost");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "conn-lost";
      banner.className = "conn-banner";
      document.body.appendChild(banner);
    }
    banner.innerHTML = `<div class="conn-banner-inner">
      <span class="conn-spinner" aria-hidden="true"></span>
      <span class="conn-banner-msg">Connection lost. Reconnecting...</span>
      <span class="conn-banner-sub">${escapeHtml(msg || "The server is unreachable.")}</span>
      <button class="btn outline sm" data-action="retry-conn" type="button">Retry now</button>
    </div>`;
    const btn = banner.querySelector("[data-action='retry-conn']");
    if (btn) btn.addEventListener("click", e => { e.preventDefault(); retryLastRoute(); });
  } else {
    const banner = document.getElementById("conn-lost");
    if (banner) {
      const sub = banner.querySelector(".conn-banner-sub");
      if (sub && msg) sub.textContent = msg;
    }
  }
  scheduleConnRetry();
}

function scheduleConnRetry() {
  if (connRetryTimer) return;
  connRetryTimer = setTimeout(() => {
    connRetryTimer = null;
    retryLastRoute();
  }, connBackoff);
  connBackoff = Math.min(30000, Math.round(connBackoff * 1.6));
}

function hideConnectionLost() {
  connectionLostVisible = false;
  connBackoff = 5000;
  if (connRetryTimer) { clearTimeout(connRetryTimer); connRetryTimer = null; }
  const banner = document.getElementById("conn-lost");
  if (banner) banner.remove();
}

function retryLastRoute() {
  if (connRetryTimer) { clearTimeout(connRetryTimer); connRetryTimer = null; }
  hideConnectionLost();
  route(currentRoute);
}

function staleBanner() {
  return `<div class="stale-banner">${ICONS.alert}<span>Showing last known data - connection issue</span></div>`;
}

function errorPanel(name, msg, action) {
  return `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load ${escapeHtml(name)}</div><div class="sub">${escapeHtml(msg || "Server error or unreachable")}</div><button class="btn outline sm mt" data-action="${escapeHtml(action)}">${ICONS.refresh}Retry</button></div>`;
}

function partialWarning(label) {
  return `<div class="partial-warning">${ICONS.alert}<span>${escapeHtml(label)}</span></div>`;
}

function skeletonRow(cols) {
  return `<tr aria-hidden="true">${Array(cols).fill('<td><div class="skeleton line"></div></td>').join("")}</tr>`;
}

function skeletonTable(cols, rows) {
  return `<div class="table-wrap"><table class="data-table" aria-busy="true"><tbody>${Array(rows).fill(skeletonRow(cols)).join("")}</tbody></table></div>`;
}

function skeletonEaCards(n) {
  return Array(n).fill('<div class="ea-card" aria-hidden="true"><div class="ea-card-head"><div class="skeleton line short"></div></div><div class="ea-card-body"><div class="skeleton line"></div><div class="skeleton line"></div><div class="skeleton line short"></div></div></div>').join("");
}

function skeletonTimeline(n) {
  return Array(n).fill('<div class="tl-entry" aria-hidden="true"><div class="tl-head"><div class="skeleton line short"></div></div><div class="tl-meta"><div class="skeleton line"></div></div></div>').join("");
}

function bindRetryToScope(action, fn) {
  const el = document.querySelector(`[data-action="${action}"]`);
  if (el) el.addEventListener("click", e => { e.preventDefault(); fn(); });
}

function sigOf(obj) {
  try { return JSON.stringify(obj); } catch { return ""; }
}

window.addEventListener("unhandledrejection", e => {
  if (e.reason && (e.reason instanceof TypeError || e.reason.name === "AbortError")) {
    showConnectionLost(friendlyMsg(e.reason));
    e.preventDefault();
  }
});
window.addEventListener("error", e => {
  const msg = e.message || "Runtime error";
  if (/fetch|network|abort|timeout|load failed/i.test(msg)) showConnectionLost(msg);
});

let healthState = { data: null, error: null, stale: false };
let healthActive = false;
let healthPollStarted = false;

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
  if (pct == null || isNaN(pct)) return PALETTE.muted2;
  if (diskMode) {
    return pct < 70 ? PALETTE.green : pct <= 90 ? PALETTE.amber : PALETTE.red;
  }
  return pct < 50 ? PALETTE.green : pct <= 80 ? PALETTE.amber : PALETTE.red;
}

function svgGauge(value, label, opts = {}) {
  const size = Math.max(2, opts.size || 120);
  const stroke = Math.max(1, opts.stroke || 8);
  const r = Math.max(1, (size - stroke) / 2);
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
      <title>${label}: ${display}</title>
      <desc>${label} usage is ${display}</desc>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${PALETTE.gridTrack}" stroke-width="${stroke}"/>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}"
        stroke-dasharray="${dash} ${circumference}" stroke-linecap="round"
        transform="rotate(-90 ${cx} ${cy})" class="gauge-arc"/>
      <text x="${cx}" y="${cy - 2}" text-anchor="middle" class="gauge-value" fill="${color}">${display}</text>
      <text x="${cx}" y="${cy + 16}" text-anchor="middle" class="gauge-label" fill="${PALETTE.muted}">${label}</text>
    </svg>
  </div>`;
}

function updateGauge(cell, value, label, opts = {}) {
  if (!cell) return;
  const arc = cell.querySelector(".gauge-arc");
  if (!arc) { cell.innerHTML = svgGauge(value, label, opts); return; }
  const size = Math.max(2, opts.size || 120);
  const stroke = Math.max(1, opts.stroke || 8);
  const r = Math.max(1, (size - stroke) / 2);
  const circumference = 2 * Math.PI * r;
  const pct = (value != null && !isNaN(value)) ? Math.max(0, Math.min(100, value)) : null;
  const color = loadColorHex(pct, !!opts.diskMode);
  const display = (pct != null) ? `${pct.toFixed(0)}%` : "--";
  const dash = (pct != null) ? (pct / 100) * circumference : 0;
  arc.setAttribute("stroke-dasharray", `${dash} ${circumference}`);
  arc.setAttribute("stroke", color);
  const valEl = cell.querySelector(".gauge-value");
  if (valEl) { valEl.textContent = display; valEl.setAttribute("fill", color); }
}

function eaColor(n) {
  return n > 0 ? "ok" : "warn";
}

function getEaConnectionCount(c) {
  if (!c) return 0;
  const httpCount = c.http_polling_connections || 0;
  const ws = (c.websocket && c.websocket.websocket_connections) || 0;
  return httpCount + ws;
}

async function fetchHealth() {
  const prev = healthState.data;
  try {
    const hRes = await http("/api/system/health");
    const health = await hRes.json();
    let connections = null;
    let connError = null;
    let stats = null;
    try {
      const cRes = await http("/api/connections");
      connections = await cRes.json();
    } catch (ce) { connError = friendlyMsg(ce); }
    try {
      const sRes = await http("/api/system/stats");
      stats = await sRes.json();
    } catch (se) {}
    healthState = { data: { health, connections, stats }, error: null, stale: false, connError };
    hideConnectionLost();
  } catch (e) {
    healthState = { data: prev, error: friendlyMsg(e), stale: true };
    if (!prev && e.status !== 401) showConnectionLost(friendlyMsg(e));
  }
  if (healthActive) updateHealthCard();
}

function startHealthPolling() {
  if (!visibilityPolling) return;
  healthActive = true;
  if (healthPollStarted) return;
  healthPollStarted = true;
  startPoll(fetchHealth, POLL_INTERVAL);
}

function stopHealthPolling() {
  healthActive = false;
  healthPollStarted = false;
}

function setTile(id, value, cls) {
  const tile = document.getElementById(id);
  if (!tile) return;
  tile.className = `stat ${cls}`;
  const v = tile.querySelector(".value");
  if (v) {
    v.textContent = value;
    v.setAttribute("aria-live", "polite");
  }
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
      grid.innerHTML = errorBlock({
        title: "Could not load health metrics",
        detail: error,
        hint: "Check if the server is running and try again.",
        retryAction: "retry-overview",
        retryLabel: "Retry",
      });
      bindRetry(card, "retry-overview", () => route("overview"));
    }
    return;
  }
  if (!data) return;
  if (data.connError) {
    const existing = titleEl.querySelector(".partial-badge");
    if (!existing) {
      const b = document.createElement("span");
      b.className = "partial-badge";
      b.innerHTML = partialErrorBadge("EA count unavailable");
      titleEl.appendChild(b);
    }
  } else {
    const existing = titleEl.querySelector(".partial-badge");
    if (existing) existing.remove();
  }
  const h = data.health;
  const c = data.connections;
  const st = data.stats;
  const uptimeSec = h.uptime_seconds;
  const cpu = h.system ? h.system.cpu_percent : null;
  const mem = h.system ? h.system.memory_percent : null;
  let diskPct = null;
  if (h.system && h.system.disk_percent != null) diskPct = h.system.disk_percent;
  else if (h.disk && h.disk.used_percent != null) diskPct = h.disk.used_percent;
  let eaCount = 0;
  if (c) {
    eaCount = getEaConnectionCount(c);
  } else if (h.connections) {
    eaCount = h.connections.total_clients || 0;
  }
  const cpuCell = document.getElementById("gauge-cpu");
  const memCell = document.getElementById("gauge-mem");
  const diskCell = document.getElementById("gauge-disk");
  updateGauge(cpuCell, cpu, "CPU");
  updateGauge(memCell, mem, "RAM");
  updateGauge(diskCell, diskPct, "Disk", { diskMode: true });
  setTile("tile-uptime", formatUptime(uptimeSec), "ok");
  setTile("tile-ea", String(eaCount), eaColor(eaCount));
  const trades = (st && st.trades) || {};
  const sig = trades.today != null ? trades.today : null;
  const fill = trades.success_rate_7d != null ? trades.success_rate_7d : null;
  setTile("tile-signals", sig != null ? String(sig) : "--", sig != null && sig > 0 ? "ok" : "info");
  setTile("tile-fill", fill != null ? `${Number(fill).toFixed(1)}%` : "--", fill != null ? (fill >= 80 ? "ok" : fill >= 50 ? "warn" : "bad") : "info");
}

function clearPolls() {
  requestGen++;
  renderToken++;
  inflight.clear();
  Object.keys(panelStates).forEach(id => cleanupPanel(id));
  pollTimers.forEach(t => clearInterval(t));
  pollTimers = [];
  stopHealthPolling();
}

function toast(msg, type = "ok") {
  const container = ensureToastContainer();
  const isErr = type === "bad" || type === "error";
  const t = document.createElement("div");
  t.className = `toast ${isErr ? "bad" : "ok"}`;
  t.setAttribute("role", isErr ? "alert" : "status");
  t.setAttribute("aria-live", isErr ? "assertive" : "polite");
  t.innerHTML = `<span class="toast-msg">${escapeHtml(msg)}</span><button class="toast-close" aria-label="Dismiss">${ICONS.x}</button>`;
  t.addEventListener("click", () => dismissToast(t));
  container.appendChild(t);
  toastStack.push(t);
  while (toastStack.length > TOAST_MAX) {
    const old = toastStack.shift();
    if (old && old.parentNode) old.remove();
  }
  const ttl = isErr ? 5000 : 3000;
  const timer = setTimeout(() => dismissToast(t), ttl);
  t._timer = timer;
}

function ensureToastContainer() {
  let c = document.getElementById("toast-container");
  if (!c) {
    c = document.createElement("div");
    c.id = "toast-container";
    c.className = "toast-container";
    c.setAttribute("aria-live", "polite");
    document.body.appendChild(c);
  }
  return c;
}

function dismissToast(t) {
  if (!t || !t.parentNode) return;
  if (t._timer) { clearTimeout(t._timer); t._timer = null; }
  t.classList.add("toast-out");
  setTimeout(() => { if (t.parentNode) t.remove(); }, 250);
  const i = toastStack.indexOf(t);
  if (i >= 0) toastStack.splice(i, 1);
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
        <svg class="nav-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>
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
        <div class="footer"><svg class="pulse-dot" viewBox="0 0 8 8" aria-hidden="true"><circle cx="4" cy="4" r="3"/></svg><span>System Online - v1.0</span></div>
      </nav>
      <div class="main-area">
        <header class="header" role="banner">
          <h1 class="title" id="page-title">Overview</h1>
          <div class="actions" id="header-actions"></div>
        </header>
        <main class="content" id="content" tabindex="-1"></main>
      </div>
    </div>
    <nav class="mobile-nav" aria-label="Mobile navigation">
      ${mobilePrimaryItems}
      <button class="tab mobile-more" data-action="mobile-more" type="button" aria-haspopup="dialog" tabindex="0">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>
        <span>More</span>
      </button>
    </nav>
    <div class="mobile-sheet" id="mobile-sheet" role="dialog" aria-modal="true" aria-label="All panels">
      <div class="mobile-sheet-backdrop" data-action="close-sheet"></div>
      <div class="mobile-sheet-card">
        <div class="sheet-grab-handle" aria-hidden="true"></div>
        <div class="mobile-sheet-head">
          <h2 class="mobile-sheet-title">All Panels</h2>
          <button class="btn ghost sm" data-action="close-sheet" type="button" aria-label="Close">${ICONS.x}</button>
        </div>
        <div class="mobile-sheet-body">${mobileSheetItems}</div>
      </div>
    </div>
  `;
  const skipLink = document.querySelector(".skip-link");
  if (skipLink) {
    skipLink.addEventListener("click", e => {
      e.preventDefault();
      const target = document.getElementById("content");
      if (target) {
        target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: false });
      }
    });
  }
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

let _mobileSheetTrigger = null;
let _mobileSheetEscape = null;
let _mobileSheetTab = null;
function openMobileSheet() {
  const sheet = document.getElementById("mobile-sheet");
  if (!sheet) return;
  _mobileSheetTrigger = document.activeElement;
  sheet.classList.remove("sheet-closing");
  requestAnimationFrame(() => sheet.classList.add("sheet-open"));
  _mobileSheetEscape = e => { if (e.key === "Escape") { e.preventDefault(); closeMobileSheet(); } };
  _mobileSheetTab = e => {
    if (e.key !== "Tab") return;
    const focusable = _getFocusable(sheet);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first || !sheet.contains(document.activeElement)) { e.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last || !sheet.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", _mobileSheetEscape);
  document.addEventListener("keydown", _mobileSheetTab);
  const closeBtn = sheet.querySelector("[data-action='close-sheet']");
  if (closeBtn) setTimeout(() => closeBtn.focus(), 50);
}

function closeMobileSheet() {
  const sheet = document.getElementById("mobile-sheet");
  if (!sheet) return;
  sheet.classList.remove("sheet-open");
  sheet.classList.add("sheet-closing");
  if (_mobileSheetEscape) { document.removeEventListener("keydown", _mobileSheetEscape); _mobileSheetEscape = null; }
  if (_mobileSheetTab) { document.removeEventListener("keydown", _mobileSheetTab); _mobileSheetTab = null; }
  setTimeout(() => {
    sheet.classList.remove("sheet-closing");
    if (_mobileSheetTrigger) { try { _mobileSheetTrigger.focus(); } catch {} _mobileSheetTrigger = null; }
  }, 200);
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
    const content = document.getElementById("content");
    const actions = document.getElementById("header-actions");
    const renderPanel = () => {
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
      if (content && !prefersReducedMotion()) {
        content.classList.remove("panel-fade-in");
        void content.offsetWidth;
        content.classList.add("panel-fade-in");
      }
      const pageTitle = document.getElementById("page-title");
      if (pageTitle) {
        pageTitle.setAttribute("tabindex", "-1");
        pageTitle.focus({ preventScroll: true });
      }
    };
    if (content && !prefersReducedMotion() && content.innerHTML.trim()) {
      content.classList.remove("panel-fade-in");
      content.classList.add("panel-fade-out");
      setTimeout(() => {
        content.classList.remove("panel-fade-out");
        renderPanel();
      }, 100);
    } else {
      content.classList.remove("panel-fade-out", "panel-fade-in");
      renderPanel();
    }
  }, 100);
}

function skeletonCard(cols = 3) {
  return `<div class="card"><div class="grid grid-${cols}">${Array(cols).fill('<div><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}</div></div>`;
}

function badge(state, text, pulse = false) {
  const cls = state === "ok" ? "ok" : state === "bad" ? "bad" : state === "warn" ? "warn" : "info";
  return `<span class="badge ${cls} ${pulse ? "pulse" : ""}"><span class="dot"></span>${text}</span>`;
}

function pollOverview() {
  if (currentRoute !== "overview" || !visibilityPolling) return;
  useFetch(`${API}/setup-status`).then(({ data }) => {
    if (!data) return;
    const sig = JSON.stringify(data);
    if (sig !== lastSetupStatus) {
      lastSetupStatus = sig;
      const content = document.getElementById("content");
      const actions = document.getElementById("header-actions");
      if (content && actions) renderOverview(content, actions);
    }
  });
}
function startOverviewPoll() {
  if (!visibilityPolling) return;
  addPoll(setInterval(pollOverview, POLL_INTERVAL));
}

async function renderOverview(content, actions) {
  const tk = renderToken;
  content.innerHTML = `
    <div class="card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
    <div class="card"><h2 class="card-title" aria-hidden="true"><div class="skeleton line short"></div></h2><div class="grid grid-3">${Array(3).fill('<div class="stat"><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}</div></div>
    <div class="card"><h2 class="card-title" aria-hidden="true"><div class="skeleton line short"></div></h2><div class="grid grid-3">${Array(3).fill('<div><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}</div></div>
  `;
  const { data, error, stale } = await useFetch(`${API}/setup-status`);
  if (staleRender(tk)) return;
  if (error && !data) {
    content.innerHTML = errorPanel("overview", error, "retry-overview");
    bindRetryToScope("retry-overview", () => route("overview"));
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
      <h2 class="card-title">Your TradingView Webhook URL</h2>
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
              <label for="ov-test-symbol">Symbol <span class="req">*</span></label>
              <input class="input" id="ov-test-symbol" value="EURUSD" placeholder="EURUSD" maxlength="12" inputmode="text" spellcheck="false" autocomplete="off" aria-required="true">
              <div class="hint">Uppercase letters, e.g. EURUSD</div>
            </div>
            <div class="field">
              <label for="ov-test-action">Action <span class="req">*</span></label>
              <select class="input" id="ov-test-action" aria-required="true">
                <option value="buy">buy</option>
                <option value="sell">sell</option>
                <option value="close_long">close_long</option>
                <option value="close_short">close_short</option>
                <option value="close_all">close_all</option>
              </select>
            </div>
            <div class="field">
              <label for="ov-test-lots">Lots <span class="req">*</span></label>
              <input class="input" id="ov-test-lots" value="0.10" placeholder="0.10" type="number" min="0.01" step="0.01" inputmode="decimal" aria-required="true">
              <div class="hint">Positive number, e.g. 0.10</div>
            </div>
          </div>
          <div class="grid grid-2">
            <div class="field">
              <label for="ov-test-sl">Stop Loss <span class="opt">(optional)</span></label>
              <input class="input" id="ov-test-sl" placeholder="e.g. 1.0850" type="number" min="0" step="0.00001" inputmode="decimal">
            </div>
            <div class="field">
              <label for="ov-test-tp">Take Profit <span class="opt">(optional)</span></label>
              <input class="input" id="ov-test-tp" placeholder="e.g. 1.0950" type="number" min="0" step="0.00001" inputmode="decimal">
            </div>
          </div>
          <div class="webhook-test-actions">
            <button class="btn primary" id="ov-test-send" data-action="send-test">${ICONS.check}Send Test Signal</button>
            <button class="btn outline" id="ov-test-cancel" data-action="cancel-test">Cancel</button>
          </div>
          <div id="ov-test-result" aria-live="polite"></div>
        </div>
      </div>
      <div class="ea-verify-section mt">
        <button class="btn outline" id="ov-verify-ea" data-action="verify-ea">${ICONS.health}Verify EA Connection</button>
        <div id="ov-ea-status" aria-live="polite"></div>
      </div>
    </div>` : "";

  const setupBlock = !allDone ? `
    <div class="card">
      <h2 class="card-title">Get Started</h2>
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
      <h2 class="card-title">System Status</h2>
      <div class="card-desc">Current configuration state</div>
      <div class="grid grid-3">
        <div class="stat ${tg ? "ok" : "info"} clickable" data-action="goto-setup" tabindex="0" role="button" aria-label="Telegram Bot status - go to Setup">
          <div class="value">${tg ? "Configured" : "Pending"}</div>
          <div class="label">Telegram Bot</div>
          ${tgHint}
        </div>
        <div class="stat ${cf ? "ok" : "info"} clickable" data-action="goto-setup" tabindex="0" role="button" aria-label="Cloudflare Tunnel status - go to Setup">
          <div class="value">${cf ? "Connected" : "Pending"}</div>
          <div class="label">Cloudflare Tunnel</div>
          ${cfHint}
        </div>
        <div class="stat ${init ? "ok" : "warn"}" role="group" aria-label="Initialized: ${init ? "Yes" : "No"}">
          <div class="value">${init ? "Yes" : "No"}</div>
          <div class="label">Initialized</div>
          ${initHint}
        </div>
      </div>
    </div>
    <div class="card" id="health-card">
      <h2 class="card-title">Server Health${healthState.error && !healthState.data ? ` <span class="partial-badge">${ICONS.alert}Health unavailable</span>` : ""}</h2>
      <div class="card-desc">Live system metrics - refreshes every 10s</div>
      <div class="gauge-row" id="health-grid" role="group" aria-label="Server health gauges">
        <div class="gauge-cell" id="gauge-cpu">${svgGauge(null, "CPU")}</div>
        <div class="gauge-cell" id="gauge-mem">${svgGauge(null, "RAM")}</div>
        <div class="gauge-cell" id="gauge-disk">${svgGauge(null, "Disk", { diskMode: true })}</div>
        <div class="stat-pair" role="group" aria-label="Server health stats">
          <div class="stat" id="tile-uptime" role="group" aria-label="Uptime"><div class="value skeleton line" aria-live="polite"></div><div class="label">Uptime</div></div>
          <div class="stat" id="tile-ea" role="group" aria-label="EA Connections"><div class="value skeleton line" aria-live="polite"></div><div class="label">EA Connections</div></div>
          <div class="stat" id="tile-signals" role="group" aria-label="Signals Today"><div class="value skeleton line" aria-live="polite"></div><div class="label">Signals Today</div></div>
          <div class="stat" id="tile-fill" role="group" aria-label="Fill Rate"><div class="value skeleton line" aria-live="polite"></div><div class="label">Fill Rate</div></div>
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
  const cancelBtn = scope.querySelector("[data-action='cancel-test']");
  if (cancelBtn) cancelBtn.addEventListener("click", e => { e.preventDefault(); toggleTestForm(false); });
  const verifyBtn = scope.querySelector("[data-action='verify-ea']");
  if (verifyBtn) verifyBtn.addEventListener("click", e => { e.preventDefault(); verifyEaConnection(); });
  const viewFeedBtn = scope.querySelector("[data-action='view-feed']");
  if (viewFeedBtn) viewFeedBtn.addEventListener("click", e => { e.preventDefault(); route("signals"); });
  const symInput = scope.querySelector("#ov-test-symbol");
  if (symInput) symInput.addEventListener("input", e => {
    const v = e.target.value.toUpperCase().replace(/[^A-Z0-9.]/g, "");
    e.target.value = v;
  });
}

function setupStepState(data) {
  const tg = data?.telegram_configured;
  const cf = data?.cloudflare_configured;
  if (!tg) return { step: 1, tg, cf };
  if (!cf) return { step: 2, tg, cf };
  return { step: 3, tg, cf };
}

async function renderSetup(content) {
  const tk = renderToken;
  content.innerHTML = skeletonCard(1);
  const { data, error, stale } = await useFetch(`${API}/setup-status`);
  if (staleRender(tk)) return;
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
      <h2 class="card-title">Telegram Bot</h2>
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
          <label for="tg-token">2. Paste bot token <span class="req">*</span></label>
          <input class="input" id="tg-token" type="password" placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" autocomplete="off" spellcheck="false" aria-required="true">
          <div class="hint">From @BotFather - format: 123456789:AA... (35 char secret)</div>
        </div>
        <div class="field">
          <label for="tg-uid">3. Get your Telegram user ID <span class="req">*</span></label>
          <input class="input" id="tg-uid" type="number" placeholder="123456789" autocomplete="off" inputmode="numeric" aria-required="true">
          <div class="hint">Message @userinfobot on Telegram, it replies with your numeric ID</div>
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
  const tokenInput = body.querySelector("#tg-token");
  const uidInput = body.querySelector("#tg-uid");
  if (tokenInput) {
    attachValidator(tokenInput, "tgToken");
    addPasswordToggle(tokenInput);
    tokenInput.addEventListener("input", () => { setupDirty = true; });
  }
  if (uidInput) {
    attachValidator(uidInput, "tgUid");
    uidInput.addEventListener("input", () => { setupDirty = true; });
  }
  if (saveBtn) submitOnEnter(body.querySelector(".card"), saveTelegram);
  if (!tg) autofocusFirst(body);
}

function renderStep2(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <h2 class="card-title">Cloudflare Tunnel</h2>
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
          <label for="cf-token">Option B: Tunnel token <span class="opt">(optional)</span></label>
          <input class="input" id="cf-token" type="password" placeholder="eyJ..." autocomplete="off" spellcheck="false" disabled>
          <div class="hint">Must start with eyJ - from Cloudflare Tunnel dashboard</div>
        </div>
        <div class="field">
          <label for="cf-url">Tunnel URL <span class="opt">(optional)</span></label>
          <input class="input" id="cf-url" type="url" placeholder="https://pinetunnel.example.com" autocomplete="off" spellcheck="false" disabled>
          <div class="hint">Must start with https://</div>
        </div>
        <button class="btn outline" disabled>${ICONS.external}Connect (Phase 2)</button>
        <div id="cf-result" aria-live="polite"></div>
      `}
    </div>
  `;
  const adv = body.querySelector("[data-action='advance-3']");
  if (adv) adv.addEventListener("click", e => { e.preventDefault(); advanceStep(3); });
  const cfToken = body.querySelector("#cf-token");
  const cfUrl = body.querySelector("#cf-url");
  if (cfToken && !cfToken.disabled) {
    attachValidator(cfToken, "cfToken");
    addPasswordToggle(cfToken);
  }
  if (cfUrl && !cfUrl.disabled) attachValidator(cfUrl, "cfUrl");
  if (!cf) autofocusFirst(body);
}

async function renderStep3(body, data) {
  const cf = data?.cloudflare_configured;
  body.innerHTML = `
    <div class="card">
      <h2 class="card-title">TradingView Webhook</h2>
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
  invalidateCache(`${API}/setup-status`);
  const { data } = await useFetch(`${API}/setup-status`);
  renderSetupStep(content, step, data);
}

async function saveTelegram() {
  const btn = document.getElementById("save-tg");
  const tokenEl = document.getElementById("tg-token");
  const uidEl = document.getElementById("tg-uid");
  const result = document.getElementById("tg-result");
  const okToken = validateInput(tokenEl, "tgToken");
  const okUid = validateInput(uidEl, "tgUid");
  if (!okToken || !okUid) return;
  const token = tokenEl.value.trim();
  const uid = uidEl.value.trim();
  setBtnLoading(btn, "Saving...");
  result.innerHTML = "";
  try {
    const r = await http(`${API}/config`, {
      method: "PUT",
      headers: jsonHeaders(true),
      body: JSON.stringify({ updates: { TELEGRAM_BOT_TOKEN: token, TELEGRAM_ADMIN_IDS: uid } }),
    });
    setBtnSuccess(btn, "Saved", 2000);
    setupDirty = false;
    invalidateCache(`${API}/setup-status`);
    invalidateCache(`${API}/config`);
    if (r.needs_restart) {
      result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Restart the server for the bot to pick up the new token.</div>`;
      toast("Telegram saved - restart required", "ok");
    } else {
      result.innerHTML = `<div class="inline-ok">${ICONS.check}Saved. Send /login to your bot to test.</div>`;
      toast("Telegram configured", "ok");
    }
    setTimeout(() => advanceStep(2), 2000);
  } catch (e) {
    setBtnError(btn, "Failed");
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

function toggleTestForm(force) {
  const form = document.getElementById("ov-test-form");
  if (!form) return;
  const result = document.getElementById("ov-test-result");
  if (result) result.innerHTML = "";
  if (force === false) {
    form.classList.add("hidden");
  } else if (force === true) {
    form.classList.remove("hidden");
  } else {
    form.classList.toggle("hidden");
  }
  if (!form.classList.contains("hidden")) {
    const sym = form.querySelector("#ov-test-symbol");
    const lots = form.querySelector("#ov-test-lots");
    if (sym && !sym.dataset.validator) { sym.dataset.validator = "1"; attachValidator(sym, "symbol"); }
    if (lots && !lots.dataset.validator) { lots.dataset.validator = "1"; attachValidator(lots, "lots"); }
    submitOnEnter(form, sendTestWebhook);
    autofocusFirst(form);
  }
}

async function sendTestWebhook() {
  const btn = document.getElementById("ov-test-send");
  const result = document.getElementById("ov-test-result");
  if (!btn || !result) return;
  const symEl = document.getElementById("ov-test-symbol");
  const lotsEl = document.getElementById("ov-test-lots");
  const okSym = validateInput(symEl, "symbol");
  const okLots = validateInput(lotsEl, "lots");
  if (!okSym || !okLots) return;
  const symbol = symEl.value.trim().toUpperCase();
  const action = document.getElementById("ov-test-action").value;
  const lots = parseFloat(lotsEl.value.trim());
  const slVal = document.getElementById("ov-test-sl").value.trim();
  const tpVal = document.getElementById("ov-test-tp").value.trim();
  const body = { symbol, action, lots: String(lots) };
  if (slVal) body.sl = slVal;
  if (tpVal) body.tp = tpVal;
  setBtnLoading(btn, "Sending...");
  result.innerHTML = "";
  const t0 = Date.now();
  try {
    const r = await http(`${API}/test-webhook`, {
      method: "POST",
      headers: jsonHeaders(true),
      body: JSON.stringify(body),
    });
    const data = await r.json();
    const clientLatency = Date.now() - t0;
    if (data.status === "sent") {
      const ok = data.response_code >= 200 && data.response_code < 300;
      const serverLat = data.latency_ms != null ? data.latency_ms : clientLatency;
      if (ok) {
        toast("Signal sent - check Signal Feed", "ok");
      } else {
        toast("Webhook returned error", "bad");
      }
      result.innerHTML = `<div class="inline-${ok ? "ok" : "error"}">${ok ? ICONS.check : ICONS.x}HTTP ${escapeHtml(String(data.response_code))} - ${ok ? "Signal delivered" : "Webhook returned error"} (${escapeHtml(String(serverLat))}ms)</div>
        <div class="hint mt">Response: ${escapeHtml(data.response_body || "")}</div>
        ${ok ? `<div class="mt"><button class="btn outline sm" data-action="view-feed">${ICONS.feed}View in Signal Feed</button></div>` : ""}`;
      const vfBtn = result.querySelector("[data-action='view-feed']");
      if (vfBtn) vfBtn.addEventListener("click", e => { e.preventDefault(); route("signals"); });
    } else {
      toast("Test failed", "bad");
      result.innerHTML = `<div class="inline-error">${ICONS.x}${escapeHtml(data.message || "Test failed")}</div>`;
    }
  } catch (e) {
    toast("Request failed", "bad");
    result.innerHTML = `<div class="inline-error">${ICONS.x}Request failed: ${escapeHtml(e.message || friendlyMsg(e))}</div>`;
  }
  btn.disabled = false;
  btn.innerHTML = btn.dataset.origHtml || btn.innerHTML;
}

let eaVerifyTimer = null;
let eaVerifyStart = 0;
const EA_VERIFY_DURATION = 60000;
const EA_VERIFY_INTERVAL = 5000;

async function verifyEaConnection() {
  const statusEl = document.getElementById("ov-ea-status");
  const btn = document.getElementById("ov-verify-ea");
  if (!statusEl || !btn) return;
  if (eaVerifyTimer) {
    clearInterval(eaVerifyTimer);
    eaVerifyTimer = null;
  }
  btn.disabled = true;
  const original = btn.innerHTML;
  btn.innerHTML = `<span class="spin"></span>Verifying...`;
  eaVerifyStart = Date.now();
  await pollEaVerify();
  eaVerifyTimer = setInterval(async () => {
    if (Date.now() - eaVerifyStart >= EA_VERIFY_DURATION) {
      clearInterval(eaVerifyTimer);
      eaVerifyTimer = null;
      btn.disabled = false;
      btn.innerHTML = original;
      return;
    }
    await pollEaVerify();
  }, EA_VERIFY_INTERVAL);
}

async function pollEaVerify() {
  const statusEl = document.getElementById("ov-ea-status");
  const btn = document.getElementById("ov-verify-ea");
  if (!statusEl) return;
  try {
    const r = await http("/api/connections");
    const c = await r.json();
    const total = getEaConnectionCount(c);
    const elapsed = Math.round((Date.now() - eaVerifyStart) / 1000);
    if (total > 0) {
      statusEl.innerHTML = `<div class="inline-ok">${ICONS.check}<span>EA Connected (${total} connection${total > 1 ? "s" : ""})</span></div>`;
      if (eaVerifyTimer) {
        clearInterval(eaVerifyTimer);
        eaVerifyTimer = null;
        if (btn) { btn.disabled = false; btn.innerHTML = `${ICONS.health}Verify EA Connection`; }
      }
    } else {
      const remaining = Math.max(0, Math.round((EA_VERIFY_DURATION - (Date.now() - eaVerifyStart)) / 1000));
      statusEl.innerHTML = `<div class="inline-error">${ICONS.alert}<span>No EA connected - waiting (${remaining}s left)</span></div>
        <div class="hint mt">1. Download the EA from the Setup panel<br>2. Attach it to a chart in MetaTrader<br>3. Enter your license key and server URL in the EA inputs<br>4. Enable DLL imports in MetaTrader settings</div>`;
    }
  } catch (e) {
    statusEl.innerHTML = `<div class="inline-error">${ICONS.x}Failed to check connections: ${escapeHtml(e.message || friendlyMsg(e))}</div>`;
  }
}

async function renderSettings(content) {
  const tk = renderToken;
  content.innerHTML = skeletonCard(1);
  const { data, error, stale } = await useFetch(`${API}/config`);
  if (staleRender(tk)) return;
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load settings</div><button class="btn outline sm mt" data-action="retry-settings">Retry</button></div>`;
    bindRetry(content, "retry-settings", () => route("settings"));
    return;
  }
  const entries = Object.entries(data).filter(([k]) => !k.startsWith("#") && k);
  content.innerHTML = `
    ${stale ? staleBanner() : ""}
    <div class="card">
      <h2 class="card-title">Configuration</h2>
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

function formatTime(iso) {
  return relativeTime(iso);
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
  _renderedOnce: false,
};

function renderSignalFeed(content, actions) {
  const ps = getPanelState("signals");
  signalFeedState = {
    rows: [],
    paused: false,
    filterLicense: ps.filters.filterLicense || "",
    filterSymbol: ps.filters.filterSymbol || "",
    filterStatus: ps.filters.filterStatus || "",
    seenIds: new Set(),
    _renderedOnce: false,
  };
  content.innerHTML = `
    <div class="card">
      <h2 class="card-title">Live Signal Feed</h2>
      <div class="card-desc">Real-time webhook signals - polling every 5s</div>
      <div class="feed-toolbar">
        <div class="filter-bar">
          <select class="input filter-sel" id="feed-filter-license" aria-label="Filter by license"><option value="">All licenses</option></select>
          <input class="input filter-txt" id="feed-filter-symbol" placeholder="Symbol filter" aria-label="Filter by symbol" value="${escapeHtml(signalFeedState.filterSymbol)}">
          <select class="input filter-sel" id="feed-filter-status" aria-label="Filter by status">
            <option value="">All status</option>
            <option value="success" ${signalFeedState.filterStatus === "success" ? "selected" : ""}>Executed</option>
            <option value="pending" ${signalFeedState.filterStatus === "pending" ? "selected" : ""}>Pending</option>
            <option value="failed" ${signalFeedState.filterStatus === "failed" ? "selected" : ""}>Failed</option>
            <option value="duplicate" ${signalFeedState.filterStatus === "duplicate" ? "selected" : ""}>Duplicate</option>
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
            ${Array(5).fill(skeletonRow(7)).join("")}
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
  licenseSel.addEventListener("change", () => { signalFeedState.filterLicense = licenseSel.value; setPanelState("signals", { filters: { filterLicense: licenseSel.value } }); renderFeedRows(); });
  symbolInput.addEventListener("input", () => { signalFeedState.filterSymbol = symbolInput.value.trim().toUpperCase(); setPanelState("signals", { filters: { filterSymbol: symbolInput.value.trim().toUpperCase() } }); renderFeedRows(); });
  statusSel.addEventListener("change", () => { signalFeedState.filterStatus = statusSel.value; setPanelState("signals", { filters: { filterStatus: statusSel.value } }); renderFeedRows(); });
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
  startPoll(pollSignalFeed, 5000);
}

async function pollSignalFeed() {
  if (currentRoute !== "signals" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/api/webhooks/recent?limit=50");
  const staleEl = document.getElementById("feed-stale-banner");
  if (staleEl) staleEl.remove();
  if (error && !data) {
    const body = document.getElementById("feed-body");
    if (body) body.innerHTML = `<tr><td colspan="7" class="feed-empty feed-error">${errorPanel("signal feed", error, "retry-signals")}</td></tr>`;
    bindRetryToScope("retry-signals", () => route("signals"));
    return;
  }
  if (stale && data) {
    const card = document.querySelector("#feed-scroll");
    if (card && !document.getElementById("feed-stale-banner")) {
      const banner = document.createElement("div");
      banner.id = "feed-stale-banner";
      banner.innerHTML = staleBanner();
      card.parentNode.insertBefore(banner, card);
    }
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
  const hasFilters = signalFeedState.filterLicense || signalFeedState.filterSymbol || signalFeedState.filterStatus;
  if (newRows.length > 0 && !hasFilters) {
    const body = document.getElementById("feed-body");
    if (body) {
      const emptyRow = body.querySelector(".feed-empty");
      if (emptyRow) emptyRow.closest("tr").remove();
      const html = newRows.map(feedRowHtml).join("");
      body.insertAdjacentHTML("afterbegin", html);
      const countEl = document.getElementById("feed-count");
      if (countEl) countEl.textContent = `${signalFeedState.rows.length} signals`;
      if (!signalFeedState.paused) {
        const scroll = document.getElementById("feed-scroll");
        if (scroll) scroll.scrollTop = 0;
      }
      return;
    }
  }
  if (newRows.length > 0 || !signalFeedState._renderedOnce) {
    renderFeedRows();
    signalFeedState._renderedOnce = true;
  }
}

function feedRowHtml(r) {
    const ts = r.timestamp ? formatTime(r.timestamp) : "--";
  const lk = r.payload && r.payload.license_key ? maskKey(r.payload.license_key) : (r.ip_address || "--");
  const action = r.action || "--";
  const sym = r.symbol || "--";
  const lots = r.volume != null ? r.volume : "--";
  const status = r.status || "--";
  const cls = statusClassFor(status);
  const lat = r.execution_time_ms != null ? `${r.execution_time_ms}ms` : "--";
  const actionCls = action === "buy" ? "act-buy" : action === "sell" ? "act-sell" : action === "close" || action === "close_all" ? "act-close" : "act-other";
  return `<tr class="row-${cls}">
    <td class="td-time" scope="row">${escapeHtml(ts)}</td>
    <td class="td-key">${escapeHtml(lk)}</td>
    <td><span class="action-tag ${actionCls}">${escapeHtml(action)}</span></td>
    <td>${escapeHtml(sym)}</td>
    <td>${escapeHtml(String(lots))}</td>
    <td><span class="status-tag ${cls}">${escapeHtml(status)}</span></td>
    <td class="td-lat">${escapeHtml(lat)}</td>
  </tr>`;
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
    const isEmpty = rows.length === 0;
    body.innerHTML = `<tr><td colspan="7" class="feed-empty">${isEmpty ? emptyState(ICONS.signals, "No signals received yet. Send a test webhook to see it here.", "Send test webhook", "empty-test-webhook") : "No signals match filters"}</td></tr>`;
    if (isEmpty) {
      const btn = body.querySelector("[data-action='empty-test-webhook']");
      if (btn) btn.addEventListener("click", e => { e.preventDefault(); route("overview"); });
    }
    return;
  }
  body.innerHTML = filtered.map(feedRowHtml).join("");
  if (!signalFeedState.paused) {
    const scroll = document.getElementById("feed-scroll");
    if (scroll) scroll.scrollTop = 0;
  }
}

let eaMapState = { expanded: null };

function renderEaMap(content, actions) {
  content.innerHTML = `
    <div id="ea-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">EA Connections</h2>
      <div class="card-desc">Connected EAs with live telemetry - polling every 10s</div>
      <div class="ea-grid" id="ea-grid">
        ${skeletonEaCards(3)}
      </div>
    </div>
    <div id="ea-expand-container"></div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  startPoll(pollEaMap, 10000);
}

async function pollEaMap() {
  if (currentRoute !== "ea-map" || !visibilityPolling) return;
  const [overviewRes, eaCheckRes] = await Promise.all([
    useFetch("/api/ea/ws-telemetry/overview").catch(() => ({ data: null, error: "telemetry", stale: false })),
    useFetch("/health/ea-check").catch(() => ({ data: null, error: "ea-check", stale: false })),
  ]);
  const overview = overviewRes.data;
  const eaCheck = eaCheckRes.data;
  const grid = document.getElementById("ea-grid");
  if (!grid) return;
  const staleBannerEl = document.getElementById("ea-stale-banner");
  const partials = [];
  if (overviewRes.error && !overview) partials.push("telemetry");
  if (eaCheckRes.error && !eaCheck) partials.push("EA health check");
  if (staleBannerEl) {
    if ((overviewRes.stale || eaCheckRes.stale) && (overview || eaCheck)) {
      staleBannerEl.innerHTML = staleBanner();
    } else if (partials.length > 0 && (overview || eaCheck)) {
      staleBannerEl.innerHTML = partialWarning(partials.join(" and ") + " unavailable");
    } else {
      staleBannerEl.innerHTML = "";
    }
  }
  if (!overview && !eaCheck) {
    grid.innerHTML = errorPanel("EA connections", (overviewRes.error || "") + (eaCheckRes.error || ""), "retry-ea");
    bindRetryToScope("retry-ea", () => pollEaMap());
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
    grid.innerHTML = emptyState(ICONS.map, "No EAs connected. Install the EA on your MetaTrader to get started.", "Go to Setup", "empty-goto-setup");
    const btn = grid.querySelector("[data-action='empty-goto-setup']");
    if (btn) btn.addEventListener("click", e => { e.preventDefault(); route("setup"); });
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
  container.innerHTML = `<div class="card"><h2 class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</h2><div class="card-desc">Loading trade history...</div></div>`;
  try {
    const r = await http(`/api/ea/ws-telemetry/trade-history/${encodeURIComponent(key)}`);
    const data = await r.json();
    const trades = data.deals || data.trades || [];
    if (trades.length === 0) {
      container.innerHTML = `<div class="card"><h2 class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</h2>${emptyState(ICONS.analytics, "No trades yet. Signals will appear here after the first execution.")}</div>`;
      return;
    }
    container.innerHTML = `<div class="card">
      <h2 class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</h2>
      <div class="card-desc">${trades.length} recent trades</div>
      <div class="feed-scroll">
        <table class="feed-table" aria-label="Recent trades for ${escapeHtml(maskKey(key))}">
          <caption class="sr-only">Recent trades</caption>
          <thead><tr><th scope="col">Time</th><th scope="col">Symbol</th><th scope="col">Type</th><th scope="col">Lots</th><th scope="col">Ticket</th><th scope="col">Profit</th></tr></thead>
          <tbody>
            ${trades.map(t => {
              const ts = t.close_time || t.open_time || t.timestamp;
              const tsStr = ts ? formatTime(ts) : "--";
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
    container.innerHTML = `<div class="card"><h2 class="card-title">Recent Trades - ${escapeHtml(maskKey(key))}</h2><div class="ea-empty">Failed to load: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderTradeAnalytics(content, actions) {
  content.innerHTML = `
    <div id="analytics-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Trade Analytics</h2>
      <div class="card-desc">Performance overview - polling every 15s</div>
      <div class="grid grid-4" id="analytics-stats" role="group" aria-label="Trade analytics stats">
        <div class="stat" id="stat-total" role="group" aria-label="Total Trades"><div class="value skeleton line" aria-live="polite"></div><div class="label">Total Trades</div></div>
        <div class="stat" id="stat-winrate" role="group" aria-label="Win Rate"><div class="value skeleton line" aria-live="polite"></div><div class="label">Win Rate</div></div>
        <div class="stat" id="stat-latency" role="group" aria-label="Avg Latency"><div class="value skeleton line" aria-live="polite"></div><div class="label">Avg Latency</div></div>
        <div class="stat" id="stat-pf" role="group" aria-label="Profit Factor"><div class="value skeleton line" aria-live="polite"></div><div class="label">Profit Factor</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Trades by Hour (last 24h)</h2>
      <div class="card-desc">Hourly trade volume distribution</div>
      <div id="bar-chart-wrap" class="chart-wrap"></div>
    </div>
    <div class="card">
      <h2 class="card-title">7-Day Success Rate Trend</h2>
      <div class="card-desc">Daily success rate over the past week</div>
      <div id="line-chart-wrap" class="chart-wrap"></div>
    </div>
    <div class="card">
      <h2 class="card-title">Top Symbols by Volume</h2>
      <div class="card-desc">Top 5 symbols by trade volume</div>
      <div id="donut-chart-wrap" class="chart-wrap"></div>
    </div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 15s</span>`;
  analyticsChartsAnimated = false;
  startPoll(pollTradeAnalytics, 15000);
}

async function pollTradeAnalytics() {
  if (currentRoute !== "analytics" || !visibilityPolling) return;
  const [statsRes, dashRes] = await Promise.all([
    useFetch("/api/statistics?days=7").catch(() => ({ data: null, error: "stats", stale: false })),
    useFetch("/api/trades/admin/dashboard").catch(() => ({ data: null, error: "dashboard", stale: false })),
  ]);
  const stats = statsRes.data;
  const dash = dashRes.data;
  const staleEl = document.getElementById("analytics-stale-banner");
  if (staleEl) {
    const partials = [];
    if (statsRes.error && !stats) partials.push("statistics");
    if (dashRes.error && !dash) partials.push("dashboard");
    if ((statsRes.stale || dashRes.stale) && (stats || dash)) {
      staleEl.innerHTML = staleBanner();
    } else if (partials.length > 0 && (stats || dash)) {
      staleEl.innerHTML = partialWarning(partials.join(" and ") + " unavailable");
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (!stats && !dash) {
    const content = document.getElementById("content");
    if (content) {
      content.innerHTML = errorPanel("analytics", "Statistics and dashboard endpoints unavailable", "retry-analytics");
      bindRetryToScope("retry-analytics", () => route("analytics"));
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
  if (totalTrades === 0 && !stats) {
    const content = document.getElementById("content");
    if (content) {
      const staleHtml = (statsRes.stale || dashRes.stale) ? staleBanner() : "";
      content.innerHTML = `${staleHtml}${emptyState(ICONS.analytics, "No trades yet. Signals will appear here after the first execution.")}`;
    }
    return;
  }
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
  const totalTrades = hours.reduce((s, h) => s + h.count, 0);
  const peakHour = hours.reduce((a, h) => h.count > a.count ? h : a, { hour: 0, count: 0 });
  const W = 600, H = 200, pad = 30, barW = (W - pad * 2) / 24;
  const bars = hours.map((h, i) => {
    const bh = (h.count / maxVal) * (H - pad * 2);
    const x = pad + i * barW;
    const y = H - pad - bh;
    return `<rect x="${x + 1}" y="${y}" width="${Math.max(0, barW - 2)}" height="${Math.max(0, bh)}" fill="${PALETTE.blue}" rx="2" class="bar-rect">
      <title>${h.hour}:00 - ${h.count} trades</title>
    </rect>`;
  }).join("");
  const labels = hours.filter((_, i) => i % 3 === 0).map(h => {
    const x = pad + h.hour * barW + barW / 2;
    return `<text x="${x}" y="${H - pad + 14}" text-anchor="middle" fill="${PALETTE.muted2}" font-size="10">${h.hour}</text>`;
  }).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg${analyticsChartsAnimated ? "" : " chart-entrance"}" role="img" aria-label="Trades by hour: ${totalTrades} trades today, peak at ${peakHour.hour}:00 with ${peakHour.count} trades">
    <title>Trades by hour - ${totalTrades} total trades today</title>
    <desc>Hourly trade volume for the last 24 hours. Peak hour: ${peakHour.hour}:00 with ${peakHour.count} trades.</desc>
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
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
  const dots = coords.map((c, i) => `<circle cx="${c.x}" cy="${c.y}" r="3" fill="${PALETTE.green}"><title>${points[i].date}: ${points[i].rate.toFixed(1)}%</title></circle>`).join("");
  const xLabels = points.map((p, i) => {
    if (i % 2 !== 0) return "";
    const d = p.date ? new Date(p.date) : null;
    const lbl = d ? `${d.getMonth() + 1}/${d.getDate()}` : "";
    return `<text x="${coords[i].x}" y="${H - pad + 14}" text-anchor="middle" fill="${PALETTE.muted2}" font-size="10">${lbl}</text>`;
  }).join("");
  const gid = "lineGrad-" + Math.random().toString(36).slice(2, 8);
  const avgRate = points.reduce((s, p) => s + p.rate, 0) / points.length;
  const latestRate = points[points.length - 1].rate;
  const minRate = Math.min(...points.map(p => p.rate));
  const maxRate = Math.max(...points.map(p => p.rate));
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg${analyticsChartsAnimated ? "" : " chart-entrance"}" role="img" aria-label="7-day success rate trend: latest ${latestRate.toFixed(1)}%, average ${avgRate.toFixed(1)}%, range ${minRate.toFixed(1)}% to ${maxRate.toFixed(1)}%">
    <title>7-day success rate trend</title>
    <desc>Daily success rate over the past 7 days. Latest: ${latestRate.toFixed(1)}%. Average: ${avgRate.toFixed(1)}%. Range: ${minRate.toFixed(1)}% to ${maxRate.toFixed(1)}%.</desc>
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${PALETTE.green}" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="${PALETTE.green}" stop-opacity="0"/>
    </linearGradient></defs>
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
    <path d="${areaD}" fill="url(#${gid})"/>
    <path d="${pathD}" fill="none" stroke="${PALETTE.green}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" class="line-path"/>
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
    wrap.innerHTML = emptyState(ICONS.analytics, "No trades yet. Signals will appear here after the first execution.");
    return;
  }
  const total = entries.reduce((s, e) => s + e.vol, 0);
  const colors = [PALETTE.blue, PALETTE.green, PALETTE.amber, PALETTE.red, PALETTE.muted2];
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
    return `<path d="${path}" fill="${colors[i]}" stroke="${PALETTE.card}" stroke-width="1" class="donut-slice"><title>${escapeHtml(e.name)}: ${escapeHtml(String(e.vol))} (${escapeHtml(pct)}%)</title></path>`;
  }).join("");
  const legend = entries.map((e, i) => {
    const pct = ((e.vol / total) * 100).toFixed(1);
    return `<div class="legend-item"><span class="legend-dot c${i}" aria-hidden="true"></span><span class="legend-name">${escapeHtml(e.name)}</span><span class="legend-val">${escapeHtml(String(e.vol))} (${escapeHtml(pct)}%)</span></div>`;
  }).join("");
  const topSym = entries[0] ? entries[0].name : "none";
  const topPct = entries[0] ? ((entries[0].vol / total) * 100).toFixed(1) : "0";
  const ariaLbl = `Top symbols by volume: ${total} total trades, top symbol ${topSym} at ${topPct}%`;
  const descParts = entries.map(e => `${e.name}: ${e.vol} (${((e.vol / total) * 100).toFixed(1)}%)`).join(", ");
  wrap.innerHTML = `<div class="donut-wrap">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg donut-svg${analyticsChartsAnimated ? "" : " chart-entrance"}" role="img" aria-label="${escapeHtml(ariaLbl)}">
      <title>Top symbols by volume - ${total} total trades</title>
      <desc>${escapeHtml(descParts)}</desc>
      ${slices}
      <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="${PALETTE.text}" font-size="16" font-weight="700">${total}</text>
      <text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="${PALETTE.muted}" font-size="10">trades</text>
    </svg>
    <div class="legend" role="list" aria-label="Symbol legend">${legend}</div>
  </div>`;
  analyticsChartsAnimated = true;
}

function renderPipelineMonitor(content, actions) {
  const stages = ["Receive", "Queue", "Validate", "Deliver", "Ack"];
  content.innerHTML = `
    <div id="pipeline-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Signal Pipeline</h2>
      <div class="card-desc">5-stage signal processing pipeline - polling every 5s</div>
      <div class="pipeline-viz" id="pipeline-viz">
        ${stages.map((s, i) => {
          const arrow = i < stages.length - 1 ? '<div class="pipe-arrow"><div class="flow-dot"></div></div>' : "";
          return `<div class="pipe-stage" id="pipe-stage-${i}"><div class="pipe-name">${s}</div><div class="pipe-count" id="pipe-count-${i}" aria-live="polite">--</div></div>${arrow}`;
        }).join("")}
      </div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <h2 class="card-title">Queue Depth</h2>
        <div class="card-desc">Current pending signals in queue</div>
        <div id="queue-gauge-wrap" class="gauge-center"></div>
      </div>
      <div class="card">
        <h2 class="card-title">Throughput</h2>
        <div class="card-desc">Signals per minute (rolling 60s)</div>
        <div class="stat big-stat" id="throughput-stat" role="group" aria-label="Throughput"><div class="value" aria-live="polite">--</div><div class="label">signals/min</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Delivery Latency Histogram</h2>
      <div class="card-desc">Latency distribution across buckets</div>
      <div id="histogram-wrap" class="chart-wrap"></div>
    </div>
    <div class="grid grid-2">
      <div class="stat" id="stat-dupes"><div class="value skeleton line" aria-live="polite"></div><div class="label">Duplicate Rejections</div></div>
      <div class="stat" id="stat-retries"><div class="value skeleton line" aria-live="polite"></div><div class="label">Retries</div></div>
    </div>
  `;
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 5s</span>`;
  pipelineState.signalHistory = [];
  pipelineState.lastDelivered = 0;
  startPoll(pollPipeline, 5000);
}

let pipelineState = {
  signalHistory: [],
  lastDelivered: 0,
  latencies: [],
};

async function pollPipeline() {
  if (currentRoute !== "pipeline" || !visibilityPolling) return;
  const [statusRes, metricsText] = await Promise.all([
    useFetch("/api/status").catch(() => ({ data: null, error: "status", stale: false })),
    fetchMetrics(),
  ]);
  const status = statusRes.data;
  const m = metricsText;
  const staleEl = document.getElementById("pipeline-stale-banner");
  if (staleEl) {
    if (statusRes.stale && status) {
      staleEl.innerHTML = staleBanner();
    } else if (statusRes.error && !status && m) {
      staleEl.innerHTML = partialWarning("status endpoint unavailable");
    } else if (!m && status) {
      staleEl.innerHTML = partialWarning("metrics endpoint unavailable");
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (!status && !m) {
    const content = document.getElementById("content");
    if (content) {
      content.innerHTML = errorPanel("pipeline", "Status and metrics endpoints unavailable", "retry-pipeline");
      bindRetryToScope("retry-pipeline", () => route("pipeline"));
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
    updateGauge(queueWrap, qPct, "Queue", { size: 120, diskMode: false });
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
  const totalSamples = lats.length;
  const peak = buckets.reduce((a, b) => b.count > a.count ? b : a, { label: "none", count: 0 });
  const W = 600, H = 200, pad = 30, barW = (W - pad * 2) / buckets.length;
  const bars = buckets.map((b, i) => {
    const bh = (b.count / maxC) * (H - pad * 2);
    const x = pad + i * barW;
    const y = H - pad - bh;
    const color = i === 0 ? PALETTE.green : i === 1 ? PALETTE.green : i === 2 ? PALETTE.amber : i === 3 ? PALETTE.amber : PALETTE.red;
    return `<rect x="${x + 8}" y="${y}" width="${Math.max(0, barW - 16)}" height="${Math.max(2, bh)}" fill="${color}" rx="3"><title>${b.label}: ${b.count}</title></rect>
      <text x="${x + barW / 2}" y="${H - pad + 14}" text-anchor="middle" fill="${PALETTE.muted2}" font-size="10">${b.label}</text>
      <text x="${x + barW / 2}" y="${y - 6}" text-anchor="middle" fill="${PALETTE.muted}" font-size="10">${b.count}</text>`;
  }).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg" role="img" aria-label="Delivery latency histogram: ${totalSamples} samples, peak bucket ${peak.label} with ${peak.count} entries">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="${PALETTE.grid}"/>
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
  const color = opts.color || PALETTE.green;
  const max = opts.max != null ? opts.max : Math.max(100, ...values.map(v => v || 0));
  const min = opts.min || 0;
  const range = max - min || 1;
  const n = values.length;
  if (n === 0) return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${opts.label || "chart"}: no data"></svg>`;
  const pts = values.map((v, i) => {
    const x = pad + (i / Math.max(1, n - 1)) * (W - pad * 2);
    const norm = (v != null && !isNaN(v)) ? (v - min) / range : 0;
    const y = H - pad - norm * (H - pad * 2);
    return [x, y];
  });
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)} ${H - pad} L${pts[0][0].toFixed(1)} ${H - pad} Z`;
  const gid = "grad-" + Math.random().toString(36).slice(2, 8);
  const gridLines = [0.25, 0.5, 0.75].map(g => {
    const y = pad + g * (H - pad * 2);
    return `<line x1="${pad}" y1="${y}" x2="${W - pad}" y2="${y}" stroke="${PALETTE.gridFaint}"/>`;
  }).join("");
  const lastVal = values[values.length - 1];
  const lbl = opts.label ? `${opts.label}: current ${lastVal != null ? lastVal.toFixed(1) : "--"}` : "chart";
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${escapeHtml(lbl)}">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${gridLines}
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function updateLineChart(el, values, opts = {}) {
  if (!el) return;
  const paths = el.querySelectorAll("path");
  if (paths.length < 2) { el.innerHTML = svgLineChart(values, opts); return; }
  const W = opts.width || 600;
  const H = opts.height || 160;
  const pad = opts.pad || 24;
  const max = opts.max != null ? opts.max : Math.max(100, ...values.map(v => v || 0));
  const min = opts.min || 0;
  const range = max - min || 1;
  const n = values.length;
  if (n === 0) return;
  const pts = values.map((v, i) => {
    const x = pad + (i / Math.max(1, n - 1)) * (W - pad * 2);
    const norm = (v != null && !isNaN(v)) ? (v - min) / range : 0;
    const y = H - pad - norm * (H - pad * 2);
    return [x, y];
  });
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)} ${H - pad} L${pts[0][0].toFixed(1)} ${H - pad} Z`;
  paths[0].setAttribute("d", area);
  paths[1].setAttribute("d", line);
}

function svgSparkline(values, opts = {}) {
  const W = 60, H = 20;
  const color = opts.color || PALETTE.green;
  const n = values.length;
  if (n === 0) return `<svg viewBox="0 0 ${W} ${H}" class="sparkline" aria-hidden="true" focusable="false"></svg>`;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min;
  const pts = values.map((v, i) => {
    const x = (i / Math.max(1, n - 1)) * W;
    const norm = range > 0 ? (v - min) / range : 0.5;
    const y = H - norm * H;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" class="sparkline" preserveAspectRatio="none" aria-hidden="true" focusable="false">
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

function colorClass(c) {
  if (c === PALETTE.green) return "c-green";
  if (c === PALETTE.blue) return "c-blue";
  if (c === PALETTE.amber) return "c-amber";
  if (c === PALETTE.red) return "c-red";
  return "c-muted";
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
    useFetch("/api/system/health").catch(() => ({ data: null, error: "health", stale: false })),
    useFetch("/api/system/stats").catch(() => ({ data: null, error: "stats", stale: false })),
  ]);
  const h = hRes.data;
  const s = sRes.data;
  const staleEl = document.getElementById("sh-stale-banner");
  if (staleEl) {
    const partials = [];
    if (hRes.error && !h) partials.push("health");
    if (sRes.error && !s) partials.push("stats");
    if ((hRes.stale || sRes.stale) && (h || s)) {
      staleEl.innerHTML = staleBanner();
    } else if (partials.length > 0 && (h || s)) {
      staleEl.innerHTML = partialWarning(partials.join(" and ") + " unavailable");
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (!h && !s) {
    const content = domCache.content || document.getElementById("content");
    if (content) {
      content.innerHTML = errorPanel("system health", "Health and stats endpoints unavailable", "retry-sys-health");
      bindRetryToScope("retry-sys-health", () => route("sys-health"));
    }
    return;
  }
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
  updateLineChart(cpuEl, cpu, { color: PALETTE.green, label: "CPU %", max: 100 });
  updateLineChart(memEl, mem, { color: PALETTE.blue, label: "Memory %", max: 100 });
  const cpuVal = h.system ? h.system.cpu_percent : null;
  const memVal = h.system ? h.system.memory_percent : null;
  setTile("sh-cpu-val", cpuVal != null ? `${cpuVal.toFixed(1)}%` : "--", loadColor(cpuVal));
  setTile("sh-mem-val", memVal != null ? `${memVal.toFixed(1)}%` : "--", loadColor(memVal));
  setTile("sh-threads", h.process ? String(h.process.threads) : "--", "info");
  setTile("sh-proc-mem", h.process ? `${h.process.memory_mb.toFixed(1)} MB` : "--", "info");
  const diskWrap = document.getElementById("sh-disk-gauge");
  if (diskWrap && s) {
    const diskPct = s.disk ? s.disk.percent : null;
    updateGauge(diskWrap, diskPct, "Disk", { size: 140, diskMode: true });
  }
  const netEl = document.getElementById("sh-net");
  if (netEl && s && s.network) {
    netEl.innerHTML = `
      <div class="row"><span class="k">Bytes sent</span><span class="v">${escapeHtml(fmtBytes(s.network.bytes_sent))}</span></div>
      <div class="row"><span class="k">Bytes recv</span><span class="v">${escapeHtml(fmtBytes(s.network.bytes_recv))}</span></div>
      <div class="row"><span class="k">Packets sent</span><span class="v">${escapeHtml(String((s.network.packets_sent || 0).toLocaleString()))}</span></div>
      <div class="row"><span class="k">Packets recv</span><span class="v">${escapeHtml(String((s.network.packets_recv || 0).toLocaleString()))}</span></div>`;
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
        <div class="seg ok" data-w="${escapeHtml(String(iW))}" title="In use: ${escapeHtml(String(inUse))}"></div>
        <div class="seg info" data-w="${escapeHtml(String(aW))}" title="Available: ${escapeHtml(String(avail))}"></div>
        <div class="seg warn" data-w="${escapeHtml(String(oW))}" title="Overflow: ${escapeHtml(String(overflow))}"></div>
      </div>
      <div class="stacked-legend">
        <span class="lg ok"><span class="dot"></span>In use ${escapeHtml(String(inUse))}</span>
        <span class="lg info"><span class="dot"></span>Available ${escapeHtml(String(avail))}</span>
        <span class="lg warn"><span class="dot"></span>Overflow ${escapeHtml(String(overflow))}</span>
      </div>`;
    poolEl.querySelectorAll(".seg[data-w]").forEach(seg => {
      const w = parseFloat(seg.dataset.w);
      if (!isNaN(w)) seg.style.width = w + "%";
    });
  }
  const redisEl = document.getElementById("sh-redis");
  if (redisEl) {
    if (h.redis_info && Object.keys(h.redis_info).length) {
      const ri = h.redis_info;
      redisEl.innerHTML = `
        <div class="row"><span class="k">Used memory</span><span class="v">${escapeHtml(String(ri.used_memory_mb))} MB</span></div>
        <div class="row"><span class="k">Connected clients</span><span class="v">${escapeHtml(String(ri.connected_clients))}</span></div>
        <div class="row"><span class="k">Keyspace hits</span><span class="v">${escapeHtml(String((ri.keyspace_hits || 0).toLocaleString()))}</span></div>
        <div class="row"><span class="k">Keyspace misses</span><span class="v">${escapeHtml(String((ri.keyspace_misses || 0).toLocaleString()))}</span></div>`;
    } else {
      redisEl.innerHTML = `<div class="empty small"><div class="msg">Redis not configured</div></div>`;
    }
  }
}

function renderSystemHealth(content) {
  content.innerHTML = `
    <div id="sh-stale-banner"></div>
    <div class="grid grid-2">
      <div class="card">
        <h2 class="card-title">CPU Usage</h2>
        <div class="card-desc">60s rolling - updates every 5s</div>
        <div class="stat" id="sh-cpu-val"><div class="value skeleton line" aria-live="polite"></div><div class="label">Current</div></div>
        <div class="chart-wrap" id="sh-cpu-chart"></div>
      </div>
      <div class="card">
        <h2 class="card-title">Memory Usage</h2>
        <div class="card-desc">60s rolling - updates every 5s</div>
        <div class="stat" id="sh-mem-val"><div class="value skeleton line" aria-live="polite"></div><div class="label">Current</div></div>
        <div class="chart-wrap" id="sh-mem-chart"></div>
      </div>
    </div>
    <div class="grid grid-3">
      <div class="card">
        <h2 class="card-title">Disk Usage</h2>
        <div class="card-desc">Updates every 60s</div>
        <div class="gauge-center" id="sh-disk-gauge"></div>
      </div>
      <div class="card">
        <h2 class="card-title">Network I/O</h2>
        <div class="card-desc">Cumulative counters</div>
        <div id="sh-net"></div>
      </div>
      <div class="card">
        <h2 class="card-title">DB Pool</h2>
        <div class="card-desc">Connection pool stats</div>
        <div id="sh-db-pool"></div>
      </div>
    </div>
    <div class="grid grid-3">
      <div class="card">
        <h2 class="card-title">Redis Info</h2>
        <div class="card-desc">Cache server stats</div>
        <div id="sh-redis"></div>
      </div>
      <div class="stat" id="sh-threads"><div class="value skeleton line" aria-live="polite"></div><div class="label">Thread Count</div></div>
      <div class="stat" id="sh-proc-mem"><div class="value skeleton line" aria-live="polite"></div><div class="label">Process Memory</div></div>
    </div>
  `;
  startPoll(pollSystemHealth, 5000);
}

let webhookLogState = { rows: [], page: 0, stats: null, loaded: false, sort: { key: "timestamp", dir: "desc" }, filter: { range: "today", status: "", symbol: "", license: "" } };

function sortRows(rows, key, dir) {
  const mul = dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = a[key] == null ? "" : a[key];
    const bv = b[key] == null ? "" : b[key];
    const an = Number(av), bn = Number(bv);
    if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") return (an - bn) * mul;
    return String(av).localeCompare(String(bv)) * mul;
  });
}

function applySort(scope, sortKey) {
  const ths = scope.querySelectorAll("th.sortable");
  ths.forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === sortKey.key) th.classList.add(sortKey.dir === "asc" ? "sort-asc" : "sort-desc");
  });
}

function bindSortHeaders(scope, sortState, onSort) {
  scope.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (!k) return;
      if (sortState.key === k) sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
      else { sortState.key = k; sortState.dir = "asc"; }
      applySort(scope, sortState);
      onSort();
    });
  });
}

function skeletonRowsHTML(colspan, n) {
  return Array.from({ length: n }, () =>
    `<tr class="sk-row">${Array.from({ length: colspan }, () => `<td><div class="sk-cell"></div></td>`).join("")}</tr>`
  ).join("");
}

async function pollWebhookLogs() {
  if (currentRoute !== "sys-webhooks" || !visibilityPolling) return;
  const [recentRes, statsRes] = await Promise.all([
    useFetch("/api/webhooks/recent?limit=50").catch(() => ({ data: null, error: "logs", stale: false })),
    useFetch("/api/webhooks/stats?days=7").catch(() => ({ data: null, error: "stats", stale: false })),
  ]);
  const staleEl = document.getElementById("wl-stale-banner");
  if (staleEl) {
    const partials = [];
    if (recentRes.error && !recentRes.data) partials.push("logs");
    if (statsRes.error && !statsRes.data) partials.push("stats");
    if ((recentRes.stale || statsRes.stale) && (recentRes.data || statsRes.data)) {
      staleEl.innerHTML = staleBanner();
    } else if (partials.length > 0 && (recentRes.data || statsRes.data)) {
      staleEl.innerHTML = partialWarning(partials.join(" and ") + " unavailable");
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (recentRes.error && !recentRes.data && !webhookLogState.loaded) {
    const content = domCache.content || document.getElementById("content");
    if (content) {
      content.innerHTML = errorPanel("webhook logs", recentRes.error, "retry-sys-webhooks");
      bindRetryToScope("retry-sys-webhooks", () => route("sys-webhooks"));
    }
    return;
  }
  if (recentRes.data && recentRes.data.webhooks) {
    webhookLogState.rows = recentRes.data.webhooks;
    webhookLogState.loaded = true;
  }
  if (statsRes.data) {
    webhookLogState.stats = statsRes.data;
    updateWebhookStats();
  }
  renderWebhookTable();
}

function updateWebhookStats() {
  const s = webhookLogState.stats;
  if (!s) return;
  setTile("wl-stat-total", String(s.total || 0), "info");
  setTile("wl-stat-success", String(s.successful || 0), "ok");
  setTile("wl-stat-failed", String(s.failed || 0), s.failed > 0 ? "bad" : "info");
  setTile("wl-stat-rate", s.success_rate != null ? `${s.success_rate.toFixed(1)}%` : "--", s.success_rate >= 80 ? "ok" : s.success_rate >= 50 ? "warn" : "bad");
}

function renderWebhookLogs(content) {
  webhookLogState.loaded = false;
  webhookLogState.page = 0;
  content.innerHTML = `
    <div id="wl-stale-banner"></div>
    <div class="grid grid-4">
      <div class="stat" id="wl-stat-total"><div class="value skeleton line" aria-live="polite"></div><div class="label">Total (7d)</div></div>
      <div class="stat" id="wl-stat-success"><div class="value skeleton line" aria-live="polite"></div><div class="label">Successful</div></div>
      <div class="stat" id="wl-stat-failed"><div class="value skeleton line" aria-live="polite"></div><div class="label">Failed</div></div>
      <div class="stat" id="wl-stat-rate"><div class="value skeleton line" aria-live="polite"></div><div class="label">Success Rate</div></div>
    </div>
    <div class="card">
      <h2 class="card-title">Webhook Logs</h2>
      <div class="card-desc">Recent webhook requests - polling every 10s</div>
      <div class="filter-bar">
        <select class="input filter-sel" id="wl-range" aria-label="Filter by time range">
          <option value="today">Today</option>
          <option value="7d">7 days</option>
          <option value="30d">30 days</option>
        </select>
        <select class="input filter-sel" id="wl-status" aria-label="Filter by status code">
          <option value="">All status</option>
          <option value="200">200</option>
          <option value="202">202</option>
          <option value="400">400</option>
          <option value="401">401</option>
        </select>
        <input class="input filter-input" id="wl-symbol" placeholder="Symbol filter" aria-label="Filter by symbol">
        <input class="input filter-input" id="wl-license" placeholder="License filter" aria-label="Filter by license">
        <button class="filter-clear" id="wl-clear" type="button">Clear filters</button>
      </div>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table class="data-table" id="wl-table" aria-label="Webhook logs">
          <caption class="sr-only">Recent webhook requests</caption>
          <thead>
            <tr>
              <th scope="col" class="sortable" data-sort="timestamp">Timestamp</th>
              <th scope="col" class="sortable" data-sort="ip_address">Source IP</th>
              <th scope="col" class="sortable" data-sort="action">Action</th>
              <th scope="col" class="sortable" data-sort="symbol">Symbol</th>
              <th scope="col" class="sortable" data-sort="volume">Volume</th>
              <th scope="col" class="sortable" data-sort="response_code">Status</th>
              <th scope="col" class="sortable" data-sort="execution_time_ms">Resp ms</th>
              <th scope="col">Payload</th>
            </tr>
          </thead>
          <tbody id="wl-body">${skeletonRowsHTML(8, 5)}</tbody>
        </table>
      </div>
      <div class="table-scroll-hint">Swipe horizontally to see more columns</div>
      <div class="table-footer">
        <span id="wl-count" aria-live="polite">0 rows</span>
        <div class="pager">
          <button class="btn outline sm" id="wl-prev" type="button">Prev</button>
          <button class="btn outline sm" id="wl-next" type="button">Next</button>
          <button class="btn outline sm" id="wl-load-more" data-action="wl-load-more">Load More</button>
        </div>
      </div>
    </div>
  `;
  const rangeEl = document.getElementById("wl-range");
  const statusEl = document.getElementById("wl-status");
  const symEl = document.getElementById("wl-symbol");
  const licEl = document.getElementById("wl-license");
  const clearEl = document.getElementById("wl-clear");
  if (rangeEl) rangeEl.addEventListener("change", () => { webhookLogState.filter.range = rangeEl.value; renderWebhookTable(); });
  if (statusEl) statusEl.addEventListener("change", () => { webhookLogState.filter.status = statusEl.value; webhookLogState.page = 0; renderWebhookTable(); });
  if (symEl) symEl.addEventListener("input", () => { webhookLogState.filter.symbol = symEl.value.trim().toUpperCase(); webhookLogState.page = 0; renderWebhookTable(); });
  if (licEl) licEl.addEventListener("input", () => { webhookLogState.filter.license = licEl.value.trim(); webhookLogState.page = 0; renderWebhookTable(); });
  if (clearEl) clearEl.addEventListener("click", () => {
    webhookLogState.filter = { range: "today", status: "", symbol: "", license: "" };
    webhookLogState.page = 0;
    if (rangeEl) rangeEl.value = "today";
    if (statusEl) statusEl.value = "";
    if (symEl) symEl.value = "";
    if (licEl) licEl.value = "";
    renderWebhookTable();
  });
  const prevBtn = document.getElementById("wl-prev");
  const nextBtn = document.getElementById("wl-next");
  if (prevBtn) prevBtn.addEventListener("click", e => { e.preventDefault(); if (webhookLogState.page > 0) { webhookLogState.page--; renderWebhookTable(); } });
  if (nextBtn) nextBtn.addEventListener("click", e => { e.preventDefault(); const total = filteredWebhookRows().length; if ((webhookLogState.page + 1) * 50 < total) { webhookLogState.page++; renderWebhookTable(); } });
  const loadMore = content.querySelector("[data-action='wl-load-more']");
  if (loadMore) loadMore.addEventListener("click", e => { e.preventDefault(); webhookLogState.page++; renderWebhookTable(); });
  bindSortHeaders(content.querySelector("#wl-table"), webhookLogState.sort, renderWebhookTable);
  applySort(content.querySelector("#wl-table"), webhookLogState.sort);
  startPoll(pollWebhookLogs, 10000);
}

function filteredWebhookRows() {
  const f = webhookLogState.filter;
  let rows = webhookLogState.rows;
  if (f.status) rows = rows.filter(r => String(r.response_code) === f.status);
  if (f.symbol) rows = rows.filter(r => (r.symbol || "").toUpperCase().includes(f.symbol));
  if (f.license) rows = rows.filter(r => (JSON.stringify(r.payload || "")).includes(f.license));
  return sortRows(rows, webhookLogState.sort.key, webhookLogState.sort.dir);
}

function renderWebhookTable() {
  const tbody = document.getElementById("wl-body");
  if (!tbody) return;
  const table = document.getElementById("wl-table");
  if (table) applySort(table, webhookLogState.sort);
  if (!webhookLogState.loaded) {
    tbody.innerHTML = skeletonRowsHTML(8, 5);
    return;
  }
  const rows = filteredWebhookRows();
  const perPage = 50;
  const totalPages = Math.max(1, Math.ceil(rows.length / perPage));
  if (webhookLogState.page >= totalPages) webhookLogState.page = totalPages - 1;
  const start = webhookLogState.page * perPage;
  const end = Math.min(start + perPage, rows.length);
  const shown = rows.slice(start, end);
  if (shown.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty small">${webhookLogState.rows.length === 0 ? emptyState(ICONS.webhook, "No webhooks received yet.") : '<div class="msg">No matches</div>'}</td></tr>`;
  } else {
    tbody.innerHTML = shown.map(r => {
      const cls = statusColor(r.response_code);
      const preview = r.payload ? String(r.payload).slice(0, 60) : "";
      const payloadStr = r.payload ? escapeHtml(JSON.stringify(r.payload)) : "";
      return `<tr class="row-expandable" data-payload="${payloadStr}" tabindex="0" role="button" aria-expanded="false" aria-label="Webhook log at ${escapeHtml(formatTime(r.timestamp))}, status ${r.response_code || "--"}">
        <td class="mono" scope="row">${escapeHtml(formatTime(r.timestamp))}</td>
        <td class="mono">${escapeHtml(r.ip_address || "--")}</td>
        <td>${escapeHtml(r.action || "--")}</td>
        <td class="mono">${escapeHtml(r.symbol || "--")}</td>
        <td class="mono">${escapeHtml(String(r.volume || "--"))}</td>
        <td><span class="badge ${cls}"><span class="dot" aria-hidden="true"></span>${r.response_code || "--"}</span></td>
        <td class="mono">${escapeHtml(String(r.execution_time_ms || "--"))}</td>
        <td class="mono trunc" title="${escapeHtml(preview)}">${escapeHtml(preview)}</td>
      </tr>`;
    }).join("");
  }
  const countEl = document.getElementById("wl-count");
  if (countEl) countEl.textContent = rows.length > 0 ? `Showing ${start + 1}-${end} of ${rows.length}` : "0 rows";
  const prevBtn = document.getElementById("wl-prev");
  const nextBtn = document.getElementById("wl-next");
  if (prevBtn) prevBtn.disabled = webhookLogState.page === 0;
  if (nextBtn) nextBtn.disabled = end >= rows.length;
  const loadMore = document.querySelector("[data-action='wl-load-more']");
  if (loadMore) loadMore.style.display = end >= rows.length ? "none" : "";
  tbody.querySelectorAll(".row-expandable").forEach(tr => {
    const toggle = () => {
      const tbodyEl = tr.parentElement;
      const existingOpen = tbodyEl.querySelector(".row-expanded");
      const existingSelf = tr.nextElementSibling && tr.nextElementSibling.classList.contains("row-expanded");
      if (existingSelf) { tr.nextElementSibling.remove(); tr.classList.remove("expanded"); tr.setAttribute("aria-expanded", "false"); return; }
      if (existingOpen) {
        const prevRow = existingOpen.previousElementSibling;
        existingOpen.remove();
        if (prevRow) { prevRow.classList.remove("expanded"); prevRow.setAttribute("aria-expanded", "false"); }
      }
      const payload = tr.dataset.payload || "(no payload)";
      const exp = document.createElement("tr");
      exp.className = "row-expanded";
      const td = document.createElement("td");
      td.colSpan = 8;
      const pre = document.createElement("pre");
      pre.className = "payload-pre";
      pre.textContent = payload;
      td.appendChild(pre);
      exp.appendChild(td);
      tr.after(exp);
      tr.classList.add("expanded");
      tr.setAttribute("aria-expanded", "true");
    };
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });
}

async function pollRiskMonitor() {
  if (currentRoute !== "sys-risk" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/api/risk-status");
  const staleEl = document.getElementById("risk-stale-banner");
  if (staleEl) {
    if (stale && data) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (error || !data) {
    const card = document.getElementById("risk-status-card");
    if (card && !cache["/api/risk-status"]) {
      card.innerHTML = errorPanel("risk monitor", error, "retry-sys-risk");
      bindRetryToScope("retry-sys-risk", () => route("sys-risk"));
    } else if (card) {
      card.innerHTML = `<div class="empty small"><div class="msg">Risk data unavailable</div><div class="sub">${escapeHtml(error || "")}</div><button class="btn outline sm mt" data-action="retry-sys-risk">${ICONS.refresh}Retry</button></div>`;
      bindRetryToScope("retry-sys-risk", () => route("sys-risk"));
    }
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
    <div id="risk-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Trading Status</h2>
      <div class="card-desc">Current risk gate - polling every 10s</div>
      <div class="stat big-stat" id="risk-status-card"><div class="value skeleton line" aria-live="polite"></div><div class="label">Loading...</div></div>
    </div>
    <div class="card">
      <h2 class="card-title">Risk Metrics</h2>
      <div class="grid grid-4">
        <div class="stat" id="risk-daily-pnl"><div class="value skeleton line" aria-live="polite"></div><div class="label">Daily P&L</div></div>
        <div class="stat" id="risk-max-dd"><div class="value skeleton line" aria-live="polite"></div><div class="label">Max Drawdown</div></div>
        <div class="stat" id="risk-mode"><div class="value skeleton line" aria-live="polite"></div><div class="label">Position Sizing</div></div>
        <div class="stat" id="risk-pct"><div class="value skeleton line" aria-live="polite"></div><div class="label">Risk / Trade</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Account</h2>
      <div class="grid grid-3">
        <div class="stat" id="risk-balance"><div class="value skeleton line" aria-live="polite"></div><div class="label">Balance</div></div>
        <div class="stat" id="risk-equity"><div class="value skeleton line" aria-live="polite"></div><div class="label">Equity</div></div>
        <div class="stat" id="risk-margin"><div class="value skeleton line" aria-live="polite"></div><div class="label">Margin Level</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Alerts</h2>
      <div id="risk-alerts"></div>
    </div>
  `;
  startPoll(pollRiskMonitor, 10000);
}

let errorLogState = { entries: [], paused: false, filter: "ALL", search: "" };

async function pollErrorLogs() {
  if (currentRoute !== "sys-errors" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/api/logs/errors?limit=100");
  const staleEl = document.getElementById("el-stale-banner");
  if (staleEl) {
    if (stale && data) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (error && !data && errorLogState.entries.length === 0) {
    const wrap = document.getElementById("el-list");
    if (wrap) {
      wrap.innerHTML = errorPanel("error logs", error, "retry-sys-errors");
      bindRetryToScope("retry-sys-errors", () => route("sys-errors"));
    }
    return;
  }
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
    wrap.innerHTML = emptyState(ICONS.check, "No errors logged. All systems healthy.");
    return;
  }
  wrap.innerHTML = entries.map((e, i) => {
    const cls = e.level === "ERROR" ? "bad" : e.level === "WARN" ? "warn" : "info";
    const full = escapeHtml(e.full || e.message);
    return `<div class="log-entry ${cls}" data-idx="${i}" data-full="${full}">
      <span class="log-ts mono">${escapeHtml(formatTime(e.timestamp))}</span>
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
      const pre = document.createElement("pre");
      pre.className = "payload-pre";
      pre.textContent = full;
      exp.appendChild(pre);
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
    <div id="el-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Error Log Viewer</h2>
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
        <div id="el-list"><div class="skeleton line"></div><div class="skeleton line"></div><div class="skeleton line short"></div></div>
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
  startPoll(pollErrorLogs, 10000);
}

async function pollDatabaseManager() {
  if (currentRoute !== "sys-database" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/api/database/stats");
  const staleEl = document.getElementById("db-stale-banner");
  if (staleEl) {
    if (stale && data) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (error && !data) {
    const content = domCache.content || document.getElementById("content");
    if (content && !cache["/api/database/stats"]) {
      content.innerHTML = errorPanel("database", error, "retry-sys-database");
      bindRetryToScope("retry-sys-database", () => route("sys-database"));
    }
    return;
  }
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
    <div id="db-stale-banner"></div>
    <div class="grid grid-3">
      <div class="stat" id="db-size"><div class="value skeleton line" aria-live="polite"></div><div class="label">DB Size</div></div>
      <div class="stat" id="db-total"><div class="value skeleton line" aria-live="polite"></div><div class="label">Total Records</div></div>
      <div class="stat" id="db-type"><div class="value skeleton line" aria-live="polite"></div><div class="label">DB Type</div></div>
    </div>
    <div class="card">
      <h2 class="card-title">Table Row Counts</h2>
      <div class="card-desc">Records per table - polling every 30s</div>
      <div id="db-tables"></div>
    </div>
    <div class="card">
      <h2 class="card-title">Cleanup Tool</h2>
      <div class="card-desc">Remove old records to free space</div>
      <div class="cleanup-row">
        <label for="db-days">Days to keep <span class="req">*</span></label>
        <input class="input days-input" id="db-days" type="number" value="90" min="1" max="3650" inputmode="numeric" aria-required="true">
        <span>days</span>
        <button class="btn red sm" id="db-cleanup" data-action="db-cleanup">Delete</button>
      </div>
      <div class="hint mt">Whole number between 1 and 3650. Records older than this are deleted permanently.</div>
      <div id="db-cleanup-result" aria-live="polite"></div>
    </div>
    <div class="card">
      <h2 class="card-title">Migration Status</h2>
      <div class="card-desc">Database schema migrations</div>
      <div id="db-migrations" class="empty small"><div class="msg">Migration info not available via API</div></div>
    </div>
  `;
  const btn = content.querySelector("[data-action='db-cleanup']");
  if (btn) btn.addEventListener("click", e => { e.preventDefault(); runDbCleanup(); });
  const daysInput = content.querySelector("#db-days");
  if (daysInput) attachValidator(daysInput, "days");
  startPoll(pollDatabaseManager, 30000);
}

async function runDbCleanup() {
  const daysEl = document.getElementById("db-days");
  const result = document.getElementById("db-cleanup-result");
  if (!validateInput(daysEl, "days")) { result.innerHTML = ""; return; }
  const days = parseInt(daysEl.value.trim(), 10);
  openConfirmModal("Database Cleanup", `Delete all records older than ${days} days? This cannot be undone.`, () => doDbCleanup(days), "Confirm");
}

async function doDbCleanup(days) {
  const result = document.getElementById("db-cleanup-result");
  const btn = document.getElementById("db-cleanup");
  if (!btn) return;
  setBtnLoading(btn, "Deleting...");
  try {
    const r = await http(`/api/database/cleanup?days_to_keep=${days}`, { method: "POST", headers: jsonHeaders(true) });
    const data = await r.json();
    result.innerHTML = `<div class="inline-ok">${ICONS.check}Cleanup complete: ${escapeHtml(JSON.stringify(data.result || data))}</div>`;
    toast("Database cleanup complete", "ok");
    setBtnSuccess(btn, "Done", 2000);
    invalidateCache("/api/database/stats");
    pollDatabaseManager();
  } catch (e) {
    result.innerHTML = `<div class="inline-error">${ICONS.x}Cleanup failed: ${escapeHtml(e.message)}</div>`;
    setBtnError(btn, "Failed");
  }
}

let metricsState = { history: {}, lastValues: {} };

async function pollMetrics() {
  if (currentRoute !== "sys-metrics" || !visibilityPolling) return;
  const text = await fetchMetrics();
  const staleEl = document.getElementById("metrics-stale-banner");
  if (staleEl) {
    if (!text && metricsState.lastValues && Object.keys(metricsState.lastValues).length > 0) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (!text) {
    if (!metricsState.lastValues || Object.keys(metricsState.lastValues).length === 0) {
      const content = domCache.content || document.getElementById("content");
      if (content) {
        content.innerHTML = errorPanel("metrics", "Metrics endpoint unavailable", "retry-sys-metrics");
        bindRetryToScope("retry-sys-metrics", () => route("sys-metrics"));
      }
    }
    return;
  }
  const specs = [
    { key: "webhook_total", name: "pinetunnel_webhook_signals_total", label: "Webhook Signals Total", color: PALETTE.green },
    { key: "ws_delivered", name: "pinetunnel_websocket_signals_delivered_total", label: "WS Signals Delivered", color: PALETTE.blue },
    { key: "queue_depth", name: "pinetunnel_signal_queue_depth", label: "Signal Queue Depth", color: PALETTE.amber },
    { key: "redis_ops", name: "pinetunnel_redis_operations_total", label: "Redis Ops Total", color: PALETTE.blue },
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
    { key: "webhook_total", label: "Webhook Signals Total", color: PALETTE.green, digits: 0 },
    { key: "ws_delivered", label: "WS Signals Delivered", color: PALETTE.blue, digits: 0 },
    { key: "queue_depth", label: "Signal Queue Depth", color: PALETTE.amber, digits: 0 },
    { key: "redis_ops", label: "Redis Ops Total", color: PALETTE.blue, digits: 0 },
    { key: "ws_push_avg", label: "WS Push Avg (ms)", color: PALETTE.green, digits: 1 },
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
      <div class="metric-value" data-color="${escapeHtml(s.color)}" aria-live="polite" aria-label="${escapeHtml(s.label)}: ${val != null ? escapeHtml(fmtNum(val, s.digits)) : '--'}">${val != null ? escapeHtml(fmtNum(val, s.digits)) : "--"}</div>
    </div>`;
  }).join("");
  grid.querySelectorAll(".metric-value[data-color]").forEach(el => {
    el.style.color = el.dataset.color;
  });
}

function renderMetrics(content) {
  content.innerHTML = `
    <div id="metrics-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Performance Metrics</h2>
      <div class="card-desc">Prometheus metrics - polling every 10s - sparklines show last 20 samples</div>
    </div>
    <div class="grid grid-3" id="metrics-grid">
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
      <div class="card metric-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>
    </div>
  `;
  startPoll(pollMetrics, 10000);
}

async function pollDiagnostics() {
  if (currentRoute !== "sys-diag" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/api/diagnostics");
  const staleEl = document.getElementById("diag-stale-banner");
  if (staleEl) {
    if (stale && data) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (error || !data) {
    const overall = document.getElementById("diag-overall");
    if (overall) {
      if (!cache["/api/diagnostics"]) {
        overall.innerHTML = errorPanel("diagnostics", error, "retry-sys-diag");
        bindRetryToScope("retry-sys-diag", () => route("sys-diag"));
      } else {
        overall.innerHTML = `<div class="empty small"><div class="msg">Diagnostics unavailable</div><div class="sub">${escapeHtml(error || "")}</div><button class="btn outline sm mt" data-action="retry-sys-diag">${ICONS.refresh}Retry</button></div>`;
        bindRetryToScope("retry-sys-diag", () => route("sys-diag"));
      }
    }
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
        <div class="probe-latency mono">${p.latency_ms != null ? escapeHtml(String(p.latency_ms.toFixed(2))) + " ms" : "-- ms"}</div>
        <div class="probe-detail trunc">${escapeHtml(p.detail || "")}</div>
      </div>`;
    }).join("");
  }
}

function renderDiagnostics(content) {
  content.innerHTML = `
    <div id="diag-stale-banner"></div>
    <div class="card">
      <h2 class="card-title">Overall Status</h2>
      <div class="stat big-stat" id="diag-overall"><div class="value skeleton line" aria-live="polite"></div><div class="label">Running diagnostics...</div></div>
    </div>
    <div class="grid grid-4" id="diag-grid">
      ${Array(8).fill('<div class="card probe-card"><div class="skeleton line"></div><div class="skeleton line short"></div></div>').join("")}
    </div>
  `;
  startPoll(pollDiagnostics, 15000);
}

async function pollBotStatus() {
  if (currentRoute !== "sys-bot" || !visibilityPolling) return;
  const { data, error, stale } = await useFetch("/health/bot");
  const staleEl = document.getElementById("bot-stale-banner");
  if (staleEl) {
    if (stale && data) {
      staleEl.innerHTML = staleBanner();
    } else {
      staleEl.innerHTML = "";
    }
  }
  if (error || !data) {
    const card = document.getElementById("bot-status-card");
    if (card) {
      if (!cache["/health/bot"]) {
        card.innerHTML = errorPanel("bot status", error, "retry-sys-bot");
        bindRetryToScope("retry-sys-bot", () => route("sys-bot"));
      } else {
        card.innerHTML = `<div class="empty small"><div class="msg">Bot status unavailable</div><div class="sub">${escapeHtml(error || "")}</div><button class="btn outline sm mt" data-action="retry-sys-bot">${ICONS.refresh}Retry</button></div>`;
        bindRetryToScope("retry-sys-bot", () => route("sys-bot"));
      }
    }
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
      <div class="row"><span class="k">Token len</span><span class="v mono">${escapeHtml(String(env.TELEGRAM_BOT_TOKEN_len || 0))}</span></div>`;
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
    <div id="bot-stale-banner"></div>
    <div class="grid grid-2">
      <div class="card">
        <h2 class="card-title">Bot Status</h2>
        <div class="card-desc">Telegram bot health - polling every 15s</div>
        <div class="stat big-stat" id="bot-status-card"><div class="value skeleton line" aria-live="polite"></div><div class="label">Loading...</div></div>
      </div>
      <div class="card">
        <h2 class="card-title">Bot Info</h2>
        <div class="stat" id="bot-username"><div class="value skeleton line" aria-live="polite"></div><div class="label">Username</div></div>
        <div class="stat" id="bot-handlers"><div class="value skeleton line" aria-live="polite"></div><div class="label">Handler Count</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Status Flags</h2>
      <div class="grid grid-4">
        <div class="stat" id="bot-started"><div class="value skeleton line" aria-live="polite"></div><div class="label">Started</div></div>
        <div class="stat" id="bot-app"><div class="value skeleton line" aria-live="polite"></div><div class="label">App Exists</div></div>
        <div class="stat" id="bot-token"><div class="value skeleton line" aria-live="polite"></div><div class="label">Token</div></div>
        <div class="stat" id="bot-updater"><div class="value skeleton line" aria-live="polite"></div><div class="label">Updater</div></div>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Admin Configuration</h2>
      <div id="bot-admins"></div>
    </div>
    <div class="card">
      <h2 class="card-title">Alerts</h2>
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
  startPoll(pollBotStatus, 15000);
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

let licenseState = { rows: [], search: "", loaded: false, sort: { key: "email", dir: "asc" }, page: 0, perPage: 25 };

async function renderLicenses(content, actions) {
  const tk = renderToken;
  content.innerHTML = skeletonCard(1);
  actions.innerHTML = `<button class="btn primary sm" id="add-license-btn" data-action="add-license">${ICONS.plus}Add License</button>`;
  const addBtn = actions.querySelector("[data-action='add-license']");
  if (addBtn) addBtn.addEventListener("click", e => { e.preventDefault(); openLicenseModal(); });
  licenseState.loaded = false;
  licenseState.page = 0;
  const { data, error, stale } = await useFetch(`${API}/users`);
  if (staleRender(tk)) return;
  if (error && !data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load licenses</div><div class="sub">${escapeHtml(error)}</div><button class="btn outline sm mt" data-action="retry-licenses">Retry</button></div>`;
    bindRetry(content, "retry-licenses", () => route("licenses"));
    return;
  }
  licenseState.rows = data ? data.users : [];
  licenseState.loaded = true;
  const staleBannerHtml = stale ? staleBanner() : "";
  const total = data ? data.total_users : 0;
  const totalEAs = licenseState.rows.reduce((n, u) => n + (u.stats && u.stats.connected_eas || 0), 0);
  content.innerHTML = `
    ${staleBannerHtml}
    <div class="panel-toolbar">
      <input class="input search-input" id="lic-search" placeholder="Search by key, name, or email" value="${escapeHtml(licenseState.search)}" aria-label="Search licenses">
      <button class="filter-clear" id="lic-clear" type="button">Clear</button>
      <span class="badge info">${total} users</span>
      <span class="badge ok">${totalEAs} EAs connected</span>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table class="data-table mgr-table" id="lic-table" aria-label="Licenses">
          <caption class="sr-only">License manager</caption>
          <thead>
            <tr>
              <th scope="col" class="sortable" data-sort="license_key">License Key</th>
              <th scope="col" class="sortable" data-sort="name">Name</th>
              <th scope="col" class="sortable" data-sort="email">Email</th>
              <th scope="col" class="sortable" data-sort="status">Status</th>
              <th scope="col">Secret</th>
              <th scope="col" class="sortable" data-sort="expires_at">Expires</th>
              <th scope="col" class="sortable th-num" data-sort="connected_eas">EAs</th>
              <th scope="col" class="sortable th-num" data-sort="total_trades">Trades</th>
              <th scope="col" class="sortable" data-sort="last_activity">Last Activity</th>
              <th scope="col" class="td-actions">Actions</th>
            </tr>
          </thead>
          <tbody id="lic-body">${skeletonRowsHTML(10, 5)}</tbody>
        </table>
      </div>
      <div class="table-scroll-hint">Swipe horizontally to see more columns</div>
      <div class="table-footer">
        <span id="lic-count" aria-live="polite">0 rows</span>
        <div class="pager">
          <button class="btn outline sm" id="lic-prev" type="button">Prev</button>
          <button class="btn outline sm" id="lic-next" type="button">Next</button>
        </div>
      </div>
    </div>
  `;
  const searchEl = content.querySelector("#lic-search");
  if (searchEl) searchEl.addEventListener("input", () => {
    licenseState.search = searchEl.value.trim().toLowerCase();
    licenseState.page = 0;
    renderLicenseRows();
  });
  const clearEl = content.querySelector("#lic-clear");
  if (clearEl) clearEl.addEventListener("click", () => {
    licenseState.search = "";
    licenseState.page = 0;
    if (searchEl) searchEl.value = "";
    renderLicenseRows();
  });
  const prevBtn = content.querySelector("#lic-prev");
  const nextBtn = content.querySelector("#lic-next");
  if (prevBtn) prevBtn.addEventListener("click", e => { e.preventDefault(); if (licenseState.page > 0) { licenseState.page--; renderLicenseRows(); } });
  if (nextBtn) nextBtn.addEventListener("click", e => { e.preventDefault(); const t = filteredLicenseRows().length; if ((licenseState.page + 1) * licenseState.perPage < t) { licenseState.page++; renderLicenseRows(); } });
  bindSortHeaders(content.querySelector("#lic-table"), licenseState.sort, renderLicenseRows);
  applySort(content.querySelector("#lic-table"), licenseState.sort);
  renderLicenseRows();
  startPoll(pollLicenses, 15000);
}

function filteredLicenseRows() {
  const q = licenseState.search;
  let rows = licenseState.rows;
  if (q) {
    rows = rows.filter(u => {
      const hay = `${u.email || ""} ${u.name || ""} ` + (u.licenses || []).map(l => l.license_key || "").join(" ");
      return hay.toLowerCase().includes(q);
    });
  }
  const flat = rows.map(u => {
    const lic = (u.licenses && u.licenses[0]) || {};
    const stats = u.stats || {};
    const status = lic.status || "active";
    const enabled = lic.enabled !== false;
    let pillCls = "ok";
    let pillLabel = "Active";
    if (!enabled || status === "disabled") { pillCls = "bad"; pillLabel = "Disabled"; }
    else if (status === "expired") { pillCls = "warn"; pillLabel = "Expired"; }
    return {
      license_key: lic.license_key || "",
      name: u.name || "",
      email: u.email || "",
      status: pillLabel,
      status_cls: pillCls,
      enabled,
      expires_at: lic.expires_at || "",
      connected_eas: stats.connected_eas || 0,
      total_trades: stats.total_trades || 0,
      last_activity: lic.last_activity || "",
      _u: u,
      _lic: lic,
      _stats: stats,
    };
  });
  return sortRows(flat, licenseState.sort.key, licenseState.sort.dir);
}

function pollLicenses() {
  if (currentRoute !== "licenses" || !visibilityPolling) return;
  useFetch(`${API}/users`).then(({ data, stale }) => {
    const staleEl = document.getElementById("lic-stale-banner");
    if (staleEl) {
      staleEl.innerHTML = (stale && data) ? staleBanner() : "";
    }
    if (!data) return;
    licenseState.rows = data.users;
    renderLicenseRows();
  });
}

function renderLicenseRows() {
  const body = document.getElementById("lic-body");
  if (!body) return;
  const table = document.getElementById("lic-table");
  if (table) applySort(table, licenseState.sort);
  if (!licenseState.loaded) {
    body.innerHTML = skeletonRowsHTML(10, 5);
    return;
  }
  const rows = filteredLicenseRows();
  const pp = licenseState.perPage;
  const totalPages = Math.max(1, Math.ceil(rows.length / pp));
  if (licenseState.page >= totalPages) licenseState.page = totalPages - 1;
  const start = licenseState.page * pp;
  const end = Math.min(start + pp, rows.length);
  const shown = rows.slice(start, end);
  if (shown.length === 0) {
    body.innerHTML = `<tr><td colspan="10" class="empty small">${licenseState.rows.length === 0 ? emptyState(ICONS.license, "No licenses configured. Click 'Add License' to create one.", "Add License", "empty-add-license") : '<div class="msg">No matches</div>'}</td></tr>`;
    if (licenseState.rows.length === 0) {
      const btn = body.querySelector("[data-action='empty-add-license']");
      if (btn) btn.addEventListener("click", e => { e.preventDefault(); openLicenseModal(); });
    }
  } else {
    body.innerHTML = shown.map(r => {
      const u = r._u, lic = r._lic;
      const expires = r.expires_at ? new Date(r.expires_at).toLocaleDateString() : "--";
      const lastAct = r.last_activity ? relativeTime(r.last_activity) : (r.total_trades > 0 ? "prior" : "never");
      return `<tr>
        <td class="td-key" scope="row" title="${escapeHtml(r.license_key)}">${escapeHtml(maskKey(r.license_key))}</td>
        <td>${escapeHtml(r.name || "--")}</td>
        <td class="td-email" title="${escapeHtml(r.email)}">${escapeHtml(r.email || "--")}</td>
        <td><span class="status-pill ${r.status_cls}"><span class="dot" aria-hidden="true"></span>${r.status}</span></td>
        <td class="secret-cell" aria-label="Secret hidden">****</td>
        <td>${escapeHtml(expires)}</td>
        <td class="td-num">${r.connected_eas}</td>
        <td class="td-num">${r.total_trades}</td>
        <td>${escapeHtml(lastAct)}</td>
        <td class="td-actions">
          <button class="btn ghost sm" data-action="lic-edit" data-key="${escapeHtml(r.license_key)}" aria-label="Edit license ${escapeHtml(maskKey(r.license_key))}" title="Edit">${ICONS.edit}</button>
          <button class="btn ghost sm" data-action="lic-extend" data-key="${escapeHtml(r.license_key)}" aria-label="Extend license ${escapeHtml(maskKey(r.license_key))} by 30 days" title="Extend +30d">+30d</button>
          <button class="btn ghost sm" data-action="lic-toggle" data-key="${escapeHtml(r.license_key)}" data-enabled="${r.enabled ? "1" : "0"}" aria-label="${r.enabled ? "Disable" : "Enable"} license ${escapeHtml(maskKey(r.license_key))}" title="${r.enabled ? "Disable" : "Enable"}">${r.enabled ? ICONS.ban : ICONS.power}</button>
          <button class="btn ghost sm" data-action="lic-disconnect" data-key="${escapeHtml(r.license_key)}" aria-label="Force disconnect license ${escapeHtml(maskKey(r.license_key))}" title="Force disconnect">${ICONS.power}</button>
          <button class="btn ghost sm" data-action="lic-delete" data-key="${escapeHtml(r.license_key)}" data-name="${escapeHtml(u.email || u.name || "")}" aria-label="Delete license ${escapeHtml(maskKey(r.license_key))}" title="Delete">${ICONS.trash}</button>
        </td>
      </tr>`;
    }).join("");
  }
  const countEl = document.getElementById("lic-count");
  if (countEl) countEl.textContent = rows.length > 0 ? `Showing ${start + 1}-${end} of ${rows.length}` : "0 rows";
  const prevBtn = document.getElementById("lic-prev");
  const nextBtn = document.getElementById("lic-next");
  if (prevBtn) prevBtn.disabled = licenseState.page === 0;
  if (nextBtn) nextBtn.disabled = end >= rows.length;
  body.querySelectorAll("[data-action='lic-edit']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); comingSoon("License editing"); }));
  body.querySelectorAll("[data-action='lic-extend']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); comingSoon("License extension"); }));
  body.querySelectorAll("[data-action='lic-toggle']").forEach(b => b.addEventListener("click", e => { e.preventDefault(); e.stopPropagation(); comingSoon("Enable/disable license"); }));
  body.querySelectorAll("[data-action='lic-disconnect']").forEach(b => b.addEventListener("click", e => {
    e.preventDefault(); e.stopPropagation();
    const key = b.dataset.key;
    openConfirmModal("Force disconnect", `Force disconnect all EA sessions for license ${maskKey(key)}? The EA will need to reconnect.`, () => comingSoon("Force disconnect"), "Disconnect");
  }));
  body.querySelectorAll("[data-action='lic-delete']").forEach(b => b.addEventListener("click", e => {
    e.preventDefault(); e.stopPropagation();
    const key = b.dataset.key;
    const name = b.dataset.name;
    openConfirmModal("Delete license", `Delete license for ${name}?`, () => comingSoon("License deletion"));
  }));
}

function comingSoon(feature) {
  toast(`${feature} - coming soon (Phase 3)`, "bad");
}

function genKey(prefix) {
  const seg = () => Math.random().toString(36).slice(2, 6).toUpperCase();
  return `${prefix}-${seg()}-${seg()}-${seg()}-${seg()}`;
}

let _lastFocusedBeforeModal = null;
let _activeModalStack = [];

function _getFocusable(scope) {
  return Array.from(scope.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"]))')).filter(el => !el.disabled && el.offsetParent !== null);
}

function _trapTab(e, overlay) {
  if (e.key !== "Tab") return;
  const focusable = _getFocusable(overlay);
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey) {
    if (document.activeElement === first || !overlay.contains(document.activeElement)) { e.preventDefault(); last.focus(); }
  } else {
    if (document.activeElement === last || !overlay.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  }
}

function openModal(overlay, opts) {
  opts = opts || {};
  _lastFocusedBeforeModal = document.activeElement;
  document.body.appendChild(overlay);
  _activeModalStack.push(overlay);
  const onEscape = e => { if (e.key === "Escape") { e.preventDefault(); closeModal(overlay); } };
  const onTab = e => _trapTab(e, overlay);
  const onBackdrop = e => { if (e.target === overlay) closeModal(overlay); };
  overlay.addEventListener("click", onBackdrop);
  document.addEventListener("keydown", onEscape);
  document.addEventListener("keydown", onTab);
  overlay._modalCleanup = () => {
    overlay.removeEventListener("click", onBackdrop);
    document.removeEventListener("keydown", onEscape);
    document.removeEventListener("keydown", onTab);
  };
  requestAnimationFrame(() => overlay.classList.add("modal-open"));
  const focusable = _getFocusable(overlay);
  if (focusable.length > 0) setTimeout(() => focusable[0].focus(), 50);
  if (opts.onClose) overlay._onClose = opts.onClose;
}

function closeModal(overlay) {
  if (!overlay || !overlay.parentNode) return;
  overlay.classList.remove("modal-open");
  overlay.classList.add("modal-closing");
  if (overlay._modalCleanup) overlay._modalCleanup();
  const idx = _activeModalStack.indexOf(overlay);
  if (idx >= 0) _activeModalStack.splice(idx, 1);
  setTimeout(() => {
    if (overlay.parentNode) overlay.remove();
    if (_lastFocusedBeforeModal && _activeModalStack.length === 0) { try { _lastFocusedBeforeModal.focus(); } catch {} _lastFocusedBeforeModal = null; }
    if (overlay._onClose) overlay._onClose();
  }, 200);
}

function openLicenseModal() {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Add License");
  overlay.innerHTML = `<div class="modal-card">
    <button class="modal-close" data-action="modal-close" aria-label="Close">${ICONS.x}</button>
    <h2 class="modal-title">Add License</h2>
    <div class="modal-desc">Create a new license key. CRUD endpoints arrive in Phase 3.</div>
    <div class="modal-body" id="lic-modal-form">
      <div class="field">
        <label for="lic-modal-key">License Key <span class="req">*</span></label>
        <div class="gen-row">
          <input class="input" id="lic-modal-key" value="${genKey("PT")}" readonly>
          <button class="btn outline sm" data-action="regen-key">${ICONS.refresh}Regenerate</button>
        </div>
        <div class="hint">Auto-generated - click Regenerate for a new key</div>
      </div>
      <div class="field">
        <label for="lic-modal-name">Name <span class="opt">(optional)</span></label>
        <input class="input" id="lic-modal-name" placeholder="Client name" autocomplete="off" spellcheck="false">
      </div>
      <div class="field">
        <label for="lic-modal-email">Email <span class="opt">(optional)</span></label>
        <input class="input" id="lic-modal-email" type="email" placeholder="client@example.com" autocomplete="email" spellcheck="false" inputmode="email">
      </div>
      <div class="field">
        <label for="lic-modal-secret">Secret <span class="req">*</span></label>
        <div class="gen-row">
          <input class="input" id="lic-modal-secret" value="${genKey("SEC")}" readonly>
          <button class="btn outline sm" data-action="regen-secret">${ICONS.refresh}Regenerate</button>
        </div>
        <div class="hint">Auto-generated - click Regenerate for a new secret</div>
      </div>
      <div class="field">
        <label for="lic-modal-expires">Expires At <span class="opt">(optional)</span></label>
        <input class="input" id="lic-modal-expires" type="date">
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn outline" data-action="modal-cancel">Cancel</button>
      <button class="btn primary" data-action="modal-save">${ICONS.check}Create (Phase 3)</button>
    </div>
  </div>`;
  openModal(overlay);
  overlay.querySelector("[data-action='modal-close']").addEventListener("click", () => closeModal(overlay));
  overlay.querySelector("[data-action='modal-cancel']").addEventListener("click", () => closeModal(overlay));
  overlay.querySelector("[data-action='regen-key']").addEventListener("click", e => { e.preventDefault(); overlay.querySelector("#lic-modal-key").value = genKey("PT"); });
  overlay.querySelector("[data-action='regen-secret']").addEventListener("click", e => { e.preventDefault(); overlay.querySelector("#lic-modal-secret").value = genKey("SEC"); });
  const emailEl = overlay.querySelector("#lic-modal-email");
  if (emailEl) attachValidator(emailEl, "email");
  submitOnEnter(overlay.querySelector("#lic-modal-form"), () => overlay.querySelector("[data-action='modal-save']").click());
  overlay.querySelector("[data-action='modal-save']").addEventListener("click", e => {
    e.preventDefault();
    if (emailEl) {
      const err = VALIDATORS.email(emailEl.value.trim());
      if (err) { validateInput(emailEl, "email"); emailEl.focus(); return; }
    }
    setBtnLoading(overlay.querySelector("[data-action='modal-save']"), "Creating...");
    setTimeout(() => { closeModal(overlay); comingSoon("License creation"); }, 600);
  });
  autofocusFirst(overlay);
}

function openConfirmModal(title, msg, onConfirm, confirmLabel = "Delete") {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", escapeHtml(title));
  overlay.innerHTML = `<div class="modal-card confirm">
    <div class="confirm-icon">${ICONS.alert}</div>
    <h2 class="confirm-heading">Are you sure?</h2>
    <div class="confirm-context">${escapeHtml(title)}</div>
    <div class="confirm-desc">${escapeHtml(msg)}</div>
    <div class="modal-footer">
      <button class="btn outline" data-action="confirm-cancel">Cancel</button>
      <button class="btn red" data-action="confirm-ok">${ICONS.trash}${escapeHtml(confirmLabel)}</button>
    </div>
  </div>`;
  openModal(overlay);
  overlay.querySelector("[data-action='confirm-cancel']").addEventListener("click", () => closeModal(overlay));
  overlay.querySelector("[data-action='confirm-ok']").addEventListener("click", e => { e.preventDefault(); closeModal(overlay); onConfirm(); });
}

let securityState = { data: null, headers: null };

async function renderSecurity(content, actions) {
  const tk = renderToken;
  content.innerHTML = skeletonCard(2);
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  const [rlRes, hdrRes] = await Promise.all([
    useFetch(`${API}/rate-limits`),
    useFetch(`${API}/security-headers`),
  ]);
  if (staleRender(tk)) return;
  if (rlRes.error && !rlRes.data) {
    content.innerHTML = `<div class="empty"><div class="icon">${ICONS.alert}</div><div class="msg">Failed to load security data</div><div class="sub">${escapeHtml(rlRes.error)}</div><button class="btn outline sm mt" data-action="retry-security">Retry</button></div>`;
    bindRetry(content, "retry-security", () => route("security"));
    return;
  }
  securityState.data = rlRes.data;
  securityState.headers = hdrRes.data;
  renderSecurityContent(content);
  startPoll(pollSecurity, 10000);
}

function pollSecurity() {
  if (currentRoute !== "security" || !visibilityPolling) return;
  Promise.all([useFetch(`${API}/rate-limits`), useFetch(`${API}/security-headers`)]).then(([rl, hdr]) => {
    if (rl.data) securityState.data = rl.data;
    if (hdr.data) securityState.headers = hdr.data;
    const content = document.getElementById("content");
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
      <h2 class="card-title">Blocked IPs</h2>
      <div class="card-desc">Currently blocked by rate limiter</div>
      <div class="table-wrap">
        <table class="data-table mgr-table" aria-label="Blocked IPs">
          <caption class="sr-only">IPs currently blocked by rate limiter</caption>
          <thead><tr><th scope="col">IP</th><th scope="col">Blocked At</th><th scope="col">Reason</th><th scope="col" class="td-num">Remaining</th><th scope="col" class="td-actions">Action</th></tr></thead>
          <tbody>${blockedRows}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Security Headers</h2>
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
      <h2 class="card-title">TradingView IP Allowlist</h2>
      <div class="card-desc">Webhook requests restricted to known TradingView egress IPs</div>
      <div class="allowlist-status">
        <div>
          <div class="label">Status: <strong class="${tvAllow ? "txt-green" : "txt-muted2"}">${tvAllow ? "Enabled" : "Disabled"}</strong></div>
          ${tvIps.length > 0 ? `<div class="ips">${tvIps.map(escapeHtml).join(", ")}</div>` : ""}
        </div>
        <span class="status-pill ${tvAllow ? "ok" : "muted"}"><span class="dot"></span>${tvAllow ? "ON" : "OFF"}</span>
      </div>
    </div>
    <div class="card">
      <h2 class="card-title">Recent 401/403 Responses</h2>
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
  const tk = renderToken;
  const ps = getPanelState("audit");
  content.innerHTML = skeletonCard(1);
  actions.innerHTML = `${ICONS.refresh}<span>Auto-poll 10s</span>`;
  auditState = {
    rows: [], filterAction: ps.filters.filterAction || "", filterAdmin: ps.filters.filterAdmin || "",
    filterFrom: ps.filters.filterFrom || "", filterTo: ps.filters.filterTo || "",
    search: ps.filters.search || "", loading: false, hasMore: true, limit: 50,
  };
  await loadAuditPage(true);
  if (staleRender(tk)) return;
  renderAuditContent(content);
  startPoll(pollAudit, 10000);
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
    const content = document.getElementById("content");
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
      <input class="input search-input" id="audit-search" type="search" placeholder="Search details" value="${escapeHtml(auditState.search)}" aria-label="Search" autocomplete="off" spellcheck="false" inputmode="search">
      <button class="filter-clear" id="audit-clear" type="button">Clear filters</button>
    </div>
    <div class="card">
      <h2 class="card-title">Admin Activity Timeline</h2>
      <div class="card-desc">${filtered.length} entries - polling every 10s</div>
      <div class="timeline" id="audit-timeline"></div>
      ${auditState.hasMore ? `<div class="load-more-row" id="audit-load-more">Showing ${auditState.rows.length} - increase limit for more history</div>` : `<div class="load-more-row">End of log</div>`}
    </div>
  `;
  const tl = content.querySelector("#audit-timeline");
  if (filtered.length === 0) {
    tl.innerHTML = `${auditState.rows.length === 0 ? emptyState(ICONS.audit, "No admin actions recorded yet.") : '<div class="empty small"><div class="msg">No matches</div></div>'}`;
  } else {
    tl.innerHTML = filtered.map(a => {
      const sev = auditSeverityClass(a.action);
      const ts = a.timestamp ? formatTime(a.timestamp) : "--";
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
  if (actionSel) actionSel.addEventListener("change", () => { auditState.filterAction = actionSel.value; setPanelState("audit", { filters: { filterAction: actionSel.value } }); renderAuditContent(content); });
  if (adminSel) adminSel.addEventListener("change", () => { auditState.filterAdmin = adminSel.value; setPanelState("audit", { filters: { filterAdmin: adminSel.value } }); renderAuditContent(content); });
  if (fromEl) fromEl.addEventListener("change", () => { auditState.filterFrom = fromEl.value; setPanelState("audit", { filters: { filterFrom: fromEl.value } }); renderAuditContent(content); });
  if (toEl) toEl.addEventListener("change", () => { auditState.filterTo = toEl.value; setPanelState("audit", { filters: { filterTo: toEl.value } }); renderAuditContent(content); });
  if (searchEl) searchEl.addEventListener("input", () => { auditState.search = searchEl.value.trim(); setPanelState("audit", { filters: { search: searchEl.value.trim() } }); renderAuditContent(content); });
  const clearEl = content.querySelector("#audit-clear");
  if (clearEl) clearEl.addEventListener("click", () => {
    auditState.filterAction = "";
    auditState.filterAdmin = "";
    auditState.filterFrom = "";
    auditState.filterTo = "";
    auditState.search = "";
    setPanelState("audit", { filters: { filterAction: "", filterAdmin: "", filterFrom: "", filterTo: "", search: "" } });
    if (actionSel) actionSel.value = "";
    if (adminSel) adminSel.value = "";
    if (fromEl) fromEl.value = "";
    if (toEl) toEl.value = "";
    if (searchEl) searchEl.value = "";
    renderAuditContent(content);
  });
}

window.route = route;
window.copy = copy;
window.saveTelegram = saveTelegram;
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
})();
