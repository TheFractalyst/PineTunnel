"""PineTunnel Telegram bot — admin-only, mixin-composed.

Ported from the reference repo's mixin architecture to match its menu/dashboard
logic and interface 1:1. Admin-only adaptation: no end-user registration,
``telegram_users``, per-user settings, Subscribe (NOWPayments), or Support (AI
chat). Admins browse the user dashboard via the "🏠 User Dashboard" button.

Public enhancements retained: ``auth_store`` / ``/login`` web-dashboard login
codes, ``admin_logger``-aware audit logging, ``_test_env`` Telegram test
environment, and simple ``notify_admin`` + ``on_*`` event hooks (the reference's
NotificationEngine is not ported — out of scope for menu/dashboard).
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown as _escape_md

from apps.server.services.notification import DEFAULT_PREFS

from .constants import (
    ADD_LIC_CONFIRM,
    ADD_LIC_EMAIL,
    ADD_LIC_EXPIRY,
    ADD_LIC_FEATURES,
    ADD_LIC_NAME,
    CONVERSATION_CLEANUP_PREFIXES,
    EDIT_LIC_FIELD,
    EDIT_LIC_PICK,
    EDIT_LIC_VALUE,
    EXPIRY_PICK,
    EXPIRY_VALUE,
    SEARCH_QUERY,
    USER_QH_INPUT,
    WEBHOOK_URL_CONFIRM,
    WEBHOOK_URL_INPUT,
)
from .helpers import CONNECTED_CLIENT_THRESHOLD_SEC, SEP, _sanitize_error, is_benign_edit_error
from .mixins.auth import AuthMixin, DEFAULT_QUIET_HOURS
from .mixins.conversations import ConversationMixin
from .mixins.licenses import LicenseMixin
from .mixins.menu import MenuMixin
from .mixins.monitoring import MonitoringMixin
from .mixins.settings import SettingsMixin
from .mixins.signals import SignalMixin
from .mixins.user_dashboard import UserDashboardMixin
from .mixins.webhook import WebhookMixin

logger = logging.getLogger(__name__)

_ADMIN_AUDIT_LOG_MODE = 0o600

# Catch-all callback pattern: excludes conversation-owned prefixes so the
# ConversationHandlers (registered first) receive their entry callbacks.
_CATCH_ALL_CB_PATTERN = re.compile(
    r"^(?!lic_add$|lic_edit_pick$|lic_expiry_pick$|lic_search$"
    r"|feat_|exp_|addconf_|editf_|edpick_|expick_|expval_"
    r"|qh_set_(start|end)$"
    r"|set_webhook_edit$|set_webhook_confirm_)"
)


class PineTunnelTelegramBot(
    AuthMixin,
    MenuMixin,
    LicenseMixin,
    ConversationMixin,
    MonitoringMixin,
    SignalMixin,
    SettingsMixin,
    UserDashboardMixin,
    WebhookMixin,
):
    """Admin-only Telegram bot for PineTunnel management."""

    def __init__(
        self,
        token: str,
        admin_ids: list[int],
        client_manager,
        db_manager,
        data_dir: str,
        http_polling_clients: dict | None = None,
        signal_queues: dict | None = None,
        conn_manager=None,
        ws_manager=None,
        test_env: bool = False,
        auth_store=None,
        admin_logger=None,
    ):
        self.token = token
        self.admin_ids = admin_ids
        self.client_manager = client_manager
        self.db_manager = db_manager
        self.data_dir = data_dir
        self.http_polling_clients = http_polling_clients or {}
        self.signal_queues = signal_queues or {}
        self.conn_manager = conn_manager
        self.ws_manager = ws_manager
        self._test_env = test_env
        self._auth_store = auth_store
        self.admin_logger = admin_logger

        self.alerts_enabled = True  # default; overridden by _load_bot_settings
        self.notification_prefs: dict = dict(DEFAULT_PREFS)
        self.quiet_hours: dict = dict(DEFAULT_QUIET_HOURS)
        self._load_bot_settings()
        self.app: Application | None = None
        self._started = False

        logger.info("TelegramBot initialized with %d admin(s)", len(self.admin_ids))
        if not self.admin_ids:
            logger.warning(
                "Telegram bot has NO admin IDs configured — all admin commands will be inaccessible!"
            )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self):
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")
            return

        # If a previous app exists (e.g. watchdog restart), shut it down first
        # to avoid resource leaks and 409 Conflict from dual polling.
        if self.app is not None:
            try:
                if self._started:
                    await self.app.updater.stop()
                    await self.app.stop()
                await self.app.shutdown()
            except Exception:
                logger.debug("Failed to clean up previous app instance", exc_info=True)
            self.app = None
            self._started = False

        try:
            logger.info("Initializing Telegram bot...")
            token = self.token
            if getattr(self, "_test_env", False):
                token = self.token + "/test"
                logger.info("Using Telegram TEST environment")
            self.app = (
                Application.builder()
                .token(token)
                .rate_limiter(AIORateLimiter(max_retries=3))
                .concurrent_updates(False)
                .build()
            )

            self._register_handlers()

            await self.app.initialize()

            # Clear any previous polling/webhook state to prevent 409 Conflict
            # when a previous instance's polling connection hasn't expired yet.
            try:
                await self.app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                logger.debug("delete_webhook failed (non-critical)", exc_info=True)

            admin_commands = [
                BotCommand("start", "Main menu"),
                BotCommand("onboard", "User dashboard (non-admin view)"),
                BotCommand("menu", "Show main menu"),
                BotCommand("licenses", "License management"),
                BotCommand("monitor", "Server monitoring"),
                BotCommand("signals", "Signal tracking by license"),
                BotCommand("status", "Quick server status"),
                BotCommand("download", "Download EA files"),
                BotCommand("login", "Web dashboard login code"),
                BotCommand("help", "Show help"),
            ]
            for admin_id in self.admin_ids:
                try:
                    await self.app.bot.set_my_commands(
                        admin_commands,
                        scope=BotCommandScopeChat(admin_id),
                    )
                except Exception:
                    logger.debug("Failed to set admin commands for %s", admin_id, exc_info=True)

            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)

            self._started = True
            logger.info("Telegram bot started successfully")

        except Exception as e:
            logger.error("Failed to start Telegram bot: %s", e, exc_info=True)
            if self.app is not None:
                try:
                    await self.app.stop()
                    await self.app.shutdown()
                except Exception:
                    logger.debug("Failed to clean up app after start failure", exc_info=True)
            self._started = False
            self.app = None
            return

        # Notify admin OUTSIDE the try/except so a notification failure
        # does not reset _started (which would cause the watchdog to
        # restart the bot and trigger a 409 Conflict cascade).
        try:
            await self.notify_admin("🟢 *PineTunnel Bot Started*\nServer is online and ready.")
        except Exception:
            logger.debug("notify_admin failed on startup (bot is running)", exc_info=True)

    async def stop(self):
        if self._started and self.app:
            try:
                await self.notify_admin("🔴 *PineTunnel Bot Stopping*\nServer shutting down.")
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
                self._started = False
                logger.info("Telegram bot stopped")
            except Exception as e:
                logger.error("Error stopping Telegram bot: %s", e)

    # ── Multi-license helpers ──────────────────────────────────────────────

    def _get_licenses_for_chat_id(self, chat_id: int) -> list[str]:
        """Admin-only: admins can browse every license, so return all license keys
        whose client still exists in the client manager."""
        return [
            lic
            for lic in self.client_manager.clients
            if self.client_manager.get_client_by_license(lic) is not None
        ]

    def _cascade_delete_license(self, license_key: str):
        """Remove a license's EA state, signals, and cached analytics after deletion."""
        if self.conn_manager:
            self.conn_manager.cleanup_client_state(license_key)
        else:
            self.http_polling_clients.pop(license_key, None)
            self.signal_queues.pop(license_key, None)

        try:
            self.db_manager.delete_signals_by_license(license_key)
        except Exception:
            logger.debug("Failed to delete signals for %s", license_key, exc_info=True)

        try:
            from apps.server.db.analytics_store import account_stats_latest, license_stats

            account_stats_latest.pop(license_key, None)
            license_stats.pop(license_key, None)
        except Exception:
            logger.debug(
                "Failed to clean trade analytics cache for %s", license_key, exc_info=True
            )

    def _cascade_deactivate_license(self, license_key: str):
        """Disconnect EA and clear queues for a deactivated license."""
        if self.conn_manager:
            self.conn_manager.cleanup_client_state(license_key)
        else:
            self.http_polling_clients.pop(license_key, None)
            self.signal_queues.pop(license_key, None)

    @property
    def _active_license_count(self) -> int:
        """Count of active licenses across all clients."""
        return sum(1 for c in self.client_manager.clients.values() if c.get("status") == "active")

    def _count_connected_clients(self) -> int:
        """Count unique licenses with an active EA connection (HTTP or WebSocket)."""
        now = datetime.now()
        connected_keys: set[str] = set()

        for key, poll_data in self.http_polling_clients.items():
            if (
                poll_data.get("last_poll")
                and (now - poll_data["last_poll"]).total_seconds() <= CONNECTED_CLIENT_THRESHOLD_SEC
            ):
                connected_keys.add(key)

        if self.ws_manager:
            try:
                for lic_key in self.ws_manager.get_connected_license_keys():
                    connected_keys.add(lic_key)
            except Exception:
                logger.debug("Failed to get WS license keys for connected count", exc_info=True)

        return len(connected_keys)

    # ── Handler Registration ───────────────────────────────────────────────

    def _make_conversation(
        self,
        entry: CallbackQueryHandler,
        states: dict,
        timeout_min: int = 10,
    ) -> ConversationHandler:
        """Build a ConversationHandler with shared fallback/timeout boilerplate."""
        states[ConversationHandler.TIMEOUT] = [
            MessageHandler(filters.ALL, self._conversation_timeout_handler)
        ]
        return ConversationHandler(
            entry_points=[entry],
            states=states,
            fallbacks=[
                CommandHandler("cancel", self._cancel_conversation),
                CommandHandler("start", self._cancel_conversation),
            ],
            per_message=False,
            conversation_timeout=timedelta(minutes=timeout_min),
        )

    def _register_handlers(self):
        app = self.app

        # Admin-only: filter all commands so non-admins can't trigger them.
        _admin_filter = filters.User(user_id=self.admin_ids) if self.admin_ids else filters.Chat(-1)

        app.add_handler(CommandHandler("start", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("onboard", self._cmd_onboard, filters=_admin_filter))
        app.add_handler(CommandHandler("menu", self._cmd_menu, filters=_admin_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=_admin_filter))
        app.add_handler(CommandHandler("login", self._cmd_login, filters=_admin_filter))
        app.add_handler(CommandHandler("licenses", self._cmd_licenses, filters=_admin_filter))
        app.add_handler(CommandHandler("monitor", self._cmd_monitor, filters=_admin_filter))
        app.add_handler(CommandHandler("signals", self._cmd_signals, filters=_admin_filter))
        app.add_handler(CommandHandler("status", self._cmd_quick_status, filters=_admin_filter))
        app.add_handler(CommandHandler("download", self._cmd_download, filters=_admin_filter))

        # Add License conversation
        _add_lic_entry = CallbackQueryHandler(self._add_lic_start, pattern="^lic_add$")
        app.add_handler(
            self._make_conversation(
                _add_lic_entry,
                {
                    ADD_LIC_NAME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_lic_name),
                        _add_lic_entry,
                    ],
                    ADD_LIC_EMAIL: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_lic_email),
                        _add_lic_entry,
                    ],
                    ADD_LIC_FEATURES: [
                        CallbackQueryHandler(self._add_lic_features, pattern="^feat_"),
                        _add_lic_entry,
                    ],
                    ADD_LIC_EXPIRY: [
                        CallbackQueryHandler(self._add_lic_expiry, pattern="^exp_"),
                        _add_lic_entry,
                    ],
                    ADD_LIC_CONFIRM: [
                        CallbackQueryHandler(self._add_lic_confirm, pattern="^addconf_"),
                        _add_lic_entry,
                    ],
                },
            )
        )

        # Edit License picker conversation
        _edit_entry = CallbackQueryHandler(self._edit_lic_picker_start, pattern="^lic_edit_pick$")
        app.add_handler(
            self._make_conversation(
                _edit_entry,
                {
                    EDIT_LIC_PICK: [
                        CallbackQueryHandler(self._edit_lic_picker_select, pattern="^edpick_"),
                        _edit_entry,
                    ],
                    EDIT_LIC_FIELD: [
                        CallbackQueryHandler(self._edit_lic_field, pattern="^editf_"),
                        _edit_entry,
                    ],
                    EDIT_LIC_VALUE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._edit_lic_value),
                        _edit_entry,
                    ],
                },
            )
        )

        # Expiry picker conversation
        _expiry_entry = CallbackQueryHandler(self._expiry_picker_start, pattern="^lic_expiry_pick$")
        app.add_handler(
            self._make_conversation(
                _expiry_entry,
                {
                    EXPIRY_PICK: [
                        CallbackQueryHandler(self._expiry_picker_select, pattern="^expick_"),
                        _expiry_entry,
                    ],
                    EXPIRY_VALUE: [
                        CallbackQueryHandler(self._expiry_value, pattern="^expval_"),
                        _expiry_entry,
                    ],
                },
            )
        )

        # Search conversation
        _search_entry = CallbackQueryHandler(self._search_start, pattern="^lic_search$")
        app.add_handler(
            self._make_conversation(
                _search_entry,
                {
                    SEARCH_QUERY: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._search_query),
                        _search_entry,
                    ],
                },
            )
        )

        # Quiet Hours time input conversation
        app.add_handler(
            self._make_conversation(
                CallbackQueryHandler(self._qh_time_start, pattern="^qh_set_(start|end)$"),
                {
                    USER_QH_INPUT: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._qh_time_input)
                    ],
                },
                timeout_min=5,
            )
        )

        # Webhook endpoint URL edit conversation
        app.add_handler(
            self._make_conversation(
                CallbackQueryHandler(self._webhook_edit_start, pattern="^set_webhook_edit$"),
                {
                    WEBHOOK_URL_INPUT: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._webhook_url_input)
                    ],
                    WEBHOOK_URL_CONFIRM: [
                        CallbackQueryHandler(
                            self._webhook_url_confirm, pattern="^set_webhook_confirm_(yes|no)$"
                        )
                    ],
                },
                timeout_min=5,
            )
        )

        # Catch-all callback handler (excludes conversation-owned patterns)
        app.add_handler(CallbackQueryHandler(self._cb_handler, pattern=_CATCH_ALL_CB_PATTERN))

        app.add_error_handler(self._error_handler)

    # ── Callback Router ────────────────────────────────────────────────────

    async def _cb_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_admin(update):
            await query.edit_message_text("Not authorized.")
            return

        data = query.data
        try:
            await self._route_admin_callback(update, context, data)
        except TelegramError as e:
            if is_benign_edit_error(e):
                return
            raise

    async def _route_admin_callback(self, update, context, data):
        """Dispatch admin callback actions. Wrapped by _cb_handler for TelegramError safety."""
        # Main menu navigation
        if data == "menu_main":
            await self._show_main_menu(update)
        elif data == "menu_licenses":
            await self._show_licenses_menu(update)
        elif data == "menu_monitor":
            await self._show_monitor_menu(update)
        elif data == "menu_signals":
            await self._show_signals_menu(update)

        # License actions
        elif data == "lic_list":
            await self._show_license_list(update, context)
        elif data.startswith("lic_info_"):
            await self._show_license_detail(update, data.replace("lic_info_", ""))
        elif data.startswith("lic_activate_"):
            await self._toggle_license(update, data.replace("lic_activate_", ""), "active")
        elif data.startswith("lic_deactivate_"):
            await self._toggle_license(update, data.replace("lic_deactivate_", ""), "inactive")
        elif data.startswith("lic_delconf_"):
            await self._delete_confirm(update, data.replace("lic_delconf_", ""))
        elif data.startswith("lic_dodel_"):
            await self._do_delete_license(update, data.replace("lic_dodel_", ""))
        elif data == "lic_delcancel":
            await self._show_licenses_menu(update)
        elif data.startswith("lic_page_"):
            try:
                page = int(data.replace("lic_page_", ""))
            except ValueError:
                page = 0
            await self._show_license_list(update, context, page=page)

        # Bulk operations
        elif data == "lic_bulk_deactivate_expired":
            await self._bulk_deactivate_expired(update)
        elif data == "lic_bulk_activate_all":
            await self._bulk_activate_all(update)
        elif data.startswith("lic_force_disconnect_"):
            await self._force_disconnect_client(update, data.replace("lic_force_disconnect_", ""))

        # Monitor actions
        elif data == "mon_status":
            await self._show_status(update)
        elif data == "mon_connections":
            await self._show_connections(update)
        elif data == "mon_account_stats":
            await self._show_account_stats(update)
        elif data == "mon_logs":
            await self._show_logs(update)
        elif data == "log_webhook":
            await self._show_logs(update, log_filter="webhook")
        elif data == "log_admin":
            await self._show_logs(update, log_filter="admin")
        elif data == "log_conn":
            await self._show_logs(update, log_filter="conn")
        elif data.startswith("whlog_page_"):
            try:
                page = int(data.replace("whlog_page_", ""))
            except ValueError:
                page = 0
            await self._show_logs(update, log_filter="webhook", page=page)
        elif data.startswith("audit_page_"):
            try:
                page = int(data.replace("audit_page_", ""))
            except ValueError:
                page = 0
            await self._show_logs(update, log_filter="admin", page=page)
        elif data.startswith("conn_page_"):
            try:
                page = int(data.replace("conn_page_", ""))
            except ValueError:
                page = 0
            await self._show_logs(update, log_filter="conn", page=page)
        elif data.startswith("mon_conn_detail_"):
            await self._show_connection_detail(update, data.replace("mon_conn_detail_", ""))

        # Signal tracking actions
        elif data == "sig_menu":
            await self._show_signals_menu(update)
        elif data.startswith("sig_lic_"):
            await self._show_license_signals_overview(update, data[8:])
        elif data.startswith("sig_v_"):
            await self._show_signal_list_from_callback(update, data[6:])
        elif data.startswith("sig_d_"):
            await self._show_signal_detail(update, data[6:])
        elif data.startswith("sig_pg_"):
            try:
                page = int(data.replace("sig_pg_", ""))
            except ValueError:
                page = 0
            await self._show_signals_license_picker(update, page=page)

        # Settings
        elif data == "set_toggle_alerts":
            self.alerts_enabled = not self.alerts_enabled
            self._save_bot_settings()
            await self._show_main_menu(update)
        elif data == "set_system_info":
            await self._show_system_info(update)
        elif data == "set_webhook":
            await self._show_webhook_screen(update)

        # User dashboard / settings (admin browses the user view)
        elif (
            data == "my_notifications"
            or data == "user_menu"
            or data.startswith("user_")
            or data.startswith("qh_")
            or data.startswith("ud_")
        ):
            await self._user_cb_handler(update, data, context)

        else:
            logger.warning("Unhandled callback data in _cb_handler: %s", data)

    # ── User Callback Handler (admin browsing the user dashboard) ──────────

    async def _user_cb_handler(self, update: Update, data: str, context=None):
        """Route callbacks for the user-dashboard view (admins in admin-only mode)."""
        try:
            if data in ("user_menu", "menu_main"):
                await self._show_user_menu(update)
            elif data == "user_settings":
                await self._show_user_settings(update)
            elif data == "user_notif_settings":
                await self._show_notif_presets(update)
            elif data == "user_notif_custom":
                await self._show_user_notif_settings(update)
            elif data.startswith("user_preset_"):
                await self._apply_notif_preset(update, data.replace("user_preset_", ""))
            elif data.startswith("user_toggle_"):
                await self._toggle_user_notif(update, data.replace("user_toggle_", ""))
            elif data == "user_licenses":
                await self._show_user_licenses(update)
            elif data == "user_quiet_hours":
                await self._show_user_quiet_hours(update)
            elif data.startswith("qh_"):
                await self._handle_quiet_hours_cb(update, data)

            # ── User Dashboard callbacks ──
            elif data == "ud_account_pick":
                await self._show_user_account_pick(update)
            elif data.startswith("ud_acct_"):
                await self._show_user_account(update, data[8:])
            elif data == "ud_trading":
                await self._show_user_trading_pick(update)
            elif data.startswith("ud_trade_open_"):
                await self._show_user_trading(update, data[14:], "open")
            elif data.startswith("ud_trade_closed_"):
                await self._show_user_trading(update, data[16:], "closed")
            elif data.startswith("ud_tradepg_closed_"):
                parts = data[19:].rsplit("_", 1)
                if len(parts) == 2:
                    await self._show_user_trading(update, parts[0], "closed", int(parts[1]))
            elif data == "ud_sig_pick":
                await self._show_user_signal_pick(update)
            elif data.startswith("ud_sig_") and not data.startswith("ud_sigpg_"):
                await self._show_user_signals(update, data[7:])
            elif data.startswith("ud_sigpg_"):
                parts = data[9:].rsplit("_", 1)
                if len(parts) == 2:
                    await self._show_user_signals(update, parts[0], int(parts[1]))
            elif data.startswith("ud_close_") and not data.startswith("ud_closeok_"):
                # ud_close_{license_key}_{ticket}
                parts = data[9:].split("_", 1)
                if len(parts) == 2:
                    await self._confirm_close_position(update, parts[0], parts[1])
            elif data.startswith("ud_closeok_"):
                parts = data[11:].split("_", 1)
                if len(parts) == 2:
                    await self._do_close_position(update, parts[0], parts[1])
            elif data.startswith("ud_disc_") and not data.startswith("ud_discok_"):
                # ud_disc_{license_key}
                await self._confirm_disconnect_ea(update, data[8:])
            elif data.startswith("ud_discok_"):
                await self._do_disconnect_ea(update, data[9:])

            # ── Kill Switch callbacks ──
            elif data == "ud_kill":
                await self._show_user_kill_switch(update)
            elif data.startswith("ud_closeall_") and not data.startswith("ud_closeallok_"):
                await self._confirm_close_all_positions(update, data[12:])
            elif data.startswith("ud_closeallok_"):
                await self._do_close_all_positions(update, data[14:])
            elif data == "ud_kill_disc_all":
                await self._confirm_disconnect_all(update)
            elif data == "ud_kill_disc_all_ok":
                await self._do_disconnect_all(update)

            else:
                logger.warning("Unhandled user callback: %s", data)
        except TelegramError as e:
            if is_benign_edit_error(e):
                return
            raise
        except Exception as e:
            logger.error("Error in _user_cb_handler for %s: %s", data, e, exc_info=True)
            try:
                await update.callback_query.edit_message_text(
                    f"⚠️ Error: {_sanitize_error(e)}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="user_menu")]]
                    ),
                )
            except Exception:
                logger.debug("Failed to show error message to user", exc_info=True)

    async def _show_user_settings(self, update: Update):
        """Settings sub-menu: alerts, quiet hours, credentials."""
        keyboard = [
            [
                InlineKeyboardButton("🔔 Alerts", callback_data="user_notif_settings"),
                InlineKeyboardButton("🌙 Quiet Hours", callback_data="user_quiet_hours"),
            ],
            [InlineKeyboardButton("🔑 Credentials", callback_data="user_licenses")],
            [InlineKeyboardButton("◀️ Back", callback_data="user_menu")],
        ]
        await update.callback_query.edit_message_text(
            "⚙️ *Settings*\n" f"{SEP}\n" "Manage your alerts and credentials.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Issue a web-dashboard login code for the admin."""
        user = update.effective_user
        if user is None:
            return
        store = getattr(self, "_auth_store", None)
        if store is None:
            await update.message.reply_text("Web dashboard auth not configured.")
            return
        code = await store.issue_code_async(user.id)
        await update.message.reply_text(
            f"Your PineTunnel dashboard login code:\n\n"
            f"{code}\n\n"
            f"Expires in 90 seconds. Do not share it."
        )

    # ── Cross-cutting Handlers ─────────────────────────────────────────────

    async def _log_admin_action(self, user_id: int, username: str, action: str, details: dict):
        user = f"@{username}" if username else str(user_id)
        enriched = dict(details)
        enriched.setdefault("user_id", user_id)
        enriched.setdefault("username", username)

        if self.admin_logger is not None:
            try:
                self.admin_logger.log_activity(action=action, user=user, details=enriched)
                return
            except Exception as e:
                logger.error("Failed to write audit log via admin_logger: %s", e)

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "username": username,
            "action": action,
            "details": details,
        }
        log_file = os.path.join(self.data_dir, "admin_audit.log")
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(log_file, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
            os.chmod(log_file, _ADMIN_AUDIT_LOG_MODE)
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)

    async def _conversation_timeout_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        # Clean up any orphaned conversation state
        for prefix in CONVERSATION_CLEANUP_PREFIXES:
            keys_to_del = [k for k in context.user_data if k.startswith(prefix)]
            for k in keys_to_del:
                del context.user_data[k]

        msg = update.effective_message
        if msg:
            await msg.reply_text(
                "⏱️ Operation timed out due to inactivity. Please start over.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]
                ),
            )
        return ConversationHandler.END

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_benign_edit_error(context.error):
            logger.debug("Benign Telegram edit error (not modified/not found): %s", context.error)
            return

        logger.error("Exception while handling update: %s", context.error, exc_info=context.error)

        sanitized = _sanitize_error(context.error)
        for admin_id in self.admin_ids:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🚨 *Bot Error*\n\n```\n{sanitized}\n```",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                logger.error(
                    "Failed to send error notification to admin %s", admin_id, exc_info=True
                )

    # ── Notifications (simple admin push; NotificationEngine not ported) ───

    async def notify_admin(self, message: str):
        if not self._started or not self.app:
            return
        for admin_id in self.admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id, text=message, parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)

    def _should_notify(self, pref_key: str) -> bool:
        if not self.alerts_enabled:
            return False
        return self.notification_prefs.get(pref_key, False)

    async def on_trade_executed(self, report):
        if not self._should_notify("exec_success"):
            return
        try:
            await self.notify_admin(
                f"✅ Trade Executed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Side: {report.side}\n"
                f"Volume: {report.volume}"
            )
        except Exception as e:
            logger.error("Trade executed notification error: %s", e)

    async def on_trade_execution_failed(self, report):
        if not self._should_notify("exec_failed"):
            return
        try:
            await self.notify_admin(
                f"❌ Trade Execution Failed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Error: {_escape_md(str(report.error))}"
            )
        except Exception as e:
            logger.error("Trade execution failed notification error: %s", e)

    async def on_position_closed(self, report):
        if not self._should_notify("position_closed"):
            return
        try:
            await self.notify_admin(
                f"📊 Position Closed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Profit: {report.profit}"
            )
        except Exception as e:
            logger.error("Position closed notification error: %s", e)

    async def on_trade_failure(self, license_key: str, error: str):
        if not self._should_notify("exec_failed"):
            return
        await self.notify_admin(
            f"❌ Trade Failure\n"
            f"License: {license_key}\n"
            f"Error: {_escape_md(error)}"
        )
