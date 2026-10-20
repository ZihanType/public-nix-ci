"""微信镜像测试使用的小型安装包和索引。"""

import hashlib
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from wechat_mirror.model import ARCHITECTURES, Artifact


REPOSITORY = "example/mirrors"


def artifact(directory, architecture=ARCHITECTURES[0], version="4.1.13.26", content=b"app"):
    path = Path(directory) / f"{architecture.name}-{version}-{hashlib.sha256(content).hexdigest()}"
    path.write_bytes(content)
    return Artifact(architecture, version, hashlib.sha256(content).hexdigest(), path)


def elf(architecture, version=b"4.1.13.26"):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, architecture.elf_machine)
    return bytes(header) + b"\x00" + version + b"\x00"
