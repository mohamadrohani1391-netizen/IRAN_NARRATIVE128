"""آداپتور API رسمی ربات‌های روبیکا (https://botapi.rubika.ir/v3/<TOKEN>/<method>).
برای انتقال به پلتفرم دیگر (بله، تلگرام، ...) فقط همین فایل و parse_update در main.py را بازنویسی کنید."""
from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class RubikaError(RuntimeError):
    pass


class RubikaAPI:
    def __init__(self, token: str):
        if not token:
            raise ValueError("توکن ربات تنظیم نشده است (RUBIKA_BOT_TOKEN).")
        self.base = f"https://botapi.rubika.ir/v3/{token}/"
        self.http = requests.Session()

    def _call(self, method: str, payload: dict) -> dict:
        resp = self.http.post(self.base + method, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "OK":
            raise RubikaError(f"{method}: {body}")
        return body.get("data") or {}

    def get_updates(self, offset_id: Optional[str] = None, limit: int = 100) -> dict:
        payload = {"limit": limit}
        if offset_id:
            payload["offset_id"] = offset_id
        return self._call("getUpdates", payload)

    def send_text(self, chat_id: str, text: str, rows=None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if rows:
            payload["inline_keypad"] = {"rows": [
                {"buttons": [{"id": bid, "type": "Simple", "button_text": label}
                             for bid, label in row]} for row in rows]}
        self._call("sendMessage", payload)

    def send_file(self, chat_id: str, file_id: str, caption: str = "") -> None:
        payload = {"chat_id": chat_id, "file_id": file_id}
        if caption:
            payload["text"] = caption
        self._call("sendFile", payload)

    def delete_message(self, chat_id: str, message_id: str) -> None:
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
