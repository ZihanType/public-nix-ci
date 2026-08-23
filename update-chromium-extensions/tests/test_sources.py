from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
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
