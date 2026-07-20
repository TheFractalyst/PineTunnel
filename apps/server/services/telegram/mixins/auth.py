"""Auth mixin — admin authorization and bot-wide settings persistence.

Admin-only adaptation of the reference AuthMixin: the registration / link-license /
per-user ``telegram_users`` machinery is dropped (no end-user self-service in the
public bot). What remains is admin gating, the license-save guard, the default
notification-prefs factory, and atomic load/save of bot-wide settings
(``alerts_enabled`` + ``notification_prefs`` + ``quiet_hours``) to
``bot_settings.json``.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from apps.server.services.notification import DEFAULT_PREFS
from ..constants import CONVERSATION_CLEANUP_PREFIXES

logger = logging.getLogger(__name__)

# Default quiet-hours config (bot-wide, admin-only mode)
DEFAULT_QUIET_HOURS = {
    "enabled": False,
    "start": "23:00",
    "end": "07:00",
    "timezone": "UTC",
}


class AuthMixin:
    """Admin authorization and bot-wide settings persistence."""

    def _is_admin(self, update: Update) -> bool:
        """Silent admin check for routing — no log spam on legitimate non-admin use."""
        return update.effective_user.id in self.admin_ids

    def _is_admin_logged(self, update: Update) -> bool:
        """Admin check with security logging — use for actual auth gates."""
        user_id = update.effective_user.id
        if user_id in self.admin_ids:
            return True
        logger.warning(
            "Unauthorized Telegram access attempt: user_id=%s, username=%s",
            user_id,
            update.effective_user.username,
        )
        return False

    async def _check_admin(self, update: Update) -> bool:
        if not self._is_admin_logged(update):
            await update.effective_message.reply_text("Not authorized.")
            return False
        return True

    async def _save_clients_checked(self, update_or_query, context=None) -> bool:
        """Save clients and notify admin if save fails. Returns True on success."""
        if not self.client_manager.save_clients():
            logger.error("save_clients() returned False — license data may not be persisted")
            try:
                await update_or_query.edit_message_text(
                    "⚠️ License updated in memory but *failed to persist to disk*. Check server logs.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                try:
                    await update_or_query.reply_text(
                        "⚠️ License updated in memory but *failed to persist to disk*. Check server logs.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    logger.debug("Failed to send save-failure fallback message", exc_info=True)
        return True

    @classmethod
    def _default_notif_prefs(cls) -> dict:
        return dict(DEFAULT_PREFS)

    def _atomic_json_write(self, filepath: str, data) -> None:
        try:
            parent = Path(filepath).parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, filepath)
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error("Failed to write %s: %s", filepath, e)

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
                        self.notification_prefs = {**DEFAULT_PREFS, **prefs}
                    quiet = data.get("quiet_hours", {})
                    if isinstance(quiet, dict):
                        self.quiet_hours = {**DEFAULT_QUIET_HOURS, **quiet}
                    logger.info("Bot settings loaded: alerts_enabled=%s", self.alerts_enabled)
                    return
        except Exception as e:
            logger.error("Failed to load bot settings: %s", e)
        self.alerts_enabled = True

    def _save_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        self._atomic_json_write(
            settings_file,
            {
                "alerts_enabled": self.alerts_enabled,
                "notifications": self.notification_prefs,
                "quiet_hours": self.quiet_hours,
            },
        )

    async def _cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for key in list(context.user_data.keys()):
            if any(prefix in key for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]
        context.user_data.pop("_link_state", None)

        msg = update.effective_message
        if msg:
            await msg.reply_text(
                "❌ Operation cancelled.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]
                ),
            )
        return ConversationHandler.END
