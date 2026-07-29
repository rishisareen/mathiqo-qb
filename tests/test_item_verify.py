"""Tests for tools/item_verify.py — the Stage 3 verification gate (§3).

Breakage tests operate on deep-copied template dicts written to tmp dirs;
the real fixture at content/templates/ is never modified.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import sympy
from conftest import REPO_ROOT, SCHEMAS_DIR, load_yaml, write_yaml

import item_verify

FIXTURE = REPO_ROOT / "content" / "templates" / "TPL.FRAC.ADD.UNLIKE.WORD.001.yaml"


def _fixture_dict() -> dict:
    return copy.deepcopy(load_yaml(FIXTURE))


def _verify_tmp(tmp_path: Path, template: dict) -> int:
    """Write a (possibly broken) template copy to tmp and run the CLI on it."""
    path = tmp_path / "template.yaml"
    write_yaml(path, template)
    return item_verify.main([str(path)])


# --- The §2.2 fixture -------------------------------------------------------

def test_fixture_validates_against_item_record_schema() -> None:
    schema = json.loads((SCHEMAS_DIR / "item-record.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(load_yaml(FIXTURE)))
    assert errors == [], "; ".join(e.message for e in errors)


def test_fixture_enumeration_counts() -> None:
    """Hand-computed expectations for the §2.2 domains and 5 constraints.

    Domains a,c ∈ [1,8], b,d ∈ [3,12] give 8·10·8·10 = 6400 combos.
    Constraints 3–4 (b != d, gcd(b,d) == 1) depend only on (b, d); over the
    coprime (b, d) pairs, constraints 1, 2 and 5 leave exactly 1148 valid
    (a, c) assignments (a/b + c/d < 1 with a,b,c,d positive implies a < b
    and c < d), so 5252 are rejected.
    """
    report = item_verify.verify_template(load_yaml(FIXTURE))
    assert report.total_combos == 6400
    assert report.valid_combos == 1148
    assert report.rejected_combos == 5252
    # No distractor ever collides for this template: (a+c)/(b+d) and (a+c)/b
    # differ from the answer by a*d² + c*b² > 0 and c*(d-b) != 0 respectively,
    # and the NO-SIMPLIFY distractor's unreduced displayed form only coincides
    # with the answer when its only_if guard excludes it.
    assert report.dropped_variants == 0
    assert report.collision_rate == 0.0


def test_fixture_verdict_is_flagged_by_rejection_rate() -> None:
    """NOTE: the doc's own §2.2 example violates the doc's >30% rule.

    The 5 constraints reject 5252/6400 = 82.06% of the Cartesian product, so
    a spec-faithful Stage 3 gate MUST flag this template for regeneration —
    PASS was expected when this test was commissioned, but the enumeration
    says otherwise. If the threshold or the fixture constraints change,
    update this test.
    """
    report = item_verify.verify_template(load_yaml(FIXTURE))
    assert report.rejection_rate == pytest.approx(0.820625)
    assert report.verdict == item_verify.FLAGGED
    assert item_verify.main([str(FIXTURE)]) == 2


# --- Rational correctness ----------------------------------------------------

def test_arithmetic_is_exact_rational() -> None:
    assert item_verify.evaluate_expression("1/2 + 1/3", {}) == sympy.Rational(5, 6)


def test_answer_and_distractor_are_exact_rationals() -> None:
    template = load_yaml(FIXTURE)
    values = {"a": 1, "b": 2, "c": 1, "d": 3}
    answer = item_verify.evaluate_expression(template["answer"], values)
    assert answer == sympy.Rational(5, 6)
    assert answer.is_Rational
    add_across = item_verify.evaluate_expression(template["distractors"][0]["expr"], values)
    assert add_across == sympy.Rational(2, 5)
    assert add_across != answer


# --- only_if guards ----------------------------------------------------------

def test_only_if_guards_no_simplify_distractor() -> None:
    template = load_yaml(FIXTURE)
    no_simplify = template["distractors"][2]
    assert no_simplify["only_if"] == "gcd(a*d + c*b, b*d) > 1"
    # 1/2 + 1/3 = 5/6, gcd(5, 6) == 1 → already simplest, distractor NOT offered.
    assert not item_verify.distractor_offered(no_simplify, {"a": 1, "b": 2, "c": 1, "d": 3})
    # 2/4 + 1/3 = 10/12, gcd(10, 12) == 2 > 1 → unsimplified form offered.
    assert item_verify.distractor_offered(no_simplify, {"a": 2, "b": 4, "c": 1, "d": 3})
    # Unguarded distractors are always offered.
    assert item_verify.distractor_offered(template["distractors"][0], {"a": 1, "b": 2, "c": 1, "d": 3})


def test_offered_no_simplify_distractor_never_collides_with_answer() -> None:
    """Whenever only_if fires, the unreduced displayed form differs from the
    simplified answer, so the fixture drops zero variants via NO-SIMPLIFY."""
    template = load_yaml(FIXTURE)
    # gcd(10, 12) == 2 > 1: offered, displayed as 10/12 while answer shows 5/6.
    assert item_verify.check_variant(template, {"a": 2, "b": 4, "c": 1, "d": 3}) == []


def test_unsimplified_non_quotient_expression_hard_fails() -> None:
    """sympy.fraction() only splits a single num/den quotient correctly; on a
    sum of two fractions it would silently return the wrong split (whole
    expression over 1) rather than raising — the cross-check against the
    plain evaluated value must catch that instead of shipping a fabricated
    displayed form."""
    with pytest.raises(item_verify.HardFail, match="does not match its evaluated value"):
        item_verify._unsimplified_key("a/b + c/d", {"a": 1, "b": 2, "c": 1, "d": 3})


# --- Hard failures -----------------------------------------------------------

def test_unsatisfiable_constraints_hard_fail(tmp_path: Path) -> None:
    template = _fixture_dict()
    template["constraints"] = ["b > 100"]  # b ranges over [3, 12]
    with pytest.raises(item_verify.HardFail, match="unsatisfiable"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_undeclared_stem_slot_hard_fail(tmp_path: Path) -> None:
    template = _fixture_dict()
    template["stem_i18n"]["en-IN"] += " P.S. {{undeclared_slot}}."
    with pytest.raises(item_verify.HardFail, match="undeclared_slot"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_unresolvable_solution_step_slot_hard_fail(tmp_path: Path) -> None:
    template = _fixture_dict()
    template["solution_steps_i18n"]["en-IN"]["canonical"].append("Magic: {{wizard}}.")
    with pytest.raises(item_verify.HardFail, match="wizard"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


# --- Regeneration flags ------------------------------------------------------

def test_high_constraint_rejection_flagged(tmp_path: Path) -> None:
    template = _fixture_dict()
    # a, c ∈ [1,8]: a >= 6 and c >= 6 keep 3/8 · 3/8 ≈ 14% of combos → ~86% rejected.
    template["constraints"] = ["a >= 6", "c >= 6"]
    report = item_verify.verify_template(template)
    assert report.rejection_rate > 0.30
    assert report.verdict == item_verify.FLAGGED
    assert _verify_tmp(tmp_path, template) == 2


def test_always_colliding_distractor_flagged(tmp_path: Path) -> None:
    template = _fixture_dict()
    # A distractor whose expr is identical to the answer collides on every variant.
    template["distractors"][0]["expr"] = template["answer"]
    report = item_verify.verify_template(template)
    assert report.dropped_variants == report.valid_combos
    assert report.collision_rate == 1.0
    assert report.drops_by_distractor["MISC.FRAC.ADD-ACROSS"] == report.valid_combos
    assert report.verdict == item_verify.FLAGGED
    assert _verify_tmp(tmp_path, template) == 2


# --- Parse cache -------------------------------------------------------------

def test_preparse_cache_key_matches_enumeration_when_vars_declared_unsorted() -> None:
    """verify_template's warm-up parse pass must key by the same (sorted)
    variable-name tuple that evaluate_expression uses during enumeration —
    otherwise every expression is parsed twice (once per differently-ordered
    cache key) whenever variables aren't declared in alphabetical order."""
    template = {
        "template_id": "TPL.TEST.CACHE.001",
        "template_version": "1.0.0",
        "bank_version": "2026.07-dev",
        "graph_version": "1.0.0",
        "skill_ids": ["FRAC.ADD.UNLIKE"],
        "question_type": "numeric_entry",
        "cognitive_level": "application",
        "difficulty": 0.2,
        "estimated_time_sec": 30,
        "calculator_allowed": False,
        "diagram_required": False,
        # Declared out of alphabetical order on purpose.
        "variables": {
            "d": {"type": "int", "range": [1, 2]},
            "b": {"type": "int", "range": [1, 2]},
            "a": {"type": "int", "range": [1, 2]},
        },
        "constraints": ["a + b + d <= 100"],
        "stem_i18n": {"en-IN": "What is {{a}} + {{b}} + {{d}}?"},
        "localization_slots": [],
        "answer": "a + b + d",
        "distractors": [{"expr": "a * b * d", "misconception_id": None}],
        "solution_steps_i18n": {"en-IN": {"canonical": ["Add them up."]}},
        "hints_i18n": {"en-IN": ["Add."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {"generator_version": "test", "reviewer": "test", "date": "2026-07-26"},
    }
    item_verify._parse.cache_clear()
    item_verify.verify_template(template)
    info = item_verify._parse.cache_info()
    # 2 expressions (the answer "a + b + d" and constraint "a + b + d <= 100"
    # share no cache entries with each other, but each is parsed exactly
    # once overall) + the distractor expr: 3 distinct expressions total, so
    # at most 3 misses if the warm-up pass and enumeration share cache keys.
    # With the bug, the mismatched key doubled every warm-up parse to a miss.
    assert info.misses <= 3, f"expected warm-up parses to hit the cache, got {info}"


# --- A genuinely clean template PASSes ---------------------------------------

def test_clean_template_passes(tmp_path: Path) -> None:
    template = {
        "template_id": "TPL.TEST.ADD.SMALL.001",
        "template_version": "1.0.0",
        "bank_version": "2026.07-dev",
        "graph_version": "1.0.0",
        "skill_ids": ["FRAC.ADD.UNLIKE"],
        "question_type": "numeric_entry",
        "cognitive_level": "application",
        "difficulty": 0.2,
        "estimated_time_sec": 30,
        "calculator_allowed": False,
        "diagram_required": False,
        "variables": {
            "x": {"type": "int", "range": [1, 4]},
            "y": {"type": "int", "range": [1, 4]},
        },
        "constraints": ["x + y <= 8"],  # always true over the domains
        "stem_i18n": {"en-IN": "What is {{x}} + {{y}}?"},
        "localization_slots": [],
        "answer": "x + y",
        "distractors": [{"expr": "x * y", "misconception_id": None}],
        "solution_steps_i18n": {"en-IN": {"canonical": ["Add {{x}} and {{y}}."]}},
        "hints_i18n": {"en-IN": ["Count up from {{x}}."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {
            "generator_version": "test",
            "reviewer": "test",
            "date": "2026-07-26",
        },
    }
    report = item_verify.verify_template(template)
    assert report.total_combos == 16
    assert report.valid_combos == 16
    # x*y == x+y only at (2, 2): 1 of 16 variants dropped — under the 30% bar.
    assert report.dropped_variants == 1
    assert report.verdict == item_verify.PASS
    assert _verify_tmp(tmp_path, template) == 0


# --- KTD2: check_embedding_duplicates -----------------------------------------

def _dup_template(**overrides) -> dict:
    """A small, exact synthetic template for near-duplicate-detection
    assertions — same "clean template" convention as the fixtures above."""
    base = {
        "template_id": "TPL.TEST.DUP.A.001",
        "template_version": "1.0.0",
        "bank_version": "2026.07-dev",
        "graph_version": "1.0.0",
        "skill_ids": ["FRAC.ADD.UNLIKE"],
        "question_type": "numeric_entry",
        "cognitive_level": "application",
        "difficulty": 0.2,
        "estimated_time_sec": 30,
        "calculator_allowed": False,
        "diagram_required": False,
        "variables": {
            "a": {"type": "int", "range": [1, 3]},
            "b": {"type": "int", "range": [4, 6]},
            "c": {"type": "int", "range": [1, 3]},
        },
        "constraints": ["a < b", "c < b"],
        "stem_i18n": {"en-IN": "What is {{a}}/{{b}} plus {{c}}/{{b}}?"},
        "localization_slots": [],
        "answer": "simplify((a + c) / b)",
        "distractors": [{"expr": "a * c", "misconception_id": None}],
        "solution_steps_i18n": {"en-IN": {"canonical": ["Add {{a}} and {{c}}.", "Result over {{b}}."]}},
        "hints_i18n": {"en-IN": ["Same denominator already."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {"generator_version": "test", "reviewer": "test", "date": "2026-07-29"},
    }
    base.update(overrides)
    return base


def _dup_sibling(**overrides) -> dict:
    """Same shape as _dup_template but with variable names x/y/z instead of
    a/b/c — used to prove near-duplicate detection is variable-name
    invariant (KTD2's own example: "{{a}}/{{b}} + {{c}}/{{b}}" and
    "{{x}}/{{y}} + {{z}}/{{y}}" compare as identical shape)."""
    base = _dup_template(
        template_id="TPL.TEST.DUP.B.001",
        variables={
            "x": {"type": "int", "range": [1, 3]},
            "y": {"type": "int", "range": [4, 6]},
            "z": {"type": "int", "range": [1, 3]},
        },
        constraints=["x < y", "z < y"],
        stem_i18n={"en-IN": "What is {{x}}/{{y}} plus {{z}}/{{y}}?"},
        answer="simplify((x + z) / y)",
        distractors=[{"expr": "x * z", "misconception_id": None}],
    )
    base.update(overrides)
    return base


def test_check_embedding_duplicates_flags_near_duplicate_across_renamed_variables() -> None:
    template = _dup_template()
    bank_entry = _dup_sibling()
    findings = item_verify.check_embedding_duplicates(template, [bank_entry])
    assert findings
    assert "TPL.TEST.DUP.A.001" in findings[0]
    assert "TPL.TEST.DUP.B.001" in findings[0]


def test_check_embedding_duplicates_does_not_flag_tier_siblings() -> None:
    """Same skill, same answer formula, genuinely distinct stem wording (a
    direct-computation stem vs a word-problem stem) — a tier sibling, not a
    duplicate, must not be flagged."""
    template = _dup_template()
    tier_sibling = _dup_template(
        template_id="TPL.TEST.DUP.WORD.001",
        difficulty=0.5,
        localization_slots=["name1"],
        stem_i18n={
            "en-IN": (
                "{{name1}} ate {{a}}/{{b}} of a pizza and later ate {{c}}/{{b}} "
                "more of the same pizza. What fraction of the pizza did "
                "{{name1}} eat in total?"
            )
        },
    )
    assert item_verify.check_embedding_duplicates(template, [tier_sibling]) == []


def test_check_embedding_duplicates_flags_despite_different_skill_ids() -> None:
    """Identical stem shape and matching answer under a *different*
    skill_id is still flagged — catches mis-tagged copies rather than being
    waved through for lacking skill_ids overlap."""
    template = _dup_template()
    mistagged = _dup_sibling(
        template_id="TPL.TEST.DUP.MISTAG.001",
        skill_ids=["FRAC.SUB.UNLIKE"],
    )
    findings = item_verify.check_embedding_duplicates(template, [mistagged])
    assert findings
    assert "TPL.TEST.DUP.MISTAG.001" in findings[0]
    assert "different skill_ids" in findings[0].lower()


def test_check_embedding_duplicates_no_bank_returns_empty() -> None:
    assert item_verify.check_embedding_duplicates(_dup_template(), []) == []


def test_verify_template_without_corpus_skips_check_back_compat() -> None:
    """No corpus passed (the default, every pre-KTD2 caller) — behavior is
    byte-identical to before: embedding_duplicates stays in skipped_checks
    and the verdict is unaffected by anything that would otherwise dedup."""
    report = item_verify.verify_template(_dup_template())
    assert "embedding_duplicates" in report.skipped_checks
    assert "language_level_vs_grade" in report.skipped_checks
    assert report.verdict == item_verify.PASS
    assert report.reasons == []


def test_verify_template_with_corpus_flags_near_duplicate(tmp_path: Path) -> None:
    template = _dup_template()
    bank_entry = _dup_sibling()
    report = item_verify.verify_template(template, [bank_entry])
    assert "embedding_duplicates" not in report.skipped_checks
    assert report.verdict == item_verify.FLAGGED
    assert any("TPL.TEST.DUP.A.001" in r and "TPL.TEST.DUP.B.001" in r for r in report.reasons)

    # Full CLI pipeline: exit code 2, both ids named in the printed report.
    write_yaml(tmp_path / "TPL.TEST.DUP.A.001.yaml", template)
    write_yaml(tmp_path / "TPL.TEST.DUP.B.001.yaml", bank_entry)
    report_path = tmp_path / "report.json"
    rc = item_verify.main([
        str(tmp_path / "TPL.TEST.DUP.A.001.yaml"),
        "--corpus", str(tmp_path),
        "--report", str(report_path),
    ])
    assert rc == 2
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["verdict"] == item_verify.FLAGGED
    assert any("TPL.TEST.DUP.B.001" in r for r in payload["reasons"])


def test_verify_template_with_corpus_does_not_flag_distinct_templates(tmp_path: Path) -> None:
    """A --corpus dir with only genuinely distinct content produces no
    embedding_duplicates finding (no false positive)."""
    template = _dup_template()
    distinct = _dup_template(
        template_id="TPL.TEST.DUP.UNRELATED.001",
        stem_i18n={"en-IN": "How many quarters are in {{a}} whole pizzas?"},
        answer="a * 4",
        constraints=[],
        distractors=[{"expr": "a / 4", "misconception_id": None}],
    )
    write_yaml(tmp_path / "TPL.TEST.DUP.A.001.yaml", template)
    write_yaml(tmp_path / "TPL.TEST.DUP.UNRELATED.001.yaml", distinct)
    rc = item_verify.main([str(tmp_path / "TPL.TEST.DUP.A.001.yaml"), "--corpus", str(tmp_path)])
    assert rc == 0
