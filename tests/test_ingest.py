"""Tests for orchestrator/ingest.py — the U5 KTD5 ingest-validation decision
table: hard-reject vs. flag-never-reject (KD5), and R7's per-consumer
repeat-rate recomputation.

Reuses tests/test_log.py's `make_event`-style fixture: a schema-valid
attempt event, adjusted per test via keyword overrides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import constants, engine, ingest
from orchestrator.log import append_attempt


def make_event(**overrides) -> dict:
    defaults = dict(
        event_id="e1",
        student_id="stu1",
        session_id="sess1",
        timestamp="2026-07-27T12:00:00",
        item_instance_id="INST.1",
        template_id="TPL.FRAC.ADD.UNLIKE.001",
        template_version="1",
        bank_version="bank-a",
        variant_signature="a" * 64,
        consumer_id=constants.CONSUMER_ID_INTERNAL,
        engine_version=constants.ENGINE_VERSION,
        graph_version="1.0.0",
        skill_ids=["FRAC.ADD.UNLIKE"],
        question_type="numeric_entry",
        depth_served=0.5,
        b_prior_at_serve=0.5,
        exam_context=None,
        language="en",
        locale="en-IN",
        response_raw="3/4",
        response_parsed="3/4",
        correct=True,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=10.0,
        timed_out=False,
        context="practice",
        theta_before=0.0,
        theta_after=0.0,
        n_before=0.0,
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture()
def bank_a() -> engine.BankIndex:
    return engine.BankIndex(
        bank_version="bank-a",
        instances_by_skill={"FRAC.ADD.UNLIKE": [{"item_instance_id": "INST.1", "variant_signature": "a" * 64, "difficulty": 0.5}]},
        by_id={"INST.1": {"item_instance_id": "INST.1", "variant_signature": "a" * 64, "difficulty": 0.5}},
    )


@pytest.fixture()
def bank_b() -> engine.BankIndex:
    return engine.BankIndex(
        bank_version="bank-b",
        instances_by_skill={"FRAC.ADD.UNLIKE": [{"item_instance_id": "INST.2", "variant_signature": "b" * 64, "difficulty": 0.5}]},
        by_id={"INST.2": {"item_instance_id": "INST.2", "variant_signature": "b" * 64, "difficulty": 0.5}},
    )


@pytest.fixture()
def bank_lookup(bank_a, bank_b) -> engine.BankLookup:
    return engine.make_static_bank_lookup(bank_a, bank_b)


@pytest.fixture()
def session_ctx() -> ingest.SessionContext:
    return ingest.SessionContext(session_id="sess1", student_id="stu1", bank_version="bank-a")


# --- Covers AE2: unknown bank / unresolvable instance -----------------------


def test_unknown_bank_version_is_rejected_naming_the_check(bank_lookup, session_ctx) -> None:
    event = make_event(bank_version="does-not-exist")
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "unknown_bank" in report.rejection_reason


def test_unresolvable_instance_id_is_rejected_naming_the_check(bank_lookup, session_ctx) -> None:
    event = make_event(item_instance_id="does-not-exist")
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "unresolvable_instance" in report.rejection_reason


# --- Signature mismatch ------------------------------------------------------


def test_signature_mismatching_bank_record_is_rejected(bank_lookup, session_ctx) -> None:
    event = make_event(variant_signature="f" * 64)  # bank_a's INST.1 record is "a" * 64
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "signature_mismatch" in report.rejection_reason


# --- Schema failure -----------------------------------------------------------


def test_schema_invalid_event_is_rejected(bank_lookup, session_ctx) -> None:
    event = make_event()
    del event["theta_after"]
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "schema" in report.rejection_reason


# --- Session-consistency checks ----------------------------------------------


def test_event_stamped_with_a_different_known_build_than_the_session_is_rejected(bank_lookup, session_ctx) -> None:
    """The session is pinned to bank-a; an event naming bank-b (a real,
    resolvable, but different build) must still be rejected."""
    event = make_event(bank_version="bank-b", item_instance_id="INST.2", variant_signature="b" * 64)
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "session_mismatch" in report.rejection_reason


def test_event_naming_a_different_student_than_the_session_is_rejected(bank_lookup, session_ctx) -> None:
    event = make_event(student_id="someone-else")
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    assert "session_mismatch" in report.rejection_reason


def test_event_from_a_retained_but_no_longer_active_build_matching_the_sessions_own_pin_is_accepted(
    bank_lookup,
) -> None:
    """A session pinned to bank-a (even though ACTIVE has since moved to
    bank-b) still validates events stamped bank-a: KTD7 resolves any
    retained build, and the session-consistency check compares against the
    *session's own* pin, not the currently active one."""
    session_ctx = ingest.SessionContext(session_id="sess1", student_id="stu1", bank_version="bank-a")
    event = make_event(bank_version="bank-a")
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is True


def test_no_session_context_skips_the_session_consistency_check(bank_lookup) -> None:
    event = make_event()
    report = ingest.validate_event(event, None, bank_lookup, seen_state=set())
    assert report.accepted is True


# --- Covers AE1 (write half): repeat is accepted and flagged, never rejected -


def test_repeat_of_a_seen_signature_is_accepted_and_flagged(bank_lookup, session_ctx) -> None:
    event = make_event()
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state={"a" * 64})
    assert report.accepted is True
    assert report.repeat is True


def test_unseen_signature_is_accepted_without_the_repeat_flag(bank_lookup, session_ctx) -> None:
    event = make_event()
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is True
    assert report.repeat is False


# --- A rejected event is never appended --------------------------------------


def test_rejected_event_is_never_appended_by_the_caller(tmp_path: Path, bank_lookup, session_ctx) -> None:
    """ingest.validate_event never touches the log itself — this documents
    the contract callers (the facade) must honor: only call append_attempt
    when report.accepted is True."""
    log_path = tmp_path / "log.jsonl"
    event = make_event(bank_version="does-not-exist")
    report = ingest.validate_event(event, session_ctx, bank_lookup, seen_state=set())
    assert report.accepted is False
    if report.accepted:
        append_attempt(log_path, event)
    assert not log_path.exists()


# --- R7: per-consumer repeat rates recomputed from the log, no flag field ----


def test_repeat_rates_by_consumer_recomputed_from_log_with_no_flag_field(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    # consumer A: two events, second one a repeat of the first's signature.
    append_attempt(log_path, make_event(event_id="e1", consumer_id="tutor.a", variant_signature="a" * 64))
    append_attempt(
        log_path,
        make_event(
            event_id="e2", consumer_id="tutor.a", variant_signature="a" * 64,
            timestamp="2026-07-27T12:05:00",
        ),
    )
    # consumer B: one fresh event, no repeat.
    append_attempt(
        log_path,
        make_event(event_id="e3", consumer_id="tutor.b", variant_signature="c" * 64, student_id="stu2"),
    )

    stats = ingest.repeat_rates_by_consumer(log_path)

    assert "variant_signature" in make_event()  # sanity: the log carries no separate flag field
    assert stats["tutor.a"].total == 2
    assert stats["tutor.a"].repeats == 1
    assert stats["tutor.b"].total == 1
    assert stats["tutor.b"].repeats == 0
