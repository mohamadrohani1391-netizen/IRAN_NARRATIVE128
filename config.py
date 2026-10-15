"""تنظیمات اولیه ربات. توکن را از متغیر محیطی بخوانید (در کد ننویسید)."""
import os

BOT_TOKEN = os.environ.get("RUBIKA_BOT_TOKEN", "")
DB_PATH = os.environ.get("BOT_DB_PATH", "bot.db")

# مقادیر پیش‌فرض (فقط در اولین اجرا در دیتابیس ثبت می‌شوند)
DEFAULT_BOT_NAME = "IRAN NARRATIVE"
DEFAULT_ADMIN_NAME = "محمد امین روحانی"
DEFAULT_ADMIN_PASSWORD = "@IRAN NARRATIVE"
DEFAULT_ABOUT = "هنوز متنی برای «درباره ما» ثبت نشده است."
