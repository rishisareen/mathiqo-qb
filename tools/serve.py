#!/usr/bin/env python3
"""U6 terminal session runner CLI (core design doc §5.2 "Session runner").

Matches the repo's tools-as-CLIs convention (KTD1: chosen over a minimal
local web page). All the loop logic — render, time, hint, parse, feedback,
log — lives in `orchestrator/session.py`, testable there with an injected
`InputSource`; this file is wiring only: parse arguments, load content/bank,
replay prior state, run one session, print a summary.

Usage:
    python tools/serve.py --bank banks/2026.07-dev --student stu-1 \
        --country IN --curriculum NCERT --grade 6 --log logs/stu-1.jsonl \
        [--session-time 2026-07-27T12:00:00]
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator import certification, engine  # noqa: E402
from orchestrator import log as log_module  # noqa: E402
from orchestrator import serving, session  # noqa: E402

DEFAULT_CONTENT_DIR = REPO_ROOT / "content" / "fractions"
DEFAULT_BANKS_DIR = REPO_ROOT / "banks"


def resolve_bank(bank_override: "Path | None", banks_dir: Path) -> engine.BankIndex:
    """U6: `--bank` stays an explicit override (a specific already-built
    bank directory); when omitted, resolves via `banks_dir`'s ACTIVE
    pointer (KTD3) — this CLI's own adoption of the same registry the
    facade/MCP surface use. Raises `serving.NoActiveBankError` (point 5)
    when no override is given and the pointer is absent or stale."""
    if bank_override is not None:
        return engine.load_bank(bank_override)
    version = serving.read_active_bank_version(banks_dir)
    return engine.load_bank(banks_dir / version)


def _print_state_summary(store, student_id: str, now: datetime) -> None:
    """Approach point 1: "print a short state summary (e.g. which skills are
    at their frontier, which reviews are due)"."""
    records = store.records_for(student_id)
    if not records:
        print("No prior history for this student — starting fresh (placement).")
        return
    due = certification.due_reviews(records, now)
    uncertified = sorted(sid for sid, s in records.items() if s.certified_depth is None)
    certified = sorted(sid for sid, s in records.items() if s.certified_depth is not None)
    print(f"Prior history: {len(records)} skill(s) touched.")
    if certified:
        print(f"  Certified: {', '.join(certified)}")
    if uncertified:
        print(f"  At frontier (uncertified): {', '.join(uncertified)}")
    if due:
        print(f"  Reviews due: {', '.join(sid for sid, _ in due)}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive terminal session runner (U6).")
    parser.add_argument(
        "--bank", default=None, type=Path,
        help="explicit bank directory override (tools/bank_build.py output); omit to resolve via --banks-dir's ACTIVE pointer",
    )
    parser.add_argument(
        "--banks-dir", default=DEFAULT_BANKS_DIR, type=Path,
        help=f"banks directory with an ACTIVE pointer (tools/bank_activate.py); used when --bank is omitted (default {DEFAULT_BANKS_DIR})",
    )
    parser.add_argument("--student", required=True, help="pseudonymous student id")
    parser.add_argument("--country", required=True, help="country code, e.g. IN or CA")
    parser.add_argument("--curriculum", required=True, help="curriculum identifier, e.g. NCERT or ONTARIO")
    parser.add_argument("--grade", required=True, type=int, help="grade level")
    parser.add_argument("--log", required=True, type=Path, help="attempt-event JSONL log path (appended to; replayed at startup)")
    parser.add_argument(
        "--session-time",
        default=None,
        help="ISO 8601 timestamp used as this session's single 'now' (KTD10); default is real wall-clock time",
    )
    args = parser.parse_args(argv)

    if args.bank is not None and not args.bank.is_dir():
        print(f"error: bank directory {args.bank} does not exist", file=sys.stderr)
        return 2
    if not DEFAULT_CONTENT_DIR.is_dir():
        print(f"error: content directory {DEFAULT_CONTENT_DIR} does not exist", file=sys.stderr)
        return 2

    now = log_module.parse_timestamp(args.session_time) if args.session_time else datetime.now()

    content = engine.load_content(DEFAULT_CONTENT_DIR)
    try:
        bank = resolve_bank(args.bank, args.banks_dir)
    except serving.NoActiveBankError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # One pass over the log for both the store and engine state — see
    # rebuild_session_state's docstring on why this replaces the equivalent
    # (and, at real usage scale, doubly expensive) log.replay() +
    # rebuild_engine_state() pair.
    try:
        store, engine_state = engine.rebuild_session_state(
            args.log, content, engine.make_static_bank_lookup(bank)
        )
    except log_module.AttemptLogError as exc:
        print(f"error: attempt log {args.log} could not be replayed: {exc}", file=sys.stderr)
        return 2

    session_id = f"sess-{uuid.uuid4().hex[:8]}"
    io = session.ThreadedStdinInputSource()

    def notify(text: str) -> None:
        print(text)

    print(
        f"Session {session_id} for student {args.student!r} "
        f"({args.country}, {args.curriculum}, grade {args.grade}) — now={now.isoformat()}"
    )
    _print_state_summary(store, args.student, now)

    summary = session.run_session(
        store, engine_state, content, bank, args.student, args.country, args.curriculum, args.grade,
        session_id, args.log, io, notify, now,
    )

    print("\n--- Session summary ---")
    print(f"Items served: {summary.items_served}")
    for skill_id, (before, after) in sorted(summary.theta_deltas.items()):
        print(f"  {skill_id}: theta {before:.3f} -> {after:.3f} ({after - before:+.3f})")
    if summary.certifications_earned:
        print(f"Certified this session: {', '.join(summary.certifications_earned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
