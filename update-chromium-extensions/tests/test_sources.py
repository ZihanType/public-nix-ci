from __future__ import annotations

import http.client
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile


COMPONENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT_ROOT / "scripts"))

from chromium_extensions.crx3 import (  # noqa: E402
    build_reproducible_crx3,
    canonicalize_extension_zip,
    generate_rsa_private_key,
)
from chromium_extensions.model import GitHubReleaseSource  # noqa: E402
from chromium_extensions.sources import (  # noqa: E402
    ChromeWebStoreClient,
    Download,
    HttpClient,
    PublicGitHubReleaseClient,
)


def source_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("extension/manifest.json", '{"version":"2.3.4","name":"Example"}')
        archive.writestr("extension/script.js", "// example\n")
    return output.getvalue()


class FakeHttpClient:
    def __init__(self, crx: bytes) -> None:
        self.crx = crx
        self.asset_downloads = 0

    def get_json(self, url: str, *, github_api: bool = False):
        if "chrome-for-testing" in url:
            return {"channels": {"Stable": {"version": "152.0.7977.54"}}}
        if github_api:
            return {
                "tag_name": "2.3.4",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "example-2.3.4.zip",
                        "browser_download_url": "https://example.invalid/example.zip",
                    },
                    {
                        "name": "example-firefox.zip",
                        "browser_download_url": "https://example.invalid/firefox.zip",
                    },
                ],
            }
        raise AssertionError("unexpected JSON URL %s" % url)

    def download_public_asset(self, url: str) -> Download:
        self.asset_downloads += 1
        if "clients2.google.com" in url:
            return Download(self.crx, "https://cdn.invalid/example.crx", "application/x-chrome-extension")
        return Download(source_zip(), url, "application/zip")


