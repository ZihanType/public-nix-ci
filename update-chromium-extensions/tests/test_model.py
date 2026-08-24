from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


COMPONENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT_ROOT / "scripts"))

from chromium_extensions.model import (  # noqa: E402
    Catalog,
    ChangeKind,
    LockEntry,
    diff_locks,
    parse_jsonc,
    read_lock,
    render_lock,
)


EXAMPLE_HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
# This is an independent oracle for the checked-in catalog: keep it in sync when
# intentionally adding or removing Chrome Web Store extensions.
EXPECTED_CHROME_WEB_STORE_EXTENSION_COUNT = 18


class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas_are_supported(self) -> None:
        self.assertEqual(
            parse_jsonc(
                """{
                  // line comment
                  "items": ["// is text",],
                  /* block comment */
                }"""
            ),
            {"items": ["// is text"]},
        )

    def test_unterminated_block_comment_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unterminated JSONC block comment"):
            parse_jsonc('{"items": [] /*')


class CatalogTests(unittest.TestCase):
    def test_repository_catalog_parses(self) -> None:
        catalog = Catalog.read(COMPONENT_ROOT / "extensions.jsonc")
        self.assertEqual(
            len(catalog.chrome_web_store_ids),
            EXPECTED_CHROME_WEB_STORE_EXTENSION_COUNT,
        )
        self.assertEqual(len(catalog.github_releases), 1)
        self.assertEqual(catalog.github_releases[0].name, "ublock-origin")

    def test_duplicate_store_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extensions.jsonc"
            extension_id = "a" * 32
            path.write_text(
                '{"chromeWebStore":["%s","%s"],"githubReleases":{}}'
                % (extension_id, extension_id),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                Catalog.read(path)


class LockTests(unittest.TestCase):
    def entry(
        self,
        extension_id: str,
        version: str = "1.0",
        name: str = "Example",
    ) -> LockEntry:
        return LockEntry(
            name,
            extension_id,
            version,
            "https://github.com/ZihanType/public-nix-ci/releases/download/test/file.crx",
            EXAMPLE_HASH,
        )

    def test_lock_is_rendered_in_id_order(self) -> None:
        rendered = render_lock([self.entry("b" * 32), self.entry("a" * 32)])
        self.assertLess(rendered.index('"' + "a" * 32 + '"'), rendered.index('"' + "b" * 32 + '"'))
        self.assertLess(rendered.index('"name"'), rendered.index('"id"'))

    def test_diff_order_is_add_update_remove(self) -> None:
        removed = self.entry("c" * 32)
        updated_old = self.entry("b" * 32, "1.0")
        updated_new = self.entry("b" * 32, "2.0")
        added = self.entry("a" * 32)
        changes = diff_locks([updated_old, removed], [added, updated_new])
        self.assertEqual([change.kind for change in changes], [ChangeKind.ADD, ChangeKind.UPDATE, ChangeKind.REMOVE])

    def test_same_version_mutation_is_rejected(self) -> None:
        old = self.entry("a" * 32)
        new = LockEntry(old.name, old.extension_id, old.version, old.url + "-changed", old.sha256)
        with self.assertRaisesRegex(ValueError, "without changing version"):
            diff_locks([old], [new])

    def test_same_version_name_change_is_rejected(self) -> None:
        old = self.entry("a" * 32, name="Old Name")
        new = self.entry("a" * 32, name="New Name")
        with self.assertRaisesRegex(ValueError, "without changing version"):
            diff_locks([old], [new])

    def test_legacy_lock_without_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extensions.lock"
            path.write_text(
                '[{"id":"%s","version":"1.0","url":"https://example.invalid/a.crx",'
                '"sha256":"%s"}]\n' % ("a" * 32, EXAMPLE_HASH),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly name, id, version, url, and sha256"):
                read_lock(path)

    def test_lock_name_must_already_be_normalized(self) -> None:
        with self.assertRaisesRegex(ValueError, "normalized"):
            self.entry("a" * 32, name="  Example   Extension  ")

    def test_lock_name_rejects_bidi_controls(self) -> None:
        with self.assertRaisesRegex(ValueError, "bidi formatting control"):
            self.entry("a" * 32, name="Visible\u202eHidden")

    def test_lock_name_rejects_more_than_75_characters(self) -> None:
        with self.assertRaisesRegex(ValueError, "75-character limit"):
            self.entry("a" * 32, name="a" * 76)

    def test_empty_lock_parses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extensions.lock"
            path.write_text("[]\n", encoding="utf-8")
            self.assertEqual(read_lock(path), ())

    def test_repository_lock_is_canonical(self) -> None:
        path = COMPONENT_ROOT / "extensions.lock"
        self.assertEqual(path.read_text(encoding="utf-8"), render_lock(read_lock(path)))


if __name__ == "__main__":
    unittest.main()
