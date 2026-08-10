#!/usr/bin/env python3
"""MCP server (U8): the external product surface — orchestrator/serving.py's
facade exposed as MCP tools (KTD6).

**Parity rule, both directions (R14).** Every capability `orchestrator/
serving.py`'s `ServingFacade` exposes — serving/ingest (`open_session`,
`get_candidates`, `submit_attempt`, `close_session`,
`close_session_by_student`, `get_student_progress`, `recommend_next`),
identity (U4: `register_student`, `link_student`, `find_student`), catalog
(U7: `list_curricula`, `list_units`, `list_skills`, `get_skill_graph`), and
batch (U9/U10: `fetch_batch`, `refill_batch`) — is exposed here as exactly
one MCP tool, and every tool here resolves to exactly one facade operation
(`FACADE_OPERATIONS`) — no tool reaches past the facade to touch a bank
directory or a log file directly. This module is a thin adapter: no
serving/ingest logic lives here, only session-registry bookkeeping (an MCP
tool call is stateless; `orchestrator/serving.py`'s `Session` object is
not, so this module holds the open ones between calls) and JSON-shaping of
facade return values (`schemas/api/`'s declared response shapes, R15).

**Identity and catalog tools are session-less by design (U11).** The
identity tools (`register_student`/`link_student`/`find_student`) are the
*entry point* to linking, not gated behind an existing link. The catalog
tools (`list_curricula`/`list_units`/`list_skills`/`get_skill_graph`) are
KD3's student-independent discovery layer — no session, no `consumer_id`,
no link check, confirmed against `orchestrator/catalog.py`'s own
docstring and `ServingFacade`'s U7 pass-throughs.

**Per-connection identity (KTD15) does not apply to this transport.** MCP
is stdio-based: one server process serves one client conversation for its
whole life, so the `--consumer-id` startup flag (unchanged since a prior
unit) already *is* the "startup-configured, never caller-supplied"
resolution KTD15 asks for. There is no per-connection concept to resolve
within one MCP server process the way a later REST transport will need (one
HTTP server fielding requests from many different partners, needing an
actual multi-partner credential table) — that machinery is deliberately
out of scope here.

**consumer_id is server configuration, never the caller's.** `--consumer-id`
is set once at server startup (KTD2) and stamped on every attempt this
server submits; nothing in any tool's parameters can override it — a
payload cannot forge attribution for a different partner.

**Errors carry no filesystem paths.** A domain error whose message would
otherwise include a banks/logs directory path (`serving.NoActiveBankError`)
is replaced with a generic message before it reaches a tool error; ingest
rejection reasons (`orchestrator/ingest.py`'s `IngestReport.rejection_reason`)
are already path-free structured strings and pass through unchanged.

**v1 pre-stability.** `INTERFACE_VERSION` is reported by `server_info` —
this contract may still change before general availability (Scope
Boundaries: auth/transport hardening is explicitly deferred); early
partners should bind against the declared version and expect it to move,
not assume today's shape is final.

Usage:
    python tools/mcp_server.py --banks-dir banks --logs-dir logs \\
        --consumer-id partner.acme [--content content/fractions]
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from orchestrator import batch as batch_module  # noqa: E402
from orchestrator import catalog as catalog_module  # noqa: E402
from orchestrator import constants, engine, serving, signals  # noqa: E402
from orchestrator.identity import AttributeMismatchError, UnknownStudentError  # noqa: E402

INTERFACE_VERSION = "1.0.0-pre"

# KTD6's parity rule as data: every facade operation this server exposes.
# tests/test_mcp_server.py asserts this against both the facade's own
# public methods and this server's registered tool names.
#
# U11 adds nine operations built by prior units (identity/U4, catalog/U7,
# batch/U9-U10) that had a `ServingFacade` method but no MCP tool until now
# — R14's parity rule, closed.
FACADE_OPERATIONS = (
    "open_session",
    "get_candidates",
    "submit_attempt",
    "close_session",
    "close_session_by_student",
    "get_student_progress",
    "recommend_next",
    "register_student",
    "link_student",
    "find_student",
    "list_curricula",
    "list_units",
    "list_skills",
    "get_skill_graph",
    "fetch_batch",
    "refill_batch",
)


def _resolve_instance(
    facade: serving.ServingFacade, session_obj: serving.Session, item_instance_id: str
) -> "dict[str, Any] | None":
    """`submit_attempt`'s instance-resolution step, shared by this module
    and `tools/rest_server.py` (which reuses it as `mcp_server._resolve_instance`,
    the same cross-module reuse posture as `_serialize_progress` etc.).

    An `item_instance_id` is scoped to the bank build it was materialized
    from — `tools/bank_build.py`'s `materialize_instance` stamps it
    `f"{template_id}#{bank_version}#{rank:05d}"`, and the instance dict
    itself always carries that same `bank_version`
    (`schemas/item-instance.schema.json`). A session's own pinned bank
    (`session_obj.bank.by_id`) only ever indexes instances from the one
    build it opened against (KTD3, R3/R4) — so an instance legitimately
    served from a build that a later `bank_activate` repoint has since
    superseded can never be found there once a *new* session (pinned to
    the newer build) is the one resubmitting it. That's exactly
    `ServingFacade.submit_attempt`'s Path B recovery scenario (R19): without
    this fallback, this lookup returning `None` and the caller raising
    `unresolvable_instance` right here means Path B's session-less
    validation is never reached — this whole recovery path was dead code
    from any real client, confirmed independently by two reviewers.

    Falls back to the item_instance_id's own embedded bank_version,
    resolved against any retained build `facade.bank_lookup` can still
    load (KTD7 — the same "any retained build" machinery
    `tools/replay_audit.py` and `engine.rebuild_session_state` already use
    for per-event resolution, not just the currently-ACTIVE one). Returns
    `None` — the caller still raises `unresolvable_instance` — only when
    `item_instance_id` is a genuinely bogus/foreign id: absent from the
    session's own bank AND absent from (or its embedded version names no)
    any retained build."""
    instance = session_obj.bank.by_id.get(item_instance_id)
    if instance is not None:
        return instance

    # bank_build.py's own convention: "<template_id>#<bank_version>#<rank>".
    # template_id (schemas/item-instance.schema.json:
    # "^TPL\\.[A-Z0-9][A-Z0-9.-]*$") and the zero-padded numeric rank suffix
    # never contain '#', so the first and last '#' in the id bound
    # bank_version even in the unlikely case bank_version itself contains
    # one — more robust than a plain split("#") would be.
    first_hash = item_instance_id.find("#")
    last_hash = item_instance_id.rfind("#")
    if first_hash == -1 or last_hash == first_hash:
        return None
    candidate_version = item_instance_id[first_hash + 1 : last_hash]
    if not candidate_version or candidate_version == session_obj.bank.bank_version:
        # Malformed, or names the bank already checked above.
        return None
    try:
        stale_bank = facade.bank_lookup(candidate_version)
    except Exception:
        # No retained build under that name (evicted from disk, or the
        # extracted segment was never a real bank_version at all) — treat
        # as unresolvable, same as if the id were simply unknown.
        return None
    return stale_bank.by_id.get(item_instance_id)


def _serialize_progress(summary: serving.ProgressSummary) -> dict[str, Any]:
    """`get_student_progress`'s declared contract: this exact shape, not
    whatever fields ProgressSummary happens to carry — a schema partners
    can bind to."""
    return {
        "student_id": summary.student_id,
        "skills": {
            skill_id: {
                "skill_id": p.skill_id,
                "theta": p.theta,
                "n": p.n,
                "certified_depth": p.certified_depth,
                "certification_status": p.certification_status,
                "seen_count": p.seen_count,
            }
            for skill_id, p in summary.skills.items()
        },
    }


def _serialize_recommendations(result: serving.RecommendationsResult) -> dict[str, Any]:
    """`recommend_next`'s declared contract: a ranked list of
    skill/reason/context/target_depth/unservable entries (R6/R7/KTD3).
    `content_version` (R15/KTD9) was missing from this response until a
    code-review pass caught it — every catalog/graph/recommendation read
    must stamp it, per the plan's own requirement."""
    return {
        "student_id": result.student_id,
        "bank_version": result.bank_version,
        "content_version": result.content_version,
        "recommendations": [
            {
                "skill_id": r.skill_id,
                "reason": r.reason,
                "context": r.context,
                "target_depth": r.target_depth,
                "unservable": r.unservable,
            }
            for r in result.recommendations
        ],
    }


