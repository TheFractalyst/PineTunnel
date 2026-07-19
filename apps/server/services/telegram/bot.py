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

from apps.server.routes.trade_analytics import account_stats_latest, license_stats

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
)
from .helpers import CONNECTED_CLIENT_THRESHOLD_SEC, SEP, _sanitize_error, is_benign_edit_error
from .mixins.auth import AuthMixin
from .mixins.events import EventMixin
from .mixins.menu import MenuMixin
from .mixins.monitoring import MonitoringMixin

logger = logging.getLogger(__name__)

_ADMIN_AUDIT_LOG_MODE = 0o600

_CATCH_ALL_CB_PATTERN = re.compile(
    r"^(?!lic_add$|lic_edit_pick$|lic_expiry_pick$|lic_search$"
    r"|feat_|exp_|addconf_|editf_|edpick_|expick_|expval_)"
)


class PineTunnelTelegramBot(
    AuthMixin,
    MenuMixin,
    MonitoringMixin,
    EventMixin,
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

        self.alerts_enabled = True
        self._load_bot_settings()
        self.app: Application | None = None
        self._started = False

        logger.info("TelegramBot initialized with %d admin(s)", len(self.admin_ids))
        if not self.admin_ids:
            logger.warning(
                "Telegram bot has NO admin IDs configured - all admin commands will be inaccessible!"
            )

    async def start(self):
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN not set - Telegram bot disabled")
            return

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

            try:
                await self.app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                logger.debug("delete_webhook failed (non-critical)", exc_info=True)

            admin_commands = [
                BotCommand("start", "Main menu"),
                BotCommand("menu", "Show main menu"),
                BotCommand("licenses", "License management"),
                BotCommand("monitor", "Server monitoring"),
                BotCommand("signals", "Signal tracking by license"),
                BotCommand("status", "Quick server status"),
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

        try:
            await self.notify_admin("PineTunnel Bot Started - Server is online and ready.")
        except Exception:
            logger.debug("notify_admin failed on startup (bot is running)", exc_info=True)

    async def stop(self):
        if self._started and self.app:
            try:
                await self.notify_admin("PineTunnel Bot Stopping - Server shutting down.")
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
                self._started = False
                logger.info("Telegram bot stopped")
            except Exception as e:
                logger.error("Error stopping Telegram bot: %s", e)

    def _cascade_delete_license(self, license_key: str):
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
            account_stats_latest.pop(license_key, None)
            license_stats.pop(license_key, None)
        except Exception:
            logger.debug("Failed to clean trade analytics cache for %s", license_key, exc_info=True)

    @property
    def _active_license_count(self) -> int:
        return sum(1 for c in self.client_manager.clients.values() if c.get("status") == "active")

    def _count_connected_clients(self) -> int:
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

    def _register_handlers(self):
        app = self.app

        _admin_filter = filters.User(user_id=self.admin_ids) if self.admin_ids else filters.Chat(-1)

        app.add_handler(CommandHandler("start", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("menu", self._cmd_menu, filters=_admin_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=_admin_filter))
        app.add_handler(CommandHandler("monitor", self._cmd_monitor, filters=_admin_filter))
        app.add_handler(CommandHandler("login", self._cmd_login, filters=_admin_filter))

        app.add_handler(CallbackQueryHandler(self._cb_handler, pattern=_CATCH_ALL_CB_PATTERN))
        app.add_error_handler(self._error_handler)

    def _make_conversation(
        self,
        entry: CallbackQueryHandler,
        states: dict,
        timeout_min: int = 10,
    ) -> ConversationHandler:
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

    async def _cb_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_admin(update):
            await query.edit_message_text("Admin access required.")
            return

        data = query.data

        try:
            await self._route_admin_callback(update, context, data)
        except TelegramError as e:
            if is_benign_edit_error(e):
                return
            raise

    async def _route_admin_callback(self, update, context, data):
        if data == "menu_main":
            await self._show_main_menu(update)
        elif data == "menu_monitor":
            await self._show_monitor_menu(update)

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

        elif data == "set_toggle_alerts":
            self.alerts_enabled = not self.alerts_enabled
            self._save_bot_settings()
            await self._show_main_menu(update)

        else:
            logger.warning("Unhandled callback data: %s", data)

    async def _log_admin_action(self, user_id: int, username: str, action: str, details: dict):
        user = f"@{username}" if username else str(user_id)
        enriched = dict(details)
        enriched.setdefault("user_id", user_id)
        enriched.setdefault("username", username)

        if self.admin_logger is not None:
            try:
                self.admin_logger.log_activity(
                    action=action,
                    user=user,
                    details=enriched,
                )
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
        for prefix in CONVERSATION_CLEANUP_PREFIXES:
            keys_to_del = [k for k in context.user_data if k.startswith(prefix)]
            for k in keys_to_del:
                del context.user_data[k]

        msg = update.effective_message
        if msg:
            await msg.reply_text(
                "Operation timed out. Please start over.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Main Menu", callback_data="menu_main")]]
                ),
            )
        return ConversationHandler.END

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_benign_edit_error(context.error):
            logger.debug("Benign Telegram edit error: %s", context.error)
            return

        logger.error("Exception while handling update: %s", context.error, exc_info=context.error)

        sanitized = _sanitize_error(context.error)
        for admin_id in self.admin_ids:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"Bot Error\n\n{sanitized}",
                )
            except Exception:
                logger.error("Failed to send error notification to admin %s", admin_id, exc_info=True)
