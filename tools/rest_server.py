#!/usr/bin/env python3
"""REST server (U12): the same `orchestrator/serving.py` facade
`tools/mcp_server.py` exposes over MCP, exposed over HTTP so a partner that
can't speak MCP (or needs a shared, multi-tenant server rather than one
stdio process per consumer) still gets every capability (R14's parity
rule).

**Same facade, same session registry (KTD12).** `build_app` takes an
already-constructed `serving.ServingFacade` — it never builds its own.
A session opened through this transport lives in `facade.sessions` (U5's
`SessionRegistry`), the exact object `tools/mcp_server.py`'s `build_server`
resolves `session_id`s against, so a session opened over REST is usable
over MCP (and vice versa) when both transports are built from the same
`facade` instance in the same process — a testable property, proven in
`tests/test_rest_server.py`, not a claim about two live network listeners
sharing state at runtime.

**Response shapes are the same schema, not a second one.** Every response
this module returns reuses `tools/mcp_server.py`'s `_serialize_*` builder
functions where one already exists (progress, recommendations,
registration, link, lookup, catalog, units, skills, skill graph, batch,
refill) rather than re-deriving the shape from scratch — R15's "the schema
files are the single source of truth" point would be undermined by a
second, hand-maintained Pydantic response-model layer that could drift
from `schemas/api/*.schema.json`. The handful of operations with no
existing MCP serializer (`open_session`, `get_candidates`,
`submit_attempt`, `close_session`, `close_session_by_student`) return the
identical dict literal shape `tools/mcp_server.py`'s own tool functions
build inline.

**Per-connection identity (KTD15/KTD16).** Unlike MCP (one stdio process
per consumer, so a single `--consumer-id` startup flag is enough), this is
one HTTP server fielding requests from many different partners. Every
request authenticates via `Authorization: Bearer <token>`, resolved
against a credential table (`{token: consumer_id}`) built once at startup
and closed over by a FastAPI dependency (`_resolve_consumer_id`) — mirrors
how `tools/mcp_server.py`'s `build_server` closes over its fixed
`consumer_id`, except this closure holds a table, not a single string. A
missing or unrecognized token is refused with 401 by the dependency itself,
before any route body — and therefore any facade code — runs.
`consumer_id` is never accepted as a request parameter on any route, same
"never caller-supplied" invariant every MCP tool already follows.

**Errors carry no filesystem paths, and the token is never echoed
anywhere** — not in a response body, not in the generated OpenAPI
document, not in a log line. This module adds no request-logging
middleware; if one is ever added, it must explicitly exclude the
`Authorization` header.

**Status mapping (KTD7).** `orchestrator/signals.py`'s ten closed signals
map to HTTP status via `_STATUS_BY_SIGNAL` below — the one definition site,
mirroring `signals.py`'s own "defined once" discipline. No route computes
its own status code from a signal string; every route either returns a
plain 200 body or raises `_signal_error`, which looks the status up here.

**v1 pre-stability**, same posture as `tools/mcp_server.py`:
`INTERFACE_VERSION` may still change before general availability.

Usage:
    python tools/rest_server.py --banks-dir banks --logs-dir logs \\
        --credentials-file credentials.json [--content content/fractions] \\
        [--host 127.0.0.1] [--port 8000]

`credentials.json` shape: {"token-abc": "partner.acme", "token-xyz": "partner.beta"}
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from fastapi import Depends, FastAPI, HTTPException, Query  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from orchestrator import batch as batch_module  # noqa: E402
from orchestrator import constants, engine, serving, signals  # noqa: E402
from orchestrator.identity import AttributeMismatchError, UnknownStudentError  # noqa: E402

import mcp_server  # noqa: E402  (reuse its _serialize_* builders — R15)

INTERFACE_VERSION = mcp_server.INTERFACE_VERSION

# KTD7's status table, as data — the one place a signal name resolves to an
# HTTP status. Every one of `signals.ALL` must be a key here (asserted by
# tests/test_rest_server.py against signals.ALL directly, mirroring that
# module's own "defined once" self-check).
_STATUS_BY_SIGNAL: Dict[str, int] = {
    signals.UNKNOWN_SKILL: 404,
    signals.EMPTY_POOL: 200,
    signals.EXHAUSTED: 200,
    signals.NO_FURTHER_ITEMS: 200,
    signals.SESSION_CONFLICT: 409,
    signals.SESSION_UNKNOWN: 404,
    signals.SESSION_EXPIRED: 410,
    signals.NOT_LINKED: 403,
    signals.INGEST_REJECTED: 422,
    signals.DISCARDED: 200,
}

# Domain exceptions the facade raises that carry one of the ten signals
# above, keyed to the signal they mean (mirrors how tools/mcp_server.py
# catches each one and embeds the signal name in a ToolError message).
_EXCEPTION_SIGNALS = {
    serving.SessionConflictError: signals.SESSION_CONFLICT,
    serving.SessionUnknownError: signals.SESSION_UNKNOWN,
    serving.SessionExpiredError: signals.SESSION_EXPIRED,
    serving.ConsumerNotLinkedError: signals.NOT_LINKED,
}

# Genuine domain errors that are *not* part of KTD7's closed ten-signal
# taxonomy (same posture tools/mcp_server.py's own docstrings call out for
# these exact exceptions) — surfaced as a plain 400 with a path-free
# message, never a signal field.
_GENERIC_400 = (
    serving.InvalidIdentifierError,
    serving.NoIdentityRegistryError,
    UnknownStudentError,
    AttributeMismatchError,
    batch_module.BatchPlanningError,
)


def _signal_error(signal: str, reason: str) -> NoReturn:
    """The one place a signal turns into an HTTPException — every route
    below calls this instead of hardcoding a status code inline."""
    raise HTTPException(status_code=_STATUS_BY_SIGNAL[signal], detail={"signal": signal, "reason": reason})


def _generic_error(exc: Exception) -> NoReturn:
    raise HTTPException(status_code=400, detail={"reason": str(exc)})


def _generic_error_no_path(exc: Exception) -> NoReturn:
    """`serving.NoActiveBankError`'s message would otherwise include a
    banks-directory path — replaced with the same generic, path-free
    message `tools/mcp_server.py`'s own tools use."""
    raise HTTPException(status_code=400, detail={"reason": "no active bank build is configured for this deployment"})


