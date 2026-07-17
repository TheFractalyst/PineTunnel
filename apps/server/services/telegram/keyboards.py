"""Shared Telegram keyboard helpers."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode


async def respond(update: Update, text: str, keyboard: list[list[InlineKeyboardButton]], parse_mode: str = ParseMode.HTML):
    """Send or edit a message with an inline keyboard."""
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=markup
        )
    else:
        await update.message.reply_text(
            text, parse_mode=parse_mode, reply_markup=markup
        )
