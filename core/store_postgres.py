"""the hosted store: the same four methods, over psycopg.

sqlite is the runtime. this exists so a deploy with more than one writer can put the
rows on insforge or supabase without the app knowing: `core.db.open_store` picks this
when `CRITTERTOWN_DB_URL` is set, and everything downstream keeps calling
`query`, `one`, `conn` and `tx`.

two things are deliberate:

* the connection is a wrapper, not a bare psycopg connection. every statement the
  codebase writes is sqlite dialect (`?` placeholders, `insert or replace`), and the
  wrapper translates on the way in. that means the three services that issue their own
  sql keep working unchanged, and there is one translation step instead of a rewrite
  of every query in the tree.
* the schema is created from the same `SCHEMA` string sqlite uses, through
  `core.dialect`, so a table added to one dialect is added to both.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from core.db import LATER_COLUMNS, LATER_INDEXES, SCHEMA, SCHEMA_VERSION
from core.dialect import placeholders, primary_keys, statements, to_postgres, translate
from core.errors import CritterTownError


class StoreBackendMissing(CritterTownError):
    """`CRITTERTOWN_DB_URL` points at postgres and the driver is not installed."""


class PostgresConnection:
    """a psycopg connection that speaks sqlite dialect at its edge."""

    def __init__(self, raw: Any, keys: dict[str, list[str]]):
        self.raw = raw
        self.keys = keys
        self.statements_run = 0

    def execute(self, sql: str, params: tuple = ()) -> Any:
        self.statements_run += 1
        return self.raw.execute(translate(sql, self.keys), params or ())

    def executescript(self, sql: str) -> None:
        """postgres has no multi-statement execute with parameters, and `execute`
        with a script is a driver feature, so run them one at a time."""
        for statement in statements(sql):
            self.execute(statement)

    def close(self) -> None:
        self.raw.close()


def _psycopg_connect(url: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - the extra is installed or not
        raise StoreBackendMissing(
            "CRITTERTOWN_DB_URL is set but psycopg is not installed; "
            "install the extra with: pip install 'crittertown[postgres]'"
        ) from exc
    # autocommit, so `conn()` is safe for reads and `tx()` owns the transaction
    # boundary explicitly rather than relying on driver defaults
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


class PostgresStore:
    """rows in postgres, the same interface `core.db.Store` offers."""

    kind = "postgres"

    def __init__(self, url: str, connect=None):
        if not url:
            raise StoreBackendMissing("postgres store needs a dsn")
        self.url = url
        self._connect = connect or _psycopg_connect
        self._keys = primary_keys(SCHEMA)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(to_postgres(SCHEMA))
            self._add_columns(c)
            c.execute(
                "insert into meta (key, value) values (?, ?) "
                "on conflict (key) do update set value = excluded.value",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def _add_columns(self, c: PostgresConnection) -> None:
        """v2 columns on v1 tables, the postgres way: ask the catalogue, then
        `add column if not exists`, which sqlite does not have."""
        for table, columns in LATER_COLUMNS.items():
            existing = {
                row["column_name"]
                for row in c.execute(
                    "select column_name from information_schema.columns "
                    "where table_name = ? and table_schema = current_schema()",
                    (table,),
                ).fetchall()
            }
            if not existing:
                continue  # no such table in this database; nothing to widen
            for name, decl in columns.items():
                if name in existing:
                    continue
                c.execute(f"alter table {table} add column if not exists {name} {decl}")
        for statement in LATER_INDEXES:
            c.execute(statement)

    @contextmanager
    def conn(self) -> Iterator[PostgresConnection]:
        raw = self._connect(self.url)
        connection = PostgresConnection(raw, self._keys)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def tx(self) -> Iterator[PostgresConnection]:
        """explicit write transaction, serialized per process, like the sqlite store."""
        with self._lock:
            raw = self._connect(self.url)
            connection = PostgresConnection(raw, self._keys)
            try:
                connection.execute("begin")
                yield connection
                connection.execute("commit")
            except Exception:
                try:
                    connection.execute("rollback")
                except Exception:  # the connection is already gone; the raise below matters
                    pass
                raise
            finally:
                connection.close()

    def query(self, sql: str, params: tuple = ()) -> list[Any]:
        with self.conn() as c:
            return list(c.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple = ()) -> Any | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def health(self) -> dict:
        row = self.one("select 1 as ok")
        return {"store": self.kind, "ok": bool(row and row["ok"] == 1)}


# placeholders is re-exported for callers that only need the `?` -> `%s` half
__all__ = [
    "PostgresConnection",
    "PostgresStore",
    "StoreBackendMissing",
    "placeholders",
    "translate",
]