class FakeHeaders:
    def __init__(
        self,
        content_length: int | None,
        *,
        accept_ranges: str | None = None,
        content_range: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        self.values = {
            "Content-Length": None if content_length is None else str(content_length),
            "Accept-Ranges": accept_ranges,
            "Content-Range": content_range,
            "ETag": etag,
            "Last-Modified": last_modified,
        }

    def get(self, name: str):
        return self.values.get(name)

    def get_content_type(self) -> str:
        return "application/octet-stream"


class FakeResponse:
    def __init__(
        self,
        contents: bytes,
        *,
        content_length: int | None = None,
        maximum_chunk_bytes: int | None = None,
        status: int = 200,
        effective_url: str = "https://cdn.invalid/artifact.crx",
        accept_ranges: str | None = None,
        content_range: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.contents = contents
        self.headers = FakeHeaders(
            content_length,
            accept_ranges=accept_ranges,
            content_range=content_range,
            etag=etag,
            last_modified=last_modified,
        )
        self.maximum_chunk_bytes = maximum_chunk_bytes
        self.offset = 0
        self.status = status
        self.effective_url = effective_url
        self.read_error = read_error

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, maximum_bytes: int) -> bytes:
        if self.read_error is not None:
            error = self.read_error
            self.read_error = None
            raise error
        if self.maximum_chunk_bytes is not None:
            maximum_bytes = min(maximum_bytes, self.maximum_chunk_bytes)
        chunk = self.contents[self.offset : self.offset + maximum_bytes]
        self.offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self.effective_url

    def getcode(self) -> int:
        return self.status


class HttpClientTests(unittest.TestCase):
    def test_short_socket_reads_are_accumulated_without_retry(self) -> None:
        complete_contents = b"complete"
        response = FakeResponse(
            complete_contents,
            content_length=len(complete_contents),
            maximum_chunk_bytes=3,
        )
        with mock.patch(
            "chromium_extensions.sources.urllib.request.urlopen",
            return_value=response,
        ) as urlopen:
            downloaded = HttpClient(retries=1).download_public_asset(
                "https://example.invalid/artifact.crx"
            )

        self.assertEqual(downloaded.contents, complete_contents)
        urlopen.assert_called_once()

    def test_incomplete_read_is_retried(self) -> None:
        complete = FakeResponse(b"complete", content_length=len(b"complete"))
        with (
            mock.patch(
                "chromium_extensions.sources.urllib.request.urlopen",
                side_effect=[http.client.IncompleteRead(b"partial", 1), complete],
            ) as urlopen,
            mock.patch("chromium_extensions.sources.time.sleep") as sleep,
        ):
            downloaded = HttpClient(retries=2).download_public_asset(
                "https://example.invalid/artifact.crx"
            )

        self.assertEqual(downloaded.contents, b"complete")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_body_shorter_than_content_length_is_retried(self) -> None:
        complete_contents = b"complete"
        short = FakeResponse(b"partial", content_length=len(complete_contents))
        complete = FakeResponse(
            complete_contents, content_length=len(complete_contents)
        )
        with (
            mock.patch(
                "chromium_extensions.sources.urllib.request.urlopen",
                side_effect=[short, complete],
            ) as urlopen,
            mock.patch("chromium_extensions.sources.time.sleep") as sleep,
        ):
            downloaded = HttpClient(retries=2).download_public_asset(
                "https://example.invalid/artifact.crx"
            )

        self.assertEqual(downloaded.contents, complete_contents)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_incomplete_response_resumes_from_validated_range(self) -> None:
        initial_url = "https://example.invalid/artifact.crx"
        final_url = "https://cdn.invalid/artifact.crx"
        # Chrome Web Store's CDN currently returns an unquoted ETag, which is
        # not a valid strong If-Range entity tag. Fall back to its date validator.
        etag = "nonstandard-unquoted-etag"
        last_modified = "Sun, 05 Jul 2026 20:57:05 GMT"
        partial = FakeResponse(
            b"",
            content_length=len(b"complete"),
            effective_url=final_url,
            accept_ranges="bytes",
            etag=etag,
            last_modified=last_modified,
            read_error=http.client.IncompleteRead(b"co", 6),
        )
        partial_remainder = FakeResponse(
            b"",
            content_length=6,
            status=206,
            effective_url=final_url,
            accept_ranges="bytes",
            content_range="bytes 2-7/8",
            etag=etag,
            last_modified=last_modified,
            read_error=http.client.IncompleteRead(b"mp", 4),
        )
        remainder = FakeResponse(
            b"lete",
            content_length=len(b"lete"),
            status=206,
            effective_url=final_url,
            accept_ranges="bytes",
            content_range="bytes 4-7/8",
            etag=etag,
            last_modified=last_modified,
        )
        with (
            mock.patch(
                "chromium_extensions.sources.urllib.request.urlopen",
                side_effect=[partial, partial_remainder, remainder],
            ) as urlopen,
            mock.patch("chromium_extensions.sources.time.sleep"),
        ):
            downloaded = HttpClient(retries=1).download_public_asset(initial_url)

        self.assertEqual(downloaded.contents, b"complete")
        resume_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(resume_request.full_url, final_url)
        self.assertEqual(resume_request.get_header("Range"), "bytes=2-7")
        self.assertEqual(resume_request.get_header("If-range"), last_modified)
        final_request = urlopen.call_args_list[2].args[0]
        self.assertEqual(final_request.get_header("Range"), "bytes=4-7")

    def test_failed_resume_falls_back_to_original_url(self) -> None:
        initial_url = "https://example.invalid/artifact.crx"
        final_url = "https://cdn.invalid/artifact.crx"
        large_total = 10 * 1024 * 1024
        partial = FakeResponse(
            b"part",
            content_length=large_total,
            effective_url=final_url,
            accept_ranges="bytes",
            etag='"strong-etag"',
        )
        resume_error = urllib.error.HTTPError(
            final_url,
            500,
            "Internal Server Error",
            None,
            None,
        )
        complete = FakeResponse(b"complete", content_length=len(b"complete"))
        with (
            mock.patch(
                "chromium_extensions.sources.urllib.request.urlopen",
                side_effect=[partial, resume_error, complete],
            ) as urlopen,
            mock.patch("chromium_extensions.sources.time.sleep"),
        ):
            downloaded = HttpClient(retries=2).download_public_asset(initial_url)

        self.assertEqual(downloaded.contents, b"complete")
        failed_resume = urlopen.call_args_list[1].args[0]
        self.assertEqual(failed_resume.full_url, final_url)
        self.assertEqual(failed_resume.get_header("Range"), "bytes=4-4194307")
        fallback_request = urlopen.call_args_list[2].args[0]
        self.assertEqual(fallback_request.full_url, initial_url)
        self.assertIsNone(fallback_request.get_header("Range"))


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        key_path = Path(cls.temporary_directory.name) / "example.pem"
        generate_rsa_private_key(key_path)
        canonical = canonicalize_extension_zip(source_zip())
        cls.built = build_reproducible_crx3(canonical.zip_bytes, key_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_web_store_uses_stable_full_version_and_explicit_platform(self) -> None:
        http = FakeHttpClient(self.built.contents)
        client = ChromeWebStoreClient(http)  # type: ignore[arg-type]
        chrome_version = client.latest_stable_chrome_version()
        artifact = client.resolve(self.built.extension_id, chrome_version)
        self.assertEqual(chrome_version, "152.0.7977.54")
        self.assertEqual(artifact.name, "Example")
        self.assertEqual(artifact.version, "2.3.4")
        self.assertEqual(artifact.request_parameters["os"], "linux")
        self.assertEqual(artifact.request_parameters["arch"], "x64")
        self.assertEqual(artifact.request_parameters["prod"], "chromecrx")

    def test_github_asset_glob_must_select_one_zip(self) -> None:
        http = FakeHttpClient(self.built.contents)
        client = PublicGitHubReleaseClient(http)  # type: ignore[arg-type]
        artifact = client.resolve_zip(
            GitHubReleaseSource("example", "owner/repository", "example-[0-9.]*.zip")
        )
        self.assertEqual(artifact.asset_name, "example-2.3.4.zip")
        self.assertEqual(artifact.release_tag, "2.3.4")

    def test_omitted_glob_fails_when_release_has_multiple_zips(self) -> None:
        http = FakeHttpClient(self.built.contents)
        client = PublicGitHubReleaseClient(http)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "exactly one"):
            client.resolve_zip(GitHubReleaseSource("example", "owner/repository", None))


if __name__ == "__main__":
    unittest.main()
