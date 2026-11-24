"""发布索引的外部契约及版本比较。"""

import base64
import json
import tempfile
import unittest

from support import REPOSITORY, artifact
from snipaste_mirror.model import (
    TARGETS, digest_from_sri, encode_index, load_index, make_index, sri_from_digest,
)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifacts = [artifact(self.temporary.name, arch) for arch in TARGETS]

    def test_same_version_changed_bytes_get_a_new_url(self):
        old = self.artifacts[0]
        new = artifact(self.temporary.name, content=b"replaced upstream")
        self.assertNotEqual(old.tag, new.tag)
        self.assertNotEqual(old.entry(REPOSITORY)["url"], new.entry(REPOSITORY)["url"])
        self.assertEqual(old.version, new.version)

    def test_targets_can_have_different_versions(self):
        self.artifacts[1] = artifact(
            self.temporary.name, TARGETS[1], version="2.10.8"
        )
        index = make_index(self.artifacts, None, REPOSITORY)
        self.assertEqual(load_index(encode_index(index), REPOSITORY), index)
        self.assertEqual(index["aarch64-darwin"]["version"], "2.10.8")
        self.assertEqual(set(index["x86_64-linux"]), {"version", "url", "hash"})

    def test_numeric_version_comparison_rejects_downgrade(self):
        old = make_index(self.artifacts, None, REPOSITORY)
        self.artifacts[1] = artifact(
            self.temporary.name, TARGETS[1], version="2.9.9"
        )
        with self.assertRaisesRegex(ValueError, "拒绝自动降级"):
            make_index(self.artifacts, old, REPOSITORY)

    def test_digest_encoding_preserves_all_32_bytes(self):
        digest = "00" * 31 + "01"
        self.assertEqual(digest_from_sri(sri_from_digest(digest)), digest)
        with self.assertRaises(ValueError):
            digest_from_sri("sha256-" + base64.b64encode(b"too short").decode())

    def test_malformed_or_tampered_indexes_are_rejected(self):
        index = make_index(self.artifacts, None, REPOSITORY)
        for field, bad in [
            ("url", "https://github.com/example/mirrors/releases/latest/download/snipaste"),
            ("version", "2.11"),
            ("hash", "00" * 32),
        ]:
            changed = json.loads(json.dumps(index))
            changed["x86_64-linux"][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_index(encode_index(changed), REPOSITORY)
        with self.assertRaises(ValueError):
            load_index(b'{"x86_64-linux": {}, "x86_64-linux": {}}', REPOSITORY)
        with self.assertRaises(ValueError):
            make_index([self.artifacts[0]] * 2, None, REPOSITORY)
