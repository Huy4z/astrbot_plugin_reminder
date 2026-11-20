"""插件单元测试的离线支撑（不 import 真正的 AstrBot）。

在导入 ``main.py`` 之前把最小 stub 装进 ``sys.modules``，再按文件路径加载；
main.py 的包内相对导入会失败并走绝对导入兜底（插件目录已加入 sys.path）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list = []

    def _record(self, level: str):
        def handler(*args, **kwargs) -> None:
            self.records.append((level, args))

        return handler

    debug = property(lambda self: self._record("debug"))
    info = property(lambda self: self._record("info"))
    warning = property(lambda self: self._record("warning"))
    error = property(lambda self: self._record("error"))
    exception = property(lambda self: self._record("exception"))


class _PassthroughDecorator:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def __call__(self, func):
        return func


class Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text


class MessageChain:
    def __init__(self, chain=None) -> None:
        self.chain = list(chain or [])


def _build_astrbot_stub() -> dict:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = FakeLogger()

    event_module = types.ModuleType("astrbot.api.event")
    filter_module = types.ModuleType("astrbot.api.event.filter")

    filter_module.command = _PassthroughDecorator
    filter_module.on_astrbot_loaded = _PassthroughDecorator

    class AstrMessageEvent:
        pass

    event_module.filter = filter_module
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.MessageChain = MessageChain

    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Plain = Plain

    star_module = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None, config=None) -> None:
            self.context = context
            self.config = config

    def register(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator

    star_module.Context = Context
    star_module.Star = Star
    star_module.register = register

    core_module = types.ModuleType("astrbot.core")
    core_star_module = types.ModuleType("astrbot.core.star")
    core_filter_module = types.ModuleType("astrbot.core.star.filter")
    command_module = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    command_module.GreedyStr = GreedyStr
    core_filter_module.command = command_module
    core_star_module.filter = core_filter_module
    core_module.star = core_star_module

    api.event = event_module
    api.star = star_module
    astrbot.api = api
    astrbot.core = core_module

    return {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.event.filter": filter_module,
        "astrbot.api.message_components": components_module,
        "astrbot.api.star": star_module,
        "astrbot.core": core_module,
        "astrbot.core.star": core_star_module,
        "astrbot.core.star.filter": core_filter_module,
        "astrbot.core.star.filter.command": command_module,
    }


def install_stubs() -> None:
    if "astrbot.api" not in sys.modules:
        sys.modules.update(_build_astrbot_stub())


_MODULE = None


def load_plugin():
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    install_stubs()
    spec = importlib.util.spec_from_file_location(
        "reminder_plugin_main", PLUGIN_DIR / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reminder_plugin_main"] = module
    spec.loader.exec_module(module)
    _MODULE = module
    return module
