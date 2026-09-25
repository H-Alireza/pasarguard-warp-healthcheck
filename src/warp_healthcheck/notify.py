from __future__ import annotations

import html
import logging
import socket

import httpx

from warp_healthcheck.config import TelegramConfig

logger = logging.getLogger("warp-healthcheck")


class Notifier:
    """Sends Telegram messages. Does nothing when Telegram is not configured."""

    def __init__(self, config: TelegramConfig) -> None:
        self._config = config
        self._host = socket.gethostname()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def send(self, text: str) -> bool:
        """Send `text` (HTML). Returns False on failure; never raises."""
        if not self.enabled:
            return False
        body = f"<b>warp-healthcheck</b> · {html.escape(self._host)}\n{text}"
        url = f"https://api.telegram.org/bot{self._config.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(
                timeout=10.0, proxy=self._config.proxy or None
            ) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self._config.chat_id,
                        "text": body,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("Telegram send failed: %s", type(exc).__name__)
            return False
        if response.status_code != 200:
            logger.warning(
                "Telegram send failed: HTTP %s %s",
                response.status_code,
                response.text[:200],
            )
            return False
        return True
