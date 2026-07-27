"""U6: the interactive session loop (core design doc §5.2, student-model doc
§9) — the direct product of the session-settled decision "through a real
session runner, not simulation-only."

**Boundary rule (R11).** This module never constructs mathematics. It reads
a pre-materialized item instance from a `BankIndex` (orchestrator/engine.py,
built by tools/bank_build.py) and renders it verbatim: stem, MCQ options,
hints, and solution steps are all copied straight out of the instance dict.
The one piece of "computation" this module does is grading — turning a
typed/selected response into an exact rational value or a chosen option —
and even that reuses `engine.match_distractor` (KTD8/R9) rather than
re-deriving what counts as a match.

**Known v1 limitation — `estimated_time_sec`.** The core doc's hint-offer
rule (§2.3) and this unit's timeout rule are both keyed off an item's
`estimated_time_sec`. That field lives on the *template*
(schemas/item-record.schema.json) but is not copied onto the *materialized
instance* by tools/bank_build.py (schemas/item-instance.schema.json carries
no such property — confirmed by reading both schemas). Extending U3's bank
builder/schema to carry it forward is explicitly out of this unit's scope.
This module instead falls back to `constants.DEFAULT_ESTIMATED_TIME_SEC` for
every served instance's hint-auto-offer and timeout thresholds. See
`_estimated_time_sec`.

**The feedback interface, live.** `_apply_and_log` scores the attempt via
`orchestrator/log.py`'s `compose_attempt` — the same U1/U2 composition
`apply_event` uses for a logged event, so the review-vs-non-review branching
exists in exactly one place, not mirrored by hand in two — writes the result
into the store, THEN calls `engine.consume_event` and applies its patches —
in that order, per `orchestrator/engine.py`'s module docstring.

**The async-input mechanism.** `InputSource.poll(timeout_sec)` is the one
seam between the hint ladder / timeout state machine (`collect_response`,
pure and clock-agnostic) and however a caller actually waits for input.
`ThreadedStdinInputSource` is the real terminal implementation: a daemon
thread blocks on `sys.stdin` and feeds a `queue.Queue`; the main loop polls
that queue with a timeout via `queue.Queue.get(timeout=...)`, which is what
lets it notice "no input yet" without blocking indefinitely on `input()`.
Tests use `ScriptedInputSource` instead, which advances a virtual clock
instead of sleeping — see that class's docstring.

**The discarded-event (abandonment guard) case, live.** A no-interaction
timeout (`timed_out=True, engaged=False`) is the one case `update.py`'s
abandonment guard can fire on. This module's policy: a no-interaction
timeout in an interactive terminal session is *always* treated as the
session's last event (`session_last_event=True`) — there is no reliable way
for a synchronous CLI to tell "the student is still there but silent" apart
from "the student walked away" other than issuing another prompt, which
would itself be an interaction and defeat the point. `_apply_and_log` then
mirrors `apply_attempt`'s `discarded=True` result faithfully: nothing is
written to the store, nothing is appended to the log (see log.py's module
docstring on "what gets logged"), and `run_session` ends the loop. An
*engaged* timeout (the student requested a hint or entered something before
running out of time) is never treated as the last event — it scores 0.0 like
any other wrong attempt and the session continues. `Attempt.session_last_event`
is documented (update.py) as a fact "the caller... determines" — different
callers may reasonably use different policies; `tests/test_session.py`
exercises the `session_last_event=False` ("mid-session no-interaction
timeout") branch directly against `_apply_and_log`, independent of whether
this particular CLI's own policy ever produces that combination live.
"""

from __future__ import annotations

import queue
import random
import re
import string
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from . import constants, engine, log as log_module
from .state import StudentModelStore
from .update import Attempt

# ---------------------------------------------------------------------------
# Async input abstraction (the "same async-input mechanism" for hint
# auto-offer and timeout)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PollResult:
    """One `InputSource.poll` outcome: either a line arrived (`line` is that
    line's text, `waited_sec` is how long the wait actually took) or nothing
    arrived within `timeout_sec` (`line is None`, `waited_sec == timeout_sec`,
    by both implementations' convention below)."""

    line: Optional[str]
    waited_sec: float


class InputSource:
    """Abstract per-item input source. `poll` waits up to `timeout_sec` for
    one line of input, decoupling `collect_response`'s hint-ladder/timeout
    state machine from *how* a caller waits (a background thread reading
    real stdin, or a scripted virtual clock in tests)."""

    def poll(self, timeout_sec: float) -> PollResult:  # pragma: no cover - interface
        raise NotImplementedError


