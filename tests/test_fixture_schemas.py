"""Validate the shipped fixture content against the JSON Schemas directly,
independent of the linter — so schema/content drift fails CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from conftest import FIXTURE_DIR, REPO_ROOT, SCHEMAS_DIR, TEMPLATES_DIR

sys.path.insert(0, str(REPO_ROOT))
from orchestrator import constants  # noqa: E402

KIND_TO_SCHEMA = {
    "skills": "skill-node.schema.json",
    "misconceptions": "misconception.schema.json",
    "mappings": "mapping.schema.json",
}


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(
    params=sorted(p for p in FIXTURE_DIR.glob("*.yaml") if p.name != "manifest.yaml"),
    ids=lambda p: p.name,
)
def content_file(request) -> Path:
    return request.param


def test_fixture_objects_validate(content_file: Path) -> None:
    data = yaml.safe_load(content_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{content_file.name}: top level must be a mapping"
    for kind, objects in data.items():
        schema = _load_schema(KIND_TO_SCHEMA[kind])
        validator = jsonschema.Draft202012Validator(schema)
        for obj in objects:
            errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
            assert errors == [], (
                f"{content_file.name}: {obj.get('skill_id') or obj.get('misconception_id')}: "
                + "; ".join(e.message for e in errors)
            )


@pytest.fixture(
    params=sorted(TEMPLATES_DIR.glob("*.yaml")),
    ids=lambda p: p.name,
)
def template_file(request) -> Path:
    return request.param


def test_template_files_validate_against_item_schema(template_file: Path) -> None:
    """U4: content/templates/*.yaml (the Generator -> Orchestrator item-record
    contract, §5.3) must independently validate against
    schemas/item-record.schema.json, mirroring test_fixture_objects_validate's
    glob-parametrized idiom above but pointed at the template corpus instead
    of content/fractions/ — so schema/content drift in item templates fails
    CI the same way skill/misconception/mapping drift already does."""
    data = yaml.safe_load(template_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{template_file.name}: top level must be a mapping"
    schema = _load_schema("item-record.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    assert errors == [], (
        f"{template_file.name}: " + "; ".join(e.message for e in errors)
    )


def test_schemas_are_valid_json_schema() -> None:
    # U11: schemas/api/ (the MCP tool response schemas, R15) is checked
    # alongside the top-level content schemas — recursive glob covers both.
    for schema_path in sorted(SCHEMAS_DIR.glob("**/*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


def test_question_type_enum_matches_across_item_and_attempt_schemas() -> None:
    """item-record and attempt-event classify question_type with the same
    string values (§9); format-class checks (LOW_GUESS_FORMATS,
    MCQ4_QUESTION_TYPE) match on them exactly, so the two enums must not
    drift apart."""
    item_schema = _load_schema("item-record.schema.json")
    attempt_schema = _load_schema("attempt-event.schema.json")
    item_enum = item_schema["properties"]["question_type"]["enum"]
    attempt_enum = attempt_schema["properties"]["question_type"]["enum"]
    assert set(item_enum) == set(attempt_enum)
    # The format-class constants used for guessing adjustment/certification
    # (student-model doc §3, §7) must also be valid question_type values.
    assert constants.MCQ4_QUESTION_TYPE in item_enum
    assert constants.LOW_GUESS_FORMATS <= set(item_enum)


