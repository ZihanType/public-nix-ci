#!/usr/bin/env python3
"""Resolve extensions.jsonc, publish immutable CRXs, and update extensions.lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from chromium_extensions.pipeline import PipelineOptions, run_pipeline


# This entry point lives directly under the component's scripts directory.
# Keep component-owned inputs separate from the enclosing Git repository root.
COMPONENT_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = COMPONENT_ROOT.parent


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=COMPONENT_ROOT / "extensions.jsonc")
    parser.add_argument("--lock", type=Path, default=COMPONENT_ROOT / "extensions.lock")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "ZihanType/public-nix-ci"),
        help="owner/repository that owns the immutable extension releases",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="branch that receives generated key and lock commits",
    )
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and verify everything without keys, releases, commits, or pushes",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        run_pipeline(
            PipelineOptions(
                component_root=COMPONENT_ROOT,
                repository_root=REPOSITORY_ROOT,
                catalog_path=arguments.catalog.resolve(),
                lock_path=arguments.lock.resolve(),
                repository=arguments.repository,
                branch=arguments.branch,
                github_token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
                openssl=arguments.openssl,
                dry_run=arguments.dry_run,
            )
        )
    except Exception as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
