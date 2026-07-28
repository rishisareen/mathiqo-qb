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
    # Known, accepted near-duplicate warnings: each pair holds genuinely
    # different skills whose names differ by exactly the distinguishing
    # words (like vs. unlike denominators; fractions vs. mixed numbers vs.
    # whole numbers), not accidental duplicates — the v1 string-similarity
    # check (graph_lint.py's own docstring calls it a stand-in for
    # embedding-based detection) can't tell "differ by exactly the
    # distinguishing word" from "differ by a typo". A human reviewing them
    # (this test) accepts them; anything beyond these pairs is still a
    # real signal.
    assert result.warnings == [
        "near-duplicate skill names (0.98): FRAC.ADD.LIKE "
        "('Add and subtract fractions with like denominators') vs "
        "FRAC.ADD.UNLIKE ('Add and subtract fractions with unlike denominators')",
        "near-duplicate skill names (0.87): FRAC.MIXED.ADD "
        "('Add and subtract mixed numbers') vs "
        "NUM.ADD.SUB ('Add and subtract whole numbers')",
    ]


def test_clean_fixture_cli_exit_zero(capsys) -> None:
    exit_code = graph_lint.main([str(FIXTURE_DIR)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 error(s)" in out


def test_missing_manifest_reports_an_error_naming_the_file(fixture_copy: Path) -> None:
    """KTD9: a content pack without a manifest.yaml must fail lint with an
    error that names the missing file, not silently lint clean without a
    content_version."""
    (fixture_copy / "manifest.yaml").unlink()

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("manifest.yaml" in e and "missing" in e for e in result.errors)


def test_manifest_missing_content_version_field_is_an_error(fixture_copy: Path) -> None:
    manifest_path = fixture_copy / "manifest.yaml"
    write_yaml(manifest_path, {"pack": "fractions"})  # no content_version

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("manifest.yaml" in e and "content_version" in e for e in result.errors)


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
    # The back edge closes more than one loop through FRAC.ADD.UNLIKE's
    # several prerequisite paths; how many distinct member-sets the DFS
    # reports depends on traversal order, so assert on path shape, not count.
    assert cycle_errors
    # Every reported path must be a closed loop naming both back-edge nodes.
    for error in cycle_errors:
        path = error.split(": ", 1)[1].split(" -> ")
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


def test_second_country_mapping_for_same_skill_is_not_a_duplicate(fixture_copy: Path) -> None:
    """A mapping row's identity is (skill, country, curriculum, grade), not
    skill_id alone (core doc §1.4: several rows per skill, one per country
    and one per grade that touches it) — a genuine second-country mapping
    for an already-mapped skill must lint clean."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    assert data["mappings"][0]["skill_id"] == "FRAC.ADD.UNLIKE"  # same skill as the fixture row
    # ZZ/ZZFIXTURE, not a real country/curriculum already in the fixture
    # (content/fractions/mappings.yaml has real IN/NCERT and CA/ONTARIO
    # rows) — a synthetic pair that can never collide with real content.
    data["mappings"].append({
        "skill_id": "FRAC.ADD.UNLIKE",
        "country": "ZZ",
        "curriculum": "ZZFIXTURE",
        "grade": 6,
        "depth_expectation": "medium",
        "syllabus_ref": "Grade 6, Number Sense, fixture",
        "learning_objective": "Add and subtract fractions with unlike denominators.",
    })
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert result.errors == []


def test_duplicate_mapping_same_skill_country_curriculum_grade_is_an_error(fixture_copy: Path) -> None:
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    duplicate = dict(data["mappings"][0])  # identical (skill, country, curriculum, grade)
    duplicate["depth_expectation"] = "hard"
    data["mappings"].append(duplicate)
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("duplicate mapping" in e and "FRAC.ADD.UNLIKE" in e for e in result.errors)


# --- KTD10: structured unit_code/unit_name on mapping rows ---------------------


def test_ncert_row_with_unit_code_loads_and_lints_clean(fixture_copy: Path) -> None:
    """content/fractions/mappings.yaml's FRAC.ADD.UNLIKE/IN/NCERT/6 row was
    migrated (U1) to carry unit_code/unit_name derived from its
    syllabus_ref's "Ch. 7" — it must still lint clean as shipped."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    row = data["mappings"][0]
    assert row["skill_id"] == "FRAC.ADD.UNLIKE"
    assert row["unit_code"] == "IN-NCERT-G6-CH7"
    assert row["unit_name"] == "Class 6, Ch. 7"

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert result.errors == []


def test_duplicate_mapping_with_identical_unit_is_an_error(fixture_copy: Path) -> None:
    """Two rows identical on (skill, country, curriculum, grade, unit_code)
    are a duplicate — the unit code is folded into the dedup key (KTD10)."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    original = data["mappings"][0]
    assert original["unit_code"] == "IN-NCERT-G6-CH7"
    duplicate = dict(original)  # identical (skill, country, curriculum, grade, unit_code)
    duplicate["depth_expectation"] = "hard"
    data["mappings"].append(duplicate)
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("duplicate mapping" in e and "FRAC.ADD.UNLIKE" in e for e in result.errors)


def test_ontario_row_without_unit_fields_still_validates(fixture_copy: Path) -> None:
    """KTD10: unit_code/unit_name are optional — a curriculum with no
    chapter granularity (Ontario) validates fine with neither present."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    ontario_row = next(m for m in data["mappings"] if m["curriculum"] == "ONTARIO")
    assert "unit_code" not in ontario_row
    assert "unit_name" not in ontario_row

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert result.errors == []


def test_unit_code_with_conflicting_unit_name_is_an_error(fixture_copy: Path) -> None:
    """KTD10: two mapping rows (different skills — not a dedup-key
    collision) sharing one unit_code must agree on unit_name, or the
    "chapter list" a consumer builds from structured units becomes
    ambiguous (R2/R3)."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    original = data["mappings"][0]
    assert original["skill_id"] == "FRAC.ADD.UNLIKE"
    assert original["unit_code"] == "IN-NCERT-G6-CH7"
    conflicting = dict(original)
    conflicting["skill_id"] = "FRAC.EQUIV"  # a different skill: no dedup-key collision
    conflicting["unit_name"] = "Class 6, Chapter Seven (renamed)"  # same code, different name
    data["mappings"].append(conflicting)
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any(
        "IN-NCERT-G6-CH7" in e and "inconsistent unit_name" in e for e in result.errors
    )


def test_missing_unit_name_when_unit_code_present_is_a_schema_violation(fixture_copy: Path) -> None:
    """KTD10: unit_code and unit_name must appear together — dropping
    unit_name while keeping unit_code is a schema violation
    (dependentRequired), not silently accepted."""
    mappings_path = fixture_copy / "mappings.yaml"
    data = load_yaml(mappings_path)
    del data["mappings"][0]["unit_name"]
    assert "unit_code" in data["mappings"][0]
    write_yaml(mappings_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any("schema violation" in e and "FRAC.ADD.UNLIKE" in e for e in result.errors)


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


# --- U3 (R18/AE8, KD10): child-facing misconception feedback -------------------


def test_missing_child_feedback_field_fails_lint_naming_the_misconception(fixture_copy: Path) -> None:
    """R18: every misconception must carry child_feedback_i18n — a
    misconception entry missing it must fail lint with an error that names
    the specific misconception, not a generic schema complaint. Mirrors
    test_schema_violation_is_error's shape for the pre-existing required
    fields, applied to the new one."""
    misc_path = fixture_copy / "misconceptions.yaml"
    data = load_yaml(misc_path)
    target = data["misconceptions"][0]
    assert target["misconception_id"] == "MISC.FRAC.ADD-ACROSS"
    del target["child_feedback_i18n"]
    write_yaml(misc_path, data)

    result = graph_lint.lint_directory(fixture_copy, SCHEMAS_DIR)
    assert any(
        "schema violation" in e and "MISC.FRAC.ADD-ACROSS" in e and "child_feedback_i18n" in e
        for e in result.errors
    )


def test_every_served_distractor_misconception_has_child_feedback(bank) -> None:
    """AE8: every misconception_id reachable from any served distractor in a
    real built bank must resolve to child-facing feedback text — no
    distractor can strand a child without instant feedback. This is a bank
    build cross-check (not just a content-lint check): it walks the actual
    item instances tools/bank_build.py produced from content/fractions,
    collects every misconception_id a distractor is tagged with, and
    confirms each one has non-empty child_feedback_i18n in the loaded
    misconception registry."""
    result = graph_lint.LintResult()
    content = graph_lint.load_content(FIXTURE_DIR, result)
    assert result.errors == []
    registry = {
        m["misconception_id"]: m
        for m in content["misconceptions"]
        if "misconception_id" in m
    }

    referenced_misconception_ids = {
        distractor["misconception_id"]
        for instances in bank.instances_by_skill.values()
        for instance in instances
        for distractor in instance["distractors"]
        if distractor.get("misconception_id")
    }
    # Sanity check: the built bank must actually tag distractors with
    # misconceptions, or this test would pass vacuously.
    assert referenced_misconception_ids

    missing_from_registry = referenced_misconception_ids - registry.keys()
    assert not missing_from_registry, (
        f"distractor misconception_ids with no registry entry: {sorted(missing_from_registry)}"
    )

    without_feedback = {
        misc_id
        for misc_id in referenced_misconception_ids
        if not registry[misc_id].get("child_feedback_i18n", {}).get("en-IN", "").strip()
    }
    assert not without_feedback, (
        f"misconceptions served by a bank distractor with no child_feedback_i18n: {sorted(without_feedback)}"
    )
