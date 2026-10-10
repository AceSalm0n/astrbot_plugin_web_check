import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from tests.support import Event, messages, webcheck as w


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_config_returns_actionable_error(self):
        cases = [("watchlist", "https://example.com"), ("watchlist", None),
                 ("check_timeout", 0), ("check_timeout", 121), ("check_timeout", True),
                 ("max_concurrent", 0), ("max_concurrent", 21), ("retry_count", -1),
                 ("retry_count", 4), ("max_sites", 0), ("max_sites", 101),
                 ("use_head", "true"), ("verify_ssl", 1), ("user_agent", "bad\r\nheader")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                plugin = w.WebCheckPlugin(None, {key: value})
                output = "\n".join(await messages(plugin.cmd_status(Event())))
                self.assertIn("配置错误", output)
                self.assertIn(key, output)

    async def test_defaults_are_shared_with_backend_schema(self):
        schema = json.loads(Path('_conf_schema.json').read_text(encoding='utf-8'))
        plugin = w.WebCheckPlugin(None, {})
        for key in ("max_concurrent", "retry_count", "use_head", "follow_redirects",
                    "verify_ssl", "user_agent", "watchlist", "max_sites", "check_timeout"):
            with self.subTest(key=key):
                self.assertIn(key, schema)
                self.assertEqual(plugin._cfg(key), schema.get(key, {}).get("default"))

    async def test_explicit_empty_watchlist_is_not_replaced(self):
        plugin = w.WebCheckPlugin(None, {"watchlist": []})
        output = "\n".join(await messages(plugin.cmd_batch_check(Event())))
        self.assertIn("未配置", output)

    async def test_invalid_watchlist_items_remain_visible(self):
        plugin = w.WebCheckPlugin(None, {"watchlist": [None, "", "example.com"]})
        plugin._do_check = AsyncMock(return_value=w.CheckResult("https://example.com", "example.com", status_code=200))
        output = "\n".join(await messages(plugin.cmd_batch_check(Event())))
        self.assertIn("3", output)
        self.assertIn("输入", output)
        self.assertIn("example.com", output)

    async def test_site_limit_rejects_entire_command(self):
        plugin = w.WebCheckPlugin(None, {"max_sites": 2})
        plugin._do_check = AsyncMock(return_value=w.CheckResult("https://example.com", "example.com", status_code=200))
        output = "\n".join(await messages(plugin.cmd_check(Event(), "example.com example.com example.com")))
        self.assertIn("上限", output)
        self.assertIn("3", output)
        self.assertIn("2", output)
        plugin._do_check.assert_not_awaited()

    async def test_messages_are_bounded_for_list_and_results(self):
        plugin = w.WebCheckPlugin(None, {"watchlist": ["https://example.com/" + "x" * 240 for _ in range(40)]})
        listing = await messages(plugin.cmd_list_watchlist(Event()))
        self.assertGreater(len(listing), 1)
        self.assertTrue(all(len(part) <= 1800 for part in listing))
        plugin._do_check = AsyncMock(return_value=w.CheckResult("https://example.com", "example.com", error="连接失败" * 50))
        results = await messages(plugin.cmd_batch_check(Event()))
        self.assertTrue(all(len(part) <= 1800 for part in results))
        self.assertGreater(len(results), 2)

    async def test_concurrency_is_shared_across_commands(self):
        plugin = w.WebCheckPlugin(None, {"max_concurrent": 2})
        active = peak = 0

        async def perform(url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return w.CheckResult(url, "example.com", status_code=200)

        plugin._do_check = perform
        await asyncio.gather(*(messages(plugin.cmd_check(Event(), "example.com example.com")) for _ in range(3)))
        self.assertEqual(peak, 2)
