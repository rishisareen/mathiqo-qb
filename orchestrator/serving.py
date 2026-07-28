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

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import constants
from . import engine
from . import ingest as ingest_module
from . import log as log_module
from .identity import IdentityRegistry
from .state import StudentModelStore, certification_status
from .update import Attempt

ACTIVE_POINTER_NAME = "ACTIVE"

# Point 3: alphanumerics, dot, dash, underscore; bounded length; no path
# separators. Mirrored onto schemas/attempt-event.schema.json's student_id.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
    repoint cannot shift an already-open session's content (R4)."""

    session_id: str
    student_id: str
    bank: engine.BankIndex
    log_path: Path
    store: StudentModelStore
    engine_state: engine.EngineState


@dataclass
class CandidatesResult:
    """R1: `instances` is every unseen instance in the pinned bank for the
    requested skill; when the pool is exhausted, `instances` holds exactly
    the one least-recently-seen repeat and `exhausted` is True with
    `warning` naming it explicitly (§5.4's pool-utilization signal)."""

    bank_version: str
    instances: List[dict]
    exhausted: bool
    warning: Optional[str] = None


@dataclass
class SubmitOutcome:
    """Three-way result the caller must distinguish (U6's "ingest rejection
    surfaces as an error, not a silent drop"): `event` is set only when the
    attempt was accepted and logged; a rejection sets `rejection_reason`
    (KTD5) and leaves `session.store`/`session.engine_state` untouched — the
    §3 abandonment guard discarding the attempt sets neither and is not an
    error, just nothing to log. `repeat` (KD5) is never written onto the
    event itself; it only ever lives here."""

    event: Optional[dict]
    rejection_reason: Optional[str] = None
    repeat: bool = False


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
    doesn't have). `banks_dir`/`logs_dir` may then be `None` — nothing
    reads them unless `open_session` is actually called.

    `identity`, when given, is an already-constructed `IdentityRegistry`
    (U4) — this facade never derives its path or constructs one itself,
    it only holds the instance a caller configured. It defaults to `None`:
    this unit does not wire link enforcement into `open_session` /
    `submit_attempt` / etc. (that's a later unit), it only exposes the
    registry's own operations as thin pass-throughs below, so most callers
    and every pre-U4 test can keep constructing a `ServingFacade` exactly
    as before."""

    def __init__(
        self,
        banks_dir: Optional[Path],
        logs_dir: Optional[Path],
        content: engine.ContentIndex,
        bank_lookup: Optional[engine.BankLookup] = None,
        identity: Optional[IdentityRegistry] = None,
    ):
        self.banks_dir = banks_dir
        self.logs_dir = logs_dir
        self.content = content
        self.bank_lookup = bank_lookup or engine.make_bank_lookup(banks_dir)
        self.identity = identity

    def _log_path_for(self, student_id: str) -> Path:
        validate_identifier(student_id, "student_id")
        return self.logs_dir / f"{student_id}.jsonl"

    def open_session(self, session_id: str, student_id: str) -> Session:
        """KTD3: resolve ACTIVE once, load and hold that build's
        BankIndex. Replays the student's existing log (if any) through
        KTD7's per-event bank resolution, so seen-state derived across a
        build boundary is correct even though only the *new* bank is
        pinned for serving (the session-open half of KTD7)."""
        validate_identifier(session_id, "session_id")
        log_path = self._log_path_for(student_id)
        bank_version = read_active_bank_version(self.banks_dir)
        bank = self.bank_lookup(bank_version)
        if log_path.is_file():
            store, engine_state = engine.rebuild_session_state(log_path, self.content, self.bank_lookup)
        else:
            store, engine_state = StudentModelStore(), engine.EngineState()
        return Session(
            session_id=session_id,
            student_id=student_id,
            bank=bank,
            log_path=log_path,
            store=store,
            engine_state=engine_state,
        )

    def get_candidates(self, session: Session, skill_id: str) -> CandidatesResult:
        """R1, R2: seen-filtered by variant_signature (KTD4), drawn only
        from `session.bank` (never re-resolved mid-session, R3/R4)."""
        pool = session.bank.instances_by_skill.get(skill_id, [])
        seen_history = session.engine_state.seen_history.get(skill_id, [])
        seen = set(seen_history)
        unseen = [inst for inst in pool if inst["variant_signature"] not in seen]
        if unseen or not pool:
            return CandidatesResult(bank_version=session.bank.bank_version, instances=unseen, exhausted=False)

        last_seen_at = {sig: idx for idx, sig in enumerate(seen_history)}
        ranked = sorted(
            pool,
            key=lambda inst: (last_seen_at.get(inst["variant_signature"], -1), inst["item_instance_id"]),
        )
        chosen = ranked[0]
        warning = (
            f"pool exhausted for skill {skill_id!r}: repeating least-recently-seen "
            f"instance {chosen['item_instance_id']!r}"
        )
        return CandidatesResult(
            bank_version=session.bank.bank_version, instances=[chosen], exhausted=True, warning=warning
        )

    def submit_attempt(
        self,
        session: Session,
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

        Returns a `SubmitOutcome`: `event=None, rejection_reason=None` for
        the §3 abandonment guard's discard (nothing to log, not an error);
        `event=None, rejection_reason=<str>` for an ingest rejection; the
        logged event plus a `repeat` flag (KD5) on acceptance."""
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
            return SubmitOutcome(event=None)
        theta_before, theta_after, n_before = composed.theta_before, composed.theta_after, composed.n_before

        event = {
            "event_id": attempt.event_id,
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

        session_ctx = ingest_module.SessionContext(
            session_id=session.session_id,
            student_id=session.student_id,
            bank_version=session.bank.bank_version,
        )
        seen_state = {sig for sigs in session.engine_state.seen_history.values() for sig in sigs}
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
        return SubmitOutcome(event=event, repeat=report.repeat)

    def get_progress(self, session: Session, now: datetime) -> ProgressSummary:
        """The same summary tools/replay_audit.py (U7) reproduces: theta,
        certification status, and per-skill seen count (KTD4's
        signature-keyed count, not a raw instance-id count)."""
        return build_progress_summary(session.store, session.engine_state, session.student_id, now)

    def close_session(self, session: Session) -> None:
        """No-op: the facade is stateless between calls (every Session
        object is self-contained and nothing outside it is held open) —
        kept as an explicit lifecycle method so callers (and the MCP tool
        surface, KTD6) have a symmetric open/close pair regardless."""
        return None

    # -- U4 identity pass-throughs -----------------------------------------
    #
    # Thin delegation to `self.identity` (an `IdentityRegistry`, U4) so
    # later units can build on a facade that already exposes registration
    # and linking — none of these are wired into open_session/
    # submit_attempt/get_candidates/get_progress above; that enforcement is
    # a later unit's job (see the constructor docstring).

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
