"""Minimal GitHub Releases API used by the publishing transaction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Mapping, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from .sources import GITHUB_API_VERSION, USER_AGENT, HttpClient


@dataclass(frozen=True)
class ReleaseArtifact:
    tag: str
    title: str
    asset_name: str
    contents: bytes
    notes: str

    @property
    def sha256_hex(self) -> str:
        return hashlib.sha256(self.contents).hexdigest()


@dataclass(frozen=True)
class ExistingRelease:
    release_id: int
    tag: str
    draft: bool
    prerelease: bool
    immutable: bool
    upload_url: str
    assets: Tuple[Mapping[str, object], ...]


class GitHubRepository:
    def __init__(self, repository: str, token: str, *, retries: int = 3) -> None:
        if "/" not in repository or not token:
            raise ValueError("GitHub repository and token are required")
        self.repository = repository
        self.token = token
        self.retries = retries
        self.api_root = "https://api.github.com/repos/%s" % repository

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Mapping[str, object]] = None,
        raw_body: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        allow_not_found: bool = False,
    ) -> Tuple[int, Mapping[str, str], bytes]:
        request_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if headers:
            request_headers.update(headers)
        if json_body is not None and raw_body is not None:
            raise ValueError("GitHub request cannot have both JSON and raw bodies")
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        last_error: Optional[BaseException] = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, dict(response.headers.items()), response.read()
            except urllib.error.HTTPError as error:
                response_body = error.read()
                if allow_not_found and error.code == 404:
                    return error.code, dict(error.headers.items()), response_body
                if error.code not in {429, 500, 502, 503, 504} or attempt + 1 >= self.retries:
                    message = response_body.decode("utf-8", errors="replace")
                    raise RuntimeError(
                        "GitHub API %s %s failed with HTTP %d: %s"
                        % (method, url, error.code, message)
                    ) from error
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt + 1 >= self.retries:
                    break
            time.sleep(2**attempt)
        assert last_error is not None
        raise RuntimeError("GitHub API %s %s failed: %s" % (method, url, last_error)) from last_error

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[Mapping[str, object]] = None,
        allow_not_found: bool = False,
    ) -> Optional[Mapping[str, object]]:
        status, _, contents = self._request(
            method,
            url,
            json_body=body,
            allow_not_found=allow_not_found,
        )
        if status == 404 and allow_not_found:
            return None
        if not contents:
            return {}
        try:
            value = json.loads(contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("GitHub API returned invalid JSON from %s" % url) from error
        if not isinstance(value, dict):
            raise RuntimeError("GitHub API returned a non-object from %s" % url)
        return value

    @staticmethod
    def _parse_release(value: Mapping[str, object]) -> ExistingRelease:
        release_id = value.get("id")
        tag = value.get("tag_name")
        draft = value.get("draft")
        prerelease = value.get("prerelease")
        immutable = value.get("immutable")
        upload_url = value.get("upload_url")
        assets = value.get("assets")
        if (
            not isinstance(release_id, int)
            or not isinstance(tag, str)
            or not isinstance(draft, bool)
            or not isinstance(prerelease, bool)
            or not isinstance(immutable, bool)
            or not isinstance(upload_url, str)
            or not isinstance(assets, list)
            or not all(isinstance(asset, dict) for asset in assets)
        ):
            raise RuntimeError("GitHub returned a malformed release object for tag %r" % tag)
        return ExistingRelease(
            release_id,
            tag,
            draft,
            prerelease,
            immutable,
            upload_url,
            tuple(assets),
        )

    @staticmethod
    def _require_published_immutable(release: ExistingRelease) -> None:
        # GITHUB_TOKEN cannot read the repository's Administration setting.
        # The release object is available with Contents permission and is the
        # authoritative check for the property that protects this artifact.
        if not release.draft and not release.immutable:
            raise ValueError("published release %s is not immutable" % release.tag)

    def find_release(self, tag: str) -> Optional[ExistingRelease]:
        encoded_tag = urllib.parse.quote(tag, safe="")
        published = self._json_request(
            "GET",
            self.api_root + "/releases/tags/" + encoded_tag,
            allow_not_found=True,
        )
        if published is not None:
            return self._parse_release(published)

        # The tag endpoint omits draft releases. List drafts so a failed upload
        # can be resumed without deleting external state.
        page = 1
        while True:
            status, _, contents = self._request(
                "GET",
                self.api_root + "/releases?per_page=100&page=%d" % page,
            )
            del status
            try:
                values = json.loads(contents.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("GitHub returned an invalid release list") from error
            if not isinstance(values, list):
                raise RuntimeError("GitHub returned a non-array release list")
            for value in values:
                if isinstance(value, dict) and value.get("tag_name") == tag:
                    return self._parse_release(value)
            if len(values) < 100:
                return None
            page += 1

    def _download_asset(self, asset: Mapping[str, object]) -> bytes:
        browser_url = asset.get("browser_download_url")
        if not isinstance(browser_url, str):
            raise RuntimeError("GitHub release asset has no public download URL")
        # This repository is public. Downloading from browser_download_url with
        # an anonymous client avoids forwarding GITHUB_TOKEN if GitHub redirects
        # the asset request to a separate CDN host.
        return HttpClient().download_public_asset(browser_url).contents

    def _validate_existing_assets(
        self,
        release: ExistingRelease,
        artifact: ReleaseArtifact,
        *,
        allow_missing_draft_asset: bool,
    ) -> bool:
        matching = [asset for asset in release.assets if asset.get("name") == artifact.asset_name]
        unexpected = [asset for asset in release.assets if asset.get("name") != artifact.asset_name]
        if unexpected:
            names = ", ".join(str(asset.get("name")) for asset in unexpected)
            raise ValueError("release %s has unexpected assets: %s" % (artifact.tag, names))
        if not matching:
            if release.draft and allow_missing_draft_asset:
                return False
            raise ValueError("release %s is missing asset %s" % (artifact.tag, artifact.asset_name))
        if len(matching) != 1:
            raise ValueError("release %s has duplicate asset %s" % (artifact.tag, artifact.asset_name))
        asset = matching[0]
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:"):
            actual_sha256 = digest[len("sha256:") :]
        else:
            actual_sha256 = hashlib.sha256(self._download_asset(asset)).hexdigest()
        if actual_sha256 != artifact.sha256_hex:
            raise ValueError(
                "immutable release %s contains a different %s"
                % (artifact.tag, artifact.asset_name)
            )
        return True

    def validate_existing(self, artifact: ReleaseArtifact) -> None:
        release = self.find_release(artifact.tag)
        if release is None:
            return
        if release.prerelease:
            raise ValueError("release %s unexpectedly is a prerelease" % artifact.tag)
        self._require_published_immutable(release)
        self._validate_existing_assets(release, artifact, allow_missing_draft_asset=True)

    def _create_draft(self, artifact: ReleaseArtifact, target_commitish: str) -> ExistingRelease:
        value = self._json_request(
            "POST",
            self.api_root + "/releases",
            body={
                "tag_name": artifact.tag,
                "target_commitish": target_commitish,
                "name": artifact.title,
                "body": artifact.notes,
                "draft": True,
                "prerelease": False,
            },
        )
        assert value is not None
        return self._parse_release(value)

    def _upload_asset(self, release: ExistingRelease, artifact: ReleaseArtifact) -> None:
        upload_root = release.upload_url.split("{", 1)[0]
        upload_url = upload_root + "?" + urllib.parse.urlencode({"name": artifact.asset_name})
        self._request(
            "POST",
            upload_url,
            raw_body=artifact.contents,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/x-chrome-extension",
            },
        )

    def publish(self, artifact: ReleaseArtifact, *, target_commitish: str) -> None:
        release = self.find_release(artifact.tag)
        if release is None:
            release = self._create_draft(artifact, target_commitish)
        if release.prerelease:
            raise ValueError("release %s unexpectedly is a prerelease" % artifact.tag)
        self._require_published_immutable(release)

        has_asset = self._validate_existing_assets(
            release,
            artifact,
            allow_missing_draft_asset=True,
        )
        if not has_asset:
            if not release.draft:
                raise ValueError("published release %s is missing its immutable asset" % artifact.tag)
            self._upload_asset(release, artifact)
            refreshed = self.find_release(artifact.tag)
            if refreshed is None or not self._validate_existing_assets(
                refreshed,
                artifact,
                allow_missing_draft_asset=False,
            ):
                raise RuntimeError("uploaded asset verification failed for %s" % artifact.tag)
            release = refreshed

        if release.draft:
            updated = self._json_request(
                "PATCH",
                self.api_root + "/releases/%d" % release.release_id,
                body={
                    "name": artifact.title,
                    "body": artifact.notes,
                    "draft": False,
                    "prerelease": False,
                },
            )
            assert updated is not None
            published = self._parse_release(updated)
            if published.draft:
                raise RuntimeError("GitHub did not publish release %s" % artifact.tag)
            if not published.immutable:
                raise RuntimeError("published release %s is not immutable" % artifact.tag)


def release_download_url(repository: str, tag: str, asset_name: str) -> str:
    return "https://github.com/%s/releases/download/%s/%s" % (
        repository,
        urllib.parse.quote(tag, safe=""),
        urllib.parse.quote(asset_name, safe=""),
    )
