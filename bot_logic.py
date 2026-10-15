"""منطق ربات؛ کاملاً مستقل از پلتفرم. فقط از رابط MessengerAPI استفاده می‌کند."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from database import Database

log = logging.getLogger(__name__)

PAGE_SIZE = 8
MAX_LOGIN_FAILS = 5
LOCK_SECONDS = 300
MAX_CHUNK = 3500
MAX_FEEDBACK_LEN = 2000

Rows = list[list[tuple[str, str]]]  # [[(button_id, label), ...], ...]


@dataclass
class Event:
    chat_id: str
    user_id: str
    text: str = ""
    button_id: Optional[str] = None
    file_id: Optional[str] = None
    message_id: Optional[str] = None


class MessengerAPI(Protocol):
    def send_text(self, chat_id: str, text: str, rows: Optional[Rows] = None) -> None: ...
    def send_file(self, chat_id: str, file_id: str, caption: str = "") -> None: ...
    def delete_message(self, chat_id: str, message_id: str) -> None: ...


def _chunks(text: str, size: int = MAX_CHUNK) -> list[str]:
    out, cur = [], ""
    for block in text.split("\n———\n"):
        if len(cur) + len(block) > size and cur:
            out.append(cur)
            cur = ""
        cur += block + "\n———\n"
    if cur:
        out.append(cur)
    return [o[:4000] for o in out]


class Bot:
    def __init__(self, db: Database, api: MessengerAPI):
        self.db = db
        self.api = api

    # ================= نقطهٔ ورود =================
    def handle(self, ev: Event) -> None:
        session = self.db.get_session(ev.user_id)
        if ev.button_id:
            self._on_button(ev, session)
        else:
            self._on_message(ev, session)

    # ================= ابزارهای کمکی =================
    def _say(self, ev: Event, text: str, rows: Optional[Rows] = None) -> None:
        self.api.send_text(ev.chat_id, text, rows)

    def _safe_delete(self, ev: Event) -> None:
        """حذف پیام حاوی رمز از چت (در صورت پشتیبانی؛ خطا نادیده گرفته می‌شود)."""
        if ev.message_id:
            try:
                self.api.delete_message(ev.chat_id, ev.message_id)
            except Exception:  # noqa: BLE001
                pass

    def _cancel_rows(self) -> Rows:
        return [[("cancel", "❌ انصراف")]]

    # ================= منوها =================
    def _main_menu(self, ev: Event) -> None:
        self._say(ev, "یکی از بخش‌های زیر را انتخاب کنید:",
                  [[("menu:contacts", "👥 مخاطبین"), ("menu:admins", "🛡 ادمین‌ها")]])

    def _contacts_home(self, ev: Event) -> None:
        name = self.db.get_setting("bot_name")
        self._say(ev,
                  f"به ربات اختصاصی تیم {name} خوش آمدید جهت بهره برداری از امکانات ربات "
                  "روی یکی از دکمه های زیر کلیک کنید",
                  [[("c:posts:0", "📂 لیست پست ها")],
                   [("c:feedback", "💬 ارسال نظرات")],
                   [("c:about", "ℹ️ درباره ما")],
                   [("home", "🏠 بازگشت به منوی اصلی")]])

    def _admin_panel(self, ev: Event) -> None:
        active = self.db.get_setting("bot_active") == "1"
        unread = self.db.count_unread()
        text = (f"🛡 پنل مدیریت ربات «{self.db.get_setting('bot_name')}»\n"
                f"وضعیت ربات: {'✅ فعال' if active else '⛔️ غیرفعال (حالت تعمیر)'}\n"
                f"نظرات خوانده‌نشده: {unread}\n"
                f"تعداد پست‌ها: {self.db.count_posts()}")
        self._say(ev, text, [
            [("a:addpost", "➕ افزودن فایل/ویدیو"), ("a:delposts", "🗑 حذف پست")],
            [("a:feedbacks", f"📬 نظرات ({unread})"), ("a:editabout", "✏️ ویرایش درباره ما")],
            [("a:rename", "🏷 تغییر نام ربات"), ("a:passwd", "🔑 تغییر رمز")],
            [("a:addadmin", "👤 افزودن ادمین"),
             ("a:toggle", "⛔️ غیرفعال‌سازی ربات" if active else "✅ فعال‌سازی ربات")],
            [("a:logout", "🚪 خروج از پنل"), ("home", "🏠 منوی اصلی")],
        ])

    # ================= پیام‌های متنی/فایل =================
    def _on_message(self, ev: Event, s: dict) -> None:
        if ev.text.lower() in ("/start", "start", "شروع", "/menu"):
            self.db.clear_state(ev.user_id)
            return self._main_menu(ev)

        handlers = {
            "admin_name": self._st_admin_name,
            "admin_pass": self._st_admin_pass,
            "feedback": self._st_feedback,
            "rename": self._st_rename,
            "passwd": self._st_passwd,
            "addadmin": self._st_addadmin,
            "editabout": self._st_editabout,
            "post_file": self._st_post_file,
            "post_caption": self._st_post_caption,
        }
        handler = handlers.get(s["state"])
        if not handler:
            return self._main_menu(ev)
        admin_states = {"rename", "passwd", "addadmin", "editabout", "post_file", "post_caption"}
        if s["state"] in admin_states and not s["is_admin"]:
            self.db.clear_state(ev.user_id)
            return self._main_menu(ev)
        handler(ev, s)

    # ---------- ورود ادمین ----------
    def _register_fail(self, ev: Event, s: dict, msg: str) -> None:
        fails = s["fail_count"] + 1
        if fails >= MAX_LOGIN_FAILS:
            self.db.update_session(ev.user_id, state=None, data={}, fail_count=0,
                                   locked_until=time.time() + LOCK_SECONDS)
            self._say(ev, f"⛔️ تعداد تلاش‌های ناموفق زیاد بود. تا {LOCK_SECONDS // 60} دقیقهٔ دیگر "
                          "امکان ورود ندارید.", [[("home", "🏠 منوی اصلی")]])
        else:
            self.db.update_session(ev.user_id, fail_count=fails)
            self._say(ev, f"{msg}\nلطفاً دوباره تلاش کنید.", self._cancel_rows())

    def _st_admin_name(self, ev: Event, s: dict) -> None:
        if not ev.text:
            return self._say(ev, "لطفاً نام ادمین را به صورت متن بفرستید.", self._cancel_rows())
        if self.db.is_admin_name(ev.text):
            self.db.update_session(ev.user_id, state="admin_pass", data={})
            self._say(ev, "🔑 رمز عبور را وارد کنید:", self._cancel_rows())
        else:
            self._register_fail(ev, s, "❌ این نام در لیست ادمین‌ها نیست.")

    def _st_admin_pass(self, ev: Event, s: dict) -> None:
        self._safe_delete(ev)
        if ev.text and self.db.check_admin_password(ev.text.strip()):
            self.db.update_session(ev.user_id, state=None, data={}, is_admin=True, fail_count=0)
            self._say(ev, "✅ ورود موفق بود.")
            self._admin_panel(ev)
        else:
            self._register_fail(ev, s, "❌ رمز عبور اشتباه است.")

    # ---------- حالت‌های مخاطب ----------
    def _st_feedback(self, ev: Event, s: dict) -> None:
        if not ev.text:
            return self._say(ev, "لطفاً نظر خود را به صورت متن بفرستید.", self._cancel_rows())
        self.db.add_feedback(ev.user_id, ev.text[:MAX_FEEDBACK_LEN])
        self.db.clear_state(ev.user_id)
        self._say(ev, "🙏 نظر شما ثبت شد و به دست تیم ادمین‌ها می‌رسد. سپاس!",
                  [[("c:home", "🏠 بازگشت")]])

    # ---------- حالت‌های ادمین ----------
    def _st_rename(self, ev: Event, s: dict) -> None:
        if not ev.text or len(ev.text) > 60:
            return self._say(ev, "نام باید متن و حداکثر ۶۰ کاراکتر باشد.", self._cancel_rows())
        self.db.set_setting("bot_name", ev.text.strip())
        self.db.clear_state(ev.user_id)
        self._say(ev, f"✅ نام ربات به «{ev.text.strip()}» تغییر کرد.")
        self._admin_panel(ev)

    def _st_passwd(self, ev: Event, s: dict) -> None:
        self._safe_delete(ev)
        pw = ev.text.strip()
        if len(pw) < 6:
            return self._say(ev, "رمز باید حداقل ۶ کاراکتر باشد. دوباره بفرستید:", self._cancel_rows())
        self.db.set_admin_password(pw)
        self.db.clear_state(ev.user_id)
        self._say(ev, "✅ رمز ورود ادمین تغییر کرد.")
        self._admin_panel(ev)

    def _st_addadmin(self, ev: Event, s: dict) -> None:
        if not ev.text:
            return self._say(ev, "نام ادمین جدید را به صورت متن بفرستید.", self._cancel_rows())
        ok = self.db.add_admin(ev.text)
        self.db.clear_state(ev.user_id)
        self._say(ev, f"✅ «{ev.text.strip()}» به ادمین‌ها اضافه شد." if ok
                  else "⚠️ این نام قبلاً در لیست ادمین‌ها بوده است.")
        self._admin_panel(ev)

    def _st_editabout(self, ev: Event, s: dict) -> None:
        if not ev.text:
            return self._say(ev, "متن «درباره ما» را بفرستید.", self._cancel_rows())
        self.db.set_setting("about_text", ev.text)
        self.db.clear_state(ev.user_id)
        self._say(ev, "✅ متن «درباره ما» ذخیره شد.")
        self._admin_panel(ev)

    def _st_post_file(self, ev: Event, s: dict) -> None:
        if not ev.file_id:
            return self._say(ev, "لطفاً یک فایل یا ویدیو بفرستید.", self._cancel_rows())
        if ev.text:  # کپشن همراه فایل ارسال شده
            return self._save_post(ev, ev.file_id, ev.text)
        self.db.update_session(ev.user_id, state="post_caption", data={"file_id": ev.file_id})
        self._say(ev, "📝 کپشن پست را بنویسید (برای بدون کپشن، علامت - بفرستید):",
                  self._cancel_rows())

    def _st_post_caption(self, ev: Event, s: dict) -> None:
        if not ev.text:
            return self._say(ev, "کپشن را به صورت متن بفرستید یا - بزنید.", self._cancel_rows())
        caption = "" if ev.text.strip() == "-" else ev.text
        self._save_post(ev, s["data"]["file_id"], caption)

    def _save_post(self, ev: Event, file_id: str, caption: str) -> None:
        pid = self.db.add_post(file_id, caption, ev.user_id)
        self.db.clear_state(ev.user_id)
        self._say(ev, f"✅ پست شمارهٔ {pid} ثبت شد.")
        self._admin_panel(ev)

    # ================= دکمه‌ها =================
    def _on_button(self, ev: Event, s: dict) -> None:
        bid = ev.button_id or ""
        parts = bid.split(":")
        self.db.clear_state(ev.user_id)  # هر دکمه، گفتگوی نیمه‌کاره را لغو می‌کند

        if bid == "home" or bid == "cancel":
            return self._admin_panel(ev) if (bid == "cancel" and s["is_admin"]) else self._main_menu(ev)
        if bid == "menu:contacts" or bid == "c:home":
            return self._guard_active(ev, s, self._contacts_home)
        if bid == "menu:admins":
            return self._admin_entry(ev, s)
        if parts[0] == "c":
            return self._guard_active(ev, s, lambda e: self._contact_action(e, parts))
        if parts[0] == "a":
            if not s["is_admin"]:
                return self._say(ev, "⛔️ دسترسی ندارید. ابتدا از بخش ادمین‌ها وارد شوید.",
                                 [[("menu:admins", "🛡 ورود ادمین")]])
            return self._admin_action(ev, s, parts)

    def _guard_active(self, ev: Event, s: dict, fn) -> None:
        if self.db.get_setting("bot_active") != "1" and not s["is_admin"]:
            return self._say(ev, "🛠 ربات موقتاً غیرفعال است. لطفاً بعداً مراجعه کنید.",
                             [[("home", "🏠 منوی اصلی")]])
        fn(ev)

    def _admin_entry(self, ev: Event, s: dict) -> None:
        if s["is_admin"]:
            return self._admin_panel(ev)
        if s["locked_until"] > time.time():
            mins = int((s["locked_until"] - time.time()) // 60) + 1
            return self._say(ev, f"⛔️ ورود موقتاً مسدود است. حدود {mins} دقیقهٔ دیگر تلاش کنید.",
                             [[("home", "🏠 منوی اصلی")]])
        self.db.update_session(ev.user_id, state="admin_name", data={})
        self._say(ev, "👤 نام ادمین را وارد کنید:", self._cancel_rows())

    # ---------- اکشن‌های مخاطب ----------
    def _contact_action(self, ev: Event, parts: list[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        if action == "posts":
            return self._show_posts(ev, int(parts[2]) if len(parts) > 2 else 0)
        if action == "post":
            return self._send_post(ev, int(parts[2]))
        if action == "feedback":
            self.db.update_session(ev.user_id, state="feedback", data={})
            return self._say(ev, "💬 نظر، پیشنهاد یا انتقاد خود را بنویسید:", self._cancel_rows())
        if action == "about":
            return self._say(ev, "ℹ️ درباره ما\n\n" + self.db.get_setting("about_text"),
                             [[("c:home", "🏠 بازگشت")]])

    @staticmethod
    def _post_label(p) -> str:
        first = (p["caption"] or "").strip().split("\n")[0]
        return (first[:28] + "…" if len(first) > 28 else first) or f"پست شمارهٔ {p['id']}"

    def _show_posts(self, ev: Event, page: int) -> None:
        total = self.db.count_posts()
        if total == 0:
            return self._say(ev, "هنوز پستی ثبت نشده است.", [[("c:home", "🏠 بازگشت")]])
        posts = self.db.list_posts(PAGE_SIZE, page * PAGE_SIZE)
        rows: Rows = [[(f"c:post:{p['id']}", "📄 " + self._post_label(p))] for p in posts]
        nav = []
        if page > 0:
            nav.append((f"c:posts:{page - 1}", "◀️ قبلی"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append((f"c:posts:{page + 1}", "بعدی ▶️"))
        if nav:
            rows.append(nav)
        rows.append([("c:home", "🏠 بازگشت")])
        self._say(ev, f"📂 لیست پست‌ها (صفحهٔ {page + 1})", rows)

    def _send_post(self, ev: Event, post_id: int) -> None:
        post = self.db.get_post(post_id)
        if not post:
            return self._say(ev, "این پست دیگر موجود نیست.", [[("c:posts:0", "📂 لیست پست‌ها")]])
        try:
            self.api.send_file(ev.chat_id, post["file_id"], post["caption"] or "")
        except Exception:  # noqa: BLE001
            log.exception("send_file failed")
            self._say(ev, "⚠️ ارسال فایل با خطا مواجه شد. بعداً دوباره تلاش کنید.")
        self._say(ev, "ادامه:", [[("c:posts:0", "📂 لیست پست‌ها"), ("c:home", "🏠 بازگشت")]])

    # ---------- اکشن‌های ادمین ----------
    def _admin_action(self, ev: Event, s: dict, parts: list[str]) -> None:
        action = parts[1] if len(parts) > 1 else ""
        uid = ev.user_id
        prompts = {
            "rename": ("rename", "🏷 نام جدید ربات را بفرستید:"),
            "passwd": ("passwd", "🔑 رمز جدید را بفرستید (حداقل ۶ کاراکتر):"),
            "addadmin": ("addadmin", "👤 نام ادمین جدید را بفرستید:"),
            "addpost": ("post_file", "📎 فایل یا ویدیو را بفرستید (می‌توانید همراه آن کپشن هم بنویسید):"),
        }
        if action in prompts:
            state, text = prompts[action]
            self.db.update_session(uid, state=state, data={})
            return self._say(ev, text, self._cancel_rows())
        if action == "editabout":
            self.db.update_session(uid, state="editabout", data={})
            return self._say(ev, "✏️ متن فعلی:\n\n" + self.db.get_setting("about_text") +
                             "\n\nمتن جدید «درباره ما» را بفرستید:", self._cancel_rows())
        if action == "toggle":
            now_active = self.db.get_setting("bot_active") == "1"
            self.db.set_setting("bot_active", "0" if now_active else "1")
            return self._admin_panel(ev)
        if action == "feedbacks":
            return self._show_feedbacks(ev)
        if action == "delposts":
            return self._show_delete_list(ev)
        if action == "del":
            self.db.delete_post(int(parts[2]))
            self._say(ev, "🗑 پست حذف شد.")
            return self._show_delete_list(ev)
        if action == "menu":
            return self._admin_panel(ev)
        if action == "logout":
            self.db.update_session(uid, is_admin=False, state=None, data={})
            return self._say(ev, "🚪 از پنل خارج شدید.", [[("home", "🏠 منوی اصلی")]])

    def _show_feedbacks(self, ev: Event) -> None:
        items = self.db.list_feedbacks(10)
        if not items:
            return self._say(ev, "هنوز نظری ثبت نشده است.", [[("a:menu", "🔙 پنل")]])
        lines = [f"📬 {len(items)} نظر آخر (خوانده‌نشده: {self.db.count_unread()})\n———\n"]
        for f in items:
            mark = " 🆕" if not f["is_read"] else ""
            lines.append(f"#{f['id']} • {f['created_at']}{mark}\n{f['text']}\n———\n")
        self.db.mark_all_read()
        for chunk in _chunks("".join(lines)):
            self._say(ev, chunk)
        self._admin_panel(ev)

    def _show_delete_list(self, ev: Event) -> None:
        posts = self.db.list_posts(20)
        if not posts:
            self._say(ev, "پستی برای حذف وجود ندارد.")
            return self._admin_panel(ev)
        rows: Rows = [[(f"a:del:{p['id']}", f"🗑 #{p['id']} " + self._post_label(p))] for p in posts]
        rows.append([("cancel", "🔙 بازگشت به پنل")])
        self._say(ev, "کدام پست حذف شود؟ (۲۰ پست آخر)", rows)
