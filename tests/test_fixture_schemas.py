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
from conftest import FIXTURE_DIR, REPO_ROOT, SCHEMAS_DIR

sys.path.insert(0, str(REPO_ROOT))
from orchestrator import constants  # noqa: E402

KIND_TO_SCHEMA = {
    "skills": "skill-node.schema.json",
    "misconceptions": "misconception.schema.json",
    "mappings": "mapping.schema.json",
}


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(FIXTURE_DIR.glob("*.yaml")), ids=lambda p: p.name)
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


def test_schemas_are_valid_json_schema() -> None:
    for schema_path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
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
    data = yaml.safe_load((FIXTURE_DIR / "skills.yaml").read_text(encoding="utf-8"))
    skill_ids = {s["skill_id"] for s in data["skills"]}
    assert skill_ids == {
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

    # Exactly the prerequisite edges of the §1.3 mermaid diagram.
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
    assert actual_edges == expected_edges


def test_fixture_covers_expected_misconceptions() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "misconceptions.yaml").read_text(encoding="utf-8"))
    targets = {m["misconception_id"]: m["remediation_target"] for m in data["misconceptions"]}
    assert targets == {
        "MISC.FRAC.ADD-ACROSS": "FRAC.EQUIV",
        "MISC.FRAC.FIRST-DENOM": "FRAC.ADD.LIKE",
        "MISC.FRAC.NO-SIMPLIFY": "FRAC.SIMPLIFY",
        "MISC.FRAC.BIGGER-DENOM": "FRAC.UNIT",
    }
