"""End-to-end resolution, immutable publication, and generated Git history."""

from __future__ import annotations

import base64
import concurrent.futures
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
from typing import List, Mapping, Optional, Sequence, Tuple
import unicodedata
import urllib.parse

from .crx3 import (
    build_reproducible_crx3,
    canonicalize_extension_zip,
    generate_rsa_private_key,
    sha256_hex,
)
from .github_repository import GitHubRepository, ReleaseArtifact, release_download_url
from .model import Catalog, GitHubReleaseSource, LockChange, LockEntry, diff_locks, read_lock, render_lock
from .sources import ChromeWebStoreClient, HttpClient, PublicGitHubReleaseClient


GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# A loose Git ref is created through a sibling `<ref>.lock` file. Capping the
# complete tag at 250 UTF-8 bytes keeps both names within the common 255-byte
# filesystem component limit while retaining Unicode extension names.
MAX_GIT_TAG_BYTES = 250
RELEASE_TAG_PREFIX = "extension-"
RELEASE_TAG_VERSION_SEPARATOR = "-v"


def sha256_sri(contents: bytes) -> str:
    digest = hashlib.sha256(contents).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class PendingKey:
    source_name: str
    final_path: Path
    contents: bytes

    @property
    def commit_message(self) -> str:
        return "chore(extensions): add signing key for %s" % self.source_name


@dataclass(frozen=True)
class ResolvedArtifact:
    lock_entry: LockEntry
    release: ReleaseArtifact


@dataclass(frozen=True)
class Resolution:
    artifacts: Tuple[ResolvedArtifact, ...]
    pending_keys: Tuple[PendingKey, ...]

    @property
    def lock_entries(self) -> Tuple[LockEntry, ...]:
        return tuple(sorted((artifact.lock_entry for artifact in self.artifacts), key=lambda entry: entry.extension_id))


def _validate_release_tag_collisions(artifacts: Sequence[ResolvedArtifact]) -> None:
    artifacts_by_tag = {}
    for artifact in artifacts:
        previous = artifacts_by_tag.get(artifact.release.tag)
        if previous is not None and (
            previous.lock_entry.extension_id != artifact.lock_entry.extension_id
        ):
            raise ValueError(
                "resolved release tag collision %s: %s (%s) and %s (%s)"
                % (
                    artifact.release.tag,
                    previous.lock_entry.name,
                    previous.lock_entry.extension_id,
                    artifact.lock_entry.name,
                    artifact.lock_entry.extension_id,
                )
            )
        artifacts_by_tag[artifact.release.tag] = artifact


