"""targets.resolve_umo 单元测试。"""

from __future__ import annotations

import unittest

from targets import PlatformInfo, TargetError, describe_platforms, resolve_umo

QQ = PlatformInfo(id="qq-main", type="qq_official")
WEB = PlatformInfo(id="webchat", type="webchat")


class ResolveUmoTest(unittest.TestCase):
    def test_auto_match_qq_private(self):
        umo = resolve_umo(
            user_id="u1",
            provider="qq",
            external_user_id="openid-1",
            platforms=[QQ],
        )
        self.assertEqual(umo, "qq-main:FriendMessage:openid-1")

    def test_auto_match_prefers_proactive_platform(self):
        passive = PlatformInfo(id="QQbot-passive", type="qq_official", support_proactive=False)
        proactive = PlatformInfo(id="QQbot-active", type="qq_official", support_proactive=True)
        umo = resolve_umo(
            user_id="u1",
            provider="qq",
            external_user_id="openid-1",
            platforms=[passive, proactive],
        )
        self.assertEqual(umo, "QQbot-active:FriendMessage:openid-1")

    def test_platform_map(self):
        umo = resolve_umo(
            user_id="u1",
            provider="qq",
            external_user_id="openid-1",
            platforms=[QQ],
            platform_map={"qq": "qq-main"},
        )
        self.assertEqual(umo, "qq-main:FriendMessage:openid-1")

    def test_platform_map_target_not_loaded(self):
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="qq",
                external_user_id="openid-1",
                platforms=[QQ],
                platform_map={"qq": "QQbot-missing"},
            )

    def test_override_has_highest_priority(self):
        umo = resolve_umo(
            user_id="u1",
            provider="qq",
            external_user_id="openid-1",
            platforms=[QQ],
            platform_map={"qq": "qq-main"},
            overrides={
                "u1": "qq-main:GroupMessage:group-1",
                "qq:openid-1": "qq-main:FriendMessage:ignored",
            },
        )
        self.assertEqual(umo, "qq-main:GroupMessage:group-1")

    def test_override_by_provider_external_id(self):
        umo = resolve_umo(
            user_id="u1",
            provider="qq",
            external_user_id="openid-1",
            platforms=[QQ],
            overrides={"qq:openid-1": "qq-main:GroupMessage:group-1"},
        )
        self.assertEqual(umo, "qq-main:GroupMessage:group-1")

    def test_invalid_override_rejected(self):
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="qq",
                external_user_id="openid-1",
                platforms=[QQ],
                overrides={"u1": "not-a-umo"},
            )

    def test_group_type_requires_override(self):
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="qq",
                external_user_id="openid-1",
                platforms=[QQ],
                session_type_map={"qq": "GroupMessage"},
            )

    def test_missing_provider_or_external_id(self):
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="",
                external_user_id="openid-1",
                platforms=[QQ],
            )
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="qq",
                external_user_id="",
                platforms=[QQ],
            )

    def test_no_platform_loaded(self):
        with self.assertRaises(TargetError):
            resolve_umo(
                user_id="u1",
                provider="qq",
                external_user_id="openid-1",
                platforms=[WEB],
            )

    def test_unknown_provider_falls_back_to_type(self):
        umo = resolve_umo(
            user_id="u1",
            provider="webchat",
            external_user_id="astrbot",
            platforms=[WEB],
        )
        self.assertEqual(umo, "webchat:FriendMessage:astrbot")

    def test_describe_platforms(self):
        self.assertEqual(describe_platforms([QQ, WEB]), "qq-main(qq_official), webchat(webchat)")
        self.assertEqual(describe_platforms([]), "")


if __name__ == "__main__":
    unittest.main()
