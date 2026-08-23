# Chromium Extension Update

This context describes the extension identities and distribution channels managed by the Chromium extension update component.

## Language

**Chromium extension update component**:
The project capability that resolves both Chrome Web Store and GitHub Release sources into extension artifacts and an extension lock.
_Avoid_: Chrome extension updater, Chrome Web Store updater

**Chrome Web Store extension**:
An extension acquired from the Chrome Web Store and mirrored byte-for-byte, retaining the upstream publisher's signature and extension ID.
_Avoid_: Store extension, official extension

**Re-signed extension**:
An extension whose upstream ZIP archive is packaged and signed with a key owned by this repository. It has a new identity and does not inherit the Chrome Web Store extension's data or grants.
_Avoid_: GitHub extension, converted extension, original extension

**Extension artifact**:
An immutable CRX identified by its extension ID and manifest version and published by this repository for an extension lock to reference.
_Avoid_: Package, release file, download

**Extension catalog**:
The human-maintained `extensions.jsonc` document that declares which upstream extensions the pipeline resolves.
_Avoid_: Extension list, source list, configuration

**Extension lock**:
The generated `extensions.lock` document that records exactly the ID, version, download URL, and SHA-256 hash of every resolved extension artifact. It makes no claim that a browser can install or run the artifact.
_Avoid_: Catalog, installation manifest, browser configuration