def test_fixture_covers_expected_fractions_cluster() -> None:
    """The §1.3 mermaid diagram's original 10-node cluster must still be
    present intact — a subset check, not exact equality, since Stage 1
    drafting (tools/stage1_draft.py) has since extended the graph beyond
    this worked example (see test_fixture_covers_stage1_drafted_extension)."""
    data = yaml.safe_load((FIXTURE_DIR / "skills.yaml").read_text(encoding="utf-8"))
    skill_ids = {s["skill_id"] for s in data["skills"]}
    assert skill_ids >= {
        "FRAC.UNIT",
        "FRAC.PARTWHOLE",
        "FRAC.EQUIV",
        "FRAC.CMP",
        "FRAC.SIMPLIFY",
        "FRAC.ADD.LIKE",
        "NUM.LCM",
        "FRAC.ADD.UNLIKE",
        "FRAC.MIXED.ADD",
        "NUM.MULT.TABLES",
    }

    # The prerequisite edges of the §1.3 mermaid diagram, still present.
    expected_edges = {
        ("FRAC.UNIT", "FRAC.PARTWHOLE"),
        ("FRAC.PARTWHOLE", "FRAC.EQUIV"),
        ("NUM.MULT.TABLES", "FRAC.EQUIV"),
        ("NUM.MULT.TABLES", "NUM.LCM"),
        ("FRAC.EQUIV", "FRAC.CMP"),
        ("FRAC.EQUIV", "FRAC.SIMPLIFY"),
        ("FRAC.PARTWHOLE", "FRAC.ADD.LIKE"),
        ("FRAC.ADD.LIKE", "FRAC.ADD.UNLIKE"),
        ("FRAC.EQUIV", "FRAC.ADD.UNLIKE"),
        ("NUM.LCM", "FRAC.ADD.UNLIKE"),
        ("FRAC.SIMPLIFY", "FRAC.ADD.UNLIKE"),
        ("FRAC.ADD.UNLIKE", "FRAC.MIXED.ADD"),
    }
    actual_edges = {
        (prereq, node["skill_id"])
        for node in data["skills"]
        for prereq in node["prerequisites"]
    }
    assert actual_edges >= expected_edges


def test_fixture_covers_stage1_drafted_extension() -> None:
    """The multiplication- and division-of-fractions extensions drafted via
    tools/stage1_draft.py (2026-07-27, examples/ncert-fractions-excerpt.txt)
    and promoted into content/fractions/."""
    skills_data = yaml.safe_load((FIXTURE_DIR / "skills.yaml").read_text(encoding="utf-8"))
    nodes = {s["skill_id"]: s for s in skills_data["skills"]}
    assert nodes["FRAC.MULT.WHOLE"]["prerequisites"] == ["FRAC.PARTWHOLE", "NUM.MULT.TABLES"]
    assert nodes["FRAC.MULT.FRAC"]["prerequisites"] == ["FRAC.MULT.WHOLE"]
    assert nodes["FRAC.DIV.WHOLE"]["prerequisites"] == ["FRAC.MULT.WHOLE"]
    assert nodes["FRAC.DIV.FRAC"]["prerequisites"] == ["FRAC.DIV.WHOLE", "FRAC.MULT.FRAC"]

    misc_data = yaml.safe_load((FIXTURE_DIR / "misconceptions.yaml").read_text(encoding="utf-8"))
    targets = {m["misconception_id"]: m["remediation_target"] for m in misc_data["misconceptions"]}
    assert targets["MISC.FRAC.MULT-WHOLE-BOTH"] == "FRAC.MULT.WHOLE"
    assert targets["MISC.FRAC.MULT-MAKES-BIGGER"] == "FRAC.PARTWHOLE"
    assert targets["MISC.FRAC.EQUIV-ADD-SAME"] == "FRAC.EQUIV"
    assert targets["MISC.FRAC.UNEQUAL-PARTS"] == "FRAC.PARTWHOLE"
    assert targets["MISC.FRAC.DIV-WHOLE-MULTIPLY"] == "FRAC.DIV.WHOLE"
    assert targets["MISC.FRAC.DIV-WRONG-FLIP"] == "FRAC.DIV.FRAC"
    assert targets["MISC.NUM.LCM-CONFUSED-WITH-GCF"] == "NUM.LCM"
    assert targets["MISC.FRAC.MIXED-NO-REGROUP"] == "FRAC.MIXED.ADD"

    # Bidirectional consistency: a misconception diagnosed at its own
    # remediation target must be listed on that skill's own misconceptions
    # (unlike e.g. MISC.FRAC.ADD-ACROSS, diagnosed at FRAC.ADD.UNLIKE but
    # remediated at the prerequisite FRAC.EQUIV — those don't need to match).
    for misc_id, target in [
        ("MISC.FRAC.DIV-WHOLE-MULTIPLY", "FRAC.DIV.WHOLE"),
        ("MISC.FRAC.DIV-WRONG-FLIP", "FRAC.DIV.FRAC"),
        ("MISC.NUM.LCM-CONFUSED-WITH-GCF", "NUM.LCM"),
        ("MISC.FRAC.MIXED-NO-REGROUP", "FRAC.MIXED.ADD"),
    ]:
        assert misc_id in nodes[target]["misconceptions"]


