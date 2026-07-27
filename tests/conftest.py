"""Shared fixtures: repo paths and a helper to copy the real fixture into tmp dirs."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

FIXTURE_DIR = REPO_ROOT / "content" / "fractions"
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
