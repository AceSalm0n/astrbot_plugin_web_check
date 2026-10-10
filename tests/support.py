"""AstrBot boundary stubs; HTTP, asyncio and plugin logic remain real."""
import importlib.util
import asyncio
import logging
import sys
import types
from pathlib import Path
from unittest.mock import patch
import aiohttp  # Load real dependencies before the temporary AstrBot module patch.


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


class Body:
    def __init__(self, data=b"ok", delay=0):
        self.data = data
        self.delay = delay
        self.consumed = 0

    async def iter_chunked(self, size):
        for start in range(0, len(self.data), size):
            if self.delay:
                await asyncio.sleep(self.delay)
            chunk = self.data[start:start + size]
            self.consumed += len(chunk)
            yield chunk


class Response:
    def __init__(self, status=200, headers=None, body=b"ok", delay=0):
        self.status = status
        self.headers = headers or {}
        self.content = Body(body, delay)
        self.closed = False

    async def read(self):
        chunks = [chunk async for chunk in self.content.iter_chunked(8192)]
        return b"".join(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Session:
    """Only replace the outbound HTTP boundary; record requests and release responses."""
    def __init__(self, responses, required_ssl=None):
        self.responses = iter(responses)
        self.requests = []
        self.required_ssl = required_ssl

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.required_ssl is not None and kwargs.get("ssl") != self.required_ssl:
            raise AssertionError("TLS policy differs from expected")
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)
