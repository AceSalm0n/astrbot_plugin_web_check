"""AstrBot boundary stubs; HTTP, asyncio and plugin logic remain real."""
import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch


class Event:
    def plain_result(self, text):
        return text


class Star:
    def __init__(self, context):
        self.context = context


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.core", "astrbot.core.star", "astrbot.core.star.filter",
        "astrbot.core.star.filter.command",
    )}
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("webcheck.tests")
    modules["astrbot.api.event"].AstrMessageEvent = Event
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=lambda *args, **kwargs: lambda fn: fn)
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = lambda *args: lambda cls: cls
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    spec = importlib.util.spec_from_file_location(
        "webcheck_under_test", Path(__file__).resolve().parents[1] / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


webcheck = load_plugin()


async def messages(generator):
    return [message async for message in generator]
