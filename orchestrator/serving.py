"""The serving-integrity contract's transport-free facade (KTD6).

Session-pinned, seen-filtered serving plus attempt submission — the one
surface both the internal reference orchestrator (U6) and the MCP server
(U8) call. No network, no MCP imports: `tools/mcp_server.py` is the only
transport adapter on top of this module (KTD6's parity rule — every
serving/ingest operation a caller uses is exposed here exactly once).

**What this module does not do.** Item *selection* stays with the caller
(KD1): `get_candidates` returns every unseen instance in the pinned bank for
a (student, skill) — or, once the pool is exhausted, the single
least-recently-seen repeat plus an explicit signal (R1) — and never picks
one on the caller's behalf. `submit_attempt` computes the student-model
update (theta, certification) and the engine's derived seen-state exactly
as `orchestrator/session.py` does today, because that computation belongs
to Mathiqo (the caller only reports what happened), but it never queues or
consumes this module's own selection policy.

**Session pin (KTD3, R3, R4).** `open_session` resolves the `ACTIVE`
pointer once and holds the resulting `BankIndex` for the `Session`
object's whole life — every later call against that `Session` keeps
serving from the build it opened against, even across a later
`bank_activate` repoint (R4).

**Identifier discipline (point 3).** `student_id` and `session_id` reach
filesystem paths (the per-student log file), so both are checked against
`IDENTIFIER_PATTERN` before any path is constructed — the same pattern
mirrored onto `schemas/attempt-event.schema.json`'s `student_id`.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import batch as batch_module
from . import catalog as catalog_module
from . import constants
from . import engine
from . import ingest as ingest_module
from . import log as log_module
from . import signals
from .identity import IdentityRegistry
from .state import StudentModelStore, certification_status
from .update import Attempt

logger = logging.getLogger(__name__)

ACTIVE_POINTER_NAME = "ACTIVE"

# Point 3: alphanumerics, dot, dash, underscore; bounded length; no path
# separators. Mirrored onto schemas/attempt-event.schema.json's student_id.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# U5/KTD12: idle-eviction TTL for a registered serving session. Chosen to
# comfortably span a plausible gap between practice bursts in one sitting
# (a student pausing for a snack, a hint discussion with a parent, a
# distraction) without holding a crashed/abandoned session's engine_state
# and lock indefinitely. Not a capacity cap — purely idle-time-based. A
# named constant, per the plan, so a pilot's tuning pass has one place to
# touch it.
SESSION_TTL_SECONDS: float = 30 * 60  # 30 minutes

# U5: how many recently-evicted session_ids this registry remembers, so a
# lookup on one of them can be told apart from a session_id this registry
# never issued at all (session_expired vs session_unknown, KTD4). Bounded
# rather than unbounded so a long-running server's memory doesn't grow
# forever off of nothing but the eviction traffic itself; an id evicted
# long enough ago (more evictions than this since) ages out and falls back
# to the "unknown" reading, which is an accepted, documented approximation.
EXPIRED_SESSION_MEMORY: int = 1024


class ServingError(Exception):
    """Base class for the facade's own domain errors (distinct from
    orchestrator.log.AttemptLogError, which stays schema-shaped)."""


class InvalidIdentifierError(ServingError):
    """A student_id/session_id failed IDENTIFIER_PATTERN."""


class NoActiveBankError(ServingError):
    """Point 5: no ACTIVE pointer, or it names a build no longer on disk —
    named rather than left as an undefined filesystem failure."""


class NoIdentityRegistryError(ServingError):
    """U4's identity pass-through methods (`register_student`,
    `link_student`, `find_student`, `is_linked`) were called on a facade
    constructed without an `IdentityRegistry` (the constructor's `identity`
    param defaults to `None` — most callers, and every existing test that
    predates U4, never configure one)."""


class ConsumerNotLinkedError(ServingError):
    """R17: `self.identity` is configured and `IdentityRegistry.is_linked`
    says the calling `consumer_id` has no link to `student_id` — refused
    before the operation touches any session/log state. Distinct from
    `NoIdentityRegistryError`, which fires when `self.identity` is `None`
    and one of U4's identity pass-through methods is called; this fires
    when identity *is* configured but this particular consumer isn't
    linked yet (the fix is `link_student`, not configuring identity)."""


class SessionConflictError(ServingError):
    """R20/AE9: `open_session` found a live session for this student
    already open under a *different* consumer than the one opening now.
    `student_id`/`existing_session_id` are attached for a caller that
    wants to act on them programmatically; the message itself names the
    recovery path (`close_session_by_student`) so a partner integration
    reading only the error text still knows what to do next."""

    def __init__(self, student_id: str, existing_session_id: str):
        self.student_id = student_id
        self.existing_session_id = existing_session_id
        super().__init__(
            f"{signals.SESSION_CONFLICT}: student {student_id!r} already has a live session "
            f"({existing_session_id!r}) opened by a different consumer; a consumer "
            "linked to this student may recover by calling close_session_by_student "
            "(keyed by student_id, not session_id) before opening a new session"
        )


class SessionUnknownError(ServingError):
    """A `session_id` this registry has no record of ever issuing — never
    registered, or registered so long ago its eviction fell out of
    `EXPIRED_SESSION_MEMORY`. Distinct from `SessionExpiredError` (KTD4: a
    caller needs to tell "this id never existed / I lost track of it, try
    reopening" apart from "this id existed but the session was reaped for
    inactivity, the student may still expect it")."""


class SessionExpiredError(ServingError):
    """A `session_id` this registry issued and remembers evicting via
    `SESSION_TTL_SECONDS` idle-eviction — distinct from
    `SessionUnknownError`. See that class's docstring for why the
    distinction matters."""


class IngestRejectedError(ServingError):
    """Raised by callers that want a KTD5 ingest rejection to surface as an
    exception rather than inspecting `SubmitOutcome.rejection_reason`
    themselves — e.g. the interactive session runner (U6), which has no
    sensible way to retry or repair an already-rendered, already-answered
    item mid-loop. `submit_attempt` itself never raises this: it always
    returns a `SubmitOutcome`, leaving the choice of raise-vs-inspect to
    the caller."""


def validate_identifier(value: str, label: str) -> None:
    """Point 3's allowlist, plus an explicit refusal of `.`/`..` path
    segments: both consist entirely of otherwise-allowed characters (dots),
    so the character-class regex alone would let a traversal segment
    through."""
    if not IDENTIFIER_PATTERN.match(value) or value in (".", ".."):
        raise InvalidIdentifierError(
            f"{label} {value!r} is not a valid identifier: expected 1-128 characters "
            "of letters, digits, '.', '-', or '_' only, and not '.' or '..'"
        )


def read_active_bank_version(banks_dir: Path) -> str:
    """KTD3: the ACTIVE pointer file's contents — what a session opened
    now pins to. Raises NoActiveBankError (point 5) if the pointer is
    absent or names a build no longer on disk."""
    pointer = banks_dir / ACTIVE_POINTER_NAME
    if not pointer.is_file():
        raise NoActiveBankError(f"no active bank: {pointer} does not exist")
    version = pointer.read_text(encoding="utf-8").strip()
    if not version or not (banks_dir / version).is_dir():
        raise NoActiveBankError(
            f"no active bank: ACTIVE names {version!r}, which is not on disk under {banks_dir}"
        )
    return version


@dataclass
class Session:
    """Held for the session's whole life (KTD3) — `bank` is resolved once
    at `open_session` and never re-resolved, so a later `bank_activate`
    repoint cannot shift an already-open session's content (R4).

    `country`/`curriculum`/`grade` (U5, KTD3) are pinned the same way:
    `open_session` sets them once and nothing later mutates them — R6's
    recommendation reads (a later unit) will read `session.curriculum`
    rather than take it fresh from a caller on every call. Defaulted to
    `None` so the internal session runner's own direct `Session(...)`
    construction (`orchestrator/session.py`), which predates this unit,
    does not break for a caller that doesn't set them; in practice that
    runner does set them (see its own docstring) because it already has
    the values on hand from its own `--country`/`--curriculum`/`--grade`
    CLI arguments.

    U6 (R19, KTD4): `accepted_attempts` is this session's Path-A replay
    map — `attempt_id -> SubmitOutcome` for every attempt_id `submit_attempt`
    has already accepted (in this session, or seeded at `open_session` time
    from the student's own log, see `_accepted_attempts_from_log`). A
    resubmission of an `attempt_id` already present here is returned
    unchanged, with no re-validation, re-write, or re-log (the "two-path
    idempotency design" note in the U6 plan). Defaulted to an empty dict for
    the same reason `country`/`curriculum`/`grade` default to `None`: the
    internal session runner's own direct `Session(...)` construction
    (`orchestrator/session.py`'s `_apply_and_log`) doesn't pass this.

    U9 (KTD5): `delivered` is this session's scratch record for batch
    planning — every `variant_signature` `fetch_batch` has returned in this
    session but that has not (yet, or ever) been *attempted*. Purely
    in-memory, dies with the session (R11: an item delivered but never
    attempted returns to the pool for a future batch — nothing here is
    durable, and nothing ever writes it to the log or replays it back on a
    fresh `open_session`, unlike `engine_state.seen_history`). Defaulted to
    an empty set for the same reason `accepted_attempts` is: the internal
    session runner's own direct `Session(...)` construction doesn't set
    it.

    KTD17 (P0 fix): `_lock` is this session's own mutual-exclusion lock,
    attached once by `SessionRegistry.register_or_conflict` at the moment
    `open_session` registers it — never re-resolved by `session_id` on
    every call the way it used to be. This is deliberate: a fresh
    session_id-keyed lookup on every `_lock_for` call meant a session
    already torn down mid-operation (a concurrent takeover-on-open,
    `close_session`/`close_session_by_student`, or TTL eviction removing
    its registry entry) became indistinguishable from a `Session` that was
    *never* registered at all — both read back `None` and silently fell
    through to a no-op `contextlib.nullcontext()`, exactly when teardown
    makes concurrent access most likely. Storing the lock on the `Session`
    object itself instead means a caller already holding a `Session`
    reference keeps using that same lock instance for that object's whole
    life, regardless of whether the registry has since removed its entry —
    while a `Session` that genuinely never went through
    `register_or_conflict` (the internal session runner's ephemeral
    objects, `orchestrator/session.py`) never has this attribute set and
    correctly keeps its documented unlocked, single-writer posture.
    Defaulted to `None` for the same reason every other registry-only
    field here is: the internal runner's direct `Session(...)`
    construction doesn't set it. Excluded from `repr`/equality
    (`threading.Lock` has no meaningful repr or equality of its own, and
    every existing equality-sensitive test constructs a `Session` without
    ever registering it, so this field is always `None` there anyway)."""

    session_id: str
    student_id: str
    bank: engine.BankIndex
    log_path: Path
    store: StudentModelStore
    engine_state: engine.EngineState
    country: Optional[str] = None
    curriculum: Optional[str] = None
    grade: Optional[int] = None
    accepted_attempts: Dict[str, "SubmitOutcome"] = field(default_factory=dict)
    delivered: Set[str] = field(default_factory=set)
    _lock: "Optional[threading.Lock]" = field(default=None, repr=False, compare=False)


@dataclass
class CandidatesResult:
    """R1, R13: `instances` is every unseen instance in the pinned bank for
    the requested skill in the ordinary case (`signal=None`). Three distinct
    non-ordinary outcomes, each an explicit named signal rather than an
    empty `instances` list a caller has to guess the meaning of (R13):

    - `signal=signals.UNKNOWN_SKILL`: `skill_id` isn't in the loaded content
      graph at all (`self.content.skills`) — `instances` is `[]`.
    - `signal=signals.EMPTY_POOL`: a known skill, but the pinned bank has no
      instances for it at all (`session.bank.instances_by_skill`) —
      `instances` is `[]`.
    - `signal=signals.EXHAUSTED`: the pool has instances, but every one has
      already been seen — `instances` holds exactly the one
      least-recently-seen repeat (KD5), same as before this unit.

    `exhausted` (the pre-U10 field) is kept for backward compatibility with
    every existing caller that already branches on it — `True` only for the
    `EXHAUSTED` signal, `False` for the other three outcomes (ordinary,
    `UNKNOWN_SKILL`, `EMPTY_POOL`), exactly the same as its pre-U10 meaning.
    `warning` is set on all three non-ordinary outcomes, naming the signal
    explicitly in its text."""

    bank_version: str
    instances: List[dict]
    exhausted: bool
    signal: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class SubmitOutcome:
    """Three-way result the caller must distinguish (U6's "ingest rejection
    surfaces as an error, not a silent drop"): `event` is set only when the
    attempt was accepted and logged; a rejection sets `rejection_reason`
    (KTD5) and leaves `session.store`/`session.engine_state` untouched — the
    §3 abandonment guard discarding the attempt sets neither, and is not an
    error, just nothing to log — but does set `discard_reason` (U6), naming
    why nothing was logged, so a caller doesn't have to infer a discard from
    two `None`s. `repeat` (KD5) is never written onto the event itself; it
    only ever lives here.

    U6 (R19): this is also the value cached, keyed by `attempt_id`, in
    `Session.accepted_attempts` on acceptance — a Path-A replay of the same
    `attempt_id` returns this exact object back out unchanged."""

    event: Optional[dict]
    rejection_reason: Optional[str] = None
    repeat: bool = False
    discard_reason: Optional[str] = None


def _accepted_attempts_from_log(log_path: Path) -> Dict[str, SubmitOutcome]:
    """U6 (R19, KTD4): seeds a freshly opened session's Path-A replay map
    (`Session.accepted_attempts`) from the student's own historical log, so
    a resubmission of an `attempt_id` that was accepted in a *prior*
    session (crash/eviction between the original accept and the client
    seeing the response) is still recognized after `open_session` — not
    just within the one live session that first accepted it.

    Deliberately its own pass over `log_module.iter_events(log_path)`
    rather than something threaded through `engine.rebuild_session_state`'s
    own internal replay loop: that function's docstring is explicit that
    this unit must not duplicate its apply_attempt/consume_event
    composition, and changing its return shape to also hand back per-event
    data would ripple into every one of its other callers
    (`tools/serve.py`, `tools/replay_audit.py`, `tests/test_engine.py`),
    all outside this unit's scope. This pass is cheap by comparison — it
    only JSON-decodes each line and reads `attempt_id` off it, doing none
    of `rebuild_session_state`'s per-event `compose_attempt`/`consume_event`
    work — so the extra file read costs a small, bounded fraction of what
    `rebuild_session_state`'s own already-necessary pass costs, not a
    second copy of that work.

    A reconstructed `SubmitOutcome` only carries `event` (the full logged
    row — everything a repeat submission of the same `attempt_id` needs to
    hand back unchanged). `repeat` defaults to `False`: the original KD5
    pool-exhaustion repeat flag was never persisted onto the event itself
    (by design — see `orchestrator/ingest.py`'s module docstring) and isn't
    recoverable from the log, but it also isn't part of retry-safety (AE7
    only requires the *outcome* replay exactly, not the repeat flag), so
    reconstructing it as `False` rather than guessing is the honest choice.
    Historical log lines predating this unit (no `attempt_id` field at all)
    are skipped, not errored — they simply can't participate in Path A.
    """
    accepted: Dict[str, SubmitOutcome] = {}
    for event in log_module.iter_events(log_path):
        attempt_id = event.get("attempt_id")
        if attempt_id is not None:
            accepted[attempt_id] = SubmitOutcome(event=event, repeat=False)
    return accepted


@dataclass
class _SessionEntry:
    """`SessionRegistry`'s own bookkeeping for one registered session,
    deliberately kept separate from `Session` itself (registry concerns —
    who opened it, when it was last touched — aren't serving domain state
    and have no business on the object `get_candidates`/`submit_attempt`
    operate on).

    KTD17 (P0 fix): the per-session lock is deliberately *not* duplicated
    here anymore — it lives on `session._lock` itself (see `Session`'s own
    docstring for why: a fresh session_id-keyed lookup on every call used
    to make a torn-down-but-still-in-flight session indistinguishable from
    one that was never registered at all). This entry's only job regarding
    the lock is that `register_or_conflict` attaches one to `session`
    exactly once, at registration."""

    session: Session
    consumer_id: str
    last_activity: float


class SessionRegistry:
    """KTD12: the session dict moves here, owned by the facade layer, so
    both the MCP server and a later REST transport (later units) share one
    registry rather than each keeping its own closured dict. Also owns the
    TTL/idle-eviction policy (`SESSION_TTL_SECONDS`); `register_or_conflict`
    is where KTD17's per-session lock gets attached (onto `session._lock`
    itself, not stored here — see `Session`'s own docstring for the P0 fix
    this attachment point exists to make correct).

    Two indices: `session_id -> _SessionEntry` (the primary lookup every
    tool call needs) and `student_id -> session_id` (R20/AE9's
    conflict-at-open check, and the close-by-student-id recovery path,
    which a crashed client's own lost session_id can't reach). At most one
    live session per student is ever indexed — `register_or_conflict` is
    the one place that invariant is enforced.

    All mutating operations take `self._registry_lock` — a single lock
    guarding the *index structures themselves* (registration, eviction,
    pop), not a session's own state (that's each `Session`'s own `_lock`,
    KTD17). The two are deliberately different locks: holding the registry
    lock for the width of, say, `submit_attempt`'s whole body would
    serialize unrelated students' concurrent attempts against each other,
    which KTD17 doesn't ask for and would be a needless bottleneck."""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._entries: Dict[str, _SessionEntry] = {}
        self._by_student: Dict[str, str] = {}
        # An ordered set (values unused) of recently-evicted session_ids,
        # bounded to EXPIRED_SESSION_MEMORY — see that constant's docstring.
        self._expired_ids: "OrderedDict[str, None]" = OrderedDict()
        self._registry_lock = threading.Lock()

    # -- internal: must be called with self._registry_lock held -----------

    def _evict_expired_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, entry in self._entries.items()
            if now - entry.last_activity > self._ttl_seconds
        ]
        for session_id in expired:
            self._evict_one_locked(session_id)

    def _evict_one_locked(self, session_id: str) -> None:
        """Unregister exactly `session_id` (already known/assumed expired
        by the caller) from both indices and remember it in
        `_expired_ids` — the single-entry building block
        `_evict_expired_locked`'s full sweep and `get()`'s O(1) own-entry
        check both fold into, so eviction bookkeeping (the two-index
        removal, the expired-id memory) exists in exactly one place
        regardless of which caller found the expiry."""
        entry = self._entries.pop(session_id, None)
        if entry is None:
            return
        if self._by_student.get(entry.session.student_id) == session_id:
            del self._by_student[entry.session.student_id]
        self._remember_expired_locked(session_id)

    def _remember_expired_locked(self, session_id: str) -> None:
        self._expired_ids[session_id] = None
        self._expired_ids.move_to_end(session_id)
        while len(self._expired_ids) > EXPIRED_SESSION_MEMORY:
            self._expired_ids.popitem(last=False)

    def _get_entry_locked(self, session_id: str) -> _SessionEntry:
        entry = self._entries.get(session_id)
        if entry is not None:
            return entry
        if session_id in self._expired_ids:
            raise SessionExpiredError(
                f"session {session_id!r} was idle-evicted after {self._ttl_seconds:.0f}s with no activity"
            )
        raise SessionUnknownError(f"{session_id!r} is not a session_id this registry ever issued")

    # -- public -------------------------------------------------------------

    def register_or_conflict(self, session: Session, consumer_id: str) -> Optional[Session]:
        """Atomically: if `session.student_id` has no live session, index
        `session` under `consumer_id` and return `None` (plain open). If it
        has one already opened by this same `consumer_id`, replace it
        (takeover-on-open) and return the prior `Session` so the caller can
        log its closure with the right attribution (KTD6's takeover flow).
        If it has one opened by a *different* consumer, raise
        `SessionConflictError` — this new session is never registered
        (R20/AE9: the first session is left completely unaffected).

        KTD17 (P0 fix): attaches `session`'s own mutual-exclusion lock
        here, exactly once, onto `session._lock` itself — not into this
        entry, and never re-created or re-resolved later. `session` is
        still purely local to the caller (`open_session`) at this point,
        about to be returned as its result, so setting the attribute here
        is unguarded by anything beyond `self._registry_lock` already held
        for this whole call and races with nothing. The prior session (on
        a takeover) keeps whatever lock instance was attached at *its own*
        registration — untouched by this call — so anything still holding
        a reference to it after this method removes its registry entry
        keeps serializing against that same lock (see `Session`'s and
        `ServingFacade._lock_for`'s docstrings for the full "why")."""
        with self._registry_lock:
            self._evict_expired_locked(time.monotonic())
            existing_session_id = self._by_student.get(session.student_id)
            prior_session: Optional[Session] = None
            if existing_session_id is not None:
                existing_entry = self._entries[existing_session_id]
                if existing_entry.consumer_id != consumer_id:
                    raise SessionConflictError(session.student_id, existing_session_id)
                prior_session = existing_entry.session
                del self._entries[existing_session_id]
            session._lock = threading.Lock()
            self._entries[session.session_id] = _SessionEntry(
                session=session, consumer_id=consumer_id, last_activity=time.monotonic()
            )
            self._by_student[session.student_id] = session.session_id
            return prior_session

    def get(self, session_id: str) -> Session:
        """Raises `SessionUnknownError`/`SessionExpiredError` (KTD4) rather
        than returning `None` — every caller needs to branch on *which* of
        the two it is, not just "not found". A successful lookup counts as
        activity and resets the idle-eviction clock — otherwise a caller
        that only ever reads (`get_candidates`, `get_progress`) without
        ever calling `submit_attempt` would idle-evict mid-use, which
        isn't what "idle" is meant to mean.

        O(1), not a full-registry sweep (a code-review efficiency finding):
        `get()` is called on essentially every tool/route invocation
        (`get_candidates`, `submit_attempt`, `fetch_batch`, `refill_batch`,
        ...), so it only checks the looked-up entry's own TTL against
        `now` directly, evicting just that one entry (`_evict_one_locked`)
        if it's stale — never scanning every other registered session to
        resolve one specific lookup. The full sweep (`_evict_expired_locked`)
        stays reserved for callers that structurally need to know about
        entries *other* than the one being looked up (e.g.
        `register_or_conflict`, which must reap a stale session for this
        student before its conflict check, not just this session_id)."""
        with self._registry_lock:
            now = time.monotonic()
            entry = self._entries.get(session_id)
            if entry is not None and now - entry.last_activity > self._ttl_seconds:
                self._evict_one_locked(session_id)
            entry = self._get_entry_locked(session_id)
            entry.last_activity = now
            return entry.session

    def consumer_of(self, session_id: str) -> str:
        """The consumer_id that opened (or last took over) this session —
        used by close-by-session_id to decide whether to log an ordinary
        close vs whatever attribution is appropriate."""
        with self._registry_lock:
            self._evict_expired_locked(time.monotonic())
            return self._get_entry_locked(session_id).consumer_id

    # KTD17 (P0 fix): there is no more `lock_for(session_id)` registry
    # lookup here — a fresh session_id-keyed lookup on every call was
    # exactly the bug (a torn-down-but-still-in-flight session became
    # indistinguishable from a never-registered one, both reading back
    # `None`). `register_or_conflict` above attaches the lock directly to
    # `session._lock` at registration instead; `ServingFacade._lock_for`
    # reads it straight off the `Session` object it already holds. See
    # `Session`'s and `_SessionEntry`'s own docstrings for the full "why".

    def touch(self, session_id: str) -> None:
        with self._registry_lock:
            entry = self._entries.get(session_id)
            if entry is not None:
                entry.last_activity = time.monotonic()

    def find_by_student(self, student_id: str) -> Optional[Session]:
        with self._registry_lock:
            self._evict_expired_locked(time.monotonic())
            session_id = self._by_student.get(student_id)
            if session_id is None:
                return None
            return self._entries[session_id].session

    def pop(self, session_id: str) -> Optional[Session]:
        """Close-by-session_id: unregister and return the `Session`, or
        `None` if it wasn't registered (already closed, or never opened
        through this registry at all) — a no-op, not an error, matching
        `close_session`'s pre-U5 stateless contract for that case."""
        with self._registry_lock:
            entry = self._entries.pop(session_id, None)
            if entry is None:
                return None
            if self._by_student.get(entry.session.student_id) == session_id:
                del self._by_student[entry.session.student_id]
            return entry.session

    def pop_by_student(self, student_id: str) -> Optional[Session]:
        """Close-by-student_id (KTD6 point 5): the recovery path for a
        crashed client that lost its session_id. Returns the closed
        `Session`, or `None` if the student has no live session."""
        with self._registry_lock:
            self._evict_expired_locked(time.monotonic())
            session_id = self._by_student.pop(student_id, None)
            if session_id is None:
                return None
            entry = self._entries.pop(session_id, None)
            return entry.session if entry is not None else None


