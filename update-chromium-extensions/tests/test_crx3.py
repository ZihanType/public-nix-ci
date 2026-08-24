from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile


COMPONENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT_ROOT / "scripts"))

from chromium_extensions.crx3 import (  # noqa: E402
    CANONICAL_ZIP_TIMESTAMP,
    build_reproducible_crx3,
    canonicalize_extension_zip,
    extension_manifest_from_crx3,
    generate_rsa_private_key,
    manifest_from_crx3,
    parse_crx3,
)


def make_source_zip(
    *,
    manifest_path: str = "extension/manifest.json",
    manifest: dict[str, object] | None = None,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    manifest = manifest or {"manifest_version": 2, "version": "1.2.3", "name": "Example"}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(manifest_path, json.dumps(manifest))
        archive.writestr("extension/background.js", "console.log('example');\n")
        archive.writestr("upstream-checksum.txt", "not part of the extension\n")
        for path, contents in (extra_files or {}).items():
            archive.writestr(path, contents)
    return output.getvalue()


class CanonicalZipTests(unittest.TestCase):
    def test_wrapper_directory_is_removed_and_metadata_is_fixed(self) -> None:
        canonical = canonicalize_extension_zip(make_source_zip())
        self.assertEqual(canonical.name, "Example")
        self.assertEqual(canonical.version, "1.2.3")
        self.assertEqual(canonical.source_root, "extension")
        with zipfile.ZipFile(io.BytesIO(canonical.zip_bytes)) as archive:
            self.assertEqual(archive.namelist(), ["background.js", "manifest.json"])
            for info in archive.infolist():
                self.assertEqual(info.date_time, CANONICAL_ZIP_TIMESTAMP)
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual((info.external_attr >> 16) & 0o777, 0o644)

    def test_default_locale_name_is_resolved_case_insensitively(self) -> None:
        canonical = canonicalize_extension_zip(
            make_source_zip(
                manifest={
                    "manifest_version": 3,
                    "version": "1.2.3",
                    "name": "  __MSG_ExtName__   Edition  ",
                    "default_locale": "en",
                },
                extra_files={
                    "extension/_locales/en/messages.json": json.dumps(
                        {"extname": {"message": "Localized   Name"}}
                    )
                },
            )
        )
        self.assertEqual(canonical.name, "Localized Name Edition")

    def test_missing_default_locale_message_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "has no message"):
            canonicalize_extension_zip(
                make_source_zip(
                    manifest={
                        "manifest_version": 3,
                        "version": "1.2.3",
                        "name": "__MSG_ExtName__",
                        "default_locale": "en",
                    },
                    extra_files={"extension/_locales/en/messages.json": "{}"},
                )
            )

    def test_last_case_insensitive_message_key_matches_chromium(self) -> None:
        canonical = canonicalize_extension_zip(
            make_source_zip(
                manifest={
                    "version": "1.2.3",
                    "name": "__MSG_ExtName__",
                    "default_locale": "en",
                },
                extra_files={
                    "extension/_locales/en/messages.json": json.dumps(
                        {
                            "ExtName": {"message": "Earlier Name"},
                            "extname": {"message": "Later Name"},
                        }
                    )
                },
            )
        )
        self.assertEqual(canonical.name, "Later Name")

    def test_localized_name_expands_message_placeholders(self) -> None:
        canonical = canonicalize_extension_zip(
            make_source_zip(
                manifest={
                    "version": "1.2.3",
                    "name": "__MSG_ExtName__",
                    "default_locale": "en",
                },
                extra_files={
                    "extension/_locales/en/messages.json": json.dumps(
                        {
                            "ExtName": {
                                "message": "Example $Product$",
                                "placeholders": {
                                    "product": {"content": "Extension"}
                                },
                            }
                        }
                    )
                },
            )
        )
        self.assertEqual(canonical.name, "Example Extension")

    def test_control_character_in_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "control character"):
            canonicalize_extension_zip(
                make_source_zip(
                    manifest={"version": "1.2.3", "name": "Unsafe\nName"}
                )
            )

    def test_same_input_produces_identical_zip(self) -> None:
        source = make_source_zip()
        self.assertEqual(
            canonicalize_extension_zip(source).zip_bytes,
            canonicalize_extension_zip(source).zip_bytes,
        )

    def test_path_traversal_is_rejected(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("../manifest.json", '{"version":"1"}')
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            canonicalize_extension_zip(output.getvalue())

    def test_symbolic_link_is_rejected(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            manifest = zipfile.ZipInfo("manifest.json")
            archive.writestr(manifest, '{"version":"1"}')
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "target")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            canonicalize_extension_zip(output.getvalue())


class Crx3Tests(unittest.TestCase):
    def test_build_verify_and_manifest_round_trip(self) -> None:
        canonical = canonicalize_extension_zip(make_source_zip())
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "example.pem"
            generate_rsa_private_key(key_path)
            built = build_reproducible_crx3(canonical.zip_bytes, key_path)
        parsed = parse_crx3(built.contents)
        self.assertEqual(parsed.extension_id, built.extension_id)
        self.assertEqual(parsed.zip_bytes, canonical.zip_bytes)
        metadata = extension_manifest_from_crx3(built.contents)
        self.assertEqual(metadata.name, "Example")
        self.assertEqual(metadata.version, "1.2.3")
        self.assertEqual(manifest_from_crx3(built.contents)["version"], "1.2.3")

    def test_payload_mutation_invalidates_signature(self) -> None:
        canonical = canonicalize_extension_zip(make_source_zip())
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "example.pem"
            generate_rsa_private_key(key_path)
            built = build_reproducible_crx3(canonical.zip_bytes, key_path)
        mutated = built.contents[:-1] + bytes([built.contents[-1] ^ 0x01])
        with self.assertRaisesRegex(ValueError, "no valid signature"):
            manifest_from_crx3(mutated)


if __name__ == "__main__":
    unittest.main()
