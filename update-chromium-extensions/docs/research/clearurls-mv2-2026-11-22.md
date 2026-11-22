# ClearURLs 1.28.0 的 MV2、Chromium 与 ZIP 发布调查

调查日期：2026-11-22。本文回应三个问题：ClearURLs 是否仍使用 MV2、GitHub Release 缺少 `ClearURLs.zip` 意味着什么，以及历史 ZIP 与源码、编译产物是什么关系。用户的目标浏览器是仍保留 MV2 的 ungoogled-chromium。只记录调查证据，不修改 Chromium extension update component 的实现。

## 结论与边界

**已证事实：1.28.0 仍是 Manifest V2，上游仍生成并公开提供 `ClearURLs.zip`，但 ZIP 位于 GitLab CI artifact，GitHub Release 没有上传同名附件。** GitLab tag `1.28.0` 的成功构建和实际下载内容均可确认；不能将 GitHub 的附件缺失解释成「上游已经不再生成 ZIP」。[manifest](https://github.com/ClearURLs/Addon/blob/1.28.0/manifest.json)、[GitHub Release API](https://api.github.com/repos/ClearURLs/Addon/releases/tags/1.28.0)、[GitLab tag pipeline](https://gitlab.com/ClearURLs/ClearUrls/-/pipelines/3097817770)、[固定 job 的 ZIP](https://gitlab.com/api/v4/projects/6821549/jobs/18247859715/artifacts/ClearURLs.zip)。

**官方支持声明已经收窄到 Firefox；对保留 MV2 的 ungoogled-chromium，运行兼容性仍未知。** 最新 README 删除了 Chrome 与 Edge 的描述，但 manifest 和运行时代码尚保留 MV2、`webRequestBlocking` 及 Chrome 分支。官方文档将 Chromium 系浏览器停用扩展的现象归因于浏览器移除 MV2，并明确 MV3 迁移尚未完成。这些事实支持「仍有可供尝试的 MV2 内容」，不能提升为官方兼容承诺，也不能代替目标浏览器的实际加载与功能验证。[README 修改提交](https://github.com/ClearURLs/Addon/commit/fcd1e8f994df5f11bf0510d69fa343563529172e)、[manifest](https://github.com/ClearURLs/Addon/blob/1.28.0/manifest.json)、[storage.js](https://github.com/ClearURLs/Addon/blob/1.28.0/core_js/storage.js)、[tools.js](https://github.com/ClearURLs/Addon/blob/1.28.0/core_js/tools.js)、[官方故障排查说明](https://docs.clearurls.xyz/troubleshooting#chrome-edge-or-brave-disabled-clearurls-the-manifest-v2-situation)。

本次没有在用户的 ungoogled-chromium 中安装、加载或运行该扩展。

## 版本与发布状态

| 项目 | 已证结果 | 官方来源 |
| --- | --- | --- |
| GitHub 最新 Release | tag `1.28.0`，发布时间 `2026-11-21T16:38:59Z`，`assets: []`；API 另提供源码 zipball、tarball | [latest API](https://api.github.com/repos/ClearURLs/Addon/releases/latest)、[固定 Release API](https://api.github.com/repos/ClearURLs/Addon/releases/tags/1.28.0) |
| 旧 GitHub Release | `1.27.3` 于 2025-02-05 发布，附 `ClearURLs.zip`，大小 1,080,297 字节 | [Release API](https://api.github.com/repos/ClearURLs/Addon/releases/tags/1.27.3)、[附件](https://github.com/ClearURLs/Addon/releases/download/1.27.3/ClearURLs.zip) |
| GitLab tag pipeline | `3097817770`，ref `1.28.0`，sha `5fdc21e90134d8ccd3cc80c1518b7acf004cfa1b`，状态 `success`，于 `2026-11-21T16:35:55.293Z` 完成 | [pipeline API](https://gitlab.com/api/v4/projects/6821549/pipelines/3097817770) |
| GitLab ZIP job | `bundle addon`，job `18247859715`，状态 `success`；外层 `artifacts.zip` 大小 1,142,184 字节，过期时间 `2026-12-21T16:34:58.711Z` | [pipeline jobs API](https://gitlab.com/api/v4/projects/6821549/pipelines/3097817770/jobs?per_page=100)、[job 页面](https://gitlab.com/ClearURLs/ClearUrls/-/jobs/18247859715) |
| 实际 ZIP | 固定 job 中的 `ClearURLs.zip` 可以匿名下载，大小 1,155,755 字节；SHA-256 为 `9c73aa890468df4d47461f53516c0ba5040e693e45aaa0209700d244841a7061` | [固定 job 的 raw artifact](https://gitlab.com/api/v4/projects/6821549/jobs/18247859715/artifacts/ClearURLs.zip) |

本次涉及 GitHub Release 附件、自动生成的源码归档和 GitLab CI artifact 三种对象。GitHub 页面显示的自动源码下载不等于上传的 `ClearURLs.zip` 附件；GitLab 的外层 `artifacts.zip` 也不等于其中的 `ClearURLs.zip`。单个 job 详情 API 和 trace 在本次匿名访问中返回 401，但 pipeline jobs API 可读，raw artifact 下载成功。

**未知：为什么维护者没有将 1.28.0 的 ZIP 上传到 GitHub。** 本次未找到维护者对此作出的说明，故不能将其归因为疏漏、正式撤销 Chromium 支持或永久更换分发政策。官方文档还将 GitLab 说明为主仓库、GitHub 说明为只读镜像，但这也不能单独证明缺附件的动机。[官方仓库说明](https://docs.clearurls.xyz/#source--community)。

## MV2 与浏览器支持证据

1. `1.28.0/manifest.json` 声明 `manifest_version: 2`、`version: 1.28.0`，使用后台脚本和 `webRequestBlocking`。`browser_specific_settings.gecko.strict_min_version` 为 `98.0`，并新增 `data_collection_permissions.required: ["none"]`。这些是实际格式与权限声明；`gecko` 配置不是 Chromium 最低版本或支持承诺。[manifest](https://github.com/ClearURLs/Addon/blob/1.28.0/manifest.json)。
2. 2026-11-18 的官方提交 `fcd1e8f994df5f11bf0510d69fa343563529172e` 将 README 从「optimized for Firefox and some Chrome based browsers」改为只描述 Firefox，并删除 Edge 安装按钮。提交标题仅为 `Updated README and funding`，没有写明对所有保留 MV2 的 Chromium 分支终止运行兼容。[提交差异](https://github.com/ClearURLs/Addon/commit/fcd1e8f994df5f11bf0510d69fa343563529172e)。
3. `1.28.0` Release 的 Compatibility note 只要求 Firefox >= 98；`1.27.3` 原来同时列出 Firefox >= 55 和 Chrome >= 37。新版本不再给出 Chrome 最低版本。[1.28.0 Release](https://github.com/ClearURLs/Addon/releases/tag/1.28.0)、[1.27.3 Release](https://github.com/ClearURLs/Addon/releases/tag/1.27.3)。
4. 官方故障排查页明确：Firefox 仍获完整支持；Chrome、Brave 及其他 Chromium 浏览器可能自动禁用扩展；MV3 迁移未完成。网页总览仍保留面向 Firefox/Chrome 系浏览器的旧描述，故判断当前承诺时应同时考虑更近的 README 提交及具体故障排查说明。[官方故障排查](https://docs.clearurls.xyz/troubleshooting#chrome-edge-or-brave-disabled-clearurls-the-manifest-v2-situation)、[官方总览](https://docs.clearurls.xyz/)。
5. `1.28.0` 的 `storage.js` 仍有 Chrome 专用选项分支，`tools.js` 仍能识别 Chrome。这可证明 Chrome 相关运行时代码未全部删除，不能证明用户当前浏览器中每个 API 都可用。[storage.js](https://github.com/ClearURLs/Addon/blob/1.28.0/core_js/storage.js#L232)、[tools.js](https://github.com/ClearURLs/Addon/blob/1.28.0/core_js/tools.js)。

因此，对于用户已说明仍支持 MV2 的 ungoogled-chromium，Chrome 官方发行版移除 MV2 这一解释不能直接判定该浏览器无法运行 ClearURLs；实际兼容性必须另行验证。本次证据也不足以宣称 ClearURLs 官方继续支持该浏览器。

## ZIP 是怎样生成的

`1.27.3` 和 `1.28.0` 的 `bundle addon` 使用相同的打包命令，将仓库已存在的运行时文件直接放入 ZIP：

```sh
zip ClearURLs -r -FS clearurls.js browser-polyfill.js manifest.json img/* external_js/* html/* core_js/* css/* fonts/* _locales/*
```

该 job 没有 TypeScript 转译、JavaScript bundle 或 minify 步骤。独立的 `bundle addon chrome` job 在两版配置中都已注释，因而不是 1.28.0 才删除的 Chrome 构建变化。其注释内容原本会移除 manifest 的 `.applications` 字段并生成 `ClearURLs-chrome.zip`。[1.27.3 CI](https://github.com/ClearURLs/Addon/blob/1.27.3/.gitlab-ci.yml)、[1.28.0 CI](https://github.com/ClearURLs/Addon/blob/1.28.0/.gitlab-ci.yml)。

本次还逐文件比较了官方 ZIP 与对应 tag 的 GitHub 源码归档；忽略 ZIP 容器时间戳、压缩方式和源码归档顶层目录，仅比较文件路径及解压后的字节：

| 比较对象 | ZIP 文件数 | 源码文件数 | ZIP 独有文件 | 同路径内容不同 |
| --- | ---: | ---: | ---: | ---: |
| 1.27.3 GitHub 附件 vs tag 源码 | 165 | 180 | 0 | 0 |
| 1.28.0 GitLab 固定 job artifact vs tag 源码 | 170 | 203 | 0 | 0 |

结果说明：这两份 ZIP 中的每一个文件都已原样提交在对应 tag 的源码中；上游的 bundle job 做的是选择运行时目录并压缩，无须为了复现这些已有运行时字节而重新转译 TypeScript。[1.27.3 官方 ZIP](https://github.com/ClearURLs/Addon/releases/download/1.27.3/ClearURLs.zip)、[1.27.3 源码归档](https://api.github.com/repos/ClearURLs/Addon/zipball/1.27.3)、[1.28.0 官方 CI ZIP](https://gitlab.com/api/v4/projects/6821549/jobs/18247859715/artifacts/ClearURLs.zip)、[1.28.0 源码归档](https://api.github.com/repos/ClearURLs/Addon/zipball/1.28.0)。

旧 `1.27.3` 附件的 SHA-256 为 `2d5c879d3e7d8f562b0ffbb4bd5b4a94884ef404d8ed5637bf4da2601556d148`。源码比该附件多 15 个文件：`.github/FUNDING.yml`、`.github/lockdown.yml`、`.gitignore`、`.gitlab-ci.yml`、`.gitlab/.gitkeep`、`.gitlab/issue_templates/{.gitkeep,Bug.md,New rule.md,Suggestion.md}`、`.gitmodules`、`CHANGELOG.md`、`LICENSE`、`PRIVACY.md`、`README.md`、`build_tools/minifyDataJSON.js`。1.28.0 源码比 CI ZIP 多 33 个文件，属于 CI、文档、构建工具及测试等 ZIP 打包命令未选择的内容。[1.27.3 tag 树](https://github.com/ClearURLs/Addon/tree/1.27.3)、[1.28.0 tag 树](https://github.com/ClearURLs/Addon/tree/1.28.0)。

这不表示 ClearURLs 从未使用 TypeScript。旧版已提交 `Multimap.js`、`URLHashParams.js` 及其 `.d.ts`；引入这些文件的 2022-07-26 提交明确提及 ClearURLs v2 的 TypeScript 来源。1.28.0 的 `build_tools/core-utils-source.json` 更记录了独立 core 仓库来源提交、TypeScript `5.9.3` 和输入/输出文件哈希。应区分「上游开发时产生并提交 JavaScript」与「使用 Addon tag 的既有 JavaScript 打包 ZIP」。[引入工具代码的提交](https://github.com/ClearURLs/Addon/commit/0d7381f02ae2aa77b627eb67ae2129ca8d0feffd)、[1.28.0 产物来源记录](https://github.com/ClearURLs/Addon/blob/1.28.0/build_tools/core-utils-source.json)。

规则数据也没有内嵌在上述 ZIP 中。`storage.js` 默认指向运行时下载的 `https://rules2.clearurls.xyz/data.minify.json` 与 `rules.minify.hash`；CI 的 `hash rules`、`pages` 是另外的任务。因此打包扩展运行时代码与生成规则数据应分别理解。[1.27.3 storage.js](https://github.com/ClearURLs/Addon/blob/1.27.3/core_js/storage.js#L217)、[1.28.0 storage.js](https://github.com/ClearURLs/Addon/blob/1.28.0/core_js/storage.js)、[CI 配置](https://github.com/ClearURLs/Addon/blob/1.28.0/.gitlab-ci.yml)。

## 本地核查记录

下载及比较记录暂存在 `/Users/arthur/.cache/clearurls-investigation/`，包括 `archive-analysis.json`、`archive-analysis-1.28.0.json`、`pipeline-1.28.0-jobs.json` 和两版官方 ZIP。它们是本次检查的本地结果，文中的公开链接才是可复核的上游来源。GitLab artifact 带有过期时间，下载链接的可用性也只代表调查当日的状态。
