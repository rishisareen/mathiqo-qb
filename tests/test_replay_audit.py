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
from orchestrator import constants, engine, log as log_module, serving, session
from orchestrator.identity import IdentityRegistry
from orchestrator.state import StudentModelStore

NOW = datetime(2026, 7, 27, 12, 0, 0)

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SIMPLIFY_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.SIMPLIFY.DIRECT.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"
SIMPLIFY_SKILL_ID = "FRAC.SIMPLIFY"
CONSUMER_ID = constants.CONSUMER_ID_INTERNAL

# FRAC.ADD.LIKE's own mapping (content/fractions/mappings.yaml) -- the batch
# planner (plan_ladder -> serving_ceiling) needs a real country/curriculum/
# grade mapping to compute a serving ceiling for the skill it's planning.
COUNTRY = "CA"
CURRICULUM = "ONTARIO"
GRADE = 5


def _templates_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    return dest


def _two_skill_templates_dir(tmp_path: Path) -> Path:
    """A templates dir covering two distinct skills (FRAC.ADD.LIKE,
    FRAC.SIMPLIFY) -- for the U13 "multi-skill, multi-event log passes the
    theta-chain check with no false positives" scenario, where a single-skill
    fixture wouldn't exercise the chain tracker's per-(student, skill) keying
    at all."""
    dest = tmp_path / "templates_two_skill"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    shutil.copy(SIMPLIFY_TEMPLATE, dest / SIMPLIFY_TEMPLATE.name)
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
    skill_id: str = SKILL_ID,
) -> engine.EngineState:
    """Directly logs `count` correct attempts against `bank`'s own pool via
    session._apply_and_log (the same compose/log/consume path a real
    session uses) — bypasses the interactive loop entirely for exact
    control over which instances end up in the log. `skill_id` defaults to
    this module's usual single-skill fixture (SKILL_ID); the multi-skill
    theta-chain test below passes it explicitly to log a second skill's
    events into the same log."""
    pool = bank.instances_by_skill[skill_id]
    for i in range(count):
        instance = pool[i % len(pool)]
        serve = engine.Serve(instance["item_instance_id"], skill_id, instance["difficulty"], "practice", "test")
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


# --- U13: theta-interleaving chain check -------------------------------------