class ThreadedStdinInputSource(InputSource):
    """Real-terminal `InputSource`: a daemon thread blocks on `sys.stdin`
    readline-by-readline and feeds a `queue.Queue`; `poll` drains that queue
    with a timeout via `queue.Queue.get(timeout=...)` — the threaded-timer
    choice documented in the module docstring. Chosen over `select()` on
    stdin because it is portable (Windows has no `select()` on regular
    files/pipes) and over a bare `input()` because `input()` has no timeout
    at all."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        for raw_line in sys.stdin:
            self._queue.put(raw_line.rstrip("\n"))

    def poll(self, timeout_sec: float) -> PollResult:
        start = time.monotonic()
        try:
            line = self._queue.get(timeout=max(timeout_sec, 0.0))
        except queue.Empty:
            return PollResult(line=None, waited_sec=max(timeout_sec, 0.0))
        return PollResult(line=line, waited_sec=time.monotonic() - start)


class ScriptedInputSource(InputSource):
    """Test double: a fixed script of `(arrival_time_sec, line)` pairs — each
    line's arrival time on one shared virtual clock that never sleeps, not a
    delay relative to whichever `poll` call happens to consume it. That
    distinction matters because `collect_response` chops one logical wait
    into several `poll` calls with different `timeout_sec` boundaries (the
    auto-hint boundary, then the timeout boundary) — a line arriving at
    absolute t=20 with boundaries at 15 and 30 must be seen by the *second*
    poll (whose deadline is 30), not judged against the first poll's 15s
    budget in isolation. Each `poll(timeout_sec)` advances `self.clock` to
    `min(next_scripted_arrival, clock + timeout_sec)` and reports whichever
    happened: the next line, or a timeout. This is how tests exercise hint
    auto-offer / timeout thresholds deterministically, per the module
    docstring's async-input section."""

    def __init__(self, script: List[Tuple[float, str]]) -> None:
        self._script = sorted(script, key=lambda pair: pair[0])
        self.clock: float = 0.0

    def poll(self, timeout_sec: float) -> PollResult:
        deadline = self.clock + timeout_sec
        if self._script and self._script[0][0] <= deadline + 1e-9:
            arrival, line = self._script.pop(0)
            waited = max(arrival - self.clock, 0.0)
            self.clock = arrival
            return PollResult(line=line, waited_sec=waited)
        self.clock = deadline
        return PollResult(line=None, waited_sec=timeout_sec)


# ---------------------------------------------------------------------------
# The hint ladder / timeout state machine (pure over InputSource + clock)
# ---------------------------------------------------------------------------

HINT_COMMANDS = frozenset({"hint", "h"})
QUIT_COMMANDS = frozenset({"quit", "exit", "q"})


class QuitRequested(Exception):
    """Raised out of `collect_response` when the student asks to end the
    session before answering the item currently on screen. The caller must
    not log or apply anything for that in-progress item — nothing about it
    was ever answered (see the module docstring's restart/replay note)."""


@dataclass
class CollectedResponse:
    response_raw: str
    parsed: object
    hints_used: int
    hint_auto_offered: bool
    time_taken_sec: float


@dataclass
class TimedOutResponse:
    hints_used: int
    hint_auto_offered: bool
    time_taken_sec: float
    engaged: bool


def _estimated_time_sec(instance: dict) -> float:
    """See the module docstring's known-limitation note: instances never
    actually carry this field, so `.get` always falls through to the
    default today. Written defensively (not a bare constant reference) so a
    future bank that *does* start carrying the field is honored without a
    code change here."""
    return float(instance.get("estimated_time_sec", constants.DEFAULT_ESTIMATED_TIME_SEC))


