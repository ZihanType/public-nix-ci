"""检查截断重试及重定向时的凭据边界。"""

import contextlib
import hashlib
import http.client
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.request

import support
from wechat_mirror.download import HTTPSRedirectHandler, download


def response(content, length=None, status=200):
    result = io.BytesIO(content)
    result.status = status
    result.headers = {"Content-Length": str(len(content) if length is None else length)}
    return result


class DownloadTests(unittest.TestCase):
    def test_network_and_http_failures_retry_past_three_attempts_with_capped_backoff(self):
        url = "https://example.com/package"
        failures = [
            urllib.error.URLError("DNS 失败"),
            TimeoutError("读取超时"),
            urllib.error.HTTPError(url, 404, "Not Found", {}, None),
            urllib.error.HTTPError(url, 403, "Forbidden", {}, None),
            urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None),
            urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None),
            urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None),
            http.client.HTTPException("传输中断"),
            ConnectionResetError("连接重置"),
            TimeoutError("再次超时"),
        ]
        opener = MagicMock()
        opener.open.side_effect = failures + [response(b"complete")]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep") as sleep, contextlib.redirect_stdout(output):
            path = Path(temp) / "package"
            digest = download(url, path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(digest, hashlib.sha256(b"complete").hexdigest())
        self.assertEqual(opener.open.call_count, 11)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1, 2, 4, 8, 16, 32, 60, 60, 60, 60],
        )
        self.assertIn("HTTP Error 404", output.getvalue())
        self.assertIn("60 秒后重试", output.getvalue())
        self.assertIn("第 11 次尝试", output.getvalue())

    def test_empty_download_is_retried(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b""), response(b"complete")]
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep") as sleep:
            path = Path(temp) / "package"
            download("https://example.com/package", path)
            self.assertEqual(path.read_bytes(), b"complete")
            sleep.assert_called_once_with(1)

    def test_oversized_file_is_rejected_without_retry(self):
        opener = MagicMock()
        # 两个任务均限制安装包最多 2 GiB，只模拟超限响应头，不分配大文件。
        opener.open.return_value = response(b"bytes", length=3 * 1024 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep", side_effect=AssertionError("校验失败不应重试")):
            path = Path(temp) / "package"
            with self.assertRaises(ValueError):
                download("https://example.com/package", path)
            self.assertFalse(path.exists())
            self.assertEqual(opener.open.call_count, 1)

    def test_unlimited_retry_can_be_interrupted_and_cleans_partial_file(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("暂时不可用")
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep", side_effect=KeyboardInterrupt):
            path = Path(temp) / "package"
            path.write_bytes(b"unfinished")
            with self.assertRaises(KeyboardInterrupt):
                download("https://example.com/package", path)
            self.assertFalse(path.exists())
            self.assertEqual(opener.open.call_count, 1)

    def test_truncated_download_restarts_without_appending_partial_data(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b"partial", 50) for _ in range(4)] + [response(b"complete")]
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep"):
            path = Path(temp) / "package"
            digest = download("https://example.com/package", path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(digest, hashlib.sha256(b"complete").hexdigest())
            self.assertEqual(opener.open.call_count, 5)

    def test_partial_http_response_is_retried_before_full_download(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b"partial", status=206), response(b"complete")]
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep") as sleep:
            path = Path(temp) / "package"
            download("https://example.com/package", path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(opener.open.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_redirect_drops_authorization_and_rejects_http(self):
        request = urllib.request.Request(
            "https://api.github.com/assets/1", headers={"Authorization": "Bearer secret"}
        )
        handler = HTTPSRedirectHandler()
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn.example.com/package"
        )
        self.assertFalse(redirected.has_header("Authorization"))
        with self.assertRaises(ValueError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://cdn.example.com/package"
            )
