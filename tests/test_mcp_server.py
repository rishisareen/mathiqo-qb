"""Tests for tools/mcp_server.py — the U8 MCP server: KTD6's parity rule
(every facade operation exposed as exactly one tool, every tool resolves
to exactly one facade operation), the end-to-end smoke flow, consumer_id
attribution from server config (never the payload), the declared progress
summary shape, and the declared interface version. Also the figure plan's
U5 wire proof: a materialized figure travels the batch response verbatim
while the attempt-event write path stays figure-free.

Guarded: the whole file is skipped (not failed) when the `mcp` SDK isn't
installed, per the plan's "suite still green without it" contract.
Exercises an in-process MCP client against the real server (mcp's own
`create_connected_server_and_client_session` memory-transport harness)
rather than exhaustive per-tool unit tests, per this unit's own execution
note: mostly integration/packaging, prefer the smoke flow.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

pytest.importorskip("mcp")

import bank_activate
import bank_build
from conftest import REPO_ROOT
from orchestrator import engine, log as log_module, serving
from orchestrator.identity import IdentityRegistry

mcp_server = pytest.importorskip("mcp_server")
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

TEMPLATES_DIR = REPO_ROOT / "content" / "templates"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"
NUM_TEMPLATE = TEMPLATES_DIR / "TPL.FRAC.ADD.LIKE.NUM.001.yaml"
FIGURE_TEMPLATE_ID = NUM_TEMPLATE.stem
SKILL_ID = "FRAC.ADD.LIKE"
# U11: a real skill in the content graph the built bank (only NUM_TEMPLATE,
# which mints FRAC.ADD.LIKE instances alone) has zero instances for — the
# empty_pool case.
EMPTY_POOL_SKILL_ID = "FRAC.EQUIV"

API_SCHEMAS_DIR = REPO_ROOT / "schemas" / "api"


def _load_api_schema(name: str) -> dict:
    return json.loads((API_SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def _assert_valid(schema_name: str, obj: dict) -> None:
    schema = _load_api_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    assert errors == [], "; ".join(e.message for e in errors)

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


# The synthetic figure the wire test below serves. No *real* template
# declares a `figure` yet (the census re-annotation is the figure plan's U6
# and U7), so this module authors one onto its own tmp-dir copy of
# NUM_TEMPLATE rather than waiting on content: the thing under test is the
# transport, and a synthetic figure exercises it exactly as a real one would
# — bank_build materializes it through the same path, so it goes through the
# full Stage 3 figure gate on the way in.
#
# Deliberately built to exercise all three of materialization's outputs on
# one figure: an integer (`divisions` from `b`), an exact rational string
# (`position` from `a/b`), and a label rendered through the stem's own
# substitution dict (`label` from `label_i18n`).
FIGURE_TEMPLATE_FIGURE = [
    {
        "kind": "number-line",
        "min_expr": "0",
        "max_expr": "1",
        "divisions_expr": "b",
        "points": [
            {"position_expr": "a/b", "label_i18n": {"en-IN": "{{a}} of {{b}}"}},
        ],
    }
]


def _figure_templates_dir(tmp_path: Path) -> Path:
    """A one-template dir whose copy of NUM_TEMPLATE carries a figure."""
    dest = tmp_path / "figure-templates"
    dest.mkdir(exist_ok=True)
    template = yaml.safe_load(NUM_TEMPLATE.read_text(encoding="utf-8"))
    template["diagram_required"] = True  # a figure-bearing template is figure-dependent
    template["figure"] = FIGURE_TEMPLATE_FIGURE
    (dest / NUM_TEMPLATE.name).write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    return dest


@pytest.fixture()
def content() -> engine.ContentIndex:
    return engine.load_content(REPO_ROOT / "content" / "fractions")


def _build_facade(
    tmp_path: Path,
    content: engine.ContentIndex,
    templates_dir: Path | None = None,
    **facade_kwargs,
) -> serving.ServingFacade:
    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    if templates_dir is None:
        templates_dir = _templates_dir(tmp_path)
    manifest, per_template = bank_build.run_build(templates_dir, LOCALE_PACK, "mcp-build", pool_cap=5)
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

            progress = await client.call_tool(
                "get_student_progress", {"student_id": "stu-smoke"}
            )
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
            progress = await client.call_tool("get_student_progress", {"student_id": "stu-shape"})
            return progress.structuredContent

    result = _run(flow())
    assert set(result) == {"student_id", "skills"}
    skill = result["skills"][SKILL_ID]
    assert set(skill) == {"skill_id", "theta", "n", "certified_depth", "certification_status", "seen_count"}


# --- U8: recommend_next tool + session-less get_student_progress/recommend_next
# over MCP (R5, R6, R7, R20) ---------------------------------------------------


def test_recommend_next_returns_declared_shape_over_mcp(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow() -> dict:
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("open_session", _open_args("stu-recommend"))
            return await client.call_tool(
                "recommend_next",
                {"student_id": "stu-recommend", **DEFAULT_OPEN_EXTRA},
            )

    result = _run(flow())
    assert not result.isError
    body = result.structuredContent
    assert set(body) == {"student_id", "bank_version", "content_version", "recommendations"}
    assert body["student_id"] == "stu-recommend"
    assert body["bank_version"] == "mcp-build"
    assert body["content_version"]
    assert body["recommendations"]
    _assert_valid("recommendation-result.schema.json", body)
    entry = body["recommendations"][0]
    assert set(entry) == {"skill_id", "reason", "context", "target_depth", "unservable"}
    assert entry["reason"] in {"review", "remediation", "stepdown", "prereq_probe", "placement", "frontier"}


def test_recommend_next_and_get_student_progress_are_session_less_over_mcp(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """R20/F4's whole point, at the MCP transport: consumer A opens and
    holds the student's one active serving session; consumer B, linked to
    the same student via a *separate* server (its own consumer_id), can
    still successfully call recommend_next and get_student_progress for
    that student with no session_id of its own — proof this surface
    doesn't require a live session for either read."""
    student_id = identity_registry.register_student({"guardian_email": "g@example.com"}, consumer_id="partner.a")
    identity_registry.link_student(student_id, {"guardian_email": "g@example.com"}, consumer_id="partner.b")

    server_a = mcp_server.build_server(linked_facade, "partner.a")
    server_b = mcp_server.build_server(linked_facade, "partner.b")

    async def flow():
        async with create_connected_server_and_client_session(server_a) as client_a:
            opened = await client_a.call_tool("open_session", _open_args(student_id))
            assert not opened.isError
            session_id = opened.structuredContent["session_id"]

            async with create_connected_server_and_client_session(server_b) as client_b:
                progress = await client_b.call_tool("get_student_progress", {"student_id": student_id})
                assert not progress.isError

                recommendations = await client_b.call_tool(
                    "recommend_next", {"student_id": student_id, **DEFAULT_OPEN_EXTRA}
                )
                assert not recommendations.isError
                assert recommendations.structuredContent["recommendations"]

            # partner.a's session is unaffected by partner.b's session-less reads.
            candidates = await client_a.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": SKILL_ID}
            )
            assert not candidates.isError

    _run(flow())


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


