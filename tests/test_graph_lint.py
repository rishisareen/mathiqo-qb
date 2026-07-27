"""Tests for tools/graph_lint.py against the v1 fractions fixture.

Broken-fixture tests operate on a *copy* of content/fractions in a tmp dir —
the real fixture is never corrupted.
"""

from __future__ import annotations

from pathlib import Path

import graph_lint
from conftest import FIXTURE_DIR, SCHEMAS_DIR, load_yaml, write_yaml


def test_clean_fixture_passes() -> None:
    result = graph_lint.lint_directory(FIXTURE_DIR, SCHEMAS_DIR)
    assert result.errors == []
    # One known, accepted near-duplicate warning: FRAC.ADD.LIKE and
    # FRAC.ADD.UNLIKE are genuinely different skills (like vs. unlike
    # denominators is the actual distinction the graph needs), not an
    # accidental duplicate — the v1 string-similarity check (graph_lint.py's
    # own docstring calls it a stand-in for embedding-based detection) can't
    # tell "differ by exactly the distinguishing word" from "differ by a
    # typo". A human reviewing it (this test) accepts it; anything beyond
    # this one pair is still a real signal.
    assert result.warnings == [
        "near-duplicate skill names (0.98): FRAC.ADD.LIKE "
        "('Add and subtract fractions with like denominators') vs "
        "FRAC.ADD.UNLIKE ('Add and subtract fractions with unlike denominators')"
    ]


def test_clean_fixture_cli_exit_zero(capsys) -> None:
    exit_code = graph_lint.main([str(FIXTURE_DIR)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 error(s)" in out


def test_cycle_detection_reports_cycle_path(fixture_copy: Path) -> None:
    # Add a back edge FRAC.ADD.UNLIKE -> FRAC.UNIT, closing the cycle
    # FRAC.UNIT -> FRAC.PARTWHOLE -> FRAC.ADD.LIKE -> FRAC.ADD.UNLIKE -> FRAC.UNIT
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    for node in data["skills"]:
        if node["skill_id"] == "FRAC.UNIT":
            node["prerequisites"] = ["FRAC.ADD.UNLIKE"]
    write_yaml(skills_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    cycle_errors = [e for e in result.errors if "cycle" in e]
    assert len(cycle_errors) == 1
    # The reported path must be a closed loop naming both nodes of the back edge.
    path = cycle_errors[0].split(": ", 1)[1].split(" -> ")
    assert path[0] == path[-1], "cycle path must start and end at the same node"
    assert {"FRAC.UNIT", "FRAC.ADD.UNLIKE"} <= set(path)


def test_dangling_prerequisite_ref(fixture_copy: Path) -> None:
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    for node in data["skills"]:
        if node["skill_id"] == "FRAC.UNIT":
            node["prerequisites"] = ["FRAC.DOES.NOT.EXIST"]
    write_yaml(skills_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any(
        "FRAC.UNIT" in e and "FRAC.DOES.NOT.EXIST" in e and "prerequisite" in e
        for e in result.errors
    )


def test_dangling_misconception_ref(fixture_copy: Path) -> None:
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    for node in data["skills"]:
        if node["skill_id"] == "FRAC.CMP":
            node["misconceptions"] = ["MISC.FRAC.INVENTED"]
    write_yaml(skills_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any(
        "FRAC.CMP" in e and "MISC.FRAC.INVENTED" in e and "registry" in e
        for e in result.errors
    )


def test_dangling_remediation_target(fixture_copy: Path) -> None:
    misc_path = fixture_copy / "misconceptions.yaml"
    data = load_yaml(misc_path)
    for misc in data["misconceptions"]:
        if misc["misconception_id"] == "MISC.FRAC.NO-SIMPLIFY":
            misc["remediation_target"] = "FRAC.GONE"
    write_yaml(misc_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any(
        "MISC.FRAC.NO-SIMPLIFY" in e and "FRAC.GONE" in e and "remediation" in e
        for e in result.errors
    )


def test_dangling_mapping_skill_id(fixture_copy: Path) -> None:
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    data["mappings"][0]["skill_id"] = "FRAC.UNKNOWN"
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("mapping" in e and "FRAC.UNKNOWN" in e for e in result.errors)


def test_orphan_node_is_warning_not_error(fixture_copy: Path) -> None:
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    data["skills"].append(
        {
            "skill_id": "FRAC.LONELY",
            "name": "Isolated fractions skill",
            "description": "Nothing points at this node and it points at nothing.",
            "strand": "NUMBER.FRACTIONS",
            "canonical_stage": 5,
            "prerequisites": [],
            "misconceptions": [],
        }
    )
    write_yaml(skills_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert result.errors == []
    assert any("FRAC.LONELY" in w and "orphan" in w for w in result.warnings)


def test_near_duplicate_names_flagged(fixture_copy: Path) -> None:
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    data["skills"].append(
        {
            "skill_id": "FRAC.CMP2",
            "name": "Compare and order fraction",  # one char off FRAC.CMP's name
            "description": "Deliberately near-duplicate of FRAC.CMP for the linter test.",
            "strand": "NUMBER.FRACTIONS",
            "canonical_stage": 3,
            "prerequisites": ["FRAC.EQUIV"],
            "misconceptions": [],
        }
    )
    write_yaml(skills_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert result.errors == []
    assert any("FRAC.CMP" in w and "FRAC.CMP2" in w and "near-duplicate" in w for w in result.warnings)


def test_warnings_as_errors_flag(fixture_copy: Path) -> None:
    skills_path = fixture_copy / "skills.yaml"
    data = load_yaml(skills_path)
    data["skills"].append(
        {
            "skill_id": "FRAC.LONELY",
            "name": "Isolated fractions skill",
            "description": "Orphan for the --warnings-as-errors test.",
            "strand": "NUMBER.FRACTIONS",
            "canonical_stage": 5,
            "prerequisites": [],
            "misconceptions": [],
        }
    )
    write_yaml(skills_path, data)

    assert graph_lint.main([str(fixture_copy)]) == 0
    assert graph_lint.main([str(fixture_copy), "--warnings-as-errors"]) == 1


def test_schema_violation_is_error(fixture_copy: Path) -> None:
    misc_path = fixture_copy / "misconceptions.yaml"
    data = load_yaml(misc_path)
    del data["misconceptions"][0]["remediation_target"]
    write_yaml(misc_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("schema violation" in e and "MISC.FRAC.ADD-ACROSS" in e for e in result.errors)