def _serialize_registration(student_id: str) -> dict[str, Any]:
    """`register_student`'s declared contract: just the new opaque
    student_id — the registered attributes themselves never leave
    `IdentityRegistry`'s own file (KD9/R16)."""
    return {"student_id": student_id}


def _serialize_link(student_id: str, consumer_id: str, link_basis: str | None) -> dict[str, Any]:
    """`link_student`'s declared contract: confirmation of the link that
    was just created, echoing back only opaque ids and the caller's own
    `link_basis` — never the attributes that proved the match."""
    return {"linked": True, "student_id": student_id, "consumer_id": consumer_id, "link_basis": link_basis}


def _serialize_lookup(student_id: "str | None") -> dict[str, Any]:
    """`find_student`'s declared contract: whether a match was found and,
    if so, the opaque student_id — never the attributes that were looked
    up, which the caller already has."""
    return {"found": student_id is not None, "student_id": student_id}


def _serialize_catalog(result: catalog_module.CatalogResult) -> dict[str, Any]:
    """`list_curricula`'s declared contract: R1's country -> curriculum ->
    grades grouping, verbatim off `catalog_module.CatalogResult`."""
    return {
        "content_version": result.content_version,
        "countries": [
            {
                "country": country.country,
                "curricula": [
                    {"curriculum": curriculum.curriculum, "grades": list(curriculum.grades)}
                    for curriculum in country.curricula
                ],
            }
            for country in result.countries
        ],
    }


