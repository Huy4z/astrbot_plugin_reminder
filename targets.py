"""把 paadm 用户映射成 AstrBot 主动消息的 UMO（unified_msg_origin）。

UMO 格式固定为 ``platform_id:MessageType:session_id``，例如：

- QQ 官方机器人私聊：``qq-main:FriendMessage:<QQ_OPENID>``
- WebChat：``webchat:FriendMessage:webchat!<USER>!<THREAD>``

映射优先顺序：

1. ``session_overrides``：精确覆盖，键可以是 ``user_id``、
   ``provider:external_user_id`` 或 ``provider:<provider>``，值为完整 UMO；
2. ``platform_map``：配置 provider → 平台 ID（如 ``{"qq": "qq-main"}``）；
3. 自动匹配：按平台适配器类型（``qq_official``、``webchat`` 等）选择已加载
   且支持主动消息的平台。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

SESSION_TYPES = {"FriendMessage", "GroupMessage", "OtherMessage"}
DEFAULT_SESSION_TYPE = "FriendMessage"

# provider → 可能的 AstrBot 平台适配器类型
PROVIDER_PLATFORM_TYPES: Dict[str, set] = {
    "qq": {"qq_official", "qqofficial", "aiocqhttp", "napcat", "onebot"},
    "webchat": {"webchat"},
    "telegram": {"telegram"},
    "discord": {"discord"},
    "slack": {"slack"},
    "wechat": {"wechat", "wecom"},
    "wecom": {"wecom"},
    "line": {"line"},
    "misskey": {"misskey"},
    "satori": {"satori"},
}


class TargetError(RuntimeError):
    """无法确定发送目标（用户不存在、平台未加载、UMO 配置非法等）。"""


@dataclass(frozen=True)
class PlatformInfo:
    """当前已加载平台的最小快照（来自 ``platform.meta()``）。"""

    id: str
    type: str
    support_proactive: bool = True

    def label(self) -> str:
        return f"{self.id}({self.type})"


def _provider_key(provider: str) -> str:
    return str(provider or "").strip().lower()


def _aliases(provider: str) -> set:
    key = _provider_key(provider)
    return set(PROVIDER_PLATFORM_TYPES.get(key, {key})) | ({key} if key else set())


def _valid_umo(value: str) -> bool:
    parts = str(value or "").split(":", 2)
    return len(parts) == 3 and all(part.strip() for part in parts)


def _override_for(
    *,
    user_id: str,
    provider: str,
    external_user_id: str,
    overrides: Mapping[str, str],
) -> Optional[str]:
    if not overrides:
        return None
    provider_key = _provider_key(provider)
    candidates = [
        str(user_id or ""),
        f"{provider_key}:{external_user_id}",
        f"provider:{provider_key}",
    ]
    for key in candidates:
        if not key or key not in overrides:
            continue
        value = str(overrides.get(key) or "").strip()
        if not value:
            continue
        if not _valid_umo(value):
            raise TargetError(
                f"session_overrides[{key!r}] 不是合法的 UMO（应为 "
                f"platform:MessageType:session_id）: {value!r}"
            )
        return value
    return None


def _pick_platform(
    *,
    provider: str,
    platforms: Sequence[PlatformInfo],
    platform_map: Mapping[str, str],
) -> str:
    provider_key = _provider_key(provider)
    configured = str(platform_map.get(provider_key, "") or "").strip()
    if configured:
        known = {platform.id for platform in platforms}
        if known and configured not in known:
            raise TargetError(
                f"platform_map 指定的平台 {configured!r} 当前未加载；"
                f"已加载: {_labels(platforms) or '无'}"
            )
        return configured

    aliases = _aliases(provider)
    matches = [
        platform
        for platform in platforms
        if str(platform.type or "").strip().lower() in aliases
        or str(platform.id or "").strip().lower() == provider_key
    ]
    if not matches:
        raise TargetError(
            f"provider={provider_key!r} 没有可用的 AstrBot 平台"
            f"（已加载: {_labels(platforms) or '无'}）"
        )
    matches.sort(key=lambda platform: (not platform.support_proactive, platform.id))
    return matches[0].id


def _labels(platforms: Iterable[PlatformInfo]) -> str:
    return ", ".join(platform.label() for platform in platforms)


def resolve_umo(
    *,
    user_id: str,
    provider: str,
    external_user_id: str,
    platforms: Sequence[PlatformInfo],
    platform_map: Optional[Mapping[str, str]] = None,
    session_type_map: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, str]] = None,
) -> str:
    """返回可直接传给 ``Context.send_message`` 的 UMO 字符串。"""

    override = _override_for(
        user_id=user_id,
        provider=provider,
        external_user_id=external_user_id,
        overrides=overrides or {},
    )
    if override:
        return override

    provider_key = _provider_key(provider)
    if not provider_key:
        raise TargetError("users.provider 为空，无法选择平台")
    if not str(external_user_id or "").strip():
        raise TargetError("users.external_user_id 为空，无法确定会话 ID")

    session_type = str((session_type_map or {}).get(provider_key, DEFAULT_SESSION_TYPE))
    session_type = session_type.strip() or DEFAULT_SESSION_TYPE
    if session_type not in SESSION_TYPES:
        session_type = DEFAULT_SESSION_TYPE
    if session_type != "FriendMessage":
        raise TargetError(
            f"provider={provider_key!r} 配置为 {session_type}，"
            "群聊/其它会话必须用 session_overrides 指定完整 UMO"
        )

    platform_id = _pick_platform(
        provider=provider_key,
        platforms=platforms,
        platform_map=platform_map or {},
    )
    return f"{platform_id}:{session_type}:{str(external_user_id).strip()}"


def describe_platforms(platforms: Sequence[PlatformInfo]) -> str:
    return _labels(platforms)


__all__ = [
    "DEFAULT_SESSION_TYPE",
    "PROVIDER_PLATFORM_TYPES",
    "PlatformInfo",
    "SESSION_TYPES",
    "TargetError",
    "describe_platforms",
    "resolve_umo",
]
