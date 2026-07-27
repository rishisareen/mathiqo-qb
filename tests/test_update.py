"""Tests for orchestrator/update.py — the θ update rule (student-model doc §3).

Covers: expectation() and the MCQ guessing floor (§2, §3), effective_score()
and the abandonment guard / lesson-context precedence (§3), the K schedule
(§3), and apply_attempt()'s full state-delta rule including the multi-skill
credit-assignment case (§3, core doc §2.10).
"""

from __future__ import annotations

import random

import pytest

from orchestrator import constants
from orchestrator.state import SkillState
from orchestrator.update import (
    Attempt,
    apply_attempt,
    effective_score,
    expectation,
    is_abandoned,
    k_schedule,
    weight_for,
)


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
    )
    defaults.update(overrides)
    return Attempt(**defaults)


# --- expectation(): §2 logistic E + §3 MCQ guessing floor -------------------

def test_expectation_at_equal_theta_and_b_is_one_half() -> None:
    assert expectation(0.5, 0.5, "numeric_entry") == pytest.approx(0.5)


def test_expectation_low_guess_formats_are_unadjusted() -> None:
    theta, b = 0.7, 0.4
    raw = 1.0 / (1.0 + 10.0 ** (-(theta - b) / constants.S))
    for qt in constants.LOW_GUESS_FORMATS:
        assert expectation(theta, b, qt) == pytest.approx(raw)


def test_expectation_mcq4_applies_guess_floor() -> None:
    theta, b = 0.7, 0.4
    raw = 1.0 / (1.0 + 10.0 ** (-(theta - b) / constants.S))
    adjusted = constants.MCQ4_GUESS_FLOOR + (1 - constants.MCQ4_GUESS_FLOOR) * raw
    assert expectation(theta, b, constants.MCQ4_QUESTION_TYPE) == pytest.approx(adjusted)


def test_expectation_mcq4_floors_near_quarter_when_theta_far_below_b() -> None:
    e = expectation(-5.0, 0.5, constants.MCQ4_QUESTION_TYPE)
    assert e == pytest.approx(constants.MCQ4_GUESS_FLOOR, abs=1e-3)


# --- effective_score(): §3 outcome table, guard, lesson precedence ----------

def test_clean_correct_scores_one() -> None:
    a = make_attempt(correct=True, hints_used=0)
    assert effective_score(a) == constants.SCORE_CLEAN


def test_hint_assisted_correct_scores_half() -> None:
    a = make_attempt(correct=True, hints_used=1)
    assert effective_score(a) == constants.SCORE_HINT_ASSISTED


def test_wrong_scores_zero() -> None:
    a = make_attempt(correct=False)
    assert effective_score(a) == constants.SCORE_WRONG


def test_lesson_context_suspends_hint_assisted_row() -> None:
    """§3 precedence: correct-with-hints in a lesson scores clean (1.0), not
    the hint-assisted 0.5 — the low weight already prices in scaffolding."""
    a = make_attempt(context="lesson", correct=True, hints_used=3)
    assert effective_score(a) == constants.SCORE_CLEAN


def test_lesson_context_wrong_still_scores_zero() -> None:
    a = make_attempt(context="lesson", correct=False)
    assert effective_score(a) == constants.SCORE_WRONG


def test_is_abandoned_true_for_no_interaction_final_timeout() -> None:
    a = make_attempt(correct=False, timed_out=True, engaged=False, session_last_event=True)
    assert is_abandoned(a) is True


def test_is_abandoned_false_for_mid_session_no_interaction_timeout() -> None:
    a = make_attempt(correct=False, timed_out=True, engaged=False, session_last_event=False)
    assert is_abandoned(a) is False


def test_is_abandoned_false_when_engaged_even_if_last_event() -> None:
    a = make_attempt(correct=False, timed_out=True, engaged=True, session_last_event=True)
    assert is_abandoned(a) is False


def test_is_abandoned_false_when_not_timed_out() -> None:
    a = make_attempt(correct=False, timed_out=False, engaged=False, session_last_event=True)
    assert is_abandoned(a) is False


def test_effective_score_none_when_abandoned() -> None:
    a = make_attempt(correct=False, timed_out=True, engaged=False, session_last_event=True)
    assert effective_score(a) is None


def test_mid_session_no_interaction_timeout_scores_zero() -> None:
    a = make_attempt(correct=False, timed_out=True, engaged=False, session_last_event=False)
    assert effective_score(a) == constants.SCORE_WRONG


# --- k_schedule(): §3 K = K0 / (1 + n/n0), floored at K_min ------------------

