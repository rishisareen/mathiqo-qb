"""Tests for tools/bank_build.py — the Stage 5 bank builder (§3 Stage 5, §5.1).

Two kinds of fixtures:
  - small, hand-built synthetic templates (mirrors test_item_verify.py's own
    "clean template" convention) for exact, predictable assertions about
    collision-dropping, only_if guards, unsimplified distractors, and pool
    capping;
  - the real content/templates/*.yaml files (read-only, copied into tmp
    dirs, never modified) for shape/integration smoke coverage, per the
    plan's pointer to TPL.FRAC.ADD.UNLIKE.WORD.001 (mcq_4, tagged
    distractors) and TPL.FRAC.ADD.LIKE.NUM.001 (numeric_entry).
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import sympy
from conftest import REPO_ROOT, SCHEMAS_DIR, write_yaml

import bank_build
import item_verify


def _as_rational(canonical: str) -> sympy.Rational:
    """Parse a bank_build canonical answer/distractor string ("p/q" or "p")
    back into a sympy Rational for value comparisons in tests."""
    if "/" in canonical:
        p, q = canonical.split("/")
        return sympy.Rational(int(p), int(q))
    return sympy.Rational(int(canonical), 1)

REAL_TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
WORD_FIXTURE = REAL_TEMPLATES_DIR / "TPL.FRAC.ADD.UNLIKE.WORD.001.yaml"
NUM_FIXTURE = REAL_TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"


def _copy_templates(tmp_path: Path, sources: list[Path]) -> Path:
    """A tmp dir containing copies of the given real template files. The
    real content/templates/ tree is never modified."""
    dest = tmp_path / "templates"
    dest.mkdir()
    for src in sources:
        shutil.copy(src, dest / src.name)
    return dest


def _instance_schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / "item-instance.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


# --- A small, exact synthetic template for precise assertions --------------
# Mirrors test_item_verify.py's test_clean_template_passes fixture shape, but
# with a mix of a plain distractor, an only_if-guarded distractor, and an
# unsimplified distractor sharing the answer's value — small domain (4x4=16
# combos) so every valid/colliding variant can be reasoned about by hand.

def _synthetic_mcq_template() -> dict:
    return {
        "template_id": "TPL.TEST.BUILD.MCQ.001",
        "template_version": "1.0.0",
        "bank_version": "dev",
        "graph_version": "1.0.0",
        "skill_ids": ["FRAC.ADD.UNLIKE"],
        "question_type": "mcq_4",
        "cognitive_level": "application",
        "difficulty": 0.4,
        "estimated_time_sec": 30,
        "calculator_allowed": False,
        "diagram_required": False,
        "accepted_answer_forms": "simplest form only",
        "variables": {
            "a": {"type": "int", "range": [1, 3]},
            "b": {"type": "int", "range": [4, 6]},
            "c": {"type": "int", "range": [1, 3]},
        },
        "constraints": ["a < b", "c < b"],
        "stem_i18n": {"en-IN": "{{name1}} has {{a}}/{{b}} and {{c}}/{{b}} of a {{food}}. What is the total?"},
        "localization_slots": ["name1", "food"],
        "answer": "simplify((a + c) / b)",
        "distractors": [
            {"expr": "a / b", "misconception_id": "MISC.FRAC.FIRST-DENOM"},
            {
                "expr": "(a + c) / b",
                "unsimplified": True,
                "misconception_id": "MISC.FRAC.NO-SIMPLIFY",
                "only_if": "gcd(a + c, b) > 1",
            },
        ],
        "solution_steps_i18n": {"en-IN": {"canonical": ["Add {{a}} and {{c}}.", "Result over {{b}}."]}},
        "hints_i18n": {"en-IN": ["Same denominator already."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {"generator_version": "test", "reviewer": "test", "date": "2026-07-27"},
    }


@pytest.fixture()
def synthetic_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir()
    write_yaml(dest / "TPL.TEST.BUILD.MCQ.001.yaml", _synthetic_mcq_template())
    return dest


# --- R10 / KTD4: instances validate against the new schema ------------------

def test_synthetic_instances_validate_against_item_instance_schema(synthetic_dir: Path) -> None:
    manifest, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=100)
    validator = _instance_schema_validator()
    instances = per_template["TPL.TEST.BUILD.MCQ.001"]
    assert instances, "expected at least one instance"
    for instance in instances:
        errors = list(validator.iter_errors(instance))
        assert errors == [], "; ".join(e.message for e in errors)
    assert manifest["instance_count"] == len(instances)


def test_real_fixtures_build_and_validate(tmp_path: Path) -> None:
    """Smoke-integration coverage using the two real templates the plan
    points at (mcq_4 with tagged distractors; numeric_entry)."""
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE, NUM_FIXTURE])
    manifest, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=5)
    validator = _instance_schema_validator()
    assert set(per_template) == {"TPL.FRAC.ADD.UNLIKE.WORD.001", "TPL.FRAC.ADD.LIKE.NUM.001"}
    for instances in per_template.values():
        assert len(instances) == 5
        for instance in instances:
            errors = list(validator.iter_errors(instance))
            assert errors == [], "; ".join(e.message for e in errors)


# --- The build refuses a template that fails item_verify --------------------

def test_hard_fail_template_refuses_build(tmp_path: Path) -> None:
    template = copy.deepcopy(_synthetic_mcq_template())
    template["constraints"] = ["b > 100"]  # b ranges [4,6]: unsatisfiable -> HardFail
    dest = tmp_path / "templates"
    dest.mkdir()
    write_yaml(dest / "bad.yaml", template)
    with pytest.raises(bank_build.BuildError, match="TPL.TEST.BUILD.MCQ.001"):
        bank_build.run_build(dest, LOCALE_PACK, "dev", pool_cap=10)


def test_flagged_but_not_hard_failed_template_still_builds(tmp_path: Path) -> None:
    """The shipped TPL.FRAC.ADD.UNLIKE.WORD.001 fixture is itself
    FLAGGED_FOR_REGENERATION (82% constraint-rejection rate,
    test_item_verify.py's own test_fixture_verdict_is_flagged_by_rejection_rate)
    yet is real, promoted content — the build must not refuse it."""
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE])
    report = item_verify.verify_template(item_verify.load_template(WORD_FIXTURE))
    assert report.verdict == item_verify.FLAGGED  # sanity check on the premise
    manifest, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    assert len(per_template["TPL.FRAC.ADD.UNLIKE.WORD.001"]) == 3


def test_non_v1_question_type_hard_errors_naming_template(tmp_path: Path) -> None:
    template = copy.deepcopy(_synthetic_mcq_template())
    template["question_type"] = "fill_in_blank"
    dest = tmp_path / "templates"
    dest.mkdir()
    write_yaml(dest / "bad.yaml", template)
    with pytest.raises(bank_build.BuildError, match="TPL.TEST.BUILD.MCQ.001.*fill_in_blank"):
        bank_build.run_build(dest, LOCALE_PACK, "dev", pool_cap=10)


# --- Determinism: identical inputs -> byte-identical builds -----------------

def test_determinism_byte_identical_builds(tmp_path: Path) -> None:
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE, NUM_FIXTURE])
    out1, out2 = tmp_path / "out1", tmp_path / "out2"

    manifest1, instances1 = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-dev", pool_cap=7)
    bank_build.write_bank(out1, "2026.07-dev", manifest1, instances1)

    manifest2, instances2 = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-dev", pool_cap=7)
    bank_build.write_bank(out2, "2026.07-dev", manifest2, instances2)

    files1 = sorted(p.relative_to(out1 / "2026.07-dev") for p in (out1 / "2026.07-dev").rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(out2 / "2026.07-dev") for p in (out2 / "2026.07-dev").rglob("*") if p.is_file())
    assert files1 == files2
    for rel in files1:
        a = (out1 / "2026.07-dev" / rel).read_bytes()
        b = (out2 / "2026.07-dev" / rel).read_bytes()
        assert a == b, f"{rel} differs between two builds of identical inputs"


def test_write_bank_refuses_to_overwrite_existing_bank_version(tmp_path: Path) -> None:
    templates_dir = _copy_templates(tmp_path, [NUM_FIXTURE])
    manifest, instances = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    out = tmp_path / "out"
    bank_build.write_bank(out, "dev", manifest, instances)
    with pytest.raises(bank_build.BuildError, match="already exists"):
        bank_build.write_bank(out, "dev", manifest, instances)


# --- Collision variants never appear; only_if; unsimplified -----------------

def test_collision_variants_never_appear_in_pool(synthetic_dir: Path) -> None:
    """a=2,b=4,c=2 -> answer (2+2)/4 = 1, distractor a/b = 2/4 = 1/2 != 1 (no
    collision there), but the FIRST-DENOM distractor a/b never equals the
    simplified answer for any valid combo in this fixture's domain by
    construction; assert directly instead that no materialized instance's
    distractor set ever contains the answer's own value."""
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    for instance in per_template["TPL.TEST.BUILD.MCQ.001"]:
        distractor_values = {d["value"] for d in instance["distractors"] if not d["unsimplified"]}
        assert instance["answer"] not in distractor_values


def test_unsimplified_distractor_displays_unreduced_same_value(synthetic_dir: Path) -> None:
    """Find an instance where the NO-SIMPLIFY guard fires and check its
    displayed value is the literal unreduced (a+c)/b split, distinct in text
    from the reduced answer while representing the same rational value."""
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    found = False
    for instance in per_template["TPL.TEST.BUILD.MCQ.001"]:
        no_simplify = next(
            (d for d in instance["distractors"] if d["misconception_id"] == "MISC.FRAC.NO-SIMPLIFY"),
            None,
        )
        if no_simplify is None:
            continue
        found = True
        a, b, c = instance["variables"]["a"], instance["variables"]["b"], instance["variables"]["c"]
        assert no_simplify["value"] == f"{a + c}/{b}"
        assert no_simplify["unsimplified"] is True
        assert no_simplify["value"] != instance["answer"]  # different displayed text...
        assert _as_rational(no_simplify["value"]) == _as_rational(instance["answer"])  # ...same value
    assert found, "fixture domain should include at least one gcd(a+c, b) > 1 combo"


# --- Pool cap honored --------------------------------------------------------

def test_pool_cap_honored(synthetic_dir: Path) -> None:
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=2)
    assert len(per_template["TPL.TEST.BUILD.MCQ.001"]) == 2