def _release_name_slug(name: str, version: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    pieces: List[str] = []
    previous_was_separator = False
    for char in normalized:
        if char.isalnum():
            pieces.append(char)
            previous_was_separator = False
        elif pieces and not previous_was_separator:
            pieces.append("-")
            previous_was_separator = True
    slug = "".join(pieces).strip("-")
    if not slug:
        raise ValueError("extension name tag slug is empty for %r" % name)

    suffix = RELEASE_TAG_VERSION_SEPARATOR + version
    available_slug_bytes = MAX_GIT_TAG_BYTES - len(
        (RELEASE_TAG_PREFIX + suffix).encode("utf-8")
    )
    if available_slug_bytes <= 0:
        raise ValueError("extension version leaves no room for a release tag name slug")
    rendered: List[str] = []
    rendered_bytes = 0
    for char in slug:
        encoded_length = len(char.encode("utf-8"))
        if rendered_bytes + encoded_length > available_slug_bytes:
            break
        rendered.append(char)
        rendered_bytes += encoded_length
    truncated = "".join(rendered).rstrip("-")
    if not truncated:
        raise ValueError("extension name tag slug is empty after length limiting for %r" % name)
    return truncated


def _tag_from_existing_lock(entry: LockEntry) -> str:
    path_parts = urllib.parse.urlsplit(entry.url).path.split("/")
    try:
        marker = path_parts.index("download")
        encoded_tag = path_parts[marker + 1]
    except (ValueError, IndexError) as error:
        raise ValueError("lock URL has no GitHub Release tag for %s" % entry.extension_id) from error
    tag = urllib.parse.unquote(encoded_tag)
    if not tag:
        raise ValueError("lock URL has an empty GitHub Release tag for %s" % entry.extension_id)
    return tag


def _validate_git_tag(tag: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", "refs/tags/" + tag],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git check-ref-format rejected it"
        raise ValueError("invalid generated Git tag %r: %s" % (tag, detail))


def _release_identity(
    name: str,
    extension_id: str,
    version: str,
    *,
    existing: Optional[LockEntry] = None,
) -> Tuple[str, str, str]:
    if existing is not None and existing.extension_id != extension_id:
        raise ValueError("existing lock entry does not match resolved extension ID")
    if existing is not None and existing.version == version:
        # Immutable historical ID tags remain the permanent URL for versions
        # already present in the lock. Only a future version adopts name tags.
        tag = _tag_from_existing_lock(existing)
    else:
        slug = _release_name_slug(name, version)
        tag = RELEASE_TAG_PREFIX + slug + RELEASE_TAG_VERSION_SEPARATOR + version
    if len(tag.encode("utf-8")) > MAX_GIT_TAG_BYTES:
        raise ValueError("generated Git tag exceeds the %d-byte limit" % MAX_GIT_TAG_BYTES)
    _validate_git_tag(tag)
    title = "%s v%s" % (name, version)
    asset_name = "%s-%s.crx" % (extension_id, version)
    return tag, title, asset_name


def _web_store_notes(
    *,
    extension_id: str,
    version: str,
    chrome_version: str,
    request_parameters: Mapping[str, str],
    source_url: str,
    crx_contents: bytes,
) -> str:
    rendered_parameters = "\n".join(
        "  - `%s=%s`" % (key, request_parameters[key]) for key in sorted(request_parameters)
    )
    return """# Chrome Web Store extension artifact

- Extension ID: `{extension_id}`
- Manifest version: `{version}`
- Acquisition client: Chrome for Testing Stable `{chrome_version}`
- Original download URL: `{source_url}`
- CRX SHA-256: `sha256:{crx_sha256}`
- Request parameters:
{rendered_parameters}

The upstream-signed CRX is mirrored byte-for-byte. Browser installation and runtime compatibility are outside this repository's scope.
""".format(
        extension_id=extension_id,
        version=version,
        chrome_version=chrome_version,
        source_url=source_url,
        crx_sha256=sha256_hex(crx_contents),
        rendered_parameters=rendered_parameters,
    )


def _github_notes(
    *,
    source: GitHubReleaseSource,
    upstream_tag: str,
    upstream_asset_name: str,
    upstream_asset_url: str,
    upstream_zip: bytes,
    source_root: str,
    canonical_zip: bytes,
    public_key_fingerprint: str,
    extension_id: str,
    version: str,
    crx_contents: bytes,
    signing_key_repository_path: Path,
) -> str:
    return """# Re-signed GitHub Release extension artifact

- Upstream repository: `{repository}`
- Upstream release tag: `{upstream_tag}`
- Upstream asset: [`{upstream_asset_name}`]({upstream_asset_url})
- Upstream ZIP SHA-256: `sha256:{upstream_sha256}`
- Extension root inside ZIP: `{source_root}`
- Canonical ZIP SHA-256: `sha256:{canonical_sha256}`
- Signing public-key fingerprint: `{public_key_fingerprint}`
- Derived extension ID: `{extension_id}`
- Manifest version: `{version}`
- CRX SHA-256: `sha256:{crx_sha256}`

The signing private key is deliberately public under `{signing_key_repository_path}`; the signature provides a stable extension ID, not confidential publisher authentication.
""".format(
        repository=source.repository,
        upstream_tag=upstream_tag,
        upstream_asset_name=upstream_asset_name,
        upstream_asset_url=upstream_asset_url,
        upstream_sha256=sha256_hex(upstream_zip),
        source_root=source_root,
        canonical_sha256=sha256_hex(canonical_zip),
        public_key_fingerprint=public_key_fingerprint,
        extension_id=extension_id,
        version=version,
        crx_sha256=sha256_hex(crx_contents),
        signing_key_repository_path=signing_key_repository_path.as_posix(),
    )


class Resolver:
    def __init__(
        self,
        component_root: Path,
        repository_root: Path,
        repository: str,
        *,
        github_token: Optional[str] = None,
        openssl: str = "openssl",
        workers: int = 8,
    ) -> None:
        self.component_root = component_root
        self.repository_root = repository_root
        self.repository = repository
        self.openssl = openssl
        self.workers = workers
        self.http = HttpClient(github_token=github_token)
        self.web_store = ChromeWebStoreClient(self.http, openssl=openssl)
        self.public_github = PublicGitHubReleaseClient(self.http)

    def _resolve_web_store(
        self,
        extension_id: str,
        chrome_version: str,
        existing: Optional[LockEntry],
    ) -> ResolvedArtifact:
        upstream = self.web_store.resolve(extension_id, chrome_version)
        tag, title, asset_name = _release_identity(
            upstream.name, extension_id, upstream.version, existing=existing
        )
        release = ReleaseArtifact(
            tag,
            title,
            asset_name,
            upstream.contents,
            _web_store_notes(
                extension_id=extension_id,
                version=upstream.version,
                chrome_version=upstream.chrome_version,
                request_parameters=upstream.request_parameters,
                source_url=upstream.source_url,
                crx_contents=upstream.contents,
            ),
        )
        lock_entry = LockEntry(
            upstream.name,
            extension_id,
            upstream.version,
            existing.url
            if existing is not None and existing.version == upstream.version
            else release_download_url(self.repository, tag, asset_name),
            sha256_sri(upstream.contents),
        )
        return ResolvedArtifact(lock_entry, release)

    def _resolve_github(
        self,
        source: GitHubReleaseSource,
        temporary_directory: Path,
        existing_by_id: Mapping[str, LockEntry],
    ) -> Tuple[ResolvedArtifact, Optional[PendingKey]]:
        upstream = self.public_github.resolve_zip(source)
        canonical = canonicalize_extension_zip(upstream.contents)
        final_key_path = self.component_root / source.key_path
        pending_key: Optional[PendingKey] = None
        if final_key_path.exists():
            key_path = final_key_path
        else:
            key_path = temporary_directory / (source.name + ".pem")
            generate_rsa_private_key(key_path, openssl=self.openssl)
            pending_key = PendingKey(source.name, final_key_path, key_path.read_bytes())

        built = build_reproducible_crx3(canonical.zip_bytes, key_path, openssl=self.openssl)
        existing = existing_by_id.get(built.extension_id)
        tag, title, asset_name = _release_identity(
            canonical.name,
            built.extension_id,
            canonical.version,
            existing=existing,
        )
        release = ReleaseArtifact(
            tag,
            title,
            asset_name,
            built.contents,
            _github_notes(
                source=source,
                upstream_tag=upstream.release_tag,
                upstream_asset_name=upstream.asset_name,
                upstream_asset_url=upstream.asset_url,
                upstream_zip=upstream.contents,
                source_root=canonical.source_root,
                canonical_zip=canonical.zip_bytes,
                public_key_fingerprint=built.public_key_fingerprint,
                extension_id=built.extension_id,
                version=canonical.version,
                crx_contents=built.contents,
                signing_key_repository_path=final_key_path.relative_to(self.repository_root),
            ),
        )
        lock_entry = LockEntry(
            canonical.name,
            built.extension_id,
            canonical.version,
            existing.url
            if existing is not None and existing.version == canonical.version
            else release_download_url(self.repository, tag, asset_name),
            sha256_sri(built.contents),
        )
        return ResolvedArtifact(lock_entry, release), pending_key

    def resolve(
        self,
        catalog: Catalog,
        temporary_directory: Path,
        existing_lock: Sequence[LockEntry] = (),
    ) -> Resolution:
        existing_by_id = {entry.extension_id: entry for entry in existing_lock}
        chrome_version = (
            self.web_store.latest_stable_chrome_version()
            if catalog.chrome_web_store_ids
            else ""
        )
        artifacts: List[ResolvedArtifact] = []
        pending_keys: List[PendingKey] = []
        task_count = len(catalog.chrome_web_store_ids) + len(catalog.github_releases)
        if task_count:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.workers, task_count)
            ) as executor:
                futures = []
                for extension_id in catalog.chrome_web_store_ids:
                    futures.append(
                        (
                            "Chrome Web Store %s" % extension_id,
                            executor.submit(
                                self._resolve_web_store,
                                extension_id,
                                chrome_version,
                                existing_by_id.get(extension_id),
                            ),
                        )
                    )
                for source in catalog.github_releases:
                    futures.append(
                        (
                            "GitHub Release %s" % source.name,
                            executor.submit(
                                self._resolve_github,
                                source,
                                temporary_directory,
                                existing_by_id,
                            ),
                        )
                    )
                for label, future in futures:
                    try:
                        result = future.result()
                    except Exception as error:
                        raise RuntimeError("failed to resolve %s: %s" % (label, error)) from error
                    if isinstance(result, tuple):
                        artifact, pending_key = result
                        artifacts.append(artifact)
                        if pending_key is not None:
                            pending_keys.append(pending_key)
                    else:
                        artifacts.append(result)

        ids = [artifact.lock_entry.extension_id for artifact in artifacts]
        duplicate_ids = sorted({extension_id for extension_id in ids if ids.count(extension_id) > 1})
        if duplicate_ids:
            raise ValueError("resolved duplicate extension IDs: %s" % ", ".join(duplicate_ids))

        _validate_release_tag_collisions(artifacts)
        return Resolution(
            tuple(sorted(artifacts, key=lambda artifact: artifact.lock_entry.extension_id)),
            tuple(sorted(pending_keys, key=lambda key: key.source_name)),
        )


