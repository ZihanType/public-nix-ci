from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from chromium_extensions.model import LockEntry, diff_locks, read_lock  # noqa: E402
from chromium_extensions.pipeline import _commit_lock_changes  # noqa: E402


EXAMPLE_HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class FakeGit:
    def __init__(self) -> None:
        self.commits = []
        self.pushes = 0

    def commit_file(self, path: Path, message: str) -> None:
        self.commits.append((path.read_text(encoding="utf-8"), message))

    def push(self) -> None:
        self.pushes += 1


class PipelineCommitTests(unittest.TestCase):
    def entry(self, extension_id: str, version: str) -> LockEntry:
        return LockEntry(
            extension_id,
            version,
            "https://github.com/owner/repository/releases/download/tag/asset.crx",
            EXAMPLE_HASH,
        )

    def test_each_extension_change_gets_one_commit_and_one_push(self) -> None:
        old = [self.entry("b" * 32, "1.0"), self.entry("c" * 32, "1.0")]
        desired = [self.entry("a" * 32, "1.0"), self.entry("b" * 32, "2.0")]
        changes = diff_locks(old, desired)
        git = FakeGit()
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "extensions.lock"
            lock_path.write_text("[]\n", encoding="utf-8")
            _commit_lock_changes(lock_path, old, desired, changes, git)  # type: ignore[arg-type]
            self.assertEqual(read_lock(lock_path), tuple(desired))
        self.assertEqual(
            [message for _, message in git.commits],
            [
                "chore(extensions): add %s at 1.0" % ("a" * 32),
                "chore(extensions): update %s from 1.0 to 2.0" % ("b" * 32),
                "chore(extensions): remove %s at 1.0" % ("c" * 32),
            ],
        )
        self.assertEqual(git.pushes, 1)


if __name__ == "__main__":
    unittest.main()
