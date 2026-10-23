"""Read-only доступ к sqlite панели 3x-ui: subId клиентов и список нод.

Панель при этом работает (запись идёт через WAL/короткие транзакции sqlite),
поэтому подключаемся в режиме mode=ro и держим запросы короткими.
"""
import asyncio
import json
import socket
import time

import aiosqlite


class PanelError(Exception):
    pass


class PanelDB:
    def __init__(self, path: str):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        try:
            self.db = await aiosqlite.connect(f"file:{self.path}?mode=ro", uri=True)
        except Exception as e:
            raise PanelError(f"не удалось открыть БД панели: {e}") from e

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def client_subids(self, email: str) -> dict[int, str | None]:
        """subId клиента по каждому инбаунду: {inbound_id: subId}."""
        try:
            cur = await self.db.execute("SELECT id, settings FROM inbounds")
            rows = await cur.fetchall()
        except Exception as e:
            raise PanelError(f"запрос к БД панели не удался: {e}") from e
        subs: dict[int, str | None] = {}
        for iid, settings in rows:
            try:
                clients = json.loads(settings).get("clients", [])
            except Exception:
                continue
            for c in clients:
                if c.get("email") == email:
                    subs[iid] = c.get("subId")
        return subs

    async def all_client_emails(self) -> set[str]:
        cur = await self.db.execute("SELECT settings FROM inbounds")
        rows = await cur.fetchall()
        emails: set[str] = set()
        for (settings,) in rows:
            try:
                for c in json.loads(settings).get("clients", []):
                    if c.get("email"):
                        emails.add(c["email"])
            except Exception:
                continue
        return emails

    async def nodes(self) -> list[tuple[str, str, int]]:
        """[(remark, адрес, порт)] — адрес и Reality-порт каждой ноды."""
        cur = await self.db.execute(
            """SELECT i.remark, n.address, i.port FROM inbounds i
               JOIN nodes n ON n.id = i.node_id
               WHERE i.enable AND n.enable ORDER BY i.id"""
        )
        rows = await cur.fetchall()
        if not rows:
            raise PanelError("в БД панели нет связки инбаунд→нода")
        return [(remark, address, int(port)) for remark, address, port in rows]


def _tcp_ping(host: str, port: int, timeout: float) -> int:
    """TCP-connect к Reality-порту, вернуть время в мс.

    TLS-handshake без учётных данных Reality непоказателен: часть нод релейит
    такой hello на dest (кажется «живой»), часть рвёт его alert'ом — на живость
    сервера это не влияет. Достаточность: TCP connect проходит = xray слушает.
    """
    t0 = time.monotonic()
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.close()
    return max(1, int((time.monotonic() - t0) * 1000))


async def _ping_one(address: str, port: int, timeout: float = 3.0) -> int | None:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_tcp_ping, address, port, timeout), timeout + 1.0)
    except Exception:
        return None


async def ping_nodes(nodes: list[tuple[str, str, int]], cache_secs: int = 60) -> list[tuple[str, str, int | None]]:
    """[(remark, address, ms|None)] с кэшем — рукопожатие Reality :443 дорогое."""
    now = time.monotonic()
    if now - _ping_cache_ts[0] < cache_secs and _ping_cache_ts[1]:
        return _ping_cache_ts[1]
    results = await asyncio.gather(*[_ping_one(addr, port) for _, addr, port in nodes])
    out = [(remark, addr, ms) for (remark, addr, _), ms in zip(nodes, results)]
    _ping_cache_ts[0] = now
    _ping_cache_ts[1] = out
    return out


_ping_cache_ts: list = [0.0, []]
