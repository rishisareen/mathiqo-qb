"""Tests for tools/stage1_draft.py — the Stage 1 drafting harness.

A fake LLM (tests/fixtures/fake_llm.py, driven via the cmd backend) serves
canned YAML responses from scenario directories under tests/fixtures/stage1/
and records every prompt it receives, so tests can assert on the lint-bounce
feedback loop. The real content/fractions fixture is used read-only; an
autouse fixture asserts it is byte-identical after every test.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import stage1_draft  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FAKE_LLM = FIXTURES_DIR / "fake_llm.py"
SCENARIOS_DIR = FIXTURES_DIR / "stage1"
CONTENT_DIR = REPO_ROOT / "content" / "fractions"
SYLLABUS = REPO_ROOT / "examples" / "ncert-fractions-excerpt.txt"


def snapshot_content() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(CONTENT_DIR.glob("*.yaml"))}


@pytest.fixture(autouse=True)
def content_untouched():
    """The driver must never write into content/ — verify byte-for-byte."""
    before = snapshot_content()
    yield
    assert snapshot_content() == before, "content/ was modified by the driver"


def run_driver(tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch, *extra: str) -> tuple[int, Path, Path]:
    """Run the driver against a canned scenario; returns (exit code, staging dir, scenario copy)."""
    scenario_copy = tmp_path / scenario
    shutil.copytree(SCENARIOS_DIR / scenario, scenario_copy)
    monkeypatch.setenv("STAGE1_LLM_CMD", f"{sys.executable} {FAKE_LLM} {scenario_copy}")
    staging = tmp_path / "staging"
    rc = stage1_draft.main([
        "--strand", "NUMBER.FRACTIONS",
        "--syllabus", str(SYLLABUS),
        "--content-dir", str(CONTENT_DIR),
        "--staging-dir", str(staging),
        "--backend", "cmd",
        *extra,
    ])
    return rc, staging, scenario_copy


def read_manifest(staging: Path, name: str) -> dict:
    return json.loads((staging / "accepted" / f"{name}.manifest.json").read_text(encoding="utf-8"))


def test_clean_draft_accepted_on_first_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "clean", monkeypatch)

    assert rc == 0
    strand_yaml = staging / "accepted" / "strand.yaml"
    assert strand_yaml.is_file()
    assert "FRAC.MULT.WHOLE" in strand_yaml.read_text(encoding="utf-8")

    manifest = read_manifest(staging, "strand")
    assert manifest["retries_used"] == 0
    assert manifest["lint"]["errors"] == []
    assert len(manifest["prompt_sha256"]) == 64
    assert manifest["draft"] == "strand"

    # The strand draft passed first try, so its single prompt carries no feedback.
    prompt_1 = (scenario / "received-prompt-1.md").read_text(encoding="utf-8")
    assert "Previous draft failed" not in prompt_1
    # The misconceptions step then ran and was accepted too.
    assert (scenario / "received-prompt-2.md").is_file()
    assert (staging / "accepted" / "misconceptions.yaml").is_file()
    assert read_manifest(staging, "misconceptions")["retries_used"] == 0


def test_dangling_prereq_bounces_back_with_lint_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "dangling", monkeypatch)

    assert rc == 0
    # The retry prompt must carry the lint error verbatim.
    prompt_2 = (scenario / "received-prompt-2.md").read_text(encoding="utf-8")
    assert "Previous draft failed these checks" in prompt_2
    assert "prerequisite 'FRAC.DOESNOTEXIST' is not a defined skill" in prompt_2

    manifest = read_manifest(staging, "strand")
    assert manifest["retries_used"] == 1
    accepted_text = (staging / "accepted" / "strand.yaml").read_text(encoding="utf-8")
    assert "FRAC.DOESNOTEXIST" not in accepted_text


def test_persistent_failure_quarantined_after_max_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, staging, scenario = run_driver(tmp_path, "persistent", monkeypatch, "--max-retries", "2")

    assert rc == 1
    # 1 initial attempt + 2 retries, all answered with the broken draft.
    assert (scenario / "received-prompt-3.md").is_file()
    assert not (scenario / "received-prompt-4.md").exists()

    rejected_yaml = staging / "rejected" / "strand.yaml"
    assert rejected_yaml.is_file()
    report = json.loads((staging / "rejected" / "strand.errors.json").read_text(encoding="utf-8"))
    assert report["attempts"] == 3
    assert any("FRAC.DOESNOTEXIST" in e for e in report["lint_errors"])

    # Nothing accepted, and the run stopped before the misconceptions step.
    assert not (staging / "accepted").exists()


def test_print_prompts_writes_prompts_and_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    # No STAGE1_LLM_CMD set: any backend call would fail, proving none happens.
    monkeypatch.delenv("STAGE1_LLM_CMD", raising=False)
    rc = stage1_draft.main([
        "--strand", "NUMBER.FRACTIONS",
        "--syllabus", str(SYLLABUS),
        "--content-dir", str(CONTENT_DIR),
        "--staging-dir", str(staging),
        "--backend", "file",
        "--print-prompts",
    ])

    assert rc == 0
    prompt_1 = (staging / "prompt-1.md").read_text(encoding="utf-8")
    prompt_2 = (staging / "prompt-2.md").read_text(encoding="utf-8")
    assert "NUMBER.FRACTIONS" in prompt_1
    assert "FRAC.ADD.UNLIKE" in prompt_1  # existing ids are inlined
    assert "skill-node" in prompt_1 or "skill_id" in prompt_1  # schema inlined
    assert "MISC.FRAC.ADD-ACROSS" in prompt_2  # existing misconception ids inlined
    assert not (staging / "accepted").exists()
    assert not (staging / "rejected").exists()
    # Each prompt gets a content-hash sidecar (see FileBackend) so a later
    # response can be checked against the prompt it was actually staged for.
    assert (staging / "prompt-1.md.sha256").is_file()


def test_file_backend_consumes_response_matching_its_prompt(tmp_path: Path) -> None:
    backend = stage1_draft.FileBackend(tmp_path)
    with pytest.raises(SystemExit):
        backend.complete("prompt A")
    (tmp_path / "response-1.yaml").write_text("skills: []\n", encoding="utf-8")

    result = stage1_draft.FileBackend(tmp_path).complete("prompt A")
    assert result == "skills: []\n"


def test_file_backend_rejects_response_staged_for_a_different_prompt(tmp_path: Path) -> None:
    """If the prompt this run would send at a given position no longer
    matches what prompt-<n>.md.sha256 recorded (flags changed, content
    edited), the stale response must not be silently consumed."""
    with pytest.raises(SystemExit):
        stage1_draft.FileBackend(tmp_path).complete("prompt A")
    (tmp_path / "response-1.yaml").write_text("skills: []\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="staged against a different prompt"):
        stage1_draft.FileBackend(tmp_path).complete("prompt B — different content")


def test_print_prompts_then_fill_then_resume_consumes_response_one(tmp_path: Path) -> None:
    """End-to-end regression test for a resume bug found during review: the
    file backend used to initialize its position counter from the count of
    existing response-*.yaml files, so after --print-prompts wrote prompt-1
    and prompt-2 up front and a human filled only response-1.yaml, resuming
    would skip straight to asking for response-2.yaml — response-1.yaml
    (the strand draft) was never read back by run_draft, so it never landed
    in accepted/ and the misconceptions step never saw its ids staged."""
    staging = tmp_path / "staging"
    rc = stage1_draft.main([
        "--strand", "NUMBER.FRACTIONS",
        "--syllabus", str(SYLLABUS),
        "--content-dir", str(CONTENT_DIR),
        "--staging-dir", str(staging),
        "--backend", "file",
        "--print-prompts",
    ])
    assert rc == 0
    assert (staging / "prompt-1.md").is_file()
    assert (staging / "prompt-2.md").is_file()

    shutil.copy(SCENARIOS_DIR / "clean" / "01.yaml", staging / "response-1.yaml")

    # FileBackend.complete() raises SystemExit directly (not a return code)
    # when a position still needs a response — here that must be position 2
    # (misconceptions), not response-1.yaml being skipped over.
    with pytest.raises(SystemExit, match="response-2.yaml"):
        stage1_draft.main([
            "--strand", "NUMBER.FRACTIONS",
            "--syllabus", str(SYLLABUS),
            "--content-dir", str(CONTENT_DIR),
            "--staging-dir", str(staging),
            "--backend", "file",
        ])
    assert not (staging / "response-2.yaml").exists()

    strand_yaml = staging / "accepted" / "strand.yaml"
    assert strand_yaml.is_file(), "response-1.yaml (strand draft) was never consumed"
    assert "FRAC.MULT.WHOLE" in strand_yaml.read_text(encoding="utf-8")
    # The misconceptions prompt regenerated at resume time must reflect the
    # strand step's freshly accepted ids, not the ids known at print-prompts
    # time (before anything was accepted).
    assert "FRAC.MULT.WHOLE" in (staging / "prompt-2.md").read_text(encoding="utf-8")
