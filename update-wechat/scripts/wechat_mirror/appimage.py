"""读取 AppImage 的架构及完整版本，不执行其中的程序。"""

from __future__ import annotations

import mmap
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile

from .download import download
from .model import Architecture, Artifact, version_tuple


def validate_elf(header: bytes, architecture: Architecture) -> None:
    if (
        len(header) < 64
        or header[:6] != b"\x7fELF\x02\x01"
        or struct.unpack_from("<H", header, 18)[0] != architecture.elf_machine
    ):
        raise ValueError(f"安装包中的 ELF 不是预期的 64 位 {architecture.name} 架构")


def squashfs_offset(contents) -> int:
    candidates = []
    position = contents.find(b"hsqs")
    while position != -1:
        if position + 96 <= len(contents):
            block_size = struct.unpack_from("<I", contents, position + 12)[0]
            compression, block_log = struct.unpack_from("<HH", contents, position + 20)
            major, minor = struct.unpack_from("<HH", contents, position + 28)
            size = struct.unpack_from("<Q", contents, position + 40)[0]
            if (
                (major, minor) == (4, 0)
                and compression in range(1, 7)
                and 12 <= block_log <= 20
                and block_size == 1 << block_log
                and 96 <= size <= len(contents) - position
            ):
                candidates.append(position)
        position = contents.find(b"hsqs", position + 4)
    if len(candidates) != 1:
        raise ValueError(f"无法唯一确定 AppImage 的 SquashFS 位置：{candidates}")
    return candidates[0]


def extract_version(binary, desktop: bytes) -> str:
    declared = re.findall(rb"(?m)^X-AppImage-Version=([0-9.]+)\r?$", desktop)
    if len(declared) != 1:
        raise ValueError("桌面文件缺少唯一的 X-AppImage-Version")
    base = declared[0]
    if re.fullmatch(rb"[0-9]+\.[0-9]+\.[0-9]+", base):
        pattern = rb"\x00(" + re.escape(base) + rb"\.(?:0|[1-9][0-9]*))\x00"
    elif re.fullmatch(rb"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", base):
        pattern = rb"\x00(" + re.escape(base) + rb")\x00"
    else:
        raise ValueError("桌面文件中的版本格式无法识别")
    # 使用前瞻，避免两个相邻字符串共享 NUL 分隔符时漏掉第二个版本。
    versions = {
        match.group(1).decode("ascii")
        for match in re.finditer(rb"(?=" + pattern + rb")", binary)
    }
    if len(versions) != 1:
        raise ValueError(f"无法从微信主程序确定唯一完整版本：{sorted(versions)}")
    version = versions.pop()
    version_tuple(version)
    return version


def unsquashfs(command, *, stdout=subprocess.PIPE, timeout=180):
    try:
        return subprocess.run(
            command, stdout=stdout, stderr=subprocess.PIPE, check=True, timeout=timeout,
        ).stdout
    except subprocess.CalledProcessError as error:
        details = error.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"AppImage 解包失败（退出码 {error.returncode}，命令 {command!r}）：{details}"
        ) from error


def read_payload_binary(command, binary_file) -> None:
    # 新版把主程序放入内嵌 tar；使用临时文件，避免将整个归档载入内存。
    with tempfile.TemporaryFile() as payload_file:
        unsquashfs(command + ["wechat-payload.tar"], stdout=payload_file)
        payload_file.seek(0)
        try:
            with tarfile.open(fileobj=payload_file, mode="r:") as archive:
                candidates = [
                    member for member in archive if member.name in {"wechat", "./wechat"}
                ]
                if len(candidates) != 1 or not candidates[0].isfile():
                    raise ValueError("wechat-payload.tar 中缺少唯一的普通文件主程序")
                # 只读取指定的普通文件，不执行程序、不跟随链接、不展开归档路径。
                with archive.extractfile(candidates[0]) as source:
                    shutil.copyfileobj(source, binary_file)
        except tarfile.TarError as error:
            raise ValueError(f"无法读取 wechat-payload.tar 归档：{error}") from error


def inspect_appimage(path: Path, architecture: Architecture) -> str:
    with path.open("rb") as source:
        validate_elf(source.read(64), architecture)
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            offset = squashfs_offset(contents)
    command = [
        "unsquashfs", "-no-progress", "-processors", "2", "-o", str(offset),
    ]
    files = unsquashfs(command + ["-ls", str(path.resolve())], timeout=60).splitlines()
    command += ["-cat", str(path.resolve())]
    desktop = unsquashfs(command + ["wechat.desktop"], timeout=60)
    with tempfile.TemporaryFile() as binary_file:
        # -ls 使用默认目录 squashfs-root 作为前缀；旧包没有内嵌归档。
        if b"squashfs-root/wechat-payload.tar" in files:
            read_payload_binary(command, binary_file)
        else:
            unsquashfs(command + ["opt/wechat/wechat"], stdout=binary_file)
        binary_file.seek(0)
        validate_elf(binary_file.read(64), architecture)
        with mmap.mmap(binary_file.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            return extract_version(binary, desktop)


def resolve(architecture: Architecture, directory: Path) -> Artifact:
    path = directory / f"WeChatLinux_{architecture.name}.AppImage"
    digest = download(architecture.source_url, path)
    print(f"开始解包校验 {architecture.system}……", flush=True)
    return Artifact(architecture, inspect_appimage(path, architecture), digest, path)
