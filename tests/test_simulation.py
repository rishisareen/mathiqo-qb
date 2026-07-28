"""Tests for U7 — the closing argument: simulated students with a hidden true
ability run the *whole* Generator->Orchestrator serving loop headlessly,
through the real `session.run_session` (U6), and the system is asserted to
behave as the design docs predict (student-model doc §4 misconception
trigger, §5 placement, §6 review, §7 certification).

This file adds no new orchestrator/tools code — only a test-only
`SimulatedStudent` (a know-or-guess responder built directly on
`update.expectation`, R2's exact guess-adjusted model) and a test-only
reactive `session.InputSource` subclass that answers whatever was most
recently rendered, rather than following a fixed script.

**How the reactive InputSource identifies what's on screen.** `run_session`
renders exactly two formats (session.py's `render_item` / `render_mcq_options`):
`f"[{serve.context}] {instance['stem']}"` for the stem, and
`f"  {opt.label}) {opt.value}"` per MCQ option. A shared mutable
`_LastRendered` box is written by a small `notify` closure and read by
`SimulatedStudentInputSource.poll`: the stem text is matched back to
`bank.by_id[...]['stem']` to recover the actual instance dict being served,
and from there the student uses the instance directly (question_type,
difficulty, distractors, answer) for its ability-based decision — never the
rendered option *text* itself (see module docstring cross-reference in the
class below for why `[warn] ...` lines must NOT be mistaken for a stem line).

Fixtures follow tests/test_engine.py's and tests/test_session.py's own
convention: a real bank built from content/templates via tools/bank_build.py
(session-scoped, built once), and the real content/fractions/*.yaml graph.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from conftest import _bypass_placement, _numeric_only_bank, _single_skill_bank
from orchestrator import certification, constants, engine, log as log_module, session
from orchestrator.state import SkillState, StudentModelStore, is_certified
from orchestrator.update import Attempt, expectation

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content" / "fractions"
TEMPLATES_DIR = REPO_ROOT / "content" / "templates"

NOW = datetime(2026, 7, 27, 12, 0, 0)

# A grade with no mapping rows for any fractions skill (mappings.yaml only
# has rows for grades 4-8) — used by tests that isolate one skill and don't
# want the serving ceiling (KTD9) to cap out at a mapped tier before the
# test's own tolerance band is reached. Falls back to TIER_DEPTH["hard"]
# (0.8) via engine.serving_ceiling's "no mapping row" branch.
UNMAPPED_GRADE = 9

# `content` and `bank` (session-scoped fixtures) and `_bypass_placement` /
# `_single_skill_bank` / `_numeric_only_bank` live in tests/conftest.py,
# shared with test_engine.py and test_session.py — see that module's
# docstring. `_family_bank` below is specific to this file.


# ===========================================================================
# small bank-slicing / store-seeding helpers
# ===========================================================================


def _family_bank(bank_index: engine.BankIndex, skill_ids: List[str]) -> engine.BankIndex:
    """A copy of `bank_index` restricted to a small set of skills — used to
    remove ambiguity from the frontier-practice tie-break (engine.py's
    `_priority`) in tests that care about which *one* skill gets served
    next, without needing to reason about the whole graph's alphabetical
    ordering."""
    instances_by_skill = {sid: bank_index.instances_by_skill[sid] for sid in skill_ids if sid in bank_index.instances_by_skill}
    by_id = {inst["item_instance_id"]: inst for pool in instances_by_skill.values() for inst in pool}
    return engine.BankIndex(bank_version=bank_index.bank_version, instances_by_skill=instances_by_skill, by_id=by_id)


def _seed_certified(
    store: StudentModelStore,
    student_id: str,
    skill_id: str,
    depth: float,
    theta: float = 0.9,
    n: float = 5.0,
    review_due: datetime = NOW + timedelta(days=365),
) -> None:
    """Directly certify a prerequisite so a test can serve a *downstream*
    skill without walking the whole graph through placement first — the
    same "construct the state directly" isolation tests/test_engine.py uses
    for its own prerequisite-gating tests. `review_due` defaults far in the
    future so this seeded prerequisite is never itself picked up by
    select_next's due-review priority (R12 step 1) during the test."""
    store.write_records(student_id, {skill_id: SkillState(theta=theta, n=n, certified_depth=depth, review_due=review_due)})


