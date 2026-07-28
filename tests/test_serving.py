"""Tests for orchestrator/serving.py — the U4 session-pinned, seen-filtered
serving facade (KTD3, KTD6) — and tools/bank_activate.py's KTD3 pointer
write.

Fixtures build small, real banks from a single copied template
(TPL.FRAC.ADD.LIKE.NUM.001, skill FRAC.ADD.LIKE, numeric_entry) at a small
--pool-cap, written to disk under a tmp banks/ directory — real enough to
exercise schema-valid instances and cross-build variant_signature overlap
(KTD4), small enough to exhaust deterministically in a test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import bank_activate
import bank_build
from conftest import REPO_ROOT
from orchestrator import constants, engine, log as log_module, serving

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"


def _templates_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    return dest


def _build(banks_dir: Path, bank_version: str, pool_cap: int, templates_dir: Path) -> None:
    manifest, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, bank_version, pool_cap)
    bank_build.write_bank(banks_dir, bank_version, manifest, per_template)


@pytest.fixture()
def banks_dir(tmp_path: Path) -> Path:
    out = tmp_path / "banks"
    out.mkdir()
    _build(out, "build-a", pool_cap=2, templates_dir=_templates_dir(tmp_path))
    return out


@pytest.fixture()
def logs_dir(tmp_path: Path) -> Path:
    out = tmp_path / "logs"
    out.mkdir()
    return out


@pytest.fixture()
def content() -> engine.ContentIndex:
    return engine.load_content(REPO_ROOT / "content" / "fractions")


@pytest.fixture()
def facade(banks_dir: Path, logs_dir: Path, content: engine.ContentIndex) -> serving.ServingFacade:
    return serving.ServingFacade(banks_dir, logs_dir, content)


def _make_event(instance: dict, session_id: str, student_id: str, **overrides) -> dict:
    defaults = dict(
        event_id=f"evt-{instance['item_instance_id']}",
        student_id=student_id,
        session_id=session_id,
        timestamp="2026-07-27T12:00:00",
        item_instance_id=instance["item_instance_id"],
        template_id=instance["template_id"],
        template_version=instance["template_version"],
        bank_version=instance["bank_version"],
        variant_signature=instance["variant_signature"],
        consumer_id=constants.CONSUMER_ID_INTERNAL,
        engine_version=constants.ENGINE_VERSION,
        graph_version=instance["graph_version"],
        skill_ids=[SKILL_ID],
        question_type=instance["question_type"],
        depth_served=instance["difficulty"],
        b_prior_at_serve=instance["difficulty"],
        exam_context=None,
        language=instance["language"],
        locale=instance["locale"],
        response_raw=instance["answer"],
        response_parsed=instance["answer"],
        correct=True,
        misconception_id=None,
        hints_used=0,
        hint_auto_offered=False,
        time_taken_sec=10.0,
        timed_out=False,
        context="practice",
        theta_before=0.0,
        theta_after=0.05,
        n_before=0.0,
    )
    defaults.update(overrides)
    return defaults


# --- open_session / ACTIVE pointer (KTD3, R3, R4) ---------------------------


def test_open_session_pins_to_active_build_and_ignores_later_activation(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Covers AE4: a session opened before bank_activate repoints keeps
    serving from its original build; a session opened after pins the new
    one."""
    _build(banks_dir, "build-b", pool_cap=2, templates_dir=_templates_dir(banks_dir.parent))
    bank_activate.activate(banks_dir, "build-a")

    early_session = facade.open_session("sess-early", "stu-pin")
    assert early_session.bank.bank_version == "build-a"

    bank_activate.activate(banks_dir, "build-b")
    late_session = facade.open_session("sess-late", "stu-pin")
    assert late_session.bank.bank_version == "build-b"

    # The already-open session is unaffected by the repoint (R4).
    assert early_session.bank.bank_version == "build-a"


def test_no_active_bank_pointer_raises_named_error(facade: serving.ServingFacade) -> None:
    with pytest.raises(serving.NoActiveBankError):
        facade.open_session("sess-1", "stu-1")


