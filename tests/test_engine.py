"""Tests for orchestrator/engine.py — the U5 adaptive engine: content/bank
loading, depth targeting (KTD9), the seen filter (core doc §5.4), the R12
priority chain (`select_next`), diagnosis-queue consumption (R13, KTD12,
`consume_event`), placement (SM §5), and engine-state replay (KTD11,
`rebuild_engine_state`).

Fixtures build a *real* bank from content/templates via tools/bank_build.py
(session-scoped, built once) and load the real content/fractions/*.yaml
graph — this unit is explicitly the first to exercise that real shape, so
tests run against it rather than a synthetic stand-in wherever practical.
Where a test needs to isolate one decision (e.g. prerequisite gating, or
the placement walk's pass/fail transition) from the rest of the priority
chain or from theta/certification arithmetic, it constructs a narrow
synthetic bank or writes SkillState directly via `store.replace_records` —
each such isolation is called out in the test's own docstring.

`run_attempt` below is the test harness's "feedback interface" reference
implementation: apply the ordinary student-model update
(update.apply_attempt [+ certification.maybe_certify, or
certification.apply_review_outcome for a review-context attempt], exactly
as orchestrator/log.py composes them) and then call
`engine.consume_event` — the two-step protocol engine.py's module
docstring documents for any real caller (a future U6 session runner).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import _bypass_placement, find_tagged_instance, load_yaml, write_yaml
from orchestrator import constants, engine
from orchestrator.certification import apply_review_outcome, maybe_certify
from orchestrator.log import append_attempt
from orchestrator.state import SkillState, StudentModelStore, is_certified
from orchestrator.update import Attempt, apply_attempt

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content" / "fractions"
TEMPLATES_DIR = REPO_ROOT / "content" / "templates"

NOW = datetime(2026, 7, 27, 12, 0, 0)

# `content` and `bank` (session-scoped fixtures) and `find_tagged_instance` /
# `_bypass_placement` live in tests/conftest.py, shared with test_session.py
# and test_simulation.py — see that module's docstring.


# --- test harness: the documented feedback-interface calling convention -------


def run_attempt(
    store: StudentModelStore,
    engine_state: engine.EngineState,
    content_index: engine.ContentIndex,
    bank_index: engine.BankIndex,
    student_id: str,
    serve: "engine.Serve",
    correct: bool,
    response_value: str | None = None,
    hints_used: int = 0,
    now: datetime = NOW,
) -> tuple[engine.EngineState, str | None]:
    """Score one served item exactly as a real caller must (see
    orchestrator/engine.py's module docstring): apply the student-model
    update first, write it into `store`, then call `consume_event` and
    apply any patches it returns. Returns (new_engine_state, matched
    misconception_id) for assertions."""
    instance = bank_index.by_id[serve.item_instance_id]
    misconception_id = None if correct else engine.match_distractor(instance, response_value)
    attempt = Attempt(
        skill_ids=[serve.skill_id],
        question_type=instance["question_type"],
        b_prior_at_serve=instance["difficulty"],
        correct=correct,
        hints_used=hints_used,
        timed_out=False,
        engaged=True,
        session_last_event=False,
        context=serve.context,
        misconception_id=misconception_id,
        event_id=f"e-{uuid.uuid4().hex[:8]}",
        depth_served=instance["difficulty"],
    )
    state = store.get(student_id, serve.skill_id)
    if serve.context == "review":
        if correct:
            new_state = apply_review_outcome(state, True, now)
        else:
            result = apply_attempt(state, attempt)
            new_state = apply_review_outcome(result.new_state, False, now)
    else:
        result = apply_attempt(state, attempt)
        new_state = maybe_certify(result.new_state, attempt, now)
    records = dict(store.records_for(student_id))
    records[serve.skill_id] = new_state
    store.replace_records(student_id, records)

    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=[serve.skill_id],
        item_instance_id=serve.item_instance_id,
        variant_signature=instance["variant_signature"],
        correct=correct,
        misconception_id=misconception_id,
        context=serve.context,
        depth_served=instance["difficulty"],
    )
    consume_result = engine.consume_event(engine_state, store, content_index, bank_index, outcome, now)
    engine.apply_consume_patches(store, student_id, consume_result.skill_state_patches)
    return consume_result.engine_state, misconception_id


def make_serve(bank_index: engine.BankIndex, skill_id: str, difficulty: float, context: str, reason: str) -> "engine.Serve":
    for instance in bank_index.instances_by_skill[skill_id]:
        if instance["difficulty"] == pytest.approx(difficulty):
            return engine.Serve(instance["item_instance_id"], skill_id, instance["difficulty"], context, reason)
    raise AssertionError(f"no {skill_id} instance at difficulty {difficulty}")


# --- load_content / load_bank ---------------------------------------------------


def test_load_content_surfaces_content_version_from_manifest(content: engine.ContentIndex) -> None:
    """KTD9: content/fractions/manifest.yaml declares content_version
    "1.2.0" (bumped by the Class 6 Servable Coverage plan's U3 misconception
    registry entries) — load_content must read it and surface it on
    ContentIndex."""
    assert content.content_version == "1.2.0"


def test_load_content_version_survives_merged_multi_file_load(fixture_copy: Path) -> None:
    """content/fractions/ is ~13 YAML files merged into one ContentIndex
    (skills/misconceptions/mappings each split across several files); this
    confirms content_version — read once from manifest.yaml, not per content
    file — isn't lost or clobbered by that per-file merge, and specifically
    isn't affected by ordinary content edits to the other files."""
    from orchestrator import engine as engine_module

    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    data["mappings"][0]["depth_expectation"] = "hard"  # unrelated content edit
    write_yaml(mappings_path, data)

    content_index = engine_module.load_content(fixture_copy)
    assert content_index.content_version == "1.2.0"


def test_load_content_raises_a_clear_error_when_manifest_is_missing(fixture_copy: Path) -> None:
    """engine.load_content assumes graph_lint-clean content (see its own
    docstring) but must still fail loudly, not silently produce an
    un-versioned ContentIndex, if a manifest is absent."""
    from orchestrator import engine as engine_module

    (fixture_copy / "manifest.yaml").unlink()

    with pytest.raises(FileNotFoundError, match="manifest.yaml"):
        engine_module.load_content(fixture_copy)


def test_load_content_parses_real_skills_prerequisites_and_dependents(content: engine.ContentIndex) -> None:
    node = content.skills["FRAC.ADD.UNLIKE"]
    assert node.strand == "NUMBER.FRACTIONS"
    assert set(node.prerequisites) == {"FRAC.EQUIV", "FRAC.ADD.LIKE", "NUM.LCM", "FRAC.SIMPLIFY"}
    assert content.dependents["FRAC.UNIT"] == ["FRAC.PARTWHOLE"]
    assert content.dependents["FRAC.EQUIV"] == ["FRAC.ADD.UNLIKE", "FRAC.CMP", "FRAC.SIMPLIFY"]


def test_load_content_parses_misconception_remediation_targets(content: engine.ContentIndex) -> None:
    entry = content.misconceptions["MISC.FRAC.ADD-ACROSS"]
    assert entry.remediation_target == "FRAC.EQUIV"


def test_load_content_indexes_mappings_by_skill_country_curriculum_grade(content: engine.ContentIndex) -> None:
    row = content.mappings[("FRAC.ADD.UNLIKE", "IN", "NCERT", 6)]
    assert row.depth_expectation == "medium"
    assert content.mappings.get(("NUM.MULT.TABLES", "CA", "ONTARIO", 4)) is None  # never mapped anywhere


def test_load_content_groups_grade_mapped_skills(content: engine.ContentIndex) -> None:
    assert content.mapped_skills_by_country_curriculum_grade[("CA", "ONTARIO", 4)] == sorted(
        ["FRAC.UNIT", "FRAC.PARTWHOLE", "FRAC.CMP"]
    )


def test_load_content_mapping_row_surfaces_learning_objective_and_terminology_overrides(
    content: engine.ContentIndex,
) -> None:
    """KTD10/U1: `learning_objective` and `terminology_overrides` are read
    from YAML by `_load_yaml_list` but were previously dropped when
    `MappingRow` was constructed — this is the migrated FRAC.ADD.UNLIKE/IN/
    NCERT/6 row (content/fractions/mappings.yaml), which carries both."""
    row = content.mappings[("FRAC.ADD.UNLIKE", "IN", "NCERT", 6)]
    assert row.learning_objective is not None and "unlike denominators" in row.learning_objective
    assert row.terminology_overrides == {"simplest form": "lowest form"}


def test_load_content_mapping_row_surfaces_structured_unit_fields(content: engine.ContentIndex) -> None:
    """KTD10: a migrated NCERT row carries a structured unit_code/unit_name
    derived from its syllabus_ref's chapter reference. unit_name is the
    real Ganita Prakash 6 chapter title, verified against the published
    table of contents (Class 6 Servable Coverage plan, U1)."""
    row = content.mappings[("FRAC.ADD.UNLIKE", "IN", "NCERT", 6)]
    assert row.unit_code == "IN-NCERT-G6-CH7"
    assert row.unit_name == "Fractions"


def test_load_content_mapping_row_unit_fields_absent_for_unitless_curriculum(
    content: engine.ContentIndex,
) -> None:
    """Ontario has no chapter granularity (KTD10) — its rows load with both
    unit fields absent (None), not defaulted to empty strings or omitted
    from the dataclass."""
    row = content.mappings[("FRAC.UNIT", "CA", "ONTARIO", 4)]
    assert row.unit_code is None
    assert row.unit_name is None


def test_load_content_two_curricula_in_same_country_grade_resolve_independently(
    fixture_copy: Path,
) -> None:
    """KTD10/U1 re-keying: mappings/mapped_skills indexes key on (country,
    curriculum, grade), not (country, grade) — two curricula sharing one
    country+grade must not collide. The shipped fixture has no such overlap
    (IN only carries NCERT, CA only ONTARIO), so this test adds a second
    curriculum row for IN grade 6 (a synthetic CBSE row on top of the real
    NCERT one) directly to a fixture copy and confirms both resolve without
    clobbering each other."""
    from orchestrator import engine as engine_module

    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    data["mappings"].append(
        {
            "skill_id": "FRAC.ADD.UNLIKE",
            "country": "IN",
            "curriculum": "CBSE",
            "grade": 6,
            "depth_expectation": "hard",
            "syllabus_ref": "Class 6, Maths, Ch. 9 (CBSE fixture)",
            "learning_objective": "Adds and subtracts fractions with unlike denominators (CBSE phrasing).",
        }
    )
    write_yaml(mappings_path, data)

    content_index = engine_module.load_content(fixture_copy)
    ncert_row = content_index.mappings[("FRAC.ADD.UNLIKE", "IN", "NCERT", 6)]
    cbse_row = content_index.mappings[("FRAC.ADD.UNLIKE", "IN", "CBSE", 6)]
    assert ncert_row.depth_expectation == "medium"
    assert cbse_row.depth_expectation == "hard"
    # IN/NCERT/6 has many other real fixture rows besides FRAC.ADD.UNLIKE
    # (membership, not exact-list equality); IN/CBSE/6 is the synthetic
    # curriculum this test just added, so it has exactly the one row.
    assert "FRAC.ADD.UNLIKE" in content_index.mapped_skills_by_country_curriculum_grade[("IN", "NCERT", 6)]
    assert content_index.mapped_skills_by_country_curriculum_grade[("IN", "CBSE", 6)] == ["FRAC.ADD.UNLIKE"]


def test_load_bank_indexes_single_skill_instances_only(bank: engine.BankIndex) -> None:
    assert bank.bank_version  # non-empty; exact value is conftest.py's shared fixture's concern
    for pool in bank.instances_by_skill.values():
        for instance in pool:
            assert len(instance["skill_ids"]) == 1


def test_load_bank_by_id_lookup_matches_instances_by_skill(bank: engine.BankIndex) -> None:
    some_instance = bank.instances_by_skill["FRAC.UNIT"][0]
    assert bank.by_id[some_instance["item_instance_id"]] == some_instance


# --- depth targeting: tier conversion, placement prior, ceiling (KTD9) --------


def test_placement_prior_steps_one_tier_below() -> None:
    assert engine.placement_prior_depth("medium") == constants.TIER_DEPTH["easy"]
    assert engine.placement_prior_depth("hard") == constants.TIER_DEPTH["medium"]


def test_placement_prior_floors_at_easy_when_already_easy() -> None:
    assert engine.placement_prior_depth("easy") == constants.TIER_DEPTH["easy"]


def test_placement_prior_defaults_to_easy_when_unmapped() -> None:
    assert engine.placement_prior_depth(None) == constants.TIER_DEPTH["easy"]


def test_starting_depth_uses_default_for_a_skill_with_no_mapping_row(content: engine.ContentIndex) -> None:
    """NUM.MULT.TABLES has zero mapping rows anywhere in the fixture."""
    assert engine.starting_depth_for("NUM.MULT.TABLES", "CA", "ONTARIO", 4, content) == constants.TIER_DEPTH["easy"]


def test_serving_ceiling_reads_the_mapped_tier(content: engine.ContentIndex) -> None:
    assert engine.serving_ceiling("FRAC.ADD.UNLIKE", "IN", "NCERT", 6, content) == constants.TIER_DEPTH["medium"]


def test_serving_ceiling_defaults_to_top_tier_without_a_mapping_row(content: engine.ContentIndex) -> None:
    assert engine.serving_ceiling("NUM.MULT.TABLES", "CA", "ONTARIO", 4, content) == constants.TIER_DEPTH["hard"]


def test_target_depth_adds_challenge_offset_capped_by_ceiling() -> None:
    assert engine.target_depth(theta=0.1, ceiling=0.8) == pytest.approx(0.1 + constants.CHALLENGE_OFFSET)
    assert engine.target_depth(theta=0.79, ceiling=0.8) == pytest.approx(0.8)  # capped


# --- match_distractor: shared wrong-answer matching (R13) ---------------------


def test_match_distractor_returns_tagged_misconception(bank: engine.BankIndex) -> None:
    instance, value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    assert engine.match_distractor(instance, value) == "MISC.FRAC.ADD-ACROSS"


def test_match_distractor_none_when_response_matches_no_distractor(bank: engine.BankIndex) -> None:
    instance = bank.instances_by_skill["FRAC.ADD.UNLIKE"][0]
    assert engine.match_distractor(instance, "this-matches-nothing") is None


def test_match_distractor_none_for_a_matched_but_untagged_distractor(bank: engine.BankIndex) -> None:
    """A response can match a stored distractor value whose own
    misconception_id is null — still 'untagged' for diagnosis (§2.5)."""
    untagged_instance = None
    untagged_value = None
    for instance in bank.instances_by_skill["FRAC.ADD.LIKE"]:
        for d in instance["distractors"]:
            if d["misconception_id"] is None:
                untagged_instance, untagged_value = instance, d["value"]
                break
        if untagged_instance:
            break
    assert untagged_instance is not None, "expected an untagged distractor in the built bank"
    assert engine.match_distractor(untagged_instance, untagged_value) is None


# --- pick_instance: nearest-difficulty + seen filter + LRU fallback (§5.4) ----


def test_pick_instance_nearest_difficulty_ties_broken_by_id() -> None:
    candidates = [
        {"item_instance_id": "b", "variant_signature": "sig-b", "difficulty": 0.2},
        {"item_instance_id": "a", "variant_signature": "sig-a", "difficulty": 0.2},
        {"item_instance_id": "c", "variant_signature": "sig-c", "difficulty": 0.5},
    ]
    instance, warning = engine.pick_instance(candidates, target=0.2, seen_history=[])
    assert instance["item_instance_id"] == "a"
    assert warning is None


def test_pick_instance_never_repeats_while_unseen_alternatives_exist() -> None:
    candidates = [
        {"item_instance_id": "a", "variant_signature": "sig-a", "difficulty": 0.2},
        {"item_instance_id": "b", "variant_signature": "sig-b", "difficulty": 0.2},
    ]
    instance, warning = engine.pick_instance(candidates, target=0.2, seen_history=["sig-a"])
    assert instance["item_instance_id"] == "b"
    assert warning is None


def test_pick_instance_never_repeats_a_signature_even_under_a_new_instance_id() -> None:
    """Covers AE3 (serving-side half): a signature already in seen_history
    is treated as seen even when the candidate's item_instance_id differs
    from whatever id it was originally served under (a rebuilt-bank
    re-sample of the same variant)."""
    candidates = [
        {"item_instance_id": "a-rebuilt", "variant_signature": "sig-a", "difficulty": 0.2},
        {"item_instance_id": "b", "variant_signature": "sig-b", "difficulty": 0.2},
    ]
    instance, warning = engine.pick_instance(candidates, target=0.2, seen_history=["sig-a"])
    assert instance["item_instance_id"] == "b"
    assert warning is None


def test_pick_instance_exhausted_pool_repeats_least_recently_seen_with_warning() -> None:
    candidates = [
        {"item_instance_id": "a", "variant_signature": "sig-a", "difficulty": 0.2},
        {"item_instance_id": "b", "variant_signature": "sig-b", "difficulty": 0.2},
        {"item_instance_id": "c", "variant_signature": "sig-c", "difficulty": 0.2},
    ]
    # sig-a was served longest ago (index 0); sig-c most recently (index 2).
    instance, warning = engine.pick_instance(candidates, target=0.2, seen_history=["sig-a", "sig-b", "sig-c"])
    assert instance["item_instance_id"] == "a"
    assert warning is not None


def test_pick_instance_empty_candidates_returns_none() -> None:
    assert engine.pick_instance([], target=0.2, seen_history=[]) == (None, None)


def test_pick_instance_two_distinct_signatures_in_pool_both_servable() -> None:
    """Two instances with different signatures in the same pool are both
    servable — no false-positive collision from signature-keyed identity:
    marking one seen must not make the other look seen too."""
    candidates = [
        {"item_instance_id": "x1", "variant_signature": "sig-x", "difficulty": 0.3},
        {"item_instance_id": "y1", "variant_signature": "sig-y", "difficulty": 0.3},
    ]
    first, _ = engine.pick_instance(candidates, target=0.3, seen_history=[])
    second, _ = engine.pick_instance(candidates, target=0.3, seen_history=[first["variant_signature"]])
    assert {first["variant_signature"], second["variant_signature"]} == {"sig-x", "sig-y"}
    assert first["item_instance_id"] != second["item_instance_id"]


# --- consume_event: KTD12 remediation dose ------------------------------------


def test_two_tagged_hits_in_window_queue_a_remediation_dose_and_reset_counter(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-remediation"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    tagged_instance, value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    serve = engine.Serve(
        tagged_instance["item_instance_id"], "FRAC.ADD.UNLIKE", tagged_instance["difficulty"], "practice", "test"
    )

    engine_state, misc1 = run_attempt(store, engine_state, content, bank, student_id, serve, False, value)
    assert misc1 == "MISC.FRAC.ADD-ACROSS"
    assert engine_state.remediation_queue == []  # one hit is not a pattern

    engine_state, misc2 = run_attempt(store, engine_state, content, bank, student_id, serve, False, value)
    assert misc2 == "MISC.FRAC.ADD-ACROSS"
    assert len(engine_state.remediation_queue) == 1
    dose = engine_state.remediation_queue[0]
    assert dose.skill_id == "FRAC.EQUIV"  # the registry's mapped prerequisite
    assert dose.trigger_skill_id == "FRAC.ADD.UNLIKE"
    assert dose.remaining == constants.REMEDIATION_DOSE_SIZE

    # KTD12: the trigger is consumed, not standing.
    assert store.get(student_id, "FRAC.ADD.UNLIKE").misc_count("MISC.FRAC.ADD-ACROSS") == 0


def test_remediation_dose_completes_then_a_fresh_pair_is_required_to_retrigger(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-dose-lifecycle"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    tagged_instance, value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    trigger_serve = engine.Serve(
        tagged_instance["item_instance_id"], "FRAC.ADD.UNLIKE", tagged_instance["difficulty"], "practice", "test"
    )

    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, trigger_serve, False, value)
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, trigger_serve, False, value)
    assert len(engine_state.remediation_queue) == 1

    # select_next must now serve the dose ahead of everything else.
    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    engine_state = result.engine_state
    assert result.serve.reason == "remediation"
    assert result.serve.skill_id == "FRAC.EQUIV"

    # Run the dose to completion (REMEDIATION_DOSE_SIZE attempts on FRAC.EQUIV).
    for _ in range(constants.REMEDIATION_DOSE_SIZE):
        result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
        assert result.serve.reason == "remediation"
        engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, result.serve, True)

    assert engine_state.remediation_queue == []

    # One fresh tagged wrong: not yet a pattern (old evidence was reset).
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, trigger_serve, False, value)
    assert engine_state.remediation_queue == []

    # A second fresh tagged wrong: retriggers.
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, trigger_serve, False, value)
    assert len(engine_state.remediation_queue) == 1


