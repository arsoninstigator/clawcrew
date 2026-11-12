"""the postgres store, driven by a fake driver.

no server is started: what is worth testing is the surface between our code and the
driver, which is where the dialect lives. the fake records every statement and answers
the catalogue query, so these tests fail if a `?` reaches psycopg, if the schema is
created twice, or if a transaction stops being a transaction.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from core.db import LATER_COLUMNS, SCHEMA  # noqa: E402
from core.db import open_store  # noqa: E402
from core.dialect import primary_keys  # noqa: E402
from core.store_postgres import (  # noqa: E402
    PostgresStore,
    StoreBackendMissing,
    _psycopg_connect,
)

DSN = "postgresql://user:pw@localhost:5432/crittertown"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDriver:
    """enough of a psycopg connection to answer what the store asks it."""

    def __init__(self, existing_columns: dict[str, list[str]] | None = None):
        self.log: list[tuple[str, tuple]] = []
        self.existing = existing_columns if existing_columns is not None else {}
        self.rows: list[dict] = []
        self.closed = False
        self.fail_on: str | None = None

    def execute(self, sql: str, params=()):
        self.log.append((sql, tuple(params)))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("the driver said no")
        lowered = sql.lower()
        if "information_schema.columns" in lowered:
            table = params[0] if params else ""
            return FakeCursor([{"column_name": c} for c in self.existing.get(table, [])])
        return FakeCursor(self.rows)

    def close(self):
        self.closed = True

    # assertions helpers
    def sql_text(self) -> str:
        return "\n".join(sql for sql, _ in self.log)

    def statements(self, prefix: str) -> list[str]:
        return [sql for sql, _ in self.log if sql.lower().startswith(prefix)]


def store_with(driver: FakeDriver) -> PostgresStore:
    return PostgresStore(DSN, connect=lambda url: driver)


class InitTests(unittest.TestCase):
    def test_the_schema_is_created_on_first_use(self):
        driver = FakeDriver()
        store_with(driver)
        creates = driver.statements("create table")
        self.assertEqual(len(creates), len(primary_keys(SCHEMA)))
        self.assertIn("create index", driver.sql_text())
        self.assertEqual(store_with(driver).kind, "postgres")

    def test_the_schema_is_postgres_dialect(self):
        driver = FakeDriver()
        store_with(driver)
        text = driver.sql_text().lower()
        for token in ("autoincrement", "pragma"):
            self.assertNotIn(token, text)

    def test_schema_version_is_written_as_an_upsert(self):
        driver = FakeDriver()
        store_with(driver)
        upserts = [sql for sql in driver.statements("insert into meta")]
        self.assertEqual(len(upserts), 1)
        self.assertIn("on conflict (key) do update set value = excluded.value", upserts[0])

    def test_the_catalogue_question_stays_in_this_schema(self):
        driver = FakeDriver(existing_columns={"tasks": ["channel_id"]})
        store_with(driver)
        questions = [sql for sql, _ in driver.log if "information_schema.columns" in sql]
        self.assertTrue(questions)
        self.assertIn("current_schema()", questions[0])

    def test_columns_are_widened_only_on_tables_that_exist(self):
        # `tasks` exists with channel_id already; `agents` exists without agent_key
        driver = FakeDriver(existing_columns={
            "tasks": ["channel_id"],
            "agents": ["id", "name"],
        })
        store_with(driver)
        alters = driver.statements("alter table")
        self.assertFalse([a for a in alters if a.startswith("alter table tasks")])
        agent_alters = [a for a in alters if a.startswith("alter table agents")]
        self.assertTrue(agent_alters)
        self.assertIn("add column if not exists agent_key", agent_alters[0])
        self.assertFalse([a for a in alters if a.startswith("alter table workflows")],
                         "a table the catalogue does not report must not be altered")

    def test_every_column_listed_for_widening_is_reachable(self):
        # a real table always has at least one column; the ones added later are absent
        driver = FakeDriver(existing_columns={t: ["id"] for t in LATER_COLUMNS})
        store_with(driver)
        alters = driver.statements("alter table")
        expected = sum(len(cols) for cols in LATER_COLUMNS.values())
        self.assertEqual(len(alters), expected)


class QueryTests(unittest.TestCase):
    def test_a_placeholder_never_reaches_the_driver(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.log.clear()
        store.query("select * from agents where id = ?", ("a1",))
        sql, params = driver.log[-1]
        self.assertNotIn("?", sql)
        self.assertIn("%s", sql)
        self.assertEqual(params, ("a1",))

    def test_an_upsert_written_by_a_service_is_translated(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.log.clear()
        with store.tx() as conn:
            conn.execute(
                "insert or replace into meta (key, value) values ('gate_secret', ?)", ("x",)
            )
        sql = " ".join(s for s, _ in driver.log)
        self.assertNotIn("insert or replace", sql.lower())
        self.assertIn("on conflict (key) do update set value = excluded.value", sql)

    def test_query_returns_dict_rows(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.rows = [{"id": "agt_1", "name": "wren"}]
        rows = store.query("select * from agents")
        self.assertEqual(rows[0]["name"], "wren")
        self.assertIn("id", rows[0])

    def test_one_returns_none_on_no_rows(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.rows = []
        self.assertIsNone(store.one("select * from agents where id = ?", ("nope",)))


class TransactionTests(unittest.TestCase):
    def test_a_transaction_commits(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.log.clear()
        with store.tx() as conn:
            conn.execute("insert into meta (key, value) values (?, ?)", ("a", "b"))
        words = [sql.lower() for sql, _ in driver.log]
        self.assertIn("begin", words)
        self.assertIn("commit", words)
        self.assertNotIn("rollback", words)

    def test_a_failed_transaction_rolls_back_and_raises(self):
        driver = FakeDriver()
        store = store_with(driver)
        driver.log.clear()
        with self.assertRaises(RuntimeError):
            with store.tx() as conn:
                conn.execute("insert into meta (key, value) values (?, ?)", ("a", "b"))
                driver.fail_on = "boom"
                conn.execute("select boom")
        words = [sql.lower() for sql, _ in driver.log]
        self.assertIn("rollback", words)
        self.assertNotIn("commit", words)

    def test_connections_are_closed_even_when_the_body_raises(self):
        driver = FakeDriver()
        store = store_with(driver)
        with self.assertRaises(RuntimeError):
            with store.tx():
                raise RuntimeError("body failed")
        self.assertTrue(driver.closed)


class SelectionTests(unittest.TestCase):
    def test_no_dsn_means_the_sqlite_file(self):
        store = open_store(Settings(database_path="/tmp/select.db"))
        self.assertEqual(type(store).__name__, "Store")

    def test_a_dsn_means_postgres(self):
        # the driver extra is not installed in the test environment, so this proves
        # selection reached psycopg and failed loudly instead of silently using a file
        with self.assertRaises(StoreBackendMissing) as caught:
            open_store(Settings(database_url=DSN))
        self.assertIn("crittertown[postgres]", str(caught.exception))

    def test_an_empty_dsn_is_not_a_backend(self):
        with self.assertRaises(StoreBackendMissing):
            PostgresStore("")

    def test_psycopg_is_only_imported_when_used(self):
        # importing the module must not require the driver, or the sqlite default
        # would stop working on a machine that never hosts rows
        self.assertTrue(callable(_psycopg_connect))


if __name__ == "__main__":
    unittest.main()
