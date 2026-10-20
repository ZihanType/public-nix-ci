"""验证双架构发布的副作用顺序和重跑行为。"""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import REPOSITORY, artifact
from wechat_mirror.model import ARCHITECTURES, make_index
from wechat_mirror.pipeline import main, update


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = [artifact(self.root, arch) for arch in ARCHITECTURES]
        self.resolver = Mock(side_effect=self.artifacts)
        self.publisher = Mock()
        self.publisher.preflight.return_value = {"draft": False}
        self.save = Mock()
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def run_update(self, previous=None, publisher=True):
        return update(
            previous, REPOSITORY, self.root, self.resolver,
            self.publisher if publisher else None, self.save, "a" * 40,
        )

    def test_second_architecture_download_failure_has_no_publication(self):
        self.resolver.side_effect = [self.artifacts[0], ValueError("下载失败")]
        with self.assertRaises(ValueError):
            self.run_update()
        self.publisher.preflight.assert_not_called()
        self.publisher.publish.assert_not_called()
        self.save.assert_not_called()

    def test_second_release_conflict_prevents_all_publication(self):
        self.publisher.preflight.side_effect = [None, ValueError("内容冲突")]
        with self.assertRaises(ValueError):
            self.run_update()
        self.publisher.publish.assert_not_called()
        self.save.assert_not_called()

    def test_publication_failure_does_not_update_index_and_retry_completes(self):
        self.publisher.publish.side_effect = [None, RuntimeError("上传中断")]
        with self.assertRaises(RuntimeError):
            self.run_update()
        self.save.assert_not_called()
        self.resolver.side_effect = self.artifacts
        self.publisher.publish.side_effect = None
        index = self.run_update()
        self.save.assert_called_once_with(index)

    def test_unchanged_index_does_not_publish_or_commit(self):
        previous = make_index(self.artifacts, None, REPOSITORY)
        self.assertEqual(self.run_update(previous), previous)
        self.assertEqual(self.publisher.preflight.call_count, 2)
        self.publisher.publish.assert_not_called()
        self.save.assert_not_called()

    def test_missing_release_referenced_by_old_index_is_an_error(self):
        previous = make_index(self.artifacts, None, REPOSITORY)
        self.publisher.preflight.return_value = None
        with self.assertRaises(ValueError):
            self.run_update(previous)
        self.save.assert_not_called()

    def test_dry_run_cannot_publish_or_save(self):
        result = self.run_update(publisher=False)
        self.assertEqual(set(result), {arch.system for arch in ARCHITECTURES})
        self.publisher.preflight.assert_not_called()
        self.publisher.publish.assert_not_called()
        self.save.assert_not_called()

    def test_replaced_package_at_same_version_is_published(self):
        previous = make_index(self.artifacts, None, REPOSITORY)
        self.artifacts[0] = artifact(self.root, content=b"replacement")
        self.resolver.side_effect = self.artifacts
        result = self.run_update(previous)
        self.assertNotEqual(result["x86_64-linux"]["url"], previous["x86_64-linux"]["url"])
        self.assertEqual(result["aarch64-linux"], previous["aarch64-linux"])
        self.save.assert_called_once_with(result)

    def test_cli_dry_run_needs_no_token_and_does_not_write_index(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("wechat_mirror.pipeline.read_index", return_value=None),
            patch("wechat_mirror.pipeline.resolve", side_effect=self.artifacts),
            patch("wechat_mirror.pipeline.subprocess.check_output", return_value="a" * 40),
            patch("wechat_mirror.pipeline.GitHub") as github,
            patch("wechat_mirror.pipeline.commit_index") as commit,
        ):
            self.assertEqual(main(["--dry-run", "--repository", REPOSITORY]), 0)
            github.assert_not_called()
            commit.assert_not_called()
