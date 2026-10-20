# Context Map

## Contexts

- [Chromium Extension Update](./update-chromium-extensions/GLOSSARY.md): resolves upstream extension sources into immutable artifacts and a generated extension lock.
- [微信安装包镜像](./update-wechat/GLOSSARY.md)：保存微信 Linux 官方安装包的历史内容，并提供各架构的最新镜像索引。
- **Nix Cache Orchestration**: coordinates public builds and tests of private Nix configurations through [its workflow](./.github/workflows/build-nix-cache.yml). It has no separate glossary because this repository currently owns only the orchestration entry point.

## Relationships

- The contexts are operationally independent. They share this public CI repository but do not exchange domain data or invoke one another.
