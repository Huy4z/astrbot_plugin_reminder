"""AstrBot 提醒插件：轮询 paadm 的 reminders 表并主动发送提醒。

设计要点：

- 数据仍由 paadm（Rust CLI）管理，插件只读 ``reminders`` / ``users``；
- 到点后通过 ``Context.send_message`` 主动推送，成功后把状态从
  ``pending`` 改成 ``triggered``，因此同一提醒不会重复发送；
- ``cancelled`` 的提醒不会被插件改写；
- 所有命令都直接查询 SQLite，不依赖 paadm 二进制。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:  # 正常路径：AstrBot 以 data.plugins.<name>.main 导入
    from .reminder_store import (
        ReminderStore,
        format_local,
    )
    from .scheduler import (
        ReminderScheduler,
        ScanResult,
        SchedulerSettings,
    )
    from .targets import PlatformInfo, describe_platforms
except ImportError:  # pragma: no cover - 以顶层模块/文件路径加载时
    from reminder_store import (  # type: ignore[no-redef]
        ReminderStore,
        format_local,
    )
    from scheduler import (  # type: ignore[no-redef]
        ReminderScheduler,
        ScanResult,
        SchedulerSettings,
    )
    from targets import (  # type: ignore[no-redef]
        PlatformInfo,
        describe_platforms,
    )

try:  # AstrBot 命令参数类型：收集命令名之后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 极少数加载方式下退化为 str
    GreedyStr = str  # type: ignore[assignment,misc]

PLUGIN_NAME = "astrbot_plugin_reminder"
PLUGIN_VERSION = "1.0.0"
PLUGIN_DESC = "读取 paadm 数据库中的提醒，到点后主动推送到 QQ/WebChat 等平台"
PLUGIN_REPO = "https://github.com/Huy4z/astrbot_plugin_reminder"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "db_path": "/AstrBot/data/paa/paa.db",
    "poll_interval_seconds": 20,
    "startup_delay_seconds": 10,
    "max_overdue_hours": 24,
    "batch_limit": 20,
    "message_template": "⏰ 提醒：{content}",
    "timezone": "Asia/Shanghai",
    "max_message_chars": 800,
    "retry_base_seconds": 30,
    "retry_max_seconds": 600,
    "max_delivery_attempts": 20,
    "send_interval_ms": 300,
    "platform_map_json": "",
    "session_type_map_json": "",
    "session_overrides_json": "",
}

PLATFORM_PROVIDER_MAP: Dict[str, str] = {
    "aiocqhttp": "qq",
    "napcat": "qq",
    "onebot": "qq",
    "qq": "qq",
    "qq_official": "qq",
    "qqofficial": "qq",
    "webchat": "webchat",
    "telegram": "telegram",
    "wechat": "wechat",
    "wecom": "wecom",
    "discord": "discord",
    "slack": "slack",
}
"""AstrBot 平台名 → paadm provider（未知平台名小写兜底）。"""


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "y"}:
            return True
        if lowered in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


def _config_bool(config: Dict[str, Any], key: str, default: bool) -> bool:
    return _as_bool(config.get(key, default), default)


def _config_int(
    config: Dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    value = config.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning("[reminder] 配置 %s=%r 不是整数，使用默认值 %s", key, value, default)
        number = default
    if number < minimum:
        logger.warning("[reminder] 配置 %s=%s 小于 %s，已按 %s 处理", key, number, minimum, minimum)
        number = minimum
    if maximum is not None and number > maximum:
        logger.warning("[reminder] 配置 %s=%s 大于 %s，已按 %s 处理", key, number, maximum, maximum)
        number = maximum
    return number


def _config_str(config: Dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _config_map(config: Dict[str, Any], key: str) -> Dict[str, str]:
    """读取 JSON 对象形式的配置；也兼容 WebUI 直接存成 dict。"""

    raw = config.get(key, "")
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("[reminder] 配置 %s 不是合法 JSON：%s", key, exc)
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        logger.warning("[reminder] 配置 %s 必须是 JSON 对象，已忽略", key)
        return {}
    result: Dict[str, str] = {}
    for map_key, map_value in data.items():
        if map_key is None or map_value is None:
            continue
        key_text = str(map_key).strip()
        value_text = str(map_value).strip()
        if key_text and value_text:
            result[key_text] = value_text
    return result


@register(PLUGIN_NAME, "Huy4z", PLUGIN_DESC, PLUGIN_VERSION)
class ReminderPlugin(Star):
    """到点主动推送 paadm 提醒。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config: Dict[str, Any] = dict(config or {})
        settings = self._build_settings(self.config)
        self.db_path = _config_str(self.config, "db_path", DEFAULT_CONFIG["db_path"])
        self.store = ReminderStore(self.db_path)
        self.scheduler = ReminderScheduler(
            store=self.store,
            settings=settings,
            sender=self._send_text,
            platform_lister=self._list_platforms,
            log=logger,
        )

    # ------------------------------------------------------------------
    # 配置 / 平台
    # ------------------------------------------------------------------
    def _build_settings(self, config: Dict[str, Any]) -> SchedulerSettings:
        return SchedulerSettings(
            enabled=_config_bool(config, "enabled", True),
            poll_interval_seconds=_config_int(
                config, "poll_interval_seconds", 20, minimum=5, maximum=3600
            ),
            startup_delay_seconds=_config_int(
                config, "startup_delay_seconds", 10, minimum=0, maximum=300
            ),
            max_overdue_hours=_config_int(
                config, "max_overdue_hours", 24, minimum=0, maximum=24 * 365
            ),
            batch_limit=_config_int(
                config, "batch_limit", 20, minimum=1, maximum=100
            ),
            message_template=_config_str(
                config, "message_template", str(DEFAULT_CONFIG["message_template"])
            ),
            timezone=_config_str(
                config, "timezone", str(DEFAULT_CONFIG["timezone"])
            ),
            max_message_chars=_config_int(
                config, "max_message_chars", 800, minimum=100, maximum=4000
            ),
            retry_base_seconds=_config_int(
                config, "retry_base_seconds", 30, minimum=1, maximum=3600
            ),
            retry_max_seconds=_config_int(
                config, "retry_max_seconds", 600, minimum=1, maximum=24 * 3600
            ),
            max_delivery_attempts=_config_int(
                config, "max_delivery_attempts", 20, minimum=1, maximum=100
            ),
            send_interval_ms=_config_int(
                config, "send_interval_ms", 300, minimum=0, maximum=10000
            ),
            platform_map=_config_map(config, "platform_map_json"),
            session_type_map=_config_map(config, "session_type_map_json"),
            session_overrides=_config_map(config, "session_overrides_json"),
        )

    def _list_platforms(self) -> List[PlatformInfo]:
        platforms: List[PlatformInfo] = []
        try:
            instances = list(self.context.platform_manager.platform_insts)
        except Exception:  # noqa: BLE001 - 平台管理器异常不应影响发送
            logger.exception("[reminder] 获取平台列表失败")
            return platforms
        for platform in instances:
            try:
                meta = platform.meta()
                platforms.append(
                    PlatformInfo(
                        id=str(getattr(meta, "id", "") or ""),
                        type=str(getattr(meta, "name", "") or ""),
                        support_proactive=bool(
                            getattr(meta, "support_proactive_message", True)
                        ),
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning("[reminder] 读取平台信息失败", exc_info=True)
        return platforms

    async def _send_text(self, umo: str, text: str) -> bool:
        try:
            return bool(
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            )
        except Exception:  # noqa: BLE001 - 交给 scheduler 重试
            logger.exception("[reminder] 主动发送异常 umo=%s", umo)
            return False

    def _provider_for_event(self, event: AstrMessageEvent) -> str:
        try:
            platform_name = str(event.get_platform_name() or "").strip().lower()
        except Exception:  # noqa: BLE001
            platform_name = ""
        if not platform_name:
            try:
                origin = str(event.unified_msg_origin)
                platform_name = origin.split(":", 1)[0].strip().lower()
            except Exception:  # noqa: BLE001
                platform_name = ""
        if not platform_name:
            return "unknown"
        return PLATFORM_PROVIDER_MAP.get(platform_name, platform_name)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        logger.info(
            "[reminder] 插件启动 v%s：db=%s，轮询=%s 秒，过期阈值=%s 小时",
            PLUGIN_VERSION,
            self.db_path,
            self.scheduler.settings.poll_interval_seconds,
            self.scheduler.settings.max_overdue_hours,
        )
        self._startup_check()
        self.scheduler.start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        logger.info("[reminder] AstrBot 加载完成，开始轮询提醒")
        self.scheduler.notify_loaded()

    async def terminate(self) -> None:
        await self.scheduler.stop()
        logger.info("[reminder] 插件已停止")

    def _startup_check(self) -> None:
        """只读自检；任何异常都只告警，不阻止插件加载。"""

        try:
            if not self.store.exists():
                logger.warning("[reminder] 数据库不存在：%s（请检查 db_path）", self.db_path)
                return
            missing = self.store.schema_missing()
            if missing:
                logger.warning(
                    "[reminder] reminders 表缺少字段 %s；请先运行 paadm init/迁移",
                    sorted(missing),
                )
                return
            pending = self.store.count_pending()
            next_reminder = self.store.next_pending()
            next_text = (
                format_local(next_reminder.remind_at_dt, self.scheduler.settings.timezone)
                if next_reminder is not None and next_reminder.remind_at_dt is not None
                else "无"
            )
            logger.info(
                "[reminder] 数据库就绪：pending=%s，下一条=%s", pending, next_text
            )
        except Exception:  # noqa: BLE001
            logger.warning("[reminder] 启动自检失败（插件继续运行）", exc_info=True)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    @filter.command("reminder", alias={"提醒"})
    async def reminder_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """提醒管理：status / check / list / test / help。"""

        reply = await self._dispatch_command(event, args)
        # AstrBot 的异步生成器命令必须 yield MessageEventResult，直接 return
        # 字符串不会被发送。
        yield event.plain_result(reply)

    async def _dispatch_command(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> str:
        raw = str(args or "").strip()
        action = raw.split()[0].lower() if raw else "status"
        if action in {"status", "状态"}:
            return await self._status_text()
        if action in {"check", "scan", "检查", "扫描"}:
            return await self._check_text()
        if action in {"list", "ls", "列表"}:
            return await self._list_text(event)
        if action in {"test", "测试"}:
            return await self._test_text(event)
        if action in {"help", "帮助", "?"}:
            return self._help_text()
        return "用法：/reminder status | check | list | test | help"

    async def _status_text(self) -> str:
        snapshot = await self.scheduler.status_snapshot()
        lines = ["⏰ Reminder 状态"]
        lines.append(
            "运行：{}；自动轮询：{}".format(
                "是" if snapshot["running"] else "否",
                "开启" if snapshot["enabled"] else "关闭（仍可 /reminder check）",
            )
        )
        if snapshot["db_error"]:
            lines.append(f"数据库：异常（{snapshot['db_error']}）")
        else:
            lines.append("数据库：正常")
        pending = snapshot["pending"]
        lines.append(f"待提醒：{pending if pending is not None else '未知'} 条")
        next_reminder = snapshot["next_reminder"]
        if next_reminder is not None:
            content = str(next_reminder.content)
            if len(content) > 40:
                content = content[:40] + "…"
            lines.append(
                "下一条：{}（{}）".format(self.scheduler.format_next(next_reminder), content)
            )
        else:
            lines.append("下一条：无")
        last_scan = snapshot["last_scan_at"]
        if last_scan is not None:
            lines.append(
                "最近扫描：{}".format(
                    format_local(last_scan, self.scheduler.settings.timezone)
                )
            )
        last_result: Optional[ScanResult] = snapshot["last_result"]
        if last_result is not None:
            lines.append(f"最近结果：{last_result.summary()}")
        if snapshot["retry_count"]:
            lines.append(f"等待重试：{snapshot['retry_count']} 条")
        if snapshot["last_error"]:
            lines.append(f"最近错误：{snapshot['last_error']}")
        return "\n".join(lines)

    async def _check_text(self) -> str:
        try:
            result = await self.scheduler.scan_once(force=True)
        except Exception as exc:  # noqa: BLE001 - 命令不能把异常抛给框架
            logger.exception("[reminder] 手动检查失败")
            return f"❌ 检查失败：{exc}"
        if result.error:
            return f"❌ 检查失败：{result.error}"
        return "✅ 检查完成：" + result.summary()

    async def _list_text(self, event: AstrMessageEvent) -> str:
        provider = self._provider_for_event(event)
        try:
            external_user_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            external_user_id = ""
        if not external_user_id:
            return "❌ 无法获取当前发送者 ID，暂时不能查询个人提醒"
        try:
            user = self.store.find_user(provider, external_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[reminder] 查询用户失败")
            return f"❌ 查询数据库失败：{exc}"
        if user is None:
            return (
                "当前账号还没有 paadm 用户记录"
                f"（provider={provider}, external_user_id={external_user_id}）。\n"
                "先用 /paa 创建一条待办或提醒后，paadm 会自动建用户。"
            )
        try:
            reminders = self.store.list_pending_for_user(user.id, limit=10)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[reminder] 查询提醒失败")
            return f"❌ 查询数据库失败：{exc}"
        if not reminders:
            return "📭 当前没有待提醒事项"
        lines = [f"⏰ 待提醒（{len(reminders)} 条）："]
        for index, reminder in enumerate(reminders, 1):
            when = (
                format_local(reminder.remind_at_dt, self.scheduler.settings.timezone)
                if reminder.remind_at_dt is not None
                else reminder.remind_at
            )
            content = str(reminder.content)
            if len(content) > 60:
                content = content[:60] + "…"
            lines.append(f"{index}. {when}  {content}")
        return "\n".join(lines)

    async def _test_text(self, event: AstrMessageEvent) -> str:
        try:
            umo = str(event.unified_msg_origin)
        except Exception:  # noqa: BLE001
            return "❌ 无法获取当前会话标识"
        text = (
            "⏰ Reminder 测试消息\n"
            "如果你看到这条消息，说明当前平台的主动推送可用。\n"
            f"UMO: {umo}"
        )
        ok = await self._send_text(umo, text)
        if ok:
            return "✅ 测试消息已发送（请检查是否收到）"
        return (
            "❌ 测试消息发送失败，请查看 AstrBot 日志。\n"
            f"UMO: {umo}"
        )

    def _help_text(self) -> str:
        platforms = describe_platforms(self._list_platforms()) or "未检测到平台"
        return (
            "⏰ Reminder 使用说明\n"
            "/reminder status  查看运行状态与下一条提醒\n"
            "/reminder check   立即扫描一次（会重置失败重试计数）\n"
            "/reminder list    查看自己的待提醒\n"
            "/reminder test    向当前会话发送测试消息\n"
            "/reminder help    显示本帮助\n"
            "别名：/提醒\n\n"
            f"当前平台：{platforms}\n"
            f"项目：{PLUGIN_REPO}"
        )


__all__ = ["ReminderPlugin", "PLUGIN_NAME", "PLUGIN_VERSION", "DEFAULT_CONFIG"]