# --- consume_event: R13 depth-step-down and prerequisite fallback -------------


def test_untagged_deep_wrong_queues_a_stepdown_probe_at_the_easier_depth(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-stepdown"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    deep_serve = make_serve(bank, "FRAC.ADD.UNLIKE", 0.5, "practice", "test")

    engine_state, misc = run_attempt(store, engine_state, content, bank, student_id, deep_serve, False, "no-match")
    assert misc is None
    assert len(engine_state.stepdown_queue) == 1
    probe = engine_state.stepdown_queue[0]
    assert probe.skill_id == "FRAC.ADD.UNLIKE"
    assert probe.target_depth == pytest.approx(0.2)

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "stepdown"
    assert result.serve.skill_id == "FRAC.ADD.UNLIKE"
    assert result.serve.context == "probe"
    assert result.serve.difficulty == pytest.approx(0.2)


def test_stepdown_probe_pass_continues_practice_no_prereq_walk(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-stepdown-pass"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    deep_serve = make_serve(bank, "FRAC.ADD.UNLIKE", 0.5, "practice", "test")
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, deep_serve, False, "no-match")

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, result.serve, True)

    assert engine_state.stepdown_queue == []
    assert engine_state.prereq_probe_queue == []


def test_stepdown_probe_fail_falls_back_to_prerequisite_walk(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-stepdown-fail"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    deep_serve = make_serve(bank, "FRAC.ADD.UNLIKE", 0.5, "practice", "test")
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, deep_serve, False, "no-match")

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, result.serve, False, "no-match")

    assert engine_state.stepdown_queue == []
    queued = {p.skill_id for p in engine_state.prereq_probe_queue}
    assert queued == {"FRAC.EQUIV", "FRAC.ADD.LIKE", "NUM.LCM", "FRAC.SIMPLIFY"}


