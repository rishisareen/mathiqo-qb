"""Tests for tools/rest_server.py — the U12 REST transport: KTD7's status
mapping, KTD15/KTD16's bearer-token auth (never reaching the facade on a
bad/missing token), KTD12's cross-transport session sharing with
tools/mcp_server.py, R14's route-vs-facade-operation parity, and the same
PII/token-leak canaries the MCP unit proved for its own surface.

Guarded: the whole file is skipped (not failed) when `fastapi` isn't
installed, per the same "suite still green without it" contract
tests/test_mcp_server.py already uses for `mcp`.

Mostly transport plumbing per this unit's own execution note: prefer smoke
coverage over re-testing facade logic (fetch_batch's ladder composition,
submit_attempt's idempotency, etc.) prior units already covered thoroughly.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import bank_build  # noqa: E402
from conftest import REPO_ROOT  # noqa: E402
from orchestrator import engine, serving  # noqa: E402
from orchestrator.identity import IdentityRegistry  # noqa: E402

rest_server = pytest.importorskip("rest_server")

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"
EMPTY_POOL_SKILL_ID = "FRAC.EQUIV"

DEFAULT_OPEN_EXTRA = {"country": "IN", "curriculum": "NCERT", "grade": 6}

TOKEN_OWNER = "token-owner-4f8c9a1e"
TOKEN_STRANGER = "token-stranger-2b7d6f0a"
TOKEN_HELPER = "token-helper-9e1a3c5b"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _open_body(student_id: str, **overrides) -> dict:
    body = {"student_id": student_id, **DEFAULT_OPEN_EXTRA}
    body.update(overrides)
    return body


def _submit_body(instance: dict, **overrides) -> dict:
    body = dict(
        attempt_id=f"attempt-{instance['item_instance_id']}",
        skill_id=SKILL_ID, item_instance_id=instance["item_instance_id"],
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
    )
    body.update(overrides)
    return body


def _templates_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    return dest


@pytest.fixture()
def content() -> engine.ContentIndex:
    return engine.load_content(REPO_ROOT / "content" / "fractions")


def _build_facade(tmp_path: Path, content: engine.ContentIndex, **facade_kwargs) -> serving.ServingFacade:
    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    manifest, per_template = bank_build.run_build(_templates_dir(tmp_path), LOCALE_PACK, "rest-build", pool_cap=5)
    bank_build.write_bank(banks_dir, "rest-build", manifest, per_template)
    (banks_dir / serving.ACTIVE_POINTER_NAME).write_text("rest-build", encoding="utf-8")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return serving.ServingFacade(banks_dir, logs_dir, content, **facade_kwargs)


@pytest.fixture()
def facade(tmp_path: Path, content: engine.ContentIndex) -> serving.ServingFacade:
    return _build_facade(tmp_path, content)


@pytest.fixture()
def identity_registry(tmp_path: Path) -> IdentityRegistry:
    return IdentityRegistry(tmp_path / "identity" / "registry.jsonl")


@pytest.fixture()
def linked_facade(
    tmp_path: Path, content: engine.ContentIndex, identity_registry: IdentityRegistry
) -> serving.ServingFacade:
    return _build_facade(tmp_path, content, identity=identity_registry)


CREDENTIALS = {TOKEN_OWNER: "partner.owner", TOKEN_STRANGER: "partner.stranger", TOKEN_HELPER: "partner.helper"}


def _client(facade: serving.ServingFacade, credentials: dict = CREDENTIALS) -> TestClient:
    app = rest_server.build_app(facade, credentials)
    return TestClient(app)


# --- R14 parity: every facade operation is a route and vice versa ----------


def test_route_operation_ids_match_facade_operations(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    openapi = client.get("/openapi.json").json()
    operation_ids = {
        op["operationId"]
        for path_item in openapi["paths"].values()
        for op in path_item.values()
        if "operationId" in op
    }
    assert operation_ids == set(rest_server.mcp_server.FACADE_OPERATIONS)


# --- smoke: open -> get candidates -> submit -> progress reflects it -------


def test_smoke_open_get_candidates_submit_progress(facade: serving.ServingFacade) -> None:
    client = _client(facade)

    opened = client.post("/sessions", json=_open_body("stu-smoke"), headers=_auth(TOKEN_OWNER))
    assert opened.status_code == 200, opened.text
    session_id = opened.json()["session_id"]
    assert opened.json()["bank_version"] == "rest-build"

    candidates = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert candidates.status_code == 200
    instance = candidates.json()["instances"][0]

    submitted = client.post(
        f"/sessions/{session_id}/attempts", json=_submit_body(instance), headers=_auth(TOKEN_OWNER)
    )
    assert submitted.status_code == 200
    assert submitted.json()["logged"] is True
    assert submitted.json()["repeat"] is False

    progress = client.get("/students/stu-smoke/progress", headers=_auth(TOKEN_OWNER))
    assert progress.status_code == 200
    skill_progress = progress.json()["skills"][SKILL_ID]
    assert skill_progress["seen_count"] == 1

    closed = client.delete(f"/sessions/{session_id}", headers=_auth(TOKEN_OWNER))
    assert closed.status_code == 200
    assert closed.json()["closed"] is True


# --- KTD12: a session opened over one transport is usable over the other ---


def test_session_opened_over_rest_is_usable_over_mcp(facade: serving.ServingFacade) -> None:
    mcp = pytest.importorskip("mcp")
    del mcp
    import mcp_server
    from mcp.shared.memory import create_connected_server_and_client_session

    client = _client(facade, {TOKEN_OWNER: "shared.consumer"})
    opened = client.post("/sessions", json=_open_body("stu-shared"), headers=_auth(TOKEN_OWNER))
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]

    mcp_srv = mcp_server.build_server(facade, "shared.consumer")

    async def flow():
        async with create_connected_server_and_client_session(mcp_srv) as mcp_client:
            return await mcp_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": SKILL_ID}
            )

    result = asyncio.run(flow())
    assert not result.isError
    assert result.structuredContent["instances"]


def test_session_opened_over_mcp_is_usable_over_rest(facade: serving.ServingFacade) -> None:
    pytest.importorskip("mcp")
    import mcp_server
    from mcp.shared.memory import create_connected_server_and_client_session

    mcp_srv = mcp_server.build_server(facade, "shared.consumer")

    async def flow():
        async with create_connected_server_and_client_session(mcp_srv) as mcp_client:
            opened = await mcp_client.call_tool("open_session", _open_body("stu-shared-2"))
            return opened.structuredContent["session_id"]

    session_id = asyncio.run(flow())

    client = _client(facade, {TOKEN_OWNER: "shared.consumer"})
    candidates = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert candidates.status_code == 200
    assert candidates.json()["instances"]


# --- KTD15/KTD16: bearer-token auth, refused before touching the facade ----


def test_missing_authorization_header_is_refused_with_401_and_no_facade_side_effects(
    facade: serving.ServingFacade,
) -> None:
    client = _client(facade)
    resp = client.post("/sessions", json=_open_body("stu-noauth"))
    assert resp.status_code == 401
    assert facade.sessions.find_by_student("stu-noauth") is None
    assert not list(facade.logs_dir.glob("*.jsonl"))


def test_unrecognized_token_is_refused_with_401_and_no_facade_side_effects(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    resp = client.post("/sessions", json=_open_body("stu-badtoken"), headers=_auth("not-a-real-token"))
    assert resp.status_code == 401
    assert facade.sessions.find_by_student("stu-badtoken") is None
    assert not list(facade.logs_dir.glob("*.jsonl"))


def test_valid_token_is_accepted(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    resp = client.post("/sessions", json=_open_body("stu-goodtoken"), headers=_auth(TOKEN_OWNER))
    assert resp.status_code == 200


# --- KTD7: signal -> status mapping -----------------------------------------


def test_unknown_skill_is_404(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-unknown-skill"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]

    resp = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": "NOPE.NOT.A.SKILL"}, headers=_auth(TOKEN_OWNER)
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["signal"] == "unknown_skill"


def test_empty_pool_and_exhausted_are_200_with_signal_in_body(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-empty-exhausted"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]

    empty = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": EMPTY_POOL_SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert empty.status_code == 200
    assert empty.json()["signal"] == "empty_pool"

    candidates = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    for instance in candidates.json()["instances"]:
        client.post(f"/sessions/{session_id}/attempts", json=_submit_body(instance), headers=_auth(TOKEN_OWNER))

    exhausted = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert exhausted.status_code == 200
    assert exhausted.json()["signal"] == "exhausted"


def test_no_further_items_is_200_with_signal_in_body(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-refill"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]

    client.post(
        f"/sessions/{session_id}/batches", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    refilled = client.post(
        f"/sessions/{session_id}/batches/refill",
        params={"skill_id": SKILL_ID, "attempts_flushed": True},
        headers=_auth(TOKEN_OWNER),
    )
    assert refilled.status_code == 200
    body = refilled.json()
    assert body["batch"] is None
    assert body["signal"] == "no_further_items"


def test_discarded_is_200_with_signal_and_reason_in_body(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-discard"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]
    candidates = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    instance = candidates.json()["instances"][0]

    resp = client.post(
        f"/sessions/{session_id}/attempts",
        json=_submit_body(instance, timed_out=True, engaged=False, session_last_event=True),
        headers=_auth(TOKEN_OWNER),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["logged"] is False
    assert body["signal"] == "discarded"
    assert isinstance(body["discard_reason"], str) and body["discard_reason"]


def test_ingest_rejected_is_422_with_reason_in_body(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-reject"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]
    candidates = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    instance = candidates.json()["instances"][0]

    resp = client.post(
        f"/sessions/{session_id}/attempts",
        json=_submit_body(instance, context="not_a_real_context"),
        headers=_auth(TOKEN_OWNER),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["signal"] == "ingest_rejected"
    assert isinstance(detail["reason"], str) and detail["reason"]


def test_session_unknown_is_404(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    resp = client.get(
        "/sessions/sess-never-issued/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["signal"] == "session_unknown"


def test_session_expired_is_410(tmp_path: Path, content: engine.ContentIndex) -> None:
    import time

    facade = _build_facade(tmp_path, content, session_ttl_seconds=0.05)
    client = _client(facade)
    opened = client.post("/sessions", json=_open_body("stu-ttl"), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]
    time.sleep(0.15)
    resp = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    assert resp.status_code == 410
    assert resp.json()["detail"]["signal"] == "session_expired"


def test_session_conflict_is_409(facade: serving.ServingFacade) -> None:
    client = _client(facade, {TOKEN_OWNER: "partner.a", TOKEN_STRANGER: "partner.b"})
    first = client.post("/sessions", json=_open_body("stu-conflict"), headers=_auth(TOKEN_OWNER))
    assert first.status_code == 200

    second = client.post("/sessions", json=_open_body("stu-conflict"), headers=_auth(TOKEN_STRANGER))
    assert second.status_code == 409
    assert second.json()["detail"]["signal"] == "session_conflict"


def test_not_linked_is_403(linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry) -> None:
    student_id = identity_registry.register_student({"guardian_email": "owner@example.com"}, consumer_id="partner.owner")
    client = _client(linked_facade, {TOKEN_OWNER: "partner.owner", TOKEN_STRANGER: "partner.stranger"})

    resp = client.get(f"/students/{student_id}/progress", headers=_auth(TOKEN_STRANGER))
    assert resp.status_code == 403
    assert resp.json()["detail"]["signal"] == "not_linked"

    ok = client.get(f"/students/{student_id}/progress", headers=_auth(TOKEN_OWNER))
    assert ok.status_code == 200


def test_get_candidates_not_linked_is_403(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """Closed simplify-review finding: get_candidates was the one
    student-scoped route with no link check at any layer — a stranger
    holding a session_id could read another partner's candidate pool."""
    student_id = identity_registry.register_student({"guardian_email": "cand@example.com"}, consumer_id="partner.owner")
    client = _client(linked_facade, {TOKEN_OWNER: "partner.owner", TOKEN_STRANGER: "partner.stranger"})
    opened = client.post("/sessions", json=_open_body(student_id), headers=_auth(TOKEN_OWNER))
    session_id = opened.json()["session_id"]

    resp = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": "FRAC.ADD.LIKE"}, headers=_auth(TOKEN_STRANGER)
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["signal"] == "not_linked"

    ok = client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": "FRAC.ADD.LIKE"}, headers=_auth(TOKEN_OWNER)
    )
    assert ok.status_code == 200