class GitWorkingTree:
    def __init__(self, repository_root: Path, *, branch: str = "main") -> None:
        self.repository_root = repository_root
        self.branch = branch

    def _run(self, arguments: Sequence[str], *, capture: bool = False) -> str:
        result = subprocess.run(
            ["git"] + list(arguments),
            cwd=str(self.repository_root),
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "git %s failed: %s"
                % (" ".join(arguments), result.stderr.strip())
            )
        return result.stdout.strip() if capture else ""

    def configure_bot(self) -> None:
        self._run(["config", "user.name", "github-actions[bot]"])
        self._run(
            [
                "config",
                "user.email",
                "41898282+github-actions[bot]@users.noreply.github.com",
            ]
        )

    def head(self) -> str:
        return self._run(["rev-parse", "HEAD"], capture=True)

    def commit_file(self, path: Path, message: str) -> None:
        relative = path.relative_to(self.repository_root)
        self._run(["add", "--", str(relative)])
        self._run(["commit", "-m", message, "--", str(relative)])

    def push(self) -> None:
        self._run(["push", "origin", "HEAD:%s" % self.branch])


def _persist_pending_keys(
    pending_keys: Sequence[PendingKey],
    git: GitWorkingTree,
) -> None:
    if not pending_keys:
        return
    for pending_key in pending_keys:
        if pending_key.final_path.exists():
            raise RuntimeError("signing key appeared concurrently: %s" % pending_key.final_path)
        pending_key.final_path.parent.mkdir(parents=True, exist_ok=True)
        pending_key.final_path.write_bytes(pending_key.contents)
        pending_key.final_path.chmod(0o600)
        git.commit_file(pending_key.final_path, pending_key.commit_message)
    git.push()