def collect_response(
    io: InputSource,
    hints: List[str],
    estimated_time_sec: float,
    notify: Callable[[str], None],
    parse: Callable[[str], Optional[object]],
) -> Union[CollectedResponse, TimedOutResponse]:
    """Run the hint ladder and timeout clock for one item (core doc §2.3,
    this unit's timeout rule) and collect one parseable response.

    - A `hint`/`h` command always shows the next hint and increments
      `hints_used`, regardless of elapsed time.
    - Once elapsed time crosses `HINT_AUTO_OFFER_MULTIPLIER * estimated_time_sec`
      with no response yet, hint 1 is shown automatically exactly once
      (`hint_auto_offered=True`), also incrementing `hints_used` — an
      auto-offered hint the student goes on to use is exactly as much
      evidence of scaffolding as a requested one (§2.3's three-way outcome
      table doesn't distinguish the two).
    - `quit`/`exit`/`q` raises `QuitRequested` (see that exception's
      docstring).
    - Anything else is handed to `parse`; a `None` result (unparseable
      garbage, e.g. a decimal string for numeric entry) reprompts without
      consuming the attempt — the elapsed-time clock keeps running, but
      nothing is returned or logged for the rejected line.
    - Once elapsed time reaches `TIMEOUT_MULTIPLIER * estimated_time_sec`
      with still no parseable response, returns a `TimedOutResponse`.
    """
    auto_hint_at = constants.HINT_AUTO_OFFER_MULTIPLIER * estimated_time_sec
    timeout_at = constants.TIMEOUT_MULTIPLIER * estimated_time_sec
    elapsed = 0.0
    hints_used = 0
    hint_auto_offered = False
    engaged = False

    while True:
        next_boundary = timeout_at
        if not hint_auto_offered and auto_hint_at < timeout_at:
            next_boundary = auto_hint_at
        remaining = max(next_boundary - elapsed, 0.0)
        result = io.poll(remaining)
        elapsed = min(elapsed + result.waited_sec, timeout_at)

        if result.line is None:
            if not hint_auto_offered and auto_hint_at < timeout_at and elapsed >= auto_hint_at - 1e-9:
                hint_auto_offered = True
                hints_used += 1
                idx = min(hints_used, len(hints)) - 1
                notify(f"(hint offered automatically) Hint {hints_used}: {hints[idx]}")
                continue
            return TimedOutResponse(
                hints_used=hints_used,
                hint_auto_offered=hint_auto_offered,
                time_taken_sec=elapsed,
                engaged=engaged,
            )

        raw = result.line.strip()
        low = raw.lower()
        if low in HINT_COMMANDS:
            engaged = True
            hints_used += 1
            idx = min(hints_used, len(hints)) - 1
            notify(f"Hint {hints_used}: {hints[idx]}")
            continue
        if low in QUIT_COMMANDS:
            raise QuitRequested()

        engaged = True
        parsed = parse(raw)
        if parsed is None:
            notify("Sorry, I couldn't understand that response — try again (or 'hint' / 'quit').")
            continue
        return CollectedResponse(
            response_raw=raw,
            parsed=parsed,
            hints_used=hints_used,
            hint_auto_offered=hint_auto_offered,
            time_taken_sec=elapsed,
        )


# ---------------------------------------------------------------------------
# KTD8: numeric-entry parsing — integers, "a/b", mixed numbers "W n/d"
# ---------------------------------------------------------------------------

_FRACTION_RE = re.compile(r"^(-?\d+)/(-?\d+)$")
_INT_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class ParsedNumeric:
    """`value` is the exact rational value (Python `Fraction`, always kept
    reduced — used for value-equality comparison). `literal_form` is the
    canonical "p/q" (q>1) or "p" string of exactly what was typed, with the
    numerator/denominator taken *literally* (not reduced) — the same
    convention `tools/bank_build.py` uses for distractor `value` strings, so
    it doubles as the grading value passed to `engine.match_distractor` and
    as the `simplest_form_only` literal-match check (KTD8)."""

    value: Fraction
    literal_form: str


def _match_fraction(token: str) -> Optional[Tuple[int, int]]:
    """Regex-match a bare "num/den" token and extract its integer parts —
    the one step `_parse_simple_token`'s bare-fraction case and
    `parse_numeric_response`'s mixed-number fractional part both need.
    Validation and sign handling differ between the two callers (a
    standalone fraction accepts a negative denominator and normalizes it; a
    mixed number's fractional part must be a positive proper fraction), so
    this only does the match-and-extract, not the validation."""
    m = _FRACTION_RE.match(token)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_simple_token(token: str) -> Optional[ParsedNumeric]:
    fraction = _match_fraction(token)
    if fraction is not None:
        num, den = fraction
        if den == 0:
            return None
        if den < 0:
            num, den = -num, -den
        value = Fraction(num, den)
        literal_form = f"{num}/{den}" if den != 1 else str(num)
        return ParsedNumeric(value=value, literal_form=literal_form)
    if _INT_RE.match(token):
        n = int(token)
        return ParsedNumeric(value=Fraction(n), literal_form=str(n))
    return None


