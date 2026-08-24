"""Validated catalog and lock-file models.

The catalog is deliberately convenient for humans (JSONC), while the lock is
deliberately strict and deterministic for Nix consumers.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import unicodedata


EXTENSION_ID_RE = re.compile(r"[a-p]{32}")
CATALOG_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")
SRI_SHA256_RE = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")

# Chrome's manifest `name` contract limits the user-visible extension name to
# 75 characters. Keeping the limit here makes generated lock entries obey the
# same contract even when they are constructed outside the archive parser.
MAX_EXTENSION_NAME_CHARACTERS = 75

# Unicode Bidirectional Algorithm controls can make an untrusted upstream name
# appear different from the text committed to Git or printed in Actions. These
# are the bidi controls defined by UAX #9, including the isolate controls.
BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def normalize_extension_name(value: object) -> str:
    """Return the single-line display name shared by every human-facing surface."""

    if not isinstance(value, str):
        raise ValueError("extension manifest name must be a string")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ValueError("extension manifest name contains a control character")
    if any(char in BIDI_CONTROL_CHARACTERS for char in value):
        raise ValueError("extension manifest name contains a bidi formatting control")

    # Chromium's Extension::LoadName collapses whitespace before exposing the
    # name. Reproducing that normalization keeps lock, Git, Actions, and Release
    # labels aligned with the browser-visible value.
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("extension manifest name must not be empty")
    if len(normalized) > MAX_EXTENSION_NAME_CHARACTERS:
        raise ValueError(
            "extension manifest name exceeds the %d-character limit"
            % MAX_EXTENSION_NAME_CHARACTERS
        )
    return normalized


def _remove_jsonc_comments(text: str) -> str:
    """Replace comments with whitespace while preserving error line numbers."""

    rendered: List[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if in_string:
            rendered.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            rendered.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            rendered.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                rendered.append(" ")
                index += 1
            continue

        if char == "/" and next_char == "*":
            rendered.extend((" ", " "))
            index += 2
            while index < len(text):
                if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    rendered.extend((" ", " "))
                    index += 2
                    break
                rendered.append(text[index] if text[index] in "\r\n" else " ")
                index += 1
            else:
                raise ValueError("unterminated JSONC block comment")
            continue

        rendered.append(char)
        index += 1

    if in_string:
        # json.loads will provide the precise line and column, but this message
        # is clearer than letting a comment marker near EOF obscure the cause.
        raise ValueError("unterminated JSON string")
    return "".join(rendered)


def _remove_trailing_commas(text: str) -> str:
    rendered = list(text)
    index = 0
    in_string = False
    escaped = False
    while index < len(rendered):
        char = rendered[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(rendered) and rendered[lookahead].isspace():
                lookahead += 1
            if lookahead < len(rendered) and rendered[lookahead] in "]}":
                rendered[index] = " "
        index += 1
    return "".join(rendered)


def parse_jsonc(text: str) -> object:
    """Parse JSON with line/block comments and trailing commas, but no JSON5."""

    return json.loads(_remove_trailing_commas(_remove_jsonc_comments(text)))


@dataclass(frozen=True)
class GitHubReleaseSource:
    name: str
    repository: str
    asset_glob: Optional[str]

    @property
    def key_path(self) -> Path:
        return Path("keys") / (self.name + ".pem")


@dataclass(frozen=True)
class Catalog:
    chrome_web_store_ids: Tuple[str, ...]
    github_releases: Tuple[GitHubReleaseSource, ...]

    @classmethod
    def read(cls, path: Path) -> "Catalog":
        try:
            value = parse_jsonc(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("failed to parse extension catalog %s: %s" % (path, error)) from error
        if not isinstance(value, dict):
            raise ValueError("extension catalog must be an object")

        allowed_top_level = {"$schema", "chromeWebStore", "githubReleases"}
        unexpected = sorted(set(value) - allowed_top_level)
        if unexpected:
            raise ValueError("unsupported catalog properties: %s" % ", ".join(unexpected))
        if "chromeWebStore" not in value or "githubReleases" not in value:
            raise ValueError("catalog requires chromeWebStore and githubReleases")

        web_store_value = value["chromeWebStore"]
        if not isinstance(web_store_value, list):
            raise ValueError("chromeWebStore must be an array")
        web_store_ids: List[str] = []
        seen_web_store_ids = set()
        for index, extension_id in enumerate(web_store_value):
            if not isinstance(extension_id, str) or not EXTENSION_ID_RE.fullmatch(extension_id):
                raise ValueError("chromeWebStore[%d] is not a valid extension ID" % index)
            if extension_id in seen_web_store_ids:
                raise ValueError("duplicate Chrome Web Store extension ID: %s" % extension_id)
            seen_web_store_ids.add(extension_id)
            web_store_ids.append(extension_id)

        github_value = value["githubReleases"]
        if not isinstance(github_value, dict):
            raise ValueError("githubReleases must be an object")
        github_releases: List[GitHubReleaseSource] = []
        for name in sorted(github_value):
            source = github_value[name]
            if not isinstance(name, str) or not CATALOG_NAME_RE.fullmatch(name):
                raise ValueError("invalid GitHub source name: %r" % name)
            if not isinstance(source, dict):
                raise ValueError("GitHub source %s must be an object" % name)
            unexpected_source = sorted(set(source) - {"repository", "asset"})
            if unexpected_source:
                raise ValueError(
                    "GitHub source %s has unsupported properties: %s"
                    % (name, ", ".join(unexpected_source))
                )
            repository = source.get("repository")
            if not isinstance(repository, str) or not GITHUB_REPOSITORY_RE.fullmatch(repository):
                raise ValueError("GitHub source %s has an invalid repository" % name)
            asset_glob = source.get("asset")
            if asset_glob is not None:
                if (
                    not isinstance(asset_glob, str)
                    or not asset_glob
                    or "/" in asset_glob
                    or "\\" in asset_glob
                    or not asset_glob.lower().endswith(".zip")
                ):
                    raise ValueError("GitHub source %s has an invalid ZIP asset glob" % name)
            github_releases.append(GitHubReleaseSource(name, repository, asset_glob))

        return cls(tuple(web_store_ids), tuple(github_releases))


@dataclass(frozen=True)
class LockEntry:
    name: str
    extension_id: str
    version: str
    url: str
    sha256: str

    def __post_init__(self) -> None:
        if normalize_extension_name(self.name) != self.name:
            raise ValueError("lock extension name must already be normalized")
        if not EXTENSION_ID_RE.fullmatch(self.extension_id):
            raise ValueError("invalid lock extension ID: %s" % self.extension_id)
        if not VERSION_RE.fullmatch(self.version):
            raise ValueError("invalid manifest version for %s: %s" % (self.extension_id, self.version))
        if not self.url.startswith("https://"):
            raise ValueError("lock URL must use HTTPS for %s" % self.extension_id)
        if not SRI_SHA256_RE.fullmatch(self.sha256):
            raise ValueError("invalid Nix SRI SHA-256 for %s" % self.extension_id)
        try:
            digest = base64.b64decode(self.sha256[len("sha256-") :], validate=True)
        except ValueError as error:
            raise ValueError("invalid base64 SHA-256 for %s" % self.extension_id) from error
        if len(digest) != 32:
            raise ValueError("invalid SHA-256 length for %s" % self.extension_id)

    def as_json(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "id": self.extension_id,
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
        }


def read_lock(path: Path) -> Tuple[LockEntry, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("failed to parse extension lock %s: %s" % (path, error)) from error
    if not isinstance(value, list):
        raise ValueError("extension lock must be an array")

    entries: List[LockEntry] = []
    seen_ids = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("lock entry %d must be an object" % index)
        if set(item) != {"name", "id", "version", "url", "sha256"}:
            raise ValueError(
                "lock entry %d must contain exactly name, id, version, url, and sha256"
                % index
            )
        if not all(isinstance(item[field], str) for field in item):
            raise ValueError("lock entry %d contains a non-string field" % index)
        entry = LockEntry(
            item["name"], item["id"], item["version"], item["url"], item["sha256"]
        )
        if entry.extension_id in seen_ids:
            raise ValueError("duplicate extension ID in lock: %s" % entry.extension_id)
        seen_ids.add(entry.extension_id)
        entries.append(entry)

    sorted_entries = sorted(entries, key=lambda entry: entry.extension_id)
    if entries != sorted_entries:
        raise ValueError("extension lock entries must be sorted by ID")
    return tuple(entries)


def render_lock(entries: Iterable[LockEntry]) -> str:
    ordered = sorted(entries, key=lambda entry: entry.extension_id)
    return json.dumps([entry.as_json() for entry in ordered], indent=2, ensure_ascii=False) + "\n"


class ChangeKind(Enum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass(frozen=True)
class LockChange:
    kind: ChangeKind
    extension_id: str
    old: Optional[LockEntry]
    new: Optional[LockEntry]

    @property
    def commit_message(self) -> str:
        if self.kind is ChangeKind.ADD:
            assert self.new is not None
            return "chore(chromium): add %s %s" % (self.new.name, self.new.version)
        if self.kind is ChangeKind.UPDATE:
            assert self.old is not None and self.new is not None
            return "chore(chromium): update %s to %s" % (self.new.name, self.new.version)
        assert self.old is not None
        return "chore(chromium): remove %s" % self.old.name

    @property
    def action_log(self) -> str:
        if self.kind is ChangeKind.ADD:
            assert self.new is not None
            return "Added %s: %s" % (self.new.name, self.new.version)
        if self.kind is ChangeKind.UPDATE:
            assert self.old is not None and self.new is not None
            if self.old.name == self.new.name:
                label = self.new.name
            else:
                label = "%s -> %s" % (self.old.name, self.new.name)
            return "Updated %s: %s -> %s" % (
                label,
                self.old.version,
                self.new.version,
            )
        assert self.old is not None
        return "Removed %s: %s" % (self.old.name, self.old.version)


def diff_locks(old: Sequence[LockEntry], new: Sequence[LockEntry]) -> Tuple[LockChange, ...]:
    old_by_id = {entry.extension_id: entry for entry in old}
    new_by_id = {entry.extension_id: entry for entry in new}
    changes: List[LockChange] = []

    for extension_id in sorted(set(new_by_id) - set(old_by_id)):
        changes.append(LockChange(ChangeKind.ADD, extension_id, None, new_by_id[extension_id]))
    for extension_id in sorted(set(new_by_id) & set(old_by_id)):
        old_entry = old_by_id[extension_id]
        new_entry = new_by_id[extension_id]
        if old_entry == new_entry:
            continue
        if old_entry.version == new_entry.version:
            raise ValueError(
                "extension %s changed name, content, or URL without changing version %s"
                % (extension_id, old_entry.version)
            )
        changes.append(LockChange(ChangeKind.UPDATE, extension_id, old_entry, new_entry))
    for extension_id in sorted(set(old_by_id) - set(new_by_id)):
        changes.append(LockChange(ChangeKind.REMOVE, extension_id, old_by_id[extension_id], None))
    return tuple(changes)