def test_active_pointer_naming_a_deleted_build_raises_named_error(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    shutil.rmtree(banks_dir / "build-a")
    with pytest.raises(serving.NoActiveBankError):
        facade.open_session("sess-1", "stu-1")


# --- tools/bank_activate.py (KTD3 atomic pointer write) ---------------------


def test_bank_activate_cli_refuses_version_not_on_disk_and_leaves_active_unchanged(
    banks_dir: Path,
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    pointer = banks_dir / serving.ACTIVE_POINTER_NAME
    before = pointer.read_text(encoding="utf-8")

    exit_code = bank_activate.main(["--banks", str(banks_dir), "--bank-version", "does-not-exist"])

    assert exit_code == 1
    assert pointer.read_text(encoding="utf-8") == before


def test_bank_activate_cli_activates_a_real_build(banks_dir: Path) -> None:
    exit_code = bank_activate.main(["--banks", str(banks_dir), "--bank-version", "build-a"])
    assert exit_code == 0
    assert (banks_dir / serving.ACTIVE_POINTER_NAME).read_text(encoding="utf-8") == "build-a"


# --- identifier discipline (point 3) -----------------------------------------


@pytest.mark.parametrize("bad_student_id", ["../escape", "a/b", "a\\b", "..", ""])
def test_open_session_rejects_path_traversal_student_id(
    banks_dir: Path, logs_dir: Path, facade: serving.ServingFacade, bad_student_id: str
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    with pytest.raises(serving.InvalidIdentifierError):
        facade.open_session("sess-1", bad_student_id)
    # Rejection happens before any path is constructed — logs_dir stays empty.
    assert list(logs_dir.iterdir()) == []


# --- get_candidates: seen filter + exhaustion (R1, R2) -----------------------


def test_get_candidates_are_seen_filtered_and_carry_bank_version(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    session = facade.open_session("sess-1", "stu-1")
    result = facade.get_candidates(session, SKILL_ID)
    assert result.bank_version == "build-a"
    assert result.exhausted is False
    assert len(result.instances) == 2  # pool_cap=2, nothing seen yet

    instance = result.instances[0]
    session.engine_state.seen_history[SKILL_ID] = [instance["variant_signature"]]
    result2 = facade.get_candidates(session, SKILL_ID)
    assert result2.exhausted is False
    assert len(result2.instances) == 1
    assert result2.instances[0]["variant_signature"] != instance["variant_signature"]


def test_get_candidates_exhausted_pool_repeats_least_recently_seen_with_signal(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Covers AE1 (read half): both pool instances already seen -> the
    response repeats the least-recently-seen one and says so explicitly."""
    bank_activate.activate(banks_dir, "build-a")
    session = facade.open_session("sess-1", "stu-1")
    pool = session.bank.instances_by_skill[SKILL_ID]
    session.engine_state.seen_history[SKILL_ID] = [inst["variant_signature"] for inst in pool]

    result = facade.get_candidates(session, SKILL_ID)
    assert result.exhausted is True
    assert result.warning is not None
    assert len(result.instances) == 1
    assert result.instances[0]["variant_signature"] == pool[0]["variant_signature"]


# --- session-open half of KTD7: seen-state across a build boundary ----------


def test_open_session_derives_seen_state_across_a_build_boundary(
    banks_dir: Path, logs_dir: Path, facade: serving.ServingFacade
) -> None:
    # A large cap on build-b (>= total valid variants) includes every
    # variant bank_build.sample_variant_indices could produce, guaranteeing
    # overlap with whatever subset build-a's small cap happened to sample.
    _build(banks_dir, "build-b", pool_cap=10_000, templates_dir=_templates_dir(banks_dir.parent))
    bank_a = engine.load_bank(banks_dir / "build-a")
    bank_b = engine.load_bank(banks_dir / "build-b")
    shared_signature = set(i["variant_signature"] for i in bank_a.instances_by_skill[SKILL_ID]) & set(
        i["variant_signature"] for i in bank_b.instances_by_skill[SKILL_ID]
    )
    assert shared_signature, "expected at least one overlapping variant across the two small builds"

    instance_a = next(i for i in bank_a.instances_by_skill[SKILL_ID] if i["variant_signature"] in shared_signature)
    log_path = logs_dir / "stu-boundary.jsonl"
    log_module.append_attempt(log_path, _make_event(instance_a, "sess-old", "stu-boundary"))

    bank_activate.activate(banks_dir, "build-b")
    session = facade.open_session("sess-new", "stu-boundary")

    assert session.bank.bank_version == "build-b"
    assert instance_a["variant_signature"] in session.engine_state.seen_history.get(SKILL_ID, [])

    result = facade.get_candidates(session, SKILL_ID)
    served_signatures = {i["variant_signature"] for i in result.instances}
    assert instance_a["variant_signature"] not in served_signatures or result.exhausted


# --- submit_attempt + get_progress -------------------------------------------


def test_submit_attempt_then_progress_reports_theta_certification_and_seen_count(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    session = facade.open_session("sess-1", "stu-progress")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    outcome = facade.submit_attempt(
        session, SKILL_ID, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=10.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    assert outcome.event is not None
    assert outcome.rejection_reason is None
    assert outcome.repeat is False
    event = outcome.event
    assert event["variant_signature"] == instance["variant_signature"]

    progress = facade.get_progress(session, now)
    assert SKILL_ID in progress.skills
    skill_progress = progress.skills[SKILL_ID]
    assert skill_progress.theta > 0.0
    assert skill_progress.certification_status in {"uncertified", "certified", "due", "lapsed"}
    assert skill_progress.seen_count == 1

    # The submitted event is durably logged and schema-valid.
    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 1
    assert logged[0]["event_id"] == event["event_id"]
