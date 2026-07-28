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
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator import engine, ingest, log as log_module, serving  # noqa: E402
from orchestrator.identity import IdentityRegistry  # noqa: E402
from orchestrator.state import SkillState  # noqa: E402


class AuditFailure:
    """One event that failed replay-audit — named event_id + reason, the
    same discipline tools/graph_lint.py's own findings use. `kind`
    distinguishes a KTD5 ingest rejection (the original, default kind) from
    a U13 theta-chain break (`kind="theta_chain"`) — both share the same
    accumulate-and-report shape, so callers that only care "did anything
    fail" can ignore `kind` entirely."""

    def __init__(self, event_id: str, reason: str, kind: str = "ingest") -> None:
        self.event_id = event_id
        self.reason = reason
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.event_id}: {self.reason}"


# KTD6: the theta a (student_id, skill_id) pair starts from before any event
# has ever been logged for it — orchestrator/state.py's SkillState() default,
# read here (not hardcoded) so this check tracks that module if its default
# ever changes.
_FRESH_THETA = SkillState().theta


def _check_theta_chain(
    event: dict, last_theta_after: Dict[Tuple[str, str], Tuple[float, str]]
) -> Optional[AuditFailure]:
    """U13: KTD6's theta-interleaving cross-check. For one (student_id,
    skill_id) pair, walked in file order regardless of interleaving with
    other students'/skills' events in between, event N's `theta_before` must
    equal event N-1's `theta_after` for that same pair (the very first event
    for a pair starts from `_FRESH_THETA`). A break is exactly the signature
    two writers racing on the same student (the single-active-session
    protection KTD6 names) would leave: both would read a stale theta and
    stamp a `theta_before` that disagrees with the true sequential
    predecessor.

    Only single-skill events (`len(skill_ids) == 1`) carry an unambiguous
    per-skill theta to chain-check. `orchestrator/serving.py`'s
    `submit_attempt` — the only code path that ever produces a logged event
    today — always calls `compose_attempt` for exactly one skill_id, so
    every real event qualifies; a hypothetical multi-skill event would leave
    `theta_before`/`theta_after` as one scalar pair with no sound way to
    attribute it to any one of several skills' own chains, so such an event
    is skipped here rather than guessed at.

    `last_theta_after` is the caller's running per-(student_id, skill_id)
    tracker of `(theta_after, event_id)` from the last event actually seen
    for that pair — updated unconditionally, even on a mismatch, so one
    broken link is reported once (at the event that actually disagrees with
    its real predecessor) instead of cascading into every later event for
    the same pair."""
    skill_ids = event.get("skill_ids") or []
    if len(skill_ids) != 1:
        return None
    skill_id = skill_ids[0]
    student_id = event.get("student_id", "<unknown>")
    event_id = event.get("event_id", "<unknown>")
    theta_before = event.get("theta_before")
    theta_after = event.get("theta_after")
    key = (student_id, skill_id)

    predecessor = last_theta_after.get(key)
    if predecessor is None:
        expected, predecessor_label = _FRESH_THETA, "the fresh/default theta"
    else:
        expected, predecessor_event_id = predecessor
        predecessor_label = f"event {predecessor_event_id!r}'s theta_after"

    failure: Optional[AuditFailure] = None
    if theta_before != expected:
        failure = AuditFailure(
            event_id,
            f"theta_chain: student {student_id!r} skill {skill_id!r}: theta_before={theta_before!r} "
            f"does not match {predecessor_label} ({expected!r}) — possible interleaved-session theta "
            "divergence (KTD6)",
            kind="theta_chain",
        )
    if theta_after is not None:
        last_theta_after[key] = (theta_after, event_id)
    return failure


def validate_log(log_path: Path, bank_lookup: engine.BankLookup) -> list[AuditFailure]:
    """KTD5's checks, applied after the fact to every event in the log
    (reuses `ingest.validate_event` — no session context, since a
    standalone audit has no submitting session to compare against), plus
    U13's theta-chain cross-check (`_check_theta_chain`) — a second,
    independent dimension run on every event regardless of whether that
    same event also passed or failed the KTD5 ingest checks, since chain
    consistency is about the log's own internal theta bookkeeping, not
    about whether an event resolves against a bank."""
    failures: list[AuditFailure] = []
    seen_by_student: dict[str, set] = {}
    last_theta_after: Dict[Tuple[str, str], Tuple[float, str]] = {}
    for event in log_module.iter_events(log_path):
        student_id = event.get("student_id", "<unknown>")
        seen = seen_by_student.setdefault(student_id, set())
        report = ingest.validate_event(event, None, bank_lookup, seen)
        event_id = event.get("event_id", "<unknown>")
        if not report.accepted:
            failures.append(AuditFailure(event_id, report.rejection_reason or "rejected"))
        else:
            signature = event.get("variant_signature")
            if signature is not None:
                seen.add(signature)

        chain_failure = _check_theta_chain(event, last_theta_after)
        if chain_failure is not None:
            failures.append(chain_failure)
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


def identity_annotations_for(
    summaries: "dict[str, serving.ProgressSummary]", identity: Optional[IdentityRegistry]
) -> Optional[Dict[str, List[str]]]:
    """U13's identity-aware path: per-student linked-consumer annotation for
    `--identity-registry`. Returns `None` when `identity` is `None` (the
    flag was omitted) — the CLI's signal to `_print_summary` to skip the
    annotation section entirely and behave exactly as it did before this
    unit, on every invocation that doesn't ask for identity context (this is
    additive, optional context, never a new requirement on every run).

    When `identity` is given, every student appearing in the log gets an
    entry — `IdentityRegistry.linked_consumers` already degrades gracefully
    to `[]` for a student_id that was never registered or has no links, so
    no extra branching is needed here for that case; `_print_summary`
    renders an empty list as an explicit "(none)" rather than silence, so a
    reader can't confuse "no consumers linked" with "annotation not run"."""
    if identity is None:
        return None
    return {student_id: identity.linked_consumers(student_id) for student_id in summaries}


def _print_summary(
    summaries: "dict[str, serving.ProgressSummary]",
    identity_annotations: Optional[Dict[str, List[str]]] = None,
) -> None:
    for student_id, summary in summaries.items():
        print(f"student {student_id!r}:")
        if identity_annotations is not None:
            linked = identity_annotations.get(student_id, [])
            if linked:
                print(f"  linked consumers: {', '.join(linked)}")
            else:
                print("  linked consumers: (none)")
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
    parser.add_argument(
        "--identity-registry", type=Path, default=None,
        help="U13: optional path to an orchestrator.identity.IdentityRegistry JSONL file; when given, "
        "annotates each student's report with its linked consumers. Omitted (the default), the audit "
        "behaves exactly as it does without this flag -- additive, optional context, not a new "
        "requirement on every invocation. A path that doesn't exist yet is treated as an empty "
        "registry (IdentityRegistry's own missing-file-is-empty-state contract), not an error.",
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
    identity = IdentityRegistry(args.identity_registry) if args.identity_registry is not None else None

    failures, summaries = audit_log(args.log, content, bank_lookup, now)

    if failures:
        print(f"replay audit FAILED: {len(failures)} event(s) did not resolve:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    annotations = identity_annotations_for(summaries, identity)
    _print_summary(summaries, annotations)
    print("replay audit clean: every event resolved against its stamped build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
