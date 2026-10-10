import unittest
from tests.support import webcheck as w


class ResultTests(unittest.TestCase):
    def test_http_error_is_response_but_not_success(self):
        for status in (404, 500):
            with self.subTest(status=status):
                result = w.CheckResult("https://example.com", "example.com", status_code=status)
                self.assertEqual(result.category, "http_error")
                self.assertTrue(result.received_http)
                self.assertNotIn("可访问", result.format_detail())
                self.assertIn("HTTP 错误", result.format_detail())

    def test_204_and_redirect_have_separate_categories(self):
        normal = w.CheckResult("https://example.com", "example.com", status_code=204)
        redirect = w.CheckResult("https://example.com", "example.com", status_code=302)
        self.assertEqual(normal.category, "success")
        self.assertEqual(redirect.category, "redirect")
        self.assertIn("正常响应", normal.format_detail())
        self.assertIn("重定向", redirect.format_detail())

    def test_summary_accounts_for_every_input(self):
        results = [w.CheckResult("", "host", status_code=status) for status in (200, 204, 302, 404, 500)]
        results += [w.CheckResult("", "host", error="failure", error_kind=kind)
                    for kind in ("network", "tls", "blocked", "invalid")]
        output = w.WebCheckPlugin(None, {})._format_results(results)
        for expected in ("总计：9", "HTTP 有响应：5", "正常响应：2", "重定向：1",
                         "HTTP 错误：2", "网络失败：2", "安全拦截：1", "输入错误：1"):
            self.assertIn(expected, output)

    def test_insecure_warning_also_shown_when_request_fails(self):
        result = w.CheckResult("https://example.com", "example.com", error="连接失败",
                               error_kind="network", tls_verified=False)
        self.assertIn("未验证", result.format_detail())
        self.assertIn("未验证", result.format_brief())
