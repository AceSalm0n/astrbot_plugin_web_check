import asyncio
import unittest
from unittest.mock import patch
from yarl import URL
from tests.support import Event, messages, webcheck as w


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_plugin_cannot_recreate_session(self):
        plugin = w.WebCheckPlugin(None, {})
        self.addAsyncCleanup(plugin.terminate)
        session = await plugin._get_session()
        await plugin.terminate()
        with self.assertRaises(ValueError):
            await plugin._get_session()
        self.assertTrue(session.closed)
        output = "\n".join(await messages(plugin.cmd_check(Event(), "example.com")))
        self.assertIn("停止", output)
        await plugin.terminate()

    async def test_unload_cancels_requests_and_queued_checks(self):
        plugin = w.WebCheckPlugin(None, {"max_concurrent": 1})
        started = asyncio.Event()
        finished = []

        async def slow_check(url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(url)

        plugin._do_check = slow_check
        task = asyncio.create_task(plugin._collect_results(["example.com", "other.example"]))
        await started.wait()
        await plugin.terminate()
        ended = task.done()
        if not ended:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(ended)
        self.assertEqual(len(finished), 1)
        self.assertFalse(plugin._tasks)

    async def test_unload_cancels_dns_and_releases_session(self):
        plugin = w.WebCheckPlugin(None, {})
        self.addAsyncCleanup(plugin.terminate)
        session = await plugin._get_session()
        started, released = asyncio.Event(), asyncio.Event()

        async def slow_dns(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

        with patch.object(w.SafeTCPConnector, "_resolve_host", slow_dns):
            task = asyncio.create_task(plugin._collect_results(["https://example.com"]))
            await started.wait()
            await plugin.terminate()
            ended = task.done()
            if not ended:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(ended)
        self.assertTrue(released.is_set())
        self.assertTrue(session.closed)

    async def test_cookie_state_is_not_shared(self):
        plugin = w.WebCheckPlugin(None, {})
        self.addAsyncCleanup(plugin.terminate)
        session = await plugin._get_session()
        try:
            session.cookie_jar.update_cookies({"sid": "secret"}, response_url=URL("https://example.com"))
            self.assertFalse(session.cookie_jar.filter_cookies(URL("https://example.com")))
        finally:
            await plugin.terminate()

    async def test_caller_cancellation_releases_owned_tasks(self):
        plugin = w.WebCheckPlugin(None, {})
        started = asyncio.Event()

        async def slow_check(url):
            started.set()
            await asyncio.Event().wait()

        plugin._do_check = slow_check
        task = asyncio.create_task(plugin._collect_results(["example.com"]))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(plugin._tasks)
        self.assertEqual(plugin._runs, 0)
        await plugin.terminate()
