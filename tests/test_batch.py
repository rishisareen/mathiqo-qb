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

import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from conftest import _bypass_placement
from orchestrator import batch, constants, engine, log as log_module, serving, signals
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


# ---------------------------------------------------------------------------
# U10: refill semantics (R12, KTD5) and the no_further_items signal (R13)
# ---------------------------------------------------------------------------

# --- test scenario 1: refill reflects updated (lowered) live theta (AE2) ---


def test_refill_after_wrong_answers_produces_lower_difficulty_target_rung(tmp_path, content, bank):
    """Covers AE2's refill-reflects-updated-state half: wrong, untagged
    answers submitted against this skill's target-rung items lower theta
    (log.py's update rule, applied live via submit_attempt); a subsequent
    refill_batch for the same skill re-plans against that lowered live
    theta and its target rung lands on FRAC.ADD.UNLIKE's lower depth tier
    (0.2) instead of the original batch's 0.5. This falls out of
    fetch_batch/refill_batch sharing the same planning core
    (_plan_batch_locked), which always reads session.store's live theta —
    not a refill-only special case."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank, theta=THETA_AT_CEILING)

    original = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)
    original_target_depths = {item.instance["difficulty"] for item in original.items if item.rung == "target"}
    assert original_target_depths == {0.5}, "sanity: at theta=THETA_AT_CEILING the target rung sits at the ceiling"

    target_items = [item for item in original.items if item.rung == "target"]
    assert len(target_items) >= 5, "need enough target items to drag theta below the tier midpoint"
    for i, item in enumerate(target_items[:5]):
        outcome = facade.submit_attempt(
            session,
            attempt_id=f"attempt-wrong-{i}",
            skill_id=SKILL_ID,
            instance=item.instance,
            response_raw="not-a-real-option",
            response_parsed="not-a-real-option",
            correct=False,
            misconception_id=None,
            hints_used=0,
            hint_auto_offered=False,
            time_taken_sec=10.0,
            timed_out=False,
            engaged=True,
            session_last_event=False,
            context=item.context,
            consumer_id=CONSUMER_ID,
            now=NOW,
        )
        assert outcome.event is not None, outcome.rejection_reason

    theta_after = session.store.peek(session.student_id, SKILL_ID).theta
    assert theta_after < THETA_AT_CEILING, "five wrong answers must have lowered theta"

    refill = facade.refill_batch(
        session, SKILL_ID, CONSUMER_ID, attempts_flushed=True, batch_size=constants.DEFAULT_BATCH_SIZE
    )
    assert refill.signal is None
    assert refill.batch is not None
    refill_target_depths = {item.instance["difficulty"] for item in refill.batch.items if item.rung == "target"}
    assert refill_target_depths, "expected at least one target-rung item in the refill"
    assert max(refill_target_depths) < max(original_target_depths), (
        "the refill's target rung must sit at a lower difficulty than the original batch's, "
        "reflecting theta's drop from the wrong answers"
    )


# --- test scenario 2: supersede (KTD5) --------------------------------------


def test_refill_supersede_true_when_skill_now_certified_at_or_above_ceiling(tmp_path, content, bank):
    """KTD5: a refill for a skill whose *real, live* store already shows
    certified_depth >= its serving ceiling carries supersede=True — checked
    against session.store directly, never the scratch clone planning
    uses."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank, theta=THETA_AT_CEILING)
    ceiling = engine.serving_ceiling(SKILL_ID, COUNTRY, CURRICULUM, GRADE, content)
    session.store.get(session.student_id, SKILL_ID).certified_depth = ceiling

    refill = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill.supersede is True


def test_refill_supersede_true_when_remediation_dose_freshly_queued_for_skill(tmp_path, content, bank):
    """KTD5's second supersede condition: a RemediationDose now queued for
    this exact skill_id in engine_state.remediation_queue, independent of
    certification."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank, theta=THETA_AT_CEILING)
    session.engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id=SKILL_ID, trigger_skill_id=SKILL_ID, misconception_id="MISC.WHATEVER")
    )

    refill = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill.supersede is True


def test_refill_supersede_false_for_an_ordinary_refill_with_no_plan_change(tmp_path, content, bank):
    """An ordinary refill -- no certification crossing, no remediation dose
    queued for this skill -- carries supersede=False."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank, theta=THETA_AT_CEILING)

    refill = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill.supersede is False


