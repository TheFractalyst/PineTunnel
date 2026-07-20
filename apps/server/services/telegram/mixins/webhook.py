"""Webhook endpoint mixin — view/edit the public webhook URL (SERVER_BASE_URL).

Shows the URL TradingView posts alerts to (``SERVER_BASE_URL`` = the Cloudflare
tunnel public URL). Editable on local/CLI deploys: the bot writes the new value to
``.env`` (atomic, via ``apps.lib.env_manager``) and it takes effect after a server
restart. On Render/PaaS the env var is dashboard-managed, so the screen is read-only
and points to the Render dashboard.

The bot cannot create or verify a Cloudflare tunnel — editing only changes the
configured URL string, so the admin is responsible for ensuring a tunnel already
points at the server for the new domain.
"""

import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown as _escape_md

from apps.lib.env_manager import find_env_path, read_env, write_env_updates
from apps.server.config.settings import get_config, reset_config_singleton
from ..constants import WEBHOOK_URL_CONFIRM, WEBHOOK_URL_INPUT
from ..helpers import SEP, is_benign_edit_error

logger = logging.getLogger(__name__)

_RENDER_DASHBOARD_URL = "https://dashboard.render.com/"
_MAX_URL_LEN = 200


def _validate_webhook_url(url: str) -> bool:
    """Pure validator: must be an https:// URL with no whitespace, sane length."""
    if not url or not url.startswith("https://"):
        return False
    if len(url) <= len("https://") or len(url) > _MAX_URL_LEN:
        return False
    if any(c in url for c in (" ", "\t", "\n", "\r")):
        return False
    return True


