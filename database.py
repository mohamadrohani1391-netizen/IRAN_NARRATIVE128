"""لایهٔ ذخیره‌سازی (SQLite). مستقل از پلتفرم پیام‌رسان."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import config


def normalize_name(name: str) -> str:
    """یکسان‌سازی نام (ی/ک عربی، نیم‌فاصله، فاصله‌های اضافه) برای مقایسهٔ مطمئن."""
    name = (name.replace("ي", "ی").replace("ك", "ک")
                .replace("\u200c", " ").replace("\u200f", "").replace("\u200e", ""))
    return " ".join(name.split()).casefold()


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class Database:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name_norm TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                caption TEXT DEFAULT '',
                created_by TEXT,
                created_at TEXT);
            CREATE TABLE IF NOT EXISTS feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                text TEXT NOT NULL,
                created_at TEXT,
                is_read INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY,
                state TEXT,
                data TEXT DEFAULT '{}',
                is_admin INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                locked_until REAL DEFAULT 0);
            """)
        # مقادیر پیش‌فرض
        defaults = {
            "bot_name": config.DEFAULT_BOT_NAME,
            "bot_active": "1",
            "about_text": config.DEFAULT_ABOUT,
        }
        for k, v in defaults.items():
            if self.get_setting(k) is None:
                self.set_setting(k, v)
        if self.get_setting("admin_password_hash") is None:
            self.set_setting("admin_password_hash", hash_password(config.DEFAULT_ADMIN_PASSWORD))
        if not self.list_admins():
            self.add_admin(config.DEFAULT_ADMIN_NAME)

    # ---------- تنظیمات ----------
    def get_setting(self, key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- ادمین‌ها ----------
    def list_admins(self) -> list[str]:
        with self._conn() as c:
            return [r["display_name"] for r in c.execute("SELECT display_name FROM admins")]

    def is_admin_name(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM admins WHERE name_norm=?",
                             (normalize_name(name),)).fetchone() is not None

    def add_admin(self, name: str) -> bool:
        try:
            with self._conn() as c:
                c.execute("INSERT INTO admins(name_norm, display_name) VALUES(?,?)",
                          (normalize_name(name), " ".join(name.split())))
            return True
        except sqlite3.IntegrityError:
            return False

    def check_admin_password(self, password: str) -> bool:
        return verify_password(password, self.get_setting("admin_password_hash"))

    def set_admin_password(self, password: str) -> None:
        self.set_setting("admin_password_hash", hash_password(password))

    # ---------- پست‌ها ----------
    def add_post(self, file_id: str, caption: str, created_by: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO posts(file_id,caption,created_by,created_at) VALUES(?,?,?,?)",
                            (file_id, caption, created_by, _now()))
            return cur.lastrowid

    def count_posts(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    def list_posts(self, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()

    def get_post(self, post_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def delete_post(self, post_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM posts WHERE id=?", (post_id,))

    # ---------- نظرات ----------
    def add_feedback(self, user_id: str, text: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO feedbacks(user_id,text,created_at) VALUES(?,?,?)",
                      (user_id, text, _now()))

    def list_feedbacks(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM feedbacks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def count_unread(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM feedbacks WHERE is_read=0").fetchone()[0]

    def mark_all_read(self) -> None:
        with self._conn() as c:
            c.execute("UPDATE feedbacks SET is_read=1")

    # ---------- وضعیت کاربران (state machine) ----------
    def get_session(self, user_id: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {"state": None, "data": {}, "is_admin": False, "fail_count": 0, "locked_until": 0.0}
        return {"state": row["state"], "data": json.loads(row["data"] or "{}"),
                "is_admin": bool(row["is_admin"]), "fail_count": row["fail_count"],
                "locked_until": row["locked_until"]}

    def update_session(self, user_id: str, **fields) -> None:
        allowed = {"state", "data", "is_admin", "fail_count", "locked_until"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("فیلد نامعتبر برای session")
        if "data" in fields:
            fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
        if "is_admin" in fields:
            fields["is_admin"] = int(fields["is_admin"])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO sessions(user_id) VALUES(?)", (user_id,))
            c.execute(f"UPDATE sessions SET {sets} WHERE user_id=?", (*fields.values(), user_id))

    def clear_state(self, user_id: str) -> None:
        self.update_session(user_id, state=None, data={})