# ===========================================================================
# SimulatedStudent: the R2 know-or-guess responder
# ===========================================================================


@dataclass
class SimulatedStudent:
    """A hidden true ability per skill (`true_theta`, falling back to
    `default_true_theta` for any skill not named — the uniform-ability
    simplification documented in this unit's final report) drives a
    know-or-guess responder built directly on `update.expectation` (R2):
    with probability E(theta_true, b, question_type) — the *same* function
    and the *same* MCQ guess-floor adjustment the update rule itself uses —
    the student answers correctly. Otherwise it answers wrong: for mcq_4 it
    guesses among the wrong options, and for a low-guess format it "guesses"
    a wrong numeric value — in both cases habit-weighted toward a configured
    misconception's distractor when the served instance carries one and a
    habit is configured for that skill, otherwise uniformly at random among
    the available wrong values. This is what keeps convergence unbiased on
    MCQ-containing diets: the response model and the update rule agree
    exactly on what "probability of a clean correct" means for a given
    (theta, b, question_type).
    """

    true_theta: Dict[str, float] = field(default_factory=dict)
    default_true_theta: float = 0.0
    misconception_habit: Optional[Tuple[str, str]] = None  # (skill_id, misconception_id)
    habit_weight: float = 0.85
    rng: random.Random = field(default_factory=random.Random)

    def theta_for(self, skill_id: str) -> float:
        return self.true_theta.get(skill_id, self.default_true_theta)

    def respond(self, instance: dict, options: Optional[List["session.MCQOption"]]) -> str:
        """Returns the raw line to "type": an MCQ option label, or a numeric
        literal string (always parseable by `session.parse_numeric_response`,
        since it is always either the instance's own canonical `answer`
        string or a distractor's stored `value` string, both authored in
        that literal form by tools/bank_build.py)."""
        skill_id = instance["skill_ids"][0]
        theta = self.theta_for(skill_id)
        p_know = expectation(theta, instance["difficulty"], instance["question_type"])
        knows = self.rng.random() < p_know
        if options is not None:  # mcq_4
            if knows:
                return next(o for o in options if o.is_correct).label
            return self._pick_wrong_option(skill_id, options).label
        if knows:
            return instance["answer"]
        return self._pick_wrong_numeric(skill_id, instance)

    def _habit_matches(self, skill_id: str) -> Optional[str]:
        if self.misconception_habit is not None and self.misconception_habit[0] == skill_id:
            return self.misconception_habit[1]
        return None

    def _pick_wrong_option(self, skill_id: str, options: List["session.MCQOption"]) -> "session.MCQOption":
        wrong = [o for o in options if not o.is_correct]
        habit_misconception = self._habit_matches(skill_id)
        if habit_misconception is not None:
            habit_target = next((o for o in wrong if o.misconception_id == habit_misconception), None)
            if habit_target is not None and self.rng.random() < self.habit_weight:
                return habit_target
        return self.rng.choice(wrong)

    def _pick_wrong_numeric(self, skill_id: str, instance: dict) -> str:
        distractors = instance.get("distractors", [])
        habit_misconception = self._habit_matches(skill_id)
        if habit_misconception is not None:
            habit_target = next((d for d in distractors if d["misconception_id"] == habit_misconception), None)
            if habit_target is not None and self.rng.random() < self.habit_weight:
                return habit_target["value"]
        if distractors:
            return self.rng.choice(distractors)["value"]
        return "999999/1000003"  # untagged-wrong fallback; never a real answer or distractor (test_session.py convention)


# ===========================================================================
# The reactive InputSource
# ===========================================================================


