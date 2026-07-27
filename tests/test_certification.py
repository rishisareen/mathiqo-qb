"""Tests for orchestrator/certification.py — SM §6 (forgetting and review)
and §7 (the certification status machine) transitions.

Covers: maybe_certify() firing only on unassisted correct low-guess-format
non-lesson attempts with both the depth_served and θ−MASTERY_MARGIN caps
respected; apply_review_outcome() handling the interval math and lapse side
of a review probe (the caller routes a failed probe through U1's
apply_attempt first — this module never scores an attempt itself); and
due_reviews() as the ordered STATUS_DUE read.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orchestrator import constants
from orchestrator.certification import apply_review_outcome, due_reviews, maybe_certify
from orchestrator.state import SkillState, certification_status, is_certified
from orchestrator.update import Attempt, apply_attempt

NOW = datetime(2026, 7, 27, 12, 0, 0)


def make_attempt(**overrides) -> Attempt:
    defaults = dict(
        skill_ids=["FRAC.ADD.UNLIKE"],
        question_type="numeric_entry",
        b_prior_at_serve=0.5,
        correct=True,
        hints_used=0,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context="practice",
        misconception_id=None,
        event_id="e1",
        depth_served=0.5,
    )
    defaults.update(overrides)
    return Attempt(**defaults)


# --- maybe_certify(): §7 set-only-by conditions ------------------------------

def test_certification_fires_and_depth_served_caps_it() -> None:
    """theta - margin (0.8495) exceeds depth_served (0.6) -> depth_served caps."""
    state = SkillState(theta=1.0, n=3.0)
    attempt = make_attempt(depth_served=0.6, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth == pytest.approx(0.6)
    assert new_state.review_due == NOW + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)
    assert new_state.review_interval_days == constants.REVIEW_INITIAL_INTERVAL_DAYS
    assert new_state.lapsed is False


def test_certification_fires_and_theta_margin_caps_it() -> None:
    """theta - margin (0.3495) is below depth_served (0.6) -> theta caps."""
    state = SkillState(theta=0.5, n=3.0)
    attempt = make_attempt(depth_served=0.6, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)
    expected = 0.5 - constants.MASTERY_MARGIN
    assert new_state.certified_depth == pytest.approx(expected)


def test_certification_does_not_fire_when_theta_too_low() -> None:
    """theta - margin goes negative: no depth clears the E(theta,d)>=0.8 bar."""
    state = SkillState(theta=0.0, n=3.0)
    attempt = make_attempt(depth_served=0.5, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None
    assert new_state.review_due is None


def test_mcq_never_certifies_regardless_of_theta() -> None:
    state = SkillState(theta=5.0, n=3.0)
    attempt = make_attempt(
        question_type=constants.MCQ4_QUESTION_TYPE, depth_served=0.5, correct=True, hints_used=0
    )
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None


def test_hint_assisted_correct_never_certifies() -> None:
    state = SkillState(theta=5.0, n=3.0)
    attempt = make_attempt(depth_served=0.5, correct=True, hints_used=1)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None


def test_lesson_context_never_certifies() -> None:
    state = SkillState(theta=5.0, n=3.0)
    attempt = make_attempt(context="lesson", depth_served=0.5, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None


def test_multi_skill_item_never_certifies_even_in_low_guess_format() -> None:
    """§7: 'never set by multi-skill items' -- independent of question_type,
    since a multi-skill item can still carry a low-guess format string."""
    state = SkillState(theta=5.0, n=3.0)
    attempt = make_attempt(
        skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
        question_type="numeric_entry",
        depth_served=0.5,
        correct=True,
        hints_used=0,
    )
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None


def test_wrong_answer_never_certifies() -> None:
    state = SkillState(theta=5.0, n=3.0)
    attempt = make_attempt(depth_served=0.5, correct=False)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth is None


def test_certified_depth_never_decreases() -> None:
    """A later qualifying attempt at a shallower supported depth keeps the
    prior (higher) certified_depth -- max(existing, candidate)."""
    state = SkillState(theta=1.0, n=3.0, certified_depth=0.7)
    attempt = make_attempt(depth_served=0.3, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)
    assert new_state.certified_depth == pytest.approx(0.7)


def test_maybe_certify_does_not_mutate_input_state() -> None:
    state = SkillState(theta=1.0, n=3.0)
    attempt = make_attempt(depth_served=0.5, correct=True, hints_used=0)
    maybe_certify(state, attempt, NOW)
    assert state.certified_depth is None


def test_reearning_after_lapse_clears_flag_and_restores_prerequisite() -> None:
    state = SkillState(
        theta=1.0,
        n=3.0,
        certified_depth=0.5,
        review_due=NOW - timedelta(days=30),
        review_interval_days=7.0,
        lapsed=True,
    )
    assert is_certified(state, 0.5, NOW) is False  # lapsed blocks it beforehand

    attempt = make_attempt(depth_served=0.5, correct=True, hints_used=0)
    new_state = maybe_certify(state, attempt, NOW)

    assert new_state.lapsed is False
    assert is_certified(new_state, 0.5, NOW) is True
    assert new_state.review_due == NOW + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)
    assert new_state.review_interval_days == constants.REVIEW_INITIAL_INTERVAL_DAYS


# --- apply_review_outcome(): §6 review probe interval math -------------------

def test_review_pass_multiplies_interval_and_leaves_theta_unchanged() -> None:
    state = SkillState(
        theta=0.8,
        certified_depth=0.5,
        review_due=NOW - timedelta(days=1),
        review_interval_days=constants.REVIEW_INITIAL_INTERVAL_DAYS,
    )
    new_state = apply_review_outcome(state, True, NOW)
    assert new_state.review_interval_days == pytest.approx(17.5)
    assert new_state.review_due == NOW + timedelta(days=17.5)
    assert new_state.theta == pytest.approx(0.8)
    assert new_state.lapsed is False


def test_review_pass_sequence_caps_at_max_interval() -> None:
    """7 -> 17.5 -> 43.75 -> 109.375 (capped 90) -> stays 90."""
    state = SkillState(
        theta=0.8, certified_depth=0.5, review_interval_days=constants.REVIEW_INITIAL_INTERVAL_DAYS
    )
    expected = [17.5, 43.75, 90.0, 90.0]
    for exp in expected:
        state = apply_review_outcome(state, True, NOW)
        assert state.review_interval_days == pytest.approx(exp)
    assert state.review_interval_days == pytest.approx(constants.REVIEW_MAX_INTERVAL_DAYS)


def test_review_fail_resets_interval_and_lapses_certification() -> None:
    """Caller's job: route the failed probe through apply_attempt as an
    ordinary wrong attempt first (theta drops there), then this function
    handles the interval-reset-and-lapse side."""
    state = SkillState(
        theta=0.8,
        n=5.0,
        certified_depth=0.5,
        review_due=NOW - timedelta(days=1),
        review_interval_days=43.75,
    )
    probe = make_attempt(context="probe", correct=False, depth_served=0.5)
    wrong_attempt_result = apply_attempt(state, probe)
    assert wrong_attempt_result.theta_after < state.theta  # theta dropped via U1

    new_state = apply_review_outcome(wrong_attempt_result.new_state, False, NOW)

    assert new_state.theta == pytest.approx(wrong_attempt_result.theta_after)
    assert new_state.review_interval_days == constants.REVIEW_INITIAL_INTERVAL_DAYS
    assert new_state.lapsed is True
    assert certification_status(new_state, NOW) == "lapsed"
    assert is_certified(new_state, 0.5, NOW) is False  # dependents blocked
    assert new_state.certified_depth == pytest.approx(0.5)  # but not revoked


def test_apply_review_outcome_does_not_mutate_input_state() -> None:
    state = SkillState(theta=0.8, certified_depth=0.5, review_interval_days=7.0)
    apply_review_outcome(state, True, NOW)
    assert state.review_interval_days == 7.0


# --- due_reviews(): the ordered STATUS_DUE read -------------------------------

def test_due_reviews_filters_to_due_status_only() -> None:
    records = {
        "FRAC.A": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1)),  # due
        "FRAC.B": SkillState(certified_depth=0.5, review_due=NOW + timedelta(days=1)),  # certified
        "FRAC.C": SkillState(),  # uncertified
        "FRAC.D": SkillState(
            certified_depth=0.5, review_due=NOW - timedelta(days=1), lapsed=True
        ),  # lapsed
    }
    due = due_reviews(records, NOW)
    assert [skill_id for skill_id, _ in due] == ["FRAC.A"]


def test_due_reviews_orders_by_review_due_ascending() -> None:
    records = {
        "FRAC.LATER": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1)),
        "FRAC.EARLIER": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=10)),
        "FRAC.MIDDLE": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=5)),
    }
    due = due_reviews(records, NOW)
    assert [skill_id for skill_id, _ in due] == ["FRAC.EARLIER", "FRAC.MIDDLE", "FRAC.LATER"]


def test_due_reviews_accepts_iterable_of_pairs() -> None:
    pairs = [
        ("FRAC.A", SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1))),
        ("FRAC.B", SkillState()),
    ]
    due = due_reviews(pairs, NOW)
    assert [skill_id for skill_id, _ in due] == ["FRAC.A"]
