# 微信 Linux 安装包镜像

本功能为微信官方的可变下载地址提供不可变镜像，并生成供 Nix 配置消费的发布索引。

## 执行入口

- 工作流位于 `.github/workflows/update-wechat.yml`，功能实现、测试及文档位于 `update-wechat/`。
- 每天北京时间 08:23（UTC 00:23）执行一次，也提供手动执行入口；定时触发的实际开始时间可能受 GitHub 调度影响。
- 同一工作流串行执行，不取消正在发布的任务。
- 使用工作流自带的 `GITHUB_TOKEN` 和 `contents: write` 权限。
- 仓库需启用 GitHub 不可变 Release，且分支规则允许 Action 推送索引。发布时会检查实际 Release 的 `immutable` 属性，检查失败则不更新索引。

## 官方输入

仅处理以下两个 AppImage，不镜像 deb 或 rpm：

| 索引中的系统 | 官方文件名 |
| --- | --- |
| `x86_64-linux` | `WeChatLinux_x86_64.AppImage` |
| `aarch64-linux` | `WeChatLinux_arm64.AppImage` |

下载地址前缀为 `https://dldir1v6.qq.com/weixin/Universal/Linux/`。每天完整下载两个文件并计算 SHA-256，不以 HTTP 元数据代替内容比较。

通过解包读取实际安装包中的完整版本，并校验处理器架构；不运行下载的程序，也不把官网网页显示的简略版本当作完整版本。无法唯一确定完整版本时任务失败。

## 镜像修订

两个架构分别维护自己的最新版本，允许版本号不同。任一架构的官方版本或安装包内容改变，都产生新的镜像修订；版本倒退则让任务失败并保留旧索引。

每个架构独立发布一个不可变 GitHub Release：

- tag：`wechat-<完整版本>-<架构>-<完整 SHA-256 十六进制摘要>`。
- 架构标识分别为 `x86_64` 和 `aarch64`。
- 安装包内容与官方下载的字节完全一致。
- 同版本、同内容复用已有发布；同版本、不同内容使用新 tag。
- 不覆盖或删除历史发布，也不使用仓库级最新 Release 定位微信。

## 发布索引

首次成功执行后生成 `update-wechat/wechat.json`，顶层固定为 `x86_64-linux` 和 `aarch64-linux`。每项仅包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `version` | 对应 AppImage 的完整版本号 |
| `url` | 对应不可变 Release 安装包的具体下载地址 |
| `sha256` | Nix SRI 格式的 SHA-256，例如 `sha256-BASE64=`，可作为 `fetchurl.hash` |

索引不包含每次检查时间，避免无更新时产生提交。后续 Nix 配置从仓库读取该文件，本次不修改 Nix 配置的消费逻辑。

## 失败与恢复

1. 两个架构都完成下载、版本提取、架构校验和降级检查，才开始发布。
2. 先准备草稿并上传校验通过的资产，再发布不可变 Release；已有发布必须通过内容及不可变性校验。上传失败留下的空 `starter` 资产仅在草稿中清理，已成功上传的内容校验后复用。
3. 所有发布完成后才生成索引，由 Action 自动提交并推送到 `main`；没有变化就不创建 Release 或 Git 提交。
4. 发布或推送中途失败时保留已成功的镜像，下次复用并继续；远程索引保持最近一次成功提交的状态。
5. 其他工作流同时推送时，允许有限次数的安全重试；如果同一索引被其他修改改变，则报错，避免覆盖。

## 本地使用

需要 Python 3.10 或更新版本、Git 和 `squashfs-tools`，无第三方 Python 依赖。测试会使用 `mksquashfs` 创建小型测试镜像；真实包解析只使用 `unsquashfs`，不执行 AppImage。

在本功能目录执行测试：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

下载并校验两个官方 AppImage，输出计划索引，不需要 GitHub token，也不上传或提交：

```bash
python3 scripts/update-wechat.py --dry-run
```

正式发布由工作流执行 `python3 -u scripts/update-wechat.py`，从 `GITHUB_REPOSITORY` 读取仓库，从 `GH_TOKEN` 或 `GITHUB_TOKEN` 读取凭据。实现通过临时 Git 工作树提交唯一的索引文件，不把原工作区的其他修改加入提交。

工作流提交到默认分支后，可在 Actions 中手动运行 `update-wechat` 完成首次发布。首次成功运行前没有 `wechat.json`；成功后可通过以下地址读取：

```text
https://raw.githubusercontent.com/ZihanType/public-nix-ci/main/update-wechat/wechat.json
```

在 Nix 中将选定架构记录的 `url` 传给 `fetchurl.url`，将 `sha256` 传给 `fetchurl.hash`。需要固定整个索引时，使用具体 Git commit 的 raw 地址代替 `main`。

## 验证范围

测试覆盖两架构的真实 SquashFS 解包、版本歧义、同版本换包、降级拒绝、下载截断、凭据重定向、草稿恢复、不可变性检查，以及本地临时 Git 远程中的并发推送。测试不会向 GitHub 发布资产或推送真实仓库。

领域语言见 [GLOSSARY.md](./GLOSSARY.md)，关键决策见 [ADR](./docs/adr/)。
