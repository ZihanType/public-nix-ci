"""使用本地临时裸仓库验证推送竞争，不接触真实远程仓库。"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import REPOSITORY, artifact
from wechat_mirror import git_index
from wechat_mirror.model import ARCHITECTURES, INDEX_PATH, encode_index, make_index


class GitIndexTests(unittest.TestCase):
    def git(self, root, *args):
        environment = dict(os.environ)
        environment.update({
            "GIT_AUTHOR_NAME": "测试", "GIT_COMMITTER_NAME": "测试",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        })
        return subprocess.check_output(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(root), *args],
            env=environment, text=True, stderr=subprocess.PIPE,
        ).strip()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechat-git-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git(self.root, "init", "--bare", "--initial-branch=main", "remote.git")
        self.git(self.root, "clone", str(self.root / "remote.git"), "checkout")
        self.checkout = self.root / "checkout"
        (self.checkout / "README.md").write_text("测试仓库\n")
        self.git(self.checkout, "add", "README.md")
        self.git(self.checkout, "commit", "-m", "初始提交")
        self.git(self.checkout, "push", "origin", "main")
        self.original_head = self.git(self.checkout, "rev-parse", "HEAD")
        self.artifacts = [artifact(self.root, arch) for arch in ARCHITECTURES]
        self.index = make_index(self.artifacts, None, REPOSITORY)

    def remote_file(self, path):
        return self.git(self.root / "remote.git", "show", f"main:{path}")

    def test_push_adds_only_index_and_keeps_original_workspace_untouched(self):
        (self.checkout / "local.txt").write_text("不应被提交\n")
        self.assertTrue(git_index.commit_index(self.checkout, REPOSITORY, None, self.index))
        self.assertEqual(
            self.remote_file(INDEX_PATH), encode_index(self.index).decode().strip()
        )
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.original_head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "?? local.txt")
        self.assertFalse(
            git_index.commit_index(self.checkout, REPOSITORY, None, self.index)
        )
        self.assertEqual(self.git(self.root / "remote.git", "rev-list", "--count", "main"), "2")

    def test_concurrent_unrelated_commit_is_preserved_after_retry(self):
        self.git(self.root, "clone", str(self.root / "remote.git"), "other")
        other = self.root / "other"
        original_git = git_index._git
        raced = False

        def racing_git(root, *args, **kwargs):
            nonlocal raced
            if args[0] == "push" and not raced:
                raced = True
                (other / "extensions.json").write_text('{"updated": true}\n')
                self.git(other, "add", "extensions.json")
                self.git(other, "commit", "-m", "其他工作流更新")
                self.git(other, "push", "origin", "main")
            return original_git(root, *args, **kwargs)

        with patch.object(git_index, "_git", side_effect=racing_git):
            self.assertTrue(
                git_index.commit_index(self.checkout, REPOSITORY, None, self.index)
            )
        self.assertTrue(raced)
        self.assertEqual(self.remote_file("extensions.json"), '{"updated": true}')
        self.assertEqual(
            self.remote_file(INDEX_PATH), encode_index(self.index).decode().strip()
        )

    def test_concurrent_index_change_is_not_overwritten(self):
        self.git(self.root, "clone", str(self.root / "remote.git"), "other")
        other = self.root / "other"
        changed = list(self.artifacts)
        changed[0] = artifact(self.root, content=b"other publisher")
        other_index = make_index(changed, None, REPOSITORY)
        path = other / INDEX_PATH
        path.parent.mkdir(parents=True)
        path.write_bytes(encode_index(other_index))
        self.git(other, "add", str(INDEX_PATH))
        self.git(other, "commit", "-m", "其他微信索引")
        self.git(other, "push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            git_index.commit_index(self.checkout, REPOSITORY, None, self.index)
        self.assertEqual(
            self.remote_file(INDEX_PATH), encode_index(other_index).decode().strip()
        )
