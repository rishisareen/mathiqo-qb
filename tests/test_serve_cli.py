"""Tests for tools/serve.py's U6 bank-resolution logic: `--bank` stays an
explicit override; when omitted, the CLI resolves via `--banks-dir`'s
ACTIVE pointer (KTD3) instead of requiring an explicit directory every
time. Exercises `resolve_bank` directly rather than the full interactive
`main()` loop, which reads real stdin and isn't test-friendly headlessly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import bank_activate
import bank_build
import serve
from conftest import REPO_ROOT
from orchestrator import serving

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"


@pytest.fixture()
def banks_dir(tmp_path: Path) -> Path:
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy(NUM_TEMPLATE, templates / NUM_TEMPLATE.name)
    out = tmp_path / "banks"
    out.mkdir()
    manifest, per_template = bank_build.run_build(templates, LOCALE_PACK, "active-build", pool_cap=2)
    bank_build.write_bank(out, "active-build", manifest, per_template)
    manifest2, per_template2 = bank_build.run_build(templates, LOCALE_PACK, "override-build", pool_cap=2)
    bank_build.write_bank(out, "override-build", manifest2, per_template2)
    return out


def test_resolve_bank_with_no_override_uses_the_active_build(banks_dir: Path) -> None:
    bank_activate.activate(banks_dir, "active-build")
    bank = serve.resolve_bank(None, banks_dir)
    assert bank.bank_version == "active-build"


def test_resolve_bank_with_explicit_override_ignores_active(banks_dir: Path) -> None:
    bank_activate.activate(banks_dir, "active-build")
    bank = serve.resolve_bank(banks_dir / "override-build", banks_dir)
    assert bank.bank_version == "override-build"


def test_resolve_bank_with_no_override_and_no_active_pointer_raises(banks_dir: Path) -> None:
    with pytest.raises(serving.NoActiveBankError):
        serve.resolve_bank(None, banks_dir)
