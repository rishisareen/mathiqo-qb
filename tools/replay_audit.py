#!/usr/bin/env python3
"""Replay-audit tool (U7): R10's acceptance bar as a runnable check.

For each event in a student's attempt log: resolve its `bank_version`
through KTD7's lookup (any retained build, not only the currently active
one), resolve the instance within that build, and verify the event's
`variant_signature` against the bank's own record for that instance —
reuses `orchestrator/ingest.py`'s `validate_event` (KTD5) rather than
re-implementing "what makes an event resolve" a second time; a rejection
here names the same failing check ingest would have rejected it for at
write time, just discovered after the fact. Then rebuilds the student's
state via `engine.rebuild_session_state` (KTD11) and reports the
reproduced theta/certification/seen-count summary — the exact same shape
and computation `orchestrator/serving.py`'s `get_progress` returns
(`serving.build_progress_summary`), so a replay and a live progress read
are provably the same thing.

Accumulates every failing event (tools/graph_lint.py's own style) rather
than stopping at the first; exits non-zero if any event fails, or zero on
a clean audit.

Usage:
    python tools/replay_audit.py --log logs/stu-1.jsonl --banks-dir banks
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator import engine, ingest, log as log_module, serving  # noqa: E402


class AuditFailure:
    """One event that failed replay-audit — named event_id + reason, the
    same discipline tools/graph_lint.py's own findings use."""

    def __init__(self, event_id: str, reason: str) -> None:
        self.event_id = event_id
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.event_id}: {self.reason}"


def validate_log(log_path: Path, bank_lookup: engine.BankLookup) -> list[AuditFailure]:
    """KTD5's checks, applied after the fact to every event in the log
    (reuses `ingest.validate_event` — no session context, since a
    standalone audit has no submitting session to compare against)."""
    failures: list[AuditFailure] = []
    seen_by_student: dict[str, set] = {}
    for event in log_module.iter_events(log_path):
        student_id = event.get("student_id", "<unknown>")
        seen = seen_by_student.setdefault(student_id, set())
        report = ingest.validate_event(event, None, bank_lookup, seen)
        event_id = event.get("event_id", "<unknown>")
        if not report.accepted:
            failures.append(AuditFailure(event_id, report.rejection_reason or "rejected"))
            continue
        signature = event.get("variant_signature")
        if signature is not None:
            seen.add(signature)
    return failures


def audit_log(
    log_path: Path, content: engine.ContentIndex, bank_lookup: engine.BankLookup, now
) -> "tuple[list[AuditFailure], dict[str, serving.ProgressSummary]]":
    """Full audit: validate every event, then — only if the log validated
    clean, since replay assumes a valid log — rebuild student state and
    report the reproduced progress summary per student touched (R10, AE5).
    A validation failure short-circuits replay entirely: reporting a
    "reproduced" summary computed by replaying past a corrupt event would
    be meaningless, not merely incomplete."""
    failures = validate_log(log_path, bank_lookup)
    if failures:
        return failures, {}

    store, engine_state = engine.rebuild_session_state(log_path, content, bank_lookup)
    student_ids = sorted({event["student_id"] for event in log_module.iter_events(log_path)})
    summaries = {
        student_id: serving.build_progress_summary(store, engine_state, student_id, now)
        for student_id in student_ids
    }
    return failures, summaries


def _print_summary(summaries: "dict[str, serving.ProgressSummary]") -> None:
    for student_id, summary in summaries.items():
        print(f"student {student_id!r}:")
        for skill_id, progress in sorted(summary.skills.items()):
            print(
                f"  {skill_id}: theta={progress.theta:.3f} n={progress.n:.1f} "
                f"certified_depth={progress.certified_depth} status={progress.certification_status} "
                f"seen={progress.seen_count}"
            )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, type=Path, help="attempt-event JSONL log to audit")
    parser.add_argument("--banks-dir", required=True, type=Path, help="banks directory (KTD7 lookup: any retained build)")
    parser.add_argument(
        "--content", type=Path, default=REPO_ROOT / "content" / "fractions",
        help="content directory for the skill graph (default content/fractions)",
    )
    parser.add_argument(
        "--now", default=None,
        help="ISO 8601 timestamp certification status is evaluated against (default: real wall-clock time)",
    )
    args = parser.parse_args(argv)

    if not args.log.is_file():
        print(f"error: log file {args.log} does not exist", file=sys.stderr)
        return 2
    if not args.banks_dir.is_dir():
        print(f"error: banks directory {args.banks_dir} does not exist", file=sys.stderr)
        return 2
    if not args.content.is_dir():
        print(f"error: content directory {args.content} does not exist", file=sys.stderr)
        return 2

    now = log_module.parse_timestamp(args.now) if args.now else datetime.now()
    content = engine.load_content(args.content)
    bank_lookup = engine.make_bank_lookup(args.banks_dir)

    failures, summaries = audit_log(args.log, content, bank_lookup, now)

    if failures:
        print(f"replay audit FAILED: {len(failures)} event(s) did not resolve:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    _print_summary(summaries)
    print("replay audit clean: every event resolved against its stamped build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
