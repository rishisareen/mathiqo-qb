"""Attempt-event ingest validation (KTD5, R5-R7).

`validate_event` is the KTD5 decision table as a pure function: hard-reject
on schema failure, an unknown `bank_version`, an `item_instance_id` that
doesn't resolve within that build, or a `variant_signature` mismatching the
bank's own record for that instance; for an event submitted through a
session, additionally reject a `bank_version`/`student_id`/`session_id`
that differs from the submitting session's. A repeat of an already-seen
signature is accepted and flagged (KD5), never rejected — the flag lives
only in the returned `IngestReport`. It is never written onto the event
itself: per-consumer repeat rates (R6, R7) are recomputed from the log's
existing `variant_signature`/`consumer_id` fields at query time, which
keeps KTD2's two-field schema change the only schema change this contract
makes.

Modeled on `tools/graph_lint.py`'s `LintResult`: a report object, not an
exception — the caller (the facade) decides what a rejection means for its
own flow (raise, surface via a notify channel, return a structured tool
error), and `validate_event` never touches the log itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from . import log as log_module
from .engine import BankLookup


@dataclass(frozen=True)
class SessionContext:
    """The submitting session's own pin — just enough for KTD5's
    session-consistency check, without importing orchestrator/serving.py
    (which imports this module, not the other way around)."""

    session_id: str
    student_id: str
    bank_version: str


@dataclass(frozen=True)
class IngestReport:
    accepted: bool
    rejection_reason: Optional[str] = None
    repeat: bool = False


def _reject(reason: str) -> IngestReport:
    return IngestReport(accepted=False, rejection_reason=reason)


def validate_event(
    event: dict,
    session_ctx: Optional[SessionContext],
    bank_lookup: BankLookup,
    seen_state: Iterable[str],
) -> IngestReport:
    """KTD5's decision path. `bank_lookup` resolves any retained build
    (KTD7) — not only the currently ACTIVE one, since a session pinned to
    an older, still-retained build must keep validating against it.
    `seen_state` is the submitting student's already-seen variant
    signatures (KD5's repeat check), independent of which skill each was
    served for — v1's bank publishes only single-skill instances."""
    errors = sorted(log_module._validator().iter_errors(event), key=lambda e: list(e.path))  # noqa: SLF001
    if errors:
        detail = errors[0].message
        return _reject(f"schema: {detail}")

    bank_version = event["bank_version"]
    try:
        bank = bank_lookup(bank_version)
    except Exception:
        return _reject(f"unknown_bank: {bank_version!r} does not name a retained build")

    instance_id = event["item_instance_id"]
    instance = bank.by_id.get(instance_id)
    if instance is None:
        return _reject(f"unresolvable_instance: {instance_id!r} does not resolve within bank {bank_version!r}")

    if instance["variant_signature"] != event["variant_signature"]:
        return _reject(
            f"signature_mismatch: event variant_signature does not match bank {bank_version!r}'s "
            f"record for instance {instance_id!r}"
        )

    if session_ctx is not None:
        if bank_version != session_ctx.bank_version:
            return _reject(
                f"session_mismatch: bank_version {bank_version!r} differs from the submitting "
                f"session's pinned {session_ctx.bank_version!r}"
            )
        if event["student_id"] != session_ctx.student_id:
            return _reject(
                f"session_mismatch: student_id {event['student_id']!r} differs from the submitting "
                f"session's {session_ctx.student_id!r}"
            )
        if event["session_id"] != session_ctx.session_id:
            return _reject(
                f"session_mismatch: session_id {event['session_id']!r} differs from the submitting "
                f"session's {session_ctx.session_id!r}"
            )

    repeat = event["variant_signature"] in set(seen_state)
    return IngestReport(accepted=True, repeat=repeat)


@dataclass(frozen=True)
class ConsumerRepeatStats:
    consumer_id: str
    total: int
    repeats: int


def repeat_rates_by_consumer(log_path) -> "dict[str, ConsumerRepeatStats]":
    """R7: per-consumer repeat-flag rates, recomputed at query time from the
    log's own `variant_signature`/`consumer_id` fields rather than a stored
    flag — a signature is a repeat exactly when it has already appeared
    earlier in that same student's event stream (independent of skill, same
    as `validate_event`'s own `seen_state` check)."""
    seen_by_student: "dict[str, set]" = {}
    stats: "dict[str, ConsumerRepeatStats]" = {}
    for event in log_module.iter_events(log_path):
        student_id = event["student_id"]
        consumer_id = event["consumer_id"]
        signature = event["variant_signature"]
        seen = seen_by_student.setdefault(student_id, set())
        is_repeat = signature in seen
        seen.add(signature)

        current = stats.get(consumer_id, ConsumerRepeatStats(consumer_id=consumer_id, total=0, repeats=0))
        stats[consumer_id] = ConsumerRepeatStats(
            consumer_id=consumer_id,
            total=current.total + 1,
            repeats=current.repeats + (1 if is_repeat else 0),
        )
    return stats