def _serialize_units(result: catalog_module.UnitsResult) -> dict[str, Any]:
    """`list_units`'s declared contract: KTD10's chapter list (or the bare
    `skill_ids` fallback for a unit-less curriculum/uncovered scope)."""
    return {
        "content_version": result.content_version,
        "country": result.country,
        "curriculum": result.curriculum,
        "grade": result.grade,
        "has_units": result.has_units,
        "units": [
            {"unit_code": unit.unit_code, "unit_name": unit.unit_name, "skill_ids": list(unit.skill_ids)}
            for unit in result.units
        ],
        "skill_ids": list(result.skill_ids),
    }


def _serialize_skills(result: serving.CatalogSkillsResult) -> dict[str, Any]:
    """`list_skills`'s declared contract: R3's curriculum-local
    presentation (learning objective, depth expectation, terminology
    overrides) per mapped skill, plus `servable` — True when the checked
    bank build (`bank_version`, null if none resolved) holds at least one
    instance for that skill. Resolved from `ServingFacade.list_skills`
    (U14), which degrades to `servable=False`/`bank_version=null` rather
    than raising when no bank can be resolved or loaded (KD3)."""
    return {
        "content_version": result.content_version,
        "country": result.country,
        "curriculum": result.curriculum,
        "grade": result.grade,
        "unit_code": result.unit_code,
        "bank_version": result.bank_version,
        "skills": [
            {
                "skill_id": skill.skill_id,
                "depth_expectation": skill.depth_expectation,
                "learning_objective": skill.learning_objective,
                "terminology_overrides": dict(skill.terminology_overrides),
                "unit_code": skill.unit_code,
                "unit_name": skill.unit_name,
                "servable": skill.servable,
            }
            for skill in result.skills
        ],
    }


