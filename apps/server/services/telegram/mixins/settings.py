"""Settings mixin — system info, quick status, and bot-wide notification/quiet-hours settings.

Adapted from the reference SettingsMixin for admin-only mode: notification prefs and
quiet hours are bot-wide (``self.notification_prefs`` / ``self.quiet_hours`` persisted
in ``bot_settings.json``), not per-license in ``telegram_users``. The multi-license
merge logic and the "Registered Users" system-info line are dropped.
"""

import json
import logging
import os
import platform
from datetime import datetime, time as _time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from apps.server.config.settings import get_config
from apps.server.services.notification import NOTIF_LABELS, NOTIF_TYPES
from ..constants import USER_QH_INPUT
from ..helpers import SEP, is_benign_edit_error

logger = logging.getLogger(__name__)


class SettingsMixin:
    """Settings: alerts toggle, system info, quick status, bot-wide notif/quiet-hours."""

    async def _show_system_info(self, update: Update):
        ea_ver = "N/A"
        try:
            version_path = (
                Path(__file__).parent.parent.parent.parent.parent / "ea" / "mt5" / "VERSION.json"
            )
            if version_path.exists():
                with open(version_path) as f:
                    ea_ver = json.load(f).get("version", "N/A")
        except Exception:
            logger.debug("Failed to read EA version from VERSION.json", exc_info=True)

        try:
            cfg = get_config()
            app_env = cfg.environment
            log_level = cfg.logging.level
            debug = str(cfg.debug).lower()
        except Exception:
            app_env = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development"))
            log_level = os.getenv("LOG_LEVEL", "INFO")
            debug = os.getenv("DEBUG", "false")

        text = (
            "ℹ️ *System Info*\n"
            f"{SEP}\n"
            f"Bot Started: {'Yes' if self._started else 'No'}\n"
            f"Alerts: {'ON' if self.alerts_enabled else 'OFF'}\n"
            f"Admins: {len(self.admin_ids)}\n"
            f"Total Licenses: {len(self.client_manager.clients)}\n"
            f"{SEP}\n"
            f"Platform: `{platform.system()} {platform.release()}`\n"
            f"Python: `{platform.python_version()}`\n"
            f"APP_ENV: `{app_env}`\n"
            f"LOG_LEVEL: `{log_level}`\n"
            f"DEBUG: `{debug}`\n"
            f"EA Version: `{ea_ver}`\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_main")]]
                ),
            )
        except Exception as e:
            if not is_benign_edit_error(e):
                raise

    async def _cmd_quick_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return

        now = datetime.now()
        total_lic = len(self.client_manager.clients)
        active_lic = self._active_license_count

        # Count HTTP and WS connections separately for display
        http_count = sum(
            1
            for poll_data in self.http_polling_clients.values()
            if poll_data.get("last_poll") and (now - poll_data["last_poll"]).total_seconds() <= 10
        )
        ws_count = 0
        if self.ws_manager:
            try:
                ws_count = self.ws_manager.get_total_connections()
            except Exception:
                logger.debug("Failed to get WS count in quick status", exc_info=True)

        db_ok = False
        try:
            self.db_manager.execute_query("SELECT 1")
            db_ok = True
        except Exception:
            logger.debug("DB check failed in quick status", exc_info=True)

        uptime_str = "N/A"
        try:
            process = psutil.Process()
            uptime = now - datetime.fromtimestamp(process.create_time())
            h, rem = divmod(int(uptime.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m"
        except Exception:
            logger.debug("Uptime check failed in quick status", exc_info=True)

        db_emoji = "🟢" if db_ok else "🔴"
        conn_detail = f"{http_count} HTTP" + (f", {ws_count} WS" if ws_count else "")
        text = (
            f"⚡ *Quick Status*\n"
            f"{SEP}\n"
            f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📋 Licenses: {active_lic}/{total_lic} active\n"
            f"🔌 Connected: {conn_detail}\n"
            f"{db_emoji} Database: {'OK' if db_ok else 'ERROR'}\n"
            f"⏱ Uptime: {uptime_str}\n"
        )

        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _show_notif_presets(self, update: Update):
        """Show notification preset menu: All / Critical Only / Silent / Custom."""
        prefs = self.notification_prefs

        all_on = all(prefs.get(t, True) for t in NOTIF_TYPES)
        all_off = all(not prefs.get(t, True) for t in NOTIF_TYPES)
        critical_keys = {"exec_failed", "margin_warning", "equity_drawdown"}
        critical_only = all(prefs.get(t, True) for t in critical_keys) and all(
            not prefs.get(t, True) for t in NOTIF_TYPES if t not in critical_keys
        )

        if all_on:
            current = "All Alerts"
        elif critical_only:
            current = "Critical Only"
        elif all_off:
            current = "Silent"
        else:
            current = "Custom"

        keyboard = [
            [
                InlineKeyboardButton(
                    f"{'✅' if all_on else '🔘'} All Alerts",
                    callback_data="user_preset_all",
                ),
                InlineKeyboardButton(
                    f"{'✅' if critical_only else '🔘'} Critical Only",
                    callback_data="user_preset_critical",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{'✅' if all_off else '🔘'} Silent",
                    callback_data="user_preset_silent",
                ),
                InlineKeyboardButton(
                    f"{'✅' if current == 'Custom' else '🔘'} Custom",
                    callback_data="user_notif_custom",
                ),
            ],
            [InlineKeyboardButton("🌙 Quiet Hours", callback_data="user_quiet_hours")],
            [InlineKeyboardButton("◀️ Back", callback_data="user_settings")],
        ]

        text = (
            "🔔 *Alert Settings*\n"
            f"{SEP}\n"
            f"Current: *{current}*\n\n"
            "Choose a preset or tap Custom for individual toggles."
        )
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as e:
            if not is_benign_edit_error(e):
                raise

    async def _apply_notif_preset(self, update: Update, preset: str):
        """Apply a notification preset (bot-wide)."""
        critical_keys = {"exec_failed", "margin_warning", "equity_drawdown"}

        if preset == "all":
            new_vals = {t: True for t in NOTIF_TYPES}
        elif preset == "critical":
            new_vals = {t: t in critical_keys for t in NOTIF_TYPES}
        elif preset == "silent":
            new_vals = {t: False for t in NOTIF_TYPES}
        else:
            return

        self.notification_prefs = dict(new_vals)
        self._save_bot_settings()

        try:
            await update.callback_query.answer("Settings updated")
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
        await self._show_notif_presets(update)

    async def _show_user_notif_settings(self, update: Update):
        """Show individual notification toggle menu (Custom mode)."""
        prefs = self.notification_prefs
        labels = NOTIF_LABELS

        keyboard = []
        for key, label in labels.items():
            emoji = "✅" if prefs.get(key, True) else "❌"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{emoji} {label}",
                        callback_data=f"user_toggle_{key}",
                    )
                ]
            )
        keyboard.append(
            [InlineKeyboardButton("◀️ Back to Presets", callback_data="user_notif_settings")]
        )

        text = "🔔 *Custom Alerts*\n" f"{SEP}\n" "Toggle each notification type:\n" f"{SEP}"
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as e:
            if not is_benign_edit_error(e):
                raise

    async def _toggle_user_notif(self, update: Update, event_type: str):
        """Toggle a bot-wide notification preference."""
        if event_type not in NOTIF_TYPES:
            return
        current = self.notification_prefs.get(event_type, True)
        self.notification_prefs[event_type] = not current
        self._save_bot_settings()
        await self._show_user_notif_settings(update)

    async def _show_user_quiet_hours(self, update: Update):
        """Show bot-wide quiet hours settings."""
        qh = self.quiet_hours
        enabled = qh.get("enabled", False)
        status = "🌙 ON" if enabled else "☀️ OFF"
        start = qh.get("start", "23:00")
        end = qh.get("end", "07:00")
        tz = qh.get("timezone", "UTC")

        keyboard = [
            [InlineKeyboardButton(f"Quiet Hours: {status}", callback_data="qh_toggle")],
            [InlineKeyboardButton(f"Start: {start}", callback_data="qh_set_start")],
            [InlineKeyboardButton(f"End: {end}", callback_data="qh_set_end")],
            [InlineKeyboardButton(f"Timezone: {tz}", callback_data="qh_set_tz")],
            [InlineKeyboardButton("◀️ Back", callback_data="user_settings")],
        ]
        text = (
            "🌙 *Quiet Hours*\n"
            f"{SEP}\n"
            f"Status: {status}\n"
            f"Start: {start}\n"
            f"End: {end}\n"
            f"Timezone: {tz}\n"
            f"{SEP}"
        )
        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as e:
            if not is_benign_edit_error(e):
                raise

    async def _handle_quiet_hours_cb(self, update: Update, data: str):
        """Handle quiet hours callback actions (bot-wide)."""
        if data == "qh_toggle":
            self.quiet_hours["enabled"] = not self.quiet_hours.get("enabled", False)
            self._save_bot_settings()
            await self._show_user_quiet_hours(update)
        elif data == "qh_set_tz":
            keyboard = [
                [InlineKeyboardButton(tz, callback_data=f"qh_tz_{tz}")]
                for tz in [
                    "UTC",
                    "Europe/London",
                    "Europe/Berlin",
                    "US/Eastern",
                    "US/Pacific",
                    "Asia/Tokyo",
                    "Asia/Dubai",
                ]
            ]
            keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="user_quiet_hours")])
            try:
                await update.callback_query.edit_message_text(
                    "🌍 Select timezone:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception as e:
                if not is_benign_edit_error(e):
                    raise
        elif data.startswith("qh_tz_"):
            tz = data.replace("qh_tz_", "")
            self.quiet_hours["timezone"] = tz
            self._save_bot_settings()
            await self._show_user_quiet_hours(update)
        else:
            logger.warning("Unhandled quiet hours callback: %s", data)

    async def _qh_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point: user tapped qh_set_start or qh_set_end."""
        field = update.callback_query.data
        context.user_data["qh_field"] = "start" if field == "qh_set_start" else "end"
        try:
            await update.callback_query.edit_message_text(
                f"🌙 Enter new {context.user_data['qh_field']} time (24h format, e.g. `23:00`):\n\n"
                f"Send /cancel to go back.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            if not is_benign_edit_error(e):
                raise
        return USER_QH_INPUT

    async def _qh_time_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process the time text input (bot-wide)."""
        text = update.message.text.strip()
        try:
            _time.fromisoformat(text)
        except (ValueError, TypeError):
            await update.message.reply_text(
                "Invalid format. Use HH:MM (e.g. `23:00`):", parse_mode=ParseMode.MARKDOWN
            )
            return USER_QH_INPUT

        field = context.user_data.pop("qh_field", "start")
        self.quiet_hours[field] = text
        self._save_bot_settings()

        await update.message.reply_text(
            f"✅ Quiet hours {field} set to `{text}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🌙 Back to Quiet Hours", callback_data="user_quiet_hours")]]
            ),
        )
        return ConversationHandler.END
