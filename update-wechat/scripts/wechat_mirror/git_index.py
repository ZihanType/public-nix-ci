"""只推送生成的索引，避免覆盖其他工作流并发提交的修改。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from .model import INDEX_PATH, encode_index, load_index


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.update({
        "GIT_AUTHOR_NAME": "github-actions[bot]",
        "GIT_COMMITTER_NAME": "github-actions[bot]",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    })
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-C", str(root), *args],
        capture_output=True, text=True, env=environment, check=check,
    )


def read_index(root: Path, repository: str) -> dict | None:
    path = root / INDEX_PATH
    if path.is_symlink():
        raise ValueError("发布索引不能是符号链接")
    return load_index(path.read_bytes() if path.exists() else None, repository)


def commit_index(
    root: Path, repository: str, previous: dict | None, index: dict,
    *, attempts: int = 3,
) -> bool:
    for attempt in range(attempts):
        _git(root, "fetch", "--no-tags", "origin", "refs/heads/main")
        base = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="wechat-index-") as temporary:
            worktree = Path(temporary) / "checkout"
            _git(root, "worktree", "add", "--quiet", "--detach", str(worktree), base)
            try:
                current = read_index(worktree, repository)
                if current == index:
                    return False
                if current != previous:
                    raise ValueError("远程微信索引已被其他修改改变，拒绝覆盖")
                destination = worktree / INDEX_PATH
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(encode_index(index))
                _git(worktree, "add", "--", str(INDEX_PATH))
                versions = "、".join(
                    f"{system} {entry['version']}" for system, entry in sorted(index.items())
                )
                _git(worktree, "commit", "--quiet", "-m", f"更新微信镜像：{versions}")
                pushed = _git(
                    worktree, "push", "origin", "HEAD:refs/heads/main", check=False
                )
                if pushed.returncode == 0:
                    return True
                _git(root, "fetch", "--no-tags", "origin", "refs/heads/main")
                remote = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
                if remote == base or attempt + 1 == attempts:
                    raise RuntimeError(
                        "索引推送失败，已发布的镜像仍然保留；请检查权限或分支规则后重跑"
                    )
            finally:
                # 仅移除本函数创建的临时工作树，不改变原工作区及其分支。
                _git(root, "worktree", "remove", "--force", str(worktree))
    raise RuntimeError("索引推送重试次数必须大于零")
