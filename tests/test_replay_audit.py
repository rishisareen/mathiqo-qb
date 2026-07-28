"""Tests for tools/replay_audit.py — the U7 replay-audit tool: R10's
acceptance bar (AE5) as a runnable check, reusing orchestrator/ingest.py's
KTD5 decision path after the fact and orchestrator/serving.py's
build_progress_summary for the reproduced view.

Fixtures build two small, real bank versions from a single copied template
(TPL.FRAC.ADD.LIKE.NUM.001, skill FRAC.ADD.LIKE, numeric_entry) written to
disk under a tmp banks/ directory — real enough for schema-valid instances
and KTD7 multi-build resolution, small enough for exact control over which
instance each logged event references.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

import bank_build
import replay_audit
from conftest import REPO_ROOT, _bypass_placement
from orchestrator import engine, log as log_module, session
from orchestrator.state import StudentModelStore

NOW = datetime(2026, 7, 27, 12, 0, 0)

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"


def _templates_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    return dest


def _build_on_disk(banks_dir: Path, bank_version: str, templates_dir: Path, pool_cap: int) -> engine.BankIndex:
    manifest, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, bank_version, pool_cap)
    bank_build.write_bank(banks_dir, bank_version, manifest, per_template)
    return engine.load_bank(banks_dir / bank_version)


@pytest.fixture()
def content() -> engine.ContentIndex:
    return engine.load_content(REPO_ROOT / "content" / "fractions")


@pytest.fixture()
def banks_dir(tmp_path: Path) -> Path:
    out = tmp_path / "banks"
    out.mkdir()
    return out


def _log_attempts(
    log_path: Path, content: engine.ContentIndex, bank: engine.BankIndex, student_id: str,
    session_id: str, store: StudentModelStore, engine_state: engine.EngineState, count: int,
) -> engine.EngineState:
    """Directly logs `count` correct attempts against `bank`'s own pool via
    session._apply_and_log (the same compose/log/consume path a real
    session uses) — bypasses the interactive loop entirely for exact
    control over which instances end up in the log."""
    pool = bank.instances_by_skill[SKILL_ID]
    for i in range(count):
        instance = pool[i % len(pool)]
        serve = engine.Serve(instance["item_instance_id"], SKILL_ID, instance["difficulty"], "practice", "test")
        applied = session._apply_and_log(
            store, engine_state, content, bank, student_id, session_id, log_path, serve, instance,
            response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
            misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
            timed_out=False, engaged=True, session_last_event=False, now=NOW,
        )
        assert applied is not None
        _theta_before, _theta_after, engine_state, _event = applied
    return engine_state


# --- Covers AE5: a clean multi-session log audits clean and matches live ----


def test_clean_log_audits_clean_and_matches_live_end_state(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    bank = _build_on_disk(banks_dir, "audit-clean", _templates_dir(tmp_path), pool_cap=5)
    student_id = "stu-clean"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    engine_state = _log_attempts(log_path, content, bank, student_id, "sess-1", store, engine_state, count=4)

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert failures == []
    assert student_id in summaries
    live_state = store.peek(student_id, SKILL_ID)
    reproduced = summaries[student_id].skills[SKILL_ID]
    assert reproduced.theta == pytest.approx(live_state.theta)
    assert reproduced.n == pytest.approx(live_state.n)
    assert reproduced.certified_depth == live_state.certified_depth
    assert reproduced.seen_count == len(engine_state.seen_history.get(SKILL_ID, []))


# --- A log spanning two bank versions audits clean --------------------------


def test_log_spanning_two_bank_versions_audits_clean(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    templates_dir = _templates_dir(tmp_path)
    bank_a = _build_on_disk(banks_dir, "audit-a", templates_dir, pool_cap=5)
    bank_b = _build_on_disk(banks_dir, "audit-b", templates_dir, pool_cap=5)

    student_id = "stu-boundary"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    engine_state = _log_attempts(log_path, content, bank_a, student_id, "sess-pre", store, engine_state, count=3)
    engine_state = _log_attempts(log_path, content, bank_b, student_id, "sess-post", store, engine_state, count=3)

    events = list(log_module.iter_events(log_path))
    assert {e["bank_version"] for e in events} == {"audit-a", "audit-b"}

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert failures == []
    live_state = store.peek(student_id, SKILL_ID)
    reproduced = summaries[student_id].skills[SKILL_ID]
    assert reproduced.theta == pytest.approx(live_state.theta)
    assert reproduced.certified_depth == live_state.certified_depth


# --- Tampered event -> named failure, non-zero exit -------------------------


def _tamper_last_event(log_path: Path, **overrides) -> None:
    lines = log_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event.update(overrides)
    lines[-1] = json.dumps(event, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_unknown_bank_version_fails_audit_naming_the_event(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    bank = _build_on_disk(banks_dir, "audit-tamper-1", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-tamper-1"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)
    _tamper_last_event(log_path, bank_version="does-not-exist")

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert len(failures) == 1
    assert "unknown_bank" in failures[0].reason
    assert summaries == {}


def test_signature_mismatch_fails_audit_naming_the_event(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    bank = _build_on_disk(banks_dir, "audit-tamper-2", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-tamper-2"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)
    _tamper_last_event(log_path, variant_signature="f" * 64)

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, _summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert len(failures) == 1
    assert "signature_mismatch" in failures[0].reason


def test_unresolvable_instance_fails_audit_naming_the_event(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    bank = _build_on_disk(banks_dir, "audit-tamper-3", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-tamper-3"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)
    _tamper_last_event(log_path, item_instance_id="does-not-exist")

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, _summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert len(failures) == 1
    assert "unresolvable_instance" in failures[0].reason


def test_cli_exits_nonzero_on_tampered_log_and_zero_on_clean_log(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    bank = _build_on_disk(banks_dir, "audit-cli", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-cli"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)

    clean_exit = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
    ])
    assert clean_exit == 0

    _tamper_last_event(log_path, bank_version="does-not-exist")
    tampered_exit = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
    ])
    assert tampered_exit == 1
