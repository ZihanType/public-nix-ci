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

Versions and extension names are never entered manually. The workflow reads `manifest.json` from the current Web Store CRX or latest public GitHub Release ZIP. A localized manifest name is resolved through `default_locale`, then normalized once for the lock, generated Git history, Actions logs, and GitHub Releases; an invalid or missing localized name fails the atomic resolution.

For Web Store requests, the client version is the full version from the `Stable` channel in Google's Chrome for Testing metadata. “Chrome for Testing” names the automation-oriented distribution and metadata service; `Stable` still means the Stable channel, not Beta, Dev, or Canary.

### Published artifacts

Every newly resolved extension version uses a human-readable name slug in its permanent tag while retaining the extension ID in the asset name:

```text
tag:   extension-<name-slug>-v<version>
asset: <id>-<version>.crx
```

The Release title is `<name> v<version>`. Name slugs preserve Unicode letters and numbers, normalize punctuation to hyphens, and are capped at 250 UTF-8 bytes before Git ref validation. An empty slug or a collision between different extensions fails the complete resolution. Versions already present in the lock retain their historical `extension-<id>-v<version>` tag and URL; immutable history is never renamed or republished solely to migrate the tag format.

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
