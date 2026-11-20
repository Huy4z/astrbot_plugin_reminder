"""提醒数据的 SQLite 访问与消息渲染。

本模块不依赖 AstrBot，方便离线单测。表结构由 ``paadm`` 的迁移创建：

```sql
CREATE TABLE reminders (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    item_id    TEXT,
    content    TEXT NOT NULL,
    remind_at  TEXT NOT NULL,          -- ISO 8601，必须带时区偏移
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'triggered', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

插件只做两件事：读取 ``status='pending'`` 且已到点的提醒；发送成功后把
``status`` 从 ``pending`` 改为 ``triggered``。取消、删除、创建仍由 ``paadm``
负责，插件不碰这些语义。
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterator, List, Optional
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TEMPLATE = "⏰ 提醒：{content}"
DEFAULT_MAX_MESSAGE_CHARS = 800

STATUS_PENDING = "pending"
STATUS_TRIGGERED = "triggered"
STATUS_CANCELLED = "cancelled"

REQUIRED_COLUMNS = {"id", "user_id", "content", "remind_at", "status"}

_FRACTION_PATTERN = re.compile(r"^(.*\.\d{6})\d+(.*)$")


class ReminderStoreError(RuntimeError):
    """数据库不存在、表结构不完整等可读错误。"""


@dataclass(frozen=True)
class Reminder:
    """一行 reminders 记录。``remind_at_dt`` 解析失败时为 None。"""

    id: str
    user_id: str
    item_id: Optional[str]
    content: str
    remind_at: str
    status: str
    created_at: str = ""
    updated_at: str = ""
    remind_at_dt: Optional[datetime] = None


@dataclass(frozen=True)
class UserTarget:
    """一行 users 记录：paadm 用户 → 平台账号。"""

    id: str
    provider: str
    external_user_id: str


def parse_iso8601(value: str) -> datetime:
    """解析带时区偏移的 ISO 8601 时间，返回 aware datetime。

    - 接受 ``Z`` / ``z`` / ``+08:00``；
    - 兼容小数秒超过 6 位（Rust RFC3339 可能输出纳秒）；
    - 拒绝没有时区偏移的字符串：paadm 写入时也要求偏移，没有偏移无法准确
      判断是否到点。
    """

    text = str(value or "").strip()
    if not text:
        raise ValueError("时间为空")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Python 的 fromisoformat 在部分版本不接受 7~9 位小数秒。
        shortened = _FRACTION_PATTERN.sub(r"\1\2", text)
        try:
            parsed = datetime.fromisoformat(shortened)
        except ValueError as exc:
            raise ValueError(f"无法解析 ISO 8601 时间: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间缺少时区偏移: {value!r}")
    return parsed


@lru_cache(maxsize=8)
def _timezone(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - 容器缺少 tzdata 时退化为东八区
        return timezone(timedelta(hours=8))


def format_local(value: datetime | str, tz_name: str = DEFAULT_TIMEZONE) -> str:
    """把 aware 时间格式化为配置时区的本地时间字符串。"""

    if isinstance(value, str):
        value = parse_iso8601(value)
    return value.astimezone(_timezone(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def to_iso_z(value: datetime) -> str:
    """UTC aware datetime → ``2026-11-20T15:40:00.000Z``（与 paadm 风格一致）。"""

    if value.tzinfo is None:
        raise ValueError("to_iso_z 需要 aware datetime")
    text = value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z")


def truncate(text: str, max_chars: int) -> str:
    """按字符数截断，超长时补省略号。``max_chars<=0`` 表示不截断。"""

    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1] + "…"


def render_message(
    template: str,
    reminder: Reminder,
    *,
    tz_name: str = DEFAULT_TIMEZONE,
    max_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
) -> str:
    """渲染提醒文案。

    支持占位符（用简单字符串替换，避免 ``str.format`` 遇到用户内容里的
    花括号时报错）：``{content}``、``{remind_at}``、``{remind_at_local}``、
    ``{item_id}``、``{user_id}``、``{reminder_id}``。
    """

    body = template.strip() if str(template or "").strip() else DEFAULT_TEMPLATE
    local_time = ""
    if reminder.remind_at_dt is not None:
        local_time = format_local(reminder.remind_at_dt, tz_name)
    values = {
        "content": reminder.content or "",
        "remind_at": reminder.remind_at or "",
        "remind_at_local": local_time,
        "item_id": reminder.item_id or "",
        "user_id": reminder.user_id or "",
        "reminder_id": reminder.id or "",
    }
    text = body
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return truncate(text.strip(), max_chars)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    raw_time = str(row["remind_at"] or "")
    try:
        parsed = parse_iso8601(raw_time)
    except ValueError:
        parsed = None
    return Reminder(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        item_id=(str(row["item_id"]) if row["item_id"] is not None else None),
        content=str(row["content"] or ""),
        remind_at=raw_time,
        status=str(row["status"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        remind_at_dt=parsed,
    )


class ReminderStore:
    """reminders / users 表的只读查询 + 受控状态写回。"""

    def __init__(self, db_path: str, *, timeout_seconds: float = 5.0) -> None:
        self.db_path = str(db_path or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    # ------------------------------------------------------------------
    # 连接与结构检查
    # ------------------------------------------------------------------
    def exists(self) -> bool:
        return bool(self.db_path) and os.path.isfile(self.db_path)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.exists():
            raise ReminderStoreError(f"数据库不存在: {self.db_path}")
        connection = sqlite3.connect(self.db_path, timeout=self.timeout_seconds)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不遮盖原始异常
                    pass
            raise
        finally:
            connection.close()

    def schema_missing(self) -> List[str]:
        """返回缺失的表/列描述；空列表表示结构可用。"""

        with self._connect() as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reminders'"
            ).fetchone()
            if table is None:
                return ["reminders 表不存在"]
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(reminders)")
            }
        return [f"缺少列 {name}" for name in sorted(REQUIRED_COLUMNS - columns)]

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def list_due(
        self,
        now: datetime,
        limit: int = 20,
        *,
        not_before: Optional[datetime] = None,
    ) -> List[Reminder]:
        """按 remind_at 升序返回已到点且仍为 pending 的提醒。

        ``not_before`` 用于过滤“太旧、不再补发”的提醒，避免它们长期占据
        ``LIMIT`` 的名额，导致新提醒永远轮不到。
        """

        if limit <= 0:
            return []
        sql = """
            SELECT id, user_id, item_id, content, remind_at, status,
                   created_at, updated_at
            FROM reminders
            WHERE status = 'pending'
              AND julianday(remind_at) IS NOT NULL
              AND julianday(remind_at) <= julianday(?)
        """
        params: List[object] = [to_iso_z(now)]
        if not_before is not None:
            sql += " AND julianday(remind_at) >= julianday(?)"
            params.append(to_iso_z(not_before))
        sql += " ORDER BY julianday(remind_at) ASC, id ASC LIMIT ?"
        params.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_reminder(row) for row in rows]

    def count_overdue(self, now: datetime, before: datetime) -> int:
        """统计已到点但早于 ``before`` 的 pending 提醒数量（仅用于展示）。"""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM reminders
                WHERE status = 'pending'
                  AND julianday(remind_at) IS NOT NULL
                  AND julianday(remind_at) <= julianday(?)
                  AND julianday(remind_at) < julianday(?)
                """,
                (to_iso_z(now), to_iso_z(before)),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def get_reminder(self, reminder_id: str) -> Optional[Reminder]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, item_id, content, remind_at, status,
                       created_at, updated_at
                FROM reminders
                WHERE id = ?
                """,
                (str(reminder_id),),
            ).fetchone()
        return _row_to_reminder(row) if row is not None else None

    def count_pending(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM reminders WHERE status = 'pending'"
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def next_pending(self) -> Optional[Reminder]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, item_id, content, remind_at, status,
                       created_at, updated_at
                FROM reminders
                WHERE status = 'pending'
                  AND julianday(remind_at) IS NOT NULL
                ORDER BY julianday(remind_at) ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
        return _row_to_reminder(row) if row is not None else None

    def list_pending_for_user(self, user_id: str, limit: int = 10) -> List[Reminder]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, item_id, content, remind_at, status,
                       created_at, updated_at
                FROM reminders
                WHERE user_id = ? AND status = 'pending'
                ORDER BY julianday(remind_at) ASC, id ASC
                LIMIT ?
                """,
                (str(user_id), int(limit)),
            ).fetchall()
        return [_row_to_reminder(row) for row in rows]

    def get_user(self, user_id: str) -> Optional[UserTarget]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, provider, external_user_id FROM users WHERE id = ?",
                (str(user_id),),
            ).fetchone()
        return _row_to_user(row) if row is not None else None

    def find_user(self, provider: str, external_user_id: str) -> Optional[UserTarget]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, provider, external_user_id
                FROM users
                WHERE provider = ? AND external_user_id = ?
                """,
                (str(provider), str(external_user_id)),
            ).fetchone()
        return _row_to_user(row) if row is not None else None

    # ------------------------------------------------------------------
    # 写回：只有发送成功才允许 pending → triggered
    # ------------------------------------------------------------------
    def mark_triggered(self, reminder_id: str, now: datetime) -> int:
        """原子地把一条 pending 提醒标记为 triggered，返回受影响行数。

        ``WHERE status='pending'`` 保证 cancelled/triggered 的记录不会被改写；
        返回 0 说明提醒在发送前后已被用户取消或已被其它进程触发。
        """

        with self._connect(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET status = 'triggered', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (to_iso_z(now), str(reminder_id)),
            )
            return int(cursor.rowcount or 0)


def _row_to_user(row: sqlite3.Row) -> UserTarget:
    return UserTarget(
        id=str(row["id"]),
        provider=str(row["provider"] or ""),
        external_user_id=str(row["external_user_id"] or ""),
    )


__all__ = [
    "DEFAULT_MAX_MESSAGE_CHARS",
    "DEFAULT_TEMPLATE",
    "DEFAULT_TIMEZONE",
    "Reminder",
    "ReminderStore",
    "ReminderStoreError",
    "STATUS_CANCELLED",
    "STATUS_PENDING",
    "STATUS_TRIGGERED",
    "UserTarget",
    "format_local",
    "parse_iso8601",
    "render_message",
    "to_iso_z",
    "truncate",
]
