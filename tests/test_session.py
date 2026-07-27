"""Tests for orchestrator/session.py — the U6 interactive session runner:
KTD8 numeric parsing, deterministic MCQ shuffling, the hint-ladder/timeout
state machine (`collect_response`, exercised via a scripted virtual-clock
`InputSource`), the live feedback-interface composition (`_apply_and_log`,
mirroring log.py's `apply_event`), and the end-to-end scripted-session loop
(`run_session`) including restart/replay equivalence.

Fixtures build a real bank from content/templates via tools/bank_build.py
(session-scoped, built once) and load the real content/fractions/*.yaml
graph — same convention as tests/test_engine.py, which this file leans on
directly for several skill/template facts (FRAC.ADD.UNLIKE's prerequisites,
CA grade-4 placement start, etc.).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from conftest import _bypass_placement, _numeric_only_bank, _single_skill_bank, find_tagged_instance
from orchestrator import certification, constants, engine, log as log_module, session
from orchestrator.state import SkillState, StudentModelStore

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content" / "fractions"
TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
SCHEMAS_DIR = REPO_ROOT / "schemas"

NOW = datetime(2026, 7, 27, 12, 0, 0)

# `content` and `bank` (session-scoped fixtures) and the small bank/engine-
# state helpers above live in tests/conftest.py, shared with test_engine.py
# and test_simulation.py — see that module's docstring.


@pytest.fixture(scope="session")
def attempt_event_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / "attempt-event.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _notifier() -> tuple[list, "callable"]:
    messages: list = []

    def notify(text: str) -> None:
        messages.append(text)

    return messages, notify


# ===========================================================================
# KTD8: numeric-entry parsing
# ===========================================================================


def test_parse_numeric_response_integer() -> None:
    parsed = session.parse_numeric_response("6")
    assert parsed.value == 6
    assert parsed.literal_form == "6"


def test_parse_numeric_response_fraction() -> None:
    parsed = session.parse_numeric_response("5/6")
    assert parsed.value == session.Fraction(5, 6)
    assert parsed.literal_form == "5/6"


def test_parse_numeric_response_mixed_number() -> None:
    parsed = session.parse_numeric_response("1 5/6")
    assert parsed.value == session.Fraction(11, 6)
    assert parsed.literal_form == "11/6"


def test_parse_numeric_response_mixed_number_tolerates_extra_whitespace() -> None:
    parsed = session.parse_numeric_response("  2   3/4  ")
    assert parsed.value == session.Fraction(11, 4)


@pytest.mark.parametrize("raw", ["0.83", "abc", "", "   ", "1/0", "1 2 3", "1/2/3"])
def test_parse_numeric_response_rejects_garbage(raw: str) -> None:
    assert session.parse_numeric_response(raw) is None


# ===========================================================================
# KTD8: grading (value equality; simplest_form_only literal check)
# ===========================================================================


def _synthetic_instance(**overrides) -> dict:
    base = {
        "item_instance_id": "TPL.TEST#dev#00000",
        "answer": "1/2",
        "simplest_form_only": False,
        "distractors": [{"value": "1/3", "misconception_id": "MISC.TEST.X", "unsimplified": False}],
    }
    base.update(overrides)
    return base


def test_grade_numeric_response_simplest_form_only_rejects_unreduced_equal_value() -> None:
    instance = _synthetic_instance(simplest_form_only=True)
    parsed = session.parse_numeric_response("2/4")
    correct, misc, response_parsed = session.grade_numeric_response(instance, parsed)
    assert correct is False
    assert response_parsed == "2/4"


def test_grade_numeric_response_simplest_form_only_accepts_reduced_value() -> None:
    instance = _synthetic_instance(simplest_form_only=True)
    parsed = session.parse_numeric_response("1/2")
    correct, misc, _ = session.grade_numeric_response(instance, parsed)
    assert correct is True
    assert misc is None


def test_grade_numeric_response_not_simplest_form_only_accepts_unreduced_equal_value() -> None:
    instance = _synthetic_instance(simplest_form_only=False)
    parsed = session.parse_numeric_response("2/4")
    correct, _, _ = session.grade_numeric_response(instance, parsed)
    assert correct is True


def test_grade_numeric_response_matches_tagged_distractor(bank: engine.BankIndex) -> None:
    instance, value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    assert instance["question_type"] == "numeric_entry"
    parsed = session.parse_numeric_response(value)
    correct, misc, response_parsed = session.grade_numeric_response(instance, parsed)
    assert correct is False
    assert misc == "MISC.FRAC.ADD-ACROSS"
    assert response_parsed == value


def test_grade_numeric_response_untagged_wrong_value_is_none(bank: engine.BankIndex) -> None:
    instance, _ = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    parsed = session.parse_numeric_response("999999/1000003")  # never a real answer or distractor here
    correct, misc, _ = session.grade_numeric_response(instance, parsed)
    assert correct is False
    assert misc is None


def test_grade_numeric_response_mixed_number_correct_on_mixed_number_template(bank: engine.BankIndex) -> None:
    """KTD8 test scenario: a mixed-number answer like "1 5/6" is accepted and
    correct on the mixed-number template TPL.FRAC.MIXED.ADD.DIRECT.001."""
    instance = bank.instances_by_skill["FRAC.MIXED.ADD"][0]
    assert instance["simplest_form_only"] is False  # "mixed number or improper fraction... accepted"
    answer = session.parse_numeric_response(instance["answer"])
    whole, remainder = divmod(answer.value.numerator, answer.value.denominator)
    mixed_text = f"{whole} {remainder}/{answer.value.denominator}"
    parsed = session.parse_numeric_response(mixed_text)
    correct, misc, _ = session.grade_numeric_response(instance, parsed)
    assert correct is True
    assert misc is None


# ===========================================================================
# MCQ: deterministic shuffle, grading
# ===========================================================================


def _mcq_instance(bank_index: engine.BankIndex) -> dict:
    for instance in bank_index.instances_by_skill["FRAC.ADD.UNLIKE"]:
        if instance["question_type"] == "mcq_4":
            return instance
    raise AssertionError("expected an mcq_4 FRAC.ADD.UNLIKE instance")


def test_build_mcq_options_shuffle_is_deterministic_per_instance(bank: engine.BankIndex) -> None:
    instance = _mcq_instance(bank)
    first = session.build_mcq_options(instance)
    second = session.build_mcq_options(instance)
    assert [(o.label, o.value) for o in first] == [(o.label, o.value) for o in second]
    assert {o.value for o in first} == {instance["answer"]} | {d["value"] for d in instance["distractors"]}


def test_grade_mcq_response_correct_option() -> None:
    opt = session.MCQOption(label="A", value="1/2", misconception_id=None, is_correct=True)
    correct, misc, response_parsed = session.grade_mcq_response(opt)
    assert correct is True
    assert misc is None
    assert response_parsed == "1/2"


def test_grade_mcq_response_tagged_distractor_option_real_bank(bank: engine.BankIndex) -> None:
    instance = _mcq_instance(bank)
    options = session.build_mcq_options(instance)
    tagged = next(o for o in options if o.misconception_id == "MISC.FRAC.ADD-ACROSS")
    correct, misc, response_parsed = session.grade_mcq_response(tagged)
    assert correct is False
    assert misc == "MISC.FRAC.ADD-ACROSS"
    assert response_parsed == tagged.value


def test_make_mcq_parser_maps_label_case_insensitively(bank: engine.BankIndex) -> None:
    instance = _mcq_instance(bank)
    options = session.build_mcq_options(instance)
    parse = session.make_mcq_parser(options)
    assert parse(options[0].label.lower()) is options[0]
    assert parse("not-a-label") is None


# ===========================================================================
# collect_response: hint ladder, timeout, reprompt-without-consuming
# ===========================================================================


def test_collect_response_garbage_reprompts_without_consuming() -> None:
    messages, notify = _notifier()
    io = session.ScriptedInputSource([(0.0, "banana"), (0.0, "5/6")])
    result = session.collect_response(io, ["hint text"], estimated_time_sec=100.0, notify=notify, parse=session.parse_numeric_response)
    assert isinstance(result, session.CollectedResponse)
    assert result.response_raw == "5/6"
    assert any("couldn't understand" in m for m in messages)


def test_collect_response_decimal_input_rejected_re_prompts() -> None:
    io = session.ScriptedInputSource([(0.0, "0.83"), (0.0, "5/6")])
    result = session.collect_response(io, ["hint text"], estimated_time_sec=100.0, notify=lambda _t: None, parse=session.parse_numeric_response)
    assert result.response_raw == "5/6"


def test_collect_response_hint_command_increments_hints_used_and_demotes() -> None:
    io = session.ScriptedInputSource([(0.0, "hint"), (0.0, "5/6")])
    result = session.collect_response(io, ["a hint"], estimated_time_sec=100.0, notify=lambda _t: None, parse=session.parse_numeric_response)
    assert result.hints_used == 1
    assert result.hint_auto_offered is False


def test_collect_response_auto_offer_fires_once_then_response_is_collected() -> None:
    """estimated_time_sec=10 -> auto-offer boundary at 15s, timeout at 30s.
    The response arrives at virtual t=20s: past the auto-offer boundary
    (so the offer must fire first) but still inside the timeout window."""
    io = session.ScriptedInputSource([(20.0, "5/6")])
    messages, notify = _notifier()
    result = session.collect_response(io, ["h1", "h2"], estimated_time_sec=10.0, notify=notify, parse=session.parse_numeric_response)
    assert isinstance(result, session.CollectedResponse)
    assert result.response_raw == "5/6"
    assert result.hint_auto_offered is True
    assert result.hints_used == 1
    assert result.time_taken_sec == pytest.approx(20.0)
    assert any("auto" in m for m in messages)


def test_collect_response_no_interaction_times_out() -> None:
    io = session.ScriptedInputSource([])  # nothing ever arrives
    result = session.collect_response(io, ["h1"], estimated_time_sec=10.0, notify=lambda _t: None, parse=session.parse_numeric_response)
    assert isinstance(result, session.TimedOutResponse)
    assert result.engaged is False
    assert result.time_taken_sec == pytest.approx(constants.TIMEOUT_MULTIPLIER * 10.0)
    assert result.hint_auto_offered is True  # boundary crossed on the way to timeout


def test_collect_response_engaged_then_times_out() -> None:
    io = session.ScriptedInputSource([(0.0, "hint")])  # one interaction, then silence
    result = session.collect_response(io, ["h1"], estimated_time_sec=10.0, notify=lambda _t: None, parse=session.parse_numeric_response)
    assert isinstance(result, session.TimedOutResponse)
    assert result.engaged is True


def test_collect_response_quit_raises() -> None:
    io = session.ScriptedInputSource([(0.0, "quit")])
    with pytest.raises(session.QuitRequested):
        session.collect_response(io, ["h1"], estimated_time_sec=10.0, notify=lambda _t: None, parse=session.parse_numeric_response)


# ===========================================================================
# _apply_and_log: the live feedback-interface composition
# ===========================================================================


def _fresh_serve(instance: dict, context: str, reason: str) -> "engine.Serve":
    return engine.Serve(instance["item_instance_id"], instance["skill_ids"][0], instance["difficulty"], context, reason)


def test_apply_and_log_correct_unassisted_certifies_and_logs(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex, attempt_event_validator
) -> None:
    instance, distractor_value = find_tagged_instance(bank, "FRAC.ADD.UNLIKE", "MISC.FRAC.ADD-ACROSS")
    student_id = "stu-apply-cert"
    store = StudentModelStore()
    store.replace_records(student_id, {"FRAC.ADD.UNLIKE": SkillState(theta=0.6, n=2.0)})
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "practice", "test")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=12.0, timed_out=False, engaged=True,
        session_last_event=False, now=NOW,
    )
    assert result is not None
    theta_before, theta_after, new_engine_state, event = result
    assert theta_after > theta_before
    assert store.peek(student_id, "FRAC.ADD.UNLIKE").certified_depth is not None

    errors = list(attempt_event_validator.iter_errors(event))
    assert errors == [], "; ".join(e.message for e in errors)

    logged = list(log_module.iter_events(log_path))
    assert len(logged) == 1
    assert logged[0]["correct"] is True


def test_apply_and_log_engaged_timeout_scores_wrong_and_logs(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex, attempt_event_validator
) -> None:
    instance = bank.instances_by_skill["FRAC.ADD.LIKE"][0]
    student_id = "stu-apply-timeout"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "practice", "test")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw="", response_parsed=None, correct=False, misconception_id=None,
        hints_used=1, hint_auto_offered=True, time_taken_sec=30.0, timed_out=True, engaged=True,
        session_last_event=False, now=NOW,
    )
    assert result is not None
    _theta_before, theta_after, _engine_state, event = result
    assert event["correct"] is False
    assert event["timed_out"] is True
    errors = list(attempt_event_validator.iter_errors(event))
    assert errors == []
    assert len(list(log_module.iter_events(log_path))) == 1


def test_apply_and_log_no_interaction_last_event_is_discarded_and_never_logged(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    instance = bank.instances_by_skill["FRAC.ADD.LIKE"][0]
    student_id = "stu-apply-discard"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "practice", "test")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw="", response_parsed=None, correct=False, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=180.0, timed_out=True, engaged=False,
        session_last_event=True, now=NOW,
    )
    assert result is None
    assert store.peek(student_id, "FRAC.ADD.LIKE").n == 0.0  # nothing applied
    assert not log_path.exists()  # nothing logged (log.py's "what gets logged" decision)


def test_apply_and_log_mid_session_no_interaction_timeout_scores_zero(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """The "mid-session no-interaction timeout" branch (session_last_event
    explicitly False) — see session.py's module docstring on how this
    differs from the live CLI's own always-ends-the-session policy."""
    instance = bank.instances_by_skill["FRAC.ADD.LIKE"][0]
    student_id = "stu-apply-midsession"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "practice", "test")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw="", response_parsed=None, correct=False, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=180.0, timed_out=True, engaged=False,
        session_last_event=False, now=NOW,
    )
    assert result is not None
    _theta_before, theta_after, _engine_state, event = result
    assert event["correct"] is False
    assert event["timed_out"] is True
    assert len(list(log_module.iter_events(log_path))) == 1


