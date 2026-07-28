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
from orchestrator import catalog as catalog_module, constants, engine, log as log_module, serving, signals
from orchestrator.identity import IdentityRegistry
from orchestrator.state import StudentModelStore

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
        attempt_id=f"attempt-{instance['item_instance_id']}",
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
    result = facade.get_candidates(session, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
    assert result.bank_version == "build-a"
    assert result.exhausted is False
    assert len(result.instances) == 2  # pool_cap=2, nothing seen yet

    instance = result.instances[0]
    session.engine_state.seen_history[SKILL_ID] = [instance["variant_signature"]]
    result2 = facade.get_candidates(session, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
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

    result = facade.get_candidates(session, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
    assert result.exhausted is True
    assert result.signal == signals.EXHAUSTED
    assert result.warning is not None
    assert len(result.instances) == 1
    assert result.instances[0]["variant_signature"] == pool[0]["variant_signature"]


def test_get_candidates_distinguishes_unknown_skill_empty_pool_and_exhausted(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Covers AE5: U10's three-way pool signal on get_candidates --
    `unknown_skill` (the skill_id isn't in the content graph at all),
    `empty_pool` (a known skill, but this tiny bank -- built from a single
    FRAC.ADD.LIKE template -- has no instances for it), and `exhausted`
    (the pre-U10 pool-exhaustion behavior, unchanged: the pool has
    instances, all seen, and the one least-recently-seen repeat comes
    back)."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-signals", "stu-signals")

    unknown = facade.get_candidates(session, "NOT.A.REAL.SKILL", constants.CONSUMER_ID_INTERNAL)
    assert unknown.signal == signals.UNKNOWN_SKILL
    assert unknown.exhausted is False
    assert unknown.instances == []
    assert unknown.warning is not None

    # FRAC.ADD.UNLIKE is a real skill in the content graph, but banks_dir's
    # build only ever sampled FRAC.ADD.LIKE's template -- a known skill
    # with a genuinely empty pool in this pinned bank.
    empty = facade.get_candidates(session, "FRAC.ADD.UNLIKE", constants.CONSUMER_ID_INTERNAL)
    assert empty.signal == signals.EMPTY_POOL
    assert empty.exhausted is False
    assert empty.instances == []
    assert empty.warning is not None

    pool = session.bank.instances_by_skill[SKILL_ID]
    session.engine_state.seen_history[SKILL_ID] = [inst["variant_signature"] for inst in pool]
    exhausted = facade.get_candidates(session, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
    assert exhausted.signal == signals.EXHAUSTED
    assert exhausted.exhausted is True
    assert len(exhausted.instances) == 1
    assert exhausted.instances[0]["variant_signature"] == pool[0]["variant_signature"]


# --- U10: the error-signal taxonomy is defined in exactly one place --------


def test_taxonomy_signal_names_are_each_defined_in_exactly_one_place() -> None:
    """KTD7: a grep-based parity check mirroring U8's
    test_rank_serve_candidates_has_exactly_two_call_sites (tests/test_engine.py)
    -- each of the ten canonical signal names (orchestrator/signals.py)
    must be *assigned* as a literal string exactly once across the source
    tree (orchestrator/signals.py itself); every other raise/return site
    that needs one references the constant (e.g. `signals.NOT_LINKED`)
    instead of hardcoding the string a second time."""
    import re

    source_files = [
        REPO_ROOT / "orchestrator" / "signals.py",
        REPO_ROOT / "orchestrator" / "serving.py",
        REPO_ROOT / "orchestrator" / "batch.py",
        REPO_ROOT / "orchestrator" / "engine.py",
        REPO_ROOT / "orchestrator" / "ingest.py",
        REPO_ROOT / "orchestrator" / "session.py",
        REPO_ROOT / "tools" / "mcp_server.py",
    ]
    for signal in signals.ALL:
        pattern = re.compile(r'=\s*"' + re.escape(signal) + r'"')
        definitions = sum(len(pattern.findall(path.read_text(encoding="utf-8"))) for path in source_files)
        assert definitions == 1, f"{signal!r} must be assigned as a literal string exactly once, found {definitions}"


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

    result = facade.get_candidates(session, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
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
        session, "attempt-progress-1", SKILL_ID, instance,
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

    progress = facade.get_progress(session.student_id, now=now, consumer_id=constants.CONSUMER_ID_INTERNAL)
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
        linked_facade.get_progress(session.student_id, now=now, consumer_id="partner.stranger")

    with pytest.raises(serving.ConsumerNotLinkedError):
        linked_facade.submit_attempt(
            session, "attempt-unlinked-1", SKILL_ID, instance,
            response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
            misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
            timed_out=False, engaged=True, session_last_event=False, context="practice",
            consumer_id="partner.stranger", now=now,
        )

    # The owning consumer, meanwhile, is unaffected.
    progress = linked_facade.get_progress(session.student_id, now=now, consumer_id="partner.owner")
    assert progress.student_id == student_id


def test_get_candidates_refused_for_unlinked_consumer(
    banks_dir: Path, linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """A closed simplify-review finding: get_candidates was the one
    student-scoped read that never called _check_linked, so a consumer
    holding (or guessing/logging/replaying) another partner's session_id
    could read that student's seen-filtered candidate pool — a
    cross-tenant leak every sibling operation (open_session, submit_attempt,
    get_progress, recommend_next, close_session*) already refused. A
    stranger with the right session_id must be refused just like every
    other read; the owning consumer must be unaffected."""
    bank_activate.activate(banks_dir, "build-a")
    student_id = identity_registry.register_student({"guardian_email": "c@example.com"}, consumer_id="partner.owner")
    session = _open(linked_facade, "sess-owner", student_id, consumer_id="partner.owner")

    with pytest.raises(serving.ConsumerNotLinkedError):
        linked_facade.get_candidates(session, SKILL_ID, consumer_id="partner.stranger")

    result = linked_facade.get_candidates(session, SKILL_ID, consumer_id="partner.owner")
    assert result.instances


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
        session, "attempt-nolink-1", SKILL_ID, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id="somebody.else.entirely", now=now,
    )
    assert outcome.event is not None

    progress = facade.get_progress(session.student_id, now=now, consumer_id="yet.another.consumer")
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
    result = facade.get_candidates(first, SKILL_ID, constants.CONSUMER_ID_INTERNAL)
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


def _fake_session(session_id: str, student_id: str) -> serving.Session:
    """A minimal, unbuilt `Session` for exercising `SessionRegistry`
    directly — no real bank/content needed, since `get()`'s TTL check only
    ever touches `session_id`/`student_id` and the registry's own
    bookkeeping."""
    return serving.Session(
        session_id=session_id,
        student_id=student_id,
        bank=engine.BankIndex(bank_version="v1", instances_by_skill={}, by_id={}),
        log_path=Path("/dev/null"),
        store=StudentModelStore(),
        engine_state=engine.EngineState(),
    )


def test_get_detects_a_single_expired_session_without_a_full_registry_sweep() -> None:
    """A code-review efficiency finding: `SessionRegistry.get()` used to
    call `_evict_expired_locked` (a full scan of every registered session)
    on every single lookup, even though it only ever needs to know about
    the one `session_id` it was asked for. `get()` now checks only that
    entry's own `last_activity` against the TTL directly.

    Proven here at the `SessionRegistry` level (not through `ServingFacade`,
    so nothing else — `open_session`'s own `register_or_conflict`, which
    legitimately still does a full sweep — runs a sweep in between): two
    sessions are registered, only one is aged past the TTL by mutating its
    own bookkeeping directly, and `get()` on the stale id alone must raise
    `SessionExpiredError` while leaving the other, still-live session's
    entry completely untouched (still registered, `last_activity`
    unchanged) — exactly the observable TTL/session_expired-vs-
    session_unknown behavior from before this fix, just without paying to
    scan every other entry to get there."""
    registry = serving.SessionRegistry(ttl_seconds=1000)
    stale = _fake_session("sess-stale", "stu-stale")
    fresh = _fake_session("sess-fresh", "stu-fresh")
    registry.register_or_conflict(stale, "consumer.a")
    registry.register_or_conflict(fresh, "consumer.b")

    # Age only the stale entry's own last_activity past the TTL -- no other
    # registry call happens in between that could sweep it for us.
    registry._entries["sess-stale"].last_activity -= 2000
    fresh_last_activity_before = registry._entries["sess-fresh"].last_activity

    with pytest.raises(serving.SessionExpiredError):
        registry.get("sess-stale")

    # The stale entry alone was evicted (and is now remembered as expired,
    # not merely unknown)...
    assert "sess-stale" not in registry._entries
    assert "sess-stale" in registry._expired_ids

    # ...while the still-live session was never touched by resolving a
    # different session_id: still registered, its own last_activity
    # unchanged, and still resolvable.
    assert "sess-fresh" in registry._entries
    assert registry._entries["sess-fresh"].last_activity == fresh_last_activity_before
    assert registry.get("sess-fresh") is fresh

    # A second lookup of the now-evicted id is still session_expired, not
    # session_unknown -- the distinction survives the O(1) path too.
    with pytest.raises(serving.SessionExpiredError):
        registry.get("sess-stale")


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
        session, "attempt-curriculum-1", SKILL_ID, instance,
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
                session, f"attempt-{instance['item_instance_id']}", SKILL_ID, instance,
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
    progress = facade.get_progress(session.student_id, now=now, consumer_id=constants.CONSUMER_ID_INTERNAL)
    assert progress.skills[SKILL_ID].seen_count == 2


# --- KTD17 P0 fix: a registered session's lock survives teardown -------------


def test_lock_for_survives_takeover_teardown_no_silent_nullcontext_degrade(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """P0 fix: `_lock_for` used to resolve a session's lock via a fresh
    `session_id` lookup into the registry on *every* call
    (`SessionRegistry.lock_for`). That lookup returning `None` meant two
    different things it could not tell apart: "this Session was never
    registered at all" (the internal runner's legitimate no-op case) and
    "this Session WAS registered, but a concurrent takeover-on-open/close/
    TTL-eviction just removed its entry" (a bug — an operation already in
    flight against the old Session would silently drop to an unlocked
    `contextlib.nullcontext()` right when concurrent access is most
    likely). This proves the fix: a caller already holding a reference to
    a *registered* `Session` keeps using that exact `threading.Lock`
    instance for mutual exclusion, even after a same-consumer
    takeover-on-open has removed its registry entry entirely."""
    bank_activate.activate(banks_dir, "build-a")
    session1 = _open(facade, "sess-lockrace-1", "stu-lockrace")

    lock1 = facade._lock_for(session1)
    # A real Lock was attached at registration -- not a no-op nullcontext
    # (which has no acquire/release methods at all).
    assert hasattr(lock1, "acquire") and hasattr(lock1, "release")

    # Simulate an operation already holding session1's lock (e.g. a slow
    # submit_attempt mid-body) at the exact moment a same-consumer
    # takeover-on-open tears down session1's registry entry.
    assert lock1.acquire(blocking=False) is True
    try:
        session2 = _open(facade, "sess-lockrace-2", "stu-lockrace")  # takeover-on-open
        assert session2.session_id != session1.session_id

        # session1's registry entry is gone now -- but _lock_for(session1)
        # must still resolve to the SAME lock instance, still held, not a
        # fresh nullcontext a second caller could silently sail through.
        lock1_again = facade._lock_for(session1)
        assert lock1_again is lock1
        assert lock1_again.acquire(blocking=False) is False  # still held: real mutual exclusion
    finally:
        lock1.release()

    # Once released, a fresh acquire succeeds normally -- confirms this is
    # a genuinely working Lock, not some inert stand-in.
    reacquired = facade._lock_for(session1)
    assert reacquired.acquire(blocking=False) is True
    reacquired.release()

    # The new, currently-registered session2 got its own, different lock
    # instance -- teardown of session1 never leaked or reused it.
    lock2 = facade._lock_for(session2)
    assert lock2 is not lock1
    assert hasattr(lock2, "acquire")


def test_lock_for_ephemeral_never_registered_session_stays_nullcontext(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """The internal session runner's ephemeral `Session` objects
    (`orchestrator/session.py`) are constructed directly via `Session(...)`
    and never pass through `register_or_conflict` — `_lock_for` must keep
    handing them a genuine no-op `contextlib.nullcontext()` (their
    documented single-writer posture, unchanged by the KTD17 P0 fix above:
    the fix only changes *how* a registered session's lock is resolved,
    never what an unregistered one gets)."""
    bank_activate.activate(banks_dir, "build-a")
    bank = engine.load_bank(banks_dir / "build-a")
    ephemeral = serving.Session(
        session_id="sess-ephemeral-never-registered",
        student_id="stu-ephemeral",
        bank=bank,
        log_path=banks_dir.parent / "logs" / "stu-ephemeral.jsonl",
        store=StudentModelStore(),
        engine_state=engine.EngineState(),
    )
    assert ephemeral._lock is None

    ctx = facade._lock_for(ephemeral)
    assert not hasattr(ctx, "acquire")  # nullcontext, not a real Lock
    with ctx:  # still usable as a context manager, exactly as submit_attempt uses it
        pass


# --- U6: attempt_id idempotency (R19, AE7, KTD4) ------------------------------


def _submit_kwargs(instance: dict, now, **overrides) -> dict:
    kwargs = dict(
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    kwargs.update(overrides)
    return kwargs


def test_submit_attempt_same_attempt_id_twice_in_same_session_is_idempotent(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Covers AE7: resubmitting the same attempt_id in the same live
    session (Path A) returns the identical SubmitOutcome, with exactly one
    log line and theta applied exactly once."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-idem", "stu-idem")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    kwargs = _submit_kwargs(instance, now)

    first = facade.submit_attempt(session, "attempt-idem-1", SKILL_ID, instance, **kwargs)
    assert first.event is not None
    progress_after_first = facade.get_progress(session.student_id, now=now, consumer_id=constants.CONSUMER_ID_INTERNAL)

    second = facade.submit_attempt(session, "attempt-idem-1", SKILL_ID, instance, **kwargs)
    assert second is first  # Path A: the exact same cached SubmitOutcome, nothing re-run

    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 1

    progress_after_second = facade.get_progress(session.student_id, now=now, consumer_id=constants.CONSUMER_ID_INTERNAL)
    assert progress_after_second.skills[SKILL_ID].theta == progress_after_first.skills[SKILL_ID].theta
    assert progress_after_second.skills[SKILL_ID].n == progress_after_first.skills[SKILL_ID].n
    assert progress_after_second.skills[SKILL_ID].seen_count == 1


def test_submit_attempt_resubmission_after_session_restart_replays_via_seeded_map(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Covers AE7's precise scenario: submit an attempt, discard/close that
    Session object (simulating a server restart between the log append and
    the client seeing the response), open a *new* session for the same
    student against the same bank, then resubmit the same attempt_id — the
    new session's `accepted_attempts` map (seeded at open_session time from
    the replayed log, not any live in-memory session) is what recognizes
    it as Path A."""
    bank_activate.activate(banks_dir, "build-a")
    session1 = _open(facade, "sess-restart-1", "stu-restart")
    instance = session1.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    kwargs = _submit_kwargs(instance, now)

    original = facade.submit_attempt(session1, "attempt-restart-1", SKILL_ID, instance, **kwargs)
    assert original.event is not None
    facade.close_session(session1, consumer_id=constants.CONSUMER_ID_INTERNAL)

    session2 = _open(facade, "sess-restart-2", "stu-restart")
    assert "attempt-restart-1" in session2.accepted_attempts

    replayed = facade.submit_attempt(session2, "attempt-restart-1", SKILL_ID, instance, **kwargs)
    assert replayed.event is not None
    assert replayed.event["event_id"] == original.event["event_id"]

    logged = list(log_module.iter_events(session2.log_path))
    assert len(logged) == 1

    progress = facade.get_progress(session2.student_id, now=now, consumer_id=constants.CONSUMER_ID_INTERNAL)
    assert progress.skills[SKILL_ID].seen_count == 1


def test_submit_attempt_path_b_stale_bank_never_logged_then_resubmit_hits_path_a(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """U6 Path B (R19): an attempt genuinely never logged (no prior
    submission at all), submitted under a session pinned to a *different*
    bank_version than the served instance's own stamped bank_version, must
    not be rejected as a bank/session mismatch — it's validated
    session-lessly against its own stamped build
    (`ingest.validate_event(event, None, ...)`, mirroring
    tools/replay_audit.py) instead of the ordinary SessionContext-based
    check. A second resubmission of the same attempt_id, from within the
    same session, then hits Path A."""
    _build(banks_dir, "build-b", pool_cap=2, templates_dir=_templates_dir(banks_dir.parent))
    bank_activate.activate(banks_dir, "build-a")
    bank_a = engine.load_bank(banks_dir / "build-a")
    instance_a = bank_a.instances_by_skill[SKILL_ID][0]

    # The live session is pinned to build-b — instance_a's own stamped
    # bank_version ("build-a") differs from session.bank.bank_version.
    bank_activate.activate(banks_dir, "build-b")
    session = _open(facade, "sess-pathb", "stu-pathb")
    assert session.bank.bank_version == "build-b"
    assert instance_a["bank_version"] == "build-a"

    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    kwargs = _submit_kwargs(instance_a, now)

    outcome = facade.submit_attempt(session, "attempt-pathb-1", SKILL_ID, instance_a, **kwargs)
    assert outcome.event is not None
    assert outcome.rejection_reason is None
    assert outcome.event["bank_version"] == "build-a"

    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 1

    # Resubmitting the same attempt_id in this same session now hits Path A
    # (the map was seeded within this session, not via a reopen).
    replay_outcome = facade.submit_attempt(session, "attempt-pathb-1", SKILL_ID, instance_a, **kwargs)
    assert replay_outcome is outcome

    logged_again = list(log_module.iter_events(session.log_path))
    assert len(logged_again) == 1


def test_two_attempts_with_different_attempt_ids_are_both_logged_even_if_content_matches(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """Dedup keys on attempt_id, not content/variant_signature — the same
    served instance submitted twice under two different attempt_ids is two
    genuine attempts, both logged (KD5's repeat flag, not idempotency,
    is what covers the 'same content served again' case)."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-distinct", "stu-distinct")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    kwargs = _submit_kwargs(instance, now)

    first = facade.submit_attempt(session, "attempt-distinct-1", SKILL_ID, instance, **kwargs)
    second = facade.submit_attempt(session, "attempt-distinct-2", SKILL_ID, instance, **kwargs)

    assert first.event is not None and second.event is not None
    assert first.event["event_id"] != second.event["event_id"]
    assert second.repeat is True  # KD5: same variant_signature seen again, not the same attempt

    logged = list(log_module.iter_events(session.log_path))
    assert len(logged) == 2
    assert {e["attempt_id"] for e in logged} == {"attempt-distinct-1", "attempt-distinct-2"}


def test_discarded_attempt_carries_a_reason_and_rejected_attempt_leaves_no_trace(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """A discarded (§3 abandonment-guard) attempt returns a `discard_reason`
    string and leaves the log untouched; a rejected (ingest-validation
    failure) attempt leaves zero trace too, and the session continues to
    work normally for a subsequent legitimate submission."""
    bank_activate.activate(banks_dir, "build-a")
    session = _open(facade, "sess-discard-reject", "stu-discard-reject")
    instance = session.bank.instances_by_skill[SKILL_ID][0]
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    discarded = facade.submit_attempt(
        session, "attempt-discard-1", SKILL_ID, instance,
        **_submit_kwargs(instance, now, correct=False, time_taken_sec=30.0, timed_out=True, engaged=False, session_last_event=True),
    )
    assert discarded.event is None
    assert discarded.rejection_reason is None
    assert isinstance(discarded.discard_reason, str) and discarded.discard_reason
    assert list(log_module.iter_events(session.log_path)) == []

    # A tampered variant_signature fails ingest's own check against the
    # bank's own record for this instance — a genuine rejection, not Path B
    # (bank_version is untouched, so the ordinary SessionContext path runs).
    bad_instance = dict(instance)
    bad_instance["variant_signature"] = "f" * 64
    rejected = facade.submit_attempt(
        session, "attempt-reject-1", SKILL_ID, bad_instance, **_submit_kwargs(instance, now),
    )
    assert rejected.event is None
    assert rejected.discard_reason is None
    assert "signature_mismatch" in rejected.rejection_reason
    assert list(log_module.iter_events(session.log_path)) == []

    # The session continues to work normally afterward.
    ok = facade.submit_attempt(session, "attempt-after-1", SKILL_ID, instance, **_submit_kwargs(instance, now))
    assert ok.event is not None
    assert len(list(log_module.iter_events(session.log_path))) == 1


# --- U8: recommend_next / session-less get_progress (R5, R6, R7, R20) --------
#
# recommend_next needs real placement/frontier progress across *several*
# skills to exercise bank-awareness and priority parity meaningfully — the
# module-level `banks_dir`/`facade` fixtures above deliberately hold only
# one skill's instances (FRAC.ADD.LIKE), which is exactly right for their
# own tests but too narrow here. `full_banks_dir`/`full_facade` build a
# bank from every content/templates/*.yaml instead.


@pytest.fixture()
def full_banks_dir(tmp_path: Path) -> Path:
    out = tmp_path / "full_banks"
    out.mkdir()
    manifest, per_template = bank_build.run_build(TEMPLATES_DIR, LOCALE_PACK, "full-build", pool_cap=3)
    bank_build.write_bank(out, "full-build", manifest, per_template)
    return out


@pytest.fixture()
def full_facade(full_banks_dir: Path, logs_dir: Path, content: engine.ContentIndex) -> serving.ServingFacade:
    return serving.ServingFacade(full_banks_dir, logs_dir, content)


def test_recommend_next_top_entry_matches_select_next_for_a_fresh_student(
    full_banks_dir: Path, full_facade: serving.ServingFacade
) -> None:
    """R7's parity, at the facade level: recommend_next's top-ranked entry
    must name the same skill (and reason) select_next would actually serve
    first for the identical (fresh, no log yet) student/state — a genuine
    check, constructing the state both ways rather than asserting they
    call the same function."""
    bank_activate.activate(full_banks_dir, "full-build")
    student_id = "stu-recommend-parity"
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    result = full_facade.recommend_next(
        student_id, "CA", "ONTARIO", 4, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now
    )
    assert result.recommendations
    assert result.bank_version == "full-build"

    bank = engine.load_bank(full_banks_dir / "full-build")
    select_result = engine.select_next(
        StudentModelStore(), engine.EngineState(), full_facade.content, bank, student_id, "CA", "ONTARIO", 4, now
    )
    assert select_result.serve is not None
    top = result.recommendations[0]
    assert top.skill_id == select_result.serve.skill_id
    assert top.reason == select_result.serve.reason
    assert top.target_depth == pytest.approx(select_result.serve.difficulty)


def test_recommend_next_mutates_nothing(
    full_banks_dir: Path, full_facade: serving.ServingFacade, logs_dir: Path
) -> None:
    """recommend_next must be a pure read: calling it twice returns
    identical results, the student's log is byte-for-byte unchanged, and
    an independent get_progress read (itself a fresh replay from the log)
    shows no seen-mark was written."""
    bank_activate.activate(full_banks_dir, "full-build")
    student_id = "stu-recommend-pure"
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    session = full_facade.open_session("sess-pure", student_id, "CA", "ONTARIO", 4, constants.CONSUMER_ID_INTERNAL)
    instance = session.bank.instances_by_skill["FRAC.UNIT"][0]
    full_facade.submit_attempt(
        session, "attempt-pure-1", "FRAC.UNIT", instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
        consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    full_facade.close_session(session, consumer_id=constants.CONSUMER_ID_INTERNAL)

    log_path = logs_dir / f"{student_id}.jsonl"
    log_before = log_path.read_text(encoding="utf-8")

    first = full_facade.recommend_next(
        student_id, "CA", "ONTARIO", 4, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now
    )
    second = full_facade.recommend_next(
        student_id, "CA", "ONTARIO", 4, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now
    )
    assert first == second

    log_after = log_path.read_text(encoding="utf-8")
    assert log_before == log_after  # nothing appended

    # Independently re-derive progress from the log alone: seen_count still
    # reflects only the one attempt actually submitted through the session,
    # never anything recommend_next might have marked seen.
    progress = full_facade.get_progress(student_id, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now)
    assert progress.skills["FRAC.UNIT"].seen_count == 1


def test_recommend_next_scope_narrows_candidates_without_changing_ceiling_rules(
    full_banks_dir: Path, full_facade: serving.ServingFacade
) -> None:
    """Scoping to one IN/NCERT grade-6 chapter (content/fractions/
    fractions-ncert.yaml's "IN-NCERT-G6-CH7", which — together with
    mappings.yaml's own row — maps several skills: FRAC.NUMLINE,
    FRAC.EQUIV, FRAC.CMP, FRAC.SIMPLIFY, FRAC.ADD.LIKE, FRAC.MIXED.ADD,
    FRAC.ADD.UNLIKE) must restrict the ranked list to that unit's own
    mapped skills without changing the country/curriculum/grade context
    that governs ceilings: whichever candidates survive scoping keep the
    exact same reason/target_depth/unservable verdict they had unscoped —
    only the candidate *set* shrinks, never the rules."""
    bank_activate.activate(full_banks_dir, "full-build")
    student_id = "stu-recommend-scope"
    now = log_module.parse_timestamp("2026-07-27T12:00:00")
    unit_code = "IN-NCERT-G6-CH7"

    unit_skill_ids = {
        mapped.skill_id
        for mapped in catalog_module.list_skills(full_facade.content, "IN", "NCERT", 6, unit_code).skills
    }
    assert unit_skill_ids, "fixture assumption: this unit maps at least one skill"

    unscoped = full_facade.recommend_next(
        student_id, "IN", "NCERT", 6, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now
    )
    scoped = full_facade.recommend_next(
        student_id, "IN", "NCERT", 6, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now, scope=unit_code,
    )

    unscoped_by_id = {r.skill_id: r for r in unscoped.recommendations}
    scoped_by_id = {r.skill_id: r for r in scoped.recommendations}

    assert set(scoped_by_id) <= unit_skill_ids  # scoping never lets in a skill outside the unit
    assert scoped_by_id  # and the unit is genuinely represented in the ranking
    assert set(scoped_by_id) < set(unscoped_by_id)  # a real narrowing, not a no-op

    # Every candidate that survives scoping is untouched by it — same
    # reason, target_depth, and unservable verdict as its unscoped entry.
    for skill_id, scoped_entry in scoped_by_id.items():
        unscoped_entry = unscoped_by_id[skill_id]
        assert scoped_entry.reason == unscoped_entry.reason
        assert scoped_entry.target_depth == pytest.approx(unscoped_entry.target_depth)
        assert scoped_entry.unservable == unscoped_entry.unservable


def test_recommend_next_flags_a_bank_absent_skill_as_unservable_without_dropping_it(
    banks_dir: Path, facade: serving.ServingFacade
) -> None:
    """`facade`/`banks_dir` (module-level fixtures) hold instances for only
    FRAC.ADD.LIKE. CA/ONTARIO grade 4 placement starts at FRAC.UNIT (see
    tests/test_engine.py's test_fresh_student_placement_starts_at_...) —
    this bank has none, so that entry must still appear in the ranked
    list, flagged unservable, never silently omitted."""
    bank_activate.activate(banks_dir, "build-a")
    student_id = "stu-recommend-unservable"
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    result = facade.recommend_next(
        student_id, "CA", "ONTARIO", 4, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now
    )
    placement_entries = [r for r in result.recommendations if r.skill_id == "FRAC.UNIT"]
    assert len(placement_entries) == 1
    assert placement_entries[0].reason == "placement"
    assert placement_entries[0].unservable is True


def test_recommend_next_and_get_progress_succeed_for_second_linked_consumer_while_first_holds_the_session(
    banks_dir: Path, linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """The whole reason this unit exists (R20/F4): consumer A opens and
    holds the student's one active serving session; consumer B, linked to
    the same student but holding no session of its own, can still
    successfully call recommend_next and get_progress for that student —
    the session-less reads never contend with, or require, A's live
    session."""
    bank_activate.activate(banks_dir, "build-a")
    student_id = identity_registry.register_student({"guardian_email": "d@example.com"}, consumer_id="partner.a")
    identity_registry.link_student(student_id, {"guardian_email": "d@example.com"}, consumer_id="partner.b")

    session = _open(linked_facade, "sess-held-by-a", student_id, consumer_id="partner.a")
    now = log_module.parse_timestamp("2026-07-27T12:00:00")

    progress = linked_facade.get_progress(student_id, consumer_id="partner.b", now=now)
    assert progress.student_id == student_id

    recommendations = linked_facade.recommend_next(
        student_id, "CA", "ONTARIO", 4, consumer_id="partner.b", now=now
    )
    assert recommendations.student_id == student_id
    assert recommendations.recommendations

    # partner.a's session is completely unaffected by B's session-less reads.
    assert linked_facade.sessions.get("sess-held-by-a") is session
