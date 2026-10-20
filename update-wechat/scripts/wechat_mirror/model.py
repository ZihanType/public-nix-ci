"""镜像修订及发布索引的约束。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
from pathlib import Path
import re


OFFICIAL_ROOT = "https://dldir1v6.qq.com/weixin/Universal/Linux/"
INDEX_PATH = Path("update-wechat/wechat.json")
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}"


@dataclass(frozen=True)
class Architecture:
    system: str
    name: str
    official_name: str
    elf_machine: int

    @property
    def source_url(self) -> str:
        return OFFICIAL_ROOT + f"WeChatLinux_{self.official_name}.AppImage"


ARCHITECTURES = (
    Architecture("x86_64-linux", "x86_64", "x86_64", 62),
    Architecture("aarch64-linux", "aarch64", "arm64", 183),
)


def check_repository(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("仓库必须使用 owner/repository 格式")
    return repository


def version_tuple(version: str) -> tuple[int, ...]:
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError(f"无效的微信完整版本：{version!r}")
    return tuple(int(part) for part in version.split("."))


def digest_from_sri(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256-"):
        raise ValueError("sha256 必须使用 SHA-256 SRI 格式")
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
    architecture: Architecture
    version: str
    digest: str
    path: Path

    def __post_init__(self) -> None:
        version_tuple(self.version)
        sri_from_digest(self.digest)
        if self.architecture not in ARCHITECTURES:
            raise ValueError("不支持的微信架构")

    @property
    def tag(self) -> str:
        return f"wechat-{self.version}-{self.architecture.name}-{self.digest}"

    @property
    def asset_name(self) -> str:
        return f"WeChatLinux_{self.version}_{self.architecture.name}.AppImage"

    def entry(self, repository: str) -> dict[str, str]:
        check_repository(repository)
        return {
            "version": self.version,
            "url": (
                f"https://github.com/{repository}/releases/download/"
                f"{self.tag}/{self.asset_name}"
            ),
            "sha256": sri_from_digest(self.digest),
        }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def load_index(contents: bytes | None, repository: str) -> dict | None:
    if contents is None:
        return None
    value = json.loads(contents, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {a.system for a in ARCHITECTURES}:
        raise ValueError("发布索引必须且只能包含 x86_64-linux 和 aarch64-linux")
    for arch in ARCHITECTURES:
        entry = value[arch.system]
        if not isinstance(entry, dict) or set(entry) != {"version", "url", "sha256"}:
            raise ValueError(f"{arch.system} 必须仅包含 version、url、sha256")
        artifact = Artifact(
            arch, entry["version"], digest_from_sri(entry["sha256"]), Path(".")
        )
        if entry != artifact.entry(repository):
            raise ValueError(f"{arch.system} 的下载地址与版本、架构或 SHA-256 不一致")
    return value


def make_index(artifacts: list[Artifact], previous: dict | None, repository: str) -> dict:
    if len(artifacts) != 2 or {a.architecture for a in artifacts} != set(ARCHITECTURES):
        raise ValueError("必须提供两个不同架构的安装包")
    result = {}
    for artifact in artifacts:
        system = artifact.architecture.system
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