class WebhookMixin:
    """View and edit the webhook endpoint domain (SERVER_BASE_URL) from Telegram."""

    @staticmethod
    def _env_path() -> Path:
        # CWD-based (not __file__-based) so it resolves correctly when the package
        # is pip-installed: start_daemon sets cwd=project root at runtime.
        return find_env_path()

    @staticmethod
    def _is_render_env() -> bool:
        return os.environ.get("RENDER", "").lower() == "true"

    async def _show_webhook_screen(self, update: Update):
        """Show the current webhook URL + deploy mode, with edit affordance if local."""
        active = get_config().server.base_url
        env_path = self._env_path()
        env_exists = env_path.exists()
        is_render = self._is_render_env()
        editable = (not is_render) and env_exists

        pending = read_env(env_path).get("SERVER_BASE_URL", "") if env_exists else ""

        if is_render:
            mode_line = "Mode: Render (dashboard-managed)"
        elif env_exists:
            mode_line = "Mode: Local (`.env`) — applies on save"
        else:
            mode_line = "Mode: Environment variables (no `.env` found)"

        text = (
            "🌐 *Webhook Endpoint*\n"
            f"{SEP}\n"
            "TradingView posts alerts to this URL.\n"
            f"{SEP}\n"
            f"Active: `{_escape_md(active, version=1)}`\n"
        )
        if env_exists and pending and pending != active:
            text += f"⏳ Pending restart: `{_escape_md(pending, version=1)}`\n"
        text += f"{mode_line}\n"
        if is_render:
            text += "Edit `SERVER_BASE_URL` in the Render dashboard.\n"
        elif not env_exists:
            text += (
                "No `.env` found — run `pinetunnel` from your project dir, "
                "or set `SERVER_BASE_URL` in your environment.\n"
            )

        keyboard: list[list[InlineKeyboardButton]] = []
        if editable:
            keyboard.append([InlineKeyboardButton("✏️ Change URL", callback_data="set_webhook_edit")])
        if is_render:
            keyboard.append([InlineKeyboardButton("↩️ Render Dashboard", url=_RENDER_DASHBOARD_URL)])
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="menu_main")])

        try:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as e:
            if not is_benign_edit_error(e):
                raise

    async def _webhook_edit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
        if not self._is_admin(update):
            return ConversationHandler.END

        context.user_data.pop("webhook_new_url", None)
        current = get_config().server.base_url
        prompt = (
            "✏️ *Change Webhook URL*\n"
            f"{SEP}\n"
            f"Current: `{_escape_md(current, version=1)}`\n\n"
            "Send the new URL (must start with `https://`).\n\n"
            "⚠️ The new domain must already have a Cloudflare tunnel pointing at this server.\n\n"
            "Send /cancel to abort."
        )
        try:
            await update.callback_query.edit_message_text(
                prompt, parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as e:
            if not is_benign_edit_error(e):
                logger.debug("Failed to send webhook edit prompt: %s", e)
        return WEBHOOK_URL_INPUT

    async def _webhook_url_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if not _validate_webhook_url(text):
            try:
                await update.message.reply_text(
                    "❌ Invalid URL. Must start with `https://`, no spaces, "
                    f"≤{_MAX_URL_LEN} chars. Try again or /cancel:",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to send invalid-url reply: %s", e)
            return WEBHOOK_URL_INPUT

        context.user_data["webhook_new_url"] = text
        current = get_config().server.base_url

        confirm = (
            "⚠️ *Confirm Webhook URL Change*\n"
            f"{SEP}\n"
            f"Old: `{_escape_md(current, version=1)}`\n"
            f"New: `{_escape_md(text, version=1)}`\n\n"
            "Saved to `.env` — *restart the server to apply*.\n"
            "Ensure a Cloudflare tunnel points at the new domain."
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Apply", callback_data="set_webhook_confirm_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="set_webhook_confirm_no"),
            ],
        ]
        try:
            await update.message.reply_text(
                confirm,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as e:
            if not is_benign_edit_error(e):
                logger.debug("Failed to send webhook confirm screen: %s", e)
        return WEBHOOK_URL_CONFIRM

    async def _webhook_url_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "set_webhook_confirm_no":
            context.user_data.pop("webhook_new_url", None)
            try:
                await query.edit_message_text(
                    "❌ Webhook URL change cancelled.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🌐 Back to Webhook", callback_data="set_webhook")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit webhook cancel message: %s", e)
            return ConversationHandler.END

        new_url = context.user_data.pop("webhook_new_url", None)
        if not new_url:
            try:
                await query.edit_message_text(
                    "⏱️ Session expired. Please start over.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🌐 Back to Webhook", callback_data="set_webhook")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit webhook expired message: %s", e)
            return ConversationHandler.END

        old_url = get_config().server.base_url
        try:
            write_env_updates(self._env_path(), {"SERVER_BASE_URL": new_url})
        except Exception as e:
            logger.error("Failed to write SERVER_BASE_URL to .env: %s", e, exc_info=True)
            try:
                await query.edit_message_text(
                    "❌ Failed to write `.env`. Check server logs.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🌐 Back to Webhook", callback_data="set_webhook")]]
                    ),
                )
            except TelegramError as e2:
                if not is_benign_edit_error(e2):
                    logger.debug("Failed to edit webhook write-failure message: %s", e2)
            return ConversationHandler.END

        # Hot-reload: Settings reads os.environ at instantiation, so sync the live
        # env var alongside .env, then clear the cached singleton. The next
        # get_config() re-reads os.environ and the new URL is live immediately —
        # no restart. (Single-worker deployments — the default — are fully applied;
        # with multiple workers only this worker updates, so .env still holds the
        # new value for any later full restart.)
        applied = old_url
        try:
            os.environ["SERVER_BASE_URL"] = new_url
            reset_config_singleton()
            applied = get_config().server.base_url
        except Exception as e:
            logger.error("Hot-reload of SERVER_BASE_URL failed: %s", e, exc_info=True)

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="change_webhook_url",
            details={"old": old_url, "new": new_url, "applied_live": applied == new_url},
        )

        if applied == new_url:
            body = (
                f"Active: `{_escape_md(new_url, version=1)}`\n\n"
                "Live now — no restart needed."
            )
        else:
            body = (
                f"New: `{_escape_md(new_url, version=1)}`\n\n"
                "Hot-reload failed — restart the server to apply."
            )

        try:
            await query.edit_message_text(
                "✅ *Webhook URL applied*\n" f"{SEP}\n" f"{body}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🌐 Back to Webhook", callback_data="set_webhook")]]
                ),
            )
        except TelegramError as e:
            if not is_benign_edit_error(e):
                logger.debug("Failed to edit webhook success message: %s", e)
        return ConversationHandler.END
