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


# --- KTD5: figure integrity (R9) ----------------------------------------------

def _figure_template(**overrides) -> dict:
    """A clean pictograph-figure template: one row of `n` whole symbols plus a
    half symbol, each whole symbol worth `2*k` (so the key is always even, as a
    half symbol requires). Answer is the row total, which no label carries."""
    base = {
        "template_id": "TPL.TEST.FIG.PICTOGRAPH.001",
        "template_version": "1.0.0",
        "bank_version": "2026.07-dev",
        "graph_version": "1.0.0",
        "skill_ids": ["DATA.PICTOGRAPH.READ"],
        "question_type": "numeric_entry",
        "cognitive_level": "application",
        "difficulty": 0.3,
        "estimated_time_sec": 45,
        "calculator_allowed": False,
        "diagram_required": True,
        "variables": {
            "n": {"type": "int", "range": [2, 5]},
            "k": {"type": "int", "range": [1, 3]},
        },
        "constraints": ["n >= 2"],
        "stem_i18n": {
            "en-IN": (
                "Each symbol stands for 2 x {{k}} mangoes. The row shows {{n}} whole "
                "symbols and one half symbol. How many mangoes is that?"
            )
        },
        "localization_slots": [],
        "answer": "2*k*n + k",
        "distractors": [{"expr": "2*k*n", "misconception_id": None}],
        "figure": [
            {
                "kind": "pictograph",
                "key_expr": "2*k",
                "unit": "mangoes",
                "rows": [
                    {"label_i18n": {"en-IN": "Mangoes"}, "whole_expr": "n", "half": True}
                ],
            }
        ],
        "solution_steps_i18n": {
            "en-IN": {"canonical": ["Each whole symbol is 2 x {{k}}.", "Add the half symbol."]}
        },
        "hints_i18n": {"en-IN": ["Count the whole symbols first."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {"generator_version": "test", "reviewer": "test", "date": "2026-08-03"},
    }
    base.update(overrides)
    return base


def _numberline_template(label_i18n: dict | None, **overrides) -> dict:
    """A read-the-value number-line template. `label_i18n` is the label on the
    queried point: ``{"en-IN": "{{p}}"}`` leaks the answer, ``{"en-IN": "A"}``
    is the legitimate way to mark it."""
    point: dict = {"position_expr": "p"}
    if label_i18n is not None:
        point["label_i18n"] = label_i18n
    base = _figure_template(
        template_id="TPL.TEST.FIG.NUMBERLINE.001",
        skill_ids=["INT.CONCEPT.POINT"],
        variables={"p": {"type": "int", "range": [1, 5]}},
        constraints=["p >= 1"],
        stem_i18n={"en-IN": "What number does the marked point on the number line show?"},
        answer="p",
        distractors=[{"expr": "p + 1", "misconception_id": None}],
        figure=[
            {
                "kind": "number-line",
                "min_expr": "0",
                "max_expr": "6",
                "divisions_expr": "1",
                "points": [point],
            }
        ],
        solution_steps_i18n={"en-IN": {"canonical": ["Read the tick the point sits on."]}},
        hints_i18n={"en-IN": ["Count the ticks from zero."]},
    )
    base.update(overrides)
    return base


def test_well_formed_figure_passes_full_enumeration(tmp_path: Path) -> None:
    """Happy path: a consistent pictograph figure survives every variant."""
    template = _figure_template()
    report = item_verify.verify_template(template)
    assert report.total_combos == 12  # n in [2,5] x k in [1,3]
    assert report.valid_combos == 12
    assert report.dropped_variants == 0
    assert report.verdict == item_verify.PASS
    assert _verify_tmp(tmp_path, template) == 0


def test_figure_expression_over_undeclared_variable_hard_fails(tmp_path: Path) -> None:
    """Caught by the pre-parse pass, before deep enumeration."""
    template = _figure_template()
    template["figure"][0]["key_expr"] = "2*z"
    with pytest.raises(item_verify.HardFail, match="undeclared"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_figure_expression_unevaluable_at_one_variant_hard_fails(tmp_path: Path) -> None:
    """A whole count of n/(4 - n) symbols: 1 at n = 2 and 3 at n = 3, but a
    division by zero at the enumerated n = 4."""
    template = _figure_template()
    template["figure"][0]["rows"][0]["whole_expr"] = "n/(4 - n)"
    assert item_verify.evaluate_expression("n/(4 - n)", {"n": 2, "k": 1}) == 1
    with pytest.raises(item_verify.HardFail, match="exact rational"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_figure_label_on_queried_tick_leaks_the_answer(tmp_path: Path) -> None:
    """The general leak rule: a label that renders to the variant's answer."""
    leaking = _numberline_template({"en-IN": "{{p}}"})
    with pytest.raises(item_verify.HardFail, match="leaks the answer"):
        item_verify.verify_template(leaking)
    assert _verify_tmp(tmp_path, leaking) == 1


def test_non_answer_point_label_is_allowed(tmp_path: Path) -> None:
    """Marking the queried point with a name, not its value, is fine — and an
    unlabelled point is fine too."""
    named = _numberline_template({"en-IN": "A"})
    assert item_verify.verify_template(named).verdict == item_verify.PASS
    assert _verify_tmp(tmp_path, named) == 0
    assert item_verify.verify_template(_numberline_template(None)).verdict == item_verify.PASS


def test_figure_label_missing_a_stem_language_hard_fails(tmp_path: Path) -> None:
    template = _figure_template()
    template["stem_i18n"]["hi-IN"] = "Kitne aam hain?"
    with pytest.raises(item_verify.HardFail, match="hi-IN"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_figure_label_with_undeclared_slot_hard_fails(tmp_path: Path) -> None:
    template = _figure_template()
    template["figure"][0]["rows"][0]["label_i18n"]["en-IN"] = "{{wizard}} mangoes"
    with pytest.raises(item_verify.HardFail, match="wizard"):
        item_verify.verify_template(template)
    assert _verify_tmp(tmp_path, template) == 1


def test_figure_violation_hard_fails_rather_than_flagging(tmp_path: Path) -> None:
    """A FLAG still builds (tools/bank_build.py warns and continues), so a
    figure-inconsistent template must hard-fail: exit 1, never 2, and
    verify_template must raise instead of returning a FLAGGED report."""
    leaking = _numberline_template({"en-IN": "{{p}}"})
    rc = _verify_tmp(tmp_path, leaking)
    assert rc == item_verify.EXIT_CODES[item_verify.HARD_FAIL] == 1
    assert rc != item_verify.EXIT_CODES[item_verify.FLAGGED]
    with pytest.raises(item_verify.HardFail):
        item_verify.verify_template(leaking)


@pytest.mark.parametrize(
    "figure, match",
    [
        pytest.param(
            {
                "kind": "pictograph",
                "key_expr": "2*k + 1",
                "rows": [{"label_i18n": {"en-IN": "Mangoes"}, "whole_expr": "n", "half": True}],
            },
            "even key",
            id="half-symbol-with-odd-key",
        ),
        pytest.param(
            {
                "kind": "region",
                "shape": "bar",
                "parts": [
                    {"weight_expr": "1", "shaded": True},
                    {"weight_expr": "k", "shaded": True},
                ],
            },
            "one shaded and one unshaded",
            id="region-with-nothing-unshaded",
        ),
        pytest.param(
            {
                "kind": "grid-region",
                "cols_expr": "n",
                "rows_expr": "n",
                "fill": "block-minus-corner",
                "cutout_cols_expr": "n",
                "cutout_rows_expr": "1",
            },
            "strictly smaller",
            id="grid-cutout-as-wide-as-the-block",
        ),
        pytest.param(
            {
                "kind": "ruler",
                "min_expr": "n",
                "max_expr": "n",
                "major_expr": "1",
                "minor_expr": "1",
                "unit": "cm",
                "objects": [{"start_expr": "n", "end_expr": "n"}],
            },
            "less than",
            id="ruler-with-empty-range",
        ),
        pytest.param(
            {
                "kind": "ruler",
                "min_expr": "0",
                "max_expr": "10",
                "major_expr": "1",
                "minor_expr": "1",
                "unit": "cm",
                "objects": [{"start_expr": "2", "end_expr": "2 + n*10"}],
            },
            "within the ruler",
            id="ruler-object-running-off-the-scale",
        ),
    ],
)
def test_per_kind_figure_rules_hard_fail(figure: dict, match: str) -> None:
    template = _figure_template(figure=[figure])
    with pytest.raises(item_verify.HardFail, match=match):
        item_verify.verify_template(template)


def test_array_per_row_expression_binds_the_row_index() -> None:
    """per_row_expr is the one field evaluated over a symbol that is not a
    declared variable: "i", the one-based row index (a triangular pile)."""
    triangular = _figure_template(
        template_id="TPL.TEST.FIG.ARRAY.001",
        answer="n*(n + 1)/2",
        distractors=[{"expr": "n*n", "misconception_id": None}],
        figure=[{"kind": "array", "rows_expr": "n", "per_row_expr": "i", "icon": "dot"}],
    )
    assert item_verify.verify_template(triangular).verdict == item_verify.PASS

    # "i" is bound, not declared — an actually undeclared symbol still fails.
    empty_rows = _figure_template(
        figure=[{"kind": "array", "rows_expr": "n", "per_row_expr": "i - 1"}],
    )
    with pytest.raises(item_verify.HardFail, match="positive whole number of marks"):
        item_verify.verify_template(empty_rows)
    with pytest.raises(item_verify.HardFail, match="undeclared"):
        item_verify.verify_template(
            _figure_template(figure=[{"kind": "array", "rows_expr": "n", "per_row_expr": "j"}])
        )


def test_unregistered_figure_kind_hard_fails() -> None:
    """Fail closed: a kind with no registered rules never slips through."""
    template = _figure_template(figure=[{"kind": "hologram", "rows_expr": "n"}])
    with pytest.raises(item_verify.HardFail, match="no figure rules are registered"):
        item_verify.verify_template(template)