def test_fixture_covers_expected_misconceptions() -> None:
    """The original §2.4 registry table's four entries must still be
    present intact — a subset check, since Stage 1 drafting has since
    extended the registry (see test_fixture_covers_stage1_drafted_extension)."""
    data = yaml.safe_load((FIXTURE_DIR / "misconceptions.yaml").read_text(encoding="utf-8"))
    targets = {m["misconception_id"]: m["remediation_target"] for m in data["misconceptions"]}
    expected = {
        "MISC.FRAC.ADD-ACROSS": "FRAC.EQUIV",
        "MISC.FRAC.FIRST-DENOM": "FRAC.ADD.LIKE",
        "MISC.FRAC.NO-SIMPLIFY": "FRAC.SIMPLIFY",
        "MISC.FRAC.BIGGER-DENOM": "FRAC.UNIT",
    }
    assert targets.items() >= expected.items()

# --- Figure union shape (U2) -----------------------------------------------
# The figure field is the partner-facing render contract: consumers validate
# and codegen against it, so the union has to reject a malformed kind rather
# than pass it through as free-form props the way the removed `render` field
# did.

def _item_record_validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load_schema("item-record.schema.json"))


def _template_with_figure(figure: object) -> dict:
    """A minimal schema-valid template carrying the given figure value."""
    return {
        "template_id": "TPL.TEST.FIGURE.001",
        "template_version": "1.0.0",
        "bank_version": "dev",
        "graph_version": "1.0.0",
        "skill_ids": ["DATA.PICTOGRAPH"],
        "question_type": "mcq_4",
        "cognitive_level": "application",
        "difficulty": 0.4,
        "estimated_time_sec": 30,
        "calculator_allowed": False,
        "diagram_required": True,
        "variables": {"n": {"type": "int", "range": [1, 4]}, "k": {"type": "int", "range": [2, 6]}},
        "constraints": ["n < k"],
        "stem_i18n": {"en-IN": "One symbol stands for {{k}} mangoes. How many do {{n}} symbols show?"},
        "localization_slots": [],
        "answer": "n * k",
        "distractors": [{"expr": "n + k", "misconception_id": None}],
        "solution_steps_i18n": {"en-IN": {"canonical": ["Multiply {{n}} by {{k}}."]}},
        "hints_i18n": {"en-IN": ["Each symbol is worth more than one."]},
        "tts": False,
        "context_tags": ["food"],
        "exam_contexts": ["SCHOOL_EXAM"],
        "provenance": {"generator_version": "test", "reviewer": "test", "date": "2026-08-03"},
        "figure": figure,
    }


def test_well_formed_figure_validates() -> None:
    template = _template_with_figure(
        [
            {
                "kind": "pictograph",
                "key_expr": "k",
                "rows": [{"label_i18n": {"en-IN": "Mangoes"}, "whole_expr": "n", "half": True}],
            }
        ]
    )
    assert list(_item_record_validator().iter_errors(template)) == []


def test_unknown_figure_kind_is_rejected() -> None:
    template = _template_with_figure([{"kind": "hologram", "whatever_expr": "n"}])
    assert list(_item_record_validator().iter_errors(template)) != []


def test_figure_kind_rejects_unknown_property() -> None:
    """Each kind is closed, so a typo'd field fails here instead of reaching a
    partner renderer that silently ignores it."""
    template = _template_with_figure(
        [{"kind": "angle", "degrees_expr": "30*h", "context": "clock", "degrese_expr": "30"}]
    )
    assert list(_item_record_validator().iter_errors(template)) != []


def test_empty_figure_array_is_rejected() -> None:
    """Absence is expressed by omitting the field, never by an empty array --
    otherwise a figure-dependent template could satisfy the coverage guard
    while shipping nothing to draw."""
    assert list(_item_record_validator().iter_errors(_template_with_figure([]))) != []


def test_render_field_is_gone() -> None:
    """The dormant early-grades render field was superseded by figure; a
    template still carrying it must fail rather than validate as a no-op."""
    template = _template_with_figure(
        [{"kind": "angle", "degrees_expr": "90", "context": "bare"}]
    )
    template["render"] = {"component": "number_line"}
    assert list(_item_record_validator().iter_errors(template)) != []
