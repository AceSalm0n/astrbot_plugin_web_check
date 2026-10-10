import unittest
from unittest.mock import AsyncMock, patch
from tests.support import Event, messages, webcheck as w


class UrlTests(unittest.TestCase):
    def test_uppercase_scheme_and_missing_scheme(self):
        self.assertEqual(w._normalize_url("HTTPS://example.com/a"), "https://example.com/a")
        self.assertEqual(w._normalize_url("example.com/a"), "https://example.com/a")

    def test_invalid_inputs_are_rejected(self):
        for value in ("", "   ", "ftp://example.com", "https://[bad",
                      "https://example.com:bad", "https://example.com:0",
                      "https://example.com:65536", "https://user:secret@example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w._normalize_url(value)


class CommandUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_schemeless_credentials_are_never_echoed(self):
        plugin = w.WebCheckPlugin(None, {})
        output = "\n".join(await messages(plugin.cmd_check(Event(), "user:secret123@example.com")))
        self.assertNotIn("secret123", output)
        self.assertIn("输入", output)

    async def test_bad_url_does_not_abort_other_targets(self):
        plugin = w.WebCheckPlugin(None, {})
        plugin._do_check = AsyncMock(return_value=w.CheckResult(
            "https://example.com", "example.com", status_code=200))
        with patch.object(w, "_is_private_url", AsyncMock(return_value=False)):
            output = "\n".join(await messages(plugin.cmd_check(Event(), "https://[bad example.com")))
        self.assertIn("example.com", output)
        self.assertIn("输入", output)
        self.assertIn("200", output)

    async def test_query_values_hidden_in_progress_and_list(self):
        plugin = w.WebCheckPlugin(None, {"watchlist": ["https://example.com/a?token=secret123"]})
        plugin._do_check = AsyncMock(return_value=w.CheckResult(
            "https://example.com/a?token=secret123", "example.com", status_code=200))
        with patch.object(w, "_is_private_url", AsyncMock(return_value=False)):
            output = await messages(plugin.cmd_check(Event(), "https://example.com/a?token=secret123"))
            output += await messages(plugin.cmd_list_watchlist(Event()))
        self.assertNotIn("secret123", "\n".join(output))
        self.assertIn("example.com", "\n".join(output))

    async def test_missing_argument_shows_usage(self):
        plugin = w.WebCheckPlugin(None, {})
        self.assertIn("用法", "\n".join(await messages(plugin.cmd_check(Event()))))
