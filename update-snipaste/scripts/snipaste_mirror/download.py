"""流式下载及内容校验，不将完整安装包载入内存。"""

from __future__ import annotations

import hashlib
import http.client
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request


# 每次读取 1 MiB，安装包最多接受 2 GiB；这些是本任务的流式处理边界。
CHUNK_SIZE = 1024 * 1024
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
USER_AGENT = "public-nix-ci-snipaste-mirror/1"
# 每 30 秒报告一次下载量，区分 CI 网络缓慢与后续安装包解析停滞。
PROGRESS_INTERVAL = 30


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
    max_size: int = MAX_DOWNLOAD_SIZE,
    expected_name: str | None = None,
    expected_sha1: str | None = None,
) -> str:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("下载地址必须使用 HTTPS")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    opener = urllib.request.build_opener(HTTPSRedirectHandler())
    for attempt in range(attempts):
        try:
            started = last_report = time.monotonic()
            print(f"下载 {destination.name}，第 {attempt + 1}/{attempts} 次尝试", flush=True)
            request = urllib.request.Request(url, headers=request_headers)
            with opener.open(request, timeout=60) as response, destination.open("wb") as out:
                if response.status != 200:
                    raise ValueError(f"完整下载要求 HTTP 200，实际为 {response.status}")
                if expected_name is not None:
                    final_url = urllib.parse.urlsplit(response.geturl())
                    final_name = urllib.parse.unquote(final_url.path.rsplit("/", 1)[-1])
                    if final_url.scheme != "https" or final_name != expected_name:
                        raise ValueError(
                            f"官方下载的最终文件名与官网版本不一致：{final_name!r}，"
                            f"预期 {expected_name!r}"
                        )
                expected = response.headers.get("Content-Length")
                expected_size = int(expected) if expected is not None else None
                if expected_size is not None and not 0 < expected_size <= max_size:
                    raise ValueError("下载文件大小不在允许范围内")
                digest = hashlib.sha256()
                # SHA-1 仅核对官网发布清单；内容身份和 Nix 校验值始终使用 SHA-256。
                upstream_digest = hashlib.sha1() if expected_sha1 is not None else None
                size = 0
                while chunk := response.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError(f"下载文件超过 {max_size} 字节限制")
                    out.write(chunk)
                    digest.update(chunk)
                    if upstream_digest is not None:
                        upstream_digest.update(chunk)
                    now = time.monotonic()
                    if now - last_report >= PROGRESS_INTERVAL:
                        print(
                            f"下载 {destination.name}：{size}/{expected_size or '未知'} 字节，"
                            f"已用时 {now - started:.1f} 秒",
                            flush=True,
                        )
                        last_report = now
                if size == 0 or (expected_size is not None and size != expected_size):
                    raise OSError(f"下载不完整：收到 {size} 字节，预期 {expected_size}")
                if upstream_digest is not None and upstream_digest.hexdigest() != expected_sha1:
                    raise ValueError(f"{expected_name} 的内容与官方 SHA-1 不一致")
                print(
                    f"下载完成 {destination.name}：{size} 字节，"
                    f"耗时 {time.monotonic() - started:.1f} 秒",
                    flush=True,
                )
                return digest.hexdigest()
        except ValueError:
            destination.unlink(missing_ok=True)
            raise
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
