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

mcp_server = pytest.importorskip("mcp_server")
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
SKILL_ID = "FRAC.ADD.LIKE"


def _templates_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "templates"
    dest.mkdir(exist_ok=True)
    shutil.copy(NUM_TEMPLATE, dest / NUM_TEMPLATE.name)
    return dest


@pytest.fixture()
def content() -> engine.ContentIndex:
    return engine.load_content(REPO_ROOT / "content" / "fractions")


@pytest.fixture()
def facade(tmp_path: Path, content: engine.ContentIndex) -> serving.ServingFacade:
    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    manifest, per_template = bank_build.run_build(_templates_dir(tmp_path), LOCALE_PACK, "mcp-build", pool_cap=5)
    bank_build.write_bank(banks_dir, "mcp-build", manifest, per_template)
    (banks_dir / serving.ACTIVE_POINTER_NAME).write_text("mcp-build", encoding="utf-8")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return serving.ServingFacade(banks_dir, logs_dir, content)


def _run(coro):
    return asyncio.run(coro)


def _submit_args(session_id: str, instance: dict, **overrides) -> dict:
    args = dict(
        session_id=session_id, skill_id=SKILL_ID, item_instance_id=instance["item_instance_id"],
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
            opened = await client.call_tool("open_session", {"student_id": "stu-smoke"})
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
            opened = await client.call_tool("open_session", {"student_id": "stu-forge"})
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
            opened = await client.call_tool("open_session", {"student_id": "stu-shape"})
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
            return await client.call_tool("open_session", {"student_id": "stu-noactive"})

    result = _run(flow())
    assert result.isError
    text = result.content[0].text
    assert str(banks_dir) not in text
