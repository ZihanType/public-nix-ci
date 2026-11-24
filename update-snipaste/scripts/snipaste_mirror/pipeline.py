"""先完成双目标解析与校验，再发布并提交索引。"""

from __future__ import annotations

import argparse
from functools import partial
import os
from pathlib import Path
import subprocess
import tempfile

from .git_index import commit_index, read_index
from .github import GitHub
from .model import TARGETS, check_repository, encode_index, make_index
from .upstream import load_metadata, resolve


def update(previous, repository, directory, resolver, publisher, save, commit):
    artifacts = []
    for target in TARGETS:
        print(f"下载并校验 {target.system}……", flush=True)
        artifact = resolver(target, directory)
        artifacts.append(artifact)
        print(
            f"{target.system}：{artifact.version}，SHA-256 {artifact.digest}",
            flush=True,
        )
    index = make_index(artifacts, previous, repository)
    if publisher is None:
        print("计划索引（仅验证，未发布或写入仓库）：", flush=True)
        print(encode_index(index).decode(), end="")
        return index

    # 检查全部已存在的发布，避免第二个目标的冲突造成不必要的部分发布。
    for artifact in artifacts:
        existing = publisher.preflight(artifact)
        if (
            previous is not None
            and previous[artifact.target.system] == index[artifact.target.system]
            and (existing is None or existing["draft"])
        ):
            raise ValueError("旧索引引用的不可变 Release 缺失或仍是草稿")
    if index == previous:
        print("两个目标均无更新，不创建 Release 或提交。", flush=True)
        return index
    for artifact in artifacts:
        publisher.publish(artifact, commit)
    save(index)
    print("镜像发布完成，远程索引已更新。", flush=True)
    return index


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="镜像 Snipaste macOS DMG 和 Linux AppImage 并更新发布索引")
    parser.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY", "ZihanType/public-nix-ci"),
        help="发布目标仓库，默认读取 GITHUB_REPOSITORY",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="下载并校验两个官方安装包，只输出计划索引，不发布、不提交",
    )
    args = parser.parse_args(argv)
    repository = check_repository(args.repository)
    root = Path(__file__).resolve().parents[3]
    previous = read_index(root, repository)
    publisher = None
    if not args.dry_run:
        publisher = GitHub(
            repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        )
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="snipaste-mirror-") as temporary:
        directory = Path(temporary)
        metadata = load_metadata(directory)
        update(
            previous, repository, directory, partial(resolve, metadata=metadata), publisher,
            lambda index: commit_index(root, repository, previous, index), commit,
        )
    return 0