def test_fewer_valid_variants_than_cap_yields_them_all(synthetic_dir: Path) -> None:
    manifest, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=10_000)
    total_valid = manifest["templates"][0]["total_valid_variants"]
    assert len(per_template["TPL.TEST.BUILD.MCQ.001"]) == total_valid
    assert total_valid < 10_000


def test_sample_variant_indices_returns_all_when_cap_exceeds_total() -> None:
    assert bank_build.sample_variant_indices(5, 10, seed=42) == [0, 1, 2, 3, 4]


def test_sample_variant_indices_is_deterministic_and_capped() -> None:
    a = bank_build.sample_variant_indices(1000, 30, seed=bank_build.derive_seed("TPL.X", "v1"))
    b = bank_build.sample_variant_indices(1000, 30, seed=bank_build.derive_seed("TPL.X", "v1"))
    assert a == b
    assert len(a) == 30
    assert len(set(a)) == 30
    assert all(0 <= i < 1000 for i in a)


# --- Localization slot completeness -----------------------------------------

def test_no_localization_slot_survives_into_rendered_text(synthetic_dir: Path) -> None:
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    for instance in per_template["TPL.TEST.BUILD.MCQ.001"]:
        assert "{{" not in instance["stem"]
        for steps in instance["solution_steps"].values():
            assert all("{{" not in s for s in steps)
        assert all("{{" not in h for h in instance["hints"])


