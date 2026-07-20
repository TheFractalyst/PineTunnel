import asyncio
import logging
from datetime import datetime

from dateutil import parser as date_parser

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown as _escape_md

from apps.server.db.analytics_store import get_stats_for_license
from ..helpers import SEP, calc_pagination, format_license_info, truncate
from ..keyboards import nav_row, respond

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {"active": "🟢", "pending": "🟡"}


def _status_emoji(status: str | None) -> str:
    return _STATUS_EMOJI.get(status or "", "🔴")


class LicenseMixin:
    """License CRUD, list/detail views, bulk operations, force disconnect."""

    async def _cmd_licenses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        await self._show_licenses_menu(update)

    async def _show_licenses_menu(self, update: Update):
        keyboard = [
            [
                InlineKeyboardButton("📋 List All", callback_data="lic_list"),
                InlineKeyboardButton("🔍 Search", callback_data="lic_search"),
            ],
            [
                InlineKeyboardButton("➕ Add License", callback_data="lic_add"),
                InlineKeyboardButton("✏️ Edit", callback_data="lic_edit_pick"),
            ],
            [
                InlineKeyboardButton("📅 Set Expiry", callback_data="lic_expiry_pick"),
            ],
            [
                InlineKeyboardButton(
                    "🔴 Deactivate Expired", callback_data="lic_bulk_deactivate_expired"
                ),
                InlineKeyboardButton("🟢 Activate All", callback_data="lic_bulk_activate_all"),
            ],
            [InlineKeyboardButton("◀️ Back to Menu", callback_data="menu_main")],
        ]

        total = len(self.client_manager.clients)
        active = self._active_license_count

        text = (
            "📋 *License Management*\n"
            f"{SEP}\n"
            f"Total: {total} | Active: {active} | Inactive: {total - active}\n"
            f"{SEP}\n"
            "Select an action:"
        )

        await respond(update, text, keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _show_license_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0
    ):
        PAGE_SIZE = 8
        clients = list(self.client_manager.clients.items())
        page, total_pages, start = calc_pagination(page, len(clients), PAGE_SIZE)

        end = start + PAGE_SIZE
        page_clients = clients[start:end]

        lines = [f"📋 *Licenses* (Page {page + 1}/{total_pages})\n{SEP}"]
        keyboard = []
        for key, data in page_clients:
            status_emoji = _status_emoji(data.get("status"))
            name_raw = data.get("name", "Unknown")
            lines.append(
                f"{status_emoji} `{key}` — {_escape_md(truncate(name_raw, 20), version=1)}"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"📄 {truncate(name_raw, 25)}", callback_data=f"lic_info_{key}"
                    )
                ]
            )

        text = "\n".join(lines)
        nr = nav_row(page, total_pages, "lic_page_")
        if nr:
            keyboard.append(nr)
        keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")])

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_license_detail(self, update: Update, key: str):
        if not self._is_admin(update):
            return

        data = self.client_manager.get_client_by_license(key)
        if not data:
            await update.callback_query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="lic_list")]]
                ),
            )
            return

        text = format_license_info(key, data)

        # Append live account stats if available
        try:
            snap = get_stats_for_license(key)
            if snap:
                balance = snap.get("balance", 0)
                equity = snap.get("equity", 0)
                margin_level = snap.get("margin_level", 0)
                positions = snap.get("open_positions", 0)
                profit = snap.get("profit", 0)
                leverage = snap.get("leverage", 0)
                currency = snap.get("currency", "")
                ml_str = f" | ML: {margin_level:.0f}%" if margin_level > 0 else ""
                profit_sign = "+" if profit >= 0 else ""
                text += (
                    f"\n\n💰 *Account*"
                    f"\n| Bal: {balance:.2f} {currency} | Eq: {equity:.2f} {currency}"
                    f"\n| P/L: {profit_sign}{profit:.2f} | Pos: {positions}{ml_str}"
                )
                if leverage:
                    text += f"\n- Leverage: 1:{leverage}"
        except Exception:
            logger.debug("Failed to get account stats for license detail %s", key, exc_info=True)
        status = data.get("status", "unknown")
        toggle_action = "deactivate" if status == "active" else "activate"
        toggle_emoji = "🔴" if status == "active" else "🟢"
        toggle_label = f"{toggle_emoji} {toggle_action.capitalize()}"

        keyboard = [
            [
                InlineKeyboardButton(toggle_label, callback_data=f"lic_{toggle_action}_{key}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"lic_delconf_{key}"),
            ],
            [
                InlineKeyboardButton(
                    "🔌 Force Disconnect", callback_data=f"lic_force_disconnect_{key}"
                ),
            ],
            [InlineKeyboardButton("◀️ Back", callback_data="lic_list")],
        ]

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _toggle_license(self, update: Update, key: str, new_status: str):
        if not self._is_admin(update):
            return
        data = self.client_manager.get_client_by_license(key)
        if not data:
            await update.callback_query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="lic_list")]]
                ),
            )
            return

        data["status"] = new_status
        self.client_manager.clients[key] = data
        await self._save_clients_checked(update)

        # Disconnect EA when deactivating
        if new_status == "inactive":
            self._cascade_deactivate_license(key)

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action=f"toggle_license_{new_status}",
            details={"license_key": key},
        )

        emoji = "🟢" if new_status == "active" else "🔴"
        await update.callback_query.edit_message_text(
            f"{emoji} License `{key}` set to *{new_status}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📄 View Details", callback_data=f"lic_info_{key}")],
                    [InlineKeyboardButton("◀️ Back", callback_data="lic_list")],
                ]
            ),
        )

    async def _delete_confirm(self, update: Update, key: str):
        data = self.client_manager.get_client_by_license(key)
        if not data:
            await update.callback_query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="lic_list")]]
                ),
            )
            return

        name = _escape_md(data.get("name", "Unknown"), version=1)
        await update.callback_query.edit_message_text(
            f"🗑️ *Confirm Deletion*\n\n"
            f"License: `{key}`\n"
            f"Name: {name}\n\n"
            f"This action cannot be undone.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Delete", callback_data=f"lic_dodel_{key}"),
                        InlineKeyboardButton("❌ Cancel", callback_data="lic_delcancel"),
                    ],
                ]
            ),
        )

    async def _do_delete_license(self, update: Update, key: str):
        if not self._is_admin(update):
            return
        data = self.client_manager.get_client_by_license(key)
        if not data:
            await update.callback_query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
                ),
            )
            return

        name = _escape_md(data.get("name", "Unknown"), version=1)

        if key in self.client_manager.clients:
            del self.client_manager.clients[key]
            await self._save_clients_checked(update)

        # Cascade: disconnect EA, clear signals, remove cached analytics
        self._cascade_delete_license(key)

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="delete_license",
            details={"license_key": key, "name": data.get("name")},
        )

        await update.callback_query.edit_message_text(
            f"✅ License `{key}` ({name}) deleted.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
            ),
        )

    async def _bulk_deactivate_expired(self, update: Update):
        if not self._is_admin(update):
            return
        now = datetime.now()
        deactivated = []
        skipped = []

        for key, data in list(self.client_manager.clients.items()):
            if data.get("status") != "active":
                continue
            expires_at = data.get("expires_at")
            if not expires_at:
                continue
            try:
                expiry = date_parser.parse(expires_at)
                if now > expiry:
                    data["status"] = "inactive"
                    self.client_manager.clients[key] = data
                    self._cascade_deactivate_license(key)
                    deactivated.append((key, data.get("name", "Unknown")))
            except Exception:
                skipped.append((key, data.get("name", "Unknown")))

        if deactivated:
            await self._save_clients_checked(update)
            lines = [f"🔴 *Deactivated {len(deactivated)} Expired Licenses*\n{SEP}"]
            for key, name in deactivated:
                lines.append(f"• {_escape_md(name, version=1)} (`{key}`)")
            if skipped:
                lines.append(f"\n⚠️ {len(skipped)} license(s) skipped due to parse errors")
        else:
            if skipped:
                lines = [
                    f"⚠️ No licenses deactivated, but {len(skipped)} had unparseable expiry dates"
                ]
            else:
                lines = ["✅ No expired licenses found to deactivate."]

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="bulk_deactivate_expired",
            details={"count": len(deactivated)},
        )

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
            ),
        )

    async def _bulk_activate_all(self, update: Update):
        if not self._is_admin(update):
            return
        activated = []

        for key, data in list(self.client_manager.clients.items()):
            if data.get("status") != "active":
                data["status"] = "active"
                self.client_manager.clients[key] = data
                activated.append((key, data.get("name", "Unknown")))

        if activated:
            await self._save_clients_checked(update)
            lines = [f"🟢 *Activated {len(activated)} Licenses*\n{SEP}"]
            for key, name in activated:
                lines.append(f"• {_escape_md(name, version=1)} (`{key}`)")
        else:
            lines = ["✅ All licenses are already active."]

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="bulk_activate_all",
            details={"count": len(activated)},
        )

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")]]
            ),
        )

    async def _force_disconnect_client(self, update: Update, license_key: str):
        if not self._is_admin(update):
            return
        data = self.client_manager.get_client_by_license(license_key)
        if not data:
            await update.callback_query.edit_message_text(
                "❌ License not found.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="lic_list")]]
                ),
            )
            return

        disconnected = []
        had_http = license_key in self.http_polling_clients
        if self.conn_manager:
            self.conn_manager.cleanup_client_state(license_key)
        else:
            self.http_polling_clients.pop(license_key, None)
            self.signal_queues.pop(license_key, None)
        if had_http:
            disconnected.append("HTTP Polling")

        # Disconnect WebSocket connections for this license
        if self.ws_manager:
            try:
                ws_conns = self.ws_manager.get_connections_for_key(license_key)
                for ws in list(ws_conns):
                    try:
                        asyncio.get_running_loop().create_task(
                            ws.close(code=4002, reason="Force disconnect by admin")
                        )
                    except Exception:
                        logger.debug(
                            "Failed to close WS in force disconnect for %s",
                            license_key,
                            exc_info=True,
                        )
                if ws_conns:
                    disconnected.append("WebSocket")
            except Exception:
                logger.debug("Failed to disconnect WS for %s", license_key, exc_info=True)

        name = _escape_md(data.get("name", "Unknown"), version=1)
        methods = ", ".join(disconnected) if disconnected else "none"

        await self._log_admin_action(
            user_id=update.effective_user.id,
            username=update.effective_user.username,
            action="force_disconnect",
            details={"license_key": license_key, "methods": methods},
        )

        await update.callback_query.edit_message_text(
            f"🔌 *Force Disconnected*\n\n"
            f"License: `{license_key}`\n"
            f"Name: {name}\n"
            f"Disconnected: {methods}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📄 View Details", callback_data=f"lic_info_{license_key}"
                        )
                    ],
                    [InlineKeyboardButton("◀️ Back", callback_data="menu_licenses")],
                ]
            ),
        )

    async def _show_license_picker(
        self, update: Update, prefix: str, title: str, subtitle: str, page: int = 0
    ):
        PAGE_SIZE = 8
        clients = list(self.client_manager.clients.items())
        page, total_pages, start = calc_pagination(page, len(clients), PAGE_SIZE)

        page_clients = clients[start : start + PAGE_SIZE]

        lines = [f"{title} (Page {page + 1}/{total_pages})\n{SEP}\n{subtitle}"]

        keyboard = []
        for key, data in page_clients:
            status_emoji = _status_emoji(data.get("status"))
            name = truncate(data.get("name", "Unknown"), 25)
            safe_name = _escape_md(name, version=1)
            lines.append(f"{status_emoji} `{key}` — {safe_name}")
            keyboard.append(
                [InlineKeyboardButton(f"{status_emoji} {name}", callback_data=f"{prefix}_{key}")]
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{prefix}_page_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"{prefix}_page_{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="menu_licenses")])

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_user_licenses(self, update: Update):
        """Show all licenses browsable by this admin with credentials."""
        chat_id = update.effective_chat.id
        licenses = self._get_licenses_for_chat_id(chat_id)
        if not licenses:
            text = "No linked licenses."
        else:
            lines = ["Your Licenses", SEP]
            for lic in licenses:
                client = self.client_manager.get_client_by_license(lic) or {}
                name = _escape_md(client.get("name", "Unknown"), version=1)
                status = client.get("status", "unknown")
                secret = client.get("secret_key", "")
                expires = _escape_md(str(client.get("expires_at") or "Lifetime"), version=1)
                status_emoji = _status_emoji(status) if status else "[?]"
                lines.append(f"{status_emoji} | {name}")
                lines.append(f"  License Key: `{lic}`")
                lines.append(f"  Secret Key: `{secret}`")
                lines.append(f"  Expires: {expires}")
                lines.append("")
            lines.append(SEP)
            lines.append("Use these in your TradingView strategy inputs.")
            text = "\n".join(lines)

        keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="user_settings")]]
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
