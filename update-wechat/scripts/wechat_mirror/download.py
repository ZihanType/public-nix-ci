"""流式下载及内容校验，不将完整安装包载入内存。"""

from __future__ import annotations

import hashlib
import http.client
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


CHUNK_SIZE = 1024 * 1024
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
USER_AGENT = "public-nix-ci-wechat-mirror/1"


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("拒绝重定向至非 HTTPS 下载地址")
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None:
            # GitHub 资产可能跳转到 CDN；签名下载地址不需要携带 API 凭据。
            redirected.remove_header("Authorization")
        return redirected


def download(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
) -> str:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("下载地址必须使用 HTTPS")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with opener.open(request, timeout=60) as response, destination.open("wb") as out:
                if response.status != 200:
                    raise ValueError(f"完整下载要求 HTTP 200，实际为 {response.status}")
                expected = response.headers.get("Content-Length")
                expected_size = int(expected) if expected is not None else None
                if expected_size is not None and not 0 < expected_size <= MAX_DOWNLOAD_SIZE:
                    raise ValueError("下载文件大小不在允许范围内")
                digest = hashlib.sha256()
                size = 0
                while chunk := response.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_SIZE:
                        raise ValueError("下载文件超过 2 GiB")
                    out.write(chunk)
                    digest.update(chunk)
                if size == 0 or (expected_size is not None and size != expected_size):
                    raise OSError(f"下载不完整：收到 {size} 字节，预期 {expected_size}")
                return digest.hexdigest()
        except urllib.error.HTTPError as error:
            destination.unlink(missing_ok=True)
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise RuntimeError(f"下载失败：HTTP {error.code}，{url}") from error
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            destination.unlink(missing_ok=True)
            if attempt + 1 == attempts:
                raise
        time.sleep(2**attempt)
    raise RuntimeError("下载重试次数必须大于零")