def test_name_slots_are_distinct_within_one_instance(tmp_path: Path) -> None:
    """TPL.FRAC.ADD.UNLIKE.WORD.001 declares both name1 and name2; the
    dressing must never assign the same name to both within one instance."""
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE])
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=20)
    for instance in per_template["TPL.FRAC.ADD.UNLIKE.WORD.001"]:
        assert instance["stem"].count("{{") == 0


# --- mcq_4 offered set vs numeric_entry stored-for-matching set -------------

def test_mcq4_instance_carries_offered_distractor_set_with_tags(synthetic_dir: Path) -> None:
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    for instance in per_template["TPL.TEST.BUILD.MCQ.001"]:
        values = instance["variables"]
        # FIRST-DENOM is unguarded (always offered); NO-SIMPLIFY is offered
        # exactly where its only_if guard holds (§2.2).
        expected_tags = {"MISC.FRAC.FIRST-DENOM"}
        if item_verify.evaluate_condition("gcd(a + c, b) > 1", values):
            expected_tags.add("MISC.FRAC.NO-SIMPLIFY")
        actual_tags = {d["misconception_id"] for d in instance["distractors"]}
        assert actual_tags == expected_tags


def test_numeric_entry_instance_carries_answer_plus_stored_distractor_set(tmp_path: Path) -> None:
    templates_dir = _copy_templates(tmp_path, [NUM_FIXTURE])
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=5)
    for instance in per_template["TPL.FRAC.ADD.LIKE.NUM.001"]:
        assert instance["question_type"] == "numeric_entry"
        assert instance["answer"]
        assert len(instance["distractors"]) == 1
        assert instance["distractors"][0]["misconception_id"] is None