def test_apply_and_log_review_pass_leaves_theta_unchanged_and_extends_interval(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    low_guess = [i for i in bank.instances_by_skill["FRAC.ADD.LIKE"] if i["question_type"] in constants.LOW_GUESS_FORMATS]
    instance = low_guess[0]
    student_id = "stu-review-pass"
    store = StudentModelStore()
    store.replace_records(
        student_id,
        {"FRAC.ADD.LIKE": SkillState(theta=0.6, n=5.0, certified_depth=0.2, review_due=NOW - timedelta(days=1))},
    )
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "review", "review")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=10.0, timed_out=False, engaged=True,
        session_last_event=False, now=NOW,
    )
    assert result is not None
    theta_before, theta_after, _engine_state, event = result
    assert theta_before == theta_after == 0.6  # review pass never moves theta
    new_state = store.peek(student_id, "FRAC.ADD.LIKE")
    assert new_state.review_due > NOW + timedelta(days=7)  # interval grew (x2.5)
    assert event["context"] == "review"


def test_apply_and_log_review_fail_moves_theta_and_lapses(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    low_guess = [i for i in bank.instances_by_skill["FRAC.ADD.LIKE"] if i["question_type"] in constants.LOW_GUESS_FORMATS]
    instance = low_guess[0]
    student_id = "stu-review-fail"
    store = StudentModelStore()
    store.replace_records(
        student_id,
        {"FRAC.ADD.LIKE": SkillState(theta=0.6, n=5.0, certified_depth=0.2, review_due=NOW - timedelta(days=1))},
    )
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    serve = _fresh_serve(instance, "review", "review")

    result = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-1", log_path, serve, instance,
        response_raw="999999/7", response_parsed="999999/7", correct=False, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=10.0, timed_out=False, engaged=True,
        session_last_event=False, now=NOW,
    )
    assert result is not None
    theta_before, theta_after, _engine_state, _event = result
    assert theta_after < theta_before
    new_state = store.peek(student_id, "FRAC.ADD.LIKE")
    assert new_state.lapsed is True
    assert new_state.certified_depth == 0.2  # lapsed blocks dependents, does not revoke (§6 point 3)


