# Context Map

## Contexts

- [Chromium Extension Update](./update-chromium-extensions/CONTEXT.md): resolves upstream extension sources into immutable artifacts and a generated extension lock.
- **Nix Cache Orchestration**: coordinates public builds and tests of private Nix configurations through [its workflow](./.github/workflows/build-nix-cache.yml). It has no separate glossary because this repository currently owns only the orchestration entry point.

## Relationships

- The contexts are operationally independent. They share this public CI repository but do not exchange domain data or invoke one another.
