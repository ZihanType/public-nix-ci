"""根据官网各平台的版本、当前下载入口和 SHA-1 清单解析官方安装包。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .download import download
from .model import TARGETS, Artifact, Target, unique_object, version_tuple


# 这两个元数据入口由官网下载页及其 js/index.js 使用。
VERSIONS_URL = "https://www.snipaste.com/versions"
CHECKSUMS_URL = "https://dl.snipaste.com/sha-1.txt"
# 官网小型元数据各限制为 1 MiB，避免将异常响应作为元数据加载进内存。
MAX_METADATA_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ReleaseMetadata:
    versions: dict[str, str]
    checksums: dict[str, str]


def parse_metadata(versions_contents: bytes, checksums_contents: bytes) -> ReleaseMetadata:
    value = json.loads(versions_contents, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("官网版本信息必须是 JSON 对象")
    versions = {}
    wanted = set()
    for target in TARGETS:
        version = value.get(target.version_field)
        version_tuple(version)
        versions[target.system] = version
        wanted.add(target.filename(version))

    checksums = {}
    for line in checksums_contents.decode("utf-8-sig").splitlines():
        # 官方清单采用 GNU 校验清单格式：40 位十六进制摘要、空格、*、文件名。
        match = re.fullmatch(r"([0-9a-fA-F]{40}) [ *](.+)", line)
        if match is None or match[2] not in wanted:
            continue
        name = match[2]
        if name in checksums:
            raise ValueError(f"官方 SHA-1 清单包含重复安装包：{name}")
        checksums[name] = match[1].lower()
    if set(checksums) != wanted:
        raise ValueError(f"官方 SHA-1 清单缺少当前安装包：{sorted(wanted - checksums.keys())}")
    return ReleaseMetadata(versions, checksums)


def load_metadata(directory: Path) -> ReleaseMetadata:
    versions = directory / "versions.json"
    checksums = directory / "sha-1.txt"
    download(VERSIONS_URL, versions, max_size=MAX_METADATA_SIZE)
    download(CHECKSUMS_URL, checksums, max_size=MAX_METADATA_SIZE)
    return parse_metadata(versions.read_bytes(), checksums.read_bytes())


def resolve(target: Target, directory: Path, *, metadata: ReleaseMetadata) -> Artifact:
    version = metadata.versions[target.system]
    name = target.filename(version)
    path = directory / name
    digest = download(
        target.source_url, path, expected_name=name,
        expected_sha1=metadata.checksums[name],
    )
    return Artifact(target, version, digest, path)
