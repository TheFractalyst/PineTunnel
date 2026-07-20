"""Menu mixin — main menu commands and screens (admin-only).

Adapted from the reference MenuMixin: the ``/start LICENSE_KEY`` registration flow,
end-user menu, and Subscribe/Support buttons are dropped (admin-only mode, omitted
commercial features). Admins browse the user dashboard via the "🏠 User Dashboard"
button (the reference's ``/onboard`` pattern).
"""

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from apps.server.routes.ea_download import generate_download_url
from ..constants import CONVERSATION_CLEANUP_PREFIXES
from ..helpers import SEP
from ..keyboards import respond

logger = logging.getLogger(__name__)


class MenuMixin:
    """Main menu commands: /start, /menu, /help, /onboard, /download (admin-only)."""

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Admin-only: clear non-conversation state, show admin main menu.
        for key in list(context.user_data.keys()):
            if not any(key.startswith(prefix) for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]
        await self._show_main_menu(update)

    async def _cmd_onboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user menu directly — lets admin browse the non-admin dashboard."""
        if not await self._check_admin(update):
            return
        await self._show_user_menu(update)

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for key in list(context.user_data.keys()):
            if not any(key.startswith(prefix) for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]
        await self._show_main_menu(update)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "🌉 <b>PineTunnel Admin Bot v2.0</b>\n\n"
            "<b>Commands:</b>\n"
            "/menu - Main menu\n"
            "/licenses - License management\n"
            "/monitor - Server monitoring\n"
            "/signals - Signal tracking\n"
            "/status - Quick server status\n"
            "/download - Download EA files\n"
            "/login - Web dashboard login code\n"
            "/cancel - Cancel current operation"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def _show_main_menu(self, update: Update):
        alerts_label = "🔔 Alerts: ON" if self.alerts_enabled else "🔕 Alerts: OFF"
        keyboard = [
            [
                InlineKeyboardButton("📋 Licenses", callback_data="menu_licenses"),
                InlineKeyboardButton("📊 Monitor", callback_data="menu_monitor"),
            ],
            [
                InlineKeyboardButton("📡 Signals", callback_data="menu_signals"),
                InlineKeyboardButton(alerts_label, callback_data="set_toggle_alerts"),
            ],
            [
                InlineKeyboardButton("ℹ️ System Info", callback_data="set_system_info"),
                InlineKeyboardButton("🌐 Webhook", callback_data="set_webhook"),
            ],
            [
                InlineKeyboardButton("🏠 User Dashboard", callback_data="user_menu"),
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
            "🌉 <b>PineTunnel Admin Bot</b>\n"
            f"{SEP}\n"
            f"📋 Licenses: {active_licenses}/{total_licenses} active\n"
            f"🔌 Connected: {connected} | 📨 Pending: {total_pending}\n"
            f"🔔 Alerts: {alerts_status}\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"{SEP}\n"
            "Select a section:"
        )

        await respond(update, text, keyboard)

    async def _show_user_menu(self, update: Update):
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        license_count = len(licenses)

        keyboard = [
            [
                InlineKeyboardButton("💰 Account", callback_data="ud_account_pick"),
                InlineKeyboardButton("📊 Trading", callback_data="ud_trading"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="user_settings"),
                InlineKeyboardButton("🚨 Kill Switch", callback_data="ud_kill"),
            ],
            [InlineKeyboardButton("◀️ Admin Menu", callback_data="menu_main")],
        ]

        # Bot-wide notification status (admin-only mode)
        any_enabled = self.alerts_enabled and any(self.notification_prefs.values())
        notif_status = "Active" if any_enabled else "Muted"

        # Connection status summary across all browsable licenses. Local in-memory
        # state only sees same-worker connections; fall back to the DB
        # ea_connections table (cross-worker single source of truth) when the EA is
        # connected on a different Gunicorn worker.
        connected_count = 0
        not_locally_connected: list[str] = []
        now = datetime.now()
        for lic in licenses:
            poll_data = self.http_polling_clients.get(lic)
            if poll_data and poll_data.get("last_poll"):
                if (now - poll_data["last_poll"]).total_seconds() <= 10:
                    connected_count += 1
                    continue
            if self.ws_manager:
                try:
                    if len(self.ws_manager.get_connections_for_key(lic)) > 0:
                        connected_count += 1
                        continue
                except Exception:
                    logger.debug("Failed to check WS connection for %s", lic, exc_info=True)
            not_locally_connected.append(lic)

        if (
            not_locally_connected
            and self.db_manager
            and hasattr(self.db_manager, "get_active_ea_connections")
        ):
            try:
                active_keys = {
                    c["license_key"] for c in self.db_manager.get_active_ea_connections()
                }
                for lic in not_locally_connected:
                    if lic in active_keys:
                        connected_count += 1
            except Exception:
                logger.debug("Failed to check DB EA connections", exc_info=True)

        # Subscription status (first license with an expiry)
        sub_line = ""
        try:
            for lic in licenses:
                client = self.client_manager.get_client_by_license(lic)
                if client and client.get("expires_at"):
                    expiry_dt = datetime.fromisoformat(client["expires_at"])
                    days_left = (expiry_dt - now).days
                    if days_left > 0:
                        sub_line = f"📅 {days_left}d left | "
                    else:
                        sub_line = "⚠️ Expired | "
                    break
        except Exception:
            logger.debug("Failed to get subscription status for user menu", exc_info=True)

        text = (
            "🌉 <b>PineTunnel Dashboard</b>\n"
            f"{SEP}\n"
            f"📋 Licenses: {license_count} | 🔌 Connected: {connected_count}/{license_count}\n"
            f"{sub_line}🔔 Alerts: {notif_status}\n"
            f"{SEP}"
        )
        await respond(update, text, keyboard)

    async def _cmd_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generate persistent EA download links for both MT5 and MT4."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        licenses = self._get_licenses_for_chat_id(chat_id)

        if not licenses:
            await update.message.reply_text("No licenses configured on the server.")
            return

        mt5_url = generate_download_url(user_id, "mt5")
        mt4_url = generate_download_url(user_id, "mt4")

        keyboard = [
            [InlineKeyboardButton("MT5 Download", url=mt5_url)],
            [InlineKeyboardButton("MT4 Download", url=mt4_url)],
            [InlineKeyboardButton("Back to Menu", callback_data="user_menu")],
        ]

        await update.message.reply_text(
            f"<b>Download EA</b>\n"
            f"{SEP}\n"
            f"Choose your platform (zip includes EA + DLL):\n\n"
            f"<b>MT5</b> - PineTunnel_EA.ex5 + PTWebSocket.dll (64-bit)\n"
            f"<b>MT4</b> - PineTunnel_EA_MT4.ex4 + PTWebSocket32.dll (32-bit)\n\n"
            f"Links are permanent and tied to your account.\n"
            f"Bookmark them for future downloads.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
