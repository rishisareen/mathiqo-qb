"""Tests for tools/mcp_server.py — the U8 MCP server: KTD6's parity rule
(every facade operation exposed as exactly one tool, every tool resolves
to exactly one facade operation), the end-to-end smoke flow, consumer_id
attribution from server config (never the payload), the declared progress
summary shape, and the declared interface version.

Guarded: the whole file is skipped (not failed) when the `mcp` SDK isn't
installed, per the plan's "suite still green without it" contract.
Exercises an in-process MCP client against the real server (mcp's own
`create_connected_server_and_client_session` memory-transport harness)
rather than exhaustive per-tool unit tests, per this unit's own execution
note: mostly integration/packaging, prefer the smoke flow.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import bank_build
from conftest import REPO_ROOT
from orchestrator import engine, log as log_module, serving
from orchestrator.identity import IdentityRegistry

mcp_server = pytest.importorskip("mcp_server")
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"

# U5: open_session now requires country/curriculum/grade (KTD3's pin).
DEFAULT_OPEN_EXTRA = {"country": "IN", "curriculum": "NCERT", "grade": 6}


def _open_args(student_id: str, **overrides) -> dict:
    args = {"student_id": student_id, **DEFAULT_OPEN_EXTRA}
    args.update(overrides)
    return args


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
    manifest, per_template = bank_build.run_build(_templates_dir(tmp_path), LOCALE_PACK, "mcp-build", pool_cap=5)
    bank_build.write_bank(banks_dir, "mcp-build", manifest, per_template)
    (banks_dir / serving.ACTIVE_POINTER_NAME).write_text("mcp-build", encoding="utf-8")
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


def _run(coro):
    return asyncio.run(coro)


def _submit_args(session_id: str, instance: dict, **overrides) -> dict:
    args = dict(
        session_id=session_id, attempt_id=f"attempt-{instance['item_instance_id']}",
        skill_id=SKILL_ID, item_instance_id=instance["item_instance_id"],
        response_raw=instance["answer"], response_parsed=instance["answer"], correct=True,
        misconception_id=None, hints_used=0, hint_auto_offered=False, time_taken_sec=5.0,
        timed_out=False, engaged=True, session_last_event=False, context="practice",
    )
    args.update(overrides)
    return args


# --- KTD6 parity rule, both directions --------------------------------------


def test_parity_every_facade_operation_is_a_tool_and_vice_versa(facade: serving.ServingFacade) -> None:
    facade_methods = {name for name in dir(serving.ServingFacade) if not name.startswith("_")}
    # get_student_progress is this surface's tool name for
    # ServingFacade.get_progress (the plan names the tool "get_student
    #_progress" explicitly; the facade method predates that naming) —
    # every other tool name matches its facade method 1:1.
    expected_facade_methods = {
        "get_progress" if op == "get_student_progress" else op for op in mcp_server.FACADE_OPERATIONS
    }
    assert expected_facade_methods <= facade_methods, "a declared facade operation isn't a real ServingFacade method"

    server = mcp_server.build_server(facade, "test.consumer")

    async def list_tool_names() -> set:
        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()
            return {t.name for t in result.tools}

    tool_names = _run(list_tool_names())
    # server_info is the pre-stability version declaration, not a
    # serving/ingest operation KTD6's parity rule is about.
    assert tool_names - {"server_info"} == set(mcp_server.FACADE_OPERATIONS)


# --- Smoke: open -> get candidates -> submit correct -> progress reflects it


def test_smoke_open_get_candidates_submit_progress(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-smoke"))
            assert not opened.isError
            session_id = opened.structuredContent["session_id"]
            assert opened.structuredContent["bank_version"] == "mcp-build"

            candidates = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            assert not candidates.isError
            instances = candidates.structuredContent["instances"]
            assert instances
            instance = instances[0]

            submitted = await client.call_tool("submit_attempt", _submit_args(session_id, instance))
            assert not submitted.isError
            assert submitted.structuredContent["logged"] is True
            assert submitted.structuredContent["repeat"] is False

            progress = await client.call_tool("get_student_progress", {"session_id": session_id})
            assert not progress.isError
            skill_progress = progress.structuredContent["skills"][SKILL_ID]
            assert skill_progress["seen_count"] == 1
            assert skill_progress["theta"] > 0.0

            closed = await client.call_tool("close_session", {"session_id": session_id})
            assert closed.structuredContent["closed"] is True

    _run(flow())


# --- consumer_id: server config, never the payload --------------------------


def test_forged_consumer_id_in_payload_is_stamped_with_the_servers_own(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "server.configured.id")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-forge"))
            session_id = opened.structuredContent["session_id"]
            candidates = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            instance = candidates.structuredContent["instances"][0]

            # submit_attempt has no consumer_id parameter in its own
            # signature — a forged one in the call payload has nowhere
            # to bind and is silently dropped by MCP's argument binding,
            # not merely "overridden": confirm via the logged event.
            args = _submit_args(session_id, instance)
            args["consumer_id"] = "evil.forged.id"
            result = await client.call_tool("submit_attempt", args)
            assert not result.isError

    _run(flow())

    log_path = next(facade.logs_dir.glob("*.jsonl"))
    events = list(log_module.iter_events(log_path))
    assert len(events) == 1
    assert events[0]["consumer_id"] == "server.configured.id"


# --- get_student_progress: declared shape ------------------------------------


def test_get_student_progress_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow() -> dict:
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-shape"))
            session_id = opened.structuredContent["session_id"]
            candidates = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            instance = candidates.structuredContent["instances"][0]
            await client.call_tool("submit_attempt", _submit_args(session_id, instance))
            progress = await client.call_tool("get_student_progress", {"session_id": session_id})
            return progress.structuredContent

    result = _run(flow())
    assert set(result) == {"student_id", "skills"}
    skill = result["skills"][SKILL_ID]
    assert set(skill) == {"skill_id", "theta", "n", "certified_depth", "certification_status", "seen_count"}


# --- interface version --------------------------------------------------------


def test_server_reports_declared_interface_version(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow() -> dict:
        async with create_connected_server_and_client_session(server) as client:
            info = await client.call_tool("server_info", {})
            return info.structuredContent

    result = _run(flow())
    assert result["interface_version"] == mcp_server.INTERFACE_VERSION
    assert set(result["facade_operations"]) == set(mcp_server.FACADE_OPERATIONS)


# --- unresolvable session / instance -> structured tool error, no paths -----


def test_unknown_session_id_raises_structured_tool_error(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("get_candidates", {"session_id": "sess-does-not-exist", "skill_id": SKILL_ID})

    result = _run(flow())
    assert result.isError
    text = result.content[0].text
    assert "sess-does-not-exist" in text
    assert str(facade.banks_dir) not in text
    assert str(facade.logs_dir) not in text


def test_no_active_bank_raises_generic_error_without_the_banks_dir_path(tmp_path: Path, content: engine.ContentIndex) -> None:
    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    facade = serving.ServingFacade(banks_dir, logs_dir, content)  # no ACTIVE pointer written
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("open_session", _open_args("stu-noactive"))

    result = _run(flow())
    assert result.isError
    text = result.content[0].text
    assert str(banks_dir) not in text


# --- U5: link enforcement over MCP (R17, AE6) --------------------------------


def test_open_session_refused_for_unlinked_consumer_over_mcp(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    student_id = identity_registry.register_student({"guardian_email": "a@example.com"}, consumer_id="partner.owner")
    server = mcp_server.build_server(linked_facade, "partner.stranger")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("open_session", _open_args(student_id))

    result = _run(flow())
    assert result.isError
    assert "not_linked" in result.content[0].text

    identity_registry.link_student(student_id, {"guardian_email": "a@example.com"}, consumer_id="partner.stranger")

    async def flow2():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("open_session", _open_args(student_id))

    result2 = _run(flow2())
    assert not result2.isError


# --- U5: session conflict + takeover-on-open over MCP (R20, AE9) ------------


def test_cross_consumer_open_over_mcp_is_refused_with_conflict_signal(facade: serving.ServingFacade) -> None:
    server_a = mcp_server.build_server(facade, "partner.a")
    server_b = mcp_server.build_server(facade, "partner.b")

    async def flow():
        async with create_connected_server_and_client_session(server_a) as client_a:
            opened = await client_a.call_tool("open_session", _open_args("stu-conflict"))
            session_id_a = opened.structuredContent["session_id"]

            async with create_connected_server_and_client_session(server_b) as client_b:
                conflict = await client_b.call_tool("open_session", _open_args("stu-conflict"))
                assert conflict.isError
                assert "session_conflict" in conflict.content[0].text
                assert "close_session_by_student" in conflict.content[0].text

            # The first consumer's session is unaffected.
            candidates = await client_a.call_tool(
                "get_candidates", {"session_id": session_id_a, "skill_id": SKILL_ID}
            )
            assert not candidates.isError

    _run(flow())


def test_same_consumer_reopen_over_mcp_takes_over(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "partner.a")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            first = await client.call_tool("open_session", _open_args("stu-takeover"))
            first_session_id = first.structuredContent["session_id"]

            second = await client.call_tool("open_session", _open_args("stu-takeover"))
            assert not second.isError
            second_session_id = second.structuredContent["session_id"]
            assert second_session_id != first_session_id

            # The prior session is gone — closed by takeover.
            stale = await client.call_tool(
                "get_candidates", {"session_id": first_session_id, "skill_id": SKILL_ID}
            )
            assert stale.isError

    _run(flow())


# --- U5: close_session_by_student recovery path (KTD6 point 5) --------------


def test_close_session_by_student_over_mcp_recovers_an_orphaned_session(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    student_id = identity_registry.register_student({"guardian_email": "f@example.com"}, consumer_id="partner.owner")
    identity_registry.link_student(student_id, {"guardian_email": "f@example.com"}, consumer_id="partner.helper")

    server_owner = mcp_server.build_server(linked_facade, "partner.owner")
    server_helper = mcp_server.build_server(linked_facade, "partner.helper")

    async def flow():
        async with create_connected_server_and_client_session(server_owner) as owner_client:
            opened = await owner_client.call_tool("open_session", _open_args(student_id))
            session_id = opened.structuredContent["session_id"]

        async with create_connected_server_and_client_session(server_helper) as helper_client:
            closed = await helper_client.call_tool("close_session_by_student", {"student_id": student_id})
            assert not closed.isError
            assert closed.structuredContent["closed"] is True

        # A fresh open for the same student now succeeds — nothing live
        # was left blocking it.
        async with create_connected_server_and_client_session(server_owner) as owner_client2:
            reopened = await owner_client2.call_tool("open_session", _open_args(student_id))
            assert not reopened.isError
            assert reopened.structuredContent["session_id"] != session_id

    _run(flow())


# --- U5: session_unknown vs session_expired over MCP (KTD4) -----------------


def test_session_unknown_vs_session_expired_over_mcp(tmp_path: Path, content: engine.ContentIndex) -> None:
    facade = _build_facade(tmp_path, content, session_ttl_seconds=0.05)
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            never_issued = await client.call_tool(
                "get_candidates", {"session_id": "sess-never-issued", "skill_id": SKILL_ID}
            )

            opened = await client.call_tool("open_session", _open_args("stu-ttl"))
            session_id = opened.structuredContent["session_id"]
            await asyncio.sleep(0.15)
            expired = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            return never_issued, expired

    never_issued, expired = _run(flow())
    assert never_issued.isError and "session_unknown" in never_issued.content[0].text
    assert expired.isError and "session_expired" in expired.content[0].text


# --- U6: attempt_id idempotency (R19, AE7) over MCP --------------------------


def test_resubmitting_the_same_attempt_id_over_mcp_returns_the_same_outcome_once_logged(
    facade: serving.ServingFacade,
) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-retry"))
            session_id = opened.structuredContent["session_id"]
            candidates = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            instance = candidates.structuredContent["instances"][0]

            args = _submit_args(session_id, instance)
            first = await client.call_tool("submit_attempt", args)
            second = await client.call_tool("submit_attempt", args)  # same attempt_id
            return first, second

    first, second = _run(flow())
    assert not first.isError and not second.isError
    assert first.structuredContent == second.structuredContent

    log_path = next(facade.logs_dir.glob("*.jsonl"))
    events = list(log_module.iter_events(log_path))
    assert len(events) == 1


def test_discarded_attempt_over_mcp_surfaces_a_reason_instead_of_bare_false(
    facade: serving.ServingFacade,
) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-discard"))
            session_id = opened.structuredContent["session_id"]
            candidates = await client.call_tool("get_candidates", {"session_id": session_id, "skill_id": SKILL_ID})
            instance = candidates.structuredContent["instances"][0]

            args = _submit_args(
                session_id, instance,
                timed_out=True, engaged=False, session_last_event=True,
            )
            return await client.call_tool("submit_attempt", args)

    result = _run(flow())
    assert not result.isError
    assert result.structuredContent["logged"] is False
    assert isinstance(result.structuredContent["discard_reason"], str)
    assert result.structuredContent["discard_reason"]