@dataclass
class _LastRendered:
    """The shared mutable box `notify` writes into and
    `SimulatedStudentInputSource.poll` reads from — the seam the plan asks
    for between the (pure, clock-agnostic) `collect_response` state machine
    and a caller that must decide what to "type" reactively."""

    stem: Optional[str] = None


# Only the three item-serving contexts that can appear in render_item's
# f"[{serve.context}] {stem}" format (placement / practice / review / probe
# — see attempt-event's context enum). This is deliberately narrower than
# "any bracketed word": select_next's own pool-exhaustion warning is
# rendered as f"[warn] {warning}" (run_session, right before render_item),
# which would otherwise false-match a naive r"^\[(\w+)\] (.*)$" and clobber
# `_LastRendered.stem` with warning text instead of the real stem — caught
# by tracing render order in session.py's run_session before writing this.
_STEM_RE = re.compile(r"^\[(placement|practice|review|probe)\] (.*)$")


def make_recording_notify(last: _LastRendered) -> Callable[[str], None]:
    def notify(text: str) -> None:
        m = _STEM_RE.match(text)
        if m:
            last.stem = m.group(2)

    return notify


class SimulatedStudentInputSource(session.InputSource):
    """Reactive InputSource (test-only, per this unit's Approach point 2):
    answers whatever `_LastRendered.stem` currently holds, using
    `SimulatedStudent.respond`. Never uses hints or times out — every
    `poll()` returns an answer immediately (`waited_sec=0.0`), so
    `collect_response`'s hint-auto-offer/timeout boundaries are never
    crossed and every served item is answered on the first try. Ends the
    session with a `quit` line once `max_items` responses have been given,
    so a session against a bank/graph that would otherwise serve forever
    (frontier practice never exhausts while any skill sits below its
    ceiling) terminates deterministically for the test.
    """

    def __init__(
        self,
        student: SimulatedStudent,
        bank_index: engine.BankIndex,
        last: _LastRendered,
        max_items: Optional[int] = None,
    ) -> None:
        self.student = student
        self._stem_to_instance = {inst["stem"]: inst for inst in bank_index.by_id.values()}
        self.last = last
        self.max_items = max_items
        self.served = 0

    def poll(self, timeout_sec: float) -> session.PollResult:
        if self.max_items is not None and self.served >= self.max_items:
            return session.PollResult(line="quit", waited_sec=0.0)
        stem = self.last.stem
        assert stem is not None, "poll() called before any item was rendered"
        instance = self._stem_to_instance.get(stem)
        assert instance is not None, f"rendered stem not found in the bank passed to this InputSource: {stem!r}"
        options = session.build_mcq_options(instance) if instance["question_type"] == "mcq_4" else None
        response = self.student.respond(instance, options)
        self.served += 1
        return session.PollResult(line=response, waited_sec=0.0)


class OneWrongThenQuitInputSource(session.InputSource):
    """A tiny scripted-but-not-fully-scripted InputSource for the review-
    lapse scenario: force a specific wrong numeric answer to the very first
    item served (the due review probe, which R12 step 1 guarantees is
    served first once due), then quit — ending the session without
    depending on the simulated student's stochastic response for the one
    attempt whose outcome (fail) the test needs to be deterministic."""

    def __init__(self, wrong_value: str) -> None:
        self._used = False
        self._wrong_value = wrong_value

    def poll(self, timeout_sec: float) -> session.PollResult:
        if not self._used:
            self._used = True
            return session.PollResult(line=self._wrong_value, waited_sec=0.0)
        return session.PollResult(line="quit", waited_sec=0.0)


# ===========================================================================
# Scenario 1: convergence — theta moves toward true ability, unbiased on an
# MCQ-containing diet
# ===========================================================================


