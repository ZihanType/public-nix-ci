# Chromium extension update component

This component turns a small human-maintained catalog into immutable, content-addressed CRX artifacts for private Nix configurations.

## Chromium extension pipeline

The [scheduled workflow](../.github/workflows/update-chromium-extensions.yml) reads [`extensions.jsonc`](./extensions.jsonc), publishes each resolved version through GitHub Immutable Releases, and updates [`extensions.lock`](./extensions.lock). The workflow remains under the repository-level `.github/` directory because GitHub only discovers workflows there. It runs every four hours at minute 15 and can also be started manually.

The pipeline only resolves artifacts. It does not install extensions or claim that Brave, Chromium, ungoogled-chromium, or any other browser can run them.

### Catalog

Chrome Web Store entries need only their extension ID. Comments and trailing commas are allowed:

```jsonc
{
  "chromeWebStore": [
    "mpiodijhokgodhhofbcjdecpffjipkle", // SingleFile
  ],

  "githubReleases": {
    "ublock-origin": {
      "repository": "gorhill/uBlock",
      "asset": "uBlock0_*.chromium.zip",
    },
  },
}
```

The GitHub map key is a stable local identity. Its signing key lives at `keys/<name>.pem`. `asset` is optional only when the latest non-draft, non-prerelease Release contains exactly one ZIP; otherwise its case-sensitive glob must match exactly one asset.

如果上游没有上传扩展 ZIP，但已将运行所需文件提交到仓库，可以显式选择最新正式 Release 对应标签的源码归档：

```jsonc
"clear-urls": {
  "repository": "ClearURLs/Addon",
  "sourceArchive": true,
  "include": [
    "clearurls.js", "browser-polyfill.js", "manifest.json",
    "img", "external_js", "html", "core_js", "css", "fonts", "_locales",
  ],
},
```

`sourceArchive: true` 与 `asset` 互斥，`include` 仅用于源码模式且必须为非空清单。清单使用相对于仓库根目录的字面文件或目录路径：去除 GitHub ZIP 的外层包装目录，目录递归包含，不展开通配符；每项必须至少选中一个文件，上游改名或删除路径会使整个解析失败并保留现有锁文件。ClearURLs 的清单来自其 [bundle addon 配置](https://github.com/ClearURLs/Addon/blob/1.28.0/.gitlab-ci.yml)，不包含源码中的文档、测试或独立规则子模块。

文件筛选前仍校验整个原始归档的安全性和资源上限，筛选后沿用唯一 manifest、本地化名称及确定性签名校验；选中的文件必须位于同一个扩展根目录中。源码模式下载 Release 元数据提供的 `zipball_url`，不跟踪开发分支，不自动回退到附件，也不执行上游编译命令或拉取子模块；需要先编译才能获得扩展文件的项目应提供已构建的发布附件。审计说明记录下载类型、源码文件清单、原始 ZIP 及规范化 ZIP 的哈希，扩展 ID 继续由原签名密钥决定。

Versions and extension names are never entered manually. The workflow reads `manifest.json` from the current Web Store CRX or latest public GitHub Release ZIP. A localized manifest name is resolved through `default_locale`, then normalized once for the lock, generated Git history, Actions logs, and GitHub Releases; an invalid or missing localized name fails the atomic resolution.

For Web Store requests, the client version is the full version from the `Stable` channel in Google's Chrome for Testing metadata. “Chrome for Testing” names the automation-oriented distribution and metadata service; `Stable` still means the Stable channel, not Beta, Dev, or Canary.

### Published artifacts

Every newly resolved extension version uses its stable extension ID in both its permanent tag and asset name:

```text
tag:   extension-<id>-v<version>
asset: <id>-<version>.crx
```

The Release title remains the human-readable `<name> v<version>`, while the tag uses the extension artifact's stable ID and version identity. Tags must not exceed 250 UTF-8 bytes and are validated as Git refs. Versions already present in the lock retain their historical tag and URL, including name-based tags created under the previous policy; immutable history is never renamed or republished solely to migrate the tag format.

Web Store CRXs retain their upstream bytes, signature, and ID. GitHub ZIPs are safely unpacked, normalized into a deterministic ZIP, and wrapped in a reproducible CRX3 signed by the source-specific key committed under `keys/`.

CRX3 is version 3 of the signed `.crx` container format. It is independent of an extension's `manifest_version`: CRX3 does not mean Manifest V3 and can contain upstream Manifest V2 or Manifest V3 files.

The generated lock is strict JSON, sorted by ID, and contains only:

```json
{
  "name": "Example Extension",
  "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "version": "1.2.3",
  "url": "https://github.com/.../aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1.2.3.crx",
  "sha256": "sha256-...="
}
```

GitHub Immutable Releases must be enabled as a repository bootstrap setting. The workflow requires every published or reused Release to report itself as immutable before it updates the lock. An existing ID/version with identical bytes is reused; different bytes for the same version are rejected and never overwrite history.

### Public signing keys

Files under [`keys/`](./keys/) are deliberately public private keys used only to preserve CRX extension IDs. Anyone can sign another CRX with the same ID. Nix content hashes and trusted repository changes—not key secrecy—are the integrity boundary.

When a GitHub source has no key, the workflow generates and commits one before publishing. Removing a catalog entry removes it from the current lock but retains its key and historical Releases.

### Atomicity and recovery

Every catalog entry must download, build, and validate before publication begins. Runs are serialized and are never cancelled in progress. If Release publication succeeds but the final Git push fails, the next run verifies and reuses that immutable asset before completing the generated commits.

Each extension change receives its own commit:

```text
chore(chromium): add <name> <version>
chore(chromium): update <name> to <new-version>
chore(chromium): remove <name>
```

Dry runs and successful publishing runs print the same name-based change summary in Actions:

```text
Added <name>: <version>
Updated <name>: <old-version> -> <new-version>
Removed <name>: <version>
```

When an upstream version also changes its name, the update summary uses `Updated <old-name> -> <new-name>: <old-version> -> <new-version>`.

### Local verification

Run these commands from the `update-chromium-extensions` directory.

Run unit tests:

```fish
env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Resolve every live upstream without creating keys, Releases, commits, or pushes:

```fish
env PYTHONDONTWRITEBYTECODE=1 python3 scripts/update-chromium-extensions.py --dry-run
```

The publishing mode is intended for GitHub Actions because it requires `contents: write`, a clean checkout of `main`, and enabled Immutable Releases. The workflow validates immutability on each Release instead of reading the repository setting because the latter requires an Administration-scoped token that GitHub Actions does not provide.
