"""Personal accounts: the administrator creates a person with e-mail, name, role and password.

No self-registration and no mail: the administrator tells the password in person.
The owner's admin password from config.yaml keeps working (login left empty).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

ROLES = ("viewer", "admin")
MIN_PASSWORD = 8
_ITERATIONS = 200_000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    login      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL DEFAULT 'viewer',
    pw_hash    TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT,
    last_ip    TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest = stored.split("$")
        given = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(given, digest)


# compared against when the login is unknown, so a wrong login takes as long as a wrong password
_DUMMY = hash_password("graddok-dummy")


class UserError(ValueError):
    pass


def check_new(login: str | None = None, password: str | None = None, role: str | None = None) -> None:
    if login is not None and not _EMAIL.match(login):
        raise UserError("Логин — адрес e-mail, например ivanov@mail.ru")
    if password is not None and len(password) < MIN_PASSWORD:
        raise UserError(f"Пароль — не короче {MIN_PASSWORD} знаков")
    if role is not None and role not in ROLES:
        raise UserError("Роль: просмотр или администратор")


class Users:
    """Accounts in index.db (it is backed up daily)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @staticmethod
    def _public(row) -> dict:
        d = dict(row)
        d.pop("pw_hash", None)
        d["active"] = bool(d["active"])
        return d

    def list(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM users ORDER BY active DESC, name COLLATE NOCASE, login").fetchall()
        return [self._public(r) for r in rows]

    def get(self, user_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def any_active(self) -> bool:
        return self._conn.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone() is not None

    def add(self, login: str, name: str, role: str, password: str) -> dict:
        login = (login or "").strip().lower()
        check_new(login, password, role)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users(login,name,role,pw_hash,created_at) VALUES(?,?,?,?,?)",
                    (login, (name or "").strip(), role, hash_password(password), _now()))
            except sqlite3.IntegrityError:
                raise UserError(f"Пользователь {login} уже есть") from None
            self._conn.commit()
        return self._public(self.get(cur.lastrowid))

    def update(self, user_id: int, changes: dict) -> dict:
        if not self.get(user_id):
            raise UserError("Пользователь не найден")
        sets: dict = {}
        if "name" in changes:
            sets["name"] = str(changes["name"] or "").strip()
        if "role" in changes:
            check_new(role=changes["role"])
            sets["role"] = changes["role"]
        if "active" in changes:
            sets["active"] = 1 if changes["active"] else 0
        if changes.get("password"):
            check_new(password=changes["password"])
            sets["pw_hash"] = hash_password(changes["password"])
        if sets:
            with self._lock:
                self._conn.execute(
                    "UPDATE users SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                    (*sets.values(), user_id))
                self._conn.commit()
        return self._public(self.get(user_id))

    def delete(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._conn.commit()

    def authenticate(self, login: str, password: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE login=?", ((login or "").strip().lower(),)).fetchone()
        if row is None:
            verify_password(password, _DUMMY)
            return None
        if not verify_password(password, row["pw_hash"]) or not row["active"]:
            return None
        return dict(row)

    def touch(self, user_id: int, ip: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET last_login=?, last_ip=? WHERE id=?", (_now(), ip, user_id))
            self._conn.commit()

    def session_tag(self, user: dict) -> str:
        """Changes when the password, role or state changes: old sessions end."""
        raw = f"{user['pw_hash']}|{user['role']}|{user['active']}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
