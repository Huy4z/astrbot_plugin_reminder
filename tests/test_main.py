"""main.py 命令层测试（AstrBot 用最小 stub，不联网）。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from _support import load_plugin

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


class FakeMeta:
    def __init__(self, platform_id="qq-main", platform_type="qq_official"):
        self.id = platform_id
        self.name = platform_type
        self.support_proactive_message = True


class FakePlatform:
    def __init__(self):
        self._meta = FakeMeta()

    def meta(self):
        return self._meta


class FakePlatformManager:
    def __init__(self):
        self.platform_insts = [FakePlatform()]


class FakeContext:
    def __init__(self):
        self.platform_manager = FakePlatformManager()
        self.sent = []
        self.ok = True

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return self.ok


class FakeEvent:
    def __init__(self, platform="qq_official", sender="openid-1"):
        self._platform = platform
        self._sender = sender
        self.unified_msg_origin = f"qq-main:FriendMessage:{sender}"

    def get_platform_name(self):
        return self._platform

    def get_sender_id(self):
        return self._sender

    def plain_result(self, text):
        return type("PlainResult", (), {"text": text})()


class MainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "paa.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self.module = load_plugin()
        self.context = FakeContext()
        self.event = FakeEvent()
        self.plugin = self.module.ReminderPlugin(
            self.context,
            {
                "db_path": str(self.db_path),
                "poll_interval_seconds": 5,
                "send_interval_ms": 0,
            },
        )

    def _user(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO users (id, provider, external_user_id, created_at)"
            " VALUES ('u1', 'qq', 'openid-1', '2026-11-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

    def _reminder(self, remind_at, status="pending", reminder_id="r1"):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO reminders"
            " (id, user_id, item_id, content, remind_at, status, created_at, updated_at)"
            " VALUES (?, 'u1', NULL, '交周报', ?, ?, '2026-11-01T00:00:00Z',"
            " '2026-11-01T00:00:00Z')",
            (reminder_id, remind_at, status),
        )
        conn.commit()
        conn.close()

    async def test_initialize_is_safe_and_starts_scheduler(self):
        with mock.patch.object(self.plugin.scheduler, "start") as start:
            await self.plugin.initialize()
        start.assert_called_once()
        self.assertTrue(
            any("数据库就绪" in str(args[0]) for _, args in self.module.logger.records)
        )

    async def test_terminate_stops_scheduler(self):
        with mock.patch.object(self.plugin.scheduler, "stop", new=mock.AsyncMock()) as stop:
            await self.plugin.terminate()
        stop.assert_awaited_once()

    async def test_help_command(self):
        results = [
            result async for result in self.plugin.reminder_cmd(self.event, "help")
        ]
        self.assertEqual(len(results), 1)
        self.assertIn("/reminder check", results[0].text)
        self.assertIn("qq-main(qq_official)", results[0].text)

    async def test_status_command(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._user()
        self._reminder(future)
        text = await self.plugin._status_text()
        self.assertIn("Reminder 状态", text)
        self.assertIn("待提醒：1 条", text)
        self.assertIn("下一条", text)

    async def test_check_command_sends_due_reminder(self):
        self._user()
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._reminder(past)
        text = await self.plugin._check_text()
        self.assertIn("已发送 1 条", text)
        self.assertEqual(len(self.context.sent), 1)
        umo, chain = self.context.sent[0]
        self.assertEqual(umo, "qq-main:FriendMessage:openid-1")
        self.assertIn("交周报", chain.chain[0].text)

    async def test_check_command_reports_send_failure(self):
        self._user()
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._reminder(past)
        self.context.ok = False
        text = await self.plugin._check_text()
        self.assertIn("失败 1 条", text)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.plugin.store.get_reminder("r1").status, "pending")

    async def test_list_command_for_known_user(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self._user()
        self._reminder(future)
        text = await self.plugin._list_text(self.event)
        self.assertIn("待提醒（1 条）", text)
        self.assertIn("交周报", text)

    async def test_list_command_for_unknown_user(self):
        text = await self.plugin._list_text(self.event)
        self.assertIn("还没有 paadm 用户记录", text)

    async def test_test_command_uses_current_session(self):
        text = await self.plugin._test_text(self.event)
        self.assertIn("测试消息已发送", text)
        self.assertEqual(self.context.sent[0][0], "qq-main:FriendMessage:openid-1")
        self.assertIn("Reminder 测试消息", self.context.sent[0][1].chain[0].text)

    async def test_test_command_reports_failure(self):
        self.context.ok = False
        text = await self.plugin._test_text(self.event)
        self.assertIn("发送失败", text)

    def test_provider_mapping(self):
        self.assertEqual(self.plugin._provider_for_event(self.event), "qq")
        self.assertEqual(
            self.plugin._provider_for_event(FakeEvent(platform="weird_platform")),
            "weird_platform",
        )

    def test_config_helpers(self):
        self.assertTrue(self.module._config_bool({"enabled": "false"}, "enabled", True) is False)
        self.assertEqual(self.module._config_int({"n": "7"}, "n", 1), 7)
        self.assertEqual(self.module._config_int({"n": 0}, "n", 5, minimum=5), 5)
        self.assertEqual(
            self.module._config_map({"m": '{"qq": "qq-main"}'}, "m"),
            {"qq": "qq-main"},
        )
        self.assertEqual(self.module._config_map({"m": "not-json"}, "m"), {})

    def test_build_settings_clamps(self):
        settings = self.plugin._build_settings(
            {
                "poll_interval_seconds": 1,
                "max_overdue_hours": -1,
                "max_message_chars": 10,
                "platform_map_json": {"qq": "qq-main"},
            }
        )
        self.assertEqual(settings.poll_interval_seconds, 5)
        self.assertEqual(settings.max_overdue_hours, 0)
        self.assertEqual(settings.max_message_chars, 100)
        self.assertEqual(settings.platform_map, {"qq": "qq-main"})


if __name__ == "__main__":
    unittest.main()
