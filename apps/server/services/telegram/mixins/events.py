import logging

from telegram.constants import ParseMode
from telegram.helpers import escape_markdown as _escape_md

logger = logging.getLogger(__name__)


class EventMixin:
    """Admin notifications and server event hooks."""

    async def notify_admin(self, message: str):
        if not self._started or not self.app:
            return

        for admin_id in self.admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error("Failed to notify admin %s: %s", admin_id, e)

    async def on_trade_failure(self, license_key: str, error: str):
        if not self.alerts_enabled:
            return
        await self.notify_admin(
            f"Trade Failure\n"
            f"License: {license_key}\n"
            f"Error: {_escape_md(error, version=1)}"
        )

    async def on_trade_executed(self, report):
        if not self.alerts_enabled:
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
        if not self.alerts_enabled:
            return
        try:
            await self.notify_admin(
                f"Trade Execution Failed\n"
                f"License: {report.license_key}\n"
                f"Symbol: {report.symbol}\n"
                f"Error: {_escape_md(str(report.error), version=1)}"
            )
        except Exception as e:
            logger.error("Trade execution failed notification error: %s", e)

    async def on_position_closed(self, report):
        if not self.alerts_enabled:
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
