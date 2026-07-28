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
import threading
import time
from pathlib import Path

import pytest

import bank_activate
import bank_build
from conftest import REPO_ROOT
from orchestrator import constants, engine, log as log_module, serving
from orchestrator.identity import IdentityRegistry

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"

# U5: open_session now requires country/curriculum/grade (pinned like the
# bank) and a consumer_id (R17 link check, R20 conflict check) — these
# defaults keep every pre-U5 test call site short. consumer_id defaults to
# the same sentinel the internal runner stamps on its own attempts;
# meaningless here since none of these facades configure `identity`, so
# _check_linked is always a no-op regardless of which consumer_id is used.
DEFAULT_COUNTRY = "IN"
DEFAULT_CURRICULUM = "NCERT"
DEFAULT_GRADE = 6


def _open(
    facade: serving.ServingFacade,
    session_id: str,
    student_id: str,
    consumer_id: str = constants.CONSUMER_ID_INTERNAL,
    country: str = DEFAULT_COUNTRY,
    curriculum: str = DEFAULT_CURRICULUM,
    grade: int = DEFAULT_GRADE,
) -> serving.Session:
    return facade.open_session(session_id, student_id, country, curriculum, grade, consumer_id)


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

    early_session = _open(facade, "sess-early", "stu-pin")
    assert early_session.bank.bank_version == "build-a"

    bank_activate.activate(banks_dir, "build-b")
    late_session = _open(facade, "sess-late", "stu-pin")
    assert late_session.bank.bank_version == "build-b"

    # The already-open session is unaffected by the repoint (R4).
    assert early_session.bank.bank_version == "build-a"


def test_no_active_bank_pointer_raises_named_error(facade: serving.ServingFacade) -> None:
    with pytest.raises(serving.NoActiveBankError):
        _open(facade, "sess-1", "stu-1")


