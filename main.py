"""نقطه‌ی ورود ربات: دریافت آپدیت‌ها از روبیکا و ارسال پاسخ‌ها."""
from __future__ import annotations

import logging
import time
from typing import Optional

import config
from database import Database
from logic import BotLogic, Reply
from rubika_api import RubikaClient, RubikaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")


def extract(update: dict) -> Optional[tuple[str, str, Optional[dict]]]:
    """از آپدیت روبیکا (chat_id, text, file) را بیرون می‌کشد؛ در غیر این صورت None."""
    kind = update.get("type")
    chat_id = update.get("chat_id")
    if not chat_id:
        return None
    if kind == "StartedBot":
        return chat_id, "/start", None
    if kind == "NewMessage":
        msg = update.get("new_message") or {}
        if msg.get("sender_type") not in (None, "User"):
            return None
        return chat_id, msg.get("text") or "", msg.get("file")
    return None


def deliver(client: RubikaClient, chat_id: str, replies: list[Reply]) -> None:
    for reply in replies:
        try:
            if reply.file_id:
                client.send_file(chat_id, reply.file_id, reply.text)
            elif reply.text:
                client.send_message(chat_id, reply.text, reply.keyboard)
        except RubikaError as exc:
            log.error("ارسال ناموفق: %s", exc)
            if reply.file_id:  # جایگزین: حداقل کپشن را بفرست
                client.send_message(chat_id, "⚠️ ارسال فایل با خطا مواجه شد.\n" + reply.text)


def run() -> None:
    db = Database()
    logic = BotLogic(db)
    client = RubikaClient(config.BOT_TOKEN)
    log.info("ربات آماده است: %s", client.get_me())

    offset = db.get_setting("offset_id")
    while True:
        try:
            data = client.get_updates(offset)
            for update in data.get("updates", []):
                parsed = extract(update)
                if not parsed:
                    continue
                chat_id, text, file = parsed
                try:
                    deliver(client, chat_id, logic.handle(chat_id, text, file))
                except Exception:
                    log.exception("خطا در پردازش پیام chat_id=%s", chat_id)
            next_offset = data.get("next_offset_id")
            if next_offset:
                offset = next_offset
                db.set_setting("offset_id", offset)
        except KeyboardInterrupt:
            log.info("خروج.")
            break
        except Exception:
            log.exception("خطا در دریافت آپدیت‌ها؛ تلاش دوباره تا چند ثانیه‌ی دیگر")
            time.sleep(5)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    run()
