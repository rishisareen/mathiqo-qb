#!/usr/bin/env python3
"""Activate (or roll back to) a built bank version (KTD3).

Atomically writes an `ACTIVE` pointer file in the banks directory naming
the bank_version new sessions should pin to (`orchestrator/serving.py`'s
`read_active_bank_version`). A session already open keeps whatever build it
originally pinned (R4) — this tool only ever affects sessions opened after
it runs.

Refuses to point at a bank_version that isn't on disk under the banks
directory: a domain error (exit 1), distinct from a malformed invocation
(exit 2, argparse's own convention) — mirrors tools/bank_build.py's
BuildError exit-code discipline and stage2_draft.py's staging-dir approach
to atomic writes (temp file + rename in the same directory, so the
replacement is a single filesystem operation and readers never observe a
half-written pointer).

Usage:
    python tools/bank_activate.py --banks banks --bank-version 2026.07-dev
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.serving import ACTIVE_POINTER_NAME  # noqa: E402


class ActivationError(Exception):
    """A domain-level refusal (exit 1) — the target build isn't on disk."""


def activate(banks_dir: Path, bank_version: str) -> Path:
    """Point `banks_dir/ACTIVE` at `bank_version`, refusing (ActivationError)
    if `banks_dir/<bank_version>/` doesn't exist. Atomic: write a temp file
    in `banks_dir`, then `os.replace` it onto the pointer's final name — the
    pointer is never observed half-written or missing mid-update."""
    target = banks_dir / bank_version
    if not target.is_dir():
        raise ActivationError(f"bank version {bank_version!r} is not on disk at {target} — refusing to activate it")

    pointer = banks_dir / ACTIVE_POINTER_NAME
    tmp = banks_dir / f".{ACTIVE_POINTER_NAME}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(bank_version, encoding="utf-8")
    os.replace(tmp, pointer)
    return pointer


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--banks", required=True, type=Path, help="banks directory (tools/bank_build.py's --out)")
    parser.add_argument("--bank-version", required=True, help="the built version to activate")
    args = parser.parse_args(argv)

    if not args.banks.is_dir():
        print(f"error: banks directory {args.banks} does not exist", file=sys.stderr)
        return 2

    try:
        pointer = activate(args.banks, args.bank_version)
    except ActivationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"activated {args.bank_version} -> {pointer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
