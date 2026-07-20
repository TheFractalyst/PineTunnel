"""Telegram inline keyboard helpers."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .helpers import is_benign_edit_error

_logger = logging.getLogger(__name__)


async def respond(update, text: str, keyboard: list, parse_mode: str = ParseMode.HTML) -> None:
    """Send or edit a message — handles both callback_query and new message."""
    markup = InlineKeyboardMarkup(keyboard)
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=parse_mode,
                reply_markup=markup,
            )
            return
        if update.message:
            await update.message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=markup,
            )
    except TelegramError as e:
        if is_benign_edit_error(e):
            return
        raise


def nav_row(page: int, total_pages: int, prefix: str) -> list:
    """Prev/Next navigation row. Returns [] if no navigation needed."""
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{prefix}{page - 1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{prefix}{page + 1}"))
    return row