def test_k_schedule_at_zero_n_is_k0() -> None:
    assert k_schedule(0.0) == pytest.approx(constants.K0)


def test_k_schedule_decreases_with_n() -> None:
    assert k_schedule(constants.N0) < k_schedule(0.0)


def test_k_schedule_floors_at_k_min() -> None:
    assert k_schedule(1_000_000.0) == pytest.approx(constants.K_MIN)


# --- weight_for(): §3 n-increment weight classes -----------------------------

def test_weight_clean_unassisted_is_one() -> None:
    a = make_attempt(correct=True, hints_used=0)
    assert weight_for(a, constants.SCORE_CLEAN) == 1.0


def test_weight_hint_assisted_is_half() -> None:
    a = make_attempt(correct=True, hints_used=1)
    assert weight_for(a, constants.SCORE_HINT_ASSISTED) == constants.HINT_ASSISTED_WEIGHT


def test_weight_lesson_is_quarter() -> None:
    a = make_attempt(context="lesson", correct=True, hints_used=2)
    assert weight_for(a, constants.SCORE_CLEAN) == constants.LESSON_WEIGHT


# --- apply_attempt(): the full state delta -----------------------------------

def test_clean_correct_at_e_half_raises_theta_by_k_times_half() -> None:
    state = SkillState(theta=0.5, n=0.0)
    attempt = make_attempt(b_prior_at_serve=0.5, correct=True, hints_used=0)
    result = apply_attempt(state, attempt)
    k = constants.K0  # n = 0
    assert result.theta_after == pytest.approx(0.5 + k * 0.5)
    assert result.theta_before == 0.5


def test_wrong_at_e_half_lowers_theta_symmetrically() -> None:
    state = SkillState(theta=0.5, n=0.0)
    attempt = make_attempt(b_prior_at_serve=0.5, correct=False)
    result = apply_attempt(state, attempt)
    k = constants.K0
    assert result.theta_after == pytest.approx(0.5 - k * 0.5)


def test_hint_assisted_correct_lowers_theta_when_e_above_half() -> None:
    """SM §3's stated marginal behavior: needing hints on an item you were
    expected to solve cleanly (E > 0.5) is negative evidence."""
    state = SkillState(theta=1.0, n=0.0)  # theta >> b => E > 0.5
    attempt = make_attempt(b_prior_at_serve=0.3, correct=True, hints_used=1)
    result = apply_attempt(state, attempt)
    assert result.theta_after < result.theta_before


def test_apply_attempt_does_not_mutate_input_state() -> None:
    state = SkillState(theta=0.5, n=0.0)
    attempt = make_attempt(correct=True)
    apply_attempt(state, attempt)
    assert state.theta == 0.5
    assert state.n == 0.0


def test_numeric_entry_uses_unadjusted_expectation_in_update() -> None:
    state = SkillState(theta=0.7, n=2.0)
    attempt = make_attempt(question_type="numeric_entry", b_prior_at_serve=0.4, correct=True)
    result = apply_attempt(state, attempt)
    raw_e = 1.0 / (1.0 + 10.0 ** (-(0.7 - 0.4) / constants.S))
    assert result.expectation == pytest.approx(raw_e)


def test_pure_guess_mcq4_neutrality_expected_drift() -> None:
    """With the E' guess-floor adjustment, a pure random 4-option guesser's
    (25% correct, independent of ability) expected score matches E' when
    theta is far below b (unadjusted E ~ 0, so E' ~ MCQ4_GUESS_FLOOR, the
    same 0.25 as the guess rate) — i.e. expected θ drift is ~0 (§3).

    A single long chained simulation is the wrong tool here: each step's
    update is a zero-mean random-walk increment, so its cumulative variance
    grows without bound rather than shrinking, and a fixed seed can land
    arbitrarily far from zero by chance. Averaging many independent trials
    of the effective score against E' directly tests the "s - E' = 0 on
    average" claim without that compounding-variance trap.
    """
    rng = random.Random(42)
    theta, b = -5.0, 0.5
    e_prime = expectation(theta, b, constants.MCQ4_QUESTION_TYPE)
    n_trials = 20_000
    total_score = 0.0
    for _ in range(n_trials):
        correct = rng.random() < 0.25
        attempt = make_attempt(
            question_type=constants.MCQ4_QUESTION_TYPE,
            b_prior_at_serve=b,
            correct=correct,
            hints_used=0,
        )
        total_score += effective_score(attempt)
    mean_score = total_score / n_trials
    assert mean_score == pytest.approx(e_prime, abs=0.02)


# --- abandonment guard: full state-change assertions -------------------------

