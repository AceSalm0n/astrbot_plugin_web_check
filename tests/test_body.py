import asyncio
import unittest
from tests.support import Response, Session, webcheck as w


class EndlessBody:
    def __init__(self):
        self.consumed = 0

    async def iter_chunked(self, size):
        while True:
            await asyncio.sleep(0)
            self.consumed += size
            yield b"x" * size

    async def read(self, size):
        await asyncio.sleep(0)
        self.consumed += size
        return b"x" * size

    def at_eof(self):
        return False


class ShortChunks:
    def __init__(self):
        self.parts = [b"x"] + [b"x" * 8192 for _ in range(8)]
        self.consumed = 0

    async def iter_chunked(self, size):
        for part in self.parts:
            self.consumed += len(part)
            yield part

    async def read(self, size):
        if not self.parts:
            return b""
        chunk = self.parts[0][:size]
        self.parts[0] = self.parts[0][size:]
        if not self.parts[0]:
            self.parts.pop(0)
        self.consumed += len(chunk)
        return chunk

    def at_eof(self):
        return not self.parts


class BodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_chunks_cannot_overshoot_cap(self):
        response = Response()
        response.content = ShortChunks()
        result = await w.check_url(Session([response]), "https://example.com", use_head=False, retry=0)
        self.assertEqual(response.content.consumed, 65536)
        self.assertEqual(result.content_length, 65536)
        self.assertTrue(result.content_truncated)

    async def test_complete_body_exactly_at_cap_is_not_marked_truncated(self):
        response = Response(body=b"x" * 65536)
        result = await w.check_url(Session([response]), "https://example.com", use_head=False, retry=0)
        self.assertEqual(result.content_length, 65536)
        self.assertFalse(result.content_truncated)

    async def test_large_get_is_capped_and_marked(self):
        response = Response(headers={"Content-Length": "1048576"}, body=b"x" * 1048576)
        result = await w.check_url(Session([response]), "https://example.com", use_head=False, retry=0)
        self.assertFalse(result.error)
        self.assertLessEqual(response.content.consumed, 65536)
        self.assertEqual(result.content_length, 65536)
        self.assertEqual(result.declared_length, 1048576)
        self.assertTrue(result.content_truncated)
        self.assertIn("截断", result.format_detail())
        self.assertTrue(response.closed)

    async def test_endless_body_finishes_at_cap(self):
        response = Response()
        response.content = EndlessBody()
        result = await asyncio.wait_for(w.check_url(Session([response]), "https://example.com",
                                       use_head=False, retry=0), .2)
        self.assertFalse(result.error)
        self.assertEqual(response.content.consumed, 65536)
        self.assertTrue(result.content_truncated)

    async def test_head_reads_no_body_and_reports_declared_size(self):
        response = Response(headers={"Content-Length": "123456"}, body=b"large")
        result = await w.check_url(Session([response]), "https://example.com", retry=0)
        self.assertEqual(response.content.consumed, 0)
        self.assertEqual(result.content_length, 0)
        self.assertEqual(result.declared_length, 123456)
        self.assertIn("声明", result.format_detail())

    async def test_405_fallback_releases_head_and_limits_get(self):
        first, second = Response(405), Response(body=b"x" * 100000)
        session = Session([first, second])
        result = await w.check_url(session, "https://example.com", retry=0)
        self.assertEqual([method for method, _, _ in session.requests], ["HEAD", "GET"])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(result.content_length, 65536)
        self.assertTrue(result.content_truncated)

    async def test_unknown_or_bad_declared_size_does_not_fail(self):
        for headers in ({}, {"Content-Length": "wrong"}, {"Content-Length": "-1"}):
            with self.subTest(headers=headers):
                result = await w.check_url(Session([Response(headers=headers, body=b"abc")]),
                                           "https://example.com", use_head=False, retry=0)
                self.assertFalse(result.error)
                self.assertIsNone(result.declared_length)
                self.assertEqual(result.content_length, 3)