def parse_numeric_response(raw: str) -> Optional[ParsedNumeric]:
    """KTD8: accepts integers ("6"), fraction strings ("5/6"), and
    whitespace-separated mixed numbers ("1 5/6") — nothing else. In
    particular a decimal string ("0.83") matches neither `_FRACTION_RE` nor
    `_INT_RE` and returns None, the same "unparseable, re-prompt" signal as
    any other garbage input; this function never raises."""
    text = raw.strip()
    if not text:
        return None
    tokens = text.split()

    if len(tokens) == 1:
        return _parse_simple_token(tokens[0])

    if len(tokens) == 2:
        whole_tok, frac_tok = tokens
        if not _INT_RE.match(whole_tok):
            return None
        fraction = _match_fraction(frac_tok)
        if fraction is None:
            return None
        num, den = fraction
        if den <= 0 or num < 0:
            return None
        whole = int(whole_tok)
        sign = -1 if whole_tok.startswith("-") else 1
        whole_abs = abs(whole)
        value = sign * (Fraction(whole_abs) + Fraction(num, den))
        improper_num = sign * (whole_abs * den + num)
        literal_form = f"{improper_num}/{den}" if den != 1 else str(improper_num)
        return ParsedNumeric(value=value, literal_form=literal_form)

    return None


def grade_numeric_response(instance: dict, parsed: ParsedNumeric) -> Tuple[bool, Optional[str], str]:
    """Judge one numeric-entry response against the instance's stored
    `answer` (KTD8): exact rational value equality, plus — when
    `simplest_form_only` is set — the response's own literal string form
    must equal the instance's canonical `answer` string exactly (an
    unsimplified-but-equal value is wrong; MISC.FRAC.NO-SIMPLIFY, core doc
    §2.2). Returns (correct, misconception_id, response_parsed_field_value)
    — the last is what gets written into the attempt event's
    `response_parsed` field and is also what `engine.match_distractor` is
    called against for a wrong answer (both instances of "grading value" use
    the same literal, unreduced form the bank's own distractors are stored
    in)."""
    answer = parse_numeric_response(instance["answer"])
    if answer is None:  # pragma: no cover - bank_build only ever emits parseable canonical answers
        raise ValueError(f"instance {instance['item_instance_id']!r} has an unparseable answer {instance['answer']!r}")

    value_equal = parsed.value == answer.value
    if instance["simplest_form_only"]:
        correct = value_equal and parsed.literal_form == instance["answer"]
    else:
        correct = value_equal

    misconception_id = None if correct else engine.match_distractor(instance, parsed.literal_form)
    return correct, misconception_id, parsed.literal_form


# ---------------------------------------------------------------------------
# MCQ rendering: deterministic per-instance shuffle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCQOption:
    label: str
    value: str
    misconception_id: Optional[str]
    is_correct: bool


def build_mcq_options(instance: dict) -> List[MCQOption]:
    """Shuffle the answer + distractors with an RNG seeded by the instance's
    own `item_instance_id` (deterministic across a re-render of the same
    instance, e.g. after a garbage-input reprompt, without needing to persist
    the shuffle order anywhere)."""
    items = [{"value": instance["answer"], "misconception_id": None, "is_correct": True}]
    for distractor in instance["distractors"]:
        items.append(
            {"value": distractor["value"], "misconception_id": distractor["misconception_id"], "is_correct": False}
        )
    rng = random.Random(instance["item_instance_id"])
    rng.shuffle(items)
    labels = string.ascii_uppercase
    return [
        MCQOption(label=labels[i], value=item["value"], misconception_id=item["misconception_id"], is_correct=item["is_correct"])
        for i, item in enumerate(items)
    ]


def make_mcq_parser(options: List[MCQOption]) -> Callable[[str], Optional[MCQOption]]:
    by_label = {opt.label: opt for opt in options}

    def parse(raw: str) -> Optional[MCQOption]:
        return by_label.get(raw.strip().upper())

    return parse


def grade_mcq_response(chosen: MCQOption) -> Tuple[bool, Optional[str], str]:
    correct = chosen.is_correct
    misconception_id = None if correct else chosen.misconception_id
    return correct, misconception_id, chosen.value


# ---------------------------------------------------------------------------
# The feedback interface, live: apply the student-model update, log, then
# consume_event (orchestrator/engine.py's module docstring; log.py's
# apply_event composition; tests/test_engine.py's run_attempt reference)
# ---------------------------------------------------------------------------


