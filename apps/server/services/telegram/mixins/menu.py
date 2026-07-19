import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown as _escape_md

from apps.server.routes.ea_download import generate_download_url
from ..constants import CONVERSATION_CLEANUP_PREFIXES
from ..helpers import SEP, calc_pagination, format_license_info, mask_secret
from ..keyboards import respond

logger = logging.getLogger(__name__)


class MenuMixin:
    """Admin menu commands: /start, /menu, /help, /download."""

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for key in list(context.user_data.keys()):
            if not any(key.startswith(prefix) for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]
        await self._show_main_menu(update)

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for key in list(context.user_data.keys()):
            if not any(key.startswith(prefix) for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]
        await self._show_main_menu(update)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "<b>PineTunnel Admin Bot</b>\n\n"
            "<b>Commands:</b>\n"
            "/menu - Main menu\n"
            "/licenses - License management\n"
            "/monitor - Server monitoring\n"
            "/signals - Signal tracking\n"
            "/status - Quick server status\n"
            "/download - Download EA files\n"
            "/cancel - Cancel current operation\n\n"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def _cmd_licenses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        await self._show_licenses(update, page=0)

    async def _show_licenses(self, update: Update, page: int = 0):
        from ..helpers import LICENSE_PAGE_SIZE

        clients = self.client_manager.clients
        total = len(clients)
        page, total_pages, start = calc_pagination(page, total, LICENSE_PAGE_SIZE)
        keys = list(clients.keys())[start : start + LICENSE_PAGE_SIZE]

        active = self._active_license_count
        connected = self._count_connected_clients()

        lines = [
            f"[L] *Licenses* ({active}/{total} active, {connected} connected)",
            f"{SEP}",
            f"Page {page + 1}/{total_pages} - {total} total",
            "",
        ]

        for key in keys:
            data = clients[key]
            name = _escape_md(data.get("name", "Unknown"), version=1)
            email = _escape_md(data.get("email", "N/A"), version=1)
            status = data.get("status", "unknown")
            status_icon = "[OK]" if status == "active" else "[X]"
            enabled = data.get("enabled", True)
            if not enabled:
                status_icon = "[X]"
                status = "disabled"

            ws_conns = 0
            if self.ws_manager:
                try:
                    ws_conns = self.ws_manager.get_connection_count(key)
                except Exception:
                    pass

            total_trades = 0
            try:
                from apps.server.routes.trade_analytics import license_stats

                stats = license_stats.get(key)
                if stats:
                    total_trades = stats.get("total_trades", 0)
            except Exception:
                pass

            expires_at = data.get("expires_at")
            if expires_at and len(str(expires_at)) > 10:
                exp_str = str(expires_at)[:10]
            else:
                exp_str = "Lifetime"

            last_activity = data.get("last_activity")
            if last_activity:
                last_str = str(last_activity)[:16]
            else:
                last_str = "never"

            lines.append(
                f"{status_icon} {name} (`{key[:8]}...`)\n"
                f"  | Email: {email}\n"
                f"  | Status: {status} | Expires: {exp_str}\n"
                f"  | EAs: {ws_conns} | Trades: {total_trades}\n"
                f"  | Secret: `{mask_secret(data.get('secret_key', ''))}`\n"
                f"  | Last: {last_str}"
            )

        keyboard = []
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("<= Prev", callback_data=f"lic_page_{page - 1}"))
        if total > (page + 1) * LICENSE_PAGE_SIZE:
            nav.append(InlineKeyboardButton("Next >", callback_data=f"lic_page_{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(" Refresh", callback_data="menu_licenses")])
        keyboard.append([InlineKeyboardButton("<= Main Menu", callback_data="menu_main")])

        await respond(update, "\n".join(lines), keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _show_main_menu(self, update: Update):
        alerts_label = "Alerts: ON" if self.alerts_enabled else "Alerts: OFF"
        keyboard = [
            [
                InlineKeyboardButton("Licenses", callback_data="menu_licenses"),
                InlineKeyboardButton("Monitor", callback_data="menu_monitor"),
            ],
            [
                InlineKeyboardButton("Signals", callback_data="menu_signals"),
                InlineKeyboardButton(alerts_label, callback_data="set_toggle_alerts"),
            ],
            [
                InlineKeyboardButton("System Info", callback_data="set_system_info"),
            ],
        ]

        total_licenses = len(self.client_manager.clients)
        active_licenses = self._active_license_count

        connected = self._count_connected_clients()

        total_pending = 0
        try:
            for key in self.client_manager.clients:
                count = self.db_manager.get_signal_count(key, "pending")
                total_pending += count
        except Exception:
            logger.debug("Failed to count pending signals for main menu", exc_info=True)

        alerts_status = "ON" if self.alerts_enabled else "OFF"
        text = (
            "<b>PineTunnel Admin Bot</b>\n"
            f"{SEP}\n"
            f"Licenses: {active_licenses}/{total_licenses} active\n"
            f"Connected: {connected} | Pending: {total_pending}\n"
            f"Alerts: {alerts_status}\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"{SEP}\n"
            "Select a section:"
        )

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        ) if update.callback_query else await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generate EA download links for admin."""
        user_id = update.effective_user.id

        mt5_url = generate_download_url(user_id, "mt5")
        mt4_url = generate_download_url(user_id, "mt4")

        keyboard = [
            [InlineKeyboardButton("MT5 Download", url=mt5_url)],
            [InlineKeyboardButton("MT4 Download", url=mt4_url)],
            [InlineKeyboardButton("Back to Menu", callback_data="menu_main")],
        ]

        await update.message.reply_text(
            f"<b>Download EA</b>\n"
            f"{SEP}\n"
            f"Choose your platform (zip includes EA + DLL):\n\n"
            f"<b>MT5</b> - PineTunnel_EA.ex5 + PTWebSocket.dll (64-bit)\n"
            f"<b>MT4</b> - PineTunnel_EA_MT4.ex4 + PTWebSocket32.dll (32-bit)\n\n"
            f"Links are permanent and tied to your account.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
