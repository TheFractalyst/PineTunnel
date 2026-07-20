"""User dashboard mixin — account (with connection), trading (positions + history),
signals, and actions (close, disconnect, kill switch).

Admin-only adaptation: the per-chat ``_get_licenses_for_chat_id`` helper (defined on
the bot) returns ALL license keys, so admins browse any license's dashboard via
pickers. The single-license shortcut still skips the picker when only one license
exists.
"""

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown as _escape_md

from apps.server.db.analytics_store import get_stats_for_license
from apps.server.ws.handler import request_close_position
from ..helpers import SEP, _sanitize_error, calc_pagination

logger = logging.getLogger(__name__)

_PAGE_SIZE = 5
_STATUS_EMOJI = {
    "delivered": "✅",
    "executed": "✅",
    "failed": "❌",
    "error": "⚠️",
}


class UserDashboardMixin:
    """Dashboard: account stats with connection, trading (positions + history),
    signals, kill switch, and action buttons."""

    def _user_back_target(self, update: Update, picker_cb: str) -> str:
        """Return the correct 'back' callback: picker for multi-license, user_menu for single."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if len(licenses) <= 1:
            return "user_menu"
        return picker_cb

    # ── Account ────────────────────────────────────────────────────────────

    async def _show_user_account(self, update: Update, license_key: str):
        """Show account stats and connection status for one license."""
        client = self.client_manager.get_client_by_license(license_key)
        name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
        short_key = license_key[:8] + "..."

        snap = None
        try:
            snap = get_stats_for_license(license_key)
        except Exception:
            logger.debug("Failed to get stats snapshot for %s", license_key, exc_info=True)

        if not snap:
            try:
                rows = self.db_manager.get_latest_account_stats(license_key)
                if rows:
                    snap = dict(rows[0])
            except Exception:
                logger.debug(
                    "Failed to get account stats from DB for %s", license_key, exc_info=True
                )

        audit = None
        try:
            audit = self.db_manager.get_ea_audit_for_license(license_key)
        except Exception:
            logger.debug("Failed to get EA audit for %s", license_key, exc_info=True)

        http_connected = False
        last_poll = None
        now = datetime.now()
        poll_data = self.http_polling_clients.get(license_key)
        if poll_data and poll_data.get("last_poll"):
            last_poll = poll_data["last_poll"]
            if (now - last_poll).total_seconds() <= 10:
                http_connected = True

        ws_connected = False
        ws_count = 0
        if self.ws_manager:
            try:
                ws_count = len(self.ws_manager.get_connections_for_key(license_key))
                ws_connected = ws_count > 0
            except Exception:
                logger.debug("Failed to get WS connections for %s", license_key, exc_info=True)

        if ws_connected:
            transport = f"WS ({ws_count} conn)"
        elif http_connected:
            transport = "HTTP Polling"
            if last_poll:
                transport += f" (last {last_poll.strftime('%H:%M:%S')})"
        else:
            # Check license validity for more specific status
            valid, msg = self.client_manager.validate_license(license_key)
            if not valid:
                transport = "Expired" if "expired" in msg.lower() else "Invalid"
            else:
                transport = "Disconnected"

        if not snap:
            text = (
                f"💰 *Account*\n{SEP}\n"
                f"👤 {name} (`{short_key}`)\n\n"
                f"⏳ No stats received yet.\n"
                f"Make sure your EA is running and connected."
            )
        else:
            balance = snap.get("balance", 0) or 0
            equity = snap.get("equity", 0) or 0
            profit = snap.get("profit", 0) or 0
            margin = snap.get("margin", 0) or 0
            margin_free = snap.get("margin_free", 0) or 0
            margin_level = snap.get("margin_level", 0) or 0
            leverage = snap.get("leverage", 0) or 0
            positions = snap.get("open_positions", snap.get("positions", 0)) or 0
            currency = snap.get("currency", "") or ""
            server = snap.get("server", snap.get("broker", "")) or ""

            profit_sign = "+" if profit >= 0 else ""
            ml_str = f" | ML: {margin_level:.0f}%" if margin_level > 0 else ""

            text = (
                f"💰 *Account*\n{SEP}\n"
                f"👤 {name} (`{short_key}`)\n"
                f"🏦 {_escape_md(server, version=1)}\n"
                f"{SEP}\n"
                f"💰 Balance: {balance:.2f} {currency}\n"
                f"📈 Equity: {equity:.2f} {currency}\n"
                f"💵 P/L: {profit_sign}{profit:.2f} {currency}\n"
                f"📊 Margin: {margin:.2f} | Free: {margin_free:.2f}{ml_str}\n"
                f"📐 Leverage: 1:{leverage} | Positions: {positions}\n"
            )

        text += f"\n{SEP}\n"
        text += f"🔌 Transport: {transport}\n"

        if audit:
            ea_ver = audit.get("ea_version", "")
            dll_ver = audit.get("dll_version", "")
            platform = audit.get("platform", "")
            conn_mode = audit.get("connection_mode", "")
            ver_parts = []
            if ea_ver:
                ver_parts.append(f"EA: {ea_ver}")
            if dll_ver:
                ver_parts.append(f"DLL: {dll_ver}")
            if platform:
                ver_parts.append(platform)
            if ver_parts:
                text += " | ".join(ver_parts) + "\n"
            if conn_mode:
                text += f"📡 Mode: {_escape_md(conn_mode, version=1)}\n"

        text += f"\n🕐 {now.strftime('%H:%M:%S')}"

        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"ud_acct_{license_key}")],
            [InlineKeyboardButton("🔌 Disconnect EA", callback_data=f"ud_disc_{license_key}")],
            [
                InlineKeyboardButton(
                    "◀️ Back", callback_data=self._user_back_target(update, "ud_account_pick")
                )
            ],
        ]
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _show_user_account_pick(self, update: Update):
        """If multiple licenses, show picker; otherwise go straight to account."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            await update.callback_query.edit_message_text(
                "No linked licenses.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return
        if len(licenses) == 1:
            await self._show_user_account(update, licenses[0])
            return

        lines = [f"💰 *Account Stats*\n{SEP}\nSelect a license:"]
        keyboard = []
        for lic in licenses:
            client = self.client_manager.get_client_by_license(lic)
            name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
            short = lic[:8] + "..."
            lines.append(f"• {name} (`{short}`)")
            keyboard.append([InlineKeyboardButton(f"💰 {name}", callback_data=f"ud_acct_{lic}")])
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="user_menu")])
        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # ── Signals ────────────────────────────────────────────────────────────

    async def _show_user_signals(self, update: Update, license_key: str, page: int = 0):
        """Show recent signal history for one license."""
        client = self.client_manager.get_client_by_license(license_key)
        name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)

        try:
            total_count = self.db_manager.get_signal_count(license_key)
            page, total_pages, offset = calc_pagination(page, total_count, _PAGE_SIZE)
            signals = self.db_manager.get_signal_log_for_license(
                license_key, limit=_PAGE_SIZE, offset=offset
            )
        except Exception as e:
            await update.callback_query.edit_message_text(
                f"⚠️ Error fetching signals: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="ud_sig_pick")]]
                ),
            )
            return

        lines = [f"📡 *Signals — {name}*\n{SEP}"]
        if not signals:
            lines.append("\n_No signals found._")
        else:
            for sig in signals:
                action = (sig.get("action") or "?").upper()
                symbol = sig.get("symbol") or "?"
                exec_status = sig.get("execution_status") or "pending"
                acknowledged = sig.get("acknowledged", False)
                # Determine display status
                if exec_status == "delivered" or (acknowledged and exec_status == "pending"):
                    display_status = "delivered"
                elif exec_status in ("executed",):
                    display_status = "executed"
                elif exec_status in ("failed", "error"):
                    display_status = exec_status
                else:
                    display_status = "pending"

                ts = str(sig.get("timestamp", ""))[:16]
                vol = sig.get("volume")
                vol_str = f" x{vol}" if vol else ""

                status_emoji = _STATUS_EMOJI.get(display_status, "⏳")

                lines.append(
                    f"{status_emoji} `{ts}` | {action} {_escape_md(symbol, version=1)}{vol_str}"
                )

        lines.append(f"\n🕐 {datetime.now().strftime('%H:%M:%S')}")

        keyboard = []
        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀️ Prev", callback_data=f"ud_sigpg_{license_key}_{page - 1}")
            )
        if len(signals) == _PAGE_SIZE:
            nav.append(
                InlineKeyboardButton("Next ▶️", callback_data=f"ud_sigpg_{license_key}_{page + 1}")
            )
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"ud_sig_{license_key}")])
        keyboard.append(
            [
                InlineKeyboardButton(
                    "◀️ Back", callback_data=self._user_back_target(update, "ud_sig_pick")
                )
            ]
        )

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_user_signal_pick(self, update: Update):
        """Picker for signal history if multiple licenses."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            await update.callback_query.edit_message_text(
                "No linked licenses.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return
        if len(licenses) == 1:
            await self._show_user_signals(update, licenses[0])
            return

        lines = [f"📡 *Signals*\n{SEP}\nSelect a license:"]
        keyboard = []
        for lic in licenses:
            client = self.client_manager.get_client_by_license(lic)
            name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
            keyboard.append([InlineKeyboardButton(f"📡 {name}", callback_data=f"ud_sig_{lic}")])
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="user_menu")])
        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # ── Trading (Positions + History) ──────────────────────────────────────

    async def _show_user_trading(
        self, update: Update, license_key: str, tab: str = "open", page: int = 0
    ):
        """Show open positions or closed trade history for one license."""
        client = self.client_manager.get_client_by_license(license_key)
        name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)

        open_label = "✅ Open" if tab == "open" else "📊 Open"
        closed_label = "✅ Closed" if tab == "closed" else "📜 Closed"
        tab_row = [
            InlineKeyboardButton(open_label, callback_data=f"ud_trade_open_{license_key}"),
            InlineKeyboardButton(closed_label, callback_data=f"ud_trade_closed_{license_key}"),
            InlineKeyboardButton("📡 Signals", callback_data=f"ud_sig_{license_key}"),
        ]

        if tab == "open":
            try:
                positions = self.db_manager.get_latest_open_positions(license_key)
            except Exception as e:
                await update.callback_query.edit_message_text(
                    f"⚠️ Error fetching positions: {_sanitize_error(e)}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="ud_trading")]]
                    ),
                )
                return

            lines = [f"📊 *Positions — {name}*\n{SEP}"]
            if not positions:
                lines.append("\n_No open positions._")
            else:
                currency = ""
                try:
                    snap = get_stats_for_license(license_key)
                    currency = snap.get("currency", "") if snap else ""
                except Exception:
                    logger.debug("Failed to get currency for %s", license_key, exc_info=True)

                total_pl = 0.0
                for pos in positions:
                    ticket = pos.get("ticket", "?")
                    symbol = pos.get("symbol", "?")
                    pos_type = (pos.get("type") or "?").upper()
                    vol = pos.get("volume", 0) or 0
                    profit = pos.get("profit", 0) or 0
                    total_pl += profit
                    pl_sign = "+" if profit >= 0 else ""
                    pl_emoji = "📈" if profit >= 0 else "📉"
                    lines.append(
                        f"{pl_emoji} #{ticket} {_escape_md(symbol, version=1)} "
                        f"{pos_type} {vol:.2f} | {pl_sign}{profit:.2f}"
                    )

                if len(positions) > 1:
                    total_sign = "+" if total_pl >= 0 else ""
                    lines.append(f"\n💵 Total P/L: {total_sign}{total_pl:.2f} {currency}")

            lines.append(f"\n🕐 {datetime.now().strftime('%H:%M:%S')}")

            keyboard = [tab_row]
            if positions:
                for pos in positions[:5]:
                    ticket = pos.get("ticket", "?")
                    symbol = pos.get("symbol", "?")
                    pos_type = (pos.get("type") or "?").upper()
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f"❌ Close #{ticket} {symbol} {pos_type}",
                                callback_data=f"ud_close_{license_key}_{ticket}",
                            )
                        ]
                    )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "❌ Close All Positions",
                            callback_data=f"ud_closeall_{license_key}",
                        )
                    ]
                )

            keyboard.append(
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"ud_trade_open_{license_key}")]
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "◀️ Back", callback_data=self._user_back_target(update, "ud_trading")
                    )
                ]
            )

            await update.callback_query.edit_message_text(
                "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            try:
                page, total_pages, offset = calc_pagination(page, 10000, _PAGE_SIZE)
                deals = self.db_manager.get_trade_history_for_license(
                    license_key, limit=_PAGE_SIZE, offset=offset
                )
            except Exception as e:
                await update.callback_query.edit_message_text(
                    f"⚠️ Error fetching history: {_sanitize_error(e)}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="ud_trading")]]
                    ),
                )
                return

            lines = [f"📜 *History — {name}*\n{SEP}"]
            if not deals:
                lines.append("\n_No trade history found._")
            else:
                total_pl = 0.0
                for deal in deals:
                    ticket = deal.get("ticket", "?")
                    symbol = deal.get("symbol", "?")
                    deal_type = (deal.get("type") or "?").upper()
                    profit = deal.get("profit", 0) or 0
                    total_pl += profit
                    pl_sign = "+" if profit >= 0 else ""
                    pl_emoji = "📈" if profit >= 0 else "📉"
                    raw_time = deal.get("close_time", deal.get("time", deal.get("open_time", "")))
                    if raw_time and str(raw_time).isdigit():
                        close_time = datetime.fromtimestamp(int(raw_time)).strftime(
                            "%Y-%m-%d %H:%M"
                        )
                    else:
                        close_time = str(raw_time)[:16] if raw_time else ""
                    lines.append(
                        f"{pl_emoji} #{ticket} {_escape_md(symbol, version=1)} "
                        f"{deal_type} | {pl_sign}{profit:.2f} | {close_time}"
                    )

            lines.append(f"\n🕐 {datetime.now().strftime('%H:%M:%S')}")

            keyboard = [tab_row]
            nav = []
            if page > 0:
                nav.append(
                    InlineKeyboardButton(
                        "◀️ Prev",
                        callback_data=f"ud_tradepg_closed_{license_key}_{page - 1}",
                    )
                )
            if len(deals) == _PAGE_SIZE:
                nav.append(
                    InlineKeyboardButton(
                        "Next ▶️",
                        callback_data=f"ud_tradepg_closed_{license_key}_{page + 1}",
                    )
                )
            if nav:
                keyboard.append(nav)
            keyboard.append(
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"ud_trade_closed_{license_key}")]
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "◀️ Back", callback_data=self._user_back_target(update, "ud_trading")
                    )
                ]
            )

            await update.callback_query.edit_message_text(
                "\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    async def _show_user_trading_pick(self, update: Update):
        """Picker for trading view if multiple licenses."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            await update.callback_query.edit_message_text(
                "No linked licenses.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return
        if len(licenses) == 1:
            await self._show_user_trading(update, licenses[0])
            return

        lines = [f"📊 *Trading*\n{SEP}\nSelect a license:"]
        keyboard = []
        for lic in licenses:
            client = self.client_manager.get_client_by_license(lic)
            name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
            keyboard.append(
                [InlineKeyboardButton(f"📊 {name}", callback_data=f"ud_trade_open_{lic}")]
            )
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="user_menu")])
        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    # ── Kill Switch ─────────────────────────────────────────────────────────

    async def _show_user_kill_switch(self, update: Update):
        """Show kill switch menu — emergency controls for all linked licenses."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            await update.callback_query.edit_message_text(
                "No linked licenses.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return

        lines = [f"🚨 *Kill Switch*\n{SEP}\nEmergency controls for your account(s).\n{SEP}"]

        # Summarize connection status for each license
        any_connected = False
        keyboard = []
        now = datetime.now()
        for lic in licenses:
            client = self.client_manager.get_client_by_license(lic)
            name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)
            short = lic[:8] + "..."

            # Check connection
            connected = False
            conn_type = ""
            poll_data = self.http_polling_clients.get(lic)
            if poll_data and poll_data.get("last_poll"):
                if (now - poll_data["last_poll"]).total_seconds() <= 10:
                    connected = True
                    conn_type = "HTTP"
            if self.ws_manager:
                try:
                    ws_count = len(self.ws_manager.get_connections_for_key(lic))
                    if ws_count > 0:
                        connected = True
                        conn_type = f"WS ({ws_count})"
                except Exception:
                    logger.debug(
                        "Failed to get WS connections in kill switch for %s", lic, exc_info=True
                    )

            status = f"Online ({conn_type})" if connected else "Offline"

            # When not connected, check license validity for more specific status
            if not connected:
                valid, msg = self.client_manager.validate_license(lic)
                if not valid:
                    status = "Expired" if "expired" in msg.lower() else "Invalid"

            lines.append(f"• {name} (`{short}`): {status}")

            if connected:
                any_connected = True
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"🔌 Disconnect {name}",
                            callback_data=f"ud_disc_{lic}",
                        )
                    ]
                )

            # Close all positions button
            try:
                positions = self.db_manager.get_latest_open_positions(lic)
                if positions:
                    keyboard.append(
                        [
                            InlineKeyboardButton(
                                f"❌ Close {len(positions)} position(s) — {name}",
                                callback_data=f"ud_closeall_{lic}",
                            )
                        ]
                    )
            except Exception:
                logger.debug(
                    "Failed to get open positions in kill switch for %s", lic, exc_info=True
                )

        if any_connected:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🔴 Disconnect ALL",
                        callback_data="ud_kill_disc_all",
                    )
                ]
            )

        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="user_menu")])
        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _confirm_close_all_positions(self, update: Update, license_key: str):
        """Confirm closing all open positions for a license."""
        client = self.client_manager.get_client_by_license(license_key)
        name = _escape_md(client.get("name", "Unknown") if client else "Unknown", version=1)

        try:
            positions = self.db_manager.get_latest_open_positions(license_key)
        except Exception as e:
            await update.callback_query.edit_message_text(
                f"⚠️ Error fetching positions: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )
            return

        if not positions:
            await update.callback_query.edit_message_text(
                f"No open positions for {name}.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )
            return

        total_pl = sum(p.get("profit", 0) or 0 for p in positions)
        pl_sign = "+" if total_pl >= 0 else ""

        text = (
            f"⚠️ *Close ALL Positions*\n{SEP}\n"
            f"👤 {name}\n"
            f"📊 {len(positions)} position(s)\n"
            f"💵 Total P/L: {pl_sign}{total_pl:.2f}\n\n"
            f"This will send close requests for ALL positions.\n"
            f"Are you sure?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Close All",
                    callback_data=f"ud_closeallok_{license_key}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"ud_trade_open_{license_key}",
                ),
            ],
        ]
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _do_close_all_positions(self, update: Update, license_key: str):
        """Close all open positions for a license via WebSocket."""
        if not self.ws_manager:
            await update.callback_query.edit_message_text(
                "❌ WebSocket manager not available. Cannot close positions.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )
            return

        try:
            positions = self.db_manager.get_latest_open_positions(license_key)
            if not positions:
                await update.callback_query.edit_message_text(
                    "No open positions to close.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                                )
                            ]
                        ]
                    ),
                )
                return

            total_sent = 0
            for pos in positions:
                ticket = pos.get("ticket")
                if ticket:
                    try:
                        count = await request_close_position(
                            self.ws_manager, license_key, int(ticket)
                        )
                        total_sent += count
                    except Exception:
                        logger.debug(
                            "Failed to close position %s for %s", ticket, license_key, exc_info=True
                        )

            await update.callback_query.edit_message_text(
                f"✅ Close requests sent for {len(positions)} position(s).\n"
                f"Connected EAs: {total_sent} received the request(s).\n"
                f"Waiting for EA to execute...",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📊 Check Positions", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ],
                    ]
                ),
            )
        except Exception as e:
            logger.error("Error sending close-all requests: %s", e)
            await update.callback_query.edit_message_text(
                f"⚠️ Error: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )

    async def _confirm_disconnect_all(self, update: Update):
        """Confirm disconnecting ALL EAs for all linked licenses."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            await update.callback_query.edit_message_text(
                "No linked licenses.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return

        # Count connections
        total_http = 0
        total_ws = 0
        now = datetime.now()
        for lic in licenses:
            poll_data = self.http_polling_clients.get(lic)
            if poll_data and poll_data.get("last_poll"):
                if (now - poll_data["last_poll"]).total_seconds() <= 10:
                    total_http += 1
            if self.ws_manager:
                try:
                    total_ws += len(self.ws_manager.get_connections_for_key(lic))
                except Exception:
                    logger.debug(
                        "Failed to count WS connections for disconnect-all %s", lic, exc_info=True
                    )

        total = total_http + total_ws
        if total == 0:
            await update.callback_query.edit_message_text(
                "No active connections to disconnect.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                ),
            )
            return

        text = (
            f"🔴 *Disconnect ALL EAs*\n{SEP}\n"
            f"Active connections: {total_http} HTTP, {total_ws} WebSocket\n"
            f"Licenses affected: {len(licenses)}\n\n"
            f"⚠️ This will disconnect ALL your EAs from the server.\n"
            f"Are you sure?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔴 Disconnect ALL",
                    callback_data="ud_kill_disc_all_ok",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="user_menu",
                ),
            ],
        ]
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _do_disconnect_all(self, update: Update):
        """Disconnect all EAs for all linked licenses."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        disconnected = []

        for lic in licenses:
            # Disconnect HTTP
            had_http = lic in self.http_polling_clients
            if self.conn_manager:
                self.conn_manager.cleanup_client_state(lic)
            else:
                self.http_polling_clients.pop(lic, None)
                self.signal_queues.pop(lic, None)
            if had_http:
                disconnected.append("HTTP")

            # Disconnect WebSocket
            if self.ws_manager:
                try:
                    ws_conns = self.ws_manager.get_connections_for_key(lic)
                    for ws in list(ws_conns):
                        try:
                            await ws.close(code=4002, reason="User kill switch via Telegram")
                        except Exception:
                            logger.debug("Failed to close WS in disconnect-all", exc_info=True)
                    if ws_conns:
                        disconnected.append("WS")
                except Exception:
                    logger.debug("Failed to disconnect WS for %s", lic, exc_info=True)

        methods = ", ".join(set(disconnected)) if disconnected else "none"
        text = (
            f"🔴 *All EAs Disconnected*\n{SEP}\n"
            f"Methods: {methods}\n"
            f"Licenses affected: {len(licenses)}"
        )
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔌 Check Connections", callback_data="ud_account_pick")],
                    [InlineKeyboardButton("◀️ Back", callback_data="user_menu")],
                ]
            ),
        )

    # ── Actions ─────────────────────────────────────────────────────────────

    async def _confirm_close_position(self, update: Update, license_key: str, ticket: str):
        """Confirm closing a single position."""
        try:
            positions = self.db_manager.get_latest_open_positions(license_key)
            pos = next((p for p in positions if str(p.get("ticket")) == str(ticket)), None)
            if not pos:
                await update.callback_query.edit_message_text(
                    "❌ Position not found (may have already been closed).",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                                )
                            ]
                        ]
                    ),
                )
                return

            symbol = pos.get("symbol", "?")
            pos_type = (pos.get("type") or "?").upper()
            vol = pos.get("volume", 0) or 0
            profit = pos.get("profit", 0) or 0
            pl_sign = "+" if profit >= 0 else ""

            text = (
                f"⚠️ *Confirm Close Position*\n{SEP}\n"
                f"#{ticket} {_escape_md(symbol, version=1)} {pos_type} {vol:.2f} lots\n"
                f"P/L: {pl_sign}{profit:.2f}\n\n"
                f"Are you sure you want to close this position?"
            )
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Confirm Close",
                        callback_data=f"ud_closeok_{license_key}_{ticket}",
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"ud_trade_open_{license_key}",
                    ),
                ],
            ]
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error("Error confirming close position: %s", e)
            await update.callback_query.edit_message_text(
                f"⚠️ Error: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )

    async def _do_close_position(self, update: Update, license_key: str, ticket: str):
        """Execute position close via WebSocket request to EA."""
        if not self.ws_manager:
            await update.callback_query.edit_message_text(
                "❌ WebSocket manager not available. Cannot close position.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )
            return

        try:
            count = await request_close_position(self.ws_manager, license_key, int(ticket))
            if count > 0:
                await update.callback_query.edit_message_text(
                    f"✅ Close request sent to EA for ticket #{ticket}.\n"
                    f"Waiting for EA to execute...",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📊 Check Positions",
                                    callback_data=f"ud_trade_open_{license_key}",
                                )
                            ]
                        ]
                    ),
                )
            else:
                await update.callback_query.edit_message_text(
                    f"❌ No connected EA for this license.\n"
                    f"Make sure your EA is running and connected via WebSocket.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                                )
                            ]
                        ]
                    ),
                )
        except Exception as e:
            logger.error("Error sending close request: %s", e)
            await update.callback_query.edit_message_text(
                f"⚠️ Error sending close request: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back", callback_data=f"ud_trade_open_{license_key}"
                            )
                        ]
                    ]
                ),
            )

    async def _confirm_disconnect_ea(self, update: Update, license_key: str):
        """Confirm disconnecting a single EA."""
        http_count = 0
        ws_count = 0
        now = datetime.now()
        poll_data = self.http_polling_clients.get(license_key)
        if poll_data and poll_data.get("last_poll"):
            if (now - poll_data["last_poll"]).total_seconds() <= 10:
                http_count = 1

        if self.ws_manager:
            try:
                ws_count = len(self.ws_manager.get_connections_for_key(license_key))
            except Exception:
                logger.debug(
                    "Failed to get WS count for disconnect confirm %s", license_key, exc_info=True
                )

        if http_count == 0 and ws_count == 0:
            await update.callback_query.edit_message_text(
                "No active connections for this license.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data=f"ud_acct_{license_key}")]]
                ),
            )
            return

        text = (
            f"⚠️ *Confirm Disconnect EA*\n{SEP}\n"
            f"License: `{license_key[:8]}...`\n"
            f"Active connections: {http_count} HTTP, {ws_count} WebSocket\n\n"
            f"This will disconnect the EA from the server. Continue?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Disconnect",
                    callback_data=f"ud_discok_{license_key}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"ud_acct_{license_key}",
                ),
            ],
        ]
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _do_disconnect_ea(self, update: Update, license_key: str):
        """Disconnect EA from both HTTP and WebSocket (does NOT delete signals or unlink user)."""
        disconnected = []

        # Disconnect HTTP polling
        had_http = license_key in self.http_polling_clients
        if self.conn_manager:
            self.conn_manager.cleanup_client_state(license_key)
        else:
            self.http_polling_clients.pop(license_key, None)
            self.signal_queues.pop(license_key, None)
        if had_http:
            disconnected.append("HTTP")

        # Disconnect WebSocket
        if self.ws_manager:
            try:
                ws_conns = self.ws_manager.get_connections_for_key(license_key)
                for ws in list(ws_conns):
                    try:
                        await ws.close(code=4002, reason="User disconnect via Telegram")
                    except Exception:
                        logger.debug("Failed to close WS for %s", license_key, exc_info=True)
                if ws_conns:
                    disconnected.append("WebSocket")
            except Exception:
                logger.debug("Failed to disconnect WS for %s", license_key, exc_info=True)

        methods = ", ".join(disconnected) if disconnected else "none"
        text = f"🔌 *EA Disconnected*\n\nLicense: `{license_key[:8]}...`\nMethod: {methods}"
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Check Connection", callback_data=f"ud_acct_{license_key}"
                        )
                    ]
                ]
            ),
        )