def _serialize_skill_graph(result: catalog_module.SkillGraphResult) -> dict[str, Any]:
    """`get_skill_graph`'s declared contract: R4's canonical graph nodes,
    keyed by skill_id."""
    return {
        "content_version": result.content_version,
        "skills": {
            skill_id: {
                "skill_id": node.skill_id,
                "strand": node.strand,
                "canonical_stage": node.canonical_stage,
                "prerequisites": list(node.prerequisites),
                "misconceptions": list(node.misconceptions),
            }
            for skill_id, node in result.skills.items()
        },
    }


def _serialize_batch_item(item: batch_module.PlannedItem) -> dict[str, Any]:
    return {"instance": item.instance, "skill_id": item.skill_id, "rung": item.rung, "context": item.context}


def _serialize_batch_result(result: batch_module.BatchResult) -> dict[str, Any]:
    """`fetch_batch`'s declared contract: the full laddered batch (R8-R11)
    — every item's full rendered instance (KD6, nothing stripped), which
    rung it fills, the submission context the double-diagnosis rule
    (orchestrator/batch.py's module docstring) requires, and the shipped
    branching-policy object (KD2, KTD8)."""
    return {
        "batch_id": result.batch_id,
        "student_id": result.student_id,
        "skill_id": result.skill_id,
        "items": [_serialize_batch_item(item) for item in result.items],
        "count_requested": result.count_requested,
        "count_returned": result.count_returned,
        "policy": {
            "policy_version": result.policy.policy_version,
            "floor": result.policy.floor,
            "low_water": result.policy.low_water,
            "rules": dict(result.policy.rules),
        },
        "feedback_sidecar": dict(result.feedback_sidecar),
        "bank_version": result.bank_version,
        "content_version": result.content_version,
    }


def _serialize_refill_result(result: batch_module.RefillResult) -> dict[str, Any]:
    """`refill_batch`'s declared contract: the freshly planned batch (or
    `null` with `signal=no_further_items` when nothing new is left, R13),
    plus `supersede` (KTD5) on every response regardless."""
    return {
        "batch": _serialize_batch_result(result.batch) if result.batch is not None else None,
        "signal": result.signal,
        "supersede": result.supersede,
    }


