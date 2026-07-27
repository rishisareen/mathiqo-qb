"""Shared fixtures: repo paths, a helper to copy the real fixture into tmp
dirs, and (below) the real content/bank fixtures plus small bank/engine-state
helpers shared by tests/test_engine.py, tests/test_session.py, and
tests/test_simulation.py — consolidated here after a simplify pass found the
`content`/`bank` session fixtures and four small helpers independently
copy-pasted across those three files, byte-identical apart from a docstring
here and there. Session-scoping `bank` in conftest.py also means the real
bank is now built once for the whole test run, not once per file.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

FIXTURE_DIR = REPO_ROOT / "content" / "fractions"
TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
SCHEMAS_DIR = REPO_ROOT / "schemas"


@pytest.fixture()
def fixture_copy(tmp_path: Path) -> Path:
    """A mutable copy of content/fractions — the real fixture is never touched."""
    dest = tmp_path / "fractions"
    shutil.copytree(FIXTURE_DIR, dest)
    return dest


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared across test_engine.py / test_session.py / test_simulation.py: the
# real content graph, a real built bank, and small isolation helpers those
# files' tests use to construct narrow scenarios.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def content():
    from orchestrator import engine

    return engine.load_content(FIXTURE_DIR)


@pytest.fixture(scope="session")
def bank(tmp_path_factory: pytest.TempPathFactory):
    import bank_build  # tools/bank_build.py; this module puts tools/ on sys.path
    from orchestrator import engine

    out_dir = tmp_path_factory.mktemp("shared_bank_out")
    manifest, per_template = bank_build.run_build(
        TEMPLATES_DIR, bank_build.DEFAULT_LOCALE_PACK, "test-shared", bank_build.DEFAULT_POOL_CAP
    )
    target_dir = bank_build.write_bank(out_dir, "test-shared", manifest, per_template)
    return engine.load_bank(target_dir)


def find_tagged_instance(bank_index, skill_id: str, misconception_id: str):
    """A real bank instance of `skill_id` carrying a distractor tagged
    `misconception_id`, plus that distractor's value (the wrong-answer
    response that will match it)."""
    for instance in bank_index.instances_by_skill.get(skill_id, []):
        for distractor in instance["distractors"]:
            if distractor["misconception_id"] == misconception_id:
                return instance, distractor["value"]
    raise AssertionError(f"no {skill_id} instance in the built bank tags {misconception_id}")


def _bypass_placement():
    from orchestrator import engine

    return engine.EngineState(placement_cursor={}, placement_initialized=True)


def _single_skill_bank(bank_index, skill_id: str):
    from orchestrator import engine

    pool = bank_index.instances_by_skill[skill_id]
    return engine.BankIndex(
        bank_version=bank_index.bank_version,
        instances_by_skill={skill_id: pool},
        by_id={inst["item_instance_id"]: inst for inst in pool},
    )


def _numeric_only_bank(bank_index):
    """A copy of `bank_index` restricted to numeric_entry instances, so a
    scripted smoke-test session never has to distinguish MCQ from numeric
    rendering: the served question_type is always known in advance."""
    from orchestrator import engine

    instances_by_skill = {}
    by_id = {}
    for skill_id, pool in bank_index.instances_by_skill.items():
        filtered = [inst for inst in pool if inst["question_type"] == "numeric_entry"]
        if filtered:
            instances_by_skill[skill_id] = filtered
        for inst in filtered:
            by_id[inst["item_instance_id"]] = inst
    return engine.BankIndex(bank_version=bank_index.bank_version, instances_by_skill=instances_by_skill, by_id=by_id)
