import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown as _escape_md

from ..constants import (
    ADD_LIC_CONFIRM,
    ADD_LIC_EMAIL,
    ADD_LIC_EXPIRY,
    ADD_LIC_FEATURES,
    ADD_LIC_NAME,
    EDIT_LIC_FIELD,
    EDIT_LIC_PICK,
    EDIT_LIC_VALUE,
    EXPIRY_PICK,
    EXPIRY_VALUE,
    SEARCH_QUERY,
)
from ..helpers import (
    SEP,
    is_benign_edit_error,
    truncate,
    validate_email,
    validate_symbols,
    validate_volume,
)

logger = logging.getLogger(__name__)

# Feature options shown during license creation
_FEATURE_OPTIONS = ["websocket", "trading", "unlimited_symbols"]


class ConversationMixin:
    """All ConversationHandler handler methods: add/edit license, expiry, search."""

    async def _add_lic_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
        if not self._is_admin(update):
            return ConversationHandler.END

        # Clean up any previous attempt's data (handles restart from stuck state)
        for k in list(context.user_data.keys()):
            if k.startswith("new_lic_"):
                del context.user_data[k]

        prompt = "+ Add New License\n\n" "Enter client name:\n\n" "Send /cancel to abort."
        try:
            await update.callback_query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            try:
                await update.callback_query.message.reply_text(
                    prompt, parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                logger.error("Failed to send Add License prompt")
                return ConversationHandler.END
        return ADD_LIC_NAME

    async def _add_lic_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        name = update.message.text.strip()[:100]
        if not name:
            try:
                await update.message.reply_text("❌ Name cannot be empty. Try again:")
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to send name-empty reply: %s", e)
            return ADD_LIC_NAME
        context.user_data["new_lic_name"] = name
        try:
            await update.message.reply_text("Enter client *email*:", parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            return ConversationHandler.END
        return ADD_LIC_EMAIL

    async def _add_lic_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        email = update.message.text.strip()
        if not validate_email(email):
            try:
                await update.message.reply_text(
                    "❌ Invalid email format. Please try again:",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to send invalid-email reply: %s", e)
            return ADD_LIC_EMAIL
        context.user_data["new_lic_email"] = email

        context.user_data["new_lic_features"] = ["websocket", "trading", "unlimited_symbols"]

        keyboard = [
            [InlineKeyboardButton("✅ websocket", callback_data="feat_toggle_websocket")],
            [InlineKeyboardButton("✅ trading", callback_data="feat_toggle_trading")],
            [
                InlineKeyboardButton(
                    "✅ unlimited_symbols", callback_data="feat_toggle_unlimited_symbols"
                )
            ],
            [InlineKeyboardButton("✅ Done — Continue", callback_data="feat_done")],
        ]

        try:
            await update.message.reply_text(
                "Select features (tap to toggle):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError:
            return ConversationHandler.END
        return ADD_LIC_FEATURES

    async def _add_lic_features(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "feat_done":
            keyboard = [
                [
                    InlineKeyboardButton("30 days", callback_data="exp_30"),
                    InlineKeyboardButton("90 days", callback_data="exp_90"),
                ],
                [
                    InlineKeyboardButton("1 year", callback_data="exp_365"),
                    InlineKeyboardButton("Lifetime", callback_data="exp_0"),
                ],
            ]
            try:
                await query.edit_message_text(
                    "Set license expiry:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except TelegramError:
                return ConversationHandler.END
            return ADD_LIC_EXPIRY

        feature = data.replace("feat_toggle_", "")
        features = context.user_data.get("new_lic_features", [])
        if feature in features:
            features.remove(feature)
        else:
            features.append(feature)
        context.user_data["new_lic_features"] = features

        keyboard = []
        for f in _FEATURE_OPTIONS:
            prefix = "✅" if f in features else "❌"
            keyboard.append(
                [InlineKeyboardButton(f"{prefix} {f}", callback_data=f"feat_toggle_{f}")]
            )
        keyboard.append([InlineKeyboardButton("✅ Done — Continue", callback_data="feat_done")])

        try:
            await query.edit_message_text(
                "Select features (tap to toggle):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError as e:
            if not is_benign_edit_error(e):
                logger.debug("edit failed: %s", e)
        return ADD_LIC_FEATURES

    async def _add_lic_expiry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        try:
            days = int(query.data.replace("exp_", ""))
        except ValueError:
            days = 0
        context.user_data["new_lic_expiry_days"] = days

        name = context.user_data.get("new_lic_name")
        email = context.user_data.get("new_lic_email")
        features = context.user_data.get("new_lic_features")
        if features is None:
            try:
                await query.edit_message_text(
                    "Session expired. Please start over.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Back", callback_data="menu_licenses")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit session-expired message: %s", e)
            return ConversationHandler.END
        expiry_str = f"{days} days" if days > 0 else "Lifetime"

        text = (
            "Confirm New License\n"
            f"{SEP}\n"
            f"Name: {_escape_md(name or '', version=1)}\n"
            f"Email: {_escape_md(email or '', version=1)}\n"
            f"Features: {_escape_md(', '.join(features), version=1)}\n"
            f"Expiry: {expiry_str}\n"
            f"{SEP}"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ Create", callback_data="addconf_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="addconf_no"),
            ],
        ]

        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except TelegramError:
            return ConversationHandler.END
        return ADD_LIC_CONFIRM

    async def _add_lic_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "addconf_no":
            try:
                await query.edit_message_text(
                    "❌ License creation cancelled.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit cancel message: %s", e)
            return ConversationHandler.END

        name = context.user_data.get("new_lic_name")
        email = context.user_data.get("new_lic_email")
        features = context.user_data.get("new_lic_features")
        expiry_days = context.user_data.get("new_lic_expiry_days")
        if features is None:
            try:
                await query.edit_message_text(
                    "⏱️ Session expired. Please start over.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit session-expired message in confirm: %s", e)
            return ConversationHandler.END

        now = datetime.now()
        expires_at = (now + timedelta(days=expiry_days)).isoformat() if expiry_days > 0 else None

        user_id = (
            max((c.get("user_id", 0) for c in self.client_manager.clients.values()), default=0) + 1
        )

        # Generate opaque tokens with embedded expiry
        license_token, secret_token = self.client_manager.generate_tokens(user_id, expires_at)

        client_data = {
            "name": name or "",
            "email": email or "",
            "status": "active",
            "created_at": now.isoformat(),
            "features": features,
            "max_symbols": 100 if "unlimited_symbols" in features else 25,
            "max_volume": 1000.0,
            "secret_key": secret_token,
            "require_secret": True,
            "expires_at": expires_at,
            "user_id": user_id,
        }

        key = license_token

        try:
            success = self.client_manager.add_client(key, client_data)
        except Exception as e:
            logger.error("add_client failed: %s", e, exc_info=True)
            success = False

        if success:
            await self._save_clients_checked(query)

            await self._log_admin_action(
                user_id=update.effective_user.id,
                username=update.effective_user.username,
                action="add_license",
                details={
                    "license_key": key,
                    "name": name,
                    "email": email,
                    "expires_at": client_data.get("expires_at"),
                },
            )

            try:
                await query.edit_message_text(
                    f"✅ *License Created*\n\n"
                    f"Key: `{key}`\n"
                    f"Secret: `{secret_token}`\n"
                    f"Name: {_escape_md(name, version=1)}\n"
                    f"Email: {_escape_md(email, version=1)}\n\n"
                    f"License is active and ready to use.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📄 View Details", callback_data=f"lic_info_{key}"
                                )
                            ],
                            [InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")],
                        ]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit license-created message: %s", e)
        else:
            try:
                await query.edit_message_text(
                    "❌ Failed to create license. Check server logs.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                    ),
                )
            except TelegramError as e:
                if not is_benign_edit_error(e):
                    logger.debug("Failed to edit license-failed message: %s", e)

        for k in list(context.user_data.keys()):
            if k.startswith("new_lic_"):
                del context.user_data[k]

        return ConversationHandler.END

    async def _edit_lic_picker_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
        if not self._is_admin(update):
            return ConversationHandler.END
        await self._show_license_picker(
            update, "edpick", "✏️ Edit License", "Select a license to edit:"
        )
        return EDIT_LIC_PICK

    async def _edit_lic_picker_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        payload = query.data.replace("edpick_", "")

        if payload.startswith("page_"):
            try:
                page = int(payload.split("_", 1)[1])
            except (ValueError, IndexError):
                page = 0
            await self._show_license_picker(
                update, "edpick", "✏️ Edit License", "Select a license to edit:", page=page
            )
            return EDIT_LIC_PICK

        license_key = payload
        data = self.client_manager.get_client_by_license(license_key)
        if not data:
            await query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END

        context.user_data["edit_lic_key"] = license_key

        keyboard = [
            [
                InlineKeyboardButton("Name", callback_data="editf_name"),
                InlineKeyboardButton("Email", callback_data="editf_email"),
            ],
            [
                InlineKeyboardButton("Max Volume", callback_data="editf_max_volume"),
                InlineKeyboardButton("Max Symbols", callback_data="editf_max_symbols"),
            ],
            [
                InlineKeyboardButton("Max Daily Trades", callback_data="editf_max_daily_trades"),
                InlineKeyboardButton("Max Daily Loss", callback_data="editf_max_daily_loss"),
            ],
            [InlineKeyboardButton("Cancel", callback_data="editf_cancel")],
        ]

        await query.edit_message_text(
            f"✏️ Editing `{license_key}` — *{_escape_md(data.get('name', 'Unknown'), version=1)}*\n\nSelect field to edit:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return EDIT_LIC_FIELD

    async def _edit_lic_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "editf_cancel":
            await query.edit_message_text(
                "❌ Edit cancelled.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END

        field = query.data.replace("editf_", "")
        context.user_data["edit_field"] = field

        key = context.user_data.get("edit_lic_key")
        if not key:
            await query.edit_message_text(
                "⏱️ Session expired. Please start over.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END
        data = self.client_manager.get_client_by_license(key)
        current_val = data.get(field, "N/A")

        await query.edit_message_text(
            f"Current `{field}`: `{current_val}`\n\nEnter new value:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return EDIT_LIC_VALUE

    async def _edit_lic_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        key = context.user_data.get("edit_lic_key")
        field = context.user_data.get("edit_field")
        if not key or not field:
            await update.message.reply_text(
                "⏱️ Session expired. Please start over.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END
        new_value = update.message.text.strip()

        data = self.client_manager.get_client_by_license(key)
        if not data:
            await update.message.reply_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]
                ),
            )
            return ConversationHandler.END

        if field == "email":
            if not validate_email(new_value):
                await update.message.reply_text("❌ Invalid email format. Try again:")
                return EDIT_LIC_VALUE
        elif field == "max_volume":
            validated = validate_volume(new_value)
            if validated is None:
                await update.message.reply_text("❌ Volume must be 0.01-10000. Try again:")
                return EDIT_LIC_VALUE
            new_value = validated
        elif field == "max_symbols":
            validated = validate_symbols(new_value)
            if validated is None:
                await update.message.reply_text("Symbols must be 1-1000. Try again:")
                return EDIT_LIC_VALUE
            new_value = validated
        elif field == "max_daily_trades":
            try:
                new_value = int(new_value)
                if new_value < 1 or new_value > 10000:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Daily trades must be 1-10000. Try again:")
                return EDIT_LIC_VALUE
        elif field == "max_daily_loss":
            try:
                new_value = float(new_value)
                if new_value < 0 or new_value > 1000000:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Daily loss must be 0-1000000. Try again:")
                return EDIT_LIC_VALUE
        data[field] = new_value
        self.client_manager.clients[key] = data
        await self._save_clients_checked(update)

        log_value = str(new_value)
        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="edit_license",
            details={"license_key": key, "field": field, "new_value": log_value},
        )

        await update.message.reply_text(
            f"✅ Updated `{field}` to `{new_value}` for `{key}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📄 View Details", callback_data=f"lic_info_{key}")],
                    [InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")],
                ]
            ),
        )
        return ConversationHandler.END

    async def _expiry_picker_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
        if not self._is_admin(update):
            return ConversationHandler.END
        await self._show_license_picker(
            update, "expick", "📅 Set Expiry", "Select a license to set expiry:"
        )
        return EXPIRY_PICK

    async def _expiry_picker_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        payload = query.data.replace("expick_", "")

        if payload.startswith("page_"):
            try:
                page = int(payload.split("_", 1)[1])
            except (ValueError, IndexError):
                page = 0
            await self._show_license_picker(
                update, "expick", "📅 Set Expiry", "Select a license to set expiry:", page=page
            )
            return EXPIRY_PICK

        license_key = payload
        data = self.client_manager.get_client_by_license(license_key)
        if not data:
            await query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END

        context.user_data["expiry_key"] = license_key

        keyboard = [
            [
                InlineKeyboardButton("30 days", callback_data="expval_30"),
                InlineKeyboardButton("90 days", callback_data="expval_90"),
            ],
            [
                InlineKeyboardButton("6 months", callback_data="expval_180"),
                InlineKeyboardButton("1 year", callback_data="expval_365"),
            ],
            [
                InlineKeyboardButton("2 years", callback_data="expval_730"),
                InlineKeyboardButton("Lifetime (remove)", callback_data="expval_0"),
            ],
        ]

        await query.edit_message_text(
            f"📅 Set expiry for `{license_key}` — *{_escape_md(data.get('name') or '', version=1)}*:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return EXPIRY_VALUE

    async def _expiry_value(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        try:
            days = int(query.data.replace("expval_", ""))
        except ValueError:
            days = 0
        key = context.user_data.get("expiry_key")
        if not key:
            await query.edit_message_text(
                "⏱️ Session expired. Please start over.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END
        data = self.client_manager.get_client_by_license(key)
        if not data:
            await query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]
                ),
            )
            return ConversationHandler.END

        if days > 0:
            expires = (datetime.now() + timedelta(days=days)).isoformat()
            data["expires_at"] = expires
            msg = (
                f"✅ Expiry set to *{days} days* from now\n({_escape_md(expires[:10], version=1)})"
            )
        else:
            data.pop("expires_at", None)
            expires = None
            msg = "✅ Expiry removed — license is now *lifetime*"

        # Regenerate tokens with new expiry (license_key embeds expiry, so it changes)
        user_id = data.get("user_id", 0)
        new_license, new_secret = self.client_manager.generate_tokens(user_id, expires)
        old_key = key
        del self.client_manager.clients[old_key]
        data["secret_key"] = new_secret
        self.client_manager.clients[new_license] = data
        key = new_license

        await self._save_clients_checked(update)

        await query.edit_message_text(
            f"{msg}\n\nLicense: `{key}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
            ),
        )
        return ConversationHandler.END

    async def _search_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.answer()
        if not self._is_admin(update):
            return ConversationHandler.END
        await update.callback_query.edit_message_text(
            "🔍 *Search Licenses*\n\nEnter search term (key, name, or email):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SEARCH_QUERY

    async def _search_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query_text = update.message.text.strip().lower()

        results = [
            (key, data)
            for key, data in self.client_manager.clients.items()
            if query_text in key.lower()
            or query_text in data.get("name", "").lower()
            or query_text in data.get("email", "").lower()
        ]

        if not results:
            await update.message.reply_text(
                "❌ No licenses found matching your search.\n\n/cancel to go back.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return ConversationHandler.END

        lines = [f"🔍 *Search Results* ({len(results)} found)\n{SEP}"]
        keyboard = []
        for key, data in results[:10]:
            status_emoji = "🟢" if data.get("status") == "active" else "🔴"
            name = truncate(data.get("name", "Unknown"), 25)
            lines.append(f"{status_emoji} `{key}` — {_escape_md(name, version=1)}")
            keyboard.append([InlineKeyboardButton(f"📄 {name}", callback_data=f"lic_info_{key}")])

        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END