def build_server(facade: serving.ServingFacade, consumer_id: str, name: str = "mathiqo-serving") -> FastMCP:
    """Wires `FACADE_OPERATIONS` up as MCP tools over `facade`. Session
    state itself lives in `facade.sessions` (U5's `SessionRegistry`,
    KTD12) — this server no longer keeps its own session dict, only
    resolves a caller's `session_id` against the shared registry on every
    call, so a later REST transport sharing the same `facade` (same
    process, KTD15) sees the same live sessions."""
    mcp = FastMCP(name)

    def _session_or_raise(session_id: str) -> serving.Session:
        try:
            return facade.sessions.get(session_id)
        except serving.SessionUnknownError as exc:
            raise ToolError(f"{signals.SESSION_UNKNOWN}: {exc}") from exc
        except serving.SessionExpiredError as exc:
            raise ToolError(f"{signals.SESSION_EXPIRED}: {exc}") from exc

    @mcp.tool()
    def open_session(student_id: str, country: str, curriculum: str, grade: int) -> dict[str, Any]:
        """Open a session pinned to the currently active bank build (R3)
        and to `country`/`curriculum`/`grade` (KTD3) for its whole life.
        Returns the session_id every other tool call needs, and the
        bank_version it pinned (visible on every response, R3).

        R17: refused (`not_linked`) if this server's `consumer_id` isn't
        linked to `student_id` (only when the facade was configured with
        an identity registry). R20/AE9: refused (`session_conflict`) if a
        *different* consumer already has a live session for this student
        — the error names `close_session_by_student` as the recovery
        path; a same-consumer reopen instead takes over silently (the
        prior session is closed and this one opens fresh)."""
        session_id = f"sess-{uuid.uuid4().hex}"
        try:
            session_obj = facade.open_session(session_id, student_id, country, curriculum, grade, consumer_id)
        except serving.NoActiveBankError as exc:
            raise ToolError("no active bank build is configured for this deployment") from exc
        except serving.InvalidIdentifierError as exc:
            raise ToolError(str(exc)) from exc
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        except serving.SessionConflictError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "session_id": session_id,
            "student_id": student_id,
            "bank_version": session_obj.bank.bank_version,
        }

    @mcp.tool()
    def get_candidates(session_id: str, skill_id: str) -> dict[str, Any]:
        """R1, R2, R13: every unseen instance in the pinned bank for
        (student, skill). `signal` is `null` in the ordinary case and one
        of three explicit strings otherwise (`CandidatesResult`'s own
        docstring, orchestrator/serving.py): `unknown_skill` (skill_id
        isn't in the content graph), `empty_pool` (a known skill, but the
        pinned bank has no instances for it), or `exhausted` (pool
        exhaustion — the pre-U10 behavior: `instances` holds exactly the
        one least-recently-seen repeat). `exhausted` (the boolean field)
        is kept for backward compatibility and is `true` only for the
        `exhausted` signal.

        R17: refused (`not_linked`) if this server's `consumer_id` isn't
        linked to the session's student (only when the facade was
        configured with an identity registry) — matching every other
        student-scoped tool."""
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.get_candidates(session_obj, skill_id, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        return {
            "bank_version": result.bank_version,
            "instances": result.instances,
            "exhausted": result.exhausted,
            "signal": result.signal,
            "warning": result.warning,
        }

    @mcp.tool()
    def submit_attempt(
        session_id: str,
        attempt_id: str,
        skill_id: str,
        item_instance_id: str,
        response_raw: str,
        response_parsed: str | None,
        correct: bool,
        misconception_id: str | None,
        hints_used: int,
        hint_auto_offered: bool,
        time_taken_sec: float,
        timed_out: bool,
        engaged: bool,
        session_last_event: bool,
        context: str,
    ) -> dict[str, Any]:
        """R5, R6: validates and logs one attempt. `item_instance_id`
        resolves first against this session's own pinned bank server-side
        (a caller can never smuggle in an instance from elsewhere), then —
        only if not found there — against whatever retained build it was
        actually served from (`_resolve_instance`, R19 Path B's transport
        half: an instance from a now-superseded build must still resolve
        so the facade's session-less stale-bank validation is reachable);
        `consumer_id` is this server's own configuration (KTD2), never a
        parameter here. A KTD5 rejection raises a structured tool error
        naming the failing check; an accepted repeat (KD5) is not an
        error — `repeat` is `true` in the returned result.

        R19/AE7: `attempt_id` is a required client-supplied idempotency
        key. Resubmitting the same `attempt_id` — even across a session
        restart, once the client reopens a session and retries — returns
        the original `logged`/`repeat`/`event_id` result unchanged rather
        than double-applying the attempt (`ServingFacade.submit_attempt`'s
        two-path idempotency design)."""
        session_obj = _session_or_raise(session_id)
        instance = _resolve_instance(facade, session_obj, item_instance_id)
        if instance is None:
            raise ToolError(
                f"unresolvable_instance: {item_instance_id!r} is not in this session's pinned bank "
                "or any retained bank build"
            )

        try:
            outcome = facade.submit_attempt(
                session_obj, attempt_id, skill_id, instance,
                response_raw=response_raw, response_parsed=response_parsed, correct=correct,
                misconception_id=misconception_id, hints_used=hints_used, hint_auto_offered=hint_auto_offered,
                time_taken_sec=time_taken_sec, timed_out=timed_out, engaged=engaged,
                session_last_event=session_last_event, context=context,
                consumer_id=consumer_id, now=datetime.now(),
            )
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        if outcome.rejection_reason is not None:
            raise ToolError(f"{signals.INGEST_REJECTED}: {outcome.rejection_reason}")
        if outcome.event is None:
            return {
                "logged": False,
                "repeat": False,
                "signal": signals.DISCARDED,
                "discard_reason": outcome.discard_reason,
            }
        return {"logged": True, "repeat": outcome.repeat, "event_id": outcome.event["event_id"]}

    @mcp.tool()
    def close_session(session_id: str) -> dict[str, Any]:
        """Ends the session: R17's link check, then removes it from the
        shared registry (`facade.sessions`) — `closed` is `false`, not an
        error, when `session_id` names a session that's already closed,
        idle-evicted, or was never opened (mirrors the pre-U5 stateless
        close's tolerance of a redundant call)."""
        try:
            session_obj = facade.sessions.get(session_id)
        except (serving.SessionUnknownError, serving.SessionExpiredError):
            return {"closed": False}
        try:
            facade.close_session(session_obj, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        return {"closed": True}

    @mcp.tool()
    def close_session_by_student(student_id: str) -> dict[str, Any]:
        """KTD6 point 5 / R20's recovery path: close whatever session is
        currently live for `student_id`, keyed by student rather than
        session_id — the path a client that crashed and lost its
        session_id needs. Any consumer linked to `student_id` may do this
        regardless of who opened the session; the closure is attributed
        to this server's own `consumer_id` (the caller), not the
        session's original opener."""
        try:
            closed = facade.close_session_by_student(student_id, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        return {"closed": closed}

    @mcp.tool()
    def get_student_progress(student_id: str) -> dict[str, Any]:
        """The declared progress summary (theta, certification status,
        per-skill seen count) — the same shape tools/replay_audit.py (U7)
        reproduces from a log alone.

        U8/R5: session-less — keyed by `student_id` directly rather than a
        `session_id`, so a consumer linked to this student can read
        progress even while a *different* consumer's serving session for
        this student is the one active session R20 allows (F4's "both
        groups' progress reads reflect all linked-group activity")."""
        try:
            summary = facade.get_progress(student_id, consumer_id, datetime.now())
        except serving.InvalidIdentifierError as exc:
            raise ToolError(str(exc)) from exc
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        return _serialize_progress(summary)

    @mcp.tool()
    def recommend_next(
        student_id: str,
        country: str,
        curriculum: str,
        grade: int,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """R6/R7: a ranked, reasoned list of ready-to-learn skills, sharing
        the same selection intelligence `submit_attempt`'s live serving
        uses (KD4: "pedagogical intelligence stays server-side; partners
        render and report") — read-only and session-less, exactly like
        `get_student_progress`: callable even while a different consumer
        holds this student's one active serving session (R20).

        `scope`, when given, narrows the ranked list to one curriculum
        unit (U7's `unit_code` vocabulary from `list_units`/`list_skills`)
        without changing which rules governed the ranking. Checked against
        the currently `ACTIVE` bank build; each entry's `unservable` flags
        a skill that build has no instances for — never silently dropped
        from the list."""
        try:
            result = facade.recommend_next(
                student_id, country, curriculum, grade, consumer_id, datetime.now(), scope=scope,
            )
        except serving.InvalidIdentifierError as exc:
            raise ToolError(str(exc)) from exc
        except serving.NoActiveBankError as exc:
            raise ToolError("no active bank build is configured for this deployment") from exc
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        return _serialize_recommendations(result)

    # -- U11: identity tools (U4's IdentityRegistry) -------------------------
    #
    # Student-scoped but session-less and consumer-id-free of their own
    # accord: these are the *entry point* to linking, not gated behind an
    # existing link the way open_session/submit_attempt/etc. are.
    # `consumer_id` is still always this server's own configuration (R17's
    # unforgeable invariant), never a tool parameter.

    @mcp.tool()
    def register_student(attributes: dict[str, Any]) -> dict[str, Any]:
        """U4: mint a new opaque student_id for `attributes` and
        auto-link this server's own `consumer_id` to it (registration is
        that first link's provenance). Returns only the opaque id — the
        attributes themselves are never echoed back (KD9/R16: they exist
        solely inside `IdentityRegistry`'s own file from this point on).

        Refused with a generic error (no filesystem path) if this
        deployment has no identity registry configured at all."""
        try:
            student_id = facade.register_student(attributes, consumer_id)
        except serving.NoIdentityRegistryError as exc:
            raise ToolError(str(exc)) from exc
        return _serialize_registration(student_id)

    @mcp.tool()
    def link_student(student_id: str, attributes: dict[str, Any], link_basis: str | None = None) -> dict[str, Any]:
        """U4/R17: link this server's own `consumer_id` to `student_id` —
        refused unless `attributes` exactly matches what `student_id` was
        registered with (no fuzzy matching). `link_basis` is recorded
        verbatim and unverified (a consent-provenance field for a deferred
        consent UX).

        Raises a structured tool error (not part of KTD7's closed
        ten-signal taxonomy, same posture as `open_session`'s
        `InvalidIdentifierError`) for an unregistered `student_id` or a
        mismatched `attributes` dict — either way, the error message never
        echoes the registered attributes back, only names the failure."""
        try:
            facade.link_student(student_id, attributes, consumer_id, link_basis)
        except serving.NoIdentityRegistryError as exc:
            raise ToolError(str(exc)) from exc
        except UnknownStudentError as exc:
            raise ToolError(str(exc)) from exc
        except AttributeMismatchError as exc:
            raise ToolError(str(exc)) from exc
        return _serialize_link(student_id, consumer_id, link_basis)

    @mcp.tool()
    def find_student(attributes: dict[str, Any]) -> dict[str, Any]:
        """U4: exact-match lookup only (no fuzzy/partial matching) —
        returns the opaque student_id if `attributes` matches an existing
        registration, `found=False`/`student_id=null` otherwise. Never
        echoes `attributes` back."""
        try:
            student_id = facade.find_student(attributes)
        except serving.NoIdentityRegistryError as exc:
            raise ToolError(str(exc)) from exc
        return _serialize_lookup(student_id)

    # -- U11: catalog tools (U7's student-independent discovery layer) -------
    #
    # Pure reads over the loaded content graph: no session, no consumer_id,
    # no link check (KD3 — this layer stays open regardless of linking).

    @mcp.tool()
    def list_curricula() -> dict[str, Any]:
        """U7/R1: every (country, curriculum, grade) triple Mathiqo covers,
        grouped country -> curriculum -> sorted grades."""
        return _serialize_catalog(facade.list_curricula())

    @mcp.tool()
    def list_units(country: str, curriculum: str, grade: int) -> dict[str, Any]:
        """U7/R1/R2/KTD10: the chapter/unit list for one (country,
        curriculum, grade) scope, built from mapping-layer unit_code/
        unit_name — never from parsing syllabus_ref. An uncovered scope or
        a unit-less curriculum returns `has_units=False`, never an error
        (plan point 6)."""
        return _serialize_units(facade.list_units(country, curriculum, grade))

    @mcp.tool()
    def list_skills(country: str, curriculum: str, grade: int, unit_code: str | None = None) -> dict[str, Any]:
        """U7/R3: the mapped skills for a curriculum scope, each with its
        curriculum-local presentation (learning objective, depth
        expectation, terminology overrides). `unit_code`, when given,
        narrows to just that chapter's skills.

        Each entry also carries `servable` (U14) — True when the currently
        `ACTIVE` bank build has at least one instance for that skill, never
        dropped when false — with `bank_version` naming the checked build
        (`null` if no bank could be resolved, in which case every skill is
        `servable=False`; the catalog stays open regardless, KD3)."""
        return _serialize_skills(facade.list_skills(country, curriculum, grade, unit_code))

    @mcp.tool()
    def get_skill_graph(skill_ids: list[str] | None = None) -> dict[str, Any]:
        """U7/R4: the canonical skill graph — prerequisites, depth axis
        (canonical_stage), strand, and associated misconception ids.
        `skill_ids` omitted (default) returns the whole graph; an id
        naming no skill is silently absent from the result rather than
        raising (plan point 6)."""
        return _serialize_skill_graph(facade.get_skill_graph(skill_ids))

    # -- U11: batch tools (U9/U10's ladder batch planner) ---------------------

    @mcp.tool()
    def fetch_batch(
        session_id: str, skill_id: str, batch_size: int = constants.DEFAULT_BATCH_SIZE
    ) -> dict[str, Any]:
        """U9 (R8-R11, R18, KD1, KD2): a server-planned, difficulty-laddered
        working set for one (student, skill) — the batch-serving sibling
        of `get_candidates`. `batch_size` is silently clamped to
        `[1, MAX_BATCH_SIZE]`, never rejected.

        R17: refused (`not_linked`) if this server's `consumer_id` isn't
        linked to the session's student. A misconception referenced by a
        served distractor with no child-facing feedback text (AE8) raises
        a structured tool error naming the gap, not part of KTD7's closed
        taxonomy (a real content-authoring bug, not a caller-facing
        signal)."""
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.fetch_batch(session_obj, skill_id, consumer_id, batch_size)
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        except batch_module.BatchPlanningError as exc:
            raise ToolError(str(exc)) from exc
        return _serialize_batch_result(result)

    @mcp.tool()
    def refill_batch(session_id: str, skill_id: str, attempts_flushed: bool) -> dict[str, Any]:
        """U10 (R12, KTD5): re-plan a batch for `skill_id` against this
        session's current live state. `attempts_flushed` is the client's
        protocol-level assertion that every attempt it intends to submit
        before this refill has already been submitted (declared, not
        verified — a `False` value is logged, not refused).

        R13: when the freshly planned batch has zero new items, returns
        `batch=null` with `signal=no_further_items` instead of an empty
        batch a caller could mistake for "try again shortly". `supersede`
        (KTD5) is set on every response regardless, including a
        `no_further_items` one.

        R17: refused (`not_linked`) if this server's `consumer_id` isn't
        linked to the session's student."""
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.refill_batch(session_obj, skill_id, consumer_id, attempts_flushed)
        except serving.ConsumerNotLinkedError as exc:
            raise ToolError(f"{signals.NOT_LINKED}: {exc}") from exc
        except batch_module.BatchPlanningError as exc:
            raise ToolError(str(exc)) from exc
        return _serialize_refill_result(result)

    @mcp.tool()
    def server_info() -> dict[str, Any]:
        """The declared interface version (pre-stability notice) and the
        full facade-operation surface this server exposes."""
        return {"interface_version": INTERFACE_VERSION, "facade_operations": list(FACADE_OPERATIONS)}

    return mcp


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--banks-dir", required=True, type=Path, help="banks directory (ACTIVE pointer + retained builds)")
    parser.add_argument("--logs-dir", required=True, type=Path, help="per-student attempt-log directory (point 4)")
    parser.add_argument(
        "--content", type=Path, default=REPO_ROOT / "content" / "fractions",
        help="content directory for the skill graph (default content/fractions)",
    )
    parser.add_argument(
        "--consumer-id", required=True,
        help="this deployment's partner identity (KTD2) — stamped on every submitted event, never taken from a tool call",
    )
    args = parser.parse_args(argv)

    if not args.banks_dir.is_dir():
        print(f"error: banks directory {args.banks_dir} does not exist", file=sys.stderr)
        return 2
    if not args.content.is_dir():
        print(f"error: content directory {args.content} does not exist", file=sys.stderr)
        return 2

    content = engine.load_content(args.content)
    facade = serving.ServingFacade(args.banks_dir, args.logs_dir, content)
    server = build_server(facade, args.consumer_id)
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
