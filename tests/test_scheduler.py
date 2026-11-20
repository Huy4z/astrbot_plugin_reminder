"""ReminderScheduler 单元测试（真实 SQLite + 假平台/发送器）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from reminder_store import ReminderStore
from scheduler import ReminderScheduler, SchedulerSettings
from targets import PlatformInfo

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

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
QQ = PlatformInfo(id="qq-main", type="qq_official")


class FakeLog:
    def __init__(self) -> None:
        self.records = []

    def _record(self, level):
        def handler(*args, **kwargs):
            self.records.append((level, args))

        return handler

    debug = property(lambda self: self._record("debug"))
    info = property(lambda self: self._record("info"))
    warning = property(lambda self: self._record("warning"))
    error = property(lambda self: self._record("error"))
    exception = property(lambda self: self._record("exception"))


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "paa.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.store = ReminderStore(str(self.db_path), timeout_seconds=1.0)
        self.log = FakeLog()
        self.sent = []
        self.sender_ok = True
        self.now_box = [NOW]
        self.settings = SchedulerSettings(
            poll_interval_seconds=1,
            startup_delay_seconds=0,
            max_overdue_hours=24,
            batch_limit=20,
            send_interval_ms=0,
        )
        self.scheduler = ReminderScheduler(
            store=self.store,
            settings=self.settings,
            sender=self._sender,
            platform_lister=lambda: [QQ],
            log=self.log,
            clock=lambda: self.now_box[0],
        )

    async def _sender(self, umo: str, text: str) -> bool:
        self.sent.append((umo, text))
        return self.sender_ok

    def _user(self, user_id="u1", provider="qq", external="openid-1"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO users (id, provider, external_user_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, provider, external, "2026-11-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    def _reminder(self, reminder_id="r1", remind_at=None, status="pending", user_id="u1"):
        when = remind_at or (self.now_box[0] - timedelta(minutes=1)).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders"
            " (id, user_id, item_id, content, remind_at, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reminder_id,
                user_id,
                None,
                "交周报",
                when,
                status,
                "2026-11-01T00:00:00Z",
                "2026-11-01T00:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

    async def test_due_reminder_sent_and_marked(self):
        self._user()
        self._reminder()
        result = await self.scheduler.scan_once()
        self.assertEqual(result.due, 1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "qq-main:FriendMessage:openid-1")
        self.assertIn("交周报", self.sent[0][1])
        self.assertEqual(self.store.get_reminder("r1").status, "triggered")

        again = await self.scheduler.scan_once()
        self.assertEqual(again.due, 0)
        self.assertEqual(len(self.sent), 1)
        snapshot = await self.scheduler.status_snapshot()
        self.assertEqual(snapshot["pending"], 0)
        self.assertIsNone(snapshot["next_reminder"])

    async def test_cancelled_reminder_never_sent(self):
        self._user()
        self._reminder(status="cancelled")
        result = await self.scheduler.scan_once()
        self.assertEqual(result.due, 0)
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(self.store.get_reminder("r1").status, "cancelled")

    async def test_overdue_reminder_skipped_and_left_pending(self):
        self._user()
        self._reminder(remind_at=(NOW - timedelta(days=2)).isoformat())
        result = await self.scheduler.scan_once()
        self.assertEqual(result.skipped_overdue, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(self.store.get_reminder("r1").status, "pending")

    async def test_old_overdue_does_not_starve_new_reminders(self):
        # 大量过期提醒不能占满 batch_limit，把真正到点的新提醒饿死。
        self.settings.batch_limit = 1
        self._user()
        for index in range(3):
            self._reminder(
                reminder_id=f"old-{index}",
                remind_at=(NOW - timedelta(days=2)).isoformat(),
            )
        self._reminder(reminder_id="new", remind_at=(NOW - timedelta(minutes=1)).isoformat())
        result = await self.scheduler.scan_once()
        self.assertEqual(result.due, 1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.skipped_overdue, 3)
        self.assertEqual(self.store.get_reminder("new").status, "triggered")
        for index in range(3):
            self.assertEqual(
                self.store.get_reminder(f"old-{index}").status, "pending"
            )

    async def test_overdue_limit_zero_disables_check(self):
        self.settings.max_overdue_hours = 0
        self._user()
        self._reminder(remind_at=(NOW - timedelta(days=30)).isoformat())
        result = await self.scheduler.scan_once()
        self.assertEqual(result.sent, 1)

    async def test_failure_retries_with_backoff_then_succeeds(self):
        self._user()
        self._reminder()
        self.sender_ok = False
        first = await self.scheduler.scan_once()
        self.assertEqual(first.failed, 1)
        self.assertEqual(self.store.get_reminder("r1").status, "pending")
        self.assertEqual(len(self.sent), 1)

        # 未到重试时间：不重复发送
        early = await self.scheduler.scan_once()
        self.assertEqual(early.skipped_retry, 1)
        self.assertEqual(len(self.sent), 1)

        self.now_box[0] = NOW + timedelta(seconds=31)
        self.sender_ok = True
        second = await self.scheduler.scan_once()
        self.assertEqual(second.sent, 1)
        self.assertEqual(self.store.get_reminder("r1").status, "triggered")
        self.assertEqual(len(self.sent), 2)

    async def test_gives_up_after_max_attempts_but_force_retries(self):
        self.settings.max_delivery_attempts = 2
        self._user()
        self._reminder()
        self.sender_ok = False
        await self.scheduler.scan_once()
        self.now_box[0] = NOW + timedelta(seconds=31)
        await self.scheduler.scan_once()
        self.now_box[0] = NOW + timedelta(seconds=1000)
        gave_up = await self.scheduler.scan_once()
        self.assertEqual(gave_up.skipped_retry, 1)
        self.assertEqual(len(self.sent), 2)

        self.sender_ok = True
        forced = await self.scheduler.scan_once(force=True)
        self.assertEqual(forced.sent, 1)
        self.assertEqual(len(self.sent), 3)

    async def test_missing_user_counts_as_failure(self):
        self._reminder()
        result = await self.scheduler.scan_once()
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.sent, 0)
        self.assertIn(
            "users",
            "".join(str(args[3]) for level, args in self.log.records if level == "warning"),
        )
        self.assertEqual(self.store.get_reminder("r1").status, "pending")

    async def test_malformed_remind_at_skipped_as_invalid(self):
        self._user()
        self._reminder(remind_at="2026-11-20 10:00:00")  # 能被 julianday 解析，但无时区
        result = await self.scheduler.scan_once()
        self.assertEqual(result.invalid, 1)
        self.assertEqual(result.sent, 0)
        self.assertEqual(self.store.get_reminder("r1").status, "pending")

    async def test_mark_failure_does_not_resend_on_next_scan(self):
        self._user()
        self._reminder()
        original = self.store.mark_triggered
        with mock.patch.object(
            self.store, "mark_triggered", side_effect=sqlite3.OperationalError("locked")
        ):
            first = await self.scheduler.scan_once()
        self.assertEqual(first.sent, 1)
        self.assertEqual(first.failed, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("r1", self.scheduler._sent_unmarked)

        # 下一轮先补写状态，不重复发送
        second = await self.scheduler.scan_once()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("r1", self.scheduler._sent_unmarked)
        self.assertEqual(original("r1", self.now_box[0]), 0)  # 已经是 triggered
        self.assertEqual(self.store.get_reminder("r1").status, "triggered")

    async def test_status_snapshot_without_task(self):
        self._user()
        self._reminder()
        snapshot = await self.scheduler.status_snapshot()
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["pending"], 1)
        self.assertIsNotNone(snapshot["next_reminder"])

    async def test_start_and_stop_background_task(self):
        self.settings.startup_delay_seconds = 1
        self.scheduler.start()
        self.assertTrue(self.scheduler.running)
        self.scheduler.notify_loaded()
        await self.scheduler.stop()
        self.assertFalse(self.scheduler.running)


if __name__ == "__main__":
    unittest.main()
