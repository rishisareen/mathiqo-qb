"""On-disk JSONL attempt log and the replay path that rebuilds
StudentModelStore from it (student-model doc §9; R15).

**What gets logged (the discarded-event decision).** `apply_attempt`
(update.py) can return `discarded=True` when the §3 abandonment guard fires
— a no-interaction timeout that is also a session's last event. A discarded
attempt produces no state change at all: theta/n are returned unchanged.
This module's answer to "does a discarded attempt get a log row" is **no**:
the live session runner must never call `append_attempt` for one. Per §9,
"anything not in this log is uncomputable later" taken literally — a
discarded attempt computed nothing, so it isn't attempt-log content. This
also keeps replay simple and exact: every event `replay()` reads is
guaranteed to have been genuinely applied when it was first scored, so
`replay()` never needs a "was this discarded" flag or special-case skip.
(The alternative — log discarded attempts for audit and have replay
recognize and skip them — was considered and rejected: it would require
smuggling the discard verdict into the schema or re-deriving it from
`engaged`/`session_last_event`, neither of which the frozen schema carries,
see below.)

**Composing U1 (update.py) and U2 (certification.py) during replay.** For
each logged event, in file order:

- Reconstruct an `Attempt` from the event's scoring-relevant fields
  (`_attempt_from_event`).
- If `event["context"] == "review"` (the §6 due-date review probe):
    - **Pass** (`attempt.correct`): call only `apply_review_outcome(state,
      True, now)`. `apply_attempt` is deliberately NOT called — SM §6 point
      2 states plainly "Pass → interval ×2.5 ..., θ unchanged"; running the
      attempt through the ordinary update rule would move θ, contradicting
      the design doc outright.
    - **Fail**: call `apply_attempt(state, attempt)` first (§6 point 2:
      "Fail → treated as a wrong attempt, θ updates"), then
      `apply_review_outcome(result.new_state, False, now)` on the result —
      the same two-step composition certification.py's own module docstring
      describes and test_certification.py already exercises directly.
    - `maybe_certify` is **never** called on a "review" event, pass or
      fail. This is a deliberate exception, not an oversight: a review
      probe is usually itself a qualifying attempt for `maybe_certify`'s
      guard (unassisted, correct, single-skill, low-guess format,
      depth_served set) — but `maybe_certify` unconditionally resets
      `review_interval_days`/`review_due` to the initial 7-day value
      whenever it fires. Calling it after a passing probe would silently
      erase the ×2.5 growth `apply_review_outcome` just computed; calling
      it after a failing probe would just redundantly reset the same
      fields `apply_review_outcome` already reset. Keeping "review" events
      entirely inside certification.py's review functions avoids that
      collision.
- Otherwise (`placement` / `practice` / `probe` / `lesson`): call
  `apply_attempt(state, attempt)` once per listed skill (multi-skill items
  update every skill on success — `apply_attempt` operates on one
  `SkillState` at a time, so the caller loops, exactly as
  test_update.py's multi-skill tests do). Then, only when the event names
  exactly one skill (`len(skill_ids) == 1` — `maybe_certify`'s own guard
  already refuses multi-skill items, and there is no single unambiguous
  `SkillState` to certify against when there are several), call
  `maybe_certify(new_state, attempt, now)` on the just-updated
  (post-`apply_attempt`) state. Certification reads "current θ" — the θ
  that already reflects this attempt's own evidence, not the θ from before
  it — so `maybe_certify` always runs after `apply_attempt`, never before
  or in parallel.

`now` for both `maybe_certify` and `apply_review_outcome` is the event's own
`timestamp` field (parsed by `_parse_timestamp`), not the wall-clock time of
the replay run — replay must reproduce the exact `review_due` the live
session computed, which depended on when the attempt actually happened.

**`engaged` / `session_last_event` reconstruction.** `Attempt` carries these
two abandonment-guard inputs (update.py), but the attempt-event schema does
not persist them — they're serve-time process, not outcome, and the schema
is frozen. Replay does not need their true historical values, though:
because discarded attempts are never logged (the decision above), every
event `replay()` sees is already known to have evaluated `is_abandoned() ==
False` when it was first scored. `_attempt_from_event` therefore sets
`engaged=True` unconditionally (`session_last_event=False`) — not a claim
about what really happened at serve time, just a synthetic pair guaranteed
to keep `is_abandoned()` False, which is the only thing either field feeds
into.

**Replay does not trust the logged θ/n snapshots.** `theta_before`,
`theta_after`, and `n_before` are required schema fields, written for
external audit ("why does the app think my child can't do fractions" —
§9's design principle) — but `replay()` never reads them back. Its
correctness instead comes from re-deriving state by calling the identical
U1/U2 pure functions on each event's causal inputs (skill_ids,
question_type, b_prior_at_serve, correct, hints_used, timed_out, context,
misconception_id, depth_served) — per KTD7, the same functions the live
session calls. That's what makes replay a property test of live-session
equivalence rather than a "trust the log's own numbers" shortcut.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import jsonschema

from .certification import apply_review_outcome, maybe_certify
from .state import SkillState, StudentModelStore
from .update import Attempt, apply_attempt

REPO_ROOT = Path(__file__).resolve().parent.parent
ATTEMPT_EVENT_SCHEMA_PATH = REPO_ROOT / "schemas" / "attempt-event.schema.json"

# The §6 review-probe context — the only context value that routes through
# certification.py's review functions instead of (or in addition to)
# update.py's apply_attempt. See the module docstring's composition rules.
REVIEW_CONTEXT = "review"


class AttemptLogError(ValueError):
    """Raised when a caller attempts to log an event that violates the
    attempt-event schema. A malformed event is a bug, not data — this is
    never silently swallowed or coerced."""


@lru_cache(maxsize=1)
def _validator() -> "jsonschema.Draft202012Validator":
    schema = json.loads(ATTEMPT_EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def append_attempt(log_path: Path, event: dict) -> None:
    """Validate `event` against schemas/attempt-event.schema.json and append
    it as one JSONL line to `log_path`, creating the file/parent directories
    if needed.

    Raises AttemptLogError (naming every violation) rather than writing a
    malformed row. Never call this for a discarded (abandonment-guard)
    attempt — see the module docstring's "what gets logged" section; this
    function has no way to detect a discard on its own, since `discarded`
    is a fact about the UpdateResult, not about the event dict.
    """
    errors = sorted(_validator().iter_errors(event), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise AttemptLogError(f"attempt-event schema violation(s): {details}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True))
        f.write("\n")


def _iter_events(log_path: Path) -> Iterator[dict]:
    """Yield every event dict from `log_path` in file (= chronological
    write) order. A missing file yields nothing — a fresh student has no
    log yet, not an error."""
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _parse_timestamp(raw: str) -> datetime:
    """ISO 8601 parse tolerant of a trailing 'Z' (UTC) — Python's
    datetime.fromisoformat only gained native 'Z' support in 3.11; this repo
    targets 3.9+."""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def _attempt_from_event(event: dict) -> Attempt:
    """Reconstruct the scoring-relevant Attempt (update.py) from one logged
    event's fields. See the module docstring for the engaged/
    session_last_event synthesis."""
    return Attempt(
        skill_ids=list(event["skill_ids"]),
        question_type=event["question_type"],
        b_prior_at_serve=event["b_prior_at_serve"],
        correct=event["correct"],
        hints_used=event["hints_used"],
        timed_out=event["timed_out"],
        engaged=True,
        session_last_event=False,
        context=event["context"],
        misconception_id=event["misconception_id"],
        event_id=event["event_id"],
        depth_served=event["depth_served"],
    )


def _set_state(store: StudentModelStore, student_id: str, skill_id: str, new_state: SkillState) -> None:
    """Write `new_state` into `store` for (student_id, skill_id) using only
    StudentModelStore's public API. There is no direct setter by design
    (state.py's contract is get / records_for / replace_records); this
    mirrors how §8 migrations swap records via replace_records, just scoped
    to one skill at a time."""
    records = dict(store.records_for(student_id))
    records[skill_id] = new_state
    store.replace_records(student_id, records)


def _apply_event(store: StudentModelStore, event: dict) -> None:
    """Apply one logged event to `store` per the module docstring's U1+U2
    composition rules."""
    student_id = event["student_id"]
    skill_ids = event["skill_ids"]
    context = event["context"]
    now = _parse_timestamp(event["timestamp"])
    attempt = _attempt_from_event(event)

    if context == REVIEW_CONTEXT:
        for skill_id in skill_ids:
            state = store.get(student_id, skill_id)
            if attempt.correct:
                new_state = apply_review_outcome(state, True, now)
            else:
                result = apply_attempt(state, attempt)
                new_state = apply_review_outcome(result.new_state, False, now)
            _set_state(store, student_id, skill_id, new_state)
        return

    updated_states: dict[str, SkillState] = {}
    for skill_id in skill_ids:
        state = store.get(student_id, skill_id)
        result = apply_attempt(state, attempt)
        updated_states[skill_id] = result.new_state

    if len(skill_ids) == 1:
        only_skill = skill_ids[0]
        updated_states[only_skill] = maybe_certify(updated_states[only_skill], attempt, now)

    for skill_id, new_state in updated_states.items():
        _set_state(store, student_id, skill_id, new_state)


def replay(log_path: Path) -> StudentModelStore:
    """Rebuild a StudentModelStore from scratch by replaying every event in
    `log_path`, in file order, through the same U1 (`apply_attempt`) / U2
    (`maybe_certify`, `apply_review_outcome`) functions the live session
    calls (KTD7) — see the module docstring for the exact composition.

    A missing or empty log yields a fresh, empty store — no error. Every
    replayed event is also appended to the returned store's `attempt_log`
    (StudentModelStore's own contract: "the append-only attempt log every
    state is reconstructible from"), so the rebuilt store's log mirrors the
    file it was replayed from.

    Ordering: events are applied strictly in file order. State only ever
    depends on the ordered sequence of events for the *same* (student_id,
    skill_id) pair — different students, or different skills for the same
    student, are independent dict slots — so interleaving unrelated
    students'/sessions' events in the file does not change the final
    result, even though the per-skill sequence itself is order-sensitive
    (the K schedule and misconception window are).
    """
    store = StudentModelStore()
    for event in _iter_events(log_path):
        _apply_event(store, event)
        store.append_event(event)
    return store
