"""Snipaste 镜像修订、目标安装包和发布索引的外部契约。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
from pathlib import Path
import re


INDEX_PATH = Path("update-snipaste/snipaste.json")
# 官网当前使用三段数字版本；未知格式交由维护者确认，避免错误比较或降级。
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"


@dataclass(frozen=True)
class Target:
    system: str
    version_field: str
    source_url: str
    filename_template: str

    def filename(self, version: str) -> str:
        version_tuple(version)
        return self.filename_template.format(version=version)


# 系统范围取自用户指定的 nixpkgs sources.nix，下载入口和文件名来自官网。
TARGETS = (
    Target(
        "x86_64-linux", "linux_version", "https://dl.snipaste.com/linux",
        "Snipaste-{version}-x86_64.AppImage",
    ),
    Target(
        "aarch64-darwin", "mac_version", "https://dl.snipaste.com/mac",
        "Snipaste-{version}.dmg",
    ),
)


def check_repository(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("仓库必须使用 owner/repository 格式")
    return repository


def version_tuple(version: str) -> tuple[int, ...]:
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError(f"无效的 Snipaste 版本：{version!r}")
    return tuple(int(part) for part in version.split("."))


def digest_from_sri(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256-"):
        raise ValueError("hash 必须使用 SHA-256 SRI 格式")
    try:
        digest = base64.b64decode(value[7:], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("无效的 SHA-256 Base64 编码") from error
    if len(digest) != 32 or sri_from_digest(digest.hex()) != value:
        raise ValueError("SHA-256 必须是规范编码的 32 字节摘要")
    return digest.hex()


def sri_from_digest(digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("SHA-256 十六进制摘要必须包含 64 个小写字符")
    return "sha256-" + base64.b64encode(bytes.fromhex(digest)).decode("ascii")


@dataclass(frozen=True)
class Artifact:
    target: Target
    version: str
    digest: str
    path: Path

    def __post_init__(self) -> None:
        version_tuple(self.version)
        sri_from_digest(self.digest)
        if self.target not in TARGETS:
            raise ValueError("不支持的 Snipaste 目标")

    @property
    def tag(self) -> str:
        return f"snipaste-{self.version}-{self.target.system}-{self.digest}"

    @property
    def asset_name(self) -> str:
        return self.target.filename(self.version)

    def entry(self, repository: str) -> dict[str, str]:
        check_repository(repository)
        return {
            "version": self.version,
            "url": (
                f"https://github.com/{repository}/releases/download/"
                f"{self.tag}/{self.asset_name}"
            ),
            "hash": sri_from_digest(self.digest),
        }


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def load_index(contents: bytes | None, repository: str) -> dict | None:
    if contents is None:
        return None
    value = json.loads(contents, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {t.system for t in TARGETS}:
        raise ValueError("发布索引必须且只能包含 aarch64-darwin 和 x86_64-linux")
    for target in TARGETS:
        entry = value[target.system]
        if not isinstance(entry, dict) or set(entry) != {"version", "url", "hash"}:
            raise ValueError(f"{target.system} 必须仅包含 version、url、hash")
        artifact = Artifact(
            target, entry["version"], digest_from_sri(entry["hash"]), Path(".")
        )
        if entry != artifact.entry(repository):
            raise ValueError(f"{target.system} 的下载地址与版本、目标或 SHA-256 不一致")
    return value


def make_index(artifacts: list[Artifact], previous: dict | None, repository: str) -> dict:
    if len(artifacts) != len(TARGETS) or {a.target for a in artifacts} != set(TARGETS):
        raise ValueError("必须提供两个不同目标的安装包")
    result = {}
    for artifact in artifacts:
        system = artifact.target.system
        if previous is not None and version_tuple(artifact.version) < version_tuple(
            previous[system]["version"]
        ):
            raise ValueError(
                f"{system} 的官网版本从 {previous[system]['version']} "
                f"倒退至 {artifact.version}，拒绝自动降级"
            )
        result[system] = artifact.entry(repository)
    return result


def encode_index(index: dict) -> bytes:
    return (json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