def test_no_easier_instance_queues_prerequisite_probes_directly(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """FRAC.EQUIV's whole v1 pool sits at 0.2 — no depth below it exists."""
    student_id = "stu-no-easier"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    only_serve = make_serve(bank, "FRAC.EQUIV", 0.2, "practice", "test")

    engine_state, misc = run_attempt(store, engine_state, content, bank, student_id, only_serve, False, "no-match")
    assert misc is None
    assert engine_state.stepdown_queue == []
    queued = {p.skill_id for p in engine_state.prereq_probe_queue}
    assert queued == {"FRAC.PARTWHOLE", "NUM.MULT.TABLES"}


def test_prereq_probe_is_consumed_once_served(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-prereq-probe"
    store = StudentModelStore()
    engine_state = engine.EngineState()
    only_serve = make_serve(bank, "FRAC.EQUIV", 0.2, "practice", "test")
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, only_serve, False, "no-match")
    assert len(engine_state.prereq_probe_queue) == 2

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "prereq_probe"
    engine_state, _ = run_attempt(store, engine_state, content, bank, student_id, result.serve, True)
    assert len(engine_state.prereq_probe_queue) == 1


# --- consume_event: review context never feeds diagnosis (code review fix) ----


def test_review_context_never_leaks_into_diagnosis_queues(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    """A review probe's pass/fail is already fully handled by
    certification.py's apply_review_outcome (see log.py's compose_attempt);
    consume_event must not additionally treat a failed one as a fresh
    diagnostic failure. Broader than the narrow "interleaves with an
    active queue" edge case originally reported by code review: before the
    fix, EVERY failed review context — empty queues or not, tagged or
    untagged — could fall through to fresh diagnosis and spawn a spurious
    stepdown probe or remediation dose on top of the correct
    certification-side lapse."""
    store = StudentModelStore()
    outcome = engine.AttemptOutcome(
        student_id="stu-review-no-leak",
        skill_ids=["FRAC.ADD.UNLIKE"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=False,
        misconception_id=None,
        context="review",
        depth_served=0.5,
    )
    result = engine.consume_event(engine.EngineState(), store, content, bank, outcome, NOW)
    assert result.engine_state.stepdown_queue == []
    assert result.engine_state.prereq_probe_queue == []
    assert result.engine_state.remediation_queue == []
    assert result.skill_state_patches == {}


def test_review_context_does_not_disturb_an_unrelated_active_stepdown_probe(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """The narrower case code review originally reported: a failed review
    on one skill must not pop or otherwise touch an already-queued
    diagnostic probe for a different skill."""
    store = StudentModelStore()
    engine_state = engine.EngineState(
        stepdown_queue=[engine.StepDownProbe(skill_id="FRAC.EQUIV", target_depth=0.2)]
    )
    outcome = engine.AttemptOutcome(
        student_id="stu1",
        skill_ids=["FRAC.ADD.UNLIKE"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=False,
        misconception_id=None,
        context="review",
        depth_served=0.5,
    )
    result = engine.consume_event(engine_state, store, content, bank, outcome, NOW)
    assert len(result.engine_state.stepdown_queue) == 1
    assert result.engine_state.stepdown_queue[0].skill_id == "FRAC.EQUIV"


# --- consume_event: multi-skill items never queue diagnosis (documented v1 gap) -


def test_consume_event_multi_skill_item_returns_immediately_no_diagnosis(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Dedicated coverage for the early-return branch itself (see
    consume_event's own comment on the documented, deliberate v1 gap):
    seen_history is still marked for every skill in the item, but no
    diagnosis queue is ever touched for a multi-skill attempt."""
    store = StudentModelStore()
    outcome = engine.AttemptOutcome(
        student_id="stu1",
        skill_ids=["FRAC.ADD.UNLIKE", "FRAC.EQUIV"],
        item_instance_id="inst-multi",
        variant_signature="sig-multi",
        correct=False,
        misconception_id=None,
        context="practice",
        depth_served=0.5,
    )
    result = engine.consume_event(engine.EngineState(), store, content, bank, outcome, NOW)
    assert result.engine_state.seen_history["FRAC.ADD.UNLIKE"] == ["sig-multi"]
    assert result.engine_state.seen_history["FRAC.EQUIV"] == ["sig-multi"]
    assert result.engine_state.stepdown_queue == []
    assert result.engine_state.prereq_probe_queue == []
    assert result.engine_state.remediation_queue == []
    assert result.skill_state_patches == {}


# --- consume_event: SM §5 placement walk (isolated from theta/certification) --


def test_placement_pass_below_frontier_n_moves_to_a_dependent(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-walk-pass"
    store = StudentModelStore()
    store.replace_records(student_id, {"FRAC.UNIT": SkillState(n=1.0, certified_depth=None)})
    engine_state = engine.EngineState(placement_cursor={"NUMBER.FRACTIONS": "FRAC.UNIT"}, placement_initialized=True)
    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=["FRAC.UNIT"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=True,
        misconception_id=None,
        context="placement",
        depth_served=0.2,
    )
    result = engine.consume_event(engine_state, store, content, bank, outcome, NOW)
    assert result.engine_state.placement_cursor["NUMBER.FRACTIONS"] == "FRAC.PARTWHOLE"


def test_placement_fail_below_frontier_n_moves_to_a_prerequisite(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-walk-fail"
    store = StudentModelStore()
    store.replace_records(
        student_id,
        {
            "FRAC.PARTWHOLE": SkillState(n=1.0, certified_depth=None),
            "FRAC.UNIT": SkillState(n=0.0, certified_depth=None),
        },
    )
    engine_state = engine.EngineState(
        placement_cursor={"NUMBER.FRACTIONS": "FRAC.PARTWHOLE"}, placement_initialized=True
    )
    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=["FRAC.PARTWHOLE"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=False,
        misconception_id=None,
        context="placement",
        depth_served=0.2,
    )
    result = engine.consume_event(engine_state, store, content, bank, outcome, NOW)
    assert result.engine_state.placement_cursor["NUMBER.FRACTIONS"] == "FRAC.UNIT"


def test_placement_stops_at_frontier_once_n_reaches_three(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    """SM §5 point 3: uncertified, prerequisites certified (vacuously, for
    a root skill), n >= 3 -> that skill is the frontier; the strand's
    placement cursor is retired."""
    student_id = "stu-frontier"
    store = StudentModelStore()
    store.replace_records(student_id, {"FRAC.UNIT": SkillState(n=3.0, certified_depth=None)})
    engine_state = engine.EngineState(placement_cursor={"NUMBER.FRACTIONS": "FRAC.UNIT"}, placement_initialized=True)
    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=["FRAC.UNIT"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=True,
        misconception_id=None,
        context="placement",
        depth_served=0.2,
    )
    result = engine.consume_event(engine_state, store, content, bank, outcome, NOW)
    assert "NUMBER.FRACTIONS" not in result.engine_state.placement_cursor


def test_placement_fail_with_prereq_already_certified_ends_the_walk(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """A fail at a skill whose only prerequisite is already certified has
    nowhere left to walk back to (a documented dead-end behavior, see the
    module report) — the strand's placement ends rather than looping."""
    student_id = "stu-dead-end"
    store = StudentModelStore()
    store.replace_records(
        student_id,
        {
            "FRAC.PARTWHOLE": SkillState(n=2.0, certified_depth=None),
            "FRAC.UNIT": SkillState(n=5.0, certified_depth=0.3, review_due=NOW + timedelta(days=7)),
        },
    )
    engine_state = engine.EngineState(
        placement_cursor={"NUMBER.FRACTIONS": "FRAC.PARTWHOLE"}, placement_initialized=True
    )
    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=["FRAC.PARTWHOLE"],
        item_instance_id="ignored",
        variant_signature="ignored-sig",
        correct=False,
        misconception_id=None,
        context="placement",
        depth_served=0.2,
    )
    result = engine.consume_event(engine_state, store, content, bank, outcome, NOW)
    assert "NUMBER.FRACTIONS" not in result.engine_state.placement_cursor


def test_fresh_student_placement_starts_at_grade_mapped_frontier_skill(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """CA grade 4 maps FRAC.UNIT/PARTWHOLE/CMP, all strand NUMBER.FRACTIONS;
    the lowest canonical_stage among them (FRAC.UNIT, stage 1) is the
    chosen starting cursor, probed at its easy-tier prior (one below its
    own easy mapping, floored at easy)."""
    store = StudentModelStore()
    engine_state = engine.EngineState()
    result = engine.select_next(store, engine_state, content, bank, "stu-fresh", "CA", "ONTARIO", 4, NOW)
    assert result.serve.reason == "placement"
    assert result.serve.skill_id == "FRAC.UNIT"
    assert result.serve.difficulty == pytest.approx(0.2)


# --- select_next: prerequisite gating (R12) ------------------------------------


def _mini_bank(bank_index: engine.BankIndex, skill_id: str) -> engine.BankIndex:
    pool = bank_index.instances_by_skill[skill_id]
    return engine.BankIndex(
        bank_version="mini",
        instances_by_skill={skill_id: pool},
        by_id={inst["item_instance_id"]: inst for inst in pool},
    )


def test_frontier_practice_blocked_while_prerequisite_uncertified(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    store = StudentModelStore()  # FRAC.EQUIV starts uncertified
    mini = _mini_bank(bank, "FRAC.CMP")
    result = engine.select_next(store, _bypass_placement(), content, mini, "stu1", "CA", "ONTARIO", 6, NOW)
    assert result.serve is None


def test_frontier_practice_allowed_while_prerequisite_due(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    """§6.3: 'due' satisfies a prerequisite check the same as 'certified'."""
    store = StudentModelStore()
    store.replace_records(
        "stu1", {"FRAC.EQUIV": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1))}
    )
    mini = _mini_bank(bank, "FRAC.CMP")
    result = engine.select_next(store, _bypass_placement(), content, mini, "stu1", "CA", "ONTARIO", 6, NOW)
    assert result.serve is not None
    assert result.serve.skill_id == "FRAC.CMP"


def test_frontier_practice_blocked_while_prerequisite_lapsed(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    store = StudentModelStore()
    store.replace_records(
        "stu1",
        {"FRAC.EQUIV": SkillState(certified_depth=0.5, review_due=NOW - timedelta(days=1), lapsed=True)},
    )
    mini = _mini_bank(bank, "FRAC.CMP")
    result = engine.select_next(store, _bypass_placement(), content, mini, "stu1", "CA", "ONTARIO", 6, NOW)
    assert result.serve is None


def test_frontier_practice_reoffers_a_skill_lapsed_at_ceiling_depth(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """P0 regression (code review correctness fix, SM §6 point 3): a skill
    lapsed at exactly its serving ceiling must remain eligible for frontier
    practice. `due_reviews` (R12 step 1) never picks it up —
    certification_status returns STATUS_LAPSED, not STATUS_DUE, once
    lapsed — so frontier practice is the only path back into the queue.
    Before the fix, the "already mastered, skip" check didn't account for
    `lapsed` and permanently excluded this skill from ever being served
    again, contradicting §6's "no cascade of revoked progress" rule."""
    store = StudentModelStore()  # FRAC.UNIT/CA/4 ceiling is easy-tier (0.2), no prerequisites
    store.replace_records("stu1", {"FRAC.UNIT": SkillState(certified_depth=0.2, n=5.0, lapsed=True)})
    mini = _mini_bank(bank, "FRAC.UNIT")
    result = engine.select_next(store, _bypass_placement(), content, mini, "stu1", "CA", "ONTARIO", 4, NOW)
    assert result.serve is not None
    assert result.serve.skill_id == "FRAC.UNIT"
    assert result.serve.reason == "frontier"


def test_frontier_practice_skips_a_certified_non_lapsed_skill_at_ceiling(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Contrast case for the fix above: the same skill, certified to
    ceiling but NOT lapsed, is correctly excluded — nothing left to offer
    at or below the ceiling."""
    store = StudentModelStore()
    store.replace_records("stu1", {"FRAC.UNIT": SkillState(certified_depth=0.2, n=5.0, lapsed=False)})
    mini = _mini_bank(bank, "FRAC.UNIT")
    result = engine.select_next(store, _bypass_placement(), content, mini, "stu1", "CA", "ONTARIO", 4, NOW)
    assert result.serve is None


# --- select_next: full R12 priority order --------------------------------------


def test_due_review_is_served_before_everything_else(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-priority"
    store = StudentModelStore()
    store.replace_records(
        student_id, {"FRAC.ADD.LIKE": SkillState(certified_depth=0.2, review_due=NOW - timedelta(days=1))}
    )
    engine_state = _bypass_placement()
    # Also stack a remediation dose + stepdown + prereq probe so a naive
    # implementation that checked them first would fail this test.
    engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.EQUIV", trigger_skill_id="FRAC.ADD.UNLIKE", misconception_id="MISC.X")
    )
    engine_state.stepdown_queue.append(engine.StepDownProbe(skill_id="FRAC.ADD.UNLIKE", target_depth=0.2))
    engine_state.prereq_probe_queue.append(engine.PrereqProbe(skill_id="FRAC.UNIT", trigger_skill_id="FRAC.PARTWHOLE"))

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "review"
    assert result.serve.skill_id == "FRAC.ADD.LIKE"
    assert result.serve.context == "review"
    assert result.serve.difficulty == pytest.approx(0.2)


def test_remediation_outranks_stepdown_and_prereq_probes(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-priority-2"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.EQUIV", trigger_skill_id="FRAC.ADD.UNLIKE", misconception_id="MISC.X")
    )
    engine_state.stepdown_queue.append(engine.StepDownProbe(skill_id="FRAC.ADD.UNLIKE", target_depth=0.2))
    engine_state.prereq_probe_queue.append(engine.PrereqProbe(skill_id="FRAC.UNIT", trigger_skill_id="FRAC.PARTWHOLE"))

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "remediation"
    assert result.serve.skill_id == "FRAC.EQUIV"


def test_stepdown_outranks_prereq_probes(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-priority-3"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    engine_state.stepdown_queue.append(engine.StepDownProbe(skill_id="FRAC.ADD.UNLIKE", target_depth=0.2))
    engine_state.prereq_probe_queue.append(engine.PrereqProbe(skill_id="FRAC.UNIT", trigger_skill_id="FRAC.PARTWHOLE"))

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "stepdown"


def test_prereq_probes_outrank_placement(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    student_id = "stu-priority-4"
    store = StudentModelStore()
    engine_state = engine.EngineState()  # placement NOT bypassed this time
    engine_state.prereq_probe_queue.append(engine.PrereqProbe(skill_id="FRAC.UNIT", trigger_skill_id="FRAC.PARTWHOLE"))

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "prereq_probe"
    assert result.serve.skill_id == "FRAC.UNIT"


def test_due_review_falls_through_to_next_priority_when_no_candidates(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """R12 step 1's due-review branch must fall through to the next
    priority level (here, step 2's remediation dose) when `_try_serve`
    can't find a candidate for the due skill — e.g. its bank pool has zero
    instances — rather than returning `serve=None` outright."""
    student_id = "stu-review-fallthrough"
    store = StudentModelStore()
    store.replace_records(
        student_id, {"FRAC.ADD.LIKE": SkillState(certified_depth=0.2, review_due=NOW - timedelta(days=1))}
    )
    engine_state = _bypass_placement()
    engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.EQUIV", trigger_skill_id="FRAC.ADD.UNLIKE", misconception_id="MISC.X")
    )
    mini = _mini_bank(bank, "FRAC.EQUIV")  # FRAC.ADD.LIKE (the due skill) has zero instances here

    result = engine.select_next(store, engine_state, content, mini, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve is not None
    assert result.serve.reason == "remediation"
    assert result.serve.skill_id == "FRAC.EQUIV"


# --- seen filter: end-to-end via select_next -----------------------------------


def test_select_next_repeats_are_avoided_then_warn_on_exhaustion(content: engine.ContentIndex, bank: engine.BankIndex) -> None:
    """Restrict to exactly two FRAC.UNIT instances so pool exhaustion is
    deterministic: two placement-tier serves at the same target depth
    should never repeat; the third must warn and repeat."""
    student_id = "stu-seen-filter"
    store = StudentModelStore()
    engine_state = engine.EngineState(placement_cursor={"NUMBER.FRACTIONS": "FRAC.UNIT"}, placement_initialized=True)
    full_pool = bank.instances_by_skill["FRAC.UNIT"]
    two = full_pool[:2]
    mini = engine.BankIndex(
        bank_version="mini", instances_by_skill={"FRAC.UNIT": two}, by_id={i["item_instance_id"]: i for i in two}
    )
    assert len(mini.instances_by_skill["FRAC.UNIT"]) == 2

    served_ids = []
    for _ in range(2):
        result = engine.select_next(store, engine_state, content, mini, student_id, "CA", "ONTARIO", 4, NOW)
        assert result.warnings == []
        served_ids.append(result.serve.item_instance_id)
        engine_state = result.engine_state
    assert len(set(served_ids)) == 2  # no repeat while both were unseen

    result = engine.select_next(store, engine_state, content, mini, student_id, "CA", "ONTARIO", 4, NOW)
    assert result.serve.item_instance_id in served_ids  # pool exhausted: must repeat
    assert result.warnings != []


def test_select_next_routes_candidate_pool_reads_through_candidates_for_skill(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """U6: when given, `candidates_for_skill` replaces every bank read
    select_next would otherwise make (the reference orchestrator routes
    these through orchestrator/serving.py's get_candidates). Proof: a
    provider that only ever exposes one FRAC.UNIT instance forces
    placement to serve exactly that one, never anything else the real bank
    pool actually contains for that skill."""
    student_id = "stu-provider-routed"
    store = StudentModelStore()
    engine_state = engine.EngineState(placement_cursor={"NUMBER.FRACTIONS": "FRAC.UNIT"}, placement_initialized=True)
    only_instance = bank.instances_by_skill["FRAC.UNIT"][0]
    calls: list[str] = []

    def provider(skill_id: str) -> list[dict]:
        calls.append(skill_id)
        return [only_instance] if skill_id == "FRAC.UNIT" else []

    result = engine.select_next(
        store, engine_state, content, bank, student_id, "CA", "ONTARIO", 4, NOW, candidates_for_skill=provider
    )
    assert "FRAC.UNIT" in calls
    assert result.serve is not None
    assert result.serve.item_instance_id == only_instance["item_instance_id"]


def test_select_next_copy_on_write_does_not_mutate_callers_engine_state(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Safety net for the `_copy_engine_state` copy-on-write performance fix
    (code review): the caller's own EngineState — and specifically the
    `seen_history` list it passed in — must remain untouched. Only the
    returned copy grows."""
    engine_state = _bypass_placement()
    engine_state.seen_history["FRAC.UNIT"] = ["existing-sig"]
    mini = _mini_bank(bank, "FRAC.UNIT")
    store = StudentModelStore()

    result = engine.select_next(store, engine_state, content, mini, "stu1", "CA", "ONTARIO", 4, NOW)
    served_signature = mini.by_id[result.serve.item_instance_id]["variant_signature"]

    assert engine_state.seen_history["FRAC.UNIT"] == ["existing-sig"]  # caller's copy is untouched
    assert served_signature not in engine_state.seen_history["FRAC.UNIT"]
    assert served_signature in result.engine_state.seen_history["FRAC.UNIT"]


# --- rank_serve_candidates: R7's shared ranking function (U8) ------------------


def test_rank_serve_candidates_has_exactly_two_call_sites() -> None:
    """R7's "exactly one implementation, two callers": `select_next`
    (orchestrator/engine.py) and `ServingFacade.recommend_next`
    (orchestrator/serving.py) must be the only two places that call
    `rank_serve_candidates` — a grep-based check that the priority-chain
    ranking logic was actually extracted into one shared function, not
    duplicated for the new recommend_next path."""
    import re

    pattern = re.compile(r"(?<!def )rank_serve_candidates\(")
    call_sites = 0
    for relative_path in ("orchestrator/engine.py", "orchestrator/serving.py"):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        call_sites += len(pattern.findall(text))
    assert call_sites == 2


def test_rank_serve_candidates_full_priority_order_review_through_placement(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Every queue head plus the placement cursor, all populated at once
    (mirroring test_due_review_is_served_before_everything_else's setup),
    must appear in `rank_serve_candidates`'s output in R12's exact order —
    not just as select_next's single winning pick, but as the full ranked
    list the plan calls for."""
    student_id = "stu-rank-order"
    store = StudentModelStore()
    store.replace_records(
        student_id, {"FRAC.ADD.LIKE": SkillState(certified_depth=0.2, review_due=NOW - timedelta(days=1))}
    )
    engine_state = engine.EngineState()  # placement NOT bypassed: exercise the cursor too
    engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.EQUIV", trigger_skill_id="FRAC.ADD.UNLIKE", misconception_id="MISC.X")
    )
    engine_state.stepdown_queue.append(engine.StepDownProbe(skill_id="FRAC.ADD.UNLIKE", target_depth=0.2))
    engine_state.prereq_probe_queue.append(engine.PrereqProbe(skill_id="FRAC.UNIT", trigger_skill_id="FRAC.PARTWHOLE"))

    ranked = engine.rank_serve_candidates(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)

    reasons = [c.reason for c in ranked]
    # The first four entries are exactly the four single-candidate steps,
    # in R12's priority order; placement/frontier entries (if any) follow.
    assert reasons[:4] == ["review", "remediation", "stepdown", "prereq_probe"]
    assert ranked[0].skill_id == "FRAC.ADD.LIKE"
    assert ranked[1].skill_id == "FRAC.EQUIV"
    assert ranked[2].skill_id == "FRAC.ADD.UNLIKE"
    assert ranked[3].skill_id == "FRAC.UNIT"
    # Placement was not bypassed, so its lazily-initialized cursor entries
    # (one per strand) must also be present, ranked after the four queues.
    assert "placement" in reasons[4:]
    # select_next, given the identical state, must still pick this same
    # top candidate — the refactor changed nothing about its own behavior.
    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve.reason == "review"
    assert result.serve.skill_id == "FRAC.ADD.LIKE"


def test_rank_serve_candidates_frontier_top_entry_matches_select_next(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """With every queue/placement step empty, only frontier candidates
    remain — `rank_serve_candidates`'s top-ranked entry must name the same
    skill `select_next` actually serves for the identical state (the R7
    parity this unit's plan asks for)."""
    student_id = "stu-rank-frontier"
    store = StudentModelStore()
    engine_state = _bypass_placement()

    ranked = engine.rank_serve_candidates(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert ranked
    assert ranked[0].reason == "frontier"

    result = engine.select_next(store, engine_state, content, bank, student_id, "CA", "ONTARIO", 6, NOW)
    assert result.serve is not None
    assert result.serve.reason == "frontier"
    assert result.serve.skill_id == ranked[0].skill_id


def test_rank_serve_candidates_flags_a_bank_absent_skill_as_unservable_without_dropping_it(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """A queue-head skill the checked bank has zero instances for must
    still appear in the ranked list, flagged `unservable=True` — never
    silently omitted (this unit's bank-awareness contract, exercised at
    the engine level; tests/test_serving.py covers the same contract
    through `ServingFacade.recommend_next`)."""
    student_id = "stu-rank-unservable"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    # FRAC.EQUIV has real instances in `bank`; a mini bank scoped to a
    # different skill has none for it at all.
    engine_state.remediation_queue.append(
        engine.RemediationDose(skill_id="FRAC.EQUIV", trigger_skill_id="FRAC.ADD.UNLIKE", misconception_id="MISC.X")
    )
    mini = _mini_bank(bank, "FRAC.ADD.UNLIKE")

    ranked = engine.rank_serve_candidates(store, engine_state, content, mini, student_id, "CA", "ONTARIO", 6, NOW)
    remediation_entries = [c for c in ranked if c.reason == "remediation"]
    assert len(remediation_entries) == 1
    assert remediation_entries[0].skill_id == "FRAC.EQUIV"
    assert remediation_entries[0].unservable is True
    assert remediation_entries[0].bank_candidates == []


# --- rebuild_engine_state: KTD11 replay reproduces live queue state -----------


def make_event(bank_index: engine.BankIndex, **overrides) -> dict:
    instance = bank_index.instances_by_skill["FRAC.ADD.UNLIKE"][-1]  # the 0.5-depth instance
    defaults = dict(
        event_id="e1",
        student_id="stu-replay",
        session_id="sess1",
        timestamp="2026-07-27T12:00:00",
        item_instance_id=instance["item_instance_id"],
        template_id=instance["template_id"],
        template_version=instance["template_version"],
        bank_version=bank_index.bank_version,
        variant_signature=instance["variant_signature"],
        consumer_id=constants.CONSUMER_ID_INTERNAL,
        engine_version=constants.ENGINE_VERSION,
        graph_version=instance["graph_version"],
        skill_ids=["FRAC.ADD.UNLIKE"],
        question_type=instance["question_type"],
        depth_served=instance["difficulty"],
        b_prior_at_serve=instance["difficulty"],
        exam_context=None,
        language="en-IN",
        locale="en-IN",
        response_raw="no-match",
        response_parsed="no-match",
        correct=False,
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


def test_engine_replay_reproduces_pending_stepdown_probe(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """A log ending right after an untagged deep failure, replayed, must
    reproduce the same queued step-down probe a live session would have
    pending (KTD11)."""
    log_path = tmp_path / "log.jsonl"
    event = make_event(bank)  # untagged (misconception_id=None), incorrect, depth 0.5
    append_attempt(log_path, event)

    # Live-equivalent: apply the same event through the documented protocol.
    live_store = StudentModelStore()
    live_engine_state = engine.EngineState()
    from orchestrator.log import apply_event  # same public helper rebuild_engine_state reuses

    apply_event(live_store, event)
    outcome = engine.attempt_outcome_from_event(event)
    live_result = engine.consume_event(live_engine_state, live_store, content, bank, outcome, NOW)

    replayed_state = engine.rebuild_engine_state(log_path, content, engine.make_static_bank_lookup(bank))

    assert len(replayed_state.stepdown_queue) == 1
    assert replayed_state.stepdown_queue == live_result.engine_state.stepdown_queue
    assert replayed_state.seen_history == live_result.engine_state.seen_history


def test_engine_replay_reproduces_remediation_dose_after_two_tagged_hits(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    log_path = tmp_path / "log.jsonl"
    _, value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    e1 = make_event(bank, event_id="e1", misconception_id="MISC.FRAC.ADD-ACROSS", response_parsed=value)
    e2 = make_event(bank, event_id="e2", misconception_id="MISC.FRAC.ADD-ACROSS", response_parsed=value, timestamp="2026-07-27T12:05:00")
    append_attempt(log_path, e1)
    append_attempt(log_path, e2)

    replayed_state = engine.rebuild_engine_state(log_path, content, engine.make_static_bank_lookup(bank))
    assert len(replayed_state.remediation_queue) == 1
    assert replayed_state.remediation_queue[0].skill_id == "FRAC.EQUIV"


def _minimal_attempt_event(**overrides) -> dict:
    defaults = dict(
        event_id="e1",
        student_id="stu-multibuild",
        session_id="sess1",
        timestamp="2026-07-27T12:00:00",
        item_instance_id="deep",
        template_id="TPL.TEST.001",
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
        language="en-IN",
        locale="en-IN",
        response_raw="wrong",
        response_parsed="wrong",
        correct=False,
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


def test_rebuild_session_state_resolves_each_event_against_its_stamped_build(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """A log spanning two bank versions replays each event against its
    stamped build (KTD7): bank-a still has an easier-depth FRAC.ADD.UNLIKE
    instance (an untagged deep failure there queues a step-down probe to
    it), while bank-b (as if rebuilt without that easier variant) does not
    — the same failure resolved against bank-b's pool instead falls
    straight to the prerequisite walk. Proves the replay path re-derives
    each event's own live decision rather than one fixed bank for the whole
    log."""
    bank_a = engine.BankIndex(
        bank_version="bank-a",
        instances_by_skill={
            "FRAC.ADD.UNLIKE": [
                {"item_instance_id": "easy", "difficulty": 0.2},
                {"item_instance_id": "deep", "difficulty": 0.5},
            ]
        },
        by_id={},
    )
    bank_b = engine.BankIndex(
        bank_version="bank-b",
        instances_by_skill={"FRAC.ADD.UNLIKE": [{"item_instance_id": "deep-b", "difficulty": 0.5}]},
        by_id={},
    )

    log_path = tmp_path / "log.jsonl"
    append_attempt(log_path, _minimal_attempt_event(bank_version="bank-a", item_instance_id="deep"))
    _store, replayed = engine.rebuild_session_state(
        log_path, content, engine.make_static_bank_lookup(bank_a, bank_b)
    )
    assert len(replayed.stepdown_queue) == 1
    assert replayed.stepdown_queue[0].target_depth == pytest.approx(0.2)

    # Same event, resolved only against bank-b (no easier instance there):
    # the diagnosis differs — a prerequisite probe, not a step-down —
    # proving the per-event bank resolution is what drives the decision.
    wrong_log = tmp_path / "wrong.jsonl"
    append_attempt(wrong_log, _minimal_attempt_event(bank_version="bank-b", item_instance_id="deep-b"))
    _store2, replayed_wrong = engine.rebuild_session_state(
        wrong_log, content, engine.make_static_bank_lookup(bank_b)
    )
    assert replayed_wrong.stepdown_queue == []
    assert len(replayed_wrong.prereq_probe_queue) >= 1
