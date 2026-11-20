"""用真实 SquashFS 验证跨架构解包和版本歧义处理。"""

import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from support import elf
from wechat_mirror.appimage import extract_version, inspect_appimage, squashfs_offset
from wechat_mirror.model import ARCHITECTURES


class AppImageTests(unittest.TestCase):
    def package(self, root, architecture):
        source = root / "squashfs-root"
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
        return package

    def payload_package(self, root, architecture, members):
        source = root / "squashfs-root"
        source.mkdir()
        with tarfile.open(source / "wechat-payload.tar", "w") as archive:
            for name, content in members:
                member = tarfile.TarInfo(name)
                if isinstance(content, bytes):
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    # 安装包里的链接不能作为主程序读取，也不能解包到宿主文件系统。
                    member.type = tarfile.SYMTYPE
                    member.linkname = content
                    archive.addfile(member)
        return self.package(root, architecture)

    def test_both_architectures_are_read_without_executing_programs(self):
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture.name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "squashfs-root"
                (source / "opt/wechat").mkdir(parents=True)
                (source / "opt/wechat/wechat").write_bytes(elf(architecture))
                package = self.package(root, architecture)
                self.assertEqual(inspect_appimage(package, architecture), "4.1.13.26")
                other = next(a for a in ARCHITECTURES if a != architecture)
                with self.assertRaisesRegex(ValueError, "架构"):
                    inspect_appimage(package, other)

    def test_payload_archives_are_read_for_both_architectures(self):
        for architecture in ARCHITECTURES:
            for main_path in ("./wechat", "wechat"):
                with self.subTest(architecture=architecture.name, path=main_path):
                    with tempfile.TemporaryDirectory() as temp:
                        package = self.payload_package(Path(temp), architecture, [
                            ("./other", elf(architecture, b"4.1.13.30")),
                            (main_path, elf(architecture, b"4.1.13.29")),
                        ])
                        self.assertEqual(inspect_appimage(package, architecture), "4.1.13.29")

    def test_payload_binary_architecture_is_validated(self):
        for architecture in ARCHITECTURES:
            other = next(a for a in ARCHITECTURES if a != architecture)
            with self.subTest(architecture=architecture.name), tempfile.TemporaryDirectory() as temp:
                package = self.payload_package(Path(temp), architecture, [("./wechat", elf(other))])
                with self.assertRaisesRegex(ValueError, "架构"):
                    inspect_appimage(package, architecture)

    def test_payload_requires_one_regular_main_binary(self):
        architecture = ARCHITECTURES[0]
        cases = [
            [("./other", elf(architecture))],
            [("./wechat", elf(architecture)), ("wechat", elf(architecture))],
            [("./wechat", "other"), ("other", elf(architecture))],
            [("../wechat", elf(architecture))],
        ]
        for members in cases:
            with self.subTest(paths=[name for name, _ in members]), tempfile.TemporaryDirectory() as temp:
                package = self.payload_package(Path(temp), architecture, members)
                with self.assertRaisesRegex(ValueError, "主程序"):
                    inspect_appimage(package, architecture)

    def test_unpack_errors_include_stderr(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "squashfs-root").mkdir()
            package = self.package(root, ARCHITECTURES[0])
            with self.assertRaisesRegex(RuntimeError, "opt/wechat/wechat.*cat:"):
                inspect_appimage(package, ARCHITECTURES[0])

    def test_corrupt_payload_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "squashfs-root"
            source.mkdir()
            (source / "wechat-payload.tar").write_bytes(b"not a tar archive")
            package = self.package(root, ARCHITECTURES[0])
            with self.assertRaisesRegex(ValueError, "wechat-payload.tar 归档"):
                inspect_appimage(package, ARCHITECTURES[0])

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
