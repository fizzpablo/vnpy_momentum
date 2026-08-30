"""Privacy-preserving Telegram event notifier; alerts never unblock trading."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

from .config import AlertConfig


class TelegramAlerts:
    def __init__(self, config: AlertConfig) -> None:
        self.chat_id = config.telegram_chat_id
        self.token = os.environ.get(config.telegram_token_env, "") if config.telegram_token_env else ""

    def send_event(self, event: str) -> None:
        """Send only an allowlisted event name, never trading or account details."""
        if not self.token or not self.chat_id:
            return
        allowed = {
            "HALTED", "ORDER_REJECTED", "GATEWAY_DISCONNECTED", "LOSS_LIMIT",
            "PROCESS_EXIT", "DISK_LOW",
        }
        if event not in allowed:
            return
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps({"chat_id": self.chat_id, "text": f"IBKR strategy event: {event}"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=5):
                pass
        except OSError:
            pass