def test_refill_supersede_false_when_remediation_dose_is_queued_for_a_different_skill(tmp_path, content, bank):
    """The remediation-dose half of supersede is scoped to this exact
    skill_id -- a dose queued for some other skill must not flip
    supersede for this refill."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank, theta=THETA_AT_CEILING)
    session.engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.ADD.LIKE", trigger_skill_id="FRAC.ADD.LIKE", misconception_id="MISC.X")
    )

    refill = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill.supersede is False


# --- test scenario 4: no_further_items, and a clean repeat (R13) -----------


def test_refill_with_nothing_left_returns_no_further_items_and_repeats_cleanly(tmp_path, content, bank):
    """R13: once a batch has delivered an entire (tiny) pool, a refill for
    the same skill returns the explicit no_further_items signal rather
    than an empty BatchResult -- and an immediate second refill returns the
    same signal again, cleanly (no loop, no different error)."""
    full_pool = bank.instances_by_skill[SKILL_ID]
    small_pool = full_pool[:4]  # bank.instances_by_skill is sorted ascending by difficulty (load_bank) --
    # this slice is entirely the skill's lowest depth tier.
    tiny_bank = engine.BankIndex(
        bank_version=bank.bank_version,
        instances_by_skill={SKILL_ID: small_pool},
        by_id={inst["item_instance_id"]: inst for inst in small_pool},
    )
    facade = _facade(content, tiny_bank)
    session = _session(tmp_path, tiny_bank)

    first = facade.fetch_batch(session, SKILL_ID, CONSUMER_ID, batch_size=constants.DEFAULT_BATCH_SIZE)
    assert first.count_returned == len(small_pool), "the whole tiny pool should fit in one batch"
    assert session.delivered == {item.instance["variant_signature"] for item in first.items}

    refill1 = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill1.batch is None
    assert refill1.signal == signals.NO_FURTHER_ITEMS

    refill2 = facade.refill_batch(session, SKILL_ID, CONSUMER_ID, attempts_flushed=True)
    assert refill2.batch is None
    assert refill2.signal == signals.NO_FURTHER_ITEMS


# --- test scenario 5: concurrent refill_batch + submit_attempt (KTD17) -----


def test_concurrent_refill_and_submit_attempt_are_serialized_by_the_session_lock(tmp_path, content, bank):
    """KTD17: refill_batch and submit_attempt dispatched concurrently
    against the same *registered* session (so `_lock_for` resolves a real
    `threading.Lock`, not the no-op used for an ephemeral, never-registered
    Session) must serialize -- the observable result must be exactly as if
    the two calls ran one after another: no torn engine_state, no
    interleaved/lost log line, no exception from either side."""
    facade = _facade(content, bank)
    session = _session(tmp_path, bank)
    facade.sessions.register_or_conflict(session, CONSUMER_ID)

    # The highest-difficulty instance in the pool: fetch_batch/refill_batch
    # target/harder-rung picks at THETA_AT_CEILING land on the 0.5 tier too,
    # but this instance is submitted directly, independent of whatever the
    # concurrent refill happens to plan, so the two calls' effects stay
    # cleanly separable in the assertions below.
    submit_instance = bank.instances_by_skill[SKILL_ID][-1]

    barrier = threading.Barrier(2)
    errors = []
    refill_results = []

    def _refill() -> None:
        barrier.wait()
        try:
            refill_results.append(
                facade.refill_batch(
                    session, SKILL_ID, CONSUMER_ID, attempts_flushed=True, batch_size=constants.DEFAULT_BATCH_SIZE
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below, not swallowed
            errors.append(exc)

    def _submit() -> None:
        barrier.wait()
        try:
            facade.submit_attempt(
                session,
                attempt_id="attempt-concurrent",
                skill_id=SKILL_ID,
                instance=submit_instance,
                response_raw=submit_instance["answer"],
                response_parsed=submit_instance["answer"],
                correct=True,
                misconception_id=None,
                hints_used=0,
                hint_auto_offered=False,
                time_taken_sec=5.0,
                timed_out=False,
                engaged=True,
                session_last_event=False,
                context="practice",
                consumer_id=CONSUMER_ID,
                now=NOW,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_refill), threading.Thread(target=_submit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent refill_batch/submit_attempt raised: {errors}"
    assert len(refill_results) == 1

    # No torn engine_state: the submitted instance's variant_signature is
    # recorded seen exactly once (not zero, not duplicated by an
    # interleaved read-modify-write).
    seen = session.engine_state.seen_history.get(SKILL_ID, [])
    assert seen.count(submit_instance["variant_signature"]) == 1

    # No interleaved/lost log line: exactly the one submitted attempt.
    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 1
    assert logged[0]["item_instance_id"] == submit_instance["item_instance_id"]

    # The refill's own delivered signatures (if it found any) landed
    # cleanly in session.delivered -- no lost write from running under the
    # same lock as the concurrent submit_attempt.
    refill_result = refill_results[0]
    if refill_result.batch is not None:
        delivered_sigs = {item.instance["variant_signature"] for item in refill_result.batch.items}
        assert delivered_sigs <= session.delivered
