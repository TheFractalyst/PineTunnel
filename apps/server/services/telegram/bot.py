import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

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
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown as _escape_md

from .dashboards import (
    SEP,
    account_screen,
    admin_screen,
    escape_md,
    main_menu,
    overview_screen,
    sanitize_error,
    settings_screen,
    signals_screen,
    trades_screen,
    DEFAULT_NOTIFICATION_PREFS,
    DEFAULT_QUIET_HOURS,
)

logger = logging.getLogger(__name__)

_ADMIN_AUDIT_LOG_MODE = 0o600


class PineTunnelTelegramBot:
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
        self.notification_prefs = dict(DEFAULT_NOTIFICATION_PREFS)
        self.quiet_hours = dict(DEFAULT_QUIET_HOURS)
        self._revealed_keys: set[str] = set()
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
                BotCommand("help", "Show help"),
                BotCommand("login", "Dashboard login code"),
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

    def _is_admin(self, update: Update) -> bool:
        return update.effective_user.id in self.admin_ids

    def _register_handlers(self):
        app = self.app
        _admin_filter = filters.User(user_id=self.admin_ids) if self.admin_ids else filters.Chat(-1)

        app.add_handler(CommandHandler("start", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("menu", self._cmd_start, filters=_admin_filter))
        app.add_handler(CommandHandler("help", self._cmd_help, filters=_admin_filter))
        app.add_handler(CommandHandler("login", self._cmd_login, filters=_admin_filter))
        app.add_handler(CallbackQueryHandler(self._cb_handler))
        app.add_error_handler(self._error_handler)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = main_menu(self)
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "<b>PineTunnel Admin Bot</b>\n\n"
            "<b>Commands:</b>\n"
            "/start - Main menu (dashboards)\n"
            "/menu - Same as /start\n"
            "/help - This help message\n"
            "/login - Get web dashboard login code\n\n"
            "Use the inline buttons to navigate dashboards."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

    async def _cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    async def _cb_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_admin(update):
            await query.edit_message_text("Admin access required.")
            return

        data = query.data or ""
        if data == "noop":
            return

        try:
            text, keyboard = self._route_callback(data)
            if text:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return
            raise

    def _route_callback(self, data: str) -> tuple[str, list[list[InlineKeyboardButton]]]:
        if data == "nav:main":
            return main_menu(self)

        if data.startswith("nav:"):
            screen = data[4:]
            return self._render_screen(screen)

        if data.startswith("refresh:"):
            screen = data[8:]
            return self._render_screen(screen)

        if data.startswith("page:"):
            parts = data.split(":")
            screen = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 0
            return self._render_screen(screen, page=page)

        if data.startswith("filter:"):
            parts = data.split(":")
            screen = parts[1]
            if screen == "trades":
                side = parts[2] if len(parts) > 2 else "all"
                return trades_screen(self, page=0, side_filter=side)
            if screen == "signals":
                ftype = parts[2] if len(parts) > 2 else "cmd"
                value = parts[3] if len(parts) > 3 else "all"
                return self._render_screen("signals", cmd_filter=value if ftype == "cmd" else "all",
                                           status_filter=value if ftype == "status" else "all")

        if data.startswith("toggle:"):
            key = data[7:]
            self._toggle_setting(key)
            return settings_screen(self)

        if data.startswith("reveal:"):
            parts = data.split(":")
            lic_key = parts[2] if len(parts) > 2 else ""
            if lic_key in self._revealed_keys:
                self._revealed_keys.discard(lic_key)
            else:
                self._revealed_keys.add(lic_key)
            return account_screen(self)

        return main_menu(self)

    def _render_screen(self, screen: str, page: int = 0, cmd_filter: str = "all", status_filter: str = "all") -> tuple[str, list[list[InlineKeyboardButton]]]:
        if screen == "overview":
            return overview_screen(self)
        if screen == "account":
            return account_screen(self, page=page)
        if screen == "trades":
            return trades_screen(self, page=page)
        if screen == "signals":
            return signals_screen(self, page=page, cmd_filter=cmd_filter, status_filter=status_filter)
        if screen == "settings":
            return settings_screen(self)
        if screen == "admin":
            return admin_screen(self)
        return main_menu(self)

    def _toggle_setting(self, key: str):
        if key == "alerts":
            self.alerts_enabled = not self.alerts_enabled
        elif key == "quiet_hours":
            self.quiet_hours["enabled"] = not self.quiet_hours.get("enabled", False)
        elif key in self.notification_prefs:
            self.notification_prefs[key] = not self.notification_prefs[key]
        self._save_bot_settings()

    def _load_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        try:
            if os.path.exists(settings_file):
                with open(settings_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.alerts_enabled = data.get("alerts_enabled", True)
                    prefs = data.get("notifications", {})
                    if isinstance(prefs, dict):
                        self.notification_prefs = {**DEFAULT_NOTIFICATION_PREFS, **prefs}
                    quiet = data.get("quiet_hours", {})
                    if isinstance(quiet, dict):
                        self.quiet_hours = {**DEFAULT_QUIET_HOURS, **quiet}
                    return
        except Exception as e:
            logger.error("Failed to load bot settings: %s", e)
        self.alerts_enabled = True

    def _save_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        try:
            parent = Path(settings_file).parent
            parent.mkdir(parents=True, exist_ok=True)
            data = {
                "alerts_enabled": self.alerts_enabled,
                "notifications": self.notification_prefs,
                "quiet_hours": self.quiet_hours,
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, settings_file)
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error("Failed to save bot settings: %s", e)

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

    async def notify_admin(self, message: str):
        if not self._started or not self.app:
            return
        for admin_id in self.admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id, text=message, parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)

    def _should_notify(self, pref_key: str) -> bool:
        if not self.alerts_enabled:
            return False
        return self.notification_prefs.get(pref_key, False)

    async def on_trade_executed(self, report):
        if not self._should_notify("trade_opened"):
            return
        try:
            await self.notify_admin(
                f"Trade Executed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Side: {report.side}\n"
                f"Volume: {report.volume}"
            )
        except Exception as e:
            logger.error("Trade executed notification error: %s", e)

    async def on_trade_execution_failed(self, report):
        if not self._should_notify("error_alerts"):
            return
        try:
            await self.notify_admin(
                f"Trade Execution Failed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Error: {escape_md(str(report.error))}"
            )
        except Exception as e:
            logger.error("Trade execution failed notification error: %s", e)

    async def on_position_closed(self, report):
        if not self._should_notify("trade_closed"):
            return
        try:
            await self.notify_admin(
                f"Position Closed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Profit: {report.profit}"
            )
        except Exception as e:
            logger.error("Position closed notification error: %s", e)

    async def on_trade_failure(self, license_key: str, error: str):
        if not self._should_notify("error_alerts"):
            return
        await self.notify_admin(
            f"Trade Failure\n"
            f"License: {license_key}\n"
            f"Error: {escape_md(error)}"
        )

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err and "message is not modified" in str(err).lower():
            logger.debug("Benign Telegram edit error: %s", err)
            return
        logger.error("Exception while handling update: %s", err, exc_info=err)
        sanitized = sanitize_error(err) if err else "Unknown error"
        for admin_id in self.admin_ids:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"Bot Error\n\n{sanitized}")
            except Exception:
                logger.error("Failed to send error notification to admin %s", admin_id, exc_info=True)
