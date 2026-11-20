"""提醒轮询与发送调度。

调度器是一个普通的 asyncio 后台任务，不依赖 AstrBot 的具体类型：

- ``store``：``ReminderStore``；
- ``settings``：``SchedulerSettings``；
- ``sender``：``async (umo, text) -> bool``，由 ``main.py`` 包装
  ``Context.send_message``；
- ``platform_lister``：返回当前已加载平台的快照，用于把 provider 映射成
  平台 ID。

发送语义是 **at-least-once**：先发送、成功后再把提醒标记为 triggered。若
发送成功但写回失败，进程内会记住这条提醒并继续尝试写回，避免重复发送；
插件重启后极端情况下可能重发一次（提醒本身不会丢）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

try:  # 包内导入（AstrBot 正常加载路径）
    from .reminder_store import (
        DEFAULT_MAX_MESSAGE_CHARS,
        DEFAULT_TEMPLATE,
        DEFAULT_TIMEZONE,
        Reminder,
        ReminderStore,
        format_local,
        render_message,
    )
    from .targets import (
        PlatformInfo,
        TargetError,
        describe_platforms,
        resolve_umo,
    )
except ImportError:  # pragma: no cover - 单测/顶层加载兜底
    from reminder_store import (  # type: ignore[no-redef]
        DEFAULT_MAX_MESSAGE_CHARS,
        DEFAULT_TEMPLATE,
        DEFAULT_TIMEZONE,
        Reminder,
        ReminderStore,
        format_local,
        render_message,
    )
    from targets import (  # type: ignore[no-redef]
        PlatformInfo,
        TargetError,
        describe_platforms,
        resolve_umo,
    )


Sender = Callable[[str, str], Awaitable[bool]]
PlatformLister = Callable[[], Sequence[PlatformInfo]]
Clock = Callable[[], datetime]
LogLike = Any


@dataclass
class SchedulerSettings:
    poll_interval_seconds: int = 20
    startup_delay_seconds: int = 10
    max_overdue_hours: int = 24
    batch_limit: int = 20
    message_template: str = DEFAULT_TEMPLATE
    timezone: str = DEFAULT_TIMEZONE
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    retry_base_seconds: int = 30
    retry_max_seconds: int = 600
    max_delivery_attempts: int = 20
    send_interval_ms: int = 300
    platform_map: Dict[str, str] = field(default_factory=dict)
    session_type_map: Dict[str, str] = field(default_factory=dict)
    session_overrides: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class ScanResult:
    started_at: datetime
    due: int = 0
    sent: int = 0
    failed: int = 0
    skipped_overdue: int = 0
    skipped_retry: int = 0
    invalid: int = 0
    sent_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        parts = [
            f"到点 {self.due} 条",
            f"已发送 {self.sent} 条",
        ]
        if self.failed:
            parts.append(f"失败 {self.failed} 条")
        if self.skipped_overdue:
            parts.append(f"跳过过期 {self.skipped_overdue} 条")
        if self.skipped_retry:
            parts.append(f"等待重试 {self.skipped_retry} 条")
        if self.invalid:
            parts.append(f"时间格式异常 {self.invalid} 条")
        if self.error:
            parts.append(f"错误：{self.error}")
        return "，".join(parts)


@dataclass
class _RetryState:
    attempts: int = 0
    next_at: Optional[datetime] = None
    last_error: str = ""
    gave_up: bool = False


class ReminderScheduler:
    """轮询 SQLite 并主动发送提醒。"""

    def __init__(
        self,
        *,
        store: ReminderStore,
        settings: SchedulerSettings,
        sender: Sender,
        platform_lister: PlatformLister,
        log: LogLike,
        clock: Optional[Clock] = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._sender = sender
        self._platform_lister = platform_lister
        self.log = log
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop_event = asyncio.Event()
        self._loaded_event = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._retry: Dict[str, _RetryState] = {}
        self._overdue_reported: set = set()
        self._sent_unmarked: set = set()
        self._last_scan_at: Optional[datetime] = None
        self._last_result: Optional[ScanResult] = None
        self._last_error = ""
        self._last_overdue_count = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="astrbot_plugin_reminder"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 停止时不应抛给 AstrBot
            self.log.exception("[reminder] 后台任务停止时异常")

    def notify_loaded(self) -> None:
        """AstrBot 完成加载后调用，让调度器尽快开始第一次扫描。"""

        self._loaded_event.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_scan_at(self) -> Optional[datetime]:
        return self._last_scan_at

    @property
    def last_result(self) -> Optional[ScanResult]:
        return self._last_result

    async def _run(self) -> None:
        try:
            try:
                await asyncio.wait_for(
                    self._loaded_event.wait(),
                    timeout=max(1, int(self.settings.startup_delay_seconds)),
                )
            except asyncio.TimeoutError:
                self.log.info(
                    "[reminder] 等待 on_astrbot_loaded 超时，按启动延迟继续"
                )
            # 平台连接通常早于 on_astrbot_loaded，这里再留一点缓冲。
            await self._sleep_or_stop(min(3, max(0, self.settings.startup_delay_seconds)))
            while not self._stop_event.is_set():
                if self.settings.enabled:
                    try:
                        await self.scan_once()
                    except Exception:  # noqa: BLE001 - 单轮失败不能杀死后台任务
                        self.log.exception("[reminder] 轮询异常（下一轮继续）")
                await self._sleep_or_stop(max(1, self.settings.poll_interval_seconds))
        except asyncio.CancelledError:
            raise

    async def _sleep_or_stop(self, seconds: int) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # 单轮扫描
    # ------------------------------------------------------------------
    async def scan_once(self, *, force: bool = False) -> ScanResult:
        async with self._scan_lock:
            now = self._clock()
            self._last_scan_at = now
            if force:
                self._retry.clear()
            result = ScanResult(started_at=now)
            await self._retry_unmarked(now)
            overdue_cutoff = self._overdue_cutoff(now)
            try:
                due = await asyncio.to_thread(
                    self.store.list_due,
                    now,
                    self.settings.batch_limit,
                    not_before=overdue_cutoff,
                )
            except Exception as exc:  # noqa: BLE001 - 数据库问题转成可读状态
                result.error = str(exc)
                self._last_error = str(exc)
                self._last_result = result
                self.log.warning("[reminder] 读取提醒失败：%s", exc)
                return result

            if overdue_cutoff is not None:
                try:
                    result.skipped_overdue = await asyncio.to_thread(
                        self.store.count_overdue, now, overdue_cutoff
                    )
                except Exception as exc:  # noqa: BLE001 - 仅统计失败，不影响发送
                    self.log.warning("[reminder] 统计过期提醒失败：%s", exc)
                if result.skipped_overdue != self._last_overdue_count:
                    if result.skipped_overdue:
                        self.log.warning(
                            "[reminder] 有 %s 条 pending 提醒已超过 %s 小时，"
                            "保持 pending 不补发（可调大 max_overdue_hours 或手动取消）",
                            result.skipped_overdue,
                            self.settings.max_overdue_hours,
                        )
                    self._last_overdue_count = result.skipped_overdue

            self._last_error = ""
            result.due = len(due)
            platforms: List[PlatformInfo] = []
            try:
                platforms = list(self._platform_lister())
            except Exception as exc:  # noqa: BLE001 - 平台列表异常不应中断扫描
                self.log.warning("[reminder] 读取平台列表失败：%s", exc)

            for reminder in due:
                await self._process_one(
                    reminder, now=now, platforms=platforms, result=result
                )
                if self.settings.send_interval_ms > 0:
                    await asyncio.sleep(self.settings.send_interval_ms / 1000.0)

            if len(self._overdue_reported) > 5000:
                self._overdue_reported.clear()
            self._last_result = result
            return result

    async def _retry_unmarked(self, now: datetime) -> None:
        """发送成功但写回失败时，下一轮只补写状态，不重复发送。"""

        if not self._sent_unmarked:
            return
        for reminder_id in list(self._sent_unmarked):
            try:
                changed = await asyncio.to_thread(
                    self.store.mark_triggered, reminder_id, now
                )
            except Exception as exc:  # noqa: BLE001 - 下轮继续补写
                self.log.warning(
                    "[reminder] 补写 triggered 状态失败 id=%s：%s", reminder_id, exc
                )
                continue
            self._sent_unmarked.discard(reminder_id)
            if changed:
                self.log.info(
                    "[reminder] 已补写 triggered 状态 id=%s", reminder_id
                )

    async def _process_one(
        self,
        reminder: Reminder,
        *,
        now: datetime,
        platforms: Sequence[PlatformInfo],
        result: ScanResult,
    ) -> None:
        if reminder.id in self._sent_unmarked:
            result.skipped_retry += 1
            return

        if self._should_skip_retry(reminder.id, now):
            result.skipped_retry += 1
            return

        if reminder.remind_at_dt is None:
            result.invalid += 1
            if reminder.id not in self._overdue_reported:
                self._overdue_reported.add(reminder.id)
                self.log.warning(
                    "[reminder] 跳过时间格式异常的提醒 id=%s remind_at=%r",
                    reminder.id,
                    reminder.remind_at,
                )
            return

        if self._is_overdue(reminder, now):
            result.skipped_overdue += 1
            if reminder.id not in self._overdue_reported:
                self._overdue_reported.add(reminder.id)
                self.log.warning(
                    "[reminder] 跳过过期提醒 id=%s remind_at=%s（超过 %s 小时，"
                    "保持 pending；可调大 max_overdue_hours 或手动取消）",
                    reminder.id,
                    reminder.remind_at,
                    self.settings.max_overdue_hours,
                )
            return

        try:
            user = await asyncio.to_thread(self.store.get_user, reminder.user_id)
            if user is None:
                raise TargetError(
                    f"users 表找不到 user_id={reminder.user_id}，无法确定发送目标"
                )
            umo = resolve_umo(
                user_id=user.id,
                provider=user.provider,
                external_user_id=user.external_user_id,
                platforms=platforms,
                platform_map=self.settings.platform_map,
                session_type_map=self.settings.session_type_map,
                overrides=self.settings.session_overrides,
            )
            text = render_message(
                self.settings.message_template,
                reminder,
                tz_name=self.settings.timezone,
                max_chars=self.settings.max_message_chars,
            )
            delivered = await self._sender(umo, text)
            if not delivered:
                raise TargetError(
                    "主动发送失败：Context.send_message 返回 False"
                    "（平台未加载或适配器拒绝发送）"
                )
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它提醒
            result.failed += 1
            self._record_failure(reminder, exc, now, result)
            return

        result.sent += 1
        result.sent_ids.append(reminder.id)
        self._retry.pop(reminder.id, None)
        self.log.info(
            "[reminder] 已发送提醒 id=%s user=%s -> %s：%s",
            reminder.id,
            reminder.user_id,
            umo,
            text[:120],
        )
        try:
            changed = await asyncio.to_thread(self.store.mark_triggered, reminder.id, now)
        except Exception as exc:  # noqa: BLE001 - 记下来下一轮补写
            self._sent_unmarked.add(reminder.id)
            result.errors.append(f"{reminder.id}: 写回状态失败: {exc}")
            self.log.exception(
                "[reminder] 消息已发送，但写回 triggered 状态失败 id=%s", reminder.id
            )
            return
        if changed != 1:
            result.errors.append(
                f"{reminder.id}: 发送后状态已被改变（可能刚被取消），未覆盖"
            )
            self.log.warning(
                "[reminder] 提醒在发送后状态已变化，未写回 id=%s", reminder.id
            )

    def _overdue_cutoff(self, now: datetime) -> Optional[datetime]:
        """早于该时间点的提醒不再补发，直接留在 pending。"""

        limit = int(self.settings.max_overdue_hours)
        if limit <= 0:
            return None
        return now - timedelta(hours=limit)

    def _is_overdue(self, reminder: Reminder, now: datetime) -> bool:
        limit = int(self.settings.max_overdue_hours)
        if limit <= 0 or reminder.remind_at_dt is None:
            return False
        return now - reminder.remind_at_dt > timedelta(hours=limit)

    def _should_skip_retry(self, reminder_id: str, now: datetime) -> bool:
        state = self._retry.get(reminder_id)
        if state is None:
            return False
        if state.gave_up:
            return True
        return state.next_at is not None and state.next_at > now

    def _record_failure(
        self,
        reminder: Reminder,
        exc: Exception,
        now: datetime,
        result: ScanResult,
    ) -> None:
        state = self._retry.setdefault(reminder.id, _RetryState())
        state.attempts += 1
        state.last_error = str(exc)
        base = max(1, int(self.settings.retry_base_seconds))
        cap = max(base, int(self.settings.retry_max_seconds))
        delay = min(base * (2 ** min(state.attempts - 1, 10)), cap)
        state.next_at = now + timedelta(seconds=delay)
        max_attempts = int(self.settings.max_delivery_attempts)
        if max_attempts > 0 and state.attempts >= max_attempts:
            state.gave_up = True
        if len(result.errors) < 10:
            result.errors.append(f"{reminder.id}: {exc}")
        if state.attempts == 1 or state.attempts % 5 == 0 or state.gave_up:
            self.log.warning(
                "[reminder] 发送失败 id=%s 第 %s 次：%s（%s 秒后重试）",
                reminder.id,
                state.attempts,
                exc,
                delay,
            )
        if state.gave_up:
            self.log.error(
                "[reminder] 提醒 id=%s 连续失败 %s 次，暂停自动重试；"
                "提醒仍保持 pending，可用 /reminder check 重置重试",
                reminder.id,
                state.attempts,
            )

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    async def status_snapshot(self) -> Dict[str, Any]:
        pending: Optional[int] = None
        next_reminder: Optional[Reminder] = None
        db_error = ""
        try:
            if self.store.exists():
                pending = await asyncio.to_thread(self.store.count_pending)
                next_reminder = await asyncio.to_thread(self.store.next_pending)
            else:
                db_error = f"数据库不存在: {self.store.db_path}"
        except Exception as exc:  # noqa: BLE001 - status 命令本身不能报错
            db_error = str(exc)
        return {
            "running": self.running,
            "enabled": bool(self.settings.enabled),
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "pending": pending,
            "next_reminder": next_reminder,
            "last_scan_at": self._last_scan_at,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "retry_count": len(self._retry),
            "db_error": db_error,
            "timezone": self.settings.timezone,
        }

    def format_next(self, reminder: Optional[Reminder]) -> str:
        if reminder is None or reminder.remind_at_dt is None:
            return "无"
        return format_local(reminder.remind_at_dt, self.settings.timezone)


__all__ = [
    "ReminderScheduler",
    "ScanResult",
    "SchedulerSettings",
    "describe_platforms",
]