def test_active_pointer_naming_a_deleted_build_raises_named_error(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    shutil.rmtree(banks_dir / "build-a")
    with pytest.raises(serving.NoActiveBankError):
        _open(facade, "sess-1", "stu-1")


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
        _open(facade, "sess-1", bad_student_id)
    # Rejection happens before any path is constructed — logs_dir stays empty.
    assert list(logs_dir.iterdir()) == []


# --- get_candidates: seen filter + exhaustion (R1, R2) -----------------------


def test_get_candidates_are_seen_filtered_and_carry_bank_version(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-1", "stu-1")
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
    session = _open(facade, "sess-1", "stu-1")
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
    session = _open(facade, "sess-new", "stu-boundary")

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
    session = _open(facade, "sess-1", "stu-progress")
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

    progress = facade.get_progress(session, now, consumer_id=constants.CONSUMER_ID_INTERNAL)
    assert SKILL_ID in progress.skills
    skill_progress = progress.skills[SKILL_ID]
    assert skill_progress.theta > 0.0
    assert skill_progress.certification_status in {"uncertified", "certified", "due", "lapsed"}
    assert skill_progress.seen_count == 1

    # The submitted event is durably logged and schema-valid.
    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 1
    assert logged[0]["event_id"] == event["event_id"]


# --- U5: link enforcement (R17, AE6) -----------------------------------------


@pytest.fixture()
def identity_registry(tmp_path: Path) -> IdentityRegistry:
    return IdentityRegistry(tmp_path / "identity" / "registry.jsonl")


@pytest.fixture()
def linked_facade(
    banks_dir: Path, logs_dir: Path, content: engine.ContentIndex, identity_registry: IdentityRegistry
) -> serving.ServingFacade:
    return serving.ServingFacade(banks_dir, logs_dir, content, identity=identity_registry)


def test_open_session_refused_for_unlinked_consumer_then_succeeds_after_linking(
    banks_dir: Path, linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    student_id = identity_registry.register_student({"guardian_email": "a@example.com"}, consumer_id="partner.owner")

    with pytest.raises(serving.ConsumerNotLinkedError):
        _open(linked_facade, "sess-1", student_id, consumer_id="partner.other")

    identity_registry.link_student(student_id, {"guardian_email": "a@example.com"}, consumer_id="partner.other")
    session = _open(linked_facade, "sess-1", student_id, consumer_id="partner.other")
    assert session.student_id == student_id


def test_submit_attempt_and_get_progress_refused_for_unlinked_consumer(
    banks_dir: Path, linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """AE6's read half: a consumer that never linked cannot read progress
    (or submit an attempt) for a student someone else registered."""
    bank_activate.activate(banks_dir, "build-a")
    student_id = identity_registry.register_student({"guardian_email": "b@example.com"}, consumer_id="partner.owner")
    session = _open(linked_facade, "sess-owner", student_id, consumer_id="partner.owner")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    with pytest.raises(serving.ConsumerNotLinkedError):
        linked_facade.get_progress(session, now, consumer_id="partner.stranger")

    with pytest.raises(serving.ConsumerNotLinkedError):
        linked_facade.submit_attempt(
            session, SKILL_ID, instance,
            response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
            misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
            timed_out=False, engaged=True, session_last_event=False, context="practice",
            consumer_id="partner.stranger", now=now,
        )

    # The owning consumer, meanwhile, is unaffected.
    progress = linked_facade.get_progress(session, now, consumer_id="partner.owner")
    assert progress.student_id == student_id


def test_without_identity_registry_operations_are_unaffected(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Test scenario 7: with no identity registry configured
    (`facade.identity is None`, the default and the internal runner's
    posture), no link check fires regardless of consumer_id — every
    operation behaves exactly as it did before this unit."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-1", "stu-nolink", consumer_id="anyone.at.all")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    outcome = facade.submit_attempt(
        session, SKILL_ID, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id="somebody.else.entirely", now=now,
    )
    assert outcome.event is not None

    progress = facade.get_progress(session, now, consumer_id="yet.another.consumer")
    assert SKILL_ID in progress.skills

    facade.close_session(session, consumer_id="a.fourth.consumer")


# --- U5: session conflict + takeover-on-open (R20, AE9) ----------------------


def test_cross_consumer_second_open_is_refused_first_session_unaffected(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    first = _open(facade, "sess-first", "stu-conflict", consumer_id="partner.a")

    with pytest.raises(serving.SessionConflictError) as excinfo:
        _open(facade, "sess-second", "stu-conflict", consumer_id="partner.b")
    assert "close_session_by_student" in str(excinfo.value)

    # The first session is unaffected: still registered under its own id,
    # still fully usable.
    assert facade.sessions.get("sess-first") is first
    result = facade.get_candidates(first, SKILL_ID)
    assert result.bank_version == "build-a"


def test_same_consumer_reopen_takes_over_prior_session(banks_dir: Path, facade: serving.ServingFacade) -> None:
    bank_activate.activate(banks_dir, "build-a")
    _open(facade, "sess-first", "stu-takeover", consumer_id="partner.a")

    second = _open(facade, "sess-second", "stu-takeover", consumer_id="partner.a")
    assert second.session_id == "sess-second"

    # The prior session is no longer registered (closed by takeover).
    with pytest.raises((serving.SessionUnknownError, serving.SessionExpiredError)):
        facade.sessions.get("sess-first")

    # The new session is the one now indexed for this student.
    found = facade.sessions.find_by_student("stu-takeover")
    assert found is not None and found.session_id == "sess-second"


# --- U5: close_session / close_session_by_student, closure attribution ------


def test_close_session_removes_from_registry_and_logs_closure(
    banks_dir: Path, facade: serving.ServingFacade, caplog: pytest.LogCaptureFixture
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-close", "stu-close")

    with caplog.at_level("INFO", logger="orchestrator.serving"):
        facade.close_session(session, consumer_id=constants.CONSUMER_ID_INTERNAL)

    with pytest.raises((serving.SessionUnknownError, serving.SessionExpiredError)):
        facade.sessions.get("sess-close")
    assert any("close_by_session_id" in record.getMessage() for record in caplog.records)


def test_close_session_by_student_from_linked_non_opener_attributes_to_closer(
    banks_dir: Path,
    linked_facade: serving.ServingFacade,
    identity_registry: IdentityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test scenario 4: a linked consumer who did not open the session can
    still close it by student_id, ending the orphaned session, with the
    closure attributed to the closing consumer, not the opener."""
    bank_activate.activate(banks_dir, "build-a")
    student_id = identity_registry.register_student({"guardian_email": "c@example.com"}, consumer_id="partner.owner")
    identity_registry.link_student(student_id, {"guardian_email": "c@example.com"}, consumer_id="partner.helper")
    session = _open(linked_facade, "sess-orphan", student_id, consumer_id="partner.owner")

    with caplog.at_level("INFO", logger="orchestrator.serving"):
        closed = linked_facade.close_session_by_student(student_id, consumer_id="partner.helper")
    assert closed is True

    closure_lines = [record.getMessage() for record in caplog.records if "close_by_student_id" in record.getMessage()]
    assert closure_lines and "closed_by=partner.helper" in closure_lines[0]
    assert f"session_id={session.session_id}" in closure_lines[0]

    with pytest.raises((serving.SessionUnknownError, serving.SessionExpiredError)):
        linked_facade.sessions.get(session.session_id)


def test_close_session_by_student_with_no_live_session_returns_false(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    student_id = identity_registry.register_student({"guardian_email": "e@example.com"}, consumer_id="partner.owner")
    assert linked_facade.close_session_by_student(student_id, consumer_id="partner.owner") is False


# --- U5: session_unknown vs session_expired (KTD4) ---------------------------


def test_unknown_vs_expired_session_ids_are_distinguishable(
    banks_dir: Path, logs_dir: Path, content: engine.ContentIndex
) -> None:
    facade = serving.ServingFacade(banks_dir, logs_dir, content, session_ttl_seconds=0.05)
    bank_activate.activate(banks_dir, "build-a")

    with pytest.raises(serving.SessionUnknownError):
        facade.sessions.get("sess-never-issued")

    _open(facade, "sess-will-expire", "stu-ttl")
    time.sleep(0.15)

    with pytest.raises(serving.SessionExpiredError):
        facade.sessions.get("sess-will-expire")

    # Still distinguishable from an id that was never issued at all.
    with pytest.raises(serving.SessionUnknownError):
        facade.sessions.get("sess-still-never-issued")


# --- U5: curriculum pin (KTD3) ------------------------------------------------


def test_curriculum_pin_readable_and_stable_across_operations(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-1", "stu-curric", country="CA", curriculum="ONTARIO", grade=7)
    assert (session.country, session.curriculum, session.grade) == ("CA", "ONTARIO", 7)

    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    facade.submit_attempt(
        session, SKILL_ID, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    # Unchanged by a later operation on the same session.
    assert (session.country, session.curriculum, session.grade) == ("CA", "ONTARIO", 7)


# --- U5: per-session lock serializes concurrent submit_attempt (KTD17) -------


def test_concurrent_submit_attempt_on_same_session_is_serialized(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Test scenario 8: two threads submit different attempts against the
    same live session at once; the per-session lock must make the outcome
    identical to running them one after another — both logged exactly
    once, no corrupted engine_state, no lost seen-history entry."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-concurrent", "stu-concurrent")
    pool = session.bank.instances_by_skill[SKILL_ID]
    assert len(pool) >= 2, "pool_cap=2 in the banks_dir fixture should yield two distinct instances"
    instance_a, instance_b = pool[0], pool[1]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _submit(instance: dict) -> None:
        barrier.wait()
        try:
            outcome = facade.submit_attempt(
                session, SKILL_ID, instance,
                response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
                misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
                timed_out=False, engaged=True, session_last_event=False, context="practice",
                consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
            )
            results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - surfaced via assertion below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_submit, args=(instance_a,)), threading.Thread(target=_submit, args=(instance_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"submit_attempt raised under concurrency: {errors}"
    assert len(results) == 2
    assert all(outcome.event is not None for outcome in results)

    # No lost updates: both attempts landed in the log, exactly once each.
    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 2
    assert {e["item_instance_id"] for e in logged} == {
        instance_a["item_instance_id"],
        instance_b["item_instance_id"],
    }

    # engine_state wasn't corrupted by an interleaved read-modify-write:
    # both variant_signatures are recorded seen, exactly once each.
    seen = session.engine_state.seen_history[SKILL_ID]
    assert sorted(seen) == sorted([instance_a["variant_signature"], instance_b["variant_signature"]])
    assert len(seen) == 2

    # theta reflects both attempts' evidence, not just one clobbering the
    # other's write.
    progress = facade.get_progress(session, now, consumer_id=constants.CONSUMER_ID_INTERNAL)
    assert progress.skills[SKILL_ID].seen_count == 2