# ===========================================================================
# run_session: end-to-end scripted smoke test + restart/replay equivalence
# ===========================================================================


def test_run_session_scripted_smoke_updates_state_logs_schema_valid_events_and_replays(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex, attempt_event_validator
) -> None:
    numeric_bank = _numeric_only_bank(bank)
    student_id = "stu-smoke"
    store = StudentModelStore()
    engine_state = engine.EngineState()  # truly fresh, matches rebuild_engine_state's own starting point
    log_path = tmp_path / "log.jsonl"
    session_id = "sess-smoke"

    # Four wrong-but-parseable numeric answers, then quit — a value that can
    # never coincide with a real (small-integer) template's answer or a
    # tagged distractor, so every attempt is deterministically an untagged
    # wrong (exercising R13's step-down/prereq-probe diagnosis path too).
    script = [(0.0, "999999/1000003")] * 4 + [(0.0, "quit")]
    io = session.ScriptedInputSource(script)
    _messages, notify = _notifier()

    summary = session.run_session(
        store, engine_state, content, numeric_bank, student_id, "CA", 4, session_id, log_path, io, notify, NOW
    )

    assert summary.items_served == 4
    logged = list(log_module.iter_events(log_path))
    assert len(logged) == 4
    for event in logged:
        errors = list(attempt_event_validator.iter_errors(event))
        assert errors == [], "; ".join(e.message for e in errors)
        assert event["correct"] is False
        assert event["question_type"] == "numeric_entry"

    assert summary.theta_deltas  # at least one skill was touched and reported
    for skill_id, (before, after) in summary.theta_deltas.items():
        assert before == 0.0  # fresh student
        assert store.peek(student_id, skill_id).theta == pytest.approx(after)

    # Restart: replay from scratch and confirm the student-model state — the
    # part that actually drives pedagogy — is exactly reproduced. (Full
    # EngineState equality is deliberately NOT asserted here: engine.py's
    # own design has select_next mark an instance "seen" at selection time
    # *and* consume_event mark it seen again once the outcome is fed back,
    # so a live full round-trip session's seen_history double-counts
    # relative to a pure replay, which only ever calls consume_event; and
    # placement_initialized only ever flips True via select_next's
    # _init_placement, which pure replay never calls. Both are pre-existing
    # U5 behavior, out of this unit's scope — replay-equivalence of the
    # pending diagnosis queues is covered precisely, and correctly, by
    # test_run_session_restart_preserves_queued_stepdown_probe below.)
    replayed_store = log_module.replay(log_path)
    replayed_engine_state = engine.rebuild_engine_state(log_path, content, numeric_bank)

    assert replayed_store.records_for(student_id) == store.records_for(student_id)
    # A fresh run_session call against the replayed state must be able to
    # continue serving (same content/bank, no crash) as a final smoke check.
    result2 = engine.select_next(replayed_store, replayed_engine_state, content, numeric_bank, student_id, "CA", 4, NOW)
    assert isinstance(result2.engine_state, engine.EngineState)


