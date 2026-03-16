from __future__ import annotations

import logging

import requests

LOGGER = logging.getLogger(__name__)
_SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        url = _SEND_MESSAGE_URL.format(token=self.token)
        try:
            response = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            response.raise_for_status()
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Telegram alert failed: %s", error)

    @classmethod
    def from_env(cls, token: str | None, chat_id: str | None) -> "TelegramAlerter | None":
        if not token or not chat_id:
            return None
        return cls(token=token.strip(), chat_id=chat_id.strip())
