import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
import aiohttp
from tests.support import Response, Session, webcheck as w


class SlowHeaders(Response):
    def __init__(self, delay, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.header_delay = delay

    async def __aenter__(self):
        await asyncio.sleep(self.header_delay)
        return self


class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_or_headers_are_inside_total_deadline(self):
        session = Session([SlowHeaders(.15)])
        started = time.monotonic()
        result = await w.check_url(session, "https://example.com", timeout=.04, retry=0)
        self.assertTrue(result.error)
        self.assertLess(time.monotonic() - started, .12)

    async def test_head_fallback_does_not_reset_deadline(self):
        session = Session([SlowHeaders(.03, 405), SlowHeaders(.08)])
        started = time.monotonic()
        result = await w.check_url(session, "https://example.com", timeout=.05, retry=0)
        self.assertTrue(result.error)
        self.assertLess(time.monotonic() - started, .12)

    async def test_body_timeout_closes_response(self):
        response = Response(body=b"abc", delay=.15)
        result = await w.check_url(Session([response]), "https://example.com", timeout=.04,
                                   retry=0, use_head=False)
        self.assertTrue(result.error)
        self.assertTrue(response.closed)

    async def test_backoff_cannot_exceed_total_deadline(self):
        session = Session([aiohttp.ServerDisconnectedError()])
        started = time.monotonic()
        result = await w.check_url(session, "https://example.com", timeout=.04, retry=3)
        self.assertTrue(result.error)
        self.assertEqual(len(session.requests), 1)
        self.assertLess(time.monotonic() - started, .12)

    async def test_only_transient_failures_are_retried(self):
        errors = [w.UnsafeTargetError("blocked"), aiohttp.ClientSSLError(None, OSError("cert")),
                  aiohttp.ClientError("bad request")]
        with patch.object(w.asyncio, "sleep", AsyncMock()):
            for error in errors:
                session = Session([error, Response()])
                result = await w.check_url(session, "https://example.com", retry=1)
                self.assertTrue(result.error)
                self.assertEqual(len(session.requests), 1)

    async def test_transient_retry_uses_exponential_delays(self):
        session = Session([aiohttp.ServerDisconnectedError() for _ in range(3)] + [Response()])
        with patch.object(w.asyncio, "sleep", AsyncMock()) as sleep:
            result = await w.check_url(session, "https://example.com", retry=3)
        self.assertFalse(result.error)
        self.assertEqual(result.retries, 3)
        self.assertEqual([args.args[0] for args in sleep.call_args_list], [.5, 1, 2])

    async def test_invalid_url_and_http_errors_do_not_retry(self):
        session = Session([Response(500)])
        result = await w.check_url(session, "https://example.com", retry=3)
        self.assertEqual(result.status_code, 500)
        self.assertEqual(len(session.requests), 1)
        session = Session([])
        result = await w.check_url(session, "https://[bad", retry=3)
        self.assertEqual(result.error_kind, "invalid")
        self.assertEqual(len(session.requests), 0)
