"""coordination: one owner per key, and a stream that survives a restart.

two primitives, because they are the two things a crew actually needs from a durable
object:

* **a lease.** "this card, this shift slot, this thread, one agent at a time." serialized
  execution, with an expiry so a crashed holder cannot hold the board hostage.
* **an append only stream.** what the room said, in order, readable from a cursor. state
  that survives the process that wrote it.

sqlite implements both by default, because a crew that runs on one box should not need a
cluster. the same protocol is what a remote actor runtime would satisfy, and
`durable_actors.py` describes exactly which actor holds which of these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clawcrew.db import Store, dumps, loads, now

SCHEMA = """
create table if not exists coordination_leases (
    group_name text not null,
    key        text not null,
    holder     text not null,
    expires_at real not null,
    created_date real not null,
    primary key (group_name, key)
);
create table if not exists coordination_streams (
    stream     text not null,
    seq        integer not null,
    payload    text not null,
    created_date real not null,
    primary key (stream, seq)
);
"""


class CoordinationBackend(Protocol):
    """what any backend has to provide. sqlite is the reference implementation."""

    name: str

    def claim(self, group: str, key: str, holder: str, ttl_seconds: int = 300) -> dict: ...

    def release(self, group: str, key: str, holder: str) -> dict: ...

    def append(self, stream: str, event: dict) -> dict: ...

    def read(self, stream: str, since: int = 0, limit: int = 100) -> list[dict]: ...


@dataclass
class SqliteCoordination:
    """a lease table and a stream table in the same sqlite file as everything else."""

    store: Store
    name: str = "sqlite"

    def __post_init__(self) -> None:
        with self.store.conn() as conn:
            conn.executescript(SCHEMA)

    # ---- leases -------------------------------------------------------
    def claim(self, group: str, key: str, holder: str, ttl_seconds: int = 300) -> dict:
        stamp = now()
        expires = stamp + max(1, int(ttl_seconds))
        with self.store.tx() as conn:
            row = conn.execute(
                "select holder, expires_at from coordination_leases "
                "where group_name = ? and key = ?", (group, key)).fetchone()
            if row is not None and row["holder"] != holder and row["expires_at"] > stamp:
                return {"granted": False, "holder": row["holder"],
                        "expires_at": row["expires_at"], "reason": "held by someone else"}
            conn.execute(
                "insert into coordination_leases "
                "(group_name, key, holder, expires_at, created_date) values (?, ?, ?, ?, ?) "
                "on conflict(group_name, key) do update set holder = excluded.holder, "
                "expires_at = excluded.expires_at, created_date = excluded.created_date",
                (group, key, holder, expires, stamp))
        return {"granted": True, "holder": holder, "expires_at": expires}

    def release(self, group: str, key: str, holder: str) -> dict:
        with self.store.tx() as conn:
            row = conn.execute(
                "select holder from coordination_leases where group_name = ? and key = ?",
                (group, key)).fetchone()
            if row is None:
                return {"released": False, "reason": "no lease"}
            if row["holder"] != holder:
                return {"released": False, "holder": row["holder"],
                        "reason": "someone else holds it"}
            conn.execute("delete from coordination_leases where group_name = ? and key = ?",
                         (group, key))
        return {"released": True, "holder": holder}

    def leases(self, group: str) -> list[dict]:
        stamp = now()
        rows = self.store.query(
            "select key, holder, expires_at from coordination_leases "
            "where group_name = ? order by key", (group,))
        return [{"key": r["key"], "holder": r["holder"], "expires_at": r["expires_at"],
                 "live": r["expires_at"] > stamp} for r in rows]

    # ---- streams ------------------------------------------------------
    def append(self, stream: str, event: dict) -> dict:
        stamp = now()
        with self.store.tx() as conn:
            next_seq = (conn.execute(
                "select coalesce(max(seq), 0) + 1 as n from coordination_streams "
                "where stream = ?", (stream,)).fetchone()["n"])
            conn.execute(
                "insert into coordination_streams (stream, seq, payload, created_date) "
                "values (?, ?, ?, ?)", (stream, next_seq, dumps(event), stamp))
        return {"stream": stream, "seq": next_seq, "created_date": stamp}

    def read(self, stream: str, since: int = 0, limit: int = 100) -> list[dict]:
        rows = self.store.query(
            "select seq, payload, created_date from coordination_streams "
            "where stream = ? and seq > ? order by seq limit ?",
            (stream, int(since), max(1, min(int(limit), 500))))
        return [{"seq": r["seq"], "event": loads(r["payload"], {}),
                 "created_date": r["created_date"]} for r in rows]

    def head(self, stream: str) -> int:
        row = self.store.one(
            "select coalesce(max(seq), 0) as n from coordination_streams where stream = ?",
            (stream,))
        return int(row["n"]) if row else 0
