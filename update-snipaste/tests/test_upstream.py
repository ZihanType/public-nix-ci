"""官网元数据的跨平台独立版本、校验清单及实际下载契约。"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from snipaste_mirror.model import TARGETS
from snipaste_mirror.upstream import (
    CHECKSUMS_URL, MAX_METADATA_SIZE, VERSIONS_URL, load_metadata, parse_metadata, resolve,
)


# 固定的小型数据模拟两个平台不同的发布节奏，不依赖官网当前版本。
VERSIONS = b'{"linux_version":"2.11.3", "mac_version":"2.10.8"}'
CHECKSUMS = (
    "# 官方清单中的其他平台不影响目标解析\r\n"
    + "a" * 40 + " *Snipaste-2.11.3-x64.zip\r\n"
    + "b" * 40 + " *Snipaste-2.11.3-x86_64.AppImage\r\n"
    + "c" * 40 + " *Snipaste-2.10.8.dmg\r\n"
).encode()


class MetadataTests(unittest.TestCase):
    def test_platform_versions_are_independent_and_other_platforms_are_ignored(self):
        metadata = parse_metadata(VERSIONS, CHECKSUMS)
        self.assertEqual(metadata.versions, {
            "x86_64-linux": "2.11.3", "aarch64-darwin": "2.10.8",
        })
        self.assertEqual(metadata.checksums, {
            "Snipaste-2.11.3-x86_64.AppImage": "b" * 40,
            "Snipaste-2.10.8.dmg": "c" * 40,
        })

    def test_missing_malformed_and_duplicate_versions_are_rejected(self):
        invalid = [
            b"[]", b"{}", b'{"mac_version":"2.11.3"}',
            b'{"linux_version":"2.11.3","linux_version":"2.10.8","mac_version":"2.10.8"}',
        ]
        for version in (None, True, "", "2.11", "v2.11.3", "2.11.3-Beta", "02.11.3"):
            invalid.append(json.dumps({
                "linux_version": version, "mac_version": "2.10.8",
            }).encode())
        for contents in invalid:
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                parse_metadata(contents, CHECKSUMS)

    def test_missing_stale_malformed_or_duplicate_sha1_is_rejected(self):
        for contents in (
            b"",
            CHECKSUMS.replace(b"2.10.8.dmg", b"2.10.7.dmg"),
            CHECKSUMS.replace(b"c" * 40, b"c" * 39),
            CHECKSUMS.replace(b"c" * 40, b"z" * 40),
            CHECKSUMS + ("d" * 40 + " *Snipaste-2.10.8.dmg\n").encode(),
        ):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                parse_metadata(VERSIONS, contents)

    def test_sha1_manifest_accepts_bom_uppercase_and_text_mode(self):
        contents = CHECKSUMS.replace(b"b" * 40, b"B" * 40).replace(b"c" * 40, b"C" * 40)
        metadata = parse_metadata(VERSIONS, b"\xef\xbb\xbf" + contents.replace(b" *", b"  "))
        self.assertEqual(metadata.checksums["Snipaste-2.10.8.dmg"], "c" * 40)

    def test_metadata_downloads_are_bounded_and_parsed_together(self):
        def get(url, destination, *, max_size):
            self.assertEqual(max_size, MAX_METADATA_SIZE)
            destination.write_bytes({VERSIONS_URL: VERSIONS, CHECKSUMS_URL: CHECKSUMS}[url])

        with tempfile.TemporaryDirectory() as directory, patch(
            "snipaste_mirror.upstream.download", side_effect=get,
        ) as download:
            metadata = load_metadata(Path(directory))
        self.assertEqual(download.call_count, 2)
        self.assertEqual(metadata.versions["aarch64-darwin"], "2.10.8")

    def test_resolver_checks_each_platform_filename_and_its_official_sha1(self):
        metadata = parse_metadata(VERSIONS, CHECKSUMS)
        with tempfile.TemporaryDirectory() as directory:
            for target, expected_name, expected_sha1 in (
                (TARGETS[0], "Snipaste-2.11.3-x86_64.AppImage", "b" * 40),
                (TARGETS[1], "Snipaste-2.10.8.dmg", "c" * 40),
            ):
                with self.subTest(system=target.system), patch(
                    "snipaste_mirror.upstream.download",
                    return_value=hashlib.sha256(b"package").hexdigest(),
                ) as download:
                    package = resolve(target, Path(directory), metadata=metadata)
                    self.assertEqual(package.asset_name, expected_name)
                    self.assertEqual(package.version, metadata.versions[target.system])
                    download.assert_called_once_with(
                        target.source_url, Path(directory) / expected_name,
                        expected_name=expected_name, expected_sha1=expected_sha1,
                    )
