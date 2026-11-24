"""检查截断重试及重定向时的凭据边界。"""

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.request

import support
from snipaste_mirror.download import HTTPSRedirectHandler, download


def response(content, length=None, status=200, url="https://example.com/package"):
    result = io.BytesIO(content)
    result.status = status
    result.headers = {"Content-Length": str(len(content) if length is None else length)}
    result.geturl = lambda: url
    return result


class DownloadTests(unittest.TestCase):
    def test_official_checksum_and_versioned_filename_are_verified(self):
        content = b"official package bytes"
        opener = MagicMock()
        opener.open.return_value = response(
            content, url="https://download.snipaste.com/archives/Snipaste-2.11.3.dmg"
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
        ):
            path = Path(temp) / "package"
            digest = download(
                "https://dl.snipaste.com/mac", path,
                expected_name="Snipaste-2.11.3.dmg",
                expected_sha1=hashlib.sha1(content).hexdigest(),
            )
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(digest, hashlib.sha256(content).hexdigest())

    def test_wrong_official_sha1_leaves_no_package(self):
        opener = MagicMock()
        opener.open.return_value = response(b"replacement")
        with tempfile.TemporaryDirectory() as temp, patch(
            "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
        ):
            path = Path(temp) / "package"
            with self.assertRaisesRegex(ValueError, "SHA-1"):
                download("https://example.com/package", path, expected_sha1="0" * 40)
            self.assertFalse(path.exists())
            self.assertEqual(opener.open.call_count, 1)

    def test_stale_or_wrong_platform_redirect_is_rejected(self):
        for final_url in (
            "https://download.snipaste.com/archives/Snipaste-2.10.8.dmg",
            "https://download.snipaste.com/archives/Snipaste-2.11.3-x86_64.AppImage",
            "http://download.snipaste.com/archives/Snipaste-2.11.3.dmg",
        ):
            opener = MagicMock()
            opener.open.return_value = response(b"bytes", url=final_url)
            with self.subTest(url=final_url), tempfile.TemporaryDirectory() as temp, patch(
                "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
            ):
                path = Path(temp) / "package"
                with self.assertRaisesRegex(ValueError, "最终文件名"):
                    download(
                        "https://dl.snipaste.com/mac", path,
                        expected_name="Snipaste-2.11.3.dmg",
                    )
                self.assertFalse(path.exists())

    def test_metadata_size_limit_applies_with_and_without_content_length(self):
        for known_length in (False, True):
            reply = response(b"too large")
            if not known_length:
                reply.headers = {}
            opener = MagicMock()
            opener.open.return_value = reply
            with self.subTest(known_length=known_length), tempfile.TemporaryDirectory() as temp, patch(
                "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
            ):
                path = Path(temp) / "metadata"
                with self.assertRaises(ValueError):
                    download("https://example.com/metadata", path, max_size=4)
                self.assertFalse(path.exists())

    def test_truncated_download_restarts_without_appending_partial_data(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b"partial", 50), response(b"complete")]
        with tempfile.TemporaryDirectory() as temp, patch(
            "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
        ), patch("snipaste_mirror.download.time.sleep"):
            path = Path(temp) / "package"
            digest = download("https://example.com/package", path)
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(digest, hashlib.sha256(b"complete").hexdigest())

    def test_partial_http_response_is_not_a_complete_package(self):
        opener = MagicMock()
        opener.open.return_value = response(b"partial", status=206)
        with tempfile.TemporaryDirectory() as temp, patch(
            "snipaste_mirror.download.urllib.request.build_opener", return_value=opener
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