def test_get_candidates_refused_for_unlinked_consumer_over_mcp(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """Closed simplify-review finding: get_candidates was the one
    student-scoped tool with no link check at any layer — a stranger
    holding a session_id could read another partner's candidate pool."""
    student_id = identity_registry.register_student({"guardian_email": "cand@example.com"}, consumer_id="partner.owner")
    owner_server = mcp_server.build_server(linked_facade, "partner.owner")
    stranger_server = mcp_server.build_server(linked_facade, "partner.stranger")

    async def flow():
        async with create_connected_server_and_client_session(owner_server) as owner_client:
            opened = await owner_client.call_tool("open_session", _open_args(student_id))
            session_id = opened.structuredContent["session_id"]

        async with create_connected_server_and_client_session(stranger_server) as stranger_client:
            return await stranger_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": "FRAC.ADD.LIKE"}
            )

    result = _run(flow())
    assert result.isError
    assert "not_linked" in result.content[0].text


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


# =============================================================================
# U11: identity, catalog, and batch tools — R14's parity rule extended
# =============================================================================


# --- U4 identity tools over MCP -----------------------------------------------


def test_register_student_returns_declared_shape_and_is_findable(
    linked_facade: serving.ServingFacade,
) -> None:
    server = mcp_server.build_server(linked_facade, "partner.a")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            registered = await client.call_tool(
                "register_student", {"attributes": {"guardian_email": "reg@example.com"}}
            )
            assert not registered.isError
            _assert_valid("identity-registration.schema.json", registered.structuredContent)
            student_id = registered.structuredContent["student_id"]
            assert student_id.startswith("stu-")

            found = await client.call_tool(
                "find_student", {"attributes": {"guardian_email": "reg@example.com"}}
            )
            assert not found.isError
            _assert_valid("identity-lookup.schema.json", found.structuredContent)
            assert found.structuredContent == {"found": True, "student_id": student_id}

            not_found = await client.call_tool(
                "find_student", {"attributes": {"guardian_email": "nope@example.com"}}
            )
            assert not not_found.isError
            _assert_valid("identity-lookup.schema.json", not_found.structuredContent)
            assert not_found.structuredContent == {"found": False, "student_id": None}

    _run(flow())