# --- Request bodies (JSON-shaped input only; responses stay plain dicts,
# built by mcp_server's _serialize_* functions or the literal shapes below,
# per this module's docstring) -------------------------------------------


class OpenSessionRequest(BaseModel):
    student_id: str
    country: str
    curriculum: str
    grade: int


class SubmitAttemptRequest(BaseModel):
    attempt_id: str
    skill_id: str
    item_instance_id: str
    response_raw: str
    response_parsed: Optional[str] = None
    correct: bool
    misconception_id: Optional[str] = None
    hints_used: int
    hint_auto_offered: bool
    time_taken_sec: float
    timed_out: bool
    engaged: bool
    session_last_event: bool
    context: str


class RegisterStudentRequest(BaseModel):
    attributes: Dict[str, Any]


class LinkStudentRequest(BaseModel):
    attributes: Dict[str, Any]
    link_basis: Optional[str] = None


class FindStudentRequest(BaseModel):
    attributes: Dict[str, Any]


def build_app(
    facade: serving.ServingFacade, credentials: Dict[str, str], name: str = "mathiqo-serving-rest"
) -> FastAPI:
    """Wires `mcp_server.FACADE_OPERATIONS` up as HTTP routes over
    `facade` — the REST sibling of `mcp_server.build_server`. `credentials`
    is the startup-configured `{token: consumer_id}` table (KTD15/KTD16);
    it is closed over by `_resolve_consumer_id` below and never appears in
    a route, a response body, or the generated OpenAPI document (the
    dependency only ever returns the *resolved* consumer_id, never the
    table or the token that unlocked it)."""
    app = FastAPI(
        title=name,
        version=INTERFACE_VERSION,
        description="Mathiqo serving-integrity contract, over REST (R14's MCP-parity sibling).",
    )

    _bearer_scheme = HTTPBearer(auto_error=False)

    def _resolve_consumer_id(
        creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    ) -> str:
        """KTD16: missing/malformed header or unrecognized token -> 401,
        raised here, before any route body (and therefore any facade code)
        runs. Resolved against `credentials`, the startup-configured table
        — never a caller-supplied consumer_id."""
        if creds is None or not creds.credentials:
            raise HTTPException(status_code=401, detail="missing bearer token")
        consumer_id = credentials.get(creds.credentials)
        if consumer_id is None:
            raise HTTPException(status_code=401, detail="unrecognized bearer token")
        return consumer_id

    def _session_or_raise(session_id: str) -> serving.Session:
        try:
            return facade.sessions.get(session_id)
        except serving.SessionUnknownError as exc:
            _signal_error(signals.SESSION_UNKNOWN, str(exc))
        except serving.SessionExpiredError as exc:
            _signal_error(signals.SESSION_EXPIRED, str(exc))

    # -- serving/ingest -------------------------------------------------

    @app.post(
        "/sessions", operation_id="open_session",
        summary="Open a session pinned to the active bank build and (country, curriculum, grade).",
    )
    def open_session(body: OpenSessionRequest, consumer_id: str = Depends(_resolve_consumer_id)) -> dict[str, Any]:
        session_id = f"sess-{uuid.uuid4().hex}"
        try:
            session_obj = facade.open_session(
                session_id, body.student_id, body.country, body.curriculum, body.grade, consumer_id
            )
        except serving.NoActiveBankError as exc:
            _generic_error_no_path(exc)
        except serving.InvalidIdentifierError as exc:
            _generic_error(exc)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        except serving.SessionConflictError as exc:
            _signal_error(signals.SESSION_CONFLICT, str(exc))
        return {
            "session_id": session_id,
            "student_id": body.student_id,
            "bank_version": session_obj.bank.bank_version,
        }

    @app.get(
        "/sessions/{session_id}/candidates", operation_id="get_candidates",
        summary="Every unseen instance in the pinned bank for (student, skill).",
    )
    def get_candidates(
        session_id: str, skill_id: str = Query(...), consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.get_candidates(session_obj, skill_id, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        body = {
            "bank_version": result.bank_version,
            "instances": result.instances,
            "exhausted": result.exhausted,
            "signal": result.signal,
            "warning": result.warning,
        }
        if result.signal == signals.UNKNOWN_SKILL:
            # KTD7's table: unknown_skill is the one pool/candidate signal
            # that is a 404, not a 200-with-signal-in-body.
            raise HTTPException(status_code=_STATUS_BY_SIGNAL[signals.UNKNOWN_SKILL], detail=body)
        return body

    @app.post(
        "/sessions/{session_id}/attempts", operation_id="submit_attempt",
        summary="Validate and log one attempt.",
    )
    def submit_attempt(
        session_id: str, body: SubmitAttemptRequest, consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        session_obj = _session_or_raise(session_id)
        # `_resolve_instance` (defined in mcp_server.py, reused here the same
        # way this module already reuses its `_serialize_*` builders): falls
        # back to whatever retained build item_instance_id was actually
        # served from when it isn't in this session's own pinned bank, so
        # R19 Path B's session-less stale-bank validation is reachable
        # through this transport too, not just tools/mcp_server.py.
        instance = mcp_server._resolve_instance(facade, session_obj, body.item_instance_id)
        if instance is None:
            _generic_error(
                ValueError(
                    f"unresolvable_instance: {body.item_instance_id!r} is not in this session's pinned bank "
                    "or any retained bank build"
                )
            )

        try:
            outcome = facade.submit_attempt(
                session_obj, body.attempt_id, body.skill_id, instance,
                response_raw=body.response_raw, response_parsed=body.response_parsed, correct=body.correct,
                misconception_id=body.misconception_id, hints_used=body.hints_used,
                hint_auto_offered=body.hint_auto_offered, time_taken_sec=body.time_taken_sec,
                timed_out=body.timed_out, engaged=body.engaged, session_last_event=body.session_last_event,
                context=body.context, consumer_id=consumer_id, now=datetime.now(),
            )
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        if outcome.rejection_reason is not None:
            _signal_error(signals.INGEST_REJECTED, outcome.rejection_reason)
        if outcome.event is None:
            return {
                "logged": False,
                "repeat": False,
                "signal": signals.DISCARDED,
                "discard_reason": outcome.discard_reason,
            }
        return {"logged": True, "repeat": outcome.repeat, "event_id": outcome.event["event_id"]}

    @app.delete(
        "/sessions/{session_id}", operation_id="close_session",
        summary="End a session, removing it from the shared registry.",
    )
    def close_session(session_id: str, consumer_id: str = Depends(_resolve_consumer_id)) -> dict[str, Any]:
        try:
            session_obj = facade.sessions.get(session_id)
        except (serving.SessionUnknownError, serving.SessionExpiredError):
            return {"closed": False}
        try:
            facade.close_session(session_obj, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        return {"closed": True}

    @app.delete(
        "/students/{student_id}/session", operation_id="close_session_by_student",
        summary="Close whatever session is currently live for student_id, keyed by student.",
    )
    def close_session_by_student(
        student_id: str, consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        try:
            closed = facade.close_session_by_student(student_id, consumer_id)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        return {"closed": closed}

    @app.get(
        "/students/{student_id}/progress", operation_id="get_student_progress",
        summary="The declared progress summary: theta, certification status, per-skill seen count.",
    )
    def get_student_progress(student_id: str, consumer_id: str = Depends(_resolve_consumer_id)) -> dict[str, Any]:
        try:
            summary = facade.get_progress(student_id, consumer_id, datetime.now())
        except serving.InvalidIdentifierError as exc:
            _generic_error(exc)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        return mcp_server._serialize_progress(summary)

    @app.get(
        "/students/{student_id}/recommendations", operation_id="recommend_next",
        summary="A ranked, reasoned list of ready-to-learn skills.",
    )
    def recommend_next(
        student_id: str,
        country: str = Query(...),
        curriculum: str = Query(...),
        grade: int = Query(...),
        scope: Optional[str] = Query(None),
        consumer_id: str = Depends(_resolve_consumer_id),
    ) -> dict[str, Any]:
        try:
            result = facade.recommend_next(
                student_id, country, curriculum, grade, consumer_id, datetime.now(), scope=scope,
            )
        except serving.InvalidIdentifierError as exc:
            _generic_error(exc)
        except serving.NoActiveBankError as exc:
            _generic_error_no_path(exc)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        return mcp_server._serialize_recommendations(result)

    # -- identity ---------------------------------------------------------

    @app.post(
        "/students", operation_id="register_student",
        summary="Mint a new opaque student_id for the given attributes and auto-link the caller.",
    )
    def register_student(
        body: RegisterStudentRequest, consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        try:
            student_id = facade.register_student(body.attributes, consumer_id)
        except serving.NoIdentityRegistryError as exc:
            _generic_error(exc)
        return mcp_server._serialize_registration(student_id)

    @app.post(
        "/students/{student_id}/link", operation_id="link_student",
        summary="Link the caller's consumer_id to student_id (exact-match attributes required).",
    )
    def link_student(
        student_id: str, body: LinkStudentRequest, consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        try:
            facade.link_student(student_id, body.attributes, consumer_id, body.link_basis)
        except serving.NoIdentityRegistryError as exc:
            _generic_error(exc)
        except UnknownStudentError as exc:
            _generic_error(exc)
        except AttributeMismatchError as exc:
            _generic_error(exc)
        return mcp_server._serialize_link(student_id, consumer_id, body.link_basis)

    @app.post(
        "/students/lookup", operation_id="find_student",
        summary="Exact-match lookup: the opaque student_id for the given attributes, if registered.",
    )
    def find_student(
        body: FindStudentRequest, consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        try:
            student_id = facade.find_student(body.attributes)
        except serving.NoIdentityRegistryError as exc:
            _generic_error(exc)
        return mcp_server._serialize_lookup(student_id)

    # -- catalog (session-less, consumer_id still resolved/authenticated,
    # but not passed to the facade — KD3's discovery layer stays open
    # regardless of linking, same as over MCP) ----------------------------

    @app.get(
        "/catalog/curricula", operation_id="list_curricula",
        summary="Every (country, curriculum, grade) triple Mathiqo covers.",
    )
    def list_curricula(consumer_id: str = Depends(_resolve_consumer_id)) -> dict[str, Any]:
        return mcp_server._serialize_catalog(facade.list_curricula())

    @app.get(
        "/catalog/units", operation_id="list_units",
        summary="The chapter/unit list for one (country, curriculum, grade) scope.",
    )
    def list_units(
        country: str = Query(...), curriculum: str = Query(...), grade: int = Query(...),
        consumer_id: str = Depends(_resolve_consumer_id),
    ) -> dict[str, Any]:
        return mcp_server._serialize_units(facade.list_units(country, curriculum, grade))

    @app.get(
        "/catalog/skills", operation_id="list_skills",
        summary="The mapped skills for a curriculum scope, each with its curriculum-local presentation and servability against the ACTIVE bank.",
    )
    def list_skills(
        country: str = Query(...), curriculum: str = Query(...), grade: int = Query(...),
        unit_code: Optional[str] = Query(None), consumer_id: str = Depends(_resolve_consumer_id),
    ) -> dict[str, Any]:
        return mcp_server._serialize_skills(facade.list_skills(country, curriculum, grade, unit_code))

    @app.get(
        "/catalog/skill-graph", operation_id="get_skill_graph",
        summary="The canonical skill graph: prerequisites, depth axis, strand, misconception ids.",
    )
    def get_skill_graph(
        skill_ids: Optional[list[str]] = Query(None), consumer_id: str = Depends(_resolve_consumer_id)
    ) -> dict[str, Any]:
        return mcp_server._serialize_skill_graph(facade.get_skill_graph(skill_ids))

    # -- batch --------------------------------------------------------------

    @app.post(
        "/sessions/{session_id}/batches", operation_id="fetch_batch",
        summary="A server-planned, difficulty-laddered working set for one (student, skill).",
    )
    def fetch_batch(
        session_id: str,
        skill_id: str = Query(...),
        batch_size: int = Query(constants.DEFAULT_BATCH_SIZE),
        consumer_id: str = Depends(_resolve_consumer_id),
    ) -> dict[str, Any]:
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.fetch_batch(session_obj, skill_id, consumer_id, batch_size)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        except batch_module.BatchPlanningError as exc:
            _generic_error(exc)
        return mcp_server._serialize_batch_result(result)

    @app.post(
        "/sessions/{session_id}/batches/refill", operation_id="refill_batch",
        summary="Re-plan a batch for skill_id against this session's current live state.",
    )
    def refill_batch(
        session_id: str,
        skill_id: str = Query(...),
        attempts_flushed: bool = Query(...),
        consumer_id: str = Depends(_resolve_consumer_id),
    ) -> dict[str, Any]:
        session_obj = _session_or_raise(session_id)
        try:
            result = facade.refill_batch(session_obj, skill_id, consumer_id, attempts_flushed)
        except serving.ConsumerNotLinkedError as exc:
            _signal_error(signals.NOT_LINKED, str(exc))
        except batch_module.BatchPlanningError as exc:
            _generic_error(exc)
        return mcp_server._serialize_refill_result(result)

    return app


def _load_credentials(path: Path) -> Dict[str, str]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"{path} must contain a flat JSON object of {{token: consumer_id}} strings")
    return data


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
        "--credentials-file", required=True, type=Path,
        help="JSON file of {token: consumer_id} (KTD15/KTD16) — resolved per-request, never taken from a request body",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default 8000)")
    args = parser.parse_args(argv)

    if not args.banks_dir.is_dir():
        print(f"error: banks directory {args.banks_dir} does not exist", file=sys.stderr)
        return 2
    if not args.content.is_dir():
        print(f"error: content directory {args.content} does not exist", file=sys.stderr)
        return 2
    if not args.credentials_file.is_file():
        print(f"error: credentials file {args.credentials_file} does not exist", file=sys.stderr)
        return 2

    credentials = _load_credentials(args.credentials_file)
    content = engine.load_content(args.content)
    facade = serving.ServingFacade(args.banks_dir, args.logs_dir, content)
    app = build_app(facade, credentials)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
