from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


COMPONENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT_ROOT / "scripts"))

from chromium_extensions.model import LockEntry, diff_locks, read_lock  # noqa: E402
from chromium_extensions.github_repository import ReleaseArtifact  # noqa: E402
from chromium_extensions.pipeline import (  # noqa: E402
    GitWorkingTree,
    ResolvedArtifact,
    _commit_lock_changes,
    _release_identity,
    _validate_release_tag_collisions,
)


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
    def entry(self, name: str, extension_id: str, version: str) -> LockEntry:
        return LockEntry(
            name,
            extension_id,
            version,
            "https://github.com/owner/repository/releases/download/tag/asset.crx",
            EXAMPLE_HASH,
        )

    def test_each_extension_change_gets_one_commit_and_one_push(self) -> None:
        old = [
            self.entry("Updated Extension", "b" * 32, "1.0"),
            self.entry("Removed Extension", "c" * 32, "1.0"),
        ]
        desired = [
            self.entry("Added Extension", "a" * 32, "1.0"),
            self.entry("Updated Extension", "b" * 32, "2.0"),
        ]
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
                "chore(chromium): add Added Extension 1.0",
                "chore(chromium): update Updated Extension to 2.0",
                "chore(chromium): remove Removed Extension",
            ],
        )
        self.assertEqual(git.pushes, 1)

    def test_action_logs_use_names_and_show_renames(self) -> None:
        added = diff_locks([], [self.entry("Added Extension", "a" * 32, "1.0")])[0]
        updated = diff_locks(
            [self.entry("Updated Extension", "b" * 32, "1.0")],
            [self.entry("Updated Extension", "b" * 32, "2.0")],
        )[0]
        renamed = diff_locks(
            [self.entry("Old Name", "c" * 32, "1.0")],
            [self.entry("New Name", "c" * 32, "2.0")],
        )[0]
        removed = diff_locks([self.entry("Removed Extension", "d" * 32, "1.0")], [])[0]
        self.assertEqual(added.action_log, "Added Added Extension: 1.0")
        self.assertEqual(updated.action_log, "Updated Updated Extension: 1.0 -> 2.0")
        self.assertEqual(renamed.action_log, "Updated Old Name -> New Name: 1.0 -> 2.0")
        self.assertEqual(removed.action_log, "Removed Removed Extension: 1.0")


class ReleaseIdentityTests(unittest.TestCase):
    extension_id = "a" * 32

    def test_release_uses_name_tag_title_and_id_asset(self) -> None:
        tag, title, asset = _release_identity(
            "C/C++ DevTools Support (DWARF)", self.extension_id, "1.2.4"
        )
        self.assertEqual(tag, "extension-c-c-devtools-support-dwarf-v1.2.4")
        self.assertEqual(title, "C/C++ DevTools Support (DWARF) v1.2.4")
        self.assertEqual(asset, "%s-1.2.4.crx" % self.extension_id)

    def test_release_tag_keeps_unicode_letters(self) -> None:
        tag, _, _ = _release_identity("隐私獾", self.extension_id, "1.0")
        self.assertEqual(tag, "extension-隐私獾-v1.0")

    def test_release_tag_is_capped_at_250_utf8_bytes(self) -> None:
        tag, _, _ = _release_identity("𐐀" * 75, self.extension_id, "1.0")
        self.assertLessEqual(len(tag.encode("utf-8")), 250)
        self.assertTrue(tag.endswith("-v1.0"))
        self.assertLess(tag.count("𐐨"), 75)

    def test_release_tag_rejects_an_empty_slug(self) -> None:
        with self.assertRaisesRegex(ValueError, "slug is empty"):
            _release_identity("+++", self.extension_id, "1.0")

    def test_existing_same_version_keeps_legacy_tag(self) -> None:
        existing = LockEntry(
            "Example",
            self.extension_id,
            "1.0",
            "https://github.com/owner/repository/releases/download/"
            "extension-%s-v1.0/%s-1.0.crx" % (self.extension_id, self.extension_id),
            EXAMPLE_HASH,
        )
        tag, title, _ = _release_identity(
            "Example", self.extension_id, "1.0", existing=existing
        )
        self.assertEqual(tag, "extension-%s-v1.0" % self.extension_id)
        self.assertEqual(title, "Example v1.0")

    def test_future_version_uses_name_tag(self) -> None:
        existing = LockEntry(
            "Example",
            self.extension_id,
            "1.0",
            "https://github.com/owner/repository/releases/download/"
            "extension-%s-v1.0/%s-1.0.crx" % (self.extension_id, self.extension_id),
            EXAMPLE_HASH,
        )
        tag, _, _ = _release_identity(
            "Example", self.extension_id, "2.0", existing=existing
        )
        self.assertEqual(tag, "extension-example-v2.0")

    def test_name_tag_collision_reports_both_extensions(self) -> None:
        artifacts = []
        for name, extension_id in (("Same Name", "a" * 32), ("Same-Name", "b" * 32)):
            tag, title, asset = _release_identity(name, extension_id, "1.0")
            entry = LockEntry(
                name,
                extension_id,
                "1.0",
                "https://github.com/owner/repository/releases/download/%s/%s" % (tag, asset),
                EXAMPLE_HASH,
            )
            release = ReleaseArtifact(tag, title, asset, b"contents", "notes")
            artifacts.append(ResolvedArtifact(entry, release))
        with self.assertRaisesRegex(
            ValueError,
            r"Same Name \(%s\).*Same-Name \(%s\)" % ("a" * 32, "b" * 32),
        ):
            _validate_release_tag_collisions(artifacts)


class GitWorkingTreeTests(unittest.TestCase):
    def test_commit_file_uses_repository_relative_component_path(self) -> None:
        repository_root = Path("/repository")
        key_path = repository_root / "update-chromium-extensions" / "keys" / "example.pem"
        git = GitWorkingTree(repository_root)

        with mock.patch.object(git, "_run") as run:
            git.commit_file(key_path, "add example key")

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["add", "--", "update-chromium-extensions/keys/example.pem"]
                ),
                mock.call(
                    [
                        "commit",
                        "-m",
                        "add example key",
                        "--",
                        "update-chromium-extensions/keys/example.pem",
                    ]
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