def _tamper_event_at(log_path: Path, index: int, **overrides) -> dict:
    """Like `_tamper_last_event`, but targets an arbitrary line by index —
    the theta-chain break this unit's tests need lives on a *non-last* event
    (its successor is what actually notices the break), unlike every
    pre-existing tamper test in this file, which only ever corrupts the
    final line."""
    lines = log_path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[index])
    event.update(overrides)
    lines[index] = json.dumps(event, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return event


def test_tampered_theta_before_breaks_chain_and_is_flagged_naming_student_skill_event(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    """A hand-corrupted log — one event's `theta_before` overwritten to a
    value that disagrees with its immediately preceding same-skill event's
    real `theta_after` — is flagged by the new interleaving check, naming
    the specific student/skill/event; this is exactly the signature two
    writers racing on the same student (KTD6's single-active-session
    protection) would leave."""
    bank = _build_on_disk(banks_dir, "audit-theta-chain", _templates_dir(tmp_path), pool_cap=5)
    student_id = "stu-chain-break"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=4)

    events_before = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    broken_event = _tamper_event_at(log_path, 1, theta_before=events_before[1]["theta_before"] + 999.0)

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert summaries == {}
    chain_failures = [f for f in failures if f.kind == "theta_chain"]
    assert len(chain_failures) == 1
    failure = chain_failures[0]
    assert failure.event_id == broken_event["event_id"]
    assert student_id in failure.reason
    assert SKILL_ID in failure.reason
    assert "theta_chain" in failure.reason
    # Names both disagreeing values, not just "something broke".
    assert str(broken_event["theta_before"]) in failure.reason
    assert str(events_before[0]["theta_after"]) in failure.reason


def test_tampered_theta_after_breaks_the_next_events_chain_check(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    """The symmetric corruption: tampering an event's own `theta_after`
    (rather than a later event's `theta_before`) still breaks the chain —
    the *next* same-skill event's `theta_before` no longer matches its real
    predecessor's `theta_after`, and that successor event is the one named."""
    bank = _build_on_disk(banks_dir, "audit-theta-chain-2", _templates_dir(tmp_path), pool_cap=5)
    student_id = "stu-chain-break-2"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=4)

    events_before = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    _tamper_event_at(log_path, 1, theta_after=events_before[1]["theta_after"] + 999.0)

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert summaries == {}
    chain_failures = [f for f in failures if f.kind == "theta_chain"]
    assert len(chain_failures) == 1
    # Event index 2 (the one after the tampered index 1) is the one whose
    # theta_before now disagrees with the tampered theta_after.
    assert chain_failures[0].event_id == events_before[2]["event_id"]


def test_clean_single_skill_log_first_event_matches_fresh_theta(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    """The very first event ever logged for a (student, skill) pair must
    carry theta_before == SkillState()'s fresh/default theta (0.0) — no
    predecessor event exists, so the check falls back to that default."""
    bank = _build_on_disk(banks_dir, "audit-theta-fresh", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-fresh"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=1)

    first_event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert first_event["theta_before"] == 0.0

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, _summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)
    assert failures == []


def test_clean_multi_skill_multi_event_log_passes_theta_chain_check(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    """A clean log spanning two distinct skills, interleaved event-by-event,
    passes the interleaving check with no false positives — proves the
    per-(student_id, skill_id) chain tracker doesn't confuse one skill's
    theta sequence with another's just because their events interleave in
    file order (don't only test the failure case)."""
    templates_dir = _two_skill_templates_dir(tmp_path)
    bank = _build_on_disk(banks_dir, "audit-multi-skill", templates_dir, pool_cap=5)
    student_id = "stu-multi-skill"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    engine_state = _bypass_placement()

    # Interleave: LIKE, SIMPLIFY, LIKE, SIMPLIFY, LIKE — each call appends to
    # the same log/store/engine_state, exactly as a real session's mixed
    # practice across skills would.
    for skill_id, count in [(SKILL_ID, 1), (SIMPLIFY_SKILL_ID, 1), (SKILL_ID, 1), (SIMPLIFY_SKILL_ID, 1), (SKILL_ID, 1)]:
        engine_state = _log_attempts(
            log_path, content, bank, student_id, "sess-1", store, engine_state, count=count, skill_id=skill_id
        )

    events = list(log_module.iter_events(log_path))
    assert [tuple(e["skill_ids"]) for e in events] == [
        (SKILL_ID,), (SIMPLIFY_SKILL_ID,), (SKILL_ID,), (SIMPLIFY_SKILL_ID,), (SKILL_ID,),
    ]

    bank_lookup = engine.make_bank_lookup(banks_dir)
    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)

    assert failures == []
    assert student_id in summaries
    assert SKILL_ID in summaries[student_id].skills
    assert SIMPLIFY_SKILL_ID in summaries[student_id].skills


# --- U13: real batch-session fixture -- replay matches live progress,
# unattempted delivered items stay out of seen-state (R11/AE3) ---------------


def test_batch_session_replay_matches_live_progress_and_unattempted_items_stay_unseen(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path
) -> None:
    """A real fetch_batch/submit_attempt session (not the lower-level
    session._apply_and_log path the rest of this file uses) where only some
    of a delivered batch's items are ever attempted: replays to the exact
    same progress summary the live facade reported at the time (this unit's
    own "audit run over a real recorded session fixture" bar), and the
    unattempted items appear nowhere in the replayed seen-state -- cross-
    checked against the live session's own engine_state.seen_history at the
    same point, not just an isolated assertion (R11, AE3)."""
    bank = _build_on_disk(banks_dir, "audit-batch", _templates_dir(tmp_path), pool_cap=10)
    student_id = "stu-batch-audit"
    session_id = "sess-batch-audit"
    logs_dir = tmp_path / "logs"
    log_path = logs_dir / f"{student_id}.jsonl"

    bank_lookup = engine.make_bank_lookup(banks_dir)
    facade = serving.ServingFacade(
        banks_dir=banks_dir, logs_dir=logs_dir, content=content, bank_lookup=bank_lookup
    )
    session_obj = serving.Session(
        session_id=session_id,
        student_id=student_id,
        bank=bank,
        log_path=log_path,
        store=StudentModelStore(),
        engine_state=_bypass_placement(),
        country=COUNTRY,
        curriculum=CURRICULUM,
        grade=GRADE,
    )

    result = facade.fetch_batch(session_obj, SKILL_ID, CONSUMER_ID, batch_size=5)
    assert result.count_returned >= 3, "fixture pool must be large enough to leave items unattempted"

    attempted_items = result.items[:-1]
    unattempted_items = result.items[-1:]
    assert attempted_items and unattempted_items

    for i, item in enumerate(attempted_items):
        outcome = facade.submit_attempt(
            session_obj,
            attempt_id=f"attempt-{i}",
            skill_id=SKILL_ID,
            instance=item.instance,
            response_raw=item.instance["answer"],
            response_parsed=item.instance["answer"],
            correct=True,
            misconception_id=None,
            hints_used=0,
            hint_auto_offered=False,
            time_taken_sec=5.0,
            timed_out=False,
            engaged=True,
            session_last_event=False,
            context=item.context,
            consumer_id=CONSUMER_ID,
            now=NOW,
        )
        assert outcome.event is not None, outcome.rejection_reason

    # The session is then abandoned without ever attempting the remaining
    # delivered item(s) -- session.delivered is purely in-memory (R11) and
    # dies here, nothing durable ever recorded it.

    live_state = session_obj.store.peek(student_id, SKILL_ID)
    live_summary = serving.build_progress_summary(session_obj.store, session_obj.engine_state, student_id, NOW)

    failures, summaries = replay_audit.audit_log(log_path, content, bank_lookup, NOW)
    assert failures == []
    reproduced = summaries[student_id].skills[SKILL_ID]
    assert reproduced.theta == pytest.approx(live_state.theta)
    assert reproduced.n == pytest.approx(live_state.n)
    assert reproduced.certified_depth == live_state.certified_depth
    assert reproduced.seen_count == live_summary.skills[SKILL_ID].seen_count

    # Same check through the live facade's own session-less get_progress, as
    # an external partner reading progress would see it.
    facade_progress = facade.get_progress(student_id, CONSUMER_ID, NOW)
    assert facade_progress.skills[SKILL_ID].theta == pytest.approx(live_state.theta)

    # R11/AE3: the unattempted item(s)' variant_signature never enters
    # seen-state -- neither the live session's own engine_state (never
    # touched by fetch_batch, per batch.py's KTD2 scratch-clone discipline)
    # nor the replayed one derived independently from the log.
    replayed_store, replayed_engine_state = engine.rebuild_session_state(log_path, content, bank_lookup)
    live_seen = set(session_obj.engine_state.seen_history.get(SKILL_ID, []))
    replayed_seen = set(replayed_engine_state.seen_history.get(SKILL_ID, []))
    assert live_seen == replayed_seen
    assert live_seen, "the attempted items must actually have marked seen-state"

    unattempted_signatures = {item.instance["variant_signature"] for item in unattempted_items}
    assert unattempted_signatures.isdisjoint(live_seen)
    assert unattempted_signatures.isdisjoint(replayed_seen)
    attempted_signatures = {item.instance["variant_signature"] for item in attempted_items}
    assert attempted_signatures <= live_seen


# --- U13: --identity-registry (identity-aware paths) -------------------------


def _write_registry_entry(registry_path: Path, student_id: str, consumer_id: str) -> None:
    """Writes a real two-event registry log — `student_registered` then
    `consumer_linked`, the same pair `IdentityRegistry.register_student`
    itself appends — directly under `student_id` (the log's own student_id
    in these tests), rather than going through `register_student` (which
    always mints its own fresh opaque id and so can never be pointed at an
    id chosen elsewhere)."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    now = NOW.isoformat()
    with registry_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_kind": "student_registered", "student_id": student_id,
            "attributes": {"family_code": "FAM-1"}, "consumer_id": consumer_id, "timestamp": now,
        }, sort_keys=True) + "\n")
        f.write(json.dumps({
            "event_kind": "consumer_linked", "student_id": student_id,
            "consumer_id": consumer_id, "link_basis": None, "timestamp": now,
        }, sort_keys=True) + "\n")


def test_identity_registry_flag_annotates_linked_consumers(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path, capsys
) -> None:
    """`--identity-registry` given: the audit's printed report reflects the
    linked-consumer annotation for a student who has one."""
    bank = _build_on_disk(banks_dir, "audit-identity-1", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-identity-linked"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)

    registry_path = tmp_path / "identity" / "registry.jsonl"
    _write_registry_entry(registry_path, student_id, "partner.acme")

    exit_code = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
        "--identity-registry", str(registry_path),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "linked consumers: partner.acme" in captured.out


def test_identity_registry_flag_given_but_student_unregistered_degrades_gracefully(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path, capsys
) -> None:
    """`--identity-registry` given, pointing at a real (but empty-for-this-
    student) registry file: no crash, and the annotation reads "(none)"
    rather than silently omitting the section or misreporting a link that
    doesn't exist."""
    bank = _build_on_disk(banks_dir, "audit-identity-2", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-identity-unregistered"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)

    # A real registry file exists on disk, but has an entry for some *other*
    # student_id entirely -- this student_id has none.
    registry_path = tmp_path / "identity" / "registry.jsonl"
    _write_registry_entry(registry_path, "stu-someone-else", "partner.acme")

    exit_code = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
        "--identity-registry", str(registry_path),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "linked consumers: (none)" in captured.out
    assert "partner.acme" not in captured.out


def test_identity_registry_flag_pointing_at_missing_file_degrades_gracefully(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path, capsys
) -> None:
    """`--identity-registry` given a path that doesn't exist on disk at all
    -- IdentityRegistry's own "missing file is empty state" contract means
    this is not an error; the audit still runs and every student's
    annotation reads "(none)"."""
    bank = _build_on_disk(banks_dir, "audit-identity-3", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-identity-no-registry-file"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)

    registry_path = tmp_path / "identity" / "does-not-exist.jsonl"

    exit_code = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
        "--identity-registry", str(registry_path),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "linked consumers: (none)" in captured.out


def test_identity_registry_flag_omitted_produces_no_annotation(
    tmp_path: Path, content: engine.ContentIndex, banks_dir: Path, capsys
) -> None:
    """`--identity-registry` omitted entirely: the audit behaves exactly as
    it did before this unit -- no "linked consumers" text anywhere in the
    report, on a log for a student who *is* linked in a registry that
    simply was never named on the command line."""
    bank = _build_on_disk(banks_dir, "audit-identity-4", _templates_dir(tmp_path), pool_cap=3)
    student_id = "stu-identity-flag-omitted"
    log_path = tmp_path / "logs" / f"{student_id}.jsonl"
    store = StudentModelStore()
    _log_attempts(log_path, content, bank, student_id, "sess-1", store, _bypass_placement(), count=2)

    registry_path = tmp_path / "identity" / "registry.jsonl"
    _write_registry_entry(registry_path, student_id, "partner.acme")  # exists, but never passed to main()

    exit_code = replay_audit.main([
        "--log", str(log_path), "--banks-dir", str(banks_dir),
        "--content", str(REPO_ROOT / "content" / "fractions"), "--now", NOW.isoformat(),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "linked consumers" not in captured.out


def test_identity_registry_linked_consumers_method_directly(tmp_path: Path) -> None:
    """Unit coverage for the new `IdentityRegistry.linked_consumers` method
    itself (added by this unit): sorted enumeration for a linked student,
    empty list (not an error) for an unregistered one."""
    registry_path = tmp_path / "identity" / "registry.jsonl"
    registry = IdentityRegistry(registry_path)
    student_id = registry.register_student(attributes={"family_code": "FAM-1"}, consumer_id="partner.zeta")
    registry.link_student(student_id, attributes={"family_code": "FAM-1"}, consumer_id="partner.alpha")

    assert registry.linked_consumers(student_id) == ["partner.alpha", "partner.zeta"]
    assert registry.linked_consumers("stu-never-registered") == []
