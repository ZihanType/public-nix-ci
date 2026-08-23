# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout

This is a multi-context repo:

- `CONTEXT-MAP.md` at the repository root lists the contexts and where they live.
- Each context keeps its glossary in its own `CONTEXT.md` and feature-specific decisions in its own `docs/adr/`.
- System-wide architecture decisions live under the repository-root `docs/adr/`.

## Before exploring, read these

- **`CONTEXT-MAP.md`** at the repository root.
- The relevant context's **`CONTEXT.md`** and **`docs/adr/`** entries.
- Repository-root **`docs/adr/`** entries that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

```text
/
├── CONTEXT-MAP.md
├── docs/adr/
│   └── 0008-organize-ci-features-by-context.md
└── update-chromium-extensions/
    ├── CONTEXT.md
    └── docs/adr/
        ├── 0001-publish-extension-signing-private-keys.md
        └── 0002-limit-pipeline-to-artifact-resolution.md
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in that context's `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts Chromium Extension Update ADR-0002 (pipeline boundary), but worth reopening because…_
