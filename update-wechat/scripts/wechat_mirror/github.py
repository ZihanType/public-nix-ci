"""通过草稿完成不可变 Release 发布，并恢复未完成的上传。"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .download import CHUNK_SIZE, USER_AGENT, download
from .model import Artifact, check_repository


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = check_repository(repository)
        if not token:
            raise ValueError("发布需要 GH_TOKEN 或 GITHUB_TOKEN")
        self.token = token
        self.api_root = f"https://api.github.com/repos/{repository}"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def _request(self, method: str, endpoint: str, body=None, *, missing=False):
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.api_root + endpoint, data=data, headers=headers, method=method
        )
        # 写请求结果不确定时交给下一次运行恢复，避免盲目重试产生重复发布。
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    contents = response.read()
                    return json.loads(contents) if contents else None
            except urllib.error.HTTPError as error:
                if error.code == 404 and missing:
                    return None
                if error.code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                    raise RuntimeError(
                        f"GitHub {method} {endpoint} 失败：HTTP {error.code}"
                    ) from error
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                if attempt + 1 == attempts:
                    raise
            time.sleep(2**attempt)
        raise RuntimeError("GitHub 请求失败")

    def find_release(self, artifact: Artifact) -> dict | None:
        release = self._request(
            "GET", "/releases/tags/" + urllib.parse.quote(artifact.tag, safe=""),
            missing=True,
        )
        if release is not None:
            return release
        # tag 接口不一定返回草稿，分页检查列表才能恢复中断的首次发布。
        page = 1
        while True:
            values = self._request("GET", f"/releases?per_page=100&page={page}")
            if not isinstance(values, list):
                raise ValueError("GitHub 未返回 Release 列表")
            matches = [
                value for value in values
                if isinstance(value, dict) and value.get("tag_name") == artifact.tag
            ]
            if len(matches) > 1:
                raise ValueError(f"{artifact.tag} 对应多个 Release")
            if matches:
                return matches[0]
            if len(values) < 100:
                return None
            page += 1

    def _verify_asset(self, asset: dict, artifact: Artifact, *, draft: bool) -> None:
        if (
            asset.get("name") != artifact.asset_name
            or asset.get("state") != "uploaded"
            or asset.get("size") != artifact.path.stat().st_size
        ):
            raise ValueError(f"{artifact.tag} 的资产名称、上传状态或大小不正确")
        # 草稿使用 untagged 临时地址；正式地址只在发布后校验，不写入草稿地址。
        if not draft and asset.get("browser_download_url") != artifact.entry(self.repository)["url"]:
            raise ValueError(f"{artifact.tag} 已发布资产的下载地址与版本地址不一致")
        digest = asset.get("digest")
        if digest is None:
            asset_id = asset.get("id")
            if type(asset_id) is not int or asset_id <= 0:
                raise ValueError("GitHub 资产缺少可校验的摘要和 ID")
            # 兼容尚未提供摘要的资产；跨域跳转时 download 会移除凭据。
            headers = self._headers()
            headers["Accept"] = "application/octet-stream"
            with tempfile.TemporaryDirectory(prefix="wechat-asset-check-") as work:
                actual = download(
                    f"{self.api_root}/releases/assets/{asset_id}",
                    Path(work) / "asset.AppImage", headers=headers,
                )
            digest = "sha256:" + actual
        if digest != "sha256:" + artifact.digest:
            raise ValueError(f"{artifact.tag} 已有资产的 SHA-256 与官方安装包不一致")

    def _validate_release(self, release: dict, artifact: Artifact) -> bool:
        if (
            not isinstance(release, dict)
            or type(release.get("id")) is not int
            or release["id"] <= 0
            or release.get("tag_name") != artifact.tag
            or type(release.get("draft")) is not bool
            or release.get("prerelease") is not False
        ):
            raise ValueError(f"{artifact.tag} 的 Release 元数据无效")
        if not release["draft"] and release.get("immutable") is not True:
            raise ValueError(f"{artifact.tag} 已发布但不是不可变 Release")
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) > 1:
            raise ValueError(f"{artifact.tag} 存在异常的资产列表")
        if not assets:
            if release["draft"]:
                return False
            raise ValueError(f"{artifact.tag} 已发布但缺少安装包")
        if not isinstance(assets[0], dict):
            raise ValueError(f"{artifact.tag} 的资产元数据无效")
        if (
            release["draft"]
            and assets[0].get("name") == artifact.asset_name
            and assets[0].get("state") == "starter"
            and assets[0].get("size") == 0
            and type(assets[0].get("id")) is int
            and assets[0]["id"] > 0
        ):
            # GitHub 上传失败可能留下空占位资产，它还不是已发布的镜像。
            return False
        self._verify_asset(assets[0], artifact, draft=release["draft"])
        return True

    def preflight(self, artifact: Artifact) -> dict | None:
        release = self.find_release(artifact)
        if release is not None:
            self._validate_release(release, artifact)
        return release

    def _upload(self, release: dict, artifact: Artifact) -> dict:
        endpoint = (
            f"/repos/{self.repository}/releases/{release['id']}/assets?"
            + urllib.parse.urlencode({"name": artifact.asset_name})
        )
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=180)
        try:
            connection.putrequest("POST", endpoint)
            headers = self._headers()
            headers.update({
                "Content-Type": "application/octet-stream",
                "Content-Length": str(artifact.path.stat().st_size),
            })
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with artifact.path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    connection.send(chunk)
            response = connection.getresponse()
            contents = response.read()
            if response.status != 201:
                raise RuntimeError(f"安装包上传失败：HTTP {response.status}")
            asset = json.loads(contents)
            if not isinstance(asset, dict):
                raise ValueError("GitHub 上传结果不是资产对象")
            self._verify_asset(asset, artifact, draft=release["draft"])
            return asset
        finally:
            connection.close()

    def publish(self, artifact: Artifact, commit: str) -> None:
        release = self.find_release(artifact)
        title = f"微信 Linux {artifact.version} ({artifact.architecture.name})"
        notes = (
            "从微信官方网站镜像的原始 AppImage，保留安装包字节不变。\n\n"
            f"- 官方地址：{artifact.architecture.source_url}\n"
            f"- SHA-256：`{artifact.digest}`\n"
            f"- Nix SRI：`{artifact.entry(self.repository)['sha256']}`\n"
        )
        if release is None:
            release = self._request("POST", "/releases", {
                "tag_name": artifact.tag,
                "target_commitish": commit,
                "name": title,
                "body": notes,
                "draft": True,
                "prerelease": False,
                "make_latest": "false",
            })
        has_asset = self._validate_release(release, artifact)
        if not has_asset:
            if release["assets"]:
                self._request("DELETE", f"/releases/assets/{release['assets'][0]['id']}")
            asset = self._upload(release, artifact)
            # 上传响应是本次写入的结果，不依赖可能延迟更新的草稿列表。
            release = dict(release, assets=[asset])
            self._validate_release(release, artifact)
        if release["draft"]:
            published = self._request("PATCH", f"/releases/{release['id']}", {
                "name": title,
                "body": notes,
                "draft": False,
                "prerelease": False,
                "make_latest": "false",
            })
            self._validate_release(published, artifact)
            if published["draft"]:
                raise ValueError(f"{artifact.tag} 仍是草稿，停止更新索引")
