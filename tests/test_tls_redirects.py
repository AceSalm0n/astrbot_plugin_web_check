import unittest
import aiohttp
from tests.support import Response, Session, webcheck as w


class TlsRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_tls_verification_is_default(self):
        result = await w.check_url(Session([Response()], required_ssl=True), "https://example.com", retry=0)
        self.assertFalse(result.error)

    async def test_explicit_insecure_mode_is_visible(self):
        result = await w.check_url(Session([Response()], required_ssl=False), "https://example.com", retry=0, verify_ssl=False)
        self.assertIn("未验证", result.format_detail())
        self.assertIn("未验证", result.format_brief())

    async def test_certificate_error_is_classified(self):
        result = await w.check_url(Session([aiohttp.ClientSSLError(None, OSError("cert"))]), "https://example.com", retry=0)
        self.assertEqual(result.error_kind, "tls")

    async def test_relative_redirects_are_manual(self):
        first = Response(302, {"Location": "/next?token=secret"})
        session = Session([first, Response()])
        result = await w.check_url(session, "https://example.com/a", retry=0, follow_redirects=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session.requests[1][1], "https://example.com/next?token=secret")
        self.assertTrue(first.closed)
        self.assertTrue(all(not kwargs["allow_redirects"] for _, _, kwargs in session.requests))
        self.assertNotIn("secret", result.format_detail())

    async def test_bad_redirect_credentials_rejected(self):
        result = await w.check_url(Session([Response(302, {"Location": "https://user:secret@example.com"})]),
                                   "https://example.com", retry=0, follow_redirects=True)
        self.assertEqual(result.error_kind, "invalid")
        self.assertNotIn("secret", result.format_detail())

    async def test_redirect_limit(self):
        session = Session([Response(302, {"Location": "/again"}) for _ in range(10)])
        result = await w.check_url(session, "https://example.com", retry=0, follow_redirects=True)
        self.assertTrue(result.error)
        self.assertEqual(len(session.requests), 6)

    async def test_redirect_disabled_stops_at_first_response(self):
        session = Session([Response(302, {"Location": "/next"})])
        result = await w.check_url(session, "https://example.com", retry=0)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(len(session.requests), 1)