def test_link_student_returns_declared_shape_for_a_second_consumer(
    linked_facade: serving.ServingFacade,
) -> None:
    server_a = mcp_server.build_server(linked_facade, "partner.a")
    server_b = mcp_server.build_server(linked_facade, "partner.b")
    attrs = {"guardian_email": "link@example.com"}

    async def flow():
        async with create_connected_server_and_client_session(server_a) as client_a:
            registered = await client_a.call_tool("register_student", {"attributes": attrs})
            return registered.structuredContent["student_id"]

    student_id = _run(flow())

    async def flow2():
        async with create_connected_server_and_client_session(server_b) as client_b:
            return await client_b.call_tool(
                "link_student",
                {"student_id": student_id, "attributes": attrs, "link_basis": "guardian_authorized"},
            )

    linked = _run(flow2())
    assert not linked.isError
    _assert_valid("identity-link.schema.json", linked.structuredContent)
    assert linked.structuredContent == {
        "linked": True,
        "student_id": student_id,
        "consumer_id": "partner.b",
        "link_basis": "guardian_authorized",
    }


def test_link_student_mismatched_or_unknown_raises_structured_error_without_leaking_attributes(
    linked_facade: serving.ServingFacade,
) -> None:
    server_a = mcp_server.build_server(linked_facade, "partner.a")
    server_c = mcp_server.build_server(linked_facade, "partner.c")

    async def register():
        async with create_connected_server_and_client_session(server_a) as client_a:
            registered = await client_a.call_tool(
                "register_student", {"attributes": {"guardian_email": "mismatch@example.com"}}
            )
            return registered.structuredContent["student_id"]

    student_id = _run(register())

    async def flow():
        async with create_connected_server_and_client_session(server_c) as client_c:
            mismatched = await client_c.call_tool(
                "link_student",
                {"student_id": student_id, "attributes": {"guardian_email": "wrong@example.com"}},
            )
            unknown = await client_c.call_tool(
                "link_student", {"student_id": "stu-doesnotexist", "attributes": {"a": 1}}
            )
            return mismatched, unknown

    mismatched, unknown = _run(flow())
    assert mismatched.isError
    mismatched_text = mismatched.content[0].text
    assert "do not match" in mismatched_text
    assert "wrong@example.com" not in mismatched_text
    assert "mismatch@example.com" not in mismatched_text

    assert unknown.isError
    assert "stu-doesnotexist" in unknown.content[0].text


def test_register_and_link_and_find_student_without_identity_registry_are_refused(
    facade: serving.ServingFacade,
) -> None:
    server = mcp_server.build_server(facade, "partner.a")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            reg = await client.call_tool("register_student", {"attributes": {"a": 1}})
            link = await client.call_tool("link_student", {"student_id": "stu-x", "attributes": {"a": 1}})
            find = await client.call_tool("find_student", {"attributes": {"a": 1}})
            return reg, link, find

    reg, link, find = _run(flow())
    assert reg.isError and link.isError and find.isError


# --- U7 catalog tools over MCP: session-less, consumer-id-free -------------


