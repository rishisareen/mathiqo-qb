#!/usr/bin/env python3
"""MCP server (U8): the external product surface — orchestrator/serving.py's
facade exposed as MCP tools (KTD6).

**Parity rule, both directions.** Every serving/ingest operation the
reference orchestrator uses (`open_session`, `get_candidates`,
`submit_attempt`, `close_session`, `get_student_progress` —
`orchestrator/serving.py`'s `ServingFacade`) is exposed here as exactly one
MCP tool, and every tool here resolves to exactly one facade operation — no
tool reaches past the facade to touch a bank directory or a log file
directly. This module is a thin adapter: no serving/ingest logic lives
here, only session-registry bookkeeping (an MCP tool call is stateless;
`orchestrator/serving.py`'s `Session` object is not, so this module holds
the open ones between calls) and JSON-shaping of facade return values.

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

from orchestrator import engine, serving  # noqa: E402

INTERFACE_VERSION = "1.0.0-pre"

# KTD6's parity rule as data: every facade operation this server exposes.
# tests/test_mcp_server.py asserts this against both the facade's own
# public methods and this server's registered tool names.
FACADE_OPERATIONS = (
    "open_session",
    "get_candidates",
    "submit_attempt",
    "close_session",
    "close_session_by_student",
    "get_student_progress",
    "recommend_next",
)


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
    skill/reason/context/target_depth/unservable entries (R6/R7/KTD3)."""
    return {
        "student_id": result.student_id,
        "bank_version": result.bank_version,
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
            raise ToolError(f"session_unknown: {exc}") from exc
        except serving.SessionExpiredError as exc:
            raise ToolError(f"session_expired: {exc}") from exc

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
            raise ToolError(f"not_linked: {exc}") from exc
        except serving.SessionConflictError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "session_id": session_id,
            "student_id": student_id,
            "bank_version": session_obj.bank.bank_version,
        }

    @mcp.tool()
    def get_candidates(session_id: str, skill_id: str) -> dict[str, Any]:
        """R1, R2: every unseen instance in the pinned bank for
        (student, skill); on pool exhaustion, exactly the one
        least-recently-seen repeat, with exhausted=True and an explicit
        warning (§5.4)."""
        session_obj = _session_or_raise(session_id)
        result = facade.get_candidates(session_obj, skill_id)
        return {
            "bank_version": result.bank_version,
            "instances": result.instances,
            "exhausted": result.exhausted,
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
        resolves against this session's own pinned bank server-side (a
        caller can never smuggle in an instance from elsewhere);
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
        instance = session_obj.bank.by_id.get(item_instance_id)
        if instance is None:
            raise ToolError(f"unresolvable_instance: {item_instance_id!r} is not in this session's pinned bank")

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
            raise ToolError(f"not_linked: {exc}") from exc
        if outcome.rejection_reason is not None:
            raise ToolError(f"ingest rejected: {outcome.rejection_reason}")
        if outcome.event is None:
            return {"logged": False, "repeat": False, "discard_reason": outcome.discard_reason}
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
            raise ToolError(f"not_linked: {exc}") from exc
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
            raise ToolError(f"not_linked: {exc}") from exc
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
            raise ToolError(f"not_linked: {exc}") from exc
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
            raise ToolError(f"not_linked: {exc}") from exc
        return _serialize_recommendations(result)

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
