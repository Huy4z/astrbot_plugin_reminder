"""reminder_store 单元测试（纯标准库，不需要 AstrBot）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from reminder_store import (
    ReminderStore,
    parse_iso8601,
    render_message,
    to_iso_z,
)

SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider, external_user_id)
);
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    item_id TEXT,
    content TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','triggered','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class ReminderStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "paa.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.store = ReminderStore(str(self.db_path), timeout_seconds=1.0)
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def _user(self, user_id="u1", provider="qq", external="openid-1"):
        conn = self._connect()
        conn.execute(
            "INSERT INTO users (id, provider, external_user_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, provider, external, "2026-11-01T00:00:00Z"),
        )
        conn.commit()

    def _reminder(
        self,
        reminder_id="r1",
        user_id="u1",
        content="交周报",
        remind_at="2026-11-20T19:00:00+08:00",
        status="pending",
        item_id=None,
    ):
        conn = self._connect()
        conn.execute(
            "INSERT INTO reminders"
            " (id, user_id, item_id, content, remind_at, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reminder_id,
                user_id,
                item_id,
                content,
                remind_at,
                status,
                "2026-11-01T00:00:00Z",
                "2026-11-01T00:00:00Z",
            ),
        )
        conn.commit()

    def test_parse_iso8601_handles_offsets_and_z(self):
        value = parse_iso8601("2026-11-19T07:00:00+08:00")
        self.assertEqual(value.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(value.hour, 7)
        self.assertEqual(parse_iso8601("2026-11-19T07:00:00Z").tzinfo, timezone.utc)
        self.assertEqual(to_iso_z(value), "2026-11-18T23:00:00.000Z")

    def test_parse_iso8601_rejects_missing_timezone(self):
        with self.assertRaises(ValueError):
            parse_iso8601("2026-11-19T07:00:00")

    def test_parse_iso8601_truncates_nanoseconds(self):
        value = parse_iso8601("2026-11-19T07:00:00.123456789+08:00")
        self.assertEqual(value.microsecond, 123456)

    def test_exists_does_not_create_database(self):
        missing = Path(self.tmp.name) / "missing.db"
        store = ReminderStore(str(missing))
        self.assertFalse(store.exists())
        self.assertFalse(missing.exists())

    def test_schema_missing(self):
        self.assertEqual(self.store.schema_missing(), [])
        empty = Path(self.tmp.name) / "empty.db"
        sqlite3.connect(empty).close()
        self.assertEqual(
            ReminderStore(str(empty)).schema_missing(),
            ["reminders 表不存在"],
        )
        partial = Path(self.tmp.name) / "partial.db"
        conn = sqlite3.connect(partial)
        conn.execute("CREATE TABLE reminders (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        missing = ReminderStore(str(partial)).schema_missing()
        self.assertIn("缺少列 status", missing)
        self.assertIn("缺少列 remind_at", missing)

    def test_list_due_only_pending_and_past(self):
        self._user()
        self._reminder("past", remind_at="2026-11-20T19:00:00+08:00")  # 11:00 UTC
        self._reminder("future", remind_at="2026-11-20T21:00:00+08:00")  # 13:00 UTC
        self._reminder("triggered", remind_at="2026-11-20T18:00:00+08:00", status="triggered")
        self._reminder("cancelled", remind_at="2026-11-20T18:00:00+08:00", status="cancelled")

        due = self.store.list_due(self.now)
        self.assertEqual([item.id for item in due], ["past"])
        self.assertEqual(due[0].remind_at_dt.utcoffset().total_seconds(), 8 * 3600)

    def test_list_due_is_inclusive_at_now(self):
        self._user()
        self._reminder("exact", remind_at="2026-11-20T12:00:00Z")
        self.assertEqual([item.id for item in self.store.list_due(self.now)], ["exact"])

    def test_mark_triggered_only_pending_and_idempotent(self):
        self._user()
        self._reminder("r1")
        self._reminder("r2", status="cancelled")
        self.assertEqual(self.store.mark_triggered("r1", self.now), 1)
        self.assertEqual(self.store.get_reminder("r1").status, "triggered")
        self.assertEqual(self.store.mark_triggered("r1", self.now), 0)
        self.assertEqual(self.store.mark_triggered("r2", self.now), 0)
        self.assertEqual(self.store.get_reminder("r2").status, "cancelled")

    def test_counts_next_and_user_lookup(self):
        self._user()
        self._reminder("late", remind_at="2026-11-20T20:00:00+08:00")
        self._reminder("early", remind_at="2026-11-20T18:00:00+08:00")
        self._reminder("done", remind_at="2026-11-20T17:00:00+08:00", status="triggered")
        self.assertEqual(self.store.count_pending(), 2)
        self.assertEqual(self.store.next_pending().id, "early")
        self.assertEqual([item.id for item in self.store.list_pending_for_user("u1")], ["early", "late"])
        user = self.store.find_user("qq", "openid-1")
        self.assertIsNotNone(user)
        self.assertEqual(user.id, "u1")
        self.assertIsNone(self.store.find_user("qq", "nope"))

    def test_render_message_placeholders_and_truncation(self):
        reminder = type(
            "R",
            (),
            {
                "content": "交周报" * 20,
                "remind_at": "2026-11-20T19:00:00+08:00",
                "remind_at_dt": parse_iso8601("2026-11-20T19:00:00+08:00"),
                "item_id": None,
                "user_id": "u1",
                "id": "r1",
            },
        )()
        text = render_message(
            "提醒 {content} @ {remind_at_local} ({user_id}/{reminder_id})",
            reminder,
            tz_name="Asia/Shanghai",
            max_chars=200,
        )
        self.assertIn("提醒 交周报", text)
        self.assertIn("2026-11-20 19:00", text)
        self.assertIn("(u1/r1)", text)

        short = render_message(
            "提醒 {content} @ {remind_at_local}",
            reminder,
            tz_name="Asia/Shanghai",
            max_chars=30,
        )
        self.assertLessEqual(len(short), 30)
        self.assertTrue(short.endswith("…"))


if __name__ == "__main__":
    unittest.main()