def test_convergence_theta_lands_near_true_ability_within_tolerance(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    skill_id = "FRAC.ADD.UNLIKE"  # mixed numeric_entry (easy) + mcq_4 (medium) pool
    skill_bank = _single_skill_bank(bank, skill_id)
    prereqs = content.skills[skill_id].prerequisites

    final_theta: Dict[str, float] = {}
    for label, true_theta, seed in [("strong", 0.6, 1001), ("weak", 0.05, 2002)]:
        student_id = f"stu-converge-{label}"
        store = StudentModelStore()
        for p in prereqs:
            _seed_certified(store, student_id, p, depth=0.5)
        engine_state = _bypass_placement()
        log_path = tmp_path / f"log-{label}.jsonl"
        last = _LastRendered()
        student = SimulatedStudent(true_theta={skill_id: true_theta}, rng=random.Random(seed))
        io = SimulatedStudentInputSource(student, skill_bank, last, max_items=80)

        summary = session.run_session(
            store, engine_state, content, skill_bank, student_id, "CA", "ONTARIO", UNMAPPED_GRADE,
            f"sess-converge-{label}", log_path, io, make_recording_notify(last), NOW,
        )
        assert summary.items_served == 80
        final_theta[label] = store.peek(student_id, skill_id).theta

    assert final_theta["strong"] == pytest.approx(0.6, abs=0.3)
    assert final_theta["weak"] == pytest.approx(0.05, abs=0.3)
    assert final_theta["strong"] > final_theta["weak"]


# ===========================================================================
# Scenario 2: certification through numeric-entry only, with a review date
# ===========================================================================


def test_strong_student_certifies_via_numeric_entry_only_with_review_date(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    skill_id = "FRAC.UNIT"  # a root skill (no prerequisites) with a numeric_entry template
    assert content.skills[skill_id].prerequisites == ()
    numeric_bank = _single_skill_bank(_numeric_only_bank(bank), skill_id)
    for inst in numeric_bank.by_id.values():
        assert inst["question_type"] == "numeric_entry"

    student_id = "stu-cert-strong"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    last = _LastRendered()
    student = SimulatedStudent(true_theta={skill_id: 0.7}, rng=random.Random(4242))
    io = SimulatedStudentInputSource(student, numeric_bank, last, max_items=30)

    session.run_session(
        store, engine_state, content, numeric_bank, student_id, "CA", "ONTARIO", UNMAPPED_GRADE,
        "sess-cert", log_path, io, make_recording_notify(last), NOW,
    )

    state = store.peek(student_id, skill_id)
    assert state.certified_depth is not None
    assert state.review_due is not None
    assert state.review_due == NOW + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)

    # Every logged event that certified anything did so via numeric_entry
    # (the only question_type in this bank) — belt-and-suspenders check that
    # nothing in this test accidentally used a low-guess-format loophole.
    for event in log_module.iter_events(log_path):
        assert event["question_type"] == "numeric_entry"


# ===========================================================================
# Scenario 3: clock advance past review_due -> review served first; a
# failed probe lapses and blocks dependents
# ===========================================================================


def test_review_due_after_clock_advance_and_failed_probe_lapses_and_blocks_dependents(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    skill_id = "FRAC.UNIT"
    numeric_bank = _single_skill_bank(_numeric_only_bank(bank), skill_id)
    student_id = "stu-review-lapse"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"

    # Session 1 (day 0): certify quickly through a strong student.
    last = _LastRendered()
    student = SimulatedStudent(true_theta={skill_id: 0.8}, rng=random.Random(7))
    io = SimulatedStudentInputSource(student, numeric_bank, last, max_items=20)
    summary1 = session.run_session(
        store, engine_state, content, numeric_bank, student_id, "CA", "ONTARIO", UNMAPPED_GRADE,
        "sess-1", log_path, io, make_recording_notify(last), NOW,
    )
    certified_state = store.peek(student_id, skill_id)
    assert certified_state.certified_depth is not None
    certified_depth = certified_state.certified_depth
    assert certified_state.review_due == NOW + timedelta(days=constants.REVIEW_INITIAL_INTERVAL_DAYS)

    # Session 2 (day 8, KTD10 — a later `now` passed into a fresh
    # run_session call, not an internal clock-advance mechanism): the
    # certification's review_due (day 7) is now in the past.
    now2 = NOW + timedelta(days=8)
    io2 = OneWrongThenQuitInputSource(wrong_value="999999/1000003")
    summary2 = session.run_session(
        store, summary1.engine_state, content, numeric_bank, student_id, "CA", "ONTARIO", UNMAPPED_GRADE,
        "sess-2", log_path, io2, lambda _t: None, now2,
    )
    assert summary2.items_served == 1

    events = list(log_module.iter_events(log_path))
    review_events = [e for e in events if e["session_id"] == "sess-2"]
    assert len(review_events) == 1
    assert review_events[0]["context"] == "review"  # served first, per R12 step 1
    assert review_events[0]["correct"] is False

    lapsed_state = store.peek(student_id, skill_id)
    assert lapsed_state.lapsed is True
    assert lapsed_state.certified_depth == certified_depth  # revokes nothing (SM §6 point 3)
    assert certification.certification_status(lapsed_state, now2) == "lapsed"

    # Dependents blocked: the prerequisite check a dependent skill's frontier
    # gating would run (engine.select_next's `is_certified`) now fails at
    # exactly the depth this certification used to satisfy.
    assert is_certified(lapsed_state, certified_depth, now2) is False


# ===========================================================================
# Scenario 4: misconception habit triggers remediation, which then clears
# (KTD12)
# ===========================================================================


def test_misconception_habit_triggers_remediation_then_clears(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """FRAC.ADD.UNLIKE carries MISC.FRAC.ADD-ACROSS (both its numeric_entry
    and mcq_4 templates tag a distractor with it — confirmed by reading
    content/templates/TPL.FRAC.ADD.UNLIKE.{DIRECT,WORD}.001.yaml), whose
    registry entry (content/fractions/misconceptions.yaml) maps
    remediation_target: FRAC.EQUIV — exactly the pairing the plan's Approach
    section names as an example of real tagged distractor content."""
    trigger_skill = "FRAC.ADD.UNLIKE"
    remediation_target = content.misconceptions["MISC.FRAC.ADD-ACROSS"].remediation_target
    assert remediation_target == "FRAC.EQUIV"

    family_bank = _family_bank(bank, [trigger_skill, remediation_target])
    prereqs = content.skills[trigger_skill].prerequisites  # FRAC.EQUIV, FRAC.ADD.LIKE, NUM.LCM, FRAC.SIMPLIFY

    student_id = "stu-misconception"
    store = StudentModelStore()
    for p in prereqs:
        _seed_certified(store, student_id, p, depth=0.5)
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    last = _LastRendered()
    # true_theta near the bottom of the scale on the trigger skill so the
    # student almost never "knows" and, habit_weight=1.0, always answers
    # with the tagged distractor when wrong -> deterministic trigger within
    # MISC_TRIGGER_COUNT (2) attempts. Neutral ability elsewhere.
    student = SimulatedStudent(
        true_theta={trigger_skill: -1.0},
        default_true_theta=0.3,
        misconception_habit=(trigger_skill, "MISC.FRAC.ADD-ACROSS"),
        habit_weight=1.0,
        rng=random.Random(99),
    )
    io = SimulatedStudentInputSource(student, family_bank, last, max_items=40)

    session.run_session(
        store, engine_state, content, family_bank, student_id, "CA", "ONTARIO", UNMAPPED_GRADE,
        "sess-misc", log_path, io, make_recording_notify(last), NOW,
    )

    events = list(log_module.iter_events(log_path))
    trigger_events = [e for e in events if e["skill_ids"] == [trigger_skill]]
    remediation_events = [e for e in events if e["skill_ids"] == [remediation_target]]

    assert any(e["misconception_id"] == "MISC.FRAC.ADD-ACROSS" for e in trigger_events), (
        "the habit-weighted responder never actually produced a tagged wrong answer"
    )
    assert len(remediation_events) >= constants.REMEDIATION_DOSE_SIZE, (
        "expected at least one full remediation dose to have been served on the remediation target"
    )

    # KTD12: the dose is bounded and clears — after REMEDIATION_DOSE_SIZE
    # consecutive attempts on the remediation target while the queue's head
    # names it, the engine must return to serving the trigger skill (or, in
    # principle, another queued item) rather than remediation forever.
    # Reconstruct engine_state at the end and confirm the queue emptied.
    final_engine_state = engine.rebuild_engine_state(log_path, content, engine.make_static_bank_lookup(family_bank))
    assert not any(dose.skill_id == remediation_target for dose in final_engine_state.remediation_queue)

    # And the triggering skill's misconception counter was reset on trigger
    # (KTD12 — "consumed, not standing"), so it isn't left in a
    # permanently-tripped state.
    final_state = store.peek(student_id, trigger_skill)
    assert final_state.misc_count("MISC.FRAC.ADD-ACROSS") < constants.MISC_TRIGGER_COUNT


# ===========================================================================
# Scenario 5: placement — strong and weak students stop at different
# frontiers from the same starting point
# ===========================================================================


def test_placement_strong_and_weak_students_reach_different_frontiers(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    country, curriculum, grade = "CA", "ONTARIO", 4  # CA grade 4 mappings: FRAC.UNIT, FRAC.PARTWHOLE, FRAC.CMP (easy)

    def run(label: str, true_theta: float, seed: int) -> Dict[str, SkillState]:
        student_id = f"stu-placement-{label}"
        store = StudentModelStore()
        engine_state = engine.EngineState()  # real placement, not bypassed
        log_path = tmp_path / f"log-{label}.jsonl"
        last = _LastRendered()
        student = SimulatedStudent(default_true_theta=true_theta, rng=random.Random(seed))
        io = SimulatedStudentInputSource(student, bank, last, max_items=60)
        session.run_session(
            store, engine_state, content, bank, student_id, country, curriculum, grade,
            f"sess-placement-{label}", log_path, io, make_recording_notify(last), NOW,
        )
        return store.records_for(student_id)

    strong_records = run("strong", 0.9, 111)
    weak_records = run("weak", -0.9, 222)

    strong_certified = {sid for sid, s in strong_records.items() if s.certified_depth is not None}
    weak_certified = {sid for sid, s in weak_records.items() if s.certified_depth is not None}

    # A strong student should certify strictly more (or at least as many, on
    # a system-level-property rather than exact-trajectory basis) skills
    # than a weak student starting from the identical mapping/grade; the
    # two frontiers should differ.
    assert len(strong_certified) > len(weak_certified)
    assert strong_certified != weak_certified


# ===========================================================================
# Scenario 6: replay equivalence at scale (KTD7/KTD11/R15), end-to-end
# through a real simulated session
# ===========================================================================


def test_replay_equivalence_at_scale_matches_live_store_and_engine_state(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-replay-scale"
    store = StudentModelStore()
    engine_state = engine.EngineState()  # real placement, exercising the full R12 chain over a long session
    log_path = tmp_path / "log.jsonl"
    last = _LastRendered()
    # A reasonably strong, habit-prone student so the session organically
    # exercises placement, frontier practice, certification (and thus
    # review-date bookkeeping), and misconception-triggered remediation
    # within one run — "at scale" per the scenario's own name.
    student = SimulatedStudent(
        default_true_theta=0.5,
        misconception_habit=("FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS"),
        habit_weight=0.6,
        rng=random.Random(31415),
    )
    io = SimulatedStudentInputSource(student, bank, last, max_items=150)

    live_summary = session.run_session(
        store, engine_state, content, bank, student_id, "CA", "ONTARIO", 4,
        "sess-replay-scale", log_path, io, make_recording_notify(last), NOW,
    )
    assert live_summary.items_served == 150

    replayed_store = log_module.replay(log_path)
    replayed_engine_state = engine.rebuild_engine_state(log_path, content, engine.make_static_bank_lookup(bank))

    assert replayed_store.records_for(student_id) == store.records_for(student_id)

    live_engine_state = live_summary.engine_state
    assert replayed_engine_state.remediation_queue == live_engine_state.remediation_queue
    assert replayed_engine_state.stepdown_queue == live_engine_state.stepdown_queue
    assert replayed_engine_state.prereq_probe_queue == live_engine_state.prereq_probe_queue
    assert replayed_engine_state.placement_cursor == live_engine_state.placement_cursor

    # Known nuance (flagged by U6's implementer, confirmed here): live
    # serving marks an instance "seen" twice per attempt (once at
    # selection time in select_next, once again at outcome time in
    # consume_event) while pure replay only ever calls consume_event, so
    # seen_history *list lengths* legitimately differ between live and
    # replayed state even though nothing is actually wrong. The *set* of
    # instance ids served per skill matches exactly -- with exactly one
    # additional, narrower exception this test's own harness introduces and
    # confirmed empirically while writing it (not something the plan
    # described, so flagged here rather than silently special-cased away):
    # this session ends via the simulated student's own "quit" (the only
    # way to bound an otherwise-endless frontier-practice loop), and
    # run_session's loop always calls select_next -- which marks the chosen
    # instance "seen" -- *before* asking for a response. The very last
    # select_next call of the session picks (and marks seen) one instance
    # that the student then immediately quits on without answering; per
    # log.py's module docstring ("what gets logged"), a quit-before-answer
    # attempt is never logged at all, so replay -- which only ever learns
    # about seen instances via logged events -- structurally cannot know
    # about that one final selection. That is a real, different-in-kind gap
    # from the double-marking nuance above, not a bug: replayed_seen must
    # always be a *subset* of live_seen (every logged event was necessarily
    # selected live first), and the live-only surplus across the whole
    # engine_state must be exactly the one dangling quit-abandoned
    # selection, never more.
    all_skills = set(live_engine_state.seen_history) | set(replayed_engine_state.seen_history)
    live_only_total = 0
    for skill_id in all_skills:
        live_seen = set(live_engine_state.seen_history.get(skill_id, []))
        replayed_seen = set(replayed_engine_state.seen_history.get(skill_id, []))
        assert replayed_seen <= live_seen, f"replay saw an instance live never selected, for {skill_id}"
        live_only_total += len(live_seen - replayed_seen)
    assert live_only_total <= 1, (
        f"expected at most the one final quit-abandoned selection to differ between live and replay; "
        f"got {live_only_total} unexplained live-only instance(s)"
    )
    # And the lengths really do differ for at least one served skill,
    # confirming this test would have caught a regression to the "raw list
    # equality" version of this assertion rather than vacuously passing.
    assert any(
        len(live_engine_state.seen_history.get(sid, [])) != len(replayed_engine_state.seen_history.get(sid, []))
        for sid in all_skills
    )


def test_facade_routed_session_matches_direct_engine_path_outcomes(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """U6's own differential test: orchestrator/session.py now routes
    selection (steps 1-5 of select_next's priority chain) and every
    submission through orchestrator/serving.py's facade rather than
    reading engine.select_next/consume_event directly. This proves the
    switch is behavior-preserving: the same simulated student, driven the
    same number of items from the same seed, reaches the same
    theta/certification/seen-count outcome whether served through
    run_session (the facade path, U6) or through a hand-driven loop
    calling engine.select_next/consume_event with no candidates_for_skill
    override at all (the pre-U6 "direct engine" path) — the post-U3
    baseline this scenario is required to match, not a pre-plan fixture."""
    skill_id = "FRAC.ADD.UNLIKE"
    skill_bank = _single_skill_bank(bank, skill_id)
    prereqs = content.skills[skill_id].prerequisites
    max_items = 60
    seed = 777

    def _seeded_store(student_id: str) -> StudentModelStore:
        store = StudentModelStore()
        for p in prereqs:
            _seed_certified(store, student_id, p, depth=0.5)
        return store

    # --- facade path: the real session runner (U6) ---
    facade_store = _seeded_store("stu-facade")
    last = _LastRendered()
    facade_student = SimulatedStudent(default_true_theta=0.5, rng=random.Random(seed))
    io = SimulatedStudentInputSource(facade_student, skill_bank, last, max_items=max_items)
    facade_summary = session.run_session(
        facade_store, _bypass_placement(), content, skill_bank, "stu-facade", "CA", "ONTARIO", UNMAPPED_GRADE,
        "sess-facade", tmp_path / "facade.jsonl", io, make_recording_notify(last), NOW,
    )
    assert facade_summary.items_served == max_items

    # --- direct engine path: no facade, no candidates_for_skill override ---
    direct_store = _seeded_store("stu-direct")
    direct_engine_state = _bypass_placement()
    direct_student = SimulatedStudent(default_true_theta=0.5, rng=random.Random(seed))
    for i in range(max_items):
        result = engine.select_next(
            direct_store, direct_engine_state, content, skill_bank, "stu-direct", "CA", "ONTARIO", UNMAPPED_GRADE, NOW
        )
        direct_engine_state = result.engine_state
        serve = result.serve
        assert serve is not None, f"direct path ran out of items at {i}/{max_items}"
        instance = skill_bank.by_id[serve.item_instance_id]
        options = session.build_mcq_options(instance) if instance["question_type"] == "mcq_4" else None
        raw = direct_student.respond(instance, options)
        if options is not None:
            chosen = next(o for o in options if o.label == raw)
            correct, misconception_id, _ = session.grade_mcq_response(chosen)
        else:
            parsed = session.parse_numeric_response(raw)
            correct, misconception_id, _ = session.grade_numeric_response(instance, parsed)

        attempt = Attempt(
            skill_ids=[skill_id], question_type=instance["question_type"], b_prior_at_serve=instance["difficulty"],
            correct=correct, hints_used=0, timed_out=False, engaged=True, session_last_event=False,
            context=serve.context, misconception_id=misconception_id, event_id=f"direct-{i}",
            depth_served=instance["difficulty"],
        )
        state = direct_store.get("stu-direct", skill_id)
        composed = log_module.compose_attempt(state, attempt, serve.context, NOW)
        assert composed.new_state is not None
        direct_store.write_records("stu-direct", {skill_id: composed.new_state})

        outcome = engine.AttemptOutcome(
            student_id="stu-direct", skill_ids=[skill_id], item_instance_id=instance["item_instance_id"],
            variant_signature=instance["variant_signature"], correct=correct, misconception_id=misconception_id,
            context=serve.context, depth_served=instance["difficulty"],
        )
        consume_result = engine.consume_event(direct_engine_state, direct_store, content, skill_bank, outcome, NOW)
        direct_engine_state = consume_result.engine_state
        engine.apply_consume_patches(direct_store, "stu-direct", consume_result.skill_state_patches)

    facade_state = facade_store.peek("stu-facade", skill_id)
    direct_state = direct_store.peek("stu-direct", skill_id)
    assert facade_state.theta == pytest.approx(direct_state.theta)
    assert facade_state.n == pytest.approx(direct_state.n)
    assert facade_state.certified_depth == direct_state.certified_depth

    # seen_history length, not just theta/n/certified_depth: the facade
    # path's run_session ends via the simulated student's own "quit" after
    # max_items responses, and (per the documented quirk in
    # test_replay_equivalence_at_scale_matches_live_store_and_engine_state
    # above) select_next always marks its chosen instance seen *before* the
    # response is collected — so the facade path's final select_next call
    # marks one instance seen that is then immediately quit on, one entry
    # more than the direct path's loop (which stops exactly at max_items,
    # never selecting a never-answered item). Tolerate exactly that ±1.
    facade_engine_state = facade_summary.engine_state
    facade_seen = len(facade_engine_state.seen_history.get(skill_id, []))
    direct_seen = len(direct_engine_state.seen_history.get(skill_id, []))
    assert abs(facade_seen - direct_seen) <= 1, (facade_seen, direct_seen)