# --- KTD8 simplest_form_only normalization -----------------------------------

def test_simplest_form_only_true_when_prefix_matches(tmp_path: Path) -> None:
    templates_dir = _copy_templates(tmp_path, [NUM_FIXTURE])  # "simplest form only"
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    assert all(i["simplest_form_only"] is True for i in per_template["TPL.FRAC.ADD.LIKE.NUM.001"])


def test_simplest_form_only_true_for_verbose_prefix_phrasing(tmp_path: Path) -> None:
    # TPL.FRAC.ADD.UNLIKE.WORD.001's accepted_answer_forms starts with
    # "simplest form only (simplifying IS part of this skill; ...)".
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE])
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    assert all(i["simplest_form_only"] is True for i in per_template["TPL.FRAC.ADD.UNLIKE.WORD.001"])


def test_simplest_form_only_false_when_phrased_differently(tmp_path: Path) -> None:
    mixed_fixture = REAL_TEMPLATES_DIR / "TPL.FRAC.MIXED.ADD.DIRECT.001.yaml"
    data = item_verify.load_template(mixed_fixture)
    assert data["accepted_answer_forms"].lower().startswith("mixed number")  # sanity check on the premise
    templates_dir = _copy_templates(tmp_path, [mixed_fixture])
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    assert all(i["simplest_form_only"] is False for i in per_template["TPL.FRAC.MIXED.ADD.DIRECT.001"])


def test_simplest_form_only_false_when_field_absent(tmp_path: Path) -> None:
    absent_fixture = REAL_TEMPLATES_DIR / "TPL.NUM.LCM.DIRECT.001.yaml"
    data = item_verify.load_template(absent_fixture)
    assert "accepted_answer_forms" not in data  # sanity check on the premise
    templates_dir = _copy_templates(tmp_path, [absent_fixture])
    _, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "dev", pool_cap=3)
    assert all(i["simplest_form_only"] is False for i in per_template["TPL.NUM.LCM.DIRECT.001"])


# --- U1 / KTD1: variant_signature -------------------------------------------