# --- token / OpenAPI leak checks --------------------------------------------


def test_configured_tokens_never_appear_in_openapi_document(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    openapi_text = json.dumps(client.get("/openapi.json").json())
    assert TOKEN_OWNER not in openapi_text
    assert TOKEN_STRANGER not in openapi_text
    assert TOKEN_HELPER not in openapi_text


def test_configured_tokens_never_appear_in_response_bodies(facade: serving.ServingFacade) -> None:
    client = _client(facade)
    responses: list[Any] = []
    responses.append(client.post("/sessions", json=_open_body("stu-tokencheck"), headers=_auth(TOKEN_OWNER)))
    session_id = responses[-1].json()["session_id"]
    responses.append(
        client.get(
            f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
        )
    )
    responses.append(client.post("/sessions", json=_open_body("stu-tokencheck2"), headers=_auth("wrong-token")))
    responses.append(client.get("/students/stu-tokencheck/progress", headers=_auth(TOKEN_OWNER)))

    for resp in responses:
        dumped = json.dumps(resp.json())
        assert TOKEN_OWNER not in dumped
        assert TOKEN_STRANGER not in dumped
        assert TOKEN_HELPER not in dumped
        assert "wrong-token" not in dumped


# --- PII canary: a sentinel lookup attribute never leaks anywhere ----------


def _assert_no_sentinel(value: Any, sentinel: str) -> None:
    dumped = json.dumps(value, default=str)
    assert sentinel not in dumped


def test_pii_canary_sentinel_never_leaks_across_the_rest_surface(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """Mirrors the U11 MCP canary: register a student with a sentinel
    guardian_email, exercise every error-taxonomy signal this REST surface
    can produce plus a batch/progress/recommendation read, and assert the
    sentinel never appears in any response body or in the OpenAPI
    document."""
    SENTINEL = "canary-rest-guardian-7c4d2a9e1f@example.com"

    identity_registry = IdentityRegistry(tmp_path / "identity" / "registry.jsonl")
    main_root = tmp_path / "main"
    main_root.mkdir()
    ttl_root = tmp_path / "ttl"
    ttl_root.mkdir()
    main_facade = _build_facade(main_root, content, identity=identity_registry)
    ttl_facade = _build_facade(ttl_root, content, identity=identity_registry, session_ttl_seconds=0.05)

    credentials = {TOKEN_OWNER: "partner.owner", TOKEN_STRANGER: "partner.stranger", TOKEN_HELPER: "partner.helper"}
    main_client = _client(main_facade, credentials)
    ttl_client = _client(ttl_facade, credentials)

    seen_payloads: list = []

    def _record(resp) -> None:
        try:
            seen_payloads.append(resp.json())
        except ValueError:
            seen_payloads.append(resp.text)

    registered = main_client.post(
        "/students", json={"attributes": {"guardian_email": SENTINEL}}, headers=_auth(TOKEN_OWNER)
    )
    _record(registered)
    assert registered.status_code == 200
    student_id = registered.json()["student_id"]

    _record(main_client.post(
        "/students/lookup", json={"attributes": {"guardian_email": SENTINEL}}, headers=_auth(TOKEN_OWNER)
    ))

    opened = main_client.post("/sessions", json=_open_body(student_id), headers=_auth(TOKEN_OWNER))
    _record(opened)
    session_id = opened.json()["session_id"]

    # unknown_skill
    _record(main_client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": "NOPE.NOT.A.SKILL"}, headers=_auth(TOKEN_OWNER)
    ))
    # empty_pool
    _record(main_client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": EMPTY_POOL_SKILL_ID}, headers=_auth(TOKEN_OWNER)
    ))

    candidates = main_client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    _record(candidates)
    for instance in candidates.json()["instances"]:
        submitted = main_client.post(
            f"/sessions/{session_id}/attempts", json=_submit_body(instance), headers=_auth(TOKEN_OWNER)
        )
        _record(submitted)

    exhausted = main_client.get(
        f"/sessions/{session_id}/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    )
    _record(exhausted)
    assert exhausted.json()["signal"] == "exhausted"
    repeat_instance = exhausted.json()["instances"][0]

    # discarded
    discarded = main_client.post(
        f"/sessions/{session_id}/attempts",
        json=_submit_body(
            repeat_instance, attempt_id="canary-discard", timed_out=True, engaged=False, session_last_event=True,
        ),
        headers=_auth(TOKEN_OWNER),
    )
    _record(discarded)
    assert discarded.json()["signal"] == "discarded"

    # ingest_rejected
    rejected = main_client.post(
        f"/sessions/{session_id}/attempts",
        json=_submit_body(repeat_instance, attempt_id="canary-reject", context="not_a_real_context"),
        headers=_auth(TOKEN_OWNER),
    )
    _record(rejected)
    assert rejected.status_code == 422

    # fetch_batch against exhausted pool
    _record(main_client.post(
        f"/sessions/{session_id}/batches", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    ))

    # no_further_items
    refilled = main_client.post(
        f"/sessions/{session_id}/batches/refill",
        params={"skill_id": SKILL_ID, "attempts_flushed": True},
        headers=_auth(TOKEN_OWNER),
    )
    _record(refilled)
    assert refilled.json()["signal"] == "no_further_items"

    # progress / recommendation reads
    _record(main_client.get(f"/students/{student_id}/progress", headers=_auth(TOKEN_OWNER)))
    _record(main_client.get(
        f"/students/{student_id}/recommendations", params=DEFAULT_OPEN_EXTRA, headers=_auth(TOKEN_OWNER)
    ))

    # session_unknown
    _record(main_client.get(
        "/sessions/sess-never-issued-canary/candidates", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER)
    ))

    # not_linked
    not_linked_progress = main_client.get(f"/students/{student_id}/progress", headers=_auth(TOKEN_STRANGER))
    _record(not_linked_progress)
    assert not_linked_progress.status_code == 403

    not_linked_batch = main_client.post(
        f"/sessions/{session_id}/batches", params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_STRANGER)
    )
    _record(not_linked_batch)

    mismatched_link = main_client.post(
        f"/students/{student_id}/link",
        json={"attributes": {"guardian_email": "wrong@example.com"}},
        headers=_auth(TOKEN_STRANGER),
    )
    _record(mismatched_link)

    # session_conflict
    identity_registry.link_student(student_id, {"guardian_email": SENTINEL}, consumer_id="partner.helper")
    conflict = main_client.post("/sessions", json=_open_body(student_id), headers=_auth(TOKEN_HELPER))
    _record(conflict)
    assert conflict.status_code == 409

    # session_expired
    ttl_opened = ttl_client.post("/sessions", json=_open_body(student_id), headers=_auth(TOKEN_OWNER))
    _record(ttl_opened)
    import time

    time.sleep(0.15)
    expired = ttl_client.get(
        f"/sessions/{ttl_opened.json()['session_id']}/candidates",
        params={"skill_id": SKILL_ID}, headers=_auth(TOKEN_OWNER),
    )
    _record(expired)
    assert expired.status_code == 410

    for payload in seen_payloads:
        _assert_no_sentinel(payload, SENTINEL)

    openapi_doc = main_client.get("/openapi.json").json()
    _assert_no_sentinel(openapi_doc, SENTINEL)
