"""Tests for orchestrator/batch.py (U9): the ladder batch planner and
policy object — R8-R11, R18, KD1, KD2, KTD5, KTD8.

Uses the shared session-scoped `content`/`bank` fixtures from
tests/conftest.py (a real bank built from every content/templates/*.yaml
file, real content/fractions graph) rather than a synthetic bank, since this
unit's own double-diagnosis test (scenario 6) must exercise the real
`consume_event` branches, not a hand-rolled stand-in of them.

`FRAC.ADD.UNLIKE` (mapped IN/NCERT/6, depth_expectation "medium" ->
ceiling 0.5) is used throughout: its two templates (DIRECT, numeric_entry,
difficulty 0.2; WORD, mcq_4, difficulty 0.5) give exactly the two depth
tiers this fixture content authors per skill, which is what makes a genuine
step-down ("easier" rung, depth 0.2 below a depth-0.5 target/harder pick)
possible at all — see plan_ladder's own docstring on why "harder" targets
the serving ceiling rather than a fixed offset. Every test that wants a
step-down sets theta=0.4, so target_depth = min(0.4+CHALLENGE_OFFSET,
ceiling) lands exactly on the 0.5 ceiling.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from conftest import _bypass_placement
from orchestrator import batch, constants, engine, serving
from orchestrator.state import StudentModelStore

SKILL_ID = "FRAC.ADD.UNLIKE"
COUNTRY = "IN"
CURRICULUM = "NCERT"
GRADE = 6
CONSUMER_ID = constants.CONSUMER_ID_INTERNAL
NOW = datetime(2026, 7, 27, 12, 0, 0)

# theta chosen so target_depth = min(theta + CHALLENGE_OFFSET, ceiling)
# lands exactly on FRAC.ADD.UNLIKE's ceiling (0.5, "medium") -- this is what
# makes both a "harder" pick (aimed at the ceiling) and a genuine "easier"
# step-down (from 0.5 down to the 0.2 tier) meaningful in this fixture's
# two-tier content, per the module docstring above.
THETA_AT_CEILING = 0.4


def _facade(content: engine.ContentIndex, bank_index: engine.BankIndex) -> serving.ServingFacade:
    return serving.ServingFacade(
        banks_dir=None, logs_dir=None, content=content, bank_lookup=engine.make_static_bank_lookup(bank_index)
    )


def _session(
    tmp_path: Path,
    bank_index: engine.BankIndex,
    student_id: str = "stu-batch",
    session_id: str = "sess-batch",
    theta: float = THETA_AT_CEILING,
) -> serving.Session:
    store = StudentModelStore()
    store.get(student_id, SKILL_ID).theta = theta
    return serving.Session(
        session_id=session_id,
        student_id=student_id,
        bank=bank_index,
        log_path=tmp_path / f"{student_id}.jsonl",
        store=store,
        engine_state=_bypass_placement(),
        country=COUNTRY,
        curriculum=CURRICULUM,
        grade=GRADE,
    )


# --- scenario 1: default batch spans the three rungs, contexts assigned ----


def test_default_batch_spans_three_rungs_with_correct_contexts(tmp_path, content, bank):
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)

    assert result.count_requested == constants.DEFAULT_BATCH_SIZE
    assert result.count_returned == constants.DEFAULT_BATCH_SIZE
    assert len(result.items) == constants.DEFAULT_BATCH_SIZE

    rungs = Counter(item.rung for item in result.items)
    assert rungs["target"] == constants.BATCH_MIX_TARGET
    assert rungs["harder"] == constants.BATCH_MIX_HARDER
    assert rungs["easier"] == constants.BATCH_MIX_EASIER

    for item in result.items:
        assert item.skill_id == SKILL_ID
        if item.rung == "easier":
            assert item.context == "probe"
        else:
            assert item.context == "practice"

    signatures = [item.instance["variant_signature"] for item in result.items]
    assert len(signatures) == len(set(signatures)), "no duplicate variant_signature within one batch"

    assert result.policy.policy_version == "1.0"
    assert result.policy.low_water == constants.BATCH_LOW_WATER
    assert result.bank_version == bank.bank_version
    assert result.content_version == content.content_version
    assert session.delivered == set(signatures)


# --- scenario 2: two consecutive batches never repeat; durable state untouched


def test_two_consecutive_batches_never_repeat_and_leave_durable_state_untouched(tmp_path, content, bank):
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    seen_before = dict(session.engine_state.seen_history)
    records_before = {
        skill_id: (state.theta, state.n, state.certified_depth)
        for skill_id, state in session.store.records_for(session.student_id).items()
    }

    batch1 = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)
    batch2 = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)

    sigs1 = {item.instance["variant_signature"] for item in batch1.items}
    sigs2 = {item.instance["variant_signature"] for item in batch2.items}
    assert len(sigs1) == constants.DEFAULT_BATCH_SIZE
    assert len(sigs2) == constants.DEFAULT_BATCH_SIZE
    assert sigs1.isdisjoint(sigs2), "second call's candidates must exclude the first call's delivered signatures"
    assert session.delivered == sigs1 | sigs2

    # Planning alone must never touch durable state.
    assert session.engine_state.seen_history == seen_before
    records_after = {
        skill_id: (state.theta, state.n, state.certified_depth)
        for skill_id, state in session.store.records_for(session.student_id).items()
    }
    assert records_after == records_before


# --- scenario 3: feedback sidecar covers exactly the reachable misconceptions


def test_feedback_sidecar_covers_every_distractor_misconception_and_matches_registry_text(tmp_path, content, bank):
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)

    referenced = {
        distractor["misconception_id"]
        for item in result.items
        for distractor in item.instance.get("distractors", [])
        if distractor.get("misconception_id")
    }
    # Sanity: this batch must actually include tagged distractors, or the
    # rest of this test would pass vacuously.
    assert referenced

    assert set(result.feedback_sidecar) == referenced
    for misconception_id in referenced:
        expected_text = content.misconceptions[misconception_id].child_feedback_i18n["en-IN"]
        assert result.feedback_sidecar[misconception_id] == expected_text
        assert result.feedback_sidecar[misconception_id].strip()


# --- scenario 4: batch_size=1 degrades to per-turn serving (AE4) -----------


def test_batch_size_one_returns_exactly_the_frontier_pick_for_this_skill(tmp_path, content, bank):
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=1)

    assert result.count_requested == 1
    assert result.count_returned == 1
    only = result.items[0]
    assert only.rung == "target"
    assert only.context == "practice"

    # The same nearest-difficulty computation ordinary frontier serving uses
    # for this skill (target_depth capped by the serving ceiling,
    # pick_instance's tie-break) -- fetch_batch is single-skill-scoped like
    # get_candidates, so this is compared against that computation directly
    # rather than engine.select_next's own cross-skill priority chain (which
    # would also have to reason about placement/prerequisite eligibility
    # for other skills entirely, out of scope for a batch request that
    # already names its skill_id).
    pool = bank.instances_by_skill[SKILL_ID]
    ceiling = engine.serving_ceiling(SKILL_ID, COUNTRY, CURRICULUM, GRADE, content)
    theta = session.store.peek(session.student_id, SKILL_ID).theta
    target = engine.target_depth(theta, ceiling)
    expected, _warning = engine.pick_instance(pool, target, [])
    assert expected is not None
    assert only.instance["item_instance_id"] == expected["item_instance_id"]


def test_batch_size_is_clamped_to_the_server_cap(tmp_path, content, bank):
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.MAX_BATCH_SIZE + 50)
    assert result.count_requested == constants.MAX_BATCH_SIZE


# --- scenario 5: small pool returns a shorter batch, never padded ----------


def test_small_pool_returns_shorter_batch_without_padding_or_duplicates(tmp_path, content, bank):
    full_pool = bank.instances_by_skill[SKILL_ID]
    small_pool = full_pool[:3]
    tiny_bank = engine.BankIndex(
        bank_version=bank.bank_version,
        instances_by_skill={SKILL_ID: small_pool},
        by_id={inst["item_instance_id"]: inst for inst in small_pool},
    )
    facade = _facade(content, tiny_bank)
    session = _session(tmp_path, tiny_bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)

    assert result.count_requested == constants.DEFAULT_BATCH_SIZE
    assert result.count_returned == len(result.items)
    assert result.count_returned <= len(small_pool)
    assert result.count_returned < result.count_requested

    signatures = [item.instance["variant_signature"] for item in result.items]
    assert len(signatures) == len(set(signatures))


# --- scenario 6: the double-diagnosis fix, end-to-end (the most important test)


def test_double_diagnosis_fix_end_to_end_via_fetch_batch_and_submit_attempt(tmp_path, content, bank):
    """The load-bearing test of this unit. Proves that answering a batch's
    target-rung item wrong+untagged queues a real StepDownProbe
    (consume_event's fresh-diagnosis fallthrough, which never checks
    context), and that submitting the batch's own pre-materialized
    easier-rung item for the same skill -- carrying its assigned
    context="probe" -- resolves (pops) that exact queue entry instead of
    queuing a second one on top of it. Exercises the real fetch_batch ->
    submit_attempt sequence, not consume_event in isolation.
    """
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)
    target_item = next(item for item in result.items if item.rung == "target")
    easier_item = next(item for item in result.items if item.rung == "easier")

    assert target_item.skill_id == SKILL_ID
    assert easier_item.skill_id == SKILL_ID
    assert target_item.context == "practice"
    assert easier_item.context == "probe"
    # A genuine step-down: the easier item really is shallower.
    assert easier_item.instance["difficulty"] < target_item.instance["difficulty"]

    assert session.engine_state.stepdown_queue == []

    # Wrong + untagged answer on the target-rung item: consume_event's
    # fresh-diagnosis fallthrough (no context check at all) must queue a
    # real StepDownProbe for this skill.
    outcome1 = facade.submit_attempt(
        session,
        attempt_id="attempt-target-wrong",
        skill_id=SKILL_ID,
        instance=target_item.instance,
        response_raw="not-a-real-option",
        response_parsed="not-a-real-option",
        correct=False,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=12.0,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context=target_item.context,
        consumer_id=CONSUMER_ID,
        now=NOW,
    )
    assert outcome1.event is not None, outcome1.rejection_reason
    assert len(session.engine_state.stepdown_queue) == 1
    assert session.engine_state.stepdown_queue[0].skill_id == SKILL_ID

    # Submit the batch's own pre-materialized easier-rung item for the same
    # skill, under its assigned context="probe" -- this must resolve (pop)
    # the queue head cleanly, not push a second StepDownProbe on top of it.
    outcome2 = facade.submit_attempt(
        session,
        attempt_id="attempt-easier-wrong",
        skill_id=SKILL_ID,
        instance=easier_item.instance,
        response_raw="also-not-a-real-option",
        response_parsed="also-not-a-real-option",
        correct=False,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=8.0,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context=easier_item.context,
        consumer_id=CONSUMER_ID,
        now=NOW,
    )
    assert outcome2.event is not None, outcome2.rejection_reason
    # Resolved, not duplicated: the wrong easier-rung answer pops the
    # existing StepDownProbe (and, since it was also wrong, queues
    # prerequisite probes instead -- the intended deeper escalation, not a
    # second step-down probe on the same skill).
    assert session.engine_state.stepdown_queue == []


def test_double_diagnosis_would_have_duplicated_under_the_old_practice_context(tmp_path, content, bank):
    """Negative control for scenario 6: proves the bug scenario 6 fixes is
    real, not hypothetical. Same two submissions, but the second one is
    resubmitted under context="practice" (what the easier item would have
    carried without this unit's context-assignment rule) instead of the
    batch's actually-assigned context="probe" -- consume_event's
    probe-resolution branch requires context=="probe" to match the queue
    head, so a "practice"-context attempt on the same skill never resolves
    it: the queue entry `submit_attempt` #1 created is still sitting there,
    unpopped, after `submit_attempt` #2 -- exactly the stuck/duplicating
    diagnosis state this unit's context="probe" assignment prevents. (In
    this fixture the easier item is already at FRAC.ADD.UNLIKE's lowest
    authored depth tier, so fresh diagnosis on it falls through to queuing
    prerequisite probes rather than a second StepDownProbe -- still visibly
    wrong: the original StepDownProbe was supposed to be resolved by an
    answer at its own depth and instead sits there forever while additional
    diagnosis piles up on top of it.)"""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)

    result = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)
    target_item = next(item for item in result.items if item.rung == "target")
    easier_item = next(item for item in result.items if item.rung == "easier")

    outcome1 = facade.submit_attempt(
        session,
        attempt_id="attempt-target-wrong-2",
        skill_id=SKILL_ID,
        instance=target_item.instance,
        response_raw="not-a-real-option",
        response_parsed="not-a-real-option",
        correct=False,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=12.0,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context="practice",
        consumer_id=CONSUMER_ID,
        now=NOW,
    )
    assert outcome1.event is not None, outcome1.rejection_reason
    assert len(session.engine_state.stepdown_queue) == 1
    original_probe = session.engine_state.stepdown_queue[0]
    assert len(session.engine_state.prereq_probe_queue) == 0

    outcome2 = facade.submit_attempt(
        session,
        attempt_id="attempt-easier-wrong-2",
        skill_id=SKILL_ID,
        instance=easier_item.instance,
        response_raw="also-not-a-real-option",
        response_parsed="also-not-a-real-option",
        correct=False,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=8.0,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context="practice",  # deliberately the old/wrong context, not easier_item.context
        consumer_id=CONSUMER_ID,
        now=NOW,
    )
    assert outcome2.event is not None, outcome2.rejection_reason
    # The bug this unit fixes: without context="probe" the answer never
    # matches the queue head, so the original StepDownProbe is left
    # unresolved (never popped) instead of being cleanly consumed --
    # exactly the "stuck diagnosis state" scenario 6's context="probe"
    # assignment avoids. This fixture's content additionally escalates to a
    # prerequisite probe on top (since the easier item is already at the
    # skill's lowest tier), which the correctly-resolved scenario 6 test
    # never sees.
    assert original_probe in session.engine_state.stepdown_queue
    assert len(session.engine_state.prereq_probe_queue) > 0


# --- compute_mix / clamp_batch_size (pure unit coverage) -------------------


@pytest.mark.parametrize(
    "requested,expected_sum",
    [(1, 1), (2, 2), (3, 3), (5, 5), (10, 10), (20, 20)],
)
def test_compute_mix_always_sums_to_batch_size(requested, expected_sum):
    target_n, easier_n, harder_n = batch.compute_mix(requested)
    assert target_n + easier_n + harder_n == expected_sum
    assert target_n >= 0 and easier_n >= 0 and harder_n >= 0


def test_compute_mix_default_matches_named_constants():
    assert batch.compute_mix(constants.DEFAULT_BATCH_SIZE) == (
        constants.BATCH_MIX_TARGET,
        constants.BATCH_MIX_EASIER,
        constants.BATCH_MIX_HARDER,
    )


def test_compute_mix_batch_size_one_is_target_only():
    assert batch.compute_mix(1) == (1, 0, 0)


def test_clamp_batch_size_bounds():
    assert batch.clamp_batch_size(0) == 1
    assert batch.clamp_batch_size(-5) == 1
    assert batch.clamp_batch_size(1) == 1
    assert batch.clamp_batch_size(constants.MAX_BATCH_SIZE) == constants.MAX_BATCH_SIZE
    assert batch.clamp_batch_size(constants.MAX_BATCH_SIZE + 1) == constants.MAX_BATCH_SIZE