def _commit_lock_changes(
    lock_path: Path,
    old_entries: Sequence[LockEntry],
    desired_entries: Sequence[LockEntry],
    changes: Sequence[LockChange],
    git: GitWorkingTree,
) -> None:
    if not changes:
        return
    current = {entry.extension_id: entry for entry in old_entries}
    for change in changes:
        if change.new is None:
            current.pop(change.extension_id)
        else:
            current[change.extension_id] = change.new
        lock_path.write_text(render_lock(current.values()), encoding="utf-8")
        git.commit_file(lock_path, change.commit_message)
    final_contents = render_lock(desired_entries)
    if lock_path.read_text(encoding="utf-8") != final_contents:
        raise RuntimeError("incremental lock commits did not produce the desired lock")
    git.push()


@dataclass(frozen=True)
class PipelineOptions:
    component_root: Path
    repository_root: Path
    catalog_path: Path
    lock_path: Path
    repository: str
    branch: str
    github_token: Optional[str]
    openssl: str = "openssl"
    dry_run: bool = False


def run_pipeline(options: PipelineOptions) -> Tuple[LockChange, ...]:
    catalog = Catalog.read(options.catalog_path)
    current_lock = read_lock(options.lock_path)
    if not GITHUB_REPOSITORY_RE.fullmatch(options.repository):
        raise ValueError("invalid target GitHub repository: %s" % options.repository)

    with tempfile.TemporaryDirectory(prefix="chromium-extensions-") as directory:
        resolver = Resolver(
            options.component_root,
            options.repository_root,
            options.repository,
            github_token=options.github_token,
            openssl=options.openssl,
        )
        resolution = resolver.resolve(catalog, Path(directory), current_lock)
        changes = diff_locks(current_lock, resolution.lock_entries)

        if options.dry_run:
            for change in changes:
                print(change.action_log)
            print(
                "dry run resolved %d extensions (%d changes, %d new keys)"
                % (len(resolution.artifacts), len(changes), len(resolution.pending_keys))
            )
            return changes

        if not options.github_token:
            raise ValueError("GITHUB_TOKEN or GH_TOKEN is required for publication")
        github = GitHubRepository(options.repository, options.github_token)

        # This is the non-mutating release preflight. It detects tag/asset
        # conflicts and rejects mutable published releases before persisting a
        # new signing identity. The Actions token cannot read the repository's
        # Administration-scoped immutable-release setting directly.
        for artifact in resolution.artifacts:
            github.validate_existing(artifact.release)

        git = GitWorkingTree(options.repository_root, branch=options.branch)
        git.configure_bot()
        _persist_pending_keys(resolution.pending_keys, git)

        target_commitish = git.head()
        for artifact in resolution.artifacts:
            github.publish(artifact.release, target_commitish=target_commitish)

        _commit_lock_changes(
            options.lock_path,
            current_lock,
            resolution.lock_entries,
            changes,
            git,
        )
        for change in changes:
            print(change.action_log)
        print(
            "resolved %d extensions; published artifacts and committed %d lock changes"
            % (len(resolution.artifacts), len(changes))
        )
        return changes