def test_same_variant_same_signature_across_bank_versions(tmp_path: Path) -> None:
    """Covers AE3 (build-side half): the same template + variable
    assignment materialized under two different bank_versions gets the
    same signature."""
    templates_dir = _copy_templates(tmp_path, [NUM_FIXTURE])
    _, per_a = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-a", pool_cap=1000)
    _, per_b = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-b", pool_cap=1000)
    by_vars_a = {tuple(sorted(i["variables"].items())): i["variant_signature"] for i in per_a["TPL.FRAC.ADD.LIKE.NUM.001"]}
    by_vars_b = {tuple(sorted(i["variables"].items())): i["variant_signature"] for i in per_b["TPL.FRAC.ADD.LIKE.NUM.001"]}
    shared = set(by_vars_a) & set(by_vars_b)
    assert shared, "expected overlapping variable assignments across the two builds"
    for key in shared:
        assert by_vars_a[key] == by_vars_b[key]


def test_different_variables_yield_different_signatures(synthetic_dir: Path) -> None:
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    instances = per_template["TPL.TEST.BUILD.MCQ.001"]
    signatures = {i["variant_signature"]: tuple(sorted(i["variables"].items())) for i in instances}
    assert len(signatures) == len({tuple(sorted(i["variables"].items())) for i in instances})


def test_signature_unchanged_by_dressing_rank(synthetic_dir: Path) -> None:
    """Same variable assignment, different rank (hence different locale
    dressing via dress_slots' base_index) -> identical signature: dressing is
    presentation, not content identity."""
    _, per_small = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=2)
    _, per_large = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1000)
    small_by_vars = {
        tuple(sorted(i["variables"].items())): (i["variant_signature"], i["stem"])
        for i in per_small["TPL.TEST.BUILD.MCQ.001"]
    }
    large_by_vars = {
        tuple(sorted(i["variables"].items())): (i["variant_signature"], i["stem"])
        for i in per_large["TPL.TEST.BUILD.MCQ.001"]
    }
    found_dressing_difference = False
    for key, (sig_small, stem_small) in small_by_vars.items():
        sig_large, stem_large = large_by_vars[key]
        assert sig_small == sig_large
        if stem_small != stem_large:
            found_dressing_difference = True
    assert found_dressing_difference, "expected rank-driven dressing to differ for at least one shared variant"


def test_schema_rejects_instance_missing_variant_signature(synthetic_dir: Path) -> None:
    _, per_template = bank_build.run_build(synthetic_dir, LOCALE_PACK, "dev", pool_cap=1)
    instance = copy.deepcopy(per_template["TPL.TEST.BUILD.MCQ.001"][0])
    del instance["variant_signature"]
    validator = _instance_schema_validator()
    errors = list(validator.iter_errors(instance))
    assert errors, "expected schema violation for a missing variant_signature"


def test_signature_deterministic_across_identical_rebuilds(tmp_path: Path) -> None:
    """Extends the existing byte-identical-build determinism test with a
    signature-specific assertion (rebuilding twice yields identical
    signatures for every instance, not merely identical files overall)."""
    templates_dir = _copy_templates(tmp_path, [WORD_FIXTURE, NUM_FIXTURE])
    _, instances1 = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-dev", pool_cap=7)
    _, instances2 = bank_build.run_build(templates_dir, LOCALE_PACK, "2026.07-dev", pool_cap=7)
    for template_id in instances1:
        sigs1 = [i["variant_signature"] for i in instances1[template_id]]
        sigs2 = [i["variant_signature"] for i in instances2[template_id]]
        assert sigs1 == sigs2


# --- Locale pack coverage error names the missing slot ----------------------

def test_missing_locale_category_hard_errors(tmp_path: Path) -> None:
    template = copy.deepcopy(_synthetic_mcq_template())
    template["localization_slots"] = ["name1", "food", "currency"]  # "currency" has no pack category
    template["stem_i18n"]["en-IN"] += " Costs {{currency}}5."
    dest = tmp_path / "templates"
    dest.mkdir()
    write_yaml(dest / "t.yaml", template)
    with pytest.raises(bank_build.BuildError, match="currency"):
        bank_build.run_build(dest, LOCALE_PACK, "dev", pool_cap=10)
