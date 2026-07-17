import json
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from ..constants import CONVERSATION_CLEANUP_PREFIXES

logger = logging.getLogger(__name__)

DEFAULT_QUIET_HOURS = {
    "enabled": False,
    "start": "23:00",
    "end": "07:00",
    "timezone": "UTC",
}


class AuthMixin:
    """Admin authorization and bot settings management."""

    def _is_admin(self, update: Update) -> bool:
        return update.effective_user.id in self.admin_ids

    def _is_admin_logged(self, update: Update) -> bool:
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
        if not self.client_manager.save_clients():
            logger.error("save_clients() returned False - license data may not be persisted")
            try:
                await update_or_query.edit_message_text(
                    "License updated in memory but failed to persist to disk. Check server logs.",
                )
            except Exception:
                pass
        return True

    def _load_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        try:
            if os.path.exists(settings_file):
                with open(settings_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.alerts_enabled = data.get("alerts_enabled", True)
                    return
        except Exception as e:
            logger.error("Failed to load bot settings: %s", e)
        self.alerts_enabled = True

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

    def _save_bot_settings(self):
        settings_file = os.path.join(self.data_dir, "bot_settings.json")
        self._atomic_json_write(settings_file, {"alerts_enabled": self.alerts_enabled})

    async def _cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for key in list(context.user_data.keys()):
            if any(prefix in key for prefix in CONVERSATION_CLEANUP_PREFIXES):
                del context.user_data[key]

        msg = update.effective_message
        if msg:
            await msg.reply_text(
                "Operation cancelled.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Main Menu", callback_data="menu_main")]]
                ),
            )
        return ConversationHandler.END
