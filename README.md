# public-nix-ci

This repository runs public CI work used by private Nix configurations.

## CI features

- [Nix cache orchestration](./.github/workflows/build-nix-cache.yml) builds and tests selected configurations from the private `ZihanType/nix-configs` repository.
- [Chromium extension update component](./update-chromium-extensions/README.md) resolves extension sources into immutable CRX artifacts and a generated lock.

The repository's domain boundaries and their documentation are indexed in [`CONTEXT-MAP.md`](./CONTEXT-MAP.md).