@dataclass
class SkillProgress:
    skill_id: str
    theta: float
    n: float
    certified_depth: Optional[float]
    certification_status: str
    seen_count: int


@dataclass
class ProgressSummary:
    student_id: str
    skills: Dict[str, SkillProgress] = field(default_factory=dict)


def build_progress_summary(
    store: StudentModelStore, engine_state: engine.EngineState, student_id: str, now: datetime
) -> ProgressSummary:
    """The one place theta/certification-status/seen-count get assembled
    into a `ProgressSummary` — shared by `ServingFacade.get_progress` and
    `tools/replay_audit.py` (U7), so the audit tool's reproduced summary is
    provably the same shape and computation a live session's progress read
    uses, not a second hand-written version of it."""
    records = store.records_for(student_id)
    skills: Dict[str, SkillProgress] = {}
    for skill_id, state in records.items():
        skills[skill_id] = SkillProgress(
            skill_id=skill_id,
            theta=state.theta,
            n=state.n,
            certified_depth=state.certified_depth,
            certification_status=certification_status(state, now),
            seen_count=len(engine_state.seen_history.get(skill_id, [])),
        )
    return ProgressSummary(student_id=student_id, skills=skills)


@dataclass
class RecommendedSkill:
    """One entry of `ServingFacade.recommend_next`'s ranked list — a thin
    reshaping of `engine.RankedCandidate` onto this facade's own response
    surface (never the engine dataclass itself, so a later engine-internal
    field never leaks onto the public contract unannounced)."""

    skill_id: str
    reason: str  # review/remediation/stepdown/prereq_probe/placement/frontier
    context: str  # attempt-event context this skill would serve under
    target_depth: float
    unservable: bool  # KTD3: True when the checked bank has no instances for this skill


