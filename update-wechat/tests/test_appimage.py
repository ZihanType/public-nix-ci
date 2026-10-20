"""用真实 SquashFS 验证跨架构解包和版本歧义处理。"""

from pathlib import Path
import subprocess
import tempfile
import unittest

from support import elf
from wechat_mirror.appimage import extract_version, inspect_appimage, squashfs_offset
from wechat_mirror.model import ARCHITECTURES


class AppImageTests(unittest.TestCase):
    def test_both_architectures_are_read_without_executing_programs(self):
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture.name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "squashfs-root"
                (source / "opt/wechat").mkdir(parents=True)
                (source / "opt/wechat/wechat").write_bytes(elf(architecture))
                (source / "wechat.desktop").write_bytes(
                    b"[Desktop Entry]\nX-AppImage-Version=4.1.13\n"
                )
                squashfs = root / "image.squashfs"
                subprocess.run(
                    ["mksquashfs", str(source), str(squashfs), "-noappend",
                     "-processors", "1", "-no-progress", "-no-xattrs"],
                    check=True, capture_output=True,
                )
                package = root / "wechat.AppImage"
                # 模拟 AppImage 运行时里先出现一个不属于超级块的 hsqs 字符串。
                runtime = elf(architecture) + b"hsqs" + b"\0" * 128
                package.write_bytes(runtime + squashfs.read_bytes())
                self.assertEqual(squashfs_offset(package.read_bytes()), len(runtime))
                self.assertEqual(inspect_appimage(package, architecture), "4.1.13.26")
                other = next(a for a in ARCHITECTURES if a != architecture)
                with self.assertRaisesRegex(ValueError, "架构"):
                    inspect_appimage(package, other)

    def test_adjacent_conflicting_versions_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "唯一完整版本"):
            extract_version(
                b"\0" + b"4.1.13.23\0" + b"4.1.13.26\0",
                b"X-AppImage-Version=4.1.13\n",
            )

    def test_version_must_come_from_the_main_binary(self):
        with self.assertRaises(ValueError):
            extract_version(b"no version", b"X-AppImage-Version=4.1.13.26\n")
        self.assertEqual(
            extract_version(b"\0" + b"4.1.13.26\0", b"X-AppImage-Version=4.1.13.26\n"),
            "4.1.13.26",
        )

    def test_corrupt_image_fails_instead_of_guessing_offset(self):
        with self.assertRaises(ValueError):
            squashfs_offset(b"\x7fELF" + b"hsqs" + b"\0" * 100)