def test_list_curricula_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool("list_curricula", {})

    result = _run(flow())
    assert not result.isError
    _assert_valid("catalog-curricula.schema.json", result.structuredContent)
    countries = {c["country"] for c in result.structuredContent["countries"]}
    assert "IN" in countries


def test_list_units_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool(
                "list_units", {"country": "IN", "curriculum": "NCERT", "grade": 6}
            )

    result = _run(flow())
    assert not result.isError
    _assert_valid("catalog-units.schema.json", result.structuredContent)


def test_list_skills_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool(
                "list_skills", {"country": "IN", "curriculum": "NCERT", "grade": 6}
            )

    result = _run(flow())
    assert not result.isError
    _assert_valid("catalog-skills.schema.json", result.structuredContent)
    assert any(s["skill_id"] == SKILL_ID for s in result.structuredContent["skills"])


def test_get_skill_graph_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            scoped = await client.call_tool("get_skill_graph", {"skill_ids": [SKILL_ID]})
            whole = await client.call_tool("get_skill_graph", {})
            return scoped, whole

    scoped, whole = _run(flow())
    assert not scoped.isError and not whole.isError
    _assert_valid("catalog-skill-graph.schema.json", scoped.structuredContent)
    _assert_valid("catalog-skill-graph.schema.json", whole.structuredContent)
    assert set(scoped.structuredContent["skills"]) == {SKILL_ID}
    assert len(whole.structuredContent["skills"]) > 1


# --- U9/U10 batch tools over MCP ---------------------------------------------


def test_fetch_batch_returns_declared_shape(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-batch"))
            session_id = opened.structuredContent["session_id"]
            return await client.call_tool(
                "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID, "batch_size": 5}
            )

    result = _run(flow())
    assert not result.isError
    body = result.structuredContent
    _assert_valid("batch-result.schema.json", body)
    assert body["count_requested"] == 5
    assert 0 < body["count_returned"] <= 5
    assert {item["rung"] for item in body["items"]} <= {"target", "easier", "harder"}
    assert {item["context"] for item in body["items"]} <= {"practice", "probe"}


# --- Figures over the wire (partner-figure plan U5; R5, AE1-AE3) -------------


def _figure_bearing_batch(tmp_path: Path, content: engine.ContentIndex, student_id: str) -> tuple[dict, Path]:
    """fetch_batch over MCP against a bank built from a figure-bearing
    template; returns (batch body, banks dir)."""
    facade = _build_facade(tmp_path, content, templates_dir=_figure_templates_dir(tmp_path))
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args(student_id))
            session_id = opened.structuredContent["session_id"]
            fetched = await client.call_tool(
                "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID, "batch_size": 5}
            )
            assert not fetched.isError
            return fetched.structuredContent

    return _run(flow()), tmp_path / "banks"