def test_run_session_restart_preserves_queued_stepdown_probe(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Quitting immediately after an untagged deep failure, then restarting,
    still serves the queued step-down probe next (KTD11 + the abandonment/
    quit interaction).

    Item 1 (the untagged deep failure that queues the probe) is applied
    directly via `_apply_and_log` rather than driven through `run_session`'s
    own `select_next` — mirroring how tests/test_engine.py's own analogous
    scenario (test_untagged_deep_wrong_queues_a_stepdown_probe_at_the_easier_depth)
    constructs its `Serve` directly instead of relying on which skill the
    R12 frontier-practice tie-break would organically pick across the whole
    graph. Once the probe is queued, though, it strictly outranks frontier
    practice (R12 step 3 vs step 6) regardless of tie-breaks, so the live
    `run_session` call for "item 2" is guaranteed to be served exactly that
    probe — the part actually under test here (quitting before answering it
    must not log or apply anything for it).
    """
    student_id = "stu-restart-stepdown"
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"

    deep_instance = next(
        i for i in bank.instances_by_skill["FRAC.ADD.UNLIKE"] if i["difficulty"] == pytest.approx(0.5)
    )
    serve1 = _fresh_serve(deep_instance, "practice", "test")
    setup = session._apply_and_log(
        store, engine_state, content, bank, student_id, "sess-setup", log_path, serve1, deep_instance,
        response_raw="999999/1000003", response_parsed="999999/1000003", correct=False, misconception_id=None,
        hints_used=0, hint_auto_offered=False, time_taken_sec=10.0, timed_out=False, engaged=True,
        session_last_event=False, now=NOW,
    )
    assert setup is not None
    _tb, _ta, engine_state, _event = setup
    assert len(engine_state.stepdown_queue) == 1
    assert engine_state.stepdown_queue[0].skill_id == "FRAC.ADD.UNLIKE"

    # Item 2: the live session is served the queued probe; quit immediately
    # without answering it.
    io = session.ScriptedInputSource([(0.0, "quit")])
    summary = session.run_session(
        store, engine_state, content, bank, student_id, "CA", 6, "sess-2", log_path, io, lambda _t: None, NOW
    )
    assert summary.items_served == 0  # quit before ever submitting a response

    logged = list(log_module.iter_events(log_path))
    assert len(logged) == 1  # only item 1's event; item 2 (quit) was never logged

    replayed_store = log_module.replay(log_path)
    replayed_engine_state = engine.rebuild_engine_state(log_path, content, bank)
    assert len(replayed_engine_state.stepdown_queue) == 1
    assert replayed_engine_state.stepdown_queue[0].skill_id == "FRAC.ADD.UNLIKE"

    result = engine.select_next(replayed_store, replayed_engine_state, content, bank, student_id, "CA", 6, NOW)
    assert result.serve is not None
    assert result.serve.reason == "stepdown"
    assert result.serve.skill_id == "FRAC.ADD.UNLIKE"


def test_run_session_ends_when_no_more_items_to_serve(
    tmp_path: Path, content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    student_id = "stu-empty-bank"
    empty_bank = engine.BankIndex(bank_version="empty", instances_by_skill={}, by_id={})
    store = StudentModelStore()
    engine_state = _bypass_placement()
    log_path = tmp_path / "log.jsonl"
    io = session.ScriptedInputSource([])
    messages, notify = _notifier()

    summary = session.run_session(
        store, engine_state, content, empty_bank, student_id, "CA", 6, "sess-empty", log_path, io, notify, NOW
    )
    assert summary.items_served == 0
    assert any("No more items" in m for m in messages)
