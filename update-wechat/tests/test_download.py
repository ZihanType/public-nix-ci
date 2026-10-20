"""检查截断重试及重定向时的凭据边界。"""

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.request

import support
from wechat_mirror.download import HTTPSRedirectHandler, download


def response(content, length=None, status=200):
    result = io.BytesIO(content)
    result.status = status
    result.headers = {"Content-Length": str(len(content) if length is None else length)}
    return result


class DownloadTests(unittest.TestCase):
    def test_truncated_download_restarts_without_appending_partial_data(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b"partial", 50), response(b"complete")]
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("wechat_mirror.download.time.sleep"):
            path = Path(temp) / "package"
            digest = download("https://example.com/package", path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(digest, hashlib.sha256(b"complete").hexdigest())

    def test_partial_http_response_is_not_a_complete_package(self):
        opener = MagicMock()
        opener.open.return_value = response(b"partial", status=206)
        with tempfile.TemporaryDirectory() as temp, patch(
            "wechat_mirror.download.urllib.request.build_opener", return_value=opener
        ):
            with self.assertRaises(ValueError):
                download("https://example.com/package", Path(temp) / "package")

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