@dataclass
class RecommendationsResult:
    student_id: str
    bank_version: str
    content_version: str
    recommendations: List[RecommendedSkill] = field(default_factory=list)


@dataclass
class ServableSkill:
    """One entry of `ServingFacade.list_skills`'s response: `catalog.MappedSkill`'s
    curriculum-local presentation (never the catalog dataclass itself, same
    reshaping rationale as `RecommendedSkill`) plus `servable` — True when
    the checked bank build holds at least one raw instance for this skill
    (the deferred catalog-honesty question from
    docs/plans/2026-07-29-001-feat-class6-servable-coverage-plan.md)."""

    skill_id: str
    depth_expectation: str
    learning_objective: Optional[str]
    terminology_overrides: Dict[str, str]
    unit_code: Optional[str]
    unit_name: Optional[str]
    servable: bool


@dataclass
class CatalogSkillsResult:
    content_version: str
    country: str
    curriculum: str
    grade: int
    unit_code: Optional[str]  # the narrowing filter this result was built with, if any
    bank_version: Optional[str]  # the build `servable` was checked against; None when none resolved
    skills: List[ServableSkill] = field(default_factory=list)


class ServingFacade:
    """Constructed once per process (or per test) against a banks
    directory (KTD3's ACTIVE pointer plus KTD7's retained-build lookup) and
    a logs directory (point 4: one JSONL per student).

    `bank_lookup`, when given, replaces the default disk-backed
    `engine.make_bank_lookup(banks_dir)` — for a caller that already holds
    its bank(s) in memory and has no versioned `banks_dir` registry at all
    (U6: the interactive session runner is handed one already-loaded
    `BankIndex` per session and only ever submits events stamped with that
    one version, so `engine.make_static_bank_lookup(bank)` is both
    sufficient and avoids requiring a registry directory that caller
    doesn't have). `banks_dir`/`logs_dir` may then be `None` — `open_session`
    requires them, and `recommend_next`/`list_skills` tolerate a `None`
    `banks_dir` by degrading (KD3: the catalog stays open regardless).

    `identity`, when given, is an already-constructed `IdentityRegistry`
    (U4) — this facade never derives its path or constructs one itself,
    it only holds the instance a caller configured. It defaults to `None`,
    which now (U5) means something operationally, not just "the pass-throughs
    raise": every consumer-facing operation below (`open_session`,
    `submit_attempt`, `get_progress`, `close_session`,
    `close_session_by_student`) checks `self.identity` first and skips its
    link check entirely when it's `None` (R17 enforcement is opt-in per
    deployment) — this is what keeps the internal CLI runner
    (`orchestrator/session.py`) working unchanged, since it constructs its
    own transient `ServingFacade` without an `identity` argument and always
    will (see that module's docstring). `session_ttl_seconds` overrides
    `SESSION_TTL_SECONDS` for `self.sessions` (U5's `SessionRegistry`,
    KTD12) — a constructor knob mainly so tests can use a short TTL rather
    than sleeping for the real default."""

    def __init__(
        self,
        banks_dir: Optional[Path],
        logs_dir: Optional[Path],
        content: engine.ContentIndex,
        bank_lookup: Optional[engine.BankLookup] = None,
        identity: Optional[IdentityRegistry] = None,
        session_ttl_seconds: float = SESSION_TTL_SECONDS,
    ):
        self.banks_dir = banks_dir
        self.logs_dir = logs_dir
        self.content = content
        self.bank_lookup = bank_lookup or engine.make_bank_lookup(banks_dir)
        self.identity = identity
        # KTD12: owned here, not by any transport — tools/mcp_server.py (and
        # a later REST transport) hold/delegate to this registry instead of
        # keeping their own session dict.
        self.sessions = SessionRegistry(ttl_seconds=session_ttl_seconds)

    def _log_path_for(self, student_id: str) -> Path:
        validate_identifier(student_id, "student_id")
        return self.logs_dir / f"{student_id}.jsonl"

    def _replay_or_fresh(self, log_path: Path) -> "Tuple[StudentModelStore, engine.EngineState]":
        """The "replay the student's log if one exists, else fresh state"
        branch every session-less read (`get_progress`, `recommend_next`)
        needs identically — extracted after a simplify pass found the same
        four lines duplicated verbatim at both call sites. `open_session`
        does NOT use this: it additionally seeds `accepted_attempts` from
        the same log (KTD4), a step specific to registering a *live*
        session, not a scratch read."""
        if log_path.is_file():
            return engine.rebuild_session_state(log_path, self.content, self.bank_lookup)
        return StudentModelStore(), engine.EngineState()

    def _check_linked(self, consumer_id: str, student_id: str) -> None:
        """R17's enforcement point: a no-op when `self.identity` is `None`
        (unconfigured deployments, the internal runner's posture); raises
        `ConsumerNotLinkedError` when it's configured and `consumer_id`
        has no link to `student_id`."""
        if self.identity is None:
            return
        if not self.identity.is_linked(consumer_id, student_id):
            raise ConsumerNotLinkedError(
                f"consumer {consumer_id!r} is not linked to student {student_id!r} — "
                "link the consumer first (register_student/link_student)"
            )

    def _log_closure(self, session: Session, consumer_id: str, reason: str) -> None:
        """The one place a session's closure is recorded, regardless of
        which path closed it (ordinary close-by-session_id, takeover-on-open,
        or close-by-student_id) — always attributed to the *closing*
        consumer_id, not the session's original opener (KTD6 point 5)."""
        logger.info(
            "session closed: session_id=%s student_id=%s closed_by=%s reason=%s",
            session.session_id, session.student_id, consumer_id, reason,
        )

    def _lock_for(self, session: Session) -> "contextlib.AbstractContextManager":
        """KTD17 (P0 fix): reads the lock straight off `session._lock` —
        set once by `SessionRegistry.register_or_conflict` at the moment
        this exact `Session` object was registered — rather than a fresh
        `session_id`-keyed lookup into `self.sessions` on every call. The
        old lookup-by-id approach silently degraded to a no-op lock not
        only for the internal session runner's legitimate never-registered
        `Session` objects, but *also* for a genuinely-registered session
        whose registry entry a concurrent takeover-on-open/close/TTL
        eviction had just removed — indistinguishable failure modes, one
        intentional and one a correctness bug (KTD17 P0). Reading the
        attribute directly fixes that: a `Session` still carries its own
        lock for its whole object lifetime regardless of what the registry
        does to its entry afterward, so anything already holding this
        exact `Session` reference keeps serializing against the same lock
        instance. A no-op `contextlib.nullcontext()` only when `session`
        never went through `register_or_conflict` at all (`_lock` stays
        its dataclass default of `None`) — the internal session runner's
        ephemeral objects (`orchestrator/session.py`) keep their existing
        unlocked, single-writer behavior exactly as documented there."""
        return session._lock if session._lock is not None else contextlib.nullcontext()

    def open_session(
        self,
        session_id: str,
        student_id: str,
        country: str,
        curriculum: str,
        grade: int,
        consumer_id: str,
    ) -> Session:
        """KTD3: resolve ACTIVE once, load and hold that build's
        BankIndex, and pin `country`/`curriculum`/`grade` on the returned
        `Session` for its whole life exactly like `bank` (R6 will read
        `session.curriculum` in a later unit). Replays the student's
        existing log (if any) through KTD7's per-event bank resolution, so
        seen-state derived across a build boundary is correct even though
        only the *new* bank is pinned for serving (the session-open half
        of KTD7). Also seeds the returned `Session.accepted_attempts` (U6,
        R19) from that same log's own `attempt_id`s via
        `_accepted_attempts_from_log`, so a resubmission after a restart or
        eviction hits Path A instead of double-applying.

        R17: refuses with `ConsumerNotLinkedError` if `self.identity` is
        configured and `consumer_id` isn't linked to `student_id`.

        R20/AE9: if `student_id` already has a live session under this
        same `consumer_id`, takes over — closes the prior session (logged,
        attributed to `consumer_id`) and opens fresh. If it's live under a
        *different* consumer, refuses with `SessionConflictError` and
        leaves that session completely unaffected."""
        validate_identifier(session_id, "session_id")
        log_path = self._log_path_for(student_id)
        self._check_linked(consumer_id, student_id)
        bank_version = read_active_bank_version(self.banks_dir)
        bank = self.bank_lookup(bank_version)
        if log_path.is_file():
            store, engine_state = engine.rebuild_session_state(log_path, self.content, self.bank_lookup)
            accepted_attempts = _accepted_attempts_from_log(log_path)
        else:
            store, engine_state = StudentModelStore(), engine.EngineState()
            accepted_attempts = {}
        new_session = Session(
            session_id=session_id,
            student_id=student_id,
            bank=bank,
            log_path=log_path,
            store=store,
            engine_state=engine_state,
            country=country,
            curriculum=curriculum,
            grade=grade,
            accepted_attempts=accepted_attempts,
        )
        prior_session = self.sessions.register_or_conflict(new_session, consumer_id)
        if prior_session is not None:
            self._log_closure(prior_session, consumer_id, reason="takeover_on_open")
        return new_session

    def get_candidates(self, session: Session, skill_id: str, consumer_id: str) -> CandidatesResult:
        """R1, R2, R13: seen-filtered by variant_signature (KTD4), drawn only
        from `session.bank` (never re-resolved mid-session, R3/R4).

        R17: refuses with `ConsumerNotLinkedError` before touching anything
        if `self.identity` is configured and `consumer_id` isn't linked to
        `session.student_id` — this operation was missed when link
        enforcement landed on every sibling read/write (`open_session`,
        `submit_attempt`, `get_progress`, `recommend_next`, `close_session*`)
        and would otherwise let any credentialed consumer holding (or
        guessing/logging/replaying) another partner's `session_id` read
        that student's seen-filtered candidate pool — a cross-tenant leak
        `_check_linked` exists specifically to prevent.

        U10: distinguishes three pool edge cases the pre-U10 binary
        `exhausted` flag conflated (see `CandidatesResult`'s own docstring)
        — checked in this order, each an explicit signal: `skill_id` not in
        the content graph at all (`UNKNOWN_SKILL`); a known skill whose
        pinned bank has no instances for it (`EMPTY_POOL`); every instance
        already seen (`EXHAUSTED`, the pre-U10 behavior, unchanged)."""
        self._check_linked(consumer_id, session.student_id)
        if skill_id not in self.content.skills:
            return CandidatesResult(
                bank_version=session.bank.bank_version,
                instances=[],
                exhausted=False,
                signal=signals.UNKNOWN_SKILL,
                warning=f"{signals.UNKNOWN_SKILL}: {skill_id!r} is not a skill in the loaded content graph",
            )

        pool = session.bank.instances_by_skill.get(skill_id, [])
        if not pool:
            return CandidatesResult(
                bank_version=session.bank.bank_version,
                instances=[],
                exhausted=False,
                signal=signals.EMPTY_POOL,
                warning=f"{signals.EMPTY_POOL}: skill {skill_id!r} is a known skill, but the pinned bank "
                "has no instances for it",
            )

        seen_history = session.engine_state.seen_history.get(skill_id, [])
        seen = set(seen_history)
        unseen = [inst for inst in pool if inst["variant_signature"] not in seen]
        if unseen:
            return CandidatesResult(
                bank_version=session.bank.bank_version, instances=unseen, exhausted=False, signal=None
            )

        last_seen_at = {sig: idx for idx, sig in enumerate(seen_history)}
        ranked = sorted(
            pool,
            key=lambda inst: (last_seen_at.get(inst["variant_signature"], -1), inst["item_instance_id"]),
        )
        chosen = ranked[0]
        warning = (
            f"{signals.EXHAUSTED}: pool exhausted for skill {skill_id!r}: repeating least-recently-seen "
            f"instance {chosen['item_instance_id']!r}"
        )
        return CandidatesResult(
            bank_version=session.bank.bank_version,
            instances=[chosen],
            exhausted=True,
            signal=signals.EXHAUSTED,
            warning=warning,
        )

    def _plan_batch_locked(
        self, session: Session, skill_id: str, batch_size: int
    ) -> batch_module.BatchResult:
        """The shared planning core behind both `fetch_batch` and
        `refill_batch` (U10: "a refill is just fetch_batch re-planned
        against the session's current live state" — genuinely the same
        code path, not a parallel copy of it). Callers must already hold
        `session`'s own lock (`_lock_for`) — this method does not acquire
        it itself, so it can be reused as-is by a caller (`refill_batch`)
        that also needs to compute something else (KTD5's `supersede`)
        under the same lock acquisition.

        Plans directly against `session.engine_state` — no defensive
        `engine._copy_engine_state` clone (a code-review efficiency
        finding: `plan_ladder`'s own docstring and implementation confirm
        it only ever reads `engine_state.seen_history`, never mutates or
        returns a new one, unlike `select_next`/`consume_event`'s
        no-aliasing contract that `_copy_engine_state` exists to protect
        elsewhere). This method never touches `session.store` either —
        only an accepted `submit_attempt` may write durable state. The one
        piece of state this method *does* mutate is `session.delivered`,
        updated with every returned item's `variant_signature` only after
        the plan (including the feedback sidecar, which can raise
        `batch_module.BatchPlanningError`) has fully succeeded — a raised
        AE8 violation leaves `session.delivered` exactly as it was, same
        as a rejected `submit_attempt` leaves `session.store` untouched."""
        clamped_size = batch_module.clamp_batch_size(batch_size)
        planned = batch_module.plan_ladder(
            session.store,
            session.engine_state,
            self.content,
            session.bank,
            session.student_id,
            skill_id,
            session.country,
            session.curriculum,
            session.grade,
            clamped_size,
            session.delivered,
        )
        sidecar = batch_module.build_feedback_sidecar(self.content, planned)
        policy = batch_module.build_policy_v1()

        for item in planned:
            session.delivered.add(item.instance["variant_signature"])

        return batch_module.BatchResult(
            batch_id=batch_module.new_batch_id(),
            student_id=session.student_id,
            skill_id=skill_id,
            items=planned,
            count_requested=clamped_size,
            count_returned=len(planned),
            policy=policy,
            feedback_sidecar=sidecar,
            bank_version=session.bank.bank_version,
            content_version=self.content.content_version,
        )

    def fetch_batch(
        self,
        session: Session,
        skill_id: str,
        consumer_id: str,
        batch_size: int = constants.DEFAULT_BATCH_SIZE,
    ) -> batch_module.BatchResult:
        """U9 (R8-R11, R18, KD1, KD2): a server-planned, difficulty-laddered
        working set for one (student, skill) — the batch-serving sibling of
        `get_candidates`'s single-skill scoping, not routed through
        `rank_serve_candidates`'s cross-skill priority chain (that stays
        `select_next`/`recommend_next`'s job; a partner requesting a batch
        has already chosen which skill it wants practiced).

        R17: refuses with `ConsumerNotLinkedError` before touching anything
        if `self.identity` is configured and `consumer_id` isn't linked to
        `session.student_id` (mirrors every other student-scoped operation
        on this facade).

        KTD17: runs under this session's own lock (`_lock_for` — a no-op
        lock for the internal session runner's ephemeral, never-registered
        `Session` objects, same as `submit_attempt`) — batch planning reads
        `session.engine_state` and reads/writes `session.delivered`, both
        session-owned state two concurrent calls against the same session
        must not race on.

        No `self.sessions.touch` call here (a code-review efficiency
        finding): both real transports (`tools/mcp_server.py`,
        `tools/rest_server.py`) resolve `session` via `self.sessions.get`
        moments before calling this method, and `get()` itself already
        resets the idle-eviction clock as a documented side effect — the
        same posture `get_candidates` (this facade's other read that never
        touches) already takes. A second touch here would just repeat
        work `get()` already did with no meaningful time gap in between.
        """
        self._check_linked(consumer_id, session.student_id)
        with self._lock_for(session):
            return self._plan_batch_locked(session, skill_id, batch_size)

    def _compute_supersede(self, session: Session, skill_id: str) -> bool:
        """KTD5: True when the plan for `skill_id` has materially changed
        since a client's last batch — checked against `session`'s real,
        live `store`/`engine_state` (the same live state
        `_plan_batch_locked` itself now plans against — see that method's
        docstring on why no defensive clone is needed), because this must
        reflect actually-accepted attempts, not a planning simulation. Two
        conditions, either one sufficient:

        - the skill is now certified at or above its own serving ceiling
          (`store.peek(...).certified_depth >= serving_ceiling(...)`) —
          deliberately not the same "mastered" check
          `engine.rank_serve_candidates` uses for frontier-serving
          eligibility (which also excludes a `lapsed` skill): a lapsed
          skill's plan for *this* skill hasn't materially changed just
          because its due-for-review clock lapsed, so this check reads
          `certified_depth` alone, regardless of `lapsed`;
        - a remediation dose for this exact `skill_id` is now queued in
          `session.engine_state.remediation_queue` (KTD12's bounded-dose
          misconception remediation) — a genuinely new plan element that
          wasn't there when the last batch for this skill was planned.

        Caller (`refill_batch`) must already hold `session`'s lock, so this
        reads a stable, non-racing snapshot of both `store` and
        `engine_state`."""
        skill_state = session.store.peek(session.student_id, skill_id)
        if skill_state.certified_depth is not None:
            ceiling = engine.serving_ceiling(
                skill_id, session.country, session.curriculum, session.grade, self.content
            )
            if skill_state.certified_depth >= ceiling:
                return True
        return any(dose.skill_id == skill_id for dose in session.engine_state.remediation_queue)

    def refill_batch(
        self,
        session: Session,
        skill_id: str,
        consumer_id: str,
        attempts_flushed: bool,
        batch_size: int = constants.DEFAULT_BATCH_SIZE,
    ) -> batch_module.RefillResult:
        """U10 (R12, KTD5): a background refill — "call `fetch_batch` again
        against this session's current live state" (AE2: a refill's whole
        point is that it reflects attempts submitted since the last batch,
        and that falls out naturally from `_plan_batch_locked` reading
        `session.store`'s live theta on every call, the same as
        `fetch_batch` always has). Two additions on top of plain
        `fetch_batch`:

        **`attempts_flushed` (R12).** The client's protocol-level assertion
        that every attempt it intends to submit before this refill has
        already been submitted. This is a documented client-protocol
        contract, not something this method can verify beyond accepting
        it — and logging a warning when it's `False`, a caller-visible
        record that the contract was declared violated, without refusing
        the call: a refill requested with `attempts_flushed=False` is still
        planned against whatever live state exists at the moment of the
        call, same as it would be if the flag were `True`; the caller that
        lied about it gets a refill planned against a possibly-stale view
        of its own submissions, not a corrupted one.

        **`supersede` (KTD5, `_compute_supersede`).** Set on every
        `RefillResult` regardless of whether the refill found new items —
        `True` when the client should discard its remaining
        delivered-but-unattempted buffer for `skill_id` because the plan
        materially changed since the last batch (skill now certified at/above
        its serving ceiling, or a remediation dose now queued for it).

        **`no_further_items` (R13).** When the freshly planned batch has
        zero new items (`count_returned == 0` — everything left in the pool
        for this skill is already in seen ∪ delivered), returns
        `RefillResult(batch=None, signal=signals.NO_FURTHER_ITEMS, ...)`
        instead of an empty `BatchResult` a caller could mistake for "try
        again shortly" rather than "nothing is coming for this skill, move
        to the next recommendation" (R13). Idempotent: a second immediate
        refill in this state re-plans against the same (unchanged) live
        state and returns the same signal again — no loop, no different
        error.

        R17/KTD17: same link check and same per-session lock as
        `fetch_batch` — `supersede` and the plan itself are computed under
        one lock acquisition, so a concurrent `submit_attempt` against this
        session can never be interleaved between the two (KTD17). Also
        mirrors `fetch_batch` in calling no `self.sessions.touch` here —
        the transport layer's `self.sessions.get` moments earlier already
        reset the idle-eviction clock (see `fetch_batch`'s docstring)."""
        self._check_linked(consumer_id, session.student_id)
        if not attempts_flushed:
            logger.warning(
                "refill_batch called with attempts_flushed=False for session_id=%s skill_id=%s -- "
                "R12's client-protocol contract (\"all attempts intended before this refill were "
                "already submitted\") was declared violated; proceeding anyway, planned against "
                "whatever live state exists right now",
                session.session_id, skill_id,
            )
        with self._lock_for(session):
            supersede = self._compute_supersede(session, skill_id)
            result = self._plan_batch_locked(session, skill_id, batch_size)
            if result.count_returned == 0:
                return batch_module.RefillResult(batch=None, signal=signals.NO_FURTHER_ITEMS, supersede=supersede)
            return batch_module.RefillResult(batch=result, signal=None, supersede=supersede)

    def submit_attempt(
        self,
        session: Session,
        attempt_id: str,
        skill_id: str,
        instance: dict,
        response_raw: str,
        response_parsed: "Optional[str | float]",
        correct: bool,
        misconception_id: Optional[str],
        hints_used: int,
        hint_auto_offered: bool,
        time_taken_sec: float,
        timed_out: bool,
        engaged: bool,
        session_last_event: bool,
        context: str,
        consumer_id: str,
        now: datetime,
    ) -> SubmitOutcome:
        """Composes the student-model update (log.py's `compose_attempt` —
        the same U1/U2 composition `orchestrator/session.py` uses) without
        writing it anywhere yet, builds the tentative event, and runs it
        through `ingest.validate_event` (KTD5) *before* touching
        `session.store` or the log — a rejected event must leave no trace,
        not even a theta write that the log itself would then disagree with
        (R10: the store must always be reconstructible from the log alone).
        Only on acceptance does this write the theta update, append the
        event, and fold the engine-state consume step (seen-state, KTD4)
        back in — mirroring `session.py`'s `_apply_and_log`, generalized
        for any caller rather than tied to the interactive loop's own
        `Serve` record.

        Returns a `SubmitOutcome`: `event=None, discard_reason=<str>` for
        the §3 abandonment guard's discard (nothing to log, not an error);
        `event=None, rejection_reason=<str>` for an ingest rejection; the
        logged event plus a `repeat` flag (KD5) on acceptance.

        **R19/AE7: retry-safety (`attempt_id`).** This is the "two-path
        idempotency design" the U6 plan documents in full; summarized here:

        - **Path A (true idempotent replay).** If `attempt_id` is already a
          key in `session.accepted_attempts` — because this exact
          `attempt_id` was accepted earlier in this same session, or
          because `open_session` seeded it from a prior session's log (see
          `_accepted_attempts_from_log`) — the stored `SubmitOutcome` is
          returned immediately: no re-validation, no re-write to
          `session.store`, no re-append to the log, no re-run of
          `engine.consume_event`. This is checked first, inside the
          session lock, before an `Attempt`/event is even constructed.
        - **Path B (legitimate resubmission against a stale bank).** If
          `attempt_id` is *not* in the map (this attempt was never
          accepted before — e.g. the session that would have accepted it
          crashed before validation ever completed) AND the served
          `instance`'s own stamped `bank_version` differs from
          `session.bank.bank_version` (a bank repoint moved `ACTIVE`
          between the crash and the client's resubmission under a new
          session), the ordinary `SessionContext`-based validation is
          skipped in favor of the session-less path
          `tools/replay_audit.py` already uses:
          `ingest_module.validate_event(event, None, self.bank_lookup,
          seen_state)` — this validates the event against *its own*
          stamped `bank_version` rather than demanding it match the
          current session's pin, so a legitimately-answered item from a
          now-superseded build isn't wrongly rejected as a bank mismatch.
          When `attempt_id` isn't in the map but the bank *does* match,
          this is just an ordinary first-time submission and the existing
          `SessionContext`-based validation runs unchanged.

        On acceptance via either path, the resulting `SubmitOutcome` is
        recorded in `session.accepted_attempts[attempt_id]` before
        returning, so any future resubmission of this same `attempt_id`
        hits Path A.

        R17: refuses with `ConsumerNotLinkedError` before touching
        anything — including before the Path A replay check — if
        `self.identity` is configured and `consumer_id` isn't linked to
        `session.student_id`. This is a deliberate choice: a Path A replay
        only ever returns data already computed, never applies anything
        new, but that data (theta, response fields) is still the
        student's; gating it behind the same link check as every other
        read on this facade (`get_progress`) is more conservative than
        letting an unlinked caller fish for a previously-accepted outcome
        by guessing `attempt_id`s, and costs a replaying caller nothing it
        wasn't already going to pay (the same consumer that submitted the
        original attempt is linked by construction). KTD17: the whole body
        (including the Path A check) runs under this session's own lock (a
        no-op lock if `session` was never registered via `open_session` —
        see `_lock_for`), so two concurrent submissions against the same
        session — including two concurrent retries of the same
        `attempt_id` — serialize instead of racing on
        `session.store`/`session.engine_state`/`session.accepted_attempts`.

        No `self.sessions.touch` call here either, for the same reason
        `fetch_batch`/`refill_batch` no longer call it (a code-review
        efficiency finding): the transport layer resolves `session` via
        `self.sessions.get` immediately before calling this method, and
        `get()` already reset the idle-eviction clock as a documented side
        effect — a second touch moments later is redundant work under the
        lock, not a correctness gap."""
        self._check_linked(consumer_id, session.student_id)
        with self._lock_for(session):
            # Path A: true idempotent replay (R19/AE7) — recognized before
            # anything else is built or touched.
            cached = session.accepted_attempts.get(attempt_id)
            if cached is not None:
                return cached

            attempt = Attempt(
                skill_ids=[skill_id],
                question_type=instance["question_type"],
                b_prior_at_serve=instance["difficulty"],
                correct=correct,
                hints_used=hints_used,
                timed_out=timed_out,
                engaged=engaged,
                session_last_event=session_last_event,
                context=context,
                misconception_id=misconception_id,
                event_id=f"evt-{uuid.uuid4().hex}",
                depth_served=instance["difficulty"],
            )
            state = session.store.get(session.student_id, skill_id)
            composed = log_module.compose_attempt(state, attempt, context, now)
            if composed.new_state is None:
                return SubmitOutcome(
                    event=None,
                    discard_reason=(
                        "abandonment guard: attempt timed out, the student was not engaged, and this "
                        "was the session's last event — the §3 abandonment guard discarded it outright, "
                        "nothing was written to the student model or the log"
                    ),
                )
            theta_before, theta_after, n_before = composed.theta_before, composed.theta_after, composed.n_before

            event = {
                "event_id": attempt.event_id,
                "attempt_id": attempt_id,
                "student_id": session.student_id,
                "session_id": session.session_id,
                "timestamp": now.isoformat(),
                "item_instance_id": instance["item_instance_id"],
                "template_id": instance["template_id"],
                "template_version": instance["template_version"],
                "bank_version": instance["bank_version"],
                "variant_signature": instance["variant_signature"],
                "consumer_id": consumer_id,
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
                "context": context,
                "theta_before": theta_before,
                "theta_after": theta_after,
                "n_before": n_before,
            }

            seen_state = {sig for sigs in session.engine_state.seen_history.values() for sig in sigs}

            # Path B trigger (R19): never-before-accepted attempt_id whose
            # own stamped bank_version differs from this session's current
            # pin — validate session-lessly against the event's own build
            # instead of rejecting it as a session/bank mismatch.
            if instance["bank_version"] != session.bank.bank_version:
                report = ingest_module.validate_event(event, None, self.bank_lookup, seen_state)
            else:
                session_ctx = ingest_module.SessionContext(
                    session_id=session.session_id,
                    student_id=session.student_id,
                    bank_version=session.bank.bank_version,
                )
                report = ingest_module.validate_event(event, session_ctx, self.bank_lookup, seen_state)

            if not report.accepted:
                return SubmitOutcome(event=None, rejection_reason=report.rejection_reason)

            session.store.write_records(session.student_id, {skill_id: composed.new_state})
            log_module.append_attempt(session.log_path, event)

            outcome = engine.AttemptOutcome(
                student_id=session.student_id,
                skill_ids=[skill_id],
                item_instance_id=instance["item_instance_id"],
                variant_signature=instance["variant_signature"],
                correct=correct,
                misconception_id=misconception_id,
                context=context,
                depth_served=instance["difficulty"],
            )
            consume_result = engine.consume_event(
                session.engine_state, session.store, self.content, session.bank, outcome, now
            )
            session.engine_state = consume_result.engine_state
            engine.apply_consume_patches(session.store, session.student_id, consume_result.skill_state_patches)
            submit_outcome = SubmitOutcome(event=event, repeat=report.repeat)
            session.accepted_attempts[attempt_id] = submit_outcome
            return submit_outcome

    def get_progress(self, student_id: str, consumer_id: str, now: datetime) -> ProgressSummary:
        """R5, session-less (U8): re-scoped from taking a `Session` to
        taking `student_id` directly. R20 allows only one active *serving*
        session per student; before this unit, a progress read required a
        live `Session`, which meant a second linked consumer (e.g. a
        school's tutor reading the same student a parent's app is actively
        serving) couldn't even *read* the student's state while the first
        consumer's session was open. This is the fix: replay the student's
        log fresh (`engine.rebuild_session_state`, KTD11 — the same replay
        `open_session` seeds a session from) into a scratch store/
        engine_state and summarize that, never touching any live
        `Session`'s in-memory state — so this read can never race with, or
        be blocked by, a concurrently open session for the same student.
        No curriculum parameter: theta/certification/seen-count are
        curriculum-neutral by design, unlike `recommend_next`.

        Same summary shape tools/replay_audit.py (U7) reproduces: theta,
        certification status, and per-skill seen count (KTD4's
        signature-keyed count, not a raw instance-id count).

        R17: refuses with `ConsumerNotLinkedError` when `self.identity` is
        configured and `consumer_id` isn't linked to `student_id` (AE6's
        "progress read without linking is refused" half)."""
        self._check_linked(consumer_id, student_id)
        store, engine_state = self._replay_or_fresh(self._log_path_for(student_id))
        return build_progress_summary(store, engine_state, student_id, now)

    def recommend_next(
        self,
        student_id: str,
        country: str,
        curriculum: str,
        grade: int,
        consumer_id: str,
        now: datetime,
        scope: Optional[str] = None,
        session: Optional[Session] = None,
        bank_version: Optional[str] = None,
    ) -> "RecommendationsResult":
        """R6/R7/KD4: a ranked, reasoned list of ready-to-learn skills,
        sharing `select_next`'s own R12 priority-chain logic
        (`engine.rank_serve_candidates` — R7's "exactly one implementation,
        two callers") read-only and session-less — the same reason
        `get_progress` above is session-less: a consumer linked to
        `student_id` can call this even while a *different* consumer holds
        the student's one active serving session (R20).

        Never mutates anything durable (KTD2's scratch-clone discipline,
        the same one the batch planner uses): replays the student's log
        fresh into a scratch `StudentModelStore`/`EngineState`
        (`engine.rebuild_session_state`, KTD11 — identical to
        `get_progress`'s own replay) and runs `rank_serve_candidates`
        against that scratch state. Nothing is written back: no seen-mark,
        no engine-state persisted, no store touched.

        **Bank-awareness (KTD3).** A recommended skill's servability
        depends on whether the *checked* bank actually holds instances for
        it. Pass `session` (an already-open `Session`) to check against
        exactly the build that session is pinned to — the natural choice
        for a caller mid-session; pass `bank_version` to check a specific
        retained build; pass neither to check the currently `ACTIVE` build
        (resolved the same way `open_session` does). A skill the checked
        bank cannot serve is never dropped from the list — it comes back
        with `unservable=True` so a caller can see it and decide, per this
        unit's plan point 3.

        **Scoping.** `scope`, when given, narrows the ranked list to one
        curriculum unit's skills (`orchestrator/catalog.py`'s `unit_code`
        vocabulary, U7's `list_units`/`list_skills`) — filtering happens
        *after* the full country/curriculum/grade-governed ranking, so
        narrowing the candidate set never narrows the rules (ceilings and
        prerequisite gating are computed the same regardless of `scope`).

        R17: refuses with `ConsumerNotLinkedError` before touching
        anything if `self.identity` is configured and `consumer_id` isn't
        linked to `student_id`."""
        self._check_linked(consumer_id, student_id)
        store, engine_state = self._replay_or_fresh(self._log_path_for(student_id))

        if session is not None:
            bank = session.bank
        else:
            resolved_version = bank_version or read_active_bank_version(self.banks_dir)
            bank = self.bank_lookup(resolved_version)

        ranked = engine.rank_serve_candidates(
            store, engine_state, self.content, bank, student_id, country, curriculum, grade, now
        )

        if scope is not None:
            scoped_skill_ids = {
                mapped.skill_id
                for mapped in catalog_module.list_skills(self.content, country, curriculum, grade, scope).skills
            }
            ranked = [candidate for candidate in ranked if candidate.skill_id in scoped_skill_ids]

        recommendations = [
            RecommendedSkill(
                skill_id=candidate.skill_id,
                reason=candidate.reason,
                context=candidate.context,
                target_depth=candidate.target_depth,
                unservable=candidate.unservable,
            )
            for candidate in ranked
        ]
        return RecommendationsResult(
            student_id=student_id,
            bank_version=bank.bank_version,
            content_version=self.content.content_version,
            recommendations=recommendations,
        )

    def close_session(self, session: Session, consumer_id: str) -> None:
        """Close-by-session_id: R17's link check, then unregister from
        `self.sessions` (a no-op if `session` was never registered — the
        pre-U5 stateless contract for that case, still relied on by the
        internal session runner's ephemeral `Session` objects). The
        closure is logged and attributed to `consumer_id` — ordinarily the
        session's own opener, but nothing here requires that; any linked
        consumer closing their *own* still-open session hits this same
        path."""
        self._check_linked(consumer_id, session.student_id)
        popped = self.sessions.pop(session.session_id)
        if popped is not None:
            self._log_closure(popped, consumer_id, reason="close_by_session_id")

    def close_session_by_student(self, student_id: str, consumer_id: str) -> bool:
        """KTD6 point 5 / R20's recovery path: close whatever session is
        currently live for `student_id`, keyed by student rather than
        session_id — the path a client that crashed and lost its
        session_id needs. Any consumer *linked* to `student_id` may do
        this regardless of who opened the session (R17 still gates it —
        just against `student_id`, not against the opener's identity);
        the closure is attributed to this closing `consumer_id`, not the
        original opener's (KTD6 point 5, explicitly). Returns whether a
        live session was actually found and closed."""
        self._check_linked(consumer_id, student_id)
        popped = self.sessions.pop_by_student(student_id)
        if popped is None:
            return False
        self._log_closure(popped, consumer_id, reason="close_by_student_id")
        return True

    # -- U7 catalog / skill-graph reads --------------------------------------
    #
    # KD3's student-independent discovery layer (R1-R4): reads over
    # `self.content`, delegated straight to orchestrator/catalog.py. No
    # session, no consumer_id, no `_check_linked` — R17's link enforcement
    # gates student-scoped operations (open_session, submit_attempt,
    # get_progress, close_session*); this layer is explicitly the one KD3
    # says stays open regardless (a partner asking "what does Mathiqo
    # cover" needs no student context to ask it). `list_skills` is the one
    # exception to "pure": it additionally annotates each skill against a
    # bank build (best-effort, degrading rather than raising — KD3 still
    # holds), same posture as `recommend_next`'s own `unservable` field.

    def list_curricula(self) -> catalog_module.CatalogResult:
        return catalog_module.list_curricula(self.content)

    def list_units(self, country: str, curriculum: str, grade: int) -> catalog_module.UnitsResult:
        return catalog_module.list_units(self.content, country, curriculum, grade)

    def list_skills(
        self,
        country: str,
        curriculum: str,
        grade: int,
        unit_code: Optional[str] = None,
        bank_version: Optional[str] = None,
    ) -> CatalogSkillsResult:
        """U7/R3 plus the deferred servability question it left open: every
        mapped skill for this scope, each carrying `servable` — True when
        the checked bank build holds at least one raw instance for it
        (`engine.bank_pool_for_skill`, the same predicate
        `rank_serve_candidates`'s remediation/stepdown/prereq_probe/
        placement branches use for their own `unservable` verdict — one
        predicate, two callers).

        Checks the currently `ACTIVE` build by default; pass `bank_version`
        to check a specific retained build instead (mirroring
        `recommend_next`'s own resolution, minus the `session` option —
        this catalog read has no session to pin against). KD3: the catalog
        stays open regardless of bank state — when no bank can be resolved
        or loaded at all (`banks_dir` is `None` with no explicit
        `bank_version`, no `ACTIVE` pointer, or the resolved build fails to
        load), every skill comes back `servable=False` with
        `bank_version=None` rather than raising."""
        result = catalog_module.list_skills(self.content, country, curriculum, grade, unit_code)
        bank, resolved_version = self._resolve_bank_for_catalog(bank_version)
        skills = [
            ServableSkill(
                skill_id=mapped.skill_id,
                depth_expectation=mapped.depth_expectation,
                learning_objective=mapped.learning_objective,
                terminology_overrides=mapped.terminology_overrides,
                unit_code=mapped.unit_code,
                unit_name=mapped.unit_name,
                servable=bank is not None and bool(engine.bank_pool_for_skill(bank, mapped.skill_id)),
            )
            for mapped in result.skills
        ]
        return CatalogSkillsResult(
            content_version=result.content_version,
            country=result.country,
            curriculum=result.curriculum,
            grade=result.grade,
            unit_code=result.unit_code,
            bank_version=resolved_version,
            skills=skills,
        )

    def _resolve_bank_for_catalog(
        self, bank_version: Optional[str]
    ) -> Tuple[Optional[engine.BankIndex], Optional[str]]:
        """Best-effort bank resolution for catalog reads (KD3): returns
        `(None, None)` instead of raising when nothing can be resolved or
        loaded — a `None` `banks_dir` with no explicit `bank_version`
        (`read_active_bank_version` would `TypeError`), a missing `ACTIVE`
        pointer (`NoActiveBankError`), or `self.bank_lookup` itself failing
        (a missing/corrupt build on disk, an unknown version against a
        static lookup) — so `list_skills` degrades to all-`servable=False`
        rather than regressing catalog availability over a bank-state
        problem.

        The version returned is the loaded bank's own `bank_version` (its
        manifest identity), never the `ACTIVE` pointer text that resolved
        it — `open_session` and `recommend_next` both stamp
        `bank.bank_version`, and a build directory whose name differs from
        its manifest would otherwise make this surface disagree with them
        about which build a partner is looking at.

        The degrade is logged: a swallowed exception here is
        indistinguishable in the response from a deployment that simply has
        no bank yet, so without the warning a corrupt build would silently
        advertise the whole catalog as unservable."""
        try:
            bank = self.bank_lookup(bank_version or read_active_bank_version(self.banks_dir))
            return bank, bank.bank_version
        except Exception as exc:
            logger.warning(
                "catalog servability degraded: bank_version=%r could not be resolved or loaded (%s) — "
                "reporting servable=False for every skill",
                bank_version,
                exc,
            )
            return None, None

    def get_skill_graph(
        self, skill_ids: "Optional[str | List[str]]" = None
    ) -> catalog_module.SkillGraphResult:
        return catalog_module.get_skill_graph(self.content, skill_ids)

    # -- U4 identity pass-throughs -----------------------------------------
    #
    # Thin delegation to `self.identity` (an `IdentityRegistry`, U4).

    def register_student(self, attributes: dict, consumer_id: str) -> str:
        if self.identity is None:
            raise NoIdentityRegistryError("this ServingFacade was constructed without an identity registry")
        return self.identity.register_student(attributes, consumer_id)

    def link_student(
        self,
        student_id: str,
        attributes: dict,
        consumer_id: str,
        link_basis: Optional[str] = None,
    ) -> None:
        if self.identity is None:
            raise NoIdentityRegistryError("this ServingFacade was constructed without an identity registry")
        self.identity.link_student(student_id, attributes, consumer_id, link_basis)

    def find_student(self, attributes: dict) -> Optional[str]:
        if self.identity is None:
            raise NoIdentityRegistryError("this ServingFacade was constructed without an identity registry")
        return self.identity.find_student(attributes)

    def is_linked(self, consumer_id: str, student_id: str) -> bool:
        if self.identity is None:
            raise NoIdentityRegistryError("this ServingFacade was constructed without an identity registry")
        return self.identity.is_linked(consumer_id, student_id)
