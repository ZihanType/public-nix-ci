"""Resolve upstream Chrome Web Store CRXs and public GitHub Release ZIPs."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import re
import time
from typing import Dict, Mapping, Optional
import urllib.error
import urllib.parse
import urllib.request

from .crx3 import extension_manifest_from_crx3, parse_crx3, sha256_hex
from .model import GitHubReleaseSource


CHROME_FOR_TESTING_VERSIONS_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json"
)
CHROME_WEB_STORE_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
GITHUB_API_VERSION = "2026-03-10"
USER_AGENT = "ZihanType/public-nix-ci Chromium extension resolver"

# The same operational cap used by the ZIP normalizer. The HTTP layer enforces
# it before buffering an untrusted response in memory.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Download:
    contents: bytes
    effective_url: str
    content_type: str


class HttpClient:
    def __init__(self, *, github_token: Optional[str] = None, retries: int = 3) -> None:
        self.github_token = github_token
        self.retries = retries

    def _request(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        maximum_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> Download:
        request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None and int(content_length) > maximum_bytes:
                        raise ValueError(
                            "response from %s exceeds the %d-byte limit" % (url, maximum_bytes)
                        )
                    contents = response.read(maximum_bytes + 1)
                    if len(contents) > maximum_bytes:
                        raise ValueError(
                            "response from %s exceeds the %d-byte limit" % (url, maximum_bytes)
                        )
                    return Download(
                        contents,
                        response.geturl(),
                        response.headers.get_content_type(),
                    )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        assert last_error is not None
        raise RuntimeError("failed to download %s: %s" % (url, last_error)) from last_error

    def get_json(self, url: str, *, github_api: bool = False) -> Mapping[str, object]:
        headers = {"Accept": "application/vnd.github+json"} if github_api else {}
        if github_api:
            headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
            if self.github_token:
                headers["Authorization"] = "Bearer " + self.github_token
        response = self._request(url, headers=headers, maximum_bytes=8 * 1024 * 1024)
        try:
            value = json.loads(response.contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("%s did not return valid UTF-8 JSON" % url) from error
        if not isinstance(value, dict):
            raise ValueError("%s did not return a JSON object" % url)
        return value

    def download_public_asset(self, url: str) -> Download:
        # Public browser_download_url responses intentionally omit Authorization,
        # so a token cannot leak across GitHub's redirect/CDN boundary.
        return self._request(url)


@dataclass(frozen=True)
class ChromeWebStoreArtifact:
    extension_id: str
    name: str
    version: str
    contents: bytes
    source_url: str
    chrome_version: str
    request_parameters: Mapping[str, str]


class ChromeWebStoreClient:
    def __init__(self, http: HttpClient, *, openssl: str = "openssl") -> None:
        self.http = http
        self.openssl = openssl

    def latest_stable_chrome_version(self) -> str:
        metadata = self.http.get_json(CHROME_FOR_TESTING_VERSIONS_URL)
        try:
            channels = metadata["channels"]
            assert isinstance(channels, dict)
            stable = channels["Stable"]
            assert isinstance(stable, dict)
            version = stable["version"]
        except (KeyError, AssertionError) as error:
            raise ValueError("Chrome for Testing metadata has no Stable channel version") from error
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version):
            raise ValueError("Chrome for Testing returned an invalid Stable version")
        return version

    @staticmethod
    def request_parameters(extension_id: str, chrome_version: str) -> Dict[str, str]:
        return {
            "response": "redirect",
            "os": "linux",
            "arch": "x64",
            "prod": "chromecrx",
            "prodchannel": "stable",
            "prodversion": chrome_version,
            "lang": "en-US",
            "acceptformat": "crx3",
            "x": "id=%s&installsource=ondemand&uc" % extension_id,
        }

    def resolve(self, extension_id: str, chrome_version: str) -> ChromeWebStoreArtifact:
        parameters = self.request_parameters(extension_id, chrome_version)
        request_url = CHROME_WEB_STORE_UPDATE_URL + "?" + urllib.parse.urlencode(parameters)
        last_validation_error: Optional[BaseException] = None
        for _ in range(3):
            downloaded = self.http.download_public_asset(request_url)
            try:
                parsed = parse_crx3(downloaded.contents)
                if parsed.extension_id != extension_id:
                    raise ValueError(
                        "Chrome Web Store returned extension ID %s for requested ID %s"
                        % (parsed.extension_id, extension_id)
                    )
                manifest = extension_manifest_from_crx3(
                    downloaded.contents, openssl=self.openssl
                )
                break
            except ValueError as error:
                # Retry the entire download, not just parsing. Only a fresh
                # response that independently passes its CRX signature is safe
                # to mirror after an incomplete or otherwise invalid response.
                last_validation_error = error
        else:
            assert last_validation_error is not None
            raise ValueError(
                "Chrome Web Store returned an invalid CRX three times: %s"
                % last_validation_error
            ) from last_validation_error
        return ChromeWebStoreArtifact(
            extension_id,
            manifest.name,
            manifest.version,
            downloaded.contents,
            downloaded.effective_url,
            chrome_version,
            parameters,
        )


@dataclass(frozen=True)
class GitHubZipArtifact:
    source_name: str
    repository: str
    release_tag: str
    asset_name: str
    asset_url: str
    contents: bytes

    @property
    def sha256(self) -> str:
        return sha256_hex(self.contents)


class PublicGitHubReleaseClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def resolve_zip(self, source: GitHubReleaseSource) -> GitHubZipArtifact:
        api_url = "https://api.github.com/repos/%s/releases/latest" % source.repository
        release = self.http.get_json(api_url, github_api=True)
        if release.get("draft") is True or release.get("prerelease") is True:
            raise ValueError("GitHub latest endpoint returned a draft or prerelease for %s" % source.name)
        tag = release.get("tag_name")
        assets = release.get("assets")
        if not isinstance(tag, str) or not tag:
            raise ValueError("GitHub release for %s has no tag" % source.name)
        if not isinstance(assets, list):
            raise ValueError("GitHub release for %s has no asset list" % source.name)

        candidates = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            if source.asset_glob is None:
                matches = name.lower().endswith(".zip")
            else:
                matches = fnmatch.fnmatchcase(name, source.asset_glob)
            if matches:
                candidates.append((name, url))

        if len(candidates) != 1:
            selector = source.asset_glob or "the only ZIP asset"
            names = ", ".join(sorted(name for name, _ in candidates)) or "none"
            raise ValueError(
                "GitHub source %s selector %r must match exactly one asset; matched %s"
                % (source.name, selector, names)
            )
        asset_name, asset_url = candidates[0]
        downloaded = self.http.download_public_asset(asset_url)
        if not downloaded.contents.startswith(b"PK"):
            raise ValueError("GitHub asset %s is not a ZIP archive" % asset_name)
        return GitHubZipArtifact(
            source.name,
            source.repository,
            tag,
            asset_name,
            asset_url,
            downloaded.contents,
        )
