import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from apps.server.routes.ea_download import generate_download_url
from ..constants import CONVERSATION_CLEANUP_PREFIXES
from ..helpers import SEP

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
