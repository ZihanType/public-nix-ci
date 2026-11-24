# Snipaste 安装包镜像

## 用户需求

- 参考 update-wechat，从 https://www.snipaste.com/download.html 获取最新 macOS 和 Linux 安装包，并镜像到本仓库 GitHub Releases。
- 生成 JSON，结构参考 https://github.com/NixOS/nixpkgs/blob/nixos-unstable/pkgs/by-name/sn/snipaste/sources.nix。
- 每天自动执行，并支持手动执行。

## 已确认的决策

- 每个目标系统及架构独立发布不可变 Release，tag 包含版本、目标系统及架构和完整 SHA-256；同版本换包产生新修订，同内容重跑复用并保留历史。见 [ADR-0001](../../update-snipaste/docs/adr/0001-preserve-mirror-revisions.md)。
- 所有目标先完成下载与校验，全部发布成功后才提交 JSON；失败保留旧索引，已发布内容留待重跑复用，各目标独立比较版本并拒绝自动降级。见 [ADR-0002](../../update-snipaste/docs/adr/0002-publish-before-updating-index.md)。
- 每天北京时间 08:43（UTC 00:43）运行，提供 workflow_dispatch 手动入口。
- 同一工作流串行执行，不取消正在执行的任务。
- JSON 自动提交到 main，没有内容变化就不提交。
- 目标与 nixpkgs sources.nix 一致：aarch64-darwin 对应官方 macOS Universal DMG，x86_64-linux 对应官方 Linux x86_64 AppImage；本次不增加其他系统键。
- 发布索引路径为 update-snipaste/snipaste.json；每个系统项仅包含 version、url、hash，url 指向本仓库不可变 Release，hash 为 SHA-256 SRI，可直接用于 Nix fetchurl.hash。
- 每日分别读取官网的 mac_version、linux_version，并完整下载安装包；核对最终文件名版本及官方 SHA-1，再计算 SHA-256。元数据缺失或不一致时失败，不解包提取内部版本。见 [ADR-0003](../../update-snipaste/docs/adr/0003-use-official-release-metadata.md)。

## 已核实的上游事实

- [官网下载页](https://www.snipaste.com/download.html) 当前提供 macOS Universal DMG 和 Linux x86_64 AppImage。
- [官网版本接口](https://www.snipaste.com/versions) 分别提供 mac_version 和 linux_version；本次调查时均为 2.11.3，运行时不得硬编码该版本。
- 最新下载入口为 https://dl.snipaste.com/mac 和 https://dl.snipaste.com/linux，均重定向至带版本号的官方归档文件。
- [官方 SHA-1 清单](https://dl.snipaste.com/sha-1.txt) 列出两个平台安装包的校验值；发布索引仍使用本地计算的 SHA-256 SRI。
- [nixpkgs sources.nix](https://github.com/NixOS/nixpkgs/blob/nixos-unstable/pkgs/by-name/sn/snipaste/sources.nix) 的系统键为 aarch64-darwin 和 x86_64-linux，条目包含 version 及 fetchurl 所用的 url、hash。

## 设计确认

用户已确认完整设计，授权开始实现并测试。

## 实现与验证

- 工作流位于 .github/workflows/update-snipaste.yml，功能实现、测试和文档位于 update-snipaste/。
- 42 项测试通过，覆盖元数据校验、下载、发布恢复、索引契约及本地 Git 推送竞争。
- 新工作流通过 actionlint 静态检查。
- 2026-11-24 使用 --dry-run 完整下载并校验两个官方 2.11.3 安装包，两者 SHA-256 SRI 均与 nixpkgs sources.nix 一致；本次只读运行未发布 Release 或提交索引。
