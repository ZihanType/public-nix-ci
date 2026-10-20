"""不可变 Release、草稿恢复和流式上传的行为测试。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import REPOSITORY, artifact
from wechat_mirror.download import CHUNK_SIZE
from wechat_mirror.github import GitHub


def asset_for(package, *, draft=False):
    url = package.entry(REPOSITORY)["url"]
    if draft:
        # GitHub 实际会为尚未发布的草稿返回临时 untagged 下载地址。
        url = (
            f"https://github.com/{REPOSITORY}/releases/download/"
            f"untagged-0c1a1a928b3119228429/{package.asset_name}"
        )
    return {
        "id": 11, "name": package.asset_name, "state": "uploaded",
        "size": package.path.stat().st_size, "digest": "sha256:" + package.digest,
        "browser_download_url": url,
    }


def release_for(package, *, draft=False, assets=True):
    return {
        "id": 1, "tag_name": package.tag, "draft": draft,
        "prerelease": False, "immutable": not draft,
        "assets": [asset_for(package, draft=draft)] if assets else [],
    }


class FakeGitHub(GitHub):
    """保存远程状态，用于模拟上传成功但客户端未收到响应。"""

    def __init__(self, package, release=None):
        super().__init__(REPOSITORY, "test-token")
        self.package = package
        self.release = copy.deepcopy(release)
        self.created = 0
        self.uploaded = 0
        self.published = 0
        self.lose_upload_response = False
        self.immutable = True

    def _request(self, method, endpoint, body=None, *, missing=False):
        if method == "GET":
            if endpoint.startswith("/releases/tags/"):
                if self.release and not self.release["draft"]:
                    return copy.deepcopy(self.release)
                return None
            return [copy.deepcopy(self.release)] if self.release else []
        if method == "POST":
            if body["make_latest"] != "false":
                raise AssertionError("微信发布不应抢占仓库最新 Release")
            self.created += 1
            self.release = release_for(self.package, draft=True, assets=False)
        elif method == "PATCH":
            if body["make_latest"] != "false":
                raise AssertionError("微信发布不应抢占仓库最新 Release")
            self.published += 1
            self.release["draft"] = False
            self.release["immutable"] = self.immutable
            for asset in self.release["assets"]:
                asset["browser_download_url"] = self.package.entry(REPOSITORY)["url"]
        elif method == "DELETE":
            if not self.release["draft"] or self.release["assets"][0]["state"] != "starter":
                raise AssertionError("只能清理未发布的失败占位资产")
            self.release["assets"] = []
        return copy.deepcopy(self.release)

    def _upload(self, release, package):
        self.uploaded += 1
        asset = asset_for(package, draft=True)
        self.release["assets"] = [asset]
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise OSError("上传成功后连接中断")
        return asset


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.package = artifact(self.temporary.name)

    def test_new_release_is_uploaded_before_publication(self):
        github = FakeGitHub(self.package)
        github.publish(self.package, "a" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (1, 1, 1))
        self.assertTrue(github.release["immutable"])
        github.publish(self.package, "b" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (1, 1, 1))

    def test_empty_draft_is_resumed_without_a_second_release(self):
        github = FakeGitHub(
            self.package, release_for(self.package, draft=True, assets=False)
        )
        github.publish(self.package, "a" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (0, 1, 1))

    def test_uploaded_draft_with_temporary_url_is_reused_and_published(self):
        github = FakeGitHub(self.package, release_for(self.package, draft=True))
        github.preflight(self.package)
        github.publish(self.package, "a" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (0, 0, 1))
        self.assertEqual(
            github.release["assets"][0]["browser_download_url"],
            self.package.entry(REPOSITORY)["url"],
        )

    def test_published_asset_must_have_the_final_versioned_url(self):
        for url in (
            asset_for(self.package, draft=True)["browser_download_url"],
            "https://github.com/example/mirrors/releases/latest/download/wechat",
        ):
            release = release_for(self.package)
            release["assets"][0]["browser_download_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                FakeGitHub(self.package, release).preflight(self.package)

    def test_draft_temporary_url_does_not_relax_content_checks(self):
        for field, value in (
            ("name", "other.AppImage"),
            ("size", self.package.path.stat().st_size + 1),
            ("state", "starter"),
            ("digest", "sha256:" + "0" * 64),
        ):
            release = release_for(self.package, draft=True)
            release["assets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                FakeGitHub(self.package, release).preflight(self.package)

    def test_lost_upload_response_reuses_the_uploaded_asset_on_retry(self):
        github = FakeGitHub(self.package)
        github.lose_upload_response = True
        with self.assertRaises(OSError):
            github.publish(self.package, "a" * 40)
        self.assertTrue(github.release["draft"])
        github.publish(self.package, "a" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (1, 1, 1))

    def test_failed_upload_placeholder_can_be_replaced_only_in_draft(self):
        release = release_for(self.package, draft=True)
        release["assets"][0].update(state="starter", size=0, digest=None)
        github = FakeGitHub(self.package, release)
        github.preflight(self.package)
        github.publish(self.package, "a" * 40)
        self.assertEqual((github.created, github.uploaded, github.published), (0, 1, 1))
        release.update(draft=False, immutable=True)
        with self.assertRaises(ValueError):
            FakeGitHub(self.package, release).preflight(self.package)

    def test_conflicting_asset_is_never_overwritten(self):
        release = release_for(self.package)
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
        github = FakeGitHub(self.package, release)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            github.preflight(self.package)
        with self.assertRaises(ValueError):
            github.publish(self.package, "a" * 40)
        self.assertEqual(github.uploaded, 0)

    def test_mutable_release_and_extra_assets_are_rejected(self):
        for mutation in ("mutable", "extra"):
            release = release_for(self.package)
            if mutation == "mutable":
                release["immutable"] = False
            else:
                release["assets"].append(asset_for(self.package))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                FakeGitHub(self.package, release).preflight(self.package)

    def test_repository_without_immutability_cannot_finish_publication(self):
        github = FakeGitHub(self.package)
        github.immutable = False
        with self.assertRaisesRegex(ValueError, "不可变"):
            github.publish(self.package, "a" * 40)

    def test_draft_lookup_is_paginated(self):
        github = GitHub(REPOSITORY, "test-token")
        draft = release_for(self.package, draft=True)
        with patch.object(
            github, "_request",
            side_effect=[None, [{"tag_name": f"other-{n}"} for n in range(100)], [draft]],
        ) as request:
            self.assertEqual(github.find_release(self.package), draft)
            self.assertIn("page=2", request.call_args.args[1])

    def test_missing_server_digest_is_checked_by_downloading_asset(self):
        github = GitHub(REPOSITORY, "test-token")
        asset = asset_for(self.package)
        asset["digest"] = None
        with patch("wechat_mirror.github.download", return_value=self.package.digest) as get:
            github._verify_asset(asset, self.package, draft=False)
            self.assertTrue(get.call_args.args[0].endswith("/releases/assets/11"))
            self.assertEqual(
                get.call_args.kwargs["headers"]["Accept"], "application/octet-stream"
            )

    def test_large_upload_uses_bounded_chunks_and_verifies_response(self):
        package = artifact(self.temporary.name, content=b"x" * (2 * CHUNK_SIZE + 9))
        connection = MagicMock()
        connection.getresponse.return_value.status = 201
        connection.getresponse.return_value.read.return_value = json.dumps(
            asset_for(package, draft=True)
        ).encode()
        github = GitHub(REPOSITORY, "test-token")
        with patch("wechat_mirror.github.http.client.HTTPSConnection", return_value=connection):
            github._upload(release_for(package, draft=True, assets=False), package)
        chunks = [call.args[0] for call in connection.send.call_args_list]
        self.assertEqual([len(chunk) for chunk in chunks], [CHUNK_SIZE, CHUNK_SIZE, 9])
        self.assertEqual(b"".join(chunks), package.path.read_bytes())
        connection.close.assert_called_once()
