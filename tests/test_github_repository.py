from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sys
import unittest
from typing import Optional
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from chromium_extensions.github_repository import (  # noqa: E402
    ExistingRelease,
    GitHubRepository,
    ReleaseArtifact,
    release_download_url,
)


class GitHubRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = GitHubRepository("owner/repository", "test-token")
        self.artifact = ReleaseArtifact(
            "extension-" + "a" * 32 + "-v1.0",
            "example",
            "example.crx",
            b"artifact",
            "notes",
        )

    def release(self, *, draft: bool, digest: Optional[str] = None) -> ExistingRelease:
        asset = {
            "name": self.artifact.asset_name,
            "digest": digest or "sha256:" + self.artifact.sha256_hex,
        }
        return ExistingRelease(
            1,
            self.artifact.tag,
            draft,
            False,
            not draft,
            "https://uploads.invalid",
            (asset,),
        )

    def test_matching_digest_is_accepted(self) -> None:
        self.assertTrue(
            self.repository._validate_existing_assets(  # pylint: disable=protected-access
                self.release(draft=False),
                self.artifact,
                allow_missing_draft_asset=False,
            )
        )

    def test_different_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "different"):
            self.repository._validate_existing_assets(  # pylint: disable=protected-access
                self.release(draft=False, digest="sha256:" + "0" * 64),
                self.artifact,
                allow_missing_draft_asset=False,
            )

    @mock.patch("chromium_extensions.github_repository.HttpClient")
    def test_digest_fallback_download_is_anonymous(self, http_client) -> None:
        http_client.return_value.download_public_asset.return_value.contents = b"artifact"
        contents = self.repository._download_asset(  # pylint: disable=protected-access
            {"browser_download_url": "https://github.com/owner/repository/releases/download/x/a.crx"}
        )
        self.assertEqual(contents, b"artifact")
        http_client.assert_called_once_with()

    def test_empty_draft_can_be_resumed(self) -> None:
        release = ExistingRelease(
            1,
            self.artifact.tag,
            True,
            False,
            False,
            "https://uploads.invalid",
            (),
        )
        self.assertFalse(
            self.repository._validate_existing_assets(  # pylint: disable=protected-access
                release,
                self.artifact,
                allow_missing_draft_asset=True,
            )
        )

    def test_upload_asset_returns_completed_asset_response(self) -> None:
        release = ExistingRelease(
            1,
            self.artifact.tag,
            True,
            False,
            False,
            "https://uploads.invalid{?name,label}",
            (),
        )
        response_asset = {
            "name": self.artifact.asset_name,
            "state": "uploaded",
            "digest": "sha256:" + self.artifact.sha256_hex,
        }
        with mock.patch.object(
            self.repository,
            "_request",
            return_value=(201, {}, json.dumps(response_asset).encode("utf-8")),
        ):
            uploaded_asset = self.repository._upload_asset(  # pylint: disable=protected-access
                release,
                self.artifact,
            )
        self.assertEqual(uploaded_asset, response_asset)

    def test_download_url_is_version_specific(self) -> None:
        self.assertEqual(
            release_download_url("owner/repository", "extension-id-v1.0", "id-1.0.crx"),
            "https://github.com/owner/repository/releases/download/extension-id-v1.0/id-1.0.crx",
        )

    def test_mutable_published_release_is_rejected(self) -> None:
        release = ExistingRelease(
            1,
            self.artifact.tag,
            False,
            False,
            False,
            "https://uploads.invalid",
            (),
        )
        with mock.patch.object(self.repository, "find_release", return_value=release):
            with self.assertRaisesRegex(ValueError, "not immutable"):
                self.repository.validate_existing(self.artifact)

    def test_upload_response_is_used_when_draft_list_is_stale(self) -> None:
        self_artifact = self.artifact

        class StaleDraftListRepository(GitHubRepository):
            def __init__(self) -> None:
                super().__init__("owner/repository", "test-token")
                self.draft = ExistingRelease(
                    1,
                    self_artifact.tag,
                    True,
                    False,
                    False,
                    "https://uploads.invalid",
                    (),
                )
                self.asset = {
                    "name": self_artifact.asset_name,
                    "state": "uploaded",
                    "digest": "sha256:" + self_artifact.sha256_hex,
                }

            def find_release(self, tag: str) -> Optional[ExistingRelease]:
                # GitHub's draft-list endpoint did not show the just-created
                # draft immediately after its asset upload in the failed run.
                return None

            def _create_draft(
                self, artifact: ReleaseArtifact, target_commitish: str
            ) -> ExistingRelease:
                del artifact, target_commitish
                return self.draft

            def _upload_asset(self, release: ExistingRelease, artifact: ReleaseArtifact):
                del release, artifact
                return self.asset

            def _json_request(self, method: str, url: str, **kwargs):
                if method != "PATCH" or not url.endswith("/releases/1"):
                    raise AssertionError("unexpected publish request")
                return {
                    "id": self.draft.release_id,
                    "tag_name": self.draft.tag,
                    "draft": False,
                    "prerelease": False,
                    "immutable": True,
                    "upload_url": self.draft.upload_url,
                    "assets": [self.asset],
                }

        repository = StaleDraftListRepository()
        repository.publish(self.artifact, target_commitish="main")

    def test_new_release_is_uploaded_as_draft_then_published(self) -> None:
        class InMemoryRepository(GitHubRepository):
            def __init__(self) -> None:
                super().__init__("owner/repository", "test-token")
                self.state: Optional[ExistingRelease] = None
                self.uploaded = False
                self.published = False

            def find_release(self, tag: str) -> Optional[ExistingRelease]:
                return self.state

            def _create_draft(
                self, artifact: ReleaseArtifact, target_commitish: str
            ) -> ExistingRelease:
                del target_commitish
                self.state = ExistingRelease(
                    1,
                    artifact.tag,
                    True,
                    False,
                    False,
                    "https://uploads.invalid",
                    (),
                )
                return self.state

            def _upload_asset(
                self, release: ExistingRelease, artifact: ReleaseArtifact
            ):
                self.uploaded = True
                uploaded_asset = {
                    "name": artifact.asset_name,
                    "state": "uploaded",
                    "digest": "sha256:" + hashlib.sha256(artifact.contents).hexdigest(),
                }
                self.state = ExistingRelease(
                    release.release_id,
                    release.tag,
                    True,
                    False,
                    False,
                    release.upload_url,
                    (uploaded_asset,),
                )
                return uploaded_asset

            def _json_request(self, method: str, url: str, **kwargs):
                self.assert_patch(method, url)
                assert self.state is not None
                self.published = True
                self.state = ExistingRelease(
                    self.state.release_id,
                    self.state.tag,
                    False,
                    False,
                    True,
                    self.state.upload_url,
                    self.state.assets,
                )
                return {
                    "id": self.state.release_id,
                    "tag_name": self.state.tag,
                    "draft": self.state.draft,
                    "prerelease": self.state.prerelease,
                    "immutable": self.state.immutable,
                    "upload_url": self.state.upload_url,
                    "assets": list(self.state.assets),
                }

            @staticmethod
            def assert_patch(method: str, url: str) -> None:
                if method != "PATCH" or not url.endswith("/releases/1"):
                    raise AssertionError("unexpected publish request")

        repository = InMemoryRepository()
        repository.publish(self.artifact, target_commitish="main")
        self.assertTrue(repository.uploaded)
        self.assertTrue(repository.published)
        self.assertIsNotNone(repository.state)
        assert repository.state is not None
        self.assertFalse(repository.state.draft)
        self.assertTrue(repository.state.immutable)


if __name__ == "__main__":
    unittest.main()