def _apply_and_log(
    store: StudentModelStore,
    engine_state: engine.EngineState,
    content: engine.ContentIndex,
    bank: engine.BankIndex,
    student_id: str,
    session_id: str,
    log_path: Path,
    serve: "engine.Serve",
    instance: dict,
    response_raw: str,
    response_parsed: Optional[str],
    correct: bool,
    misconception_id: Optional[str],
    hints_used: int,
    hint_auto_offered: bool,
    time_taken_sec: float,
    timed_out: bool,
    engaged: bool,
    session_last_event: bool,
    now: datetime,
) -> Optional[Tuple[float, float, "engine.EngineState", dict]]:
    """Score one served item via `orchestrator/log.py`'s `compose_attempt` —
    the same U1/U2 composition `apply_event` uses for a logged event — then
    run the engine's `consume_event` step, in that order (see module
    docstring).

    Returns `None` when the §3 abandonment guard fires (`compose_attempt`
    reports a `None` `new_state`): mirrors log.py's "what gets logged"
    decision exactly — nothing is written to `store`, nothing is appended
    to `log_path`, `consume_event` is never called. Otherwise returns
    `(theta_before, theta_after, new_engine_state, logged_event)`.
    """
    skill_id = serve.skill_id
    attempt = Attempt(
        skill_ids=[skill_id],
        question_type=instance["question_type"],
        b_prior_at_serve=instance["difficulty"],
        correct=correct,
        hints_used=hints_used,
        timed_out=timed_out,
        engaged=engaged,
        session_last_event=session_last_event,
        context=serve.context,
        misconception_id=misconception_id,
        event_id=f"evt-{uuid.uuid4().hex}",
        depth_served=instance["difficulty"],
    )
    state = store.get(student_id, skill_id)
    composed = log_module.compose_attempt(state, attempt, serve.context, now)
    if composed.new_state is None:
        return None
    theta_before, theta_after, n_before = composed.theta_before, composed.theta_after, composed.n_before

    store.write_records(student_id, {skill_id: composed.new_state})

    event = {
        "event_id": attempt.event_id,
        "student_id": student_id,
        "session_id": session_id,
        "timestamp": now.isoformat(),
        "item_instance_id": instance["item_instance_id"],
        "template_id": instance["template_id"],
        "template_version": instance["template_version"],
        "bank_version": instance["bank_version"],
        "engine_version": constants.ENGINE_VERSION,
        "graph_version": instance["graph_version"],
        "skill_ids": [skill_id],
        "question_type": instance["question_type"],
        "depth_served": instance["difficulty"],
        "b_prior_at_serve": instance["difficulty"],
        "exam_context": None,
        "language": instance["language"],
        "locale": instance["locale"],
        "response_raw": response_raw,
        "response_parsed": response_parsed,
        "correct": correct,
        "misconception_id": misconception_id,
        "hints_used": hints_used,
        "hint_auto_offered": hint_auto_offered,
        "time_taken_sec": time_taken_sec,
        "timed_out": timed_out,
        "context": serve.context,
        "theta_before": theta_before,
        "theta_after": theta_after,
        "n_before": n_before,
    }
    log_module.append_attempt(log_path, event)

    outcome = engine.AttemptOutcome(
        student_id=student_id,
        skill_ids=[skill_id],
        item_instance_id=instance["item_instance_id"],
        correct=correct,
        misconception_id=misconception_id,
        context=serve.context,
        depth_served=instance["difficulty"],
    )
    consume_result = engine.consume_event(engine_state, store, content, bank, outcome, now)
    engine.apply_consume_patches(store, student_id, consume_result.skill_state_patches)

    return theta_before, theta_after, consume_result.engine_state, event


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_item(notify: Callable[[str], None], instance: dict, serve: "engine.Serve") -> None:
    notify("")
    notify(f"[{serve.context}] {instance['stem']}")


def render_mcq_options(notify: Callable[[str], None], options: List[MCQOption]) -> None:
    for opt in options:
        notify(f"  {opt.label}) {opt.value}")


def show_solution(notify: Callable[[str], None], instance: dict) -> None:
    steps = instance["solution_steps"].get("canonical")
    if steps is None:
        steps = next(iter(instance["solution_steps"].values()))
    notify("Solution:")
    for i, step in enumerate(steps, start=1):
        notify(f"  {i}. {step}")


# ---------------------------------------------------------------------------
# The main loop
# ---------------------------------------------------------------------------


@dataclass
class SessionSummary:
    items_served: int = 0
    theta_deltas: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    certifications_earned: List[str] = field(default_factory=list)
    engine_state: Optional["engine.EngineState"] = None