def test_discarded_attempt_leaves_state_unchanged_and_no_n_increment() -> None:
    state = SkillState(theta=0.4, n=3.0)
    attempt = make_attempt(correct=False, timed_out=True, engaged=False, session_last_event=True)
    result = apply_attempt(state, attempt)
    assert result.discarded is True
    assert result.new_state.theta == 0.4
    assert result.new_state.n == 3.0
    assert result.theta_after == result.theta_before
    assert result.n_after == result.n_before


def test_mid_session_no_interaction_timeout_changes_state() -> None:
    state = SkillState(theta=0.6, n=1.0)
    attempt = make_attempt(
        b_prior_at_serve=0.2, correct=False, timed_out=True, engaged=False, session_last_event=False
    )
    result = apply_attempt(state, attempt)
    assert result.discarded is False
    assert result.new_state.theta < state.theta
    assert result.new_state.n == pytest.approx(1.0 + 1.0)


# --- K schedule effects inside apply_attempt --------------------------------

def test_early_attempts_move_theta_more_than_late_ones() -> None:
    early_state = SkillState(theta=0.5, n=0.0)
    late_state = SkillState(theta=0.5, n=50.0)
    attempt = make_attempt(b_prior_at_serve=0.5, correct=True)
    early_result = apply_attempt(early_state, attempt)
    late_result = apply_attempt(late_state, attempt)
    early_delta = abs(early_result.theta_after - early_result.theta_before)
    late_delta = abs(late_result.theta_after - late_result.theta_before)
    assert early_delta > late_delta


def test_n_increments_by_weight_class() -> None:
    clean = apply_attempt(SkillState(), make_attempt(correct=True, hints_used=0))
    assert clean.n_after - clean.n_before == pytest.approx(1.0)

    hinted = apply_attempt(SkillState(), make_attempt(correct=True, hints_used=1))
    assert hinted.n_after - hinted.n_before == pytest.approx(constants.HINT_ASSISTED_WEIGHT)

    lesson = apply_attempt(
        SkillState(), make_attempt(context="lesson", correct=True, hints_used=1)
    )
    assert lesson.n_after - lesson.n_before == pytest.approx(constants.LESSON_WEIGHT)


# --- multi-skill credit assignment (§3, core doc §2.10) ----------------------

def test_multi_skill_success_updates_each_skill_at_half_weight_full_score() -> None:
    state_a = SkillState(theta=0.5, n=0.0)
    state_b = SkillState(theta=0.3, n=1.0)
    attempt = make_attempt(
        skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
        b_prior_at_serve=0.5,
        correct=True,
    )
    result_a = apply_attempt(state_a, attempt)
    result_b = apply_attempt(state_b, attempt)

    assert result_a.weight == constants.MULTI_SKILL_WEIGHT
    assert result_a.score == constants.SCORE_CLEAN
    assert result_a.queue_probes is False
    k_a = k_schedule(0.0)
    e_a = expectation(0.5, 0.5, "numeric_entry")
    assert result_a.theta_after == pytest.approx(0.5 + constants.MULTI_SKILL_WEIGHT * k_a * (1.0 - e_a))
    assert result_a.n_after == pytest.approx(constants.MULTI_SKILL_WEIGHT)

    assert result_b.weight == constants.MULTI_SKILL_WEIGHT
    assert result_b.n_after == pytest.approx(1.0 + constants.MULTI_SKILL_WEIGHT)


def test_multi_skill_failure_updates_nothing_and_queues_probes() -> None:
    state_a = SkillState(theta=0.5, n=2.0)
    state_b = SkillState(theta=0.3, n=1.0)
    attempt = make_attempt(
        skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
        b_prior_at_serve=0.5,
        correct=False,
    )
    result_a = apply_attempt(state_a, attempt)
    result_b = apply_attempt(state_b, attempt)

    for result, original in ((result_a, state_a), (result_b, state_b)):
        assert result.queue_probes is True
        assert result.new_state.theta == original.theta
        assert result.new_state.n == original.n
        assert result.theta_after == result.theta_before
        assert result.n_after == result.n_before


# --- lesson context (§3) ------------------------------------------------------

def test_lesson_correct_with_hints_scores_clean_at_quarter_weight() -> None:
    state = SkillState(theta=0.4, n=0.0)
    attempt = make_attempt(context="lesson", b_prior_at_serve=0.5, correct=True, hints_used=2)
    result = apply_attempt(state, attempt)
    assert result.score == constants.SCORE_CLEAN
    assert result.weight == constants.LESSON_WEIGHT
    assert result.n_after == pytest.approx(constants.LESSON_WEIGHT)
