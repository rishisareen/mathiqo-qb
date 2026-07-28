"""Certification and review scheduling — the SM §6/§7 transitions.

`maybe_certify` is the only writer of `certified_depth`: it fires on an
unassisted correct response in a low-guess format outside lesson context
(§7's "set only by" list), computing the depth the evidence actually
supports as `min(depth_served, θ − MASTERY_MARGIN)` — capped both by what
was practiced and by what the mastery threshold `E(θ, d) ≥ 0.8` currently
supports (constants.py's MASTERY_MARGIN docstring derives that equivalence).
It never lowers an existing certified_depth (§7 doesn't discuss decreases;
"dependents block, they don't revoke" — §6 point 3 — argues the same
direction for depth as it does for lapse).

`apply_review_outcome` is the interval-and-lapse half of a review probe
(§6): pass grows the interval geometrically (capped) with θ untouched; fail
resets the interval and sets `lapsed`. It does not score the probe itself —
per §6 point 2, a failed probe "is treated as a wrong attempt", so the
caller routes it through update.py's `apply_attempt` first and passes the
resulting state in here.

Both functions take an explicit `now` rather than reading a clock (a later
unit supplies the single session timestamp the engine uses throughout) —
same discipline as `certification_status` in state.py, which this module
calls into rather than reimplementing.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Tuple, Union

from . import constants
from .state import STATUS_DUE, SkillState, certification_status
from .update import Attempt


def maybe_certify(state: SkillState, attempt: Attempt, now: datetime) -> SkillState:
    """§7: certify (or re-certify) on a qualifying unassisted correct attempt.

    Guard (all must hold, per §7's "set only by" / "never" lists):
      - attempt.correct
      - attempt.hints_used == 0 (unassisted — a hint-assisted correct never
        certifies, same as the §3 outcome table treats it as partial credit)
      - attempt.context != "lesson" (lesson attempts never certify)
      - len(attempt.skill_ids) == 1 (§7: "never set by multi-skill items",
        independent of question_type — a multi-skill item can still use a
        low-guess format string, so this needs its own check)
      - attempt.question_type in LOW_GUESS_FORMATS (§7: never MCQ)
      - attempt.depth_served is not None (nothing to certify against)

    When the guard holds, `θ − MASTERY_MARGIN` is the deepest depth whose
    mastery threshold current θ clears; `depth_served` is the deepest depth
    there's actually evidence for. The min of the two is what this attempt
    can support. If that's negative (θ doesn't clear the bar at any
    practiced depth), nothing certifies. Otherwise certified_depth becomes
    max(existing, candidate) — never decreasing — and review_due /
    review_interval_days reset to the initial §6 interval with `lapsed`
    cleared (this is also how re-earning after a lapse restores prerequisite
    satisfaction, §6 point 3).
    """
    if not attempt.correct:
        return state
    if attempt.hints_used > 0:
        return state
    if attempt.context == "lesson":
        return state
    if len(attempt.skill_ids) != 1:
        return state
    if attempt.question_type not in constants.LOW_GUESS_FORMATS:
        return state
    if attempt.depth_served is None:
        return state

    max_supported_depth = state.theta - constants.MASTERY_MARGIN
    candidate_depth = min(attempt.depth_served, max_supported_depth)
    if candidate_depth < 0:
        return state

    new_state = copy.deepcopy(state)
    existing = state.certified_depth
    new_state.certified_depth = candidate_depth if existing is None else max(existing, candidate_depth)
    _reset_to_initial_interval(new_state, now)
    new_state.lapsed = False
    return new_state


def _reset_to_initial_interval(state: SkillState, now: datetime) -> None:
    """Mutate `state` in place to the initial §6 review interval — shared by
    `maybe_certify` (fresh/re-earned certification) and `apply_review_outcome`
    (a failed probe). Caller owns the deep copy; this only sets the two
    fields both call sites reset identically."""
    state.review_interval_days = constants.REVIEW_INITIAL_INTERVAL_DAYS
    state.review_due = now + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)


def apply_review_outcome(state: SkillState, passed: bool, now: datetime) -> SkillState:
    """§6 point 2: the interval-and-lapse side of a due review probe.

    Pass: interval ×REVIEW_INTERVAL_MULTIPLIER, capped at
    REVIEW_MAX_INTERVAL_DAYS; θ is untouched here (the probe being a clean
    correct response is not itself new evidence beyond what already earned
    the certification — §6 is silent on re-scoring θ on a pass, and the
    point of "θ unchanged" is explicit in the plan).

    Fail: caller has already routed the probe through apply_attempt as an
    ordinary wrong attempt (θ already reflects that) — this function only
    resets the interval to the initial §6 value and sets `lapsed`.
    certified_depth is left untouched either way: lapsed blocks dependents
    from being served, it does not revoke the depth earned (§6 point 3).
    """
    new_state = copy.deepcopy(state)
    if passed:
        new_interval = min(
            state.review_interval_days * constants.REVIEW_INTERVAL_MULTIPLIER,
            constants.REVIEW_MAX_INTERVAL_DAYS,
        )
        new_state.review_interval_days = new_interval
        new_state.review_due = now + timedelta(days=new_interval)
    else:
        _reset_to_initial_interval(new_state, now)
        new_state.lapsed = True
    return new_state


RecordsInput = Union[Dict[str, SkillState], Iterable[Tuple[str, SkillState]]]


def due_reviews(records: RecordsInput, now: datetime) -> List[Tuple[str, SkillState]]:
    """§6 point 4: which skills owe a review probe, oldest-due first.

    `records` is either a mapping (e.g. StudentModelStore.records_for(...))
    or any iterable of (skill_id, SkillState) pairs — callers scope this to
    one student before calling in, same as records_for does; this module has
    no student_id of its own to key a StudentModelStore by.

    "Due" here means certification_status(state, now) == STATUS_DUE — the
    §7 status machine's own read, not a reimplementation of it.
    """
    pairs = records.items() if hasattr(records, "items") else records
    due = [
        (skill_id, state)
        for skill_id, state in pairs
        if certification_status(state, now) == STATUS_DUE
    ]
    due.sort(key=lambda pair: pair[1].review_due)
    return due
