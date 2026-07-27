"""Tests for orchestrator/state.py — per-(student, skill) state, the §4
misconception window, and the §7 certification status machine.

update.py (the θ update rule, §3) is not yet implemented; these tests cover
only the state container and the status machine that already exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orchestrator import constants
from orchestrator.state import (
    STATUS_CERTIFIED,
    STATUS_DUE,
    STATUS_LAPSED,
    STATUS_UNCERTIFIED,
    SkillState,
    StudentModelStore,
    certification_status,
    is_certified,
)

NOW = datetime(2026, 7, 27, 12, 0, 0)


# --- SkillState.record_attempt / misc_count: the §4 sliding window ----------

def test_misc_count_zero_for_unseen_misconception() -> None:
    state = SkillState()
    assert state.misc_count("MISC.FRAC.ADD-ACROSS") == 0


def test_record_attempt_tracks_misconception_within_window() -> None:
    state = SkillState()
    state.record_attempt("e1", "MISC.FRAC.ADD-ACROSS")
    state.record_attempt("e2", None)
    state.record_attempt("e3", "MISC.FRAC.ADD-ACROSS")
    assert state.misc_count("MISC.FRAC.ADD-ACROSS") == 2
    assert state.recent_event_ids == ["e1", "e2", "e3"]


def test_window_slides_at_misc_window_boundary() -> None:
    """§4: a counter must reach MISC_TRIGGER_COUNT within the last
    MISC_WINDOW attempts. An event that ages out of the window stops
    counting toward the trigger."""
    state = SkillState()
    state.record_attempt("e1", "MISC.X")  # will age out after MISC_WINDOW more
    for i in range(2, constants.MISC_WINDOW + 1):
        state.record_attempt(f"e{i}", None)
    assert len(state.recent_event_ids) == constants.MISC_WINDOW
    assert state.misc_count("MISC.X") == 1  # e1 is still the oldest in-window event

    state.record_attempt(f"e{constants.MISC_WINDOW + 1}", None)  # slides e1 out
    assert len(state.recent_event_ids) == constants.MISC_WINDOW
    assert state.misc_count("MISC.X") == 0


def test_two_in_last_five_trigger_reachable() -> None:
    """The concrete §4 rule: 2 occurrences within the last 5 attempts."""
    state = SkillState()
    state.record_attempt("e1", "MISC.X")
    state.record_attempt("e2", None)
    state.record_attempt("e3", None)
    state.record_attempt("e4", "MISC.X")
    assert state.misc_count("MISC.X") == 2  # both within the last 5 (only 4 so far)
    assert state.misc_count("MISC.X") >= constants.MISC_TRIGGER_COUNT


def test_empty_misc_counters_pruned_after_sliding_out() -> None:
    """Internal bookkeeping: a misconception with zero in-window events is
    dropped from misc_counters entirely, not left as an empty list."""
    state = SkillState()
    state.record_attempt("e1", "MISC.X")
    for i in range(2, constants.MISC_WINDOW + 2):
        state.record_attempt(f"e{i}", None)
    assert "MISC.X" not in state.misc_counters


def test_unrelated_misconceptions_tracked_independently() -> None:
    state = SkillState()
    state.record_attempt("e1", "MISC.A")
    state.record_attempt("e2", "MISC.B")
    state.record_attempt("e3", "MISC.A")
    assert state.misc_count("MISC.A") == 2
    assert state.misc_count("MISC.B") == 1


# --- certification_status: the §7 status machine -----------------------------

def test_uncertified_with_no_certified_depth() -> None:
    assert certification_status(SkillState(), NOW) == STATUS_UNCERTIFIED


def test_certified_with_no_review_due_yet() -> None:
    state = SkillState(certified_depth=0.5, review_due=NOW + timedelta(days=1))
    assert certification_status(state, NOW) == STATUS_CERTIFIED


def test_due_when_review_date_has_passed() -> None:
    state = SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1))
    assert certification_status(state, NOW) == STATUS_DUE


def test_certified_with_no_review_due_set() -> None:
    """review_due is Optional; a certification with none set is never 'due'."""
    state = SkillState(certified_depth=0.5, review_due=None)
    assert certification_status(state, NOW) == STATUS_CERTIFIED


def test_lapsed_overrides_due() -> None:
    """§6: lapsed is set explicitly by a failed review probe and takes
    precedence over the clock-derived 'due' status."""
    state = SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1), lapsed=True)
    assert certification_status(state, NOW) == STATUS_LAPSED


def test_lapsed_overrides_certified() -> None:
    state = SkillState(certified_depth=0.5, review_due=NOW + timedelta(days=1), lapsed=True)
    assert certification_status(state, NOW) == STATUS_LAPSED


# --- is_certified: the §1.3/§7 prerequisite-check read ------------------------

def test_is_certified_true_at_exact_depth() -> None:
    state = SkillState(certified_depth=0.5, review_due=NOW + timedelta(days=1))
    assert is_certified(state, 0.5, NOW) is True


def test_is_certified_true_above_required_depth() -> None:
    state = SkillState(certified_depth=0.7, review_due=NOW + timedelta(days=1))
    assert is_certified(state, 0.5, NOW) is True


def test_is_certified_false_below_required_depth() -> None:
    state = SkillState(certified_depth=0.3, review_due=NOW + timedelta(days=1))
    assert is_certified(state, 0.5, NOW) is False


def test_is_certified_false_when_uncertified() -> None:
    assert is_certified(SkillState(), 0.5, NOW) is False


def test_is_certified_true_when_due() -> None:
    """§6.3: a 'due' certification still counts — review pending is not yet
    a lapse, so dependents remain servable while a review probe is pending."""
    state = SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1))
    assert is_certified(state, 0.5, NOW) is True


def test_is_certified_false_when_lapsed() -> None:
    """§6.3: a lapsed prerequisite blocks dependents from being served."""
    state = SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1), lapsed=True)
    assert is_certified(state, 0.5, NOW) is False


# --- StudentModelStore --------------------------------------------------------

def test_get_creates_default_state_on_first_access() -> None:
    store = StudentModelStore()
    state = store.get("stu1", "FRAC.ADD.UNLIKE")
    assert state.theta == 0.0
    assert state.n == 0.0
    assert store.get("stu1", "FRAC.ADD.UNLIKE") is state  # same object on refetch


def test_get_is_scoped_per_student_and_skill() -> None:
    store = StudentModelStore()
    a = store.get("stu1", "FRAC.ADD.UNLIKE")
    b = store.get("stu2", "FRAC.ADD.UNLIKE")
    c = store.get("stu1", "FRAC.EQUIV")
    assert a is not b
    assert a is not c


def test_records_for_scopes_to_one_student() -> None:
    store = StudentModelStore()
    store.get("stu1", "FRAC.ADD.UNLIKE")
    store.get("stu1", "FRAC.EQUIV")
    store.get("stu2", "FRAC.ADD.UNLIKE")
    records = store.records_for("stu1")
    assert set(records) == {"FRAC.ADD.UNLIKE", "FRAC.EQUIV"}


def test_replace_records_swaps_a_students_records() -> None:
    store = StudentModelStore()
    store.get("stu1", "FRAC.ADD.UNLIKE").theta = 0.3
    new_state = SkillState(theta=0.9, graph_version="2.0.0")
    store.replace_records("stu1", {"FRAC.ADD.UNLIKE": new_state})
    records = store.records_for("stu1")
    assert set(records) == {"FRAC.ADD.UNLIKE"}
    assert records["FRAC.ADD.UNLIKE"] is new_state


def test_replace_records_does_not_touch_other_students() -> None:
    store = StudentModelStore()
    store.get("stu2", "FRAC.ADD.UNLIKE").theta = 0.4
    store.replace_records("stu1", {"FRAC.EQUIV": SkillState()})
    assert store.records_for("stu2")["FRAC.ADD.UNLIKE"].theta == 0.4


def test_append_event_and_students() -> None:
    store = StudentModelStore()
    store.get("stu2", "FRAC.ADD.UNLIKE")
    store.get("stu1", "FRAC.ADD.UNLIKE")
    store.append_event({"event_id": "e1", "student_id": "stu1"})
    assert store.attempt_log == [{"event_id": "e1", "student_id": "stu1"}]
    assert list(store.students()) == ["stu1", "stu2"]  # sorted, unique
