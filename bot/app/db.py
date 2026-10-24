"""БД бота: привязки telegram_id -> клиент панели, инвайты, семьи, аудит."""
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  email  TEXT PRIMARY KEY,
  family TEXT,
  head   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
  tg_id    INTEGER PRIMARY KEY,
  email    TEXT NOT NULL UNIQUE REFERENCES clients(email),
  username TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  bound_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code       TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  created_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used_tg_id INTEGER,
  used_at    INTEGER
);
CREATE TABLE IF NOT EXISTS pending_bind (
  username TEXT PRIMARY KEY,
  email    TEXT NOT NULL,
  created_by INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  tg_id  INTEGER NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
"""


def _now() -> int:
    return int(time.time())


class BotDB:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ---- реестр клиентов панели и семьи ----

    async def ensure_client(self, email: str) -> None:
        await self.db.execute("INSERT OR IGNORE INTO clients(email) VALUES (?)", (email,))
        await self.db.commit()

    async def get_client(self, email: str):
        cur = await self.db.execute("SELECT email, family, head FROM clients WHERE email=?", (email,))
        return await cur.fetchone()

    async def set_family(self, email: str, family: str, head: bool | None = None) -> None:
        await self.ensure_client(email)
        if head is None:
            await self.db.execute("UPDATE clients SET family=? WHERE email=?", (family, email))
        else:
            await self.db.execute(
                "UPDATE clients SET family=?, head=? WHERE email=?", (family, int(head), email)
            )
        await self.db.commit()

    async def clear_family(self, email: str) -> None:
        await self.db.execute("UPDATE clients SET family=NULL, head=0 WHERE email=?", (email,))
        await self.db.commit()

    async def family_members(self, family: str):
        cur = await self.db.execute(
            """SELECT c.email, c.head, u.tg_id, u.username
               FROM clients c LEFT JOIN users u ON u.email = c.email
               WHERE c.family = ? ORDER BY c.head DESC, c.email""",
            (family,),
        )
        return await cur.fetchall()

    async def all_clients(self):
        cur = await self.db.execute(
            """SELECT c.email, c.family, c.head, u.tg_id IS NOT NULL
               FROM clients c LEFT JOIN users u ON u.email = c.email
               ORDER BY c.family IS NOT NULL, c.family, c.email"""
        )
        return await cur.fetchall()

    async def all_families(self):
        cur = await self.db.execute(
            """SELECT c.family, c.email, c.head, u.tg_id IS NOT NULL
               FROM clients c LEFT JOIN users u ON u.email = c.email
               WHERE c.family IS NOT NULL ORDER BY c.family, c.head DESC, c.email"""
        )
        return await cur.fetchall()

    # ---- привязки пользователей ----

    async def bind_user(self, tg_id: int, email: str, username: str | None, is_admin: bool = False) -> None:
        await self.ensure_client(email)
        await self.db.execute(
            """INSERT INTO users(tg_id, email, username, is_admin, bound_at) VALUES (?,?,?,?,?)
               ON CONFLICT(tg_id) DO UPDATE SET email=excluded.email, username=excluded.username,
                   is_admin=excluded.is_admin, bound_at=excluded.bound_at""",
            (tg_id, email, username, int(is_admin), _now()),
        )
        await self.db.commit()

    async def touch_username(self, tg_id: int, username: str | None) -> None:
        await self.db.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
        await self.db.commit()

    async def get_user(self, tg_id: int):
        """(email, username, is_admin, family, head) или None."""
        cur = await self.db.execute(
            """SELECT u.email, u.username, u.is_admin, c.family, c.head
               FROM users u LEFT JOIN clients c ON c.email = u.email WHERE u.tg_id=?""",
            (tg_id,),
        )
        return await cur.fetchone()

    async def get_user_by_email(self, email: str):
        cur = await self.db.execute("SELECT tg_id, username, is_admin FROM users WHERE email=?", (email,))
        return await cur.fetchone()

    # ---- ожидающие привязки по @username ----

    async def add_pending_bind(self, username: str, email: str, created_by: int) -> None:
        await self.ensure_client(email)
        await self.db.execute(
            """INSERT INTO pending_bind(username, email, created_by, created_at) VALUES (?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET email=excluded.email,
                   created_by=excluded.created_by, created_at=excluded.created_at""",
            (username.lstrip("@").lower(), email, created_by, _now()),
        )
        await self.db.commit()

    async def take_pending_bind(self, username: str | None):
        if not username:
            return None
        cur = await self.db.execute(
            "DELETE FROM pending_bind WHERE username=? RETURNING email", (username.lstrip("@").lower(),)
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row[0] if row else None

    # ---- инвайты ----

    async def create_invite(self, code: str, email: str, created_by: int, ttl_days: int) -> None:
        await self.ensure_client(email)
        now = _now()
        await self.db.execute(
            "INSERT INTO invites(code, email, created_by, created_at, expires_at) VALUES (?,?,?,?,?)",
            (code, email, created_by, now, now + ttl_days * 86400),
        )
        await self.db.commit()

    async def take_invite(self, code: str, tg_id: int):
        """Атомарно погасить код. Вернёт email или None (нет/использован/просрочен)."""
        cur = await self.db.execute(
            """UPDATE invites SET used_tg_id=?, used_at=?
               WHERE code=? AND used_tg_id IS NULL AND expires_at > ?
               RETURNING email""",
            (tg_id, _now(), code, _now()),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row[0] if row else None

    async def active_invites(self):
        cur = await self.db.execute(
            """SELECT code, email, expires_at FROM invites
               WHERE used_tg_id IS NULL AND expires_at > ? ORDER BY created_at DESC""",
            (_now(),),
        )
        return await cur.fetchall()

    # ---- аудит ----

    async def audit(self, tg_id: int, action: str, detail: str = "") -> None:
        await self.db.execute(
            "INSERT INTO audit(ts, tg_id, action, detail) VALUES (?,?,?,?)", (_now(), tg_id, action, detail)
        )
        await self.db.commit()

    async def recent_audit(self, n: int = 20):
        cur = await self.db.execute(
            "SELECT ts, tg_id, action, detail FROM audit ORDER BY id DESC LIMIT ?", (n,)
        )
        return await cur.fetchall()
