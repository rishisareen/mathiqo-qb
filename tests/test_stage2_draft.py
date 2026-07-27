"""Tests for tools/stage2_draft.py — the Stage 2 template-drafting harness.

Mirrors tests/test_stage1_draft.py's structure: a fake LLM (tests/fixtures/
fake_llm.py, driven via the cmd backend) serves canned YAML responses from
scenario directories under tests/fixtures/stage2/ and records every prompt
it receives. The real content/fractions and content/templates fixtures are
used read-only; an autouse fixture asserts content/templates is
byte-identical after every test (stage2_draft.py must never write there).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURE_DIR, REPO_ROOT, SCHEMAS_DIR, load_yaml

sys.path.insert(0, str(REPO_ROOT / "tools"))
import item_verify  # noqa: E402
import stage2_draft  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FAKE_LLM = FIXTURES_DIR / "fake_llm.py"
SCENARIOS_DIR = FIXTURES_DIR / "stage2"
TEMPLATES_DIR = REPO_ROOT / "content" / "templates"


def snapshot_templates() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(TEMPLATES_DIR.glob("*.yaml"))}


@pytest.fixture(autouse=True)
def templates_untouched():
    """The driver must never write into content/templates/ — verify byte-for-byte."""
    before = snapshot_templates()
    yield
    assert snapshot_templates() == before, "content/templates/ was modified by the driver"


def run_driver(tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[int, Path, Path]:
    """Run the driver against a canned scenario; returns (exit code, staging dir, scenario copy)."""
    scenario_copy = tmp_path / scenario
    shutil.copytree(SCENARIOS_DIR / scenario, scenario_copy)
    monkeypatch.setenv("STAGE2_LLM_CMD", f"{sys.executable} {FAKE_LLM} {scenario_copy}")
    staging = tmp_path / "staging"
    rc = stage2_draft.main([
        "--skill", "FRAC.ADD.UNLIKE",
        "--content-dir", str(FIXTURE_DIR),
        "--templates-dir", str(TEMPLATES_DIR),
        "--staging-dir", str(staging),
        "--reviewer", "test-reviewer",
        "--backend", "cmd",
        *extra,
    ])
    return rc, staging, scenario_copy


def read_manifest(staging: Path, slot: int) -> dict:
    return json.loads((staging / "accepted" / f"template-{slot}.manifest.json").read_text(encoding="utf-8"))


# --- End-to-end: clean accept -------------------------------------------------

def test_clean_draft_accepted_on_first_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "clean", monkeypatch)

    assert rc == 0
    template_yaml = staging / "accepted" / "template-1.yaml"
    assert template_yaml.is_file()
    data = load_yaml(template_yaml)
    assert data["template_id"] == "TPL.FRAC.ADD.UNLIKE.CMP.001"
    assert data["skill_ids"] == ["FRAC.ADD.UNLIKE"]
    # Scaffolding fields the harness fills in, not the LLM.
    assert data["template_version"] == "1.0.0"
    assert data["bank_version"] == "dev"
    assert data["provenance"]["reviewer"] == "test-reviewer"
    assert data["difficulty"] == pytest.approx(stage2_draft.DIFFICULTY_BAND_MIDPOINT["easy"])

    manifest = read_manifest(staging, 1)
    assert manifest["retries_used"] == 0
    assert manifest["template_id"] == "TPL.FRAC.ADD.UNLIKE.CMP.001"
    assert manifest["verify_report"]["verdict"] == item_verify.PASS

    prompt_1 = (scenario / "received-prompt-1.md").read_text(encoding="utf-8")
    assert "Previous draft failed" not in prompt_1
    assert "FRAC.ADD.UNLIKE" in prompt_1
    assert "MISC.FRAC.ADD-ACROSS" in prompt_1  # this skill's registered misconceptions inlined


def test_accepted_yaml_validates_against_item_record_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _rc, staging, _scenario = run_driver(tmp_path, "clean", monkeypatch)
    data = load_yaml(staging / "accepted" / "template-1.yaml")
    schema = json.loads((SCHEMAS_DIR / "item-record.schema.json").read_text(encoding="utf-8"))
    import jsonschema
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(data))
    assert errors == [], "; ".join(e.message for e in errors)


# --- End-to-end: bounce then accept -------------------------------------------

def test_hard_fail_bounces_back_with_error_then_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "hard_fail_then_clean", monkeypatch)

    assert rc == 0
    prompt_2 = (scenario / "received-prompt-2.md").read_text(encoding="utf-8")
    assert "Previous draft failed these checks" in prompt_2
    assert "estimated_time_sec" in prompt_2  # the actual schema violation, fed back verbatim

    manifest = read_manifest(staging, 1)
    assert manifest["retries_used"] == 1


def test_flagged_bounces_back_with_rates_then_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "flagged_then_clean", monkeypatch)

    assert rc == 0
    prompt_2 = (scenario / "received-prompt-2.md").read_text(encoding="utf-8")
    assert "Previous draft failed these checks" in prompt_2
    # FLAGGED feedback carries the actual rate, not just a verdict string.
    assert "constraint-rejection rate" in prompt_2
    assert "82.0" in prompt_2

    manifest = read_manifest(staging, 1)
    assert manifest["retries_used"] == 1
    assert manifest["template_id"] == "TPL.FRAC.ADD.UNLIKE.CMP.001"


# --- End-to-end: persistent failure quarantined -------------------------------

def test_persistent_failure_quarantined_after_max_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "persistent", monkeypatch, "--max-retries", "2")

    assert rc == 1
    assert (scenario / "received-prompt-3.md").is_file()
    assert not (scenario / "received-prompt-4.md").exists()

    rejected_yaml = staging / "rejected" / "template-1.yaml"
    assert rejected_yaml.is_file()
    report = json.loads((staging / "rejected" / "template-1.errors.json").read_text(encoding="utf-8"))
    assert report["attempts"] == 3
    assert any("estimated_time_sec" in e for e in report["errors"])
    assert not (staging / "accepted").exists()


# --- print-prompts ------------------------------------------------------------

def test_print_prompts_writes_prompts_and_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    monkeypatch.delenv("STAGE2_LLM_CMD", raising=False)
    rc = stage2_draft.main([
        "--skill", "FRAC.ADD.UNLIKE",
        "--content-dir", str(FIXTURE_DIR),
        "--templates-dir", str(TEMPLATES_DIR),
        "--staging-dir", str(staging),
        "--reviewer", "test-reviewer",
        "--backend", "file",
        "--count", "2",
        "--print-prompts",
    ])
    assert rc == 0
    prompt_1 = (staging / "prompt-1.md").read_text(encoding="utf-8")
    prompt_2 = (staging / "prompt-2.md").read_text(encoding="utf-8")
    assert "FRAC.ADD.UNLIKE" in prompt_1
    assert "TPL.FRAC.ADD.UNLIKE.WORD.001" in prompt_1  # existing template id inlined
    assert prompt_1 == prompt_2  # no staged ids yet at print time, both slots identical
    assert (staging / "prompt-1.md.sha256").is_file()
    assert not (staging / "accepted").exists()
    assert not (staging / "rejected").exists()


def test_print_prompts_then_fill_then_resume_consumes_response_one(tmp_path: Path) -> None:
    """Same resume guarantee as stage1_draft.py's FileBackend (reused here
    directly): filling response-1.yaml after --print-prompts and resuming
    must consume it for slot 1, not skip to asking for response-2.yaml."""
    staging = tmp_path / "staging"
    rc = stage2_draft.main([
        "--skill", "FRAC.ADD.UNLIKE",
        "--content-dir", str(FIXTURE_DIR),
        "--templates-dir", str(TEMPLATES_DIR),
        "--staging-dir", str(staging),
        "--reviewer", "test-reviewer",
        "--backend", "file",
        "--count", "2",
        "--print-prompts",
    ])
    assert rc == 0

    shutil.copy(SCENARIOS_DIR / "clean" / "01.yaml", staging / "response-1.yaml")

    with pytest.raises(SystemExit, match="response-2.yaml"):
        stage2_draft.main([
            "--skill", "FRAC.ADD.UNLIKE",
            "--content-dir", str(FIXTURE_DIR),
            "--templates-dir", str(TEMPLATES_DIR),
            "--staging-dir", str(staging),
            "--reviewer", "test-reviewer",
            "--backend", "file",
            "--count", "2",
        ])

    template_yaml = staging / "accepted" / "template-1.yaml"
    assert template_yaml.is_file(), "response-1.yaml (slot 1's draft) was never consumed"
    assert load_yaml(template_yaml)["template_id"] == "TPL.FRAC.ADD.UNLIKE.CMP.001"
    # Slot 2's regenerated prompt must know about slot 1's now-accepted id.
    assert "TPL.FRAC.ADD.UNLIKE.CMP.001" in (staging / "prompt-2.md").read_text(encoding="utf-8")


# --- Unit tests: check_response_shape -----------------------------------------

def _valid_content() -> dict:
    return load_yaml(SCENARIOS_DIR / "clean" / "01.yaml")


def test_check_response_shape_accepts_valid_content() -> None:
    data, error = stage2_draft.check_response_shape(yaml.safe_dump(_valid_content()))
    assert error is None
    assert data["template_id"] == "TPL.FRAC.ADD.UNLIKE.CMP.001"


def test_check_response_shape_rejects_invalid_yaml() -> None:
    data, error = stage2_draft.check_response_shape("not: valid: yaml: [")
    assert data is None
    assert "not valid YAML" in error


def test_check_response_shape_rejects_scaffolding_leak() -> None:
    content = _valid_content()
    content["bank_version"] = "2026.07-dev"  # pipeline-filled, LLM must not draft it
    data, error = stage2_draft.check_response_shape(yaml.safe_dump(content))
    assert data is None
    assert "bank_version" in error
    assert "harness fills these in" in error


def test_check_response_shape_rejects_unknown_key() -> None:
    content = _valid_content()
    content["notes_for_reviewer"] = "please check this one"
    data, error = stage2_draft.check_response_shape(yaml.safe_dump(content))
    assert data is None
    assert "notes_for_reviewer" in error


def test_check_response_shape_rejects_missing_required_field() -> None:
    content = _valid_content()
    del content["distractors"]
    data, error = stage2_draft.check_response_shape(yaml.safe_dump(content))
    assert data is None
    assert "distractors" in error


# --- Unit tests: check_v1_restrictions -----------------------------------------

def test_check_v1_restrictions_accepts_valid_content() -> None:
    assert stage2_draft.check_v1_restrictions(_valid_content(), "FRAC.ADD.UNLIKE") == []


def test_check_v1_restrictions_rejects_out_of_scope_question_type() -> None:
    content = _valid_content()
    content["question_type"] = "true_false"
    errors = stage2_draft.check_v1_restrictions(content, "FRAC.ADD.UNLIKE")
    assert any("question_type" in e for e in errors)


def test_check_v1_restrictions_rejects_wrong_skill_ids() -> None:
    content = _valid_content()
    content["skill_ids"] = ["FRAC.ADD.UNLIKE", "GEO.PERIMETER"]  # multi-skill, out of scope
    errors = stage2_draft.check_v1_restrictions(content, "FRAC.ADD.UNLIKE")
    assert any("skill_ids" in e for e in errors)


def test_check_v1_restrictions_rejects_bad_difficulty_prior() -> None:
    content = _valid_content()
    content["difficulty_prior"] = "expert"
    errors = stage2_draft.check_v1_restrictions(content, "FRAC.ADD.UNLIKE")
    assert any("difficulty_prior" in e for e in errors)


# --- Unit tests: check_template_id_collision -----------------------------------

def test_check_template_id_collision_detects_existing_id() -> None:
    errors = stage2_draft.check_template_id_collision(
        "TPL.FRAC.ADD.UNLIKE.WORD.001", {"TPL.FRAC.ADD.UNLIKE.WORD.001"}
    )
    assert errors and "already exists" in errors[0]


def test_check_template_id_collision_allows_fresh_id() -> None:
    assert stage2_draft.check_template_id_collision(
        "TPL.FRAC.ADD.UNLIKE.WORD.002", {"TPL.FRAC.ADD.UNLIKE.WORD.001"}
    ) == []


def test_check_template_id_collision_rejects_non_string() -> None:
    errors = stage2_draft.check_template_id_collision(None, set())
    assert errors and "not a non-empty string" in errors[0]


# --- Unit tests: fill_scaffolding ----------------------------------------------

def test_fill_scaffolding_maps_difficulty_band_to_scalar() -> None:
    content = _valid_content()
    assert content["difficulty_prior"] == "easy"
    full = stage2_draft.fill_scaffolding(
        content,
        template_version="1.0.0", bank_version="dev", graph_version="1.0.0",
        tts=False, generator_version="test", reviewer="rishi", date="2026-07-27",
    )
    assert full["difficulty"] == stage2_draft.DIFFICULTY_BAND_MIDPOINT["easy"]
    assert full["provenance"] == {
        "generator_version": "test", "reviewer": "rishi", "date": "2026-07-27",
    }
    # Original content fields survive the merge untouched.
    assert full["template_id"] == content["template_id"]


# --- Unit tests: load_skill_context --------------------------------------------

def test_load_skill_context_returns_misconceptions_and_exam_contexts() -> None:
    skill_node, miscs, exam_contexts = stage2_draft.load_skill_context(FIXTURE_DIR, "FRAC.ADD.UNLIKE")
    assert skill_node["skill_id"] == "FRAC.ADD.UNLIKE"
    misc_ids = {m["misconception_id"] for m in miscs}
    assert misc_ids == {"MISC.FRAC.ADD-ACROSS", "MISC.FRAC.FIRST-DENOM", "MISC.FRAC.NO-SIMPLIFY"}
    assert exam_contexts == ["OLYMPIAD_IMO_L1", "SCHOOL_EXAM"]


def test_load_skill_context_unknown_skill_exits() -> None:
    with pytest.raises(SystemExit, match="NOT.A.REAL.SKILL"):
        stage2_draft.load_skill_context(FIXTURE_DIR, "NOT.A.REAL.SKILL")


def test_load_skill_context_skill_with_no_misconceptions() -> None:
    _skill_node, miscs, _exam_contexts = stage2_draft.load_skill_context(FIXTURE_DIR, "FRAC.ADD.LIKE")
    assert miscs == []


# --- Unit tests: load_existing_template_ids ------------------------------------

def test_load_existing_template_ids_reads_real_fixture() -> None:
    ids = stage2_draft.load_existing_template_ids(TEMPLATES_DIR)
    assert "TPL.FRAC.ADD.UNLIKE.WORD.001" in ids
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))  # no duplicates
