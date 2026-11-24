"""Snipaste 镜像测试使用的小型安装包和索引，不访问真实远程。"""

import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from snipaste_mirror.model import TARGETS, Artifact


REPOSITORY = "example/mirrors"


def artifact(directory, target=TARGETS[0], version="2.11.3", content=b"app"):
    digest = hashlib.sha256(content).hexdigest()
    path = Path(directory) / f"{target.system}-{version}-{digest}"
    path.write_bytes(content)
    return Artifact(target, version, digest, path)
