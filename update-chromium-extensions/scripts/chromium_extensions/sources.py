"""Resolve upstream Chrome Web Store CRXs and public GitHub Release ZIPs."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import http.client
import json
import re
import time
from typing import Dict, Mapping, Optional, Tuple
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

# Socket and TLS reads may legally return less than requested without reaching
# EOF. Reading in bounded chunks both handles that behavior and preserves the
# maximum response-size guardrail for responses without Content-Length.
DOWNLOAD_READ_CHUNK_BYTES = 64 * 1024
# Chrome Web Store's CDN returns HTTP 500 for some open-ended ranges. A finite
# 4 MiB range is accepted and keeps the number of continuation requests low.
RESUME_RANGE_BYTES = 4 * 1024 * 1024
# Progressive partial responses may continue beyond the ordinary retry budget,
# but this cap prevents a server that advances by tiny fragments from looping.
# At the range size above it permits up to 128 MiB of bounded continuation.
MAX_DOWNLOAD_REQUESTS = 32
CONTENT_RANGE_RE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", re.IGNORECASE)
STRONG_ETAG_RE = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"')
RETRYABLE_DOWNLOAD_ERRORS = (
    http.client.HTTPException,
    urllib.error.URLError,
    TimeoutError,
    OSError,
)


def _content_length(headers: Mapping[str, str], url: str) -> Optional[int]:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as error:
        raise ValueError("response from %s has an invalid Content-Length" % url) from error
    if length < 0:
        raise ValueError("response from %s has a negative Content-Length" % url)
    return length


def _response_validator(headers: Mapping[str, str]) -> Optional[str]:
    etag = headers.get("ETag")
    # If-Range only accepts a strong entity-tag. Some CDNs emit an unquoted
    # ETag-like value; sending it makes the server ignore the Range request.
    if etag is not None and STRONG_ETAG_RE.fullmatch(etag) is not None:
        return etag
    return headers.get("Last-Modified")


def _read_response(
    response: object,
    maximum_bytes: int,
    expected_bytes: Optional[int],
) -> Tuple[bytes, Optional[BaseException]]:
    chunks = []
    received_bytes = 0
    read_error: Optional[BaseException] = None
    while received_bytes <= maximum_bytes:
        if expected_bytes is not None:
            expected_remaining = expected_bytes - received_bytes
            if expected_remaining == 0:
                break
        else:
            expected_remaining = maximum_bytes + 1 - received_bytes
        read_bytes = min(
            DOWNLOAD_READ_CHUNK_BYTES,
            maximum_bytes + 1 - received_bytes,
            expected_remaining,
        )
        try:
            chunk = response.read(read_bytes)  # type: ignore[attr-defined]
        except http.client.IncompleteRead as error:
            chunk = error.partial
            read_error = error
        except RETRYABLE_DOWNLOAD_ERRORS as error:
            chunk = b""
            read_error = error
        if chunk:
            chunks.append(chunk)
            received_bytes += len(chunk)
        if read_error is not None or not chunk:
            break
    return b"".join(chunks), read_error


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

        partial_contents = bytearray()
        request_url = url
        expected_total_bytes: Optional[int] = None
        resume_validator: Optional[str] = None
        last_error: Optional[BaseException] = None
        requests_made = 0
        attempts_without_progress = 0
        maximum_requests = max(self.retries, MAX_DOWNLOAD_REQUESTS)
        while (
            attempts_without_progress < self.retries
            and requests_made < maximum_requests
        ):
            requests_made += 1
            made_progress = False
            attempt_headers = dict(request_headers)
            if partial_contents:
                assert expected_total_bytes is not None
                range_start = len(partial_contents)
                range_end = min(
                    range_start + RESUME_RANGE_BYTES,
                    expected_total_bytes,
                ) - 1
                attempt_headers["Range"] = "bytes=%d-%d" % (
                    range_start,
                    range_end,
                )
                assert resume_validator is not None
                attempt_headers["If-Range"] = resume_validator
                request_origin = urllib.parse.urlsplit(request_url)[:2]
                original_origin = urllib.parse.urlsplit(url)[:2]
                if request_origin != original_origin:
                    # Never forward a credential from an API origin to a CDN
                    # when resuming a redirected public response.
                    for name in tuple(attempt_headers):
                        if name.lower() == "authorization":
                            attempt_headers.pop(name)
            request = urllib.request.Request(request_url, headers=attempt_headers)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    response_url = response.geturl()
                    response_status = response.getcode()
                    segment_bytes = _content_length(response.headers, url)
                    response_validator = _response_validator(response.headers)
                    resuming = bool(partial_contents)

                    if resuming and response_status == 206:
                        content_range = response.headers.get("Content-Range")
                        match = (
                            CONTENT_RANGE_RE.fullmatch(content_range)
                            if content_range is not None
                            else None
                        )
                        if match is None:
                            raise ValueError("range response from %s has no valid Content-Range" % url)
                        range_start, range_end, range_total = (
                            int(value) for value in match.groups()
                        )
                        if (
                            range_start != len(partial_contents)
                            or range_end < range_start
                            or range_end >= range_total
                        ):
                            raise ValueError("range response from %s starts at the wrong offset" % url)
                        if (
                            expected_total_bytes is None
                            or range_total != expected_total_bytes
                        ):
                            raise ValueError("range response from %s changed total length" % url)
                        expected_segment_bytes = range_end - range_start + 1
                        if (
                            segment_bytes is not None
                            and segment_bytes != expected_segment_bytes
                        ):
                            raise ValueError("range response from %s changed segment length" % url)
                        response_validators = {
                            response.headers.get("ETag"),
                            response.headers.get("Last-Modified"),
                        }
                        if resume_validator not in response_validators:
                            raise ValueError("range response from %s changed validator" % url)
                    elif resuming and response_status == 200:
                        # If-Range returns a full response when the object changed.
                        # Discard the old prefix rather than splicing versions.
                        partial_contents.clear()
                        resume_validator = None
                        expected_total_bytes = segment_bytes
                        expected_segment_bytes = segment_bytes
                    elif resuming:
                        raise ValueError(
                            "range response from %s returned HTTP %s"
                            % (url, response_status)
                        )
                    else:
                        if response_status == 206:
                            raise ValueError("unexpected partial response from %s" % url)
                        expected_total_bytes = segment_bytes
                        expected_segment_bytes = segment_bytes

                    if (
                        expected_total_bytes is not None
                        and expected_total_bytes > maximum_bytes
                    ):
                        raise ValueError(
                            "response from %s exceeds the %d-byte limit" % (url, maximum_bytes)
                        )

                    remaining_limit = maximum_bytes - len(partial_contents)
                    segment, read_error = _read_response(
                        response,
                        remaining_limit,
                        expected_segment_bytes,
                    )
                    if len(partial_contents) + len(segment) > maximum_bytes:
                        raise ValueError(
                            "response from %s exceeds the %d-byte limit" % (url, maximum_bytes)
                        )

                    segment_is_complete = (
                        expected_segment_bytes is None
                        or len(segment) == expected_segment_bytes
                    )
                    total_received_bytes = len(partial_contents) + len(segment)
                    response_is_complete = (
                        expected_total_bytes is None
                        or total_received_bytes == expected_total_bytes
                    )
                    if read_error is None and segment_is_complete and response_is_complete:
                        contents = bytes(partial_contents) + segment
                        return Download(
                            contents,
                            response_url,
                            response.headers.get_content_type(),
                        )

                    if expected_total_bytes is not None:
                        missing_bytes = max(
                            expected_total_bytes - total_received_bytes,
                            0,
                        )
                    else:
                        missing_bytes = max(
                            (expected_segment_bytes or len(segment)) - len(segment),
                            0,
                        )
                    incomplete_error = read_error or http.client.IncompleteRead(
                        segment, missing_bytes
                    )
                    can_resume = (
                        expected_total_bytes is not None
                        and len(partial_contents) + len(segment) < expected_total_bytes
                        and (response.headers.get("Accept-Ranges") or "").lower()
                        == "bytes"
                        and (resume_validator or response_validator) is not None
                    )
                    if can_resume:
                        partial_contents.extend(segment)
                        request_url = response_url
                        resume_validator = resume_validator or response_validator
                        made_progress = bool(segment)
                    else:
                        partial_contents.clear()
                        request_url = url
                        expected_total_bytes = None
                        resume_validator = None
                    raise incomplete_error
            except RETRYABLE_DOWNLOAD_ERRORS as error:
                last_error = error
                if made_progress:
                    attempts_without_progress = 0
                else:
                    attempts_without_progress += 1
                    if partial_contents:
                        # A CDN resume URL may expire or reject this particular
                        # range. Re-resolve the original URL instead of spending
                        # every retry on the same unusable continuation.
                        partial_contents.clear()
                        request_url = url
                        expected_total_bytes = None
                        resume_validator = None
                if (
                    attempts_without_progress < self.retries
                    and requests_made < maximum_requests
                ):
                    time.sleep(2 ** max(attempts_without_progress - 1, 0))
        assert last_error is not None
        raise RuntimeError(
            "failed to download %s after %d requests: %s"
            % (url, requests_made, last_error)
        ) from last_error

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