def run_session(
    store: StudentModelStore,
    engine_state: engine.EngineState,
    content: engine.ContentIndex,
    bank: engine.BankIndex,
    student_id: str,
    country: str,
    grade: int,
    session_id: str,
    log_path: Path,
    io: InputSource,
    notify: Callable[[str], None],
    now: datetime,
) -> SessionSummary:
    """Approach point 1-5: select -> render -> collect -> grade -> apply/log
    -> feedback, repeated until `select_next` has nothing left to serve, the
    student quits, or a no-interaction timeout ends the session (see module
    docstring). `now` is KTD10's single session timestamp — every
    `select_next`/`consume_event` call and every attempt's logged
    `timestamp` use this one value, never a fresh clock read."""
    theta_start: Dict[str, float] = {}
    certified_before: Dict[str, bool] = {}
    certifications_earned: List[str] = []
    items_served = 0

    def _track_skill(skill_id: str) -> None:
        if skill_id not in theta_start:
            s = store.peek(student_id, skill_id)
            theta_start[skill_id] = s.theta
            certified_before[skill_id] = s.certified_depth is not None

    def _track_certification(skill_id: str) -> None:
        after = store.peek(student_id, skill_id)
        if after.certified_depth is not None and not certified_before.get(skill_id, False):
            certified_before[skill_id] = True
            if skill_id not in certifications_earned:
                certifications_earned.append(skill_id)

    while True:
        select_result = engine.select_next(store, engine_state, content, bank, student_id, country, grade, now)
        engine_state = select_result.engine_state
        for warning in select_result.warnings:
            notify(f"[warn] {warning}")

        serve = select_result.serve
        if serve is None:
            notify("No more items to serve right now.")
            break

        instance = bank.by_id[serve.item_instance_id]
        skill_id = serve.skill_id
        _track_skill(skill_id)

        render_item(notify, instance, serve)
        estimated_time = _estimated_time_sec(instance)

        if instance["question_type"] == "mcq_4":
            options = build_mcq_options(instance)
            render_mcq_options(notify, options)
            parse = make_mcq_parser(options)
        else:
            options = None
            parse = parse_numeric_response

        notify("Your answer (or 'hint' / 'quit'):")
        try:
            collected = collect_response(io, instance["hints"], estimated_time, notify, parse)
        except QuitRequested:
            notify("Ending session.")
            break

        items_served += 1

        if isinstance(collected, TimedOutResponse):
            session_last_event = not collected.engaged  # see module docstring's abandonment-guard section
            applied = _apply_and_log(
                store, engine_state, content, bank, student_id, session_id, log_path, serve, instance,
                response_raw="",
                response_parsed=None,
                correct=False,
                misconception_id=None,
                hints_used=collected.hints_used,
                hint_auto_offered=collected.hint_auto_offered,
                time_taken_sec=collected.time_taken_sec,
                timed_out=True,
                engaged=collected.engaged,
                session_last_event=session_last_event,
                now=now,
            )
            if applied is None:
                notify("No response received — ending session.")
                break
            _theta_before, _theta_after, engine_state, _event = applied
            notify("Time's up! Marked as not correct this time.")
            show_solution(notify, instance)
            _track_certification(skill_id)
            continue

        # collected: CollectedResponse
        if options is not None:
            correct, misconception_id, response_parsed = grade_mcq_response(collected.parsed)
        else:
            correct, misconception_id, response_parsed = grade_numeric_response(instance, collected.parsed)

        applied = _apply_and_log(
            store, engine_state, content, bank, student_id, session_id, log_path, serve, instance,
            response_raw=collected.response_raw,
            response_parsed=response_parsed,
            correct=correct,
            misconception_id=misconception_id,
            hints_used=collected.hints_used,
            hint_auto_offered=collected.hint_auto_offered,
            time_taken_sec=collected.time_taken_sec,
            timed_out=False,
            engaged=True,
            session_last_event=False,
            now=now,
        )
        assert applied is not None, "an engaged (non-timeout) attempt is never discarded by the abandonment guard"
        _theta_before, _theta_after, engine_state, _event = applied

        if correct:
            notify("Correct!")
        else:
            notify("Not quite.")
            if misconception_id:
                notify(f"(misconception tagged: {misconception_id})")
            show_solution(notify, instance)
        _track_certification(skill_id)

    theta_deltas = {
        skill_id: (before, store.peek(student_id, skill_id).theta) for skill_id, before in theta_start.items()
    }
    return SessionSummary(
        items_served=items_served,
        theta_deltas=theta_deltas,
        certifications_earned=certifications_earned,
        engine_state=engine_state,
    )
