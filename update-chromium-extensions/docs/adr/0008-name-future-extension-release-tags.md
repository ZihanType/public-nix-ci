---
status: superseded by ADR-0009
---

# Name future extension release tags

New extension versions use the normalized manifest name in `extension-<name-slug>-v<version>` tags and Release titles so generated updates identify extensions for humans; ID-based asset names and technical metadata remain stable identities, while empty or colliding name tags fail atomically. Versions already recorded in the extension lock retain their immutable ID-based tags and URLs, so both formats deliberately coexist without republishing history. This partially supersedes ADR-0003's tag naming decision while preserving its immutable, versioned artifact and ID-based asset decisions.