def test_fetch_batch_serves_materialized_figures_verbatim(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """R5/AE1-AE2: a figure reaches a partner through the *existing* batch
    response — no new batch-schema field, nothing stripped, nothing
    reshaped. The strong form of "verbatim" here is whole-instance equality
    against the JSON bank_build wrote to disk, which also covers AE2's
    "all pre-existing instance fields unchanged" in the same assertion."""
    body, banks_dir = _figure_bearing_batch(tmp_path, content, "stu-figure-batch")

    # The batch contract is unchanged by figures — same schema, still valid.
    _assert_valid("batch-result.schema.json", body)
    assert body["count_returned"] > 0, "figure-bearing skill served no items at all"

    from_bank = {
        instance["item_instance_id"]: instance
        for instance in json.loads(
            (banks_dir / "mcp-build" / "instances" / f"{FIGURE_TEMPLATE_ID}.json").read_text(
                encoding="utf-8"
            )
        )
    }

    for item in body["items"]:
        instance = item["instance"]
        assert instance == from_bank[instance["item_instance_id"]], (
            "the served instance differs from the one bank_build materialized offline "
            "— serving must pass instances through untouched (KD5, KD6)"
        )

        figures = instance["figures"]
        assert figures is not None, f"{instance['item_instance_id']} served figures: null"
        assert len(figures) == 1
        figure = figures[0]

        # Concrete on the wire: every expression evaluated, every label
        # rendered, and no authoring artifact (`*_expr`, `*_i18n`) left.
        assert figure["kind"] == "number-line"
        assert not [k for k in figure if k.endswith("_expr") or k.endswith("_i18n")]
        variables = instance["variables"]
        assert figure["min"] == 0
        assert figure["max"] == 1
        assert figure["divisions"] == variables["b"]
        point = figure["points"][0]
        assert point["position"] == f"{variables['a']}/{variables['b']}"
        assert point["label"] == f"{variables['a']} of {variables['b']}", (
            "the figure label must render through the same substitutions as the stem (KTD7)"
        )

    # KD3: the stem keeps describing the figure in words, so a consumer that
    # ignores `figures` answers the item exactly as before.
    assert all(item["instance"]["stem"] for item in body["items"])


def test_attempt_on_a_figure_bearing_item_validates_against_the_unchanged_attempt_schema(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """R4/AE3: figures are stimulus-only. An attempt against a figure-bearing
    item logs an event that still validates against the untouched
    schemas/attempt-event.schema.json, and carries no figure data — the
    write path never learned about figures and must not start now."""
    student_id = "stu-figure-attempt"
    facade = _build_facade(tmp_path, content, templates_dir=_figure_templates_dir(tmp_path))
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args(student_id))
            session_id = opened.structuredContent["session_id"]
            fetched = await client.call_tool(
                "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID, "batch_size": 1}
            )
            instance = fetched.structuredContent["items"][0]["instance"]
            assert instance["figures"], "fixture assumption: the served item carries a figure"
            submitted = await client.call_tool("submit_attempt", _submit_args(session_id, instance))
            assert not submitted.isError, submitted.content
            assert submitted.structuredContent["logged"] is True
            return instance

    instance = _run(flow())

    schema = json.loads(
        (REPO_ROOT / "schemas" / "attempt-event.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / f"{student_id}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1, f"expected exactly one logged attempt, got {len(events)}"
    event = events[0]
    errors = sorted(validator.iter_errors(event), key=lambda e: list(e.path))
    assert errors == [], "; ".join(e.message for e in errors)

    assert event["item_instance_id"] == instance["item_instance_id"]
    assert not [key for key in event if "figure" in key.lower()], (
        f"figure data leaked into the attempt-event write path: {sorted(event)}"
    )
    assert set(event) <= set(schema["properties"]), (
        "the logged attempt carries field(s) the attempt-event contract doesn't declare: "
        f"{sorted(set(event) - set(schema['properties']))}"
    )


def test_refill_batch_returns_declared_shape_including_no_further_items(
    facade: serving.ServingFacade,
) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-refill"))
            session_id = opened.structuredContent["session_id"]
            # Pool has only 5 instances (pool_cap=5); a full-size fetch
            # (default batch_size=10) exhausts everything available.
            await client.call_tool("fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID})
            return await client.call_tool(
                "refill_batch",
                {"session_id": session_id, "skill_id": SKILL_ID, "attempts_flushed": True},
            )

    result = _run(flow())
    assert not result.isError
    body = result.structuredContent
    _assert_valid("batch-refill-result.schema.json", body)
    assert body["batch"] is None
    assert body["signal"] == "no_further_items"
    assert body["supersede"] is False


def test_fetch_batch_and_refill_batch_refused_for_unlinked_consumer_over_mcp(
    linked_facade: serving.ServingFacade, identity_registry: IdentityRegistry
) -> None:
    """U11's own taxonomy-coverage requirement: at least one *new* tool
    from this unit must demonstrate surfacing not_linked correctly."""
    student_id = identity_registry.register_student(
        {"guardian_email": "batch@example.com"}, consumer_id="partner.owner"
    )
    server_owner = mcp_server.build_server(linked_facade, "partner.owner")
    server_stranger = mcp_server.build_server(linked_facade, "partner.stranger")

    async def open_flow():
        async with create_connected_server_and_client_session(server_owner) as owner_client:
            opened = await owner_client.call_tool("open_session", _open_args(student_id))
            return opened.structuredContent["session_id"]

    session_id = _run(open_flow())

    async def flow():
        async with create_connected_server_and_client_session(server_stranger) as stranger_client:
            fetched = await stranger_client.call_tool(
                "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID}
            )
            refilled = await stranger_client.call_tool(
                "refill_batch",
                {"session_id": session_id, "skill_id": SKILL_ID, "attempts_flushed": True},
            )
            return fetched, refilled

    fetched, refilled = _run(flow())
    assert fetched.isError and "not_linked" in fetched.content[0].text
    assert refilled.isError and "not_linked" in refilled.content[0].text


# --- R17: consumer_id unforgeable through every new U11 tool -----------------

NEW_TOOL_NAMES = (
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


def test_new_tools_never_accept_consumer_id_as_a_parameter(facade: serving.ServingFacade) -> None:
    server = mcp_server.build_server(facade, "test.consumer")

    async def list_tools():
        async with create_connected_server_and_client_session(server) as client:
            result = await client.list_tools()
            return {t.name: t for t in result.tools}

    tools = _run(list_tools())
    assert set(NEW_TOOL_NAMES) <= set(tools)
    for name in NEW_TOOL_NAMES:
        properties = tools[name].inputSchema.get("properties", {})
        assert "consumer_id" not in properties, f"{name} exposes a caller-suppliable consumer_id"


# --- PII canary: a sentinel lookup attribute never leaks anywhere -----------


def _assert_no_sentinel(value, sentinel: str) -> None:
    dumped = json.dumps(value, default=str)
    assert sentinel not in dumped


def test_pii_canary_sentinel_never_leaks_across_the_new_mcp_surface(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """U11's own required test scenario: register a student with a
    sentinel guardian_email, exercise every error-taxonomy signal this MCP
    surface can produce (unknown_skill, empty_pool, exhausted,
    no_further_items, session_conflict, session_unknown, session_expired,
    not_linked, ingest_rejected, discarded — signals.ALL in full) plus a
    batch fetch and a progress/recommendation read, and assert the
    sentinel never appears in any tool's returned dict or in any
    ToolError's message string."""
    SENTINEL = "canary-guardian-9f3e1c2a8b7d@example.com"

    identity_registry = IdentityRegistry(tmp_path / "identity" / "registry.jsonl")
    main_root = tmp_path / "main"
    main_root.mkdir()
    ttl_root = tmp_path / "ttl"
    ttl_root.mkdir()
    main_facade = _build_facade(main_root, content, identity=identity_registry)
    ttl_facade = _build_facade(ttl_root, content, identity=identity_registry, session_ttl_seconds=0.05)

    owner = mcp_server.build_server(main_facade, "partner.owner")
    stranger = mcp_server.build_server(main_facade, "partner.stranger")
    helper = mcp_server.build_server(main_facade, "partner.helper")
    ttl_owner = mcp_server.build_server(ttl_facade, "partner.owner")

    seen_payloads: list = []

    def _record(result) -> None:
        seen_payloads.append(result.content[0].text if result.isError else result.structuredContent)

    async def flow():
        async with create_connected_server_and_client_session(owner) as owner_client:
            registered = await owner_client.call_tool(
                "register_student", {"attributes": {"guardian_email": SENTINEL}}
            )
            _record(registered)
            assert not registered.isError
            student_id = registered.structuredContent["student_id"]

            _record(await owner_client.call_tool(
                "find_student", {"attributes": {"guardian_email": SENTINEL}}
            ))

            opened = await owner_client.call_tool("open_session", _open_args(student_id))
            _record(opened)
            session_id = opened.structuredContent["session_id"]

            # unknown_skill
            _record(await owner_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": "NOPE.NOT.A.SKILL"}
            ))
            # empty_pool
            _record(await owner_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": EMPTY_POOL_SKILL_ID}
            ))

            # exhaust every real instance for SKILL_ID, then hit `exhausted`
            candidates = await owner_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": SKILL_ID}
            )
            _record(candidates)
            for instance in candidates.structuredContent["instances"]:
                submitted = await owner_client.call_tool("submit_attempt", _submit_args(session_id, instance))
                _record(submitted)
            exhausted = await owner_client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": SKILL_ID}
            )
            _record(exhausted)
            assert exhausted.structuredContent["signal"] == "exhausted"
            repeat_instance = exhausted.structuredContent["instances"][0]

            # discarded (§3 abandonment guard)
            discarded = await owner_client.call_tool(
                "submit_attempt",
                _submit_args(
                    session_id, repeat_instance,
                    attempt_id="canary-discard", timed_out=True, engaged=False, session_last_event=True,
                ),
            )
            _record(discarded)
            assert discarded.structuredContent["signal"] == "discarded"

            # ingest_rejected: an out-of-taxonomy `context` fails attempt-event schema
            rejected = await owner_client.call_tool(
                "submit_attempt",
                _submit_args(session_id, repeat_instance, attempt_id="canary-reject", context="not_a_real_context"),
            )
            _record(rejected)
            assert rejected.isError

            # fetch_batch against an exhausted pool -> a normal, empty batch
            _record(await owner_client.call_tool(
                "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID}
            ))

            # no_further_items
            refilled = await owner_client.call_tool(
                "refill_batch",
                {"session_id": session_id, "skill_id": SKILL_ID, "attempts_flushed": True},
            )
            _record(refilled)
            assert refilled.structuredContent["signal"] == "no_further_items"

            # progress / recommendation reads
            _record(await owner_client.call_tool("get_student_progress", {"student_id": student_id}))
            _record(await owner_client.call_tool(
                "recommend_next", {"student_id": student_id, **DEFAULT_OPEN_EXTRA}
            ))

            # session_unknown
            _record(await owner_client.call_tool(
                "get_candidates", {"session_id": "sess-never-issued-canary", "skill_id": SKILL_ID}
            ))

            # not_linked, via a stranger consumer never linked to this student
            async with create_connected_server_and_client_session(stranger) as stranger_client:
                not_linked_progress = await stranger_client.call_tool(
                    "get_student_progress", {"student_id": student_id}
                )
                _record(not_linked_progress)
                assert not_linked_progress.isError and "not_linked" in not_linked_progress.content[0].text

                not_linked_batch = await stranger_client.call_tool(
                    "fetch_batch", {"session_id": session_id, "skill_id": SKILL_ID}
                )
                _record(not_linked_batch)

                mismatched_link = await stranger_client.call_tool(
                    "link_student",
                    {"student_id": student_id, "attributes": {"guardian_email": "wrong@example.com"}},
                )
                _record(mismatched_link)

            # session_conflict: a second, now-linked consumer opens while
            # the owner's session is still live.
            identity_registry.link_student(student_id, {"guardian_email": SENTINEL}, consumer_id="partner.helper")
            async with create_connected_server_and_client_session(helper) as helper_client:
                conflict = await helper_client.call_tool("open_session", _open_args(student_id))
                _record(conflict)
                assert conflict.isError and "session_conflict" in conflict.content[0].text

        # session_expired: a separate short-TTL facade sharing the same
        # identity registry.
        async with create_connected_server_and_client_session(ttl_owner) as ttl_client:
            ttl_opened = await ttl_client.call_tool("open_session", _open_args(student_id))
            _record(ttl_opened)
            await asyncio.sleep(0.15)
            expired = await ttl_client.call_tool(
                "get_candidates",
                {"session_id": ttl_opened.structuredContent["session_id"], "skill_id": SKILL_ID},
            )
            _record(expired)
            assert expired.isError and "session_expired" in expired.content[0].text

    _run(flow())

    for payload in seen_payloads:
        _assert_no_sentinel(payload, SENTINEL)


# --- R19 Path B, reachable end-to-end through the real MCP transport --------


def test_submit_attempt_path_b_reachable_through_mcp_transport_after_bank_repoint(
    tmp_path: Path, content: engine.ContentIndex
) -> None:
    """P0 fix: `submit_attempt`'s instance resolution used to look up
    `item_instance_id` only in `session_obj.bank.by_id` (the *current*
    session's pinned bank) — an instance genuinely served from a build that
    a later `bank_activate` repoint superseded could never be found there
    by a fresh session pinned to the new build, so this tool always raised
    `unresolvable_instance` before the facade's `submit_attempt` (and its
    Path B session-less stale-bank validation, R19) was ever reached. Path
    B was real and tested at the facade level (`tests/test_serving.py`) but
    unreachable from any real client.

    Scenario mirroring the recovery case Path B exists for: open a session
    against bank-a, fetch a real instance from it, repoint ACTIVE to
    bank-b, open a *new* session for the same student (now pinned to
    bank-b, simulating a client that lost its old session and reopened
    under the newer build), and submit the bank-a instance's attempt
    through the real `submit_attempt` tool. Must succeed via Path B (not
    `unresolvable_instance`), and a second identical resubmission must then
    hit Path A (cached — no double-apply)."""
    banks_dir = tmp_path / "banks"
    banks_dir.mkdir()
    templates_dir = _templates_dir(tmp_path)

    manifest_a, per_template_a = bank_build.run_build(templates_dir, LOCALE_PACK, "bank-a", pool_cap=5)
    bank_build.write_bank(banks_dir, "bank-a", manifest_a, per_template_a)
    bank_activate.activate(banks_dir, "bank-a")

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    facade = serving.ServingFacade(banks_dir, logs_dir, content)
    server = mcp_server.build_server(facade, "test.consumer")

    student_id = "stu-pathb-transport"

    async def open_a_and_fetch_instance():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args(student_id))
            assert not opened.isError
            assert opened.structuredContent["bank_version"] == "bank-a"
            session_id = opened.structuredContent["session_id"]
            candidates = await client.call_tool(
                "get_candidates", {"session_id": session_id, "skill_id": SKILL_ID}
            )
            return candidates.structuredContent["instances"][0]

    instance_a = _run(open_a_and_fetch_instance())
    assert instance_a["bank_version"] == "bank-a"

    # Repoint ACTIVE to a second, distinct build — bank-a stays on disk as
    # a retained build (KTD7), never deleted.
    manifest_b, per_template_b = bank_build.run_build(templates_dir, LOCALE_PACK, "bank-b", pool_cap=5)
    bank_build.write_bank(banks_dir, "bank-b", manifest_b, per_template_b)
    bank_activate.activate(banks_dir, "bank-b")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            # Same consumer reopening for the same student takes over
            # (R20) — the new session pins to whatever is ACTIVE now,
            # bank-b, simulating the client resubmitting under a fresh
            # session after losing the one opened against bank-a.
            opened_b = await client.call_tool("open_session", _open_args(student_id))
            assert not opened_b.isError
            assert opened_b.structuredContent["bank_version"] == "bank-b"
            session_id_b = opened_b.structuredContent["session_id"]

            args = _submit_args(session_id_b, instance_a)
            submitted = await client.call_tool("submit_attempt", args)
            assert not submitted.isError, submitted.content
            assert submitted.structuredContent["logged"] is True
            assert submitted.structuredContent["repeat"] is False
            first_event_id = submitted.structuredContent["event_id"]

            # A second, identical resubmission of the same attempt_id must
            # hit Path A — the cached outcome, not a second log line.
            resubmitted = await client.call_tool("submit_attempt", args)
            assert not resubmitted.isError, resubmitted.content
            assert resubmitted.structuredContent["event_id"] == first_event_id

    _run(flow())

    log_path = logs_dir / f"{student_id}.jsonl"
    events = list(log_module.iter_events(log_path))
    assert len(events) == 1
    assert events[0]["bank_version"] == "bank-a"
    assert events[0]["item_instance_id"] == instance_a["item_instance_id"]


def test_submit_attempt_unresolvable_instance_still_rejected_when_truly_bogus(
    facade: serving.ServingFacade,
) -> None:
    """The fallback added for R19 Path B must not turn every bogus
    item_instance_id into a silent pass-through: an id that names neither
    the session's own pinned bank nor any retained build on disk (a typo,
    or a genuinely foreign id) still raises `unresolvable_instance`."""
    server = mcp_server.build_server(facade, "test.consumer")

    async def flow():
        async with create_connected_server_and_client_session(server) as client:
            opened = await client.call_tool("open_session", _open_args("stu-bogus-instance"))
            session_id = opened.structuredContent["session_id"]
            args = _submit_args(
                session_id,
                {"item_instance_id": "TPL.FRAC.ADD.LIKE.NUM.001#no-such-build#00000", "answer": "1"},
            )
            return await client.call_tool("submit_attempt", args)

    result = _run(flow())
    assert result.isError
    assert "unresolvable_instance" in result.content[0].text
