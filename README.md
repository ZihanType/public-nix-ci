# public-nix-ci

This repository runs public CI work used by private Nix configurations.

## CI features

- [Nix cache orchestration](./.github/workflows/build-nix-cache.yml) builds and tests selected configurations from the private `ZihanType/nix-configs` repository.
- [Chromium extension update component](./update-chromium-extensions/README.md) resolves extension sources into immutable CRX artifacts and a generated lock.
- [微信 Linux 安装包镜像](./update-wechat/README.md)每天归档两个架构的 AppImage，并生成包含版本、固定下载地址和 SHA-256 的发布索引。

The repository's domain boundaries and their documentation are indexed in [`GLOSSARY-MAP.md`](./GLOSSARY-MAP.md).
