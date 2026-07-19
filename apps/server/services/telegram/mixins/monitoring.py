import json
import logging
import os
import shutil
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown as _escape_md

from apps.server.db.analytics_store import account_stats_latest, get_stats_for_license
from ..helpers import CONNECTED_CLIENT_THRESHOLD_SEC, SEP, _sanitize_error, calc_pagination
from ..keyboards import respond

logger = logging.getLogger(__name__)


class MonitoringMixin:
    """Server monitoring: status, connections, connection detail, logs, account stats."""

    _PAGE_PREFIX = {"webhook": "whlog", "admin": "audit", "conn": "conn"}

    async def _cmd_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        await self._show_monitor_menu(update)

    async def _show_monitor_menu(self, update: Update):
        keyboard = [
            [
                InlineKeyboardButton("[OK] Status", callback_data="mon_status"),
                InlineKeyboardButton("[NET] Connections", callback_data="mon_connections"),
            ],
            [
                InlineKeyboardButton("$ Accounts", callback_data="mon_account_stats"),
                InlineKeyboardButton("[LOG] Logs", callback_data="mon_logs"),
            ],
            [InlineKeyboardButton("[!] Security", callback_data="mon_security")],
            [InlineKeyboardButton("<= Back to Menu", callback_data="menu_main")],
        ]

        text = " *Server Monitor*\n" f"{SEP}\n" "Select a view:"

        await respond(update, text, keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _show_status(self, update: Update):
        text = f"[OK] *Server Status*\n{SEP}\n"
        now = datetime.now()

        disk_path = os.getenv("DATA_DIR", "/data" if os.path.exists("/data") else "/")
        try:
            usage = shutil.disk_usage(disk_path)
            free_mb = usage.free / (1024 * 1024)
            total_mb = usage.total / (1024 * 1024)
            used_pct = (usage.used / usage.total) * 100
            disk_emoji = "[OK]" if free_mb > 100 else "[X]"
            text += f"{disk_emoji} Disk: {free_mb:.0f}MB free / {total_mb:.0f}MB ({used_pct:.1f}% used)\n"
        except Exception:
            text += "[!] Disk: Unable to check\n"

        try:
            self.db_manager.execute_query("SELECT 1")
            text += "[OK] Database: Connected\n"
        except Exception:
            text += "[X] Database: Error\n"

        try:
            pool_stats = self.db_manager.get_pool_stats()
            text += f"   Pool: {pool_stats.get('in_use', 0)} in use / {pool_stats.get('total_connections', 0)} total\n"
        except Exception:
            logger.debug("Failed to get DB pool stats", exc_info=True)

        total_lic = len(self.client_manager.clients)
        active_lic = self._active_license_count
        text += f" Licenses: {active_lic}/{total_lic} active\n"

        total_pending = 0
        for key in self.client_manager.clients:
            try:
                signals = self.db_manager.get_pending_signals(key)
                if signals:
                    total_pending += len(signals)
            except Exception:
                logger.debug("Failed to get pending signals for %s", key, exc_info=True)

        if total_pending > 0:
            text += f" Pending signals: {total_pending}\n"
        else:
            text += "[OK] No pending signals\n"

        try:
            process = psutil.Process()
            create_time = datetime.fromtimestamp(process.create_time())
            uptime = now - create_time

            hours, remainder = divmod(int(uptime.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            cpu_pct = process.cpu_percent(interval=0.1)
            mem = process.memory_info()
            mem_mb = mem.rss / 1024 / 1024

            sys_cpu = psutil.cpu_percent(interval=0.1)
            sys_mem = psutil.virtual_memory()

            text += (
                f" Uptime: {hours}h {minutes}m {seconds}s\n"
                f" Process CPU: {cpu_pct:.1f}%\n"
                f" Process RAM: {mem_mb:.1f} MB\n"
                f" System CPU: {sys_cpu:.1f}%\n"
                f" System RAM: {sys_mem.percent:.1f}% "
                f"({sys_mem.available / 1024 / 1024 / 1024:.1f}GB free)\n"
                f" Threads: {process.num_threads()}\n"
            )
        except ImportError:
            text += "[!] psutil not available\n"
        except Exception as e:
            text += f"[!] Error: {_sanitize_error(e)}\n"

        text += f"\n {now.strftime('%Y-%m-%d %H:%M:%S')}"

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(" Refresh", callback_data="mon_status")],
                    [
                        InlineKeyboardButton("[NET] Connections", callback_data="mon_connections"),
                        InlineKeyboardButton("$ Accounts", callback_data="mon_account_stats"),
                    ],
                    [InlineKeyboardButton("[LOG] Logs", callback_data="mon_logs")],
                    [InlineKeyboardButton("<= Back", callback_data="menu_monitor")],
                ]
            ),
        )

    async def _show_connections(self, update: Update):
        now = datetime.now()
        lines = [f"[NET] *Active Connections*\n{SEP}"]

        http_timeout = CONNECTED_CLIENT_THRESHOLD_SEC
        keyboard = []

        for key, poll_data in list(self.http_polling_clients.items()):
            client_info = poll_data.get("client_info", {})
            last_poll = poll_data.get("last_poll")
            if last_poll and (now - last_poll).total_seconds() <= http_timeout:
                name = _escape_md(client_info.get("name", "Unknown"), version=1)
                ago = int((now - last_poll).total_seconds())
                lines.append(f"   [OK] {name} (`{key[:8]}...`) - {ago}s ago (HTTP)")
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"[OK] {client_info.get('name', 'Unknown')} (HTTP)",
                            callback_data=f"mon_conn_detail_{key}",
                        )
                    ]
                )

        http_count = len(keyboard)

        # Show WebSocket connections
        ws_total = 0
        ws_licenses = 0
        if self.ws_manager:
            try:
                for lic_key in self.ws_manager.get_connected_license_keys():
                    ws_conns = self.ws_manager.get_connection_count(lic_key)
                    client = self.client_manager.get_client_by_license(lic_key)
                    name = _escape_md(
                        client.get("name", "Unknown") if client else "Unknown", version=1
                    )
                    ws_total += ws_conns
                    ws_licenses += 1
                    # If license already shown as HTTP polling, update that line
                    if lic_key in self.http_polling_clients:
                        for i, line in enumerate(lines):
                            if lic_key[:8] in line:
                                lines[i] = line.replace("(HTTP)", f"(HTTP+WS x{ws_conns})")
                                break
                    else:
                        lines.append(f"   [OK] {name} (`{lic_key[:8]}...`) - WS x{ws_conns}")
                        keyboard.append(
                            [
                                InlineKeyboardButton(
                                    f"[OK] {client.get('name', 'Unknown') if client else 'Unknown'} (WS)",
                                    callback_data=f"mon_conn_detail_{lic_key}",
                                )
                            ]
                        )
            except Exception:
                logger.debug("WS connection listing failed", exc_info=True)

        if http_count == 0 and ws_licenses == 0:
            lines.append("   (none)")

        lines.append(f"\n*Total*: {http_count} HTTP, {ws_total} WS")

        keyboard.append([InlineKeyboardButton(" Refresh", callback_data="mon_connections")])
        keyboard.append([InlineKeyboardButton("<= Back", callback_data="menu_monitor")])

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_connection_detail(self, update: Update, license_key: str):
        client = self.client_manager.get_client_by_license(license_key)
        if not client:
            await update.callback_query.edit_message_text(
                "[X] License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("<= Back", callback_data="mon_connections")]]
                ),
            )
            return

        name = _escape_md(client.get("name", "Unknown"), version=1)
        text = (
            f" *Connection Detail*\n"
            f"{SEP}\n"
            f"License: `{license_key}`\n"
            f"Name: {name}\n"
            f"{SEP}\n"
        )

        poll_data = self.http_polling_clients.get(license_key)
        if poll_data:
            last_poll = poll_data.get("last_poll")
            client_info = poll_data.get("client_info", {})
            now = datetime.now()
            if last_poll:
                ago = int((now - last_poll).total_seconds())
                text += f"\n\n*HTTP Polling*: Active ({ago}s ago)"
            else:
                text += "\n\n*HTTP Polling*: Registered (stale)"

            if client_info:
                text += f"\n  EA Version: {client_info.get('ea_version', 'N/A')}"
        else:
            text += "\n\n*HTTP Polling*: Not connected"

        # WebSocket connection info
        ws_conns = 0
        if self.ws_manager:
            try:
                ws_conns = self.ws_manager.get_connection_count(license_key)
            except Exception:
                logger.debug("Failed to get WS connection count for %s", license_key, exc_info=True)
        if ws_conns > 0:
            text += (
                f"\n\n[NET] *WebSocket*: {ws_conns} active connection{'s' if ws_conns != 1 else ''}"
            )
        else:
            text += "\n\n[NET] *WebSocket*: Not connected"

        try:
            stats = self.db_manager.get_signal_stats_by_license(license_key)
            text += f"\n\n *Signal Stats*:"
            text += f"\n  Total: {stats.get('total', 0)}"
            text += f"\n  Pending: {stats.get('pending', 0)}"
            text += f"\n  Acknowledged: {stats.get('acknowledged', 0)}"
        except Exception:
            logger.debug("Signal stats query failed for %s", license_key, exc_info=True)

        keyboard = [
            [
                InlineKeyboardButton(
                    "[NET] Force Disconnect", callback_data=f"lic_force_disconnect_{license_key}"
                )
            ],
            [InlineKeyboardButton("View Signals", callback_data=f"sig_lic_{license_key}")],
            [InlineKeyboardButton(" License Details", callback_data=f"lic_info_{license_key}")],
            [InlineKeyboardButton("<= Back", callback_data="mon_connections")],
        ]

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    def _fetch_audit_entries(self) -> list[dict]:
        if self.admin_logger is not None:
            try:
                rows = self.admin_logger.get_recent_activity(limit=500)
                entries: list[dict] = []
                for row in rows:
                    entry = dict(row)
                    details = entry.get("details")
                    if isinstance(details, str):
                        try:
                            entry["details"] = json.loads(details)
                        except Exception:
                            entry["details"] = {}
                    entries.append(entry)
                return entries
            except Exception:
                logger.debug("Failed to read audit log via admin_logger", exc_info=True)

        log_file = os.path.join(self.data_dir, "admin_audit.log")
        entries = []
        try:
            if os.path.exists(log_file):
                with open(log_file, "r") as f:
                    for line in f:
                        try:
                            entries.append(json.loads(line.strip()))
                        except Exception:
                            logger.debug("Failed to parse audit log entry", exc_info=True)
                entries.reverse()
        except Exception:
            logger.debug("Failed to read admin_audit.log fallback", exc_info=True)
        return entries

    async def _show_logs(self, update: Update, log_filter: str = "webhook", page: int = 0):
        PAGE_SIZE = 8
        total = 0

        if log_filter == "webhook":
            lines = [f"[LOG] *Recent Webhook Logs*\n{SEP}"]
            try:
                try:
                    count_rows = self.db_manager.execute_query(
                        "SELECT COUNT(*) as cnt FROM alert_history"
                    )
                    total = count_rows[0]["cnt"] if count_rows else 0
                except Exception:
                    total = 0

                if total == 0:
                    lines.append("\n_No webhook logs found._")
                else:
                    page, total_pages, offset = calc_pagination(page, total, PAGE_SIZE)

                    rows = self.db_manager.execute_query(
                        "SELECT timestamp, action, symbol, volume, response_code, "
                        "response_message, ip_address, execution_time_ms "
                        "FROM alert_history ORDER BY timestamp DESC LIMIT :lim OFFSET :off",
                        {"lim": PAGE_SIZE, "off": offset},
                    )

                    lines.append(f"Page {page + 1}/{total_pages} ({total} total)\n")

                    for row in rows:
                        r = dict(row)
                        ts = str(r.get("timestamp", ""))[:16]
                        resp_code = r.get("response_code", 0) or 0
                        emoji = "[OK]" if resp_code == 200 else "[X]"
                        symbol = r.get("symbol", "?") or "?"
                        action = (r.get("action", "?") or "?").upper()
                        vol = r.get("volume", "") or ""
                        vol_str = f" x{vol}" if vol else ""
                        exec_ms = r.get("execution_time_ms", "") or ""
                        ms_str = f" ({exec_ms}ms)" if exec_ms else ""
                        lines.append(
                            f"{emoji} {ts} | {action} {_escape_md(symbol, version=1)}{vol_str}{ms_str}"
                        )

                text = "\n".join(lines)
            except Exception as e:
                text = f"[LOG] *Webhook Logs*\n\n[!] Error: {_sanitize_error(e)}"

        elif log_filter == "admin":
            lines = [f" *Admin Audit Log*\n{SEP}"]
            entries = self._fetch_audit_entries()

            try:
                total = len(entries)
                if total == 0:
                    lines.append("\n_No audit log entries yet._")
                else:
                    page, total_pages, start = calc_pagination(page, total, PAGE_SIZE)
                    page_entries = entries[start : start + PAGE_SIZE]

                    lines.append(f"Page {page + 1}/{total_pages} ({total} entries)\n")

                    for entry in page_entries:
                        try:
                            ts = str(entry.get("timestamp", ""))[:16]
                            action = entry.get("action", "?")
                            user = entry.get("user", "") or entry.get("username", "?")
                            if not user.startswith("@") and user != "?":
                                user = f"@{user}"
                            details = entry.get("details", {})
                            if isinstance(details, str):
                                try:
                                    details = json.loads(details)
                                except Exception:
                                    details = {}

                            detail_str = ""
                            if isinstance(details, dict):
                                lk = details.get("license_key", "")
                                if lk:
                                    detail_str = f" `{lk[:8]}...`"

                            lines.append(
                                f" {ts} | {_escape_md(user, version=1)} | "
                                f"{_escape_md(action, version=1)}{detail_str}"
                            )
                        except Exception:
                            logger.debug("Failed to parse audit log entry", exc_info=True)

                text = "\n".join(lines)
            except Exception as e:
                text = f" *Audit Log*\n\n[!] Error: {_sanitize_error(e)}"

        elif log_filter == "conn":
            lines = [f" *Connection History*\n{SEP}"]
            entries = self._fetch_audit_entries()

            try:
                conn_events = [
                    e for e in entries
                    if e.get("action", "") in (
                        "client_connected",
                        "client_disconnected",
                        "force_disconnect",
                    )
                ]

                total = len(conn_events)
                if not conn_events:
                    lines.append("\n_No connection events found._")
                else:
                    page, total_pages, start = calc_pagination(page, total, PAGE_SIZE)
                    page_events = conn_events[start : start + PAGE_SIZE]

                    lines.append(f"Page {page + 1}/{total_pages} ({total} events)\n")

                    for evt in page_events:
                        ts = str(evt.get("timestamp", ""))[:16]
                        action = evt.get("action", "")
                        details = evt.get("details", {})
                        if isinstance(details, str):
                            try:
                                details = json.loads(details)
                            except Exception:
                                details = {}
                        lic = details.get("license_key", "?")
                        method = details.get("method", details.get("connection_type", ""))

                        if "connect" in action and "disconnect" not in action:
                            emoji = "[OK]"
                        elif "disconnect" in action:
                            emoji = "[X]"
                        else:
                            emoji = ""

                        method_str = f" ({_escape_md(method, version=1)})" if method else ""
                        lines.append(
                            f"{emoji} {ts} | `{lic[:8]}...` | "
                            f"{_escape_md(action, version=1)}{method_str}"
                        )

                text = "\n".join(lines)
            except Exception as e:
                text = f" *Connection History*\n\n[!] Error: {_sanitize_error(e)}"

        else:
            text = "[LOG] *Logs*\n\n[!] Unknown filter"

        filter_row = [
            InlineKeyboardButton(
                "[OK]  Webhook" if log_filter == "webhook" else " Webhook",
                callback_data="log_webhook",
            ),
            InlineKeyboardButton(
                "[OK]  Admin" if log_filter == "admin" else " Admin",
                callback_data="log_admin",
            ),
            InlineKeyboardButton(
                "[OK] [NET] Conn" if log_filter == "conn" else "[NET] Conn",
                callback_data="log_conn",
            ),
        ]

        keyboard = [filter_row]

        prefix = self._PAGE_PREFIX.get(log_filter, "whlog")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("<= Prev", callback_data=f"{prefix}_page_{page - 1}"))
        if total > (page + 1) * PAGE_SIZE:
            nav.append(InlineKeyboardButton("Next >", callback_data=f"{prefix}_page_{page + 1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton(" Refresh", callback_data="mon_logs")])
        keyboard.append([InlineKeyboardButton("<= Back", callback_data="menu_monitor")])

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_account_stats(self, update: Update):
        lines = [f"$ *Connected Account Stats*\n{SEP}"]

        try:
            stats = dict(account_stats_latest)
        except Exception:
            stats = {}

        if not stats:
            # Try DB fallback for each active license
            try:
                for lk in self.client_manager.clients:
                    snap = get_stats_for_license(lk)
                    if snap:
                        stats[lk] = snap
            except Exception:
                logger.debug("Failed to get account stats from DB fallback", exc_info=True)

        if stats:
            for lk, snap in stats.items():
                client = self.client_manager.get_client_by_license(lk)
                name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
                balance = snap.get("balance", 0)
                equity = snap.get("equity", 0)
                margin_level = snap.get("margin_level", 0)
                positions = snap.get("open_positions", 0)
                profit = snap.get("profit", 0)

                if margin_level > 0:
                    ml_str = f" | ML: {margin_level:.0f}%"
                else:
                    ml_str = ""
                profit_sign = "+" if profit >= 0 else ""
                lines.append(
                    f"\n {name} (`{lk[:8]}...`)"
                    f"\n   Bal: {balance:.2f} | Eq: {equity:.2f} | P/L: {profit_sign}{profit:.2f}"
                    f"\n   Pos: {positions}{ml_str}"
                )

            lines.append(f"\n*Total*: {len(stats)} account(s)")
        else:
            lines.append("\nNo account stats available.")

        lines.append(f"\n {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(" Refresh", callback_data="mon_account_stats")],
                    [InlineKeyboardButton("<= Back", callback_data="menu_monitor")],
                ]
            ),
        )

    async def _show_security(self, update: Update):
        import time as _time
        from apps.server.config.settings import get_config
        from apps.server.middleware.ip_validation import _TRADINGVIEW_IPS
        from apps.server.middleware.main import failed_attempt_tracker
        from apps.server.middleware.security import get_security_headers
        from apps.server.state import rate_limiter

        lines = [f"[!] *Security Center*\n{SEP}"]

        fa_stats = {"blocked_ips": [], "blocked_ip_count": 0, "failed_attempts_24h": 0}
        if failed_attempt_tracker is not None:
            fa_stats = failed_attempt_tracker.get_statistics()

        rl_stats: dict = {}
        rl_blocked_count = 0
        if rate_limiter is not None:
            rl_stats = rate_limiter.get_statistics()
            rl_blocked_count = len(rl_stats.get("blocked_ips", []))

        fa_blocked_count = fa_stats.get("blocked_ip_count", 0)
        total_blocked = fa_blocked_count + rl_blocked_count
        failed_24h = fa_stats.get("failed_attempts_24h", 0)
        rate_hits = rl_stats.get("rate_limited_requests", 0)

        blocked_emoji = "[OK]" if total_blocked == 0 else "[X]"
        lines.append(f"{blocked_emoji} Blocked IPs: {total_blocked}")
        if fa_blocked_count > 0:
            lines.append(f"   - Failed auth blocks (1h): {fa_blocked_count}")
        if rl_blocked_count > 0:
            lines.append(f"   - Rate limiter blocks (5m): {rl_blocked_count}")

        failed_emoji = "[OK]" if failed_24h <= 10 else "[!]"
        lines.append(f"{failed_emoji} Failed Attempts (24h): {failed_24h}")

        rate_emoji = "[OK]" if rate_hits <= 50 else "[!]"
        lines.append(f"{rate_emoji} Rate Limit Hits: {rate_hits}")

        headers = get_security_headers()
        active_count = len(headers)
        headers_emoji = "[OK]" if active_count >= 6 else ("[!]" if active_count > 0 else "[X]")
        lines.append(f"{headers_emoji} Security Headers: {active_count}/6 active")
        for name in ("x-frame-options", "content-security-policy", "x-content-type-options",
                     "x-xss-protection", "referrer-policy", "strict-transport-security"):
            val = headers.get(name)
            mark = "[OK]" if val else "[X]"
            lines.append(f"   {mark} {name}: {val or 'missing'}")

        cfg = get_config()
        tv_env = cfg.tradingview_ip_allowlist.lower()
        if tv_env in ("0", "false", "no"):
            tv_on = False
        elif tv_env in ("1", "true", "yes"):
            tv_on = True
        else:
            tv_on = cfg.environment == "production"
        env_ips = cfg.tradingview_ips
        tv_ips = [ip.strip() for ip in env_ips.split(",") if ip.strip()] if env_ips else sorted(_TRADINGVIEW_IPS)
        tv_emoji = "[OK]" if tv_on else "[ ]"
        lines.append(f"{tv_emoji} TradingView IP Allowlist: {'ON' if tv_on else 'OFF'}")
        if tv_ips:
            lines.append(f"   IPs: {', '.join(tv_ips)}")

        if total_blocked > 0:
            all_blocked = []
            for entry in fa_stats.get("blocked_ips", []):
                all_blocked.append(f"   {entry['ip']} (failed auth, {entry['remaining_seconds']}s left)")
            if rate_limiter is not None:
                for ip, block_until in rate_limiter.blocked_ips.items():
                    remaining = max(0, int(block_until - _time.time()))
                    all_blocked.append(f"   {ip} (rate limit, {remaining}s left)")
            if all_blocked:
                lines.append("\n*Blocked IPs Detail*:")
                lines.extend(all_blocked[:20])
                if len(all_blocked) > 20:
                    lines.append(f"   ... and {len(all_blocked) - 20} more")

        lines.append(f"\n {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(" Refresh", callback_data="mon_security")],
                    [InlineKeyboardButton("<= Back", callback_data="menu_monitor")],
                ]
            ),
        )
