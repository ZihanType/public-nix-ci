# Snipaste macOS 和 Linux 安装包镜像

参考 update-wechat，将 Snipaste 官网当前提供的安装包保存为不可变 GitHub Release，并生成供 Nix 配置消费的发布索引。发布、草稿恢复和索引推送机制沿用微信任务的约定，本功能不调用微信任务或交换其领域数据。

## 执行入口

- 工作流：[update-snipaste.yml](../.github/workflows/update-snipaste.yml)。
- 每天北京时间 08:43（UTC 00:43）运行一次，也支持 Actions 中的手动执行；实际开始时间可能受 GitHub 调度影响。
- 同一工作流串行执行，不取消正在发布的任务。
- 使用工作流自带的 GITHUB_TOKEN 和 contents: write 权限。
- 仓库需启用 GitHub 不可变 Release，分支规则需允许 Action 推送索引；发布时会检查实际 Release 的 immutable 属性，检查失败不更新索引。

## 官方输入与校验

目标范围与用户指定的 [nixpkgs sources.nix](https://github.com/NixOS/nixpkgs/blob/nixos-unstable/pkgs/by-name/sn/snipaste/sources.nix) 一致：

| 索引中的系统 | 官方包型 | 最新下载入口 | 版本字段 |
| --- | --- | --- | --- |
| aarch64-darwin | macOS Universal DMG | https://dl.snipaste.com/mac | mac_version |
| x86_64-linux | Linux x86_64 AppImage | https://dl.snipaste.com/linux | linux_version |

macOS 镜像保留官方 Universal 原包，本次索引只提供 aarch64-darwin 系统键。

从[官网下载页](https://www.snipaste.com/download.html)所用的 [versions 接口](https://www.snipaste.com/versions)读取各平台独立版本，读取[官方 SHA-1 清单](https://dl.snipaste.com/sha-1.txt)，然后完整下载两个文件。核对重定向后的文件名和官网版本是否一致，并核对官方 SHA-1，再计算本地 SHA-256。不使用 nixpkgs 中固定的版本号作为最新版，也不以 HTTP 元数据代替内容比较。

当前支持三段数字版本。版本格式未知、版本字段缺失、校验清单缺失或重复、下载文件名不匹配、下载截断及 SHA-1 不一致都会使任务失败。版本依据官网发布信息，不解包提取内部版本，也不执行安装包。

安装包使用流式处理，每次读取 1 MiB，最多接受 2 GiB；两份元数据各最多接受 1 MiB。这些是本任务的处理边界，不是上游包大小声明。临时网络错误最多尝试三次，每次下载开始、每 30 秒的进度及完成耗时都会写入日志。

## 镜像修订

- 每个系统独立维护版本和镜像修订，允许 macOS 与 Linux 版本不同。
- tag 为 `snipaste-<版本>-<系统>-<完整 SHA-256 十六进制摘要>`，每个 Release 仅包含该目标的一个原始安装包，保留官方文件名和字节内容。
- 同版本同内容复用已有发布，同版本换包产生新的镜像修订。
- 按各目标的数字版本独立比较，拒绝自动降级；历史 Release 和下载地址保持不变。
- 发布时不抢占仓库级最新 Release，消费方通过发布索引定位具体文件。

## 发布索引与恢复

首次成功发布后生成 update-snipaste/snipaste.json，顶层固定为 aarch64-darwin 和 x86_64-linux。每项仅包含：

| 字段 | 含义 |
| --- | --- |
| version | 该平台的官网版本 |
| url | 本仓库不可变 Release 中安装包的具体下载地址 |
| hash | SHA-256 SRI，例如 `sha256-BASE64=`，可直接传给 Nix fetchurl.hash |

所有目标都完成下载、元数据校验和降级检查后才开始发布，全部 Release 都发布成功后才提交索引。索引不记录检查时间，没有内容变化就不创建 Release 或提交。

上传中断可复用已有草稿和校验通过的资产；仅在草稿内清理失败上传留下的空占位资产。发布或推送中途失败时保留已发布内容和原索引，后续重跑继续完成。索引通过临时 Git 工作树推送至 main，只提交该 JSON；其他工作流并发提交时有限重试，同一索引被他人改动时拒绝覆盖。

工作流进入 main 后，可在 Actions 手动执行 update-snipaste 完成首次发布；在首次成功前不会写入引用尚未发布文件的索引。成功后索引地址为：

```text
https://raw.githubusercontent.com/ZihanType/public-nix-ci/main/update-snipaste/snipaste.json
```

在 Nix 中读取 JSON 后，按目标系统选择条目，将 url、hash 传给 fetchurl，并使用对应的 version。需要固定整个索引时，使用具体 Git commit 的 raw 地址。

## 本地验证

需要 Python 3.10 或更新版本及 Git，无第三方 Python 依赖，无须安装解包工具。从仓库根目录执行测试：

```fish
env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s update-snipaste/tests -v
```

下载并校验两个官方安装包，输出计划 JSON，不上传或提交，不需要 GitHub token：

```fish
env PYTHONDONTWRITEBYTECODE=1 python3 -u update-snipaste/scripts/update-snipaste.py --dry-run
```

正式运行由工作流执行，从 GITHUB_REPOSITORY 读取仓库，从 GH_TOKEN 或 GITHUB_TOKEN 读取凭据。

测试覆盖独立版本、元数据缺失与重复、官网 SHA-1、重定向文件名、下载截断、凭据重定向、同版本换包、降级拒绝、草稿恢复、不可变 Release 校验、发布失败保留索引及本地临时 Git 远程中的并发推送。测试不发布真实 Release 或推送真实仓库。

领域语言见 [GLOSSARY.md](./GLOSSARY.md)，关键决策见 [ADR](./docs/adr/)。
