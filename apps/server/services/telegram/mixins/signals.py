import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown as _escape_md

from ..helpers import SEP, _sanitize_error, calc_pagination, truncate
from ..keyboards import respond

logger = logging.getLogger(__name__)

_SIG_STATUS_MAP = {"all": "all", "pen": "pending", "ack": "acknowledged"}
_SIG_CODE_MAP = {"all": "all", "pending": "pen", "acknowledged": "ack"}
_SIG_FILTER_LABELS = {"all": "All", "pending": "Pending", "acknowledged": "Acknowledged"}
_SIG_SKIP_KEYS = frozenset({"type", "timestamp", "queued_at"})


class SignalMixin:
    """Signal tracking: license picker, signal list, signal detail."""

    async def _cmd_signals(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_admin(update):
            return
        await self._show_signals_menu(update)

    async def _show_signals_menu(self, update: Update):
        await self._show_signals_license_picker(update, page=0)

    async def _show_signals_license_picker(self, update: Update, page: int = 0):
        PAGE_SIZE = 8
        clients = list(self.client_manager.clients.items())
        page, total_pages, start = calc_pagination(page, len(clients), PAGE_SIZE)

        page_clients = clients[start : start + PAGE_SIZE]

        lines = [
            f"📡 *Signal Tracking* (Page {page + 1}/{total_pages})\n{SEP}\nSelect a license to view its signals:"
        ]

        keyboard = []
        for key, data in page_clients:
            name = truncate(data.get("name", "Unknown"), 20)
            try:
                stats = self.db_manager.get_signal_stats_by_license(key)
                total = stats.get("total", 0)
                badge = f" [{total}]" if total > 0 else ""
            except Exception:
                badge = ""
            status_emoji = "🟢" if data.get("status") == "active" else "🔴"
            lines.append(f"{status_emoji} `{key}` — {_escape_md(name, version=1)}{badge}")
            keyboard.append(
                [InlineKeyboardButton(f"📡 {name}{badge}", callback_data=f"sig_lic_{key}")]
            )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"sig_pg_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"sig_pg_{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("◀️ Back to Menu", callback_data="menu_main")])

        await respond(update, "\n".join(lines), keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _show_license_signals_overview(self, update: Update, license_key: str):
        client = self.client_manager.get_client_by_license(license_key) or {}
        name = _escape_md(client.get("name", "Unknown"), version=1)

        try:
            stats = self.db_manager.get_signal_stats_by_license(license_key)
        except Exception as e:
            await update.callback_query.edit_message_text(
                f"⚠️ Error fetching signal stats: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="sig_menu")]]
                ),
            )
            return

        total = stats.get("total", 0)
        pending = stats.get("pending", 0)
        acked = stats.get("acknowledged", 0)
        expired = total - pending - acked

        text = (
            f"📡 *Signals — {name}*\n"
            f"{SEP}\n"
            f"License: `{license_key}`\n"
            f"{SEP}\n"
            f"📊 Total: {total}\n"
            f"✅ Acknowledged: {acked}\n"
            f"⏳ Pending: {pending}\n"
        )
        if expired > 0:
            text += f"⏰ Other: {expired}\n"

        keyboard = [
            [InlineKeyboardButton("📋 All Signals", callback_data=f"sig_v_{license_key}_all_0")],
            [
                InlineKeyboardButton(
                    f"⏳ Pending ({pending})", callback_data=f"sig_v_{license_key}_pen_0"
                ),
                InlineKeyboardButton(
                    f"✅ Ack'd ({acked})", callback_data=f"sig_v_{license_key}_ack_0"
                ),
            ],
            [InlineKeyboardButton("◀️ Back", callback_data="sig_menu")],
        ]

        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_signal_list_from_callback(self, update: Update, payload: str):
        parts = payload.rsplit("_", 2)
        if len(parts) != 3:
            await update.callback_query.edit_message_text(
                "❌ Invalid signal query.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="sig_menu")]]
                ),
            )
            return

        license_key, status_code, page_str = parts
        try:
            page = int(page_str)
        except ValueError:
            page = 0

        status_filter = _SIG_STATUS_MAP.get(status_code, "all")

        await self._show_signal_list(update, license_key, status_filter, page)

    async def _show_signal_list(
        self, update: Update, license_key: str, status_filter: str = "all", page: int = 0
    ):
        PAGE_SIZE = 5
        client = self.client_manager.get_client_by_license(license_key) or {}
        name = _escape_md(client.get("name", "Unknown"), version=1)

        code_map = {"all": "all", "pending": "pen", "acknowledged": "ack"}
        status_code = code_map.get(status_filter, "all")
        filter_label = {"all": "All", "pending": "Pending", "acknowledged": "Acknowledged"}.get(
            status_filter, "All"
        )

        offset = page * PAGE_SIZE

        try:
            signals = self.db_manager.get_signals_by_license(
                license_key, status_filter=status_filter, limit=PAGE_SIZE + 1, offset=offset
            )
        except Exception as e:
            await update.callback_query.edit_message_text(
                f"⚠️ Error: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data=f"sig_lic_{license_key}")]]
                ),
            )
            return

        has_next = len(signals) > PAGE_SIZE
        signals = signals[:PAGE_SIZE]

        lines = [f"📡 *{name}* — {filter_label}\n{SEP}"]

        keyboard = []
        if not signals:
            lines.append("\n_No signals found._")
        else:
            for sig in signals:
                sd = sig.get("signal_data", {})
                status = sig.get("status", "?")
                created = sig.get("created_at", "")[:16] if sig.get("created_at") else "?"
                action = sd.get("action", "?").upper()
                symbol = sd.get("symbol", "?")
                risk = sd.get("risk", "")
                risk_str = f" r={risk}" if risk else ""

                s_emoji = (
                    "✅" if status == "acknowledged" else "⏳" if status == "pending" else "⚪"
                )
                lines.append(
                    f"{s_emoji} `{created}` | {action} {_escape_md(symbol, version=1)}{risk_str}"
                )

                sig_id = sig.get("signal_id", "")
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{s_emoji} {created} {action} {symbol}",
                            callback_data=f"sig_d_{sig_id}",
                        )
                    ]
                )

        nav = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️ Prev", callback_data=f"sig_v_{license_key}_{status_code}_{page - 1}"
                )
            )
        if has_next:
            nav.append(
                InlineKeyboardButton(
                    "Next ▶️", callback_data=f"sig_v_{license_key}_{status_code}_{page + 1}"
                )
            )
        if nav:
            keyboard.append(nav)

        keyboard.append(
            [InlineKeyboardButton("◀️ Back to Overview", callback_data=f"sig_lic_{license_key}")]
        )

        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_signal_detail(self, update: Update, signal_id: str):
        try:
            rows = self.db_manager.execute_query(
                """
                SELECT signal_id, license_key, signal_data, status,
                       created_at, acknowledged_at
                FROM signal_queue WHERE signal_id = :sid
                """,
                {"sid": signal_id},
            )

            if not rows:
                await update.callback_query.edit_message_text(
                    f"❌ Signal `{signal_id}` not found (may have been cleaned up).",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("◀️ Back", callback_data="sig_menu")]]
                    ),
                )
                return

            sig = rows[0]
            try:
                sd = json.loads(sig["signal_data"])
            except (json.JSONDecodeError, TypeError):
                sd = {}

            license_key = sig.get("license_key", "?")
            client = self.client_manager.get_client_by_license(license_key) or {}
            client_name = _escape_md(client.get("name", "Unknown"), version=1)

            status = sig.get("status", "?")
            s_emoji = "✅" if status == "acknowledged" else "⏳" if status == "pending" else "⚪"
            created = sig.get("created_at", "N/A")
            acked = sig.get("acknowledged_at") or "—"

            param_lines = []
            for k, v in sd.items():
                if k not in _SIG_SKIP_KEYS:
                    param_lines.append(f"| {_escape_md(k, version=1)}: `{v}`")
            if param_lines:
                param_lines[-1] = "-" + param_lines[-1][1:]

            params_text = "\n".join(param_lines) if param_lines else "- _(no parameters)_"

            text = (
                f"📡 *Signal Detail*\n"
                f"{SEP}\n"
                f"ID: `{signal_id}`\n"
                f"License: {client_name} (`{license_key}`)\n"
                f"Status: {s_emoji} {status}\n"
                f"Created: {created}\n"
                f"Ack'd: {acked}\n"
                f"{SEP}\n"
                f"{params_text}"
            )

            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Back to Overview", callback_data=f"sig_lic_{license_key}"
                            )
                        ],
                    ]
                ),
            )

        except Exception as e:
            logger.error("Error showing signal detail %s: %s", signal_id, e)
            await update.callback_query.edit_message_text(
                f"⚠️ Error loading signal: {_sanitize_error(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Back", callback_data="sig_menu")]]
                ),
            )
