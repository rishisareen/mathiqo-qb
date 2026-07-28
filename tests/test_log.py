"""Tests for orchestrator/log.py — the on-disk JSONL attempt log and the
replay path that rebuilds StudentModelStore from it (student-model doc §9;
R15; plan Unit U4).

Covers: append_attempt() schema-validates before writing and raises on a
malformed event; replay() on a missing/empty log yields a fresh store;
replay() reproduces the same state a direct (non-log.py) composition of
update.py + certification.py would produce, across serialization; replay()
is idempotent and insensitive to interleaving independent students'/
sessions' events in the log file.

`reference_apply` below is a second, independently hand-written composition
of Attempt / apply_attempt / maybe_certify / apply_review_outcome, built
straight from the plan's and log.py's documented composition rules but
without importing log.py's private helpers. Comparing its output to
log.replay()'s output over the same events (round-tripped through
append_attempt + JSONL) is what makes the round-trip test meaningful: it
would catch a bug in log.py's own composition (e.g. a swapped pass/fail
branch, a wrong field mapping), not just "replay agrees with itself."
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator import constants
from orchestrator.certification import apply_review_outcome, maybe_certify
from orchestrator.log import AttemptLogError, append_attempt, compose_attempt, iter_events, replay
from orchestrator.state import SkillState, StudentModelStore
from orchestrator.update import Attempt, apply_attempt


def make_event(**overrides) -> dict:
    defaults = dict(
        event_id="e1",
        student_id="stu1",
        session_id="sess1",
        timestamp="2026-07-27T12:00:00",
        item_instance_id="INST.1",
        template_id="TPL.FRAC.ADD.UNLIKE.001",
        template_version="1",
        bank_version="2026.07.27",
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
    defaults.setdefault("attempt_id", f"attempt-{defaults['event_id']}")
    return defaults


# --- append_attempt(): schema-validate-then-write -----------------------------

def test_append_attempt_writes_one_valid_jsonl_line(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event()
    append_attempt(log_path, event)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == event


def test_append_attempt_appends_in_order(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, make_event(event_id="e1"))
    append_attempt(log_path, make_event(event_id="e2"))
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["event_id"] for l in lines] == ["e1", "e2"]


def test_append_attempt_creates_parent_directories(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "dir" / "log.jsonl"
    append_attempt(log_path, make_event())
    assert log_path.exists()


def test_append_attempt_rejects_missing_required_field(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event()
    del event["theta_after"]
    with pytest.raises(AttemptLogError):
        append_attempt(log_path, event)
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_append_attempt_rejects_bad_enum(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event(context="not_a_real_context")
    with pytest.raises(AttemptLogError):
        append_attempt(log_path, event)


# --- U2: variant_signature + consumer_id are schema-required (KTD2) --------

def test_append_attempt_rejects_event_missing_variant_signature(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event()
    del event["variant_signature"]
    with pytest.raises(AttemptLogError, match="variant_signature"):
        append_attempt(log_path, event)
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_append_attempt_rejects_event_missing_consumer_id(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event()
    del event["consumer_id"]
    with pytest.raises(AttemptLogError, match="consumer_id"):
        append_attempt(log_path, event)
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_append_attempt_rejects_bad_question_type(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event(question_type="essay")
    with pytest.raises(AttemptLogError):
        append_attempt(log_path, event)


def test_append_attempt_rejects_out_of_range_depth(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event(depth_served=1.5)
    with pytest.raises(AttemptLogError):
        append_attempt(log_path, event)


def test_append_attempt_error_message_names_the_field(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    event = make_event()
    del event["hints_used"]
    with pytest.raises(AttemptLogError, match="hints_used"):
        append_attempt(log_path, event)


def test_valid_event_does_not_raise(tmp_path: Path) -> None:
    # Sanity check that make_event()'s defaults are themselves schema-valid,
    # so failures above are attributable to the specific corruption made.
    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, make_event())  # must not raise


# --- iter_events(): torn-write tolerance (code review reliability fix) --------

def test_iter_events_discards_a_truncated_trailing_line_with_a_warning(tmp_path: Path, capsys) -> None:
    """A process killed mid-write can leave a truncated, unparseable last
    line (power loss, SIGKILL). Per the module docstring, that is read as a
    torn write and discarded — with a stderr warning, not raised — since
    the log is otherwise intact."""
    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, make_event(event_id="e1"))
    with log_path.open("a", encoding="utf-8") as f:
        f.write('{"event_id": "e2", "student_id": "stu1"')  # no closing brace, no trailing newline

    events = list(iter_events(log_path))

    assert [e["event_id"] for e in events] == ["e1"]
    captured = capsys.readouterr()
    assert "discarding truncated trailing line" in captured.err


def test_iter_events_raises_on_a_malformed_non_trailing_line(tmp_path: Path) -> None:
    """A malformed line anywhere *before* the last is real corruption, not a
    torn write (every append after it that succeeded would have left the
    file longer) — this raises loudly instead of silently skipping content."""
    log_path = tmp_path / "log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write('{"event_id": "e1", not valid json\n')
        f.write(json.dumps(make_event(event_id="e2"), sort_keys=True) + "\n")

    with pytest.raises(AttemptLogError, match="not the last line"):
        list(iter_events(log_path))


def test_replay_tolerates_a_torn_trailing_write(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, make_event(event_id="e1"))
    with log_path.open("a", encoding="utf-8") as f:
        f.write('{"event_id": "e2"')  # torn

    store = replay(log_path)  # must not raise

    assert [e["event_id"] for e in store.attempt_log] == ["e1"]


# --- compose_attempt(): review-context abandonment discard ---------------------

def test_compose_attempt_review_context_abandonment_is_discarded() -> None:
    """§3's abandonment guard fires even inside a review-context attempt (a
    no-interaction timeout that is also the session's last event, and this
    happened to be a due review probe): compose_attempt must discard it
    (new_state=None, per the "what gets logged" decision) rather than
    running it through apply_review_outcome."""
    state = SkillState(theta=0.4, n=5.0, certified_depth=0.2)
    attempt = Attempt(
        skill_ids=["FRAC.ADD.UNLIKE"],
        question_type="numeric_entry",
        b_prior_at_serve=0.2,
        correct=False,
        timed_out=True,
        engaged=False,
        session_last_event=True,
        context="review",
    )
    result = compose_attempt(state, attempt, "review", datetime(2026, 7, 27, 12, 0, 0))
    assert result.new_state is None
    assert result.theta_after == pytest.approx(result.theta_before)
    assert result.theta_before == pytest.approx(0.4)


# --- replay(): missing / empty log --------------------------------------------

def test_replay_missing_log_yields_empty_store(tmp_path: Path) -> None:
    store = replay(tmp_path / "does-not-exist.jsonl")
    assert list(store.students()) == []
    assert store.attempt_log == []


def test_replay_empty_log_yields_empty_store(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    log_path.write_text("", encoding="utf-8")
    store = replay(log_path)
    assert list(store.students()) == []


def test_replay_populates_attempt_log(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, make_event(event_id="e1"))
    append_attempt(log_path, make_event(event_id="e2"))
    store = replay(log_path)
    assert [e["event_id"] for e in store.attempt_log] == ["e1", "e2"]


# --- reference composition: an independent re-implementation ------------------

def reference_apply(store: StudentModelStore, event: dict) -> None:
    student_id = event["student_id"]
    skill_ids = event["skill_ids"]
    context = event["context"]
    now = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
    attempt = Attempt(
        skill_ids=list(skill_ids),
        question_type=event["question_type"],
        b_prior_at_serve=event["b_prior_at_serve"],
        correct=event["correct"],
        hints_used=event["hints_used"],
        timed_out=event["timed_out"],
        engaged=True,
        session_last_event=False,
        context=context,
        misconception_id=event["misconception_id"],
        event_id=event["event_id"],
        depth_served=event["depth_served"],
    )

    def set_state(skill_id: str, new_state: SkillState) -> None:
        records = dict(store.records_for(student_id))
        records[skill_id] = new_state
        store.replace_records(student_id, records)

    if context == "review":
        for skill_id in skill_ids:
            state = store.get(student_id, skill_id)
            if attempt.correct:
                new_state = apply_review_outcome(state, True, now)
            else:
                result = apply_attempt(state, attempt)
                new_state = apply_review_outcome(result.new_state, False, now)
            set_state(skill_id, new_state)
        return

    updated = {}
    for skill_id in skill_ids:
        state = store.get(student_id, skill_id)
        result = apply_attempt(state, attempt)
        updated[skill_id] = result.new_state
    if len(skill_ids) == 1:
        only = skill_ids[0]
        updated[only] = maybe_certify(updated[only], attempt, now)
    for skill_id, new_state in updated.items():
        set_state(skill_id, new_state)


def assert_stores_equal(a: StudentModelStore, b: StudentModelStore) -> None:
    assert set(a.students()) == set(b.students())
    for student_id in a.students():
        records_a = a.records_for(student_id)
        records_b = b.records_for(student_id)
        assert set(records_a) == set(records_b)
        for skill_id in records_a:
            sa, sb = records_a[skill_id], records_b[skill_id]
            assert sa.theta == pytest.approx(sb.theta)
            assert sa.n == pytest.approx(sb.n)
            assert sa.certified_depth == (
                pytest.approx(sb.certified_depth) if sb.certified_depth is not None else None
            )
            assert sa.review_due == sb.review_due
            assert sa.review_interval_days == pytest.approx(sb.review_interval_days)
            assert sa.lapsed == sb.lapsed
            assert sa.misc_counters == sb.misc_counters
            assert sa.recent_event_ids == sb.recent_event_ids


def build_scenario_events() -> list:
    """A synthetic multi-student, multi-skill session exercising: ordinary
    practice attempts (clean, hint-assisted, wrong-with-misconception),
    enough clean unassisted low-guess attempts to earn certification,
    a multi-skill success (credit assignment) and failure (queued probes),
    a lesson attempt (never certifies), a passing review probe, and a
    failing review probe (lapse)."""
    events = []
    t0 = datetime(2026, 7, 1, 9, 0, 0)

    def ts(minutes: int) -> str:
        return (t0 + timedelta(minutes=minutes)).isoformat()

    # stu1 / FRAC.ADD.UNLIKE: build up theta with clean, easy (low b) attempts
    # until certification fires, then a hint-assisted one, a wrong one with a
    # misconception tag, and a review pass then a review fail.
    for i in range(6):
        events.append(
            make_event(
                event_id=f"stu1-build-{i}",
                student_id="stu1",
                skill_ids=["FRAC.ADD.UNLIKE"],
                b_prior_at_serve=0.0,
                depth_served=0.0,
                correct=True,
                hints_used=0,
                context="practice",
                timestamp=ts(i),
            )
        )
    events.append(
        make_event(
            event_id="stu1-hinted",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=True,
            hints_used=2,
            context="practice",
            timestamp=ts(10),
        )
    )
    events.append(
        make_event(
            event_id="stu1-wrong-misc",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=False,
            misconception_id="MISC.FRAC.ADD-ACROSS",
            context="practice",
            timestamp=ts(11),
        )
    )
    events.append(
        make_event(
            event_id="stu1-review-pass",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=True,
            hints_used=0,
            context="review",
            timestamp=ts(20),
        )
    )
    events.append(
        make_event(
            event_id="stu1-review-fail",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=False,
            context="review",
            timestamp=ts(21),
        )
    )

    # stu1: a lesson attempt on a different skill — never certifies.
    events.append(
        make_event(
            event_id="stu1-lesson",
            student_id="stu1",
            skill_ids=["FRAC.EQUIV"],
            correct=True,
            hints_used=3,
            context="lesson",
            depth_served=0.4,
            timestamp=ts(12),
        )
    )

    # stu1: a multi-skill success then a multi-skill failure.
    events.append(
        make_event(
            event_id="stu1-multi-success",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
            question_type="mcq_4",
            correct=True,
            context="practice",
            timestamp=ts(13),
        )
    )
    events.append(
        make_event(
            event_id="stu1-multi-fail",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
            question_type="mcq_4",
            correct=False,
            context="practice",
            timestamp=ts(14),
        )
    )

    # stu2: independent student, independent skill — a probe-context attempt.
    events.append(
        make_event(
            event_id="stu2-probe",
            student_id="stu2",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=True,
            context="probe",
            timestamp=ts(1),
        )
    )
    events.append(
        make_event(
            event_id="stu2-placement",
            student_id="stu2",
            skill_ids=["FRAC.EQUIV"],
            correct=False,
            context="placement",
            timestamp=ts(2),
        )
    )
    return events


def test_round_trip_matches_reference_composition(tmp_path: Path) -> None:
    events = build_scenario_events()
    log_path = tmp_path / "log.jsonl"
    reference_store = StudentModelStore()
    for event in events:
        append_attempt(log_path, event)
        reference_apply(reference_store, event)

    replayed_store = replay(log_path)

    # Sanity: the scenario actually exercised certification and a lapse.
    stu1_records = reference_store.records_for("stu1")
    assert stu1_records["FRAC.ADD.UNLIKE"].certified_depth is not None
    assert stu1_records["FRAC.ADD.UNLIKE"].lapsed is True

    assert_stores_equal(reference_store, replayed_store)


def test_round_trip_multi_skill_updates_both_skills_at_half_weight(tmp_path: Path) -> None:
    events = [
        make_event(
            event_id="e1",
            student_id="stu1",
            skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
            question_type="mcq_4",
            correct=True,
            context="practice",
        )
    ]
    log_path = tmp_path / "log.jsonl"
    for e in events:
        append_attempt(log_path, e)
    store = replay(log_path)
    records = store.records_for("stu1")
    assert set(records) == {"FRAC.ADD.UNLIKE", "FRAC.EQUIV"}
    for skill_id in records:
        assert records[skill_id].n == pytest.approx(constants.MULTI_SKILL_WEIGHT)
        assert records[skill_id].certified_depth is None  # multi-skill never certifies


def test_review_pass_does_not_move_theta(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    append_attempt(
        log_path,
        make_event(
            event_id="e1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=True,
            context="practice",
            b_prior_at_serve=0.5,
        ),
    )
    store_before = replay(log_path)
    theta_before = store_before.get("stu1", "FRAC.ADD.UNLIKE").theta

    append_attempt(
        log_path,
        make_event(
            event_id="e2",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=True,
            context="review",
        ),
    )
    store_after = replay(log_path)
    theta_after = store_after.get("stu1", "FRAC.ADD.UNLIKE").theta

    assert theta_after == pytest.approx(theta_before)


def test_replay_handles_z_suffixed_utc_timestamp(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    append_attempt(
        log_path,
        make_event(
            event_id="e1",
            skill_ids=["FRAC.ADD.UNLIKE"],
            correct=False,
            context="review",
            timestamp="2026-07-27T12:00:00Z",
        ),
    )
    store = replay(log_path)
    state = store.get("stu1", "FRAC.ADD.UNLIKE")
    expected_now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert state.review_due == expected_now + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)
    assert state.lapsed is True


# --- idempotence and ordering-stability ----------------------------------------

def test_replay_is_idempotent(tmp_path: Path) -> None:
    events = build_scenario_events()
    log_path = tmp_path / "log.jsonl"
    for event in events:
        append_attempt(log_path, event)

    first = replay(log_path)
    second = replay(log_path)
    assert_stores_equal(first, second)


def test_replay_ordering_stable_across_interleaved_students(tmp_path: Path) -> None:
    """Two students' own event sequences, written back-to-back (blocked) vs.
    interleaved 1-2-1-2-..., must replay to the same final state — the two
    students occupy independent (student, skill) slots, so interleaving
    across them cannot change the outcome, even though each student's own
    internal event order is (and must remain) preserved."""
    stu1_events = [
        make_event(event_id=f"stu1-{i}", student_id="stu1", correct=(i % 2 == 0), timestamp=f"2026-07-0{i+1}T09:00:00")
        for i in range(5)
    ]
    stu2_events = [
        make_event(event_id=f"stu2-{i}", student_id="stu2", correct=(i % 3 == 0), timestamp=f"2026-07-0{i+1}T10:00:00")
        for i in range(5)
    ]

    blocked_path = tmp_path / "blocked.jsonl"
    for e in stu1_events + stu2_events:
        append_attempt(blocked_path, e)

    interleaved_path = tmp_path / "interleaved.jsonl"
    for a, b in zip(stu1_events, stu2_events):
        append_attempt(interleaved_path, a)
        append_attempt(interleaved_path, b)

    assert_stores_equal(replay(blocked_path), replay(interleaved_path))
