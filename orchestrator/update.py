"""The θ update rule as pure functions (student-model doc §3).

Every attempt updates θ ← θ + K·(s − E) for a served skill: `expectation`
computes E (§2) with the MCQ guessing floor applied by format class (§3);
`effective_score` resolves the outcome table, the abandonment guard, and the
lesson-context precedence rule into s (§3); `apply_attempt` ties them
together with the K schedule (§3) and the multi-skill credit-assignment rule
(§3; core doc §2.10) into one state delta — per KTD7, a pure function over
SkillState plus an attempt context, returning the new state and the event
fields (theta_before/after, n_before) so the session runner and log replay
can call the identical function and make replay equivalence a property test.

`apply_attempt` never mutates the SkillState you pass it: the updated record
comes back as a copy in UpdateResult.new_state. That's what makes "call the
same function during replay" safe — no aliasing between the live store and
whatever the caller does with the result.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import List, Optional

from . import constants
from .state import SkillState


@dataclass
class Attempt:
    """The scoring-relevant subset of one attempt-event row (schema §9).

    Identity/provenance fields the schema also carries (template_id,
    bank_version, engine_version, ...) belong to the caller's event
    construction, not to the update rule — `event_id` is kept here only
    because `record_attempt` needs it for the §4 misconception window.

    `engaged` and `session_last_event` back the §3 abandonment guard: was
    there any interaction on the item (a digit entered, an option selected,
    a hint opened), and is this the last event of its session. Neither is
    computable from a single attempt in isolation — the caller (session
    runner, or log replay reading session boundaries) determines both.
    """

    skill_ids: List[str]
    question_type: str
    b_prior_at_serve: float
    correct: bool
    hints_used: int = 0
    timed_out: bool = False
    engaged: bool = True
    session_last_event: bool = False
    context: str = "practice"  # placement / practice / review / probe / lesson (§9)
    misconception_id: Optional[str] = None
    event_id: str = ""


@dataclass
class UpdateResult:
    """Pure output of apply_attempt: the new state plus attempt-event fields.

    Field names mirror the attempt-event schema's ASCII θ spellings
    (theta_before, theta_after, n_before) so callers can copy them straight
    into the event they log (§9).

    `discarded=True` is the abandonment guard firing (§3): no attempt event
    should be logged at all, and `new_state` is an unchanged copy.
    `queue_probes=True` is the multi-skill failure signal (§3): the item's
    skills are unchanged and the caller should queue single-skill
    diagnostic probes instead of guessing which skill was at fault.
    `score`, `expectation`, `k`, and `weight` are the rule's intermediate
    values (None when discarded or when a multi-skill failure short-circuits
    the usual update), exposed for logging/debugging and tests.
    """

    new_state: SkillState
    theta_before: float
    theta_after: float
    n_before: float
    n_after: float
    discarded: bool = False
    score: Optional[float] = None
    expectation: Optional[float] = None
    k: Optional[float] = None
    weight: Optional[float] = None
    queue_probes: bool = False


def expectation(theta: float, b: float, question_type: str) -> float:
    """E(θ, b) per §2, with the §3 MCQ guessing floor applied by format class.

    4-option MCQ replaces E with E′ = 0.25 + 0.75·E (MCQ4_GUESS_FLOOR).
    Every other format — explicitly the low-guess formats (numeric entry,
    drag-and-drop, visual counting) and, per §10, anything else presently
    undecided beyond v1's MCQ + numeric-entry scope — uses unadjusted E.
    """
    e = 1.0 / (1.0 + 10.0 ** (-(theta - b) / constants.S))
    if question_type == constants.MCQ4_QUESTION_TYPE:
        return constants.MCQ4_GUESS_FLOOR + (1.0 - constants.MCQ4_GUESS_FLOOR) * e
    return e


def is_abandoned(attempt: Attempt) -> bool:
    """§3 abandonment guard: a no-interaction timeout that is also the
    session's last event is discarded outright — the child closed the app
    for dinner, not evidence about the skill. A *mid-session*
    no-interaction timeout does NOT trigger this guard; it scores 0.0
    instead (§3 — that default is deliberate and revisitable with data).
    """
    return attempt.timed_out and not attempt.engaged and attempt.session_last_event


def effective_score(attempt: Attempt) -> Optional[float]:
    """The §3 outcome table for single-skill (including lesson-context)
    attempts: clean correct 1.0, hint-assisted correct 0.5, wrong/timeout 0.0.

    Returns None when `is_abandoned` discards the attempt entirely. Multi-
    skill credit assignment does not go through this three-row table at all
    (§3) — see `apply_attempt`.
    """
    if is_abandoned(attempt):
        return None
    if not attempt.correct:
        return constants.SCORE_WRONG
    if attempt.context == "lesson":
        # §3 precedence: lesson context suspends the hint-assisted row — a
        # correct answer scores clean (1.0) regardless of hint use, because
        # lesson hints are pre-opened by design and the low lesson weight
        # already prices in the scaffolding. Implementing the table
        # literally here would double-penalize every lesson attempt.
        return constants.SCORE_CLEAN
    if attempt.hints_used > 0:
        return constants.SCORE_HINT_ASSISTED
    return constants.SCORE_CLEAN


def k_schedule(n_before: float) -> float:
    """K = K₀ / (1 + n/n₀), floored at K_min (§3).

    Early attempts (n small) move θ a lot; established estimates move
    slowly and shrug off single outliers.
    """
    return max(constants.K0 / (1.0 + n_before / constants.N0), constants.K_MIN)


def weight_for(attempt: Attempt, score: float) -> float:
    """§3 evidence weight for the single-skill path — what n increments by.

    1.0 for clean/wrong unassisted attempts, HINT_ASSISTED_WEIGHT for
    hint-assisted, LESSON_WEIGHT for lesson context (lesson takes
    precedence since the hint-assisted row is already suspended there, see
    effective_score). Multi-skill items don't call this — apply_attempt
    applies MULTI_SKILL_WEIGHT directly, since multi-skill credit
    assignment is a different axis from the single-skill outcome table.
    """
    if attempt.context == "lesson":
        return constants.LESSON_WEIGHT
    if score == constants.SCORE_HINT_ASSISTED:
        return constants.HINT_ASSISTED_WEIGHT
    return 1.0


def apply_attempt(state: SkillState, attempt: Attempt) -> UpdateResult:
    """Turn one attempt into a state delta (§3) — the pure function KTD7 asks for.

    Never mutates `state`; the updated record comes back as a copy in
    UpdateResult.new_state. Three shapes of result:
      - abandonment guard fires: new_state is an unchanged copy,
        `discarded=True` (§3 — no event should be logged at all).
      - multi-skill item, not correct: new_state is an unchanged copy,
        `queue_probes=True` (§3 credit-assignment rule — on failure nothing
        updates and diagnosis stays single-skill).
      - otherwise: θ and n move by θ ← θ + weight·K·(s − E), n += weight,
        and record_attempt runs on the copy for the §4 misconception window.
        Multi-skill success uses SCORE_CLEAN at MULTI_SKILL_WEIGHT; every
        other case (including lesson) goes through effective_score /
        weight_for.
    """
    theta_before = state.theta
    n_before = state.n

    if is_abandoned(attempt):
        return UpdateResult(
            new_state=copy.deepcopy(state),
            theta_before=theta_before,
            theta_after=theta_before,
            n_before=n_before,
            n_after=n_before,
            discarded=True,
        )

    is_multi_skill = len(attempt.skill_ids) > 1

    if is_multi_skill:
        if not attempt.correct:
            return UpdateResult(
                new_state=copy.deepcopy(state),
                theta_before=theta_before,
                theta_after=theta_before,
                n_before=n_before,
                n_after=n_before,
                queue_probes=True,
            )
        score = constants.SCORE_CLEAN
        weight = constants.MULTI_SKILL_WEIGHT
    else:
        score = effective_score(attempt)
        assert score is not None  # is_abandoned already ruled this out above
        weight = weight_for(attempt, score)

    e = expectation(theta_before, attempt.b_prior_at_serve, attempt.question_type)
    k = k_schedule(n_before)
    theta_after = theta_before + weight * k * (score - e)
    n_after = n_before + weight

    new_state = copy.deepcopy(state)
    new_state.theta = theta_after
    new_state.n = n_after
    new_state.record_attempt(attempt.event_id, attempt.misconception_id)

    return UpdateResult(
        new_state=new_state,
        theta_before=theta_before,
        theta_after=theta_after,
        n_before=n_before,
        n_after=n_after,
        score=score,
        expectation=e,
        k=k,
        weight=weight,
    )
