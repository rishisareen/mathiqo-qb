"""Tests for orchestrator/identity.py — the U4 student identity registry
(KD9, R16, R17, KTD11): opaque student ids, registration attributes held
only in this registry, and attribute-proven per-consumer links, derived by
replaying an append-only JSONL log.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from orchestrator.identity import (
    AttributeMismatchError,
    IdentityRegistry,
    UnknownStudentError,
)
from orchestrator.serving import NoIdentityRegistryError, ServingFacade

ATTRS_A = {"guardian_email": "parent-a@example.com", "family_code": "FAM-AAA"}
ATTRS_B = {"guardian_email": "parent-b@example.com", "family_code": "FAM-BBB"}


@pytest.fixture()
def registry_path(tmp_path: Path) -> Path:
    # Sibling of a hypothetical logs/ dir, not inside it (see module
    # docstring): tmp_path/identity/registry.jsonl next to tmp_path/logs/.
    return tmp_path / "identity" / "registry.jsonl"


@pytest.fixture()
def registry(registry_path: Path) -> IdentityRegistry:
    return IdentityRegistry(registry_path)


# --- registration ------------------------------------------------------


def test_register_returns_stable_opaque_id_without_attributes(registry: IdentityRegistry) -> None:
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    assert isinstance(student_id, str)
    assert student_id.startswith("stu-")
    # The returned value itself never contains the attributes dict or any
    # of its values.
    assert "example.com" not in student_id
    assert "FAM-AAA" not in student_id
    for value in ATTRS_A.values():
        assert str(value) not in student_id
    # Stable: looking the student back up by their exact attributes finds
    # the same id.
    assert registry.find_student(ATTRS_A) == student_id


def test_generated_ids_are_not_sequential_or_derivable(registry: IdentityRegistry) -> None:
    ids = [
        registry.register_student({"guardian_email": f"g{i}@example.com"}, consumer_id="partner.acme")
        for i in range(5)
    ]
    assert len(set(ids)) == 5
    # Not integers, not a shared incrementing counter, not derivable from
    # the previous id: the only structure is the fixed "stu-" prefix, the
    # random hex suffixes share no common pattern beyond length.
    suffixes = [sid[len("stu-"):] for sid in ids]
    assert len(set(suffixes)) == 5
    for suffix in suffixes:
        assert len(suffix) == 32
        int(suffix, 16)  # hex, but not sequential/incrementing
    # No suffix is a trivial increment of another (e.g. differs by 1 as an int).
    values = [int(s, 16) for s in suffixes]
    for i, v in enumerate(values):
        for j, w in enumerate(values):
            if i != j:
                assert abs(v - w) != 1


# --- linking -------------------------------------------------------------


def test_registering_consumer_is_linked_automatically(registry: IdentityRegistry) -> None:
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    assert registry.is_linked("partner.acme", student_id) is True


def test_other_consumer_not_linked_until_link_student_called(registry: IdentityRegistry) -> None:
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    assert registry.is_linked("partner.school", student_id) is False
    registry.link_student(student_id, ATTRS_A, consumer_id="partner.school", link_basis="guardian_authorized")
    assert registry.is_linked("partner.school", student_id) is True
    # The original registering consumer stays linked too — one student, many links.
    assert registry.is_linked("partner.acme", student_id) is True


def test_link_student_rejects_mismatched_attributes(registry: IdentityRegistry) -> None:
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    with pytest.raises(AttributeMismatchError):
        registry.link_student(student_id, ATTRS_B, consumer_id="partner.school")
    assert registry.is_linked("partner.school", student_id) is False


def test_link_student_rejects_unknown_student_id(registry: IdentityRegistry) -> None:
    with pytest.raises(UnknownStudentError):
        registry.link_student("stu-doesnotexist", ATTRS_A, consumer_id="partner.school")


# --- find_student: exact match only --------------------------------------


def test_find_student_exact_match(registry: IdentityRegistry) -> None:
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    assert registry.find_student(dict(ATTRS_A)) == student_id


def test_find_student_near_miss_does_not_match(registry: IdentityRegistry) -> None:
    registry.register_student(ATTRS_A, consumer_id="partner.acme")
    near_miss = dict(ATTRS_A)
    near_miss["family_code"] = "FAM-AAX"  # one differing field
    assert registry.find_student(near_miss) is None
    # Extra field also fails to match.
    extra = dict(ATTRS_A, extra_field="x")
    assert registry.find_student(extra) is None
    # Unregistered attributes entirely.
    assert registry.find_student(ATTRS_B) is None


# --- replay / "process restart" -------------------------------------------


def test_rebuilding_from_disk_reproduces_links_and_link_basis(registry_path: Path) -> None:
    registry = IdentityRegistry(registry_path)
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    registry.link_student(student_id, ATTRS_A, consumer_id="partner.school", link_basis="guardian_authorized")
    registry.link_student(student_id, ATTRS_A, consumer_id="partner.tutor", link_basis=None)

    # Simulate a process restart: a fresh IdentityRegistry over the same file.
    rebuilt = IdentityRegistry(registry_path)

    assert rebuilt.find_student(ATTRS_A) == student_id
    assert rebuilt.is_linked("partner.acme", student_id) is True
    assert rebuilt.is_linked("partner.school", student_id) is True
    assert rebuilt.is_linked("partner.tutor", student_id) is True
    assert rebuilt.is_linked("partner.stranger", student_id) is False

    # link_basis values reproduce exactly, including the auto-link's None.
    assert rebuilt._links[student_id]["partner.acme"] is None
    assert rebuilt._links[student_id]["partner.school"] == "guardian_authorized"
    assert rebuilt._links[student_id]["partner.tutor"] is None


def test_rebuild_from_missing_file_is_empty_state(tmp_path: Path) -> None:
    registry = IdentityRegistry(tmp_path / "identity" / "registry.jsonl")
    assert registry.find_student(ATTRS_A) is None
    assert registry.is_linked("partner.acme", "stu-anything") is False


def test_load_tolerates_a_torn_trailing_write(registry_path: Path, capsys) -> None:
    """`_load` now reuses `orchestrator/log.py`'s `iter_events` (a closed
    code-review reliability finding): mirrors tests/test_log.py's own
    `test_iter_events_discards_a_truncated_trailing_line_with_a_warning` —
    a registry file with a valid `student_registered`/`consumer_linked`
    pair followed by a truncated, unparseable last line (a process killed
    mid-append) must still load successfully, discarding only the torn
    last line with a stderr warning. Before this fix, `_load`'s bare
    `json.loads(line)` raised uncaught on exactly this case, permanently
    blocking registry startup."""
    registry = IdentityRegistry(registry_path)
    student_id = registry.register_student(ATTRS_A, consumer_id="partner.acme")
    with registry_path.open("a", encoding="utf-8") as f:
        f.write('{"event_kind": "consumer_linked", "student_id": "' + student_id + '"')  # no closing brace

    reloaded = IdentityRegistry(registry_path)  # must not raise

    assert reloaded.find_student(ATTRS_A) == student_id
    assert reloaded.is_linked("partner.acme", student_id) is True
    captured = capsys.readouterr()
    assert "discarding truncated trailing line" in captured.err


# --- on-disk placement and permissions ------------------------------------


def test_registry_file_permissions_are_owner_only(registry: IdentityRegistry) -> None:
    registry.register_student(ATTRS_A, consumer_id="partner.acme")
    assert registry.registry_path.is_file()
    file_mode = stat.S_IMODE(registry.registry_path.stat().st_mode)
    assert file_mode == 0o600
    dir_mode = stat.S_IMODE(registry.registry_path.parent.stat().st_mode)
    assert dir_mode == 0o700


def test_registry_path_is_sibling_not_child_of_logs_dir(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    registry_path = tmp_path / "identity" / "registry.jsonl"
    registry = IdentityRegistry(registry_path)
    registry.register_student(ATTRS_A, consumer_id="partner.acme")

    # Not inside logs_dir's tree at all.
    assert logs_dir not in registry.registry_path.parents
    assert registry.registry_path.parent != logs_dir
    # A directory walk over logs_dir (the shape attempt-log tooling uses)
    # never encounters the registry file.
    walked = {p for p in logs_dir.rglob("*")}
    assert registry.registry_path not in walked


def test_registry_events_are_valid_jsonl(registry: IdentityRegistry) -> None:
    registry.register_student(ATTRS_A, consumer_id="partner.acme")
    lines = registry.registry_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # student_registered + auto consumer_linked
    for line in lines:
        event = json.loads(line)
        assert event["event_kind"] in ("student_registered", "consumer_linked")
        assert "student_id" in event
        assert "timestamp" in event


# --- ServingFacade pass-throughs -------------------------------------------


def test_facade_without_identity_registry_raises_clear_error(tmp_path: Path) -> None:
    from orchestrator import engine

    content = engine.load_content(Path(__file__).resolve().parent.parent / "content" / "fractions")
    facade = ServingFacade(banks_dir=None, logs_dir=None, content=content)
    with pytest.raises(NoIdentityRegistryError):
        facade.register_student(ATTRS_A, consumer_id="partner.acme")
    with pytest.raises(NoIdentityRegistryError):
        facade.find_student(ATTRS_A)
    with pytest.raises(NoIdentityRegistryError):
        facade.is_linked("partner.acme", "stu-anything")
    with pytest.raises(NoIdentityRegistryError):
        facade.link_student("stu-anything", ATTRS_A, consumer_id="partner.acme")


def test_facade_delegates_to_configured_identity_registry(tmp_path: Path, registry: IdentityRegistry) -> None:
    from orchestrator import engine

    content = engine.load_content(Path(__file__).resolve().parent.parent / "content" / "fractions")
    facade = ServingFacade(banks_dir=None, logs_dir=None, content=content, identity=registry)

    student_id = facade.register_student(ATTRS_A, consumer_id="partner.acme")
    assert facade.is_linked("partner.acme", student_id) is True
    assert facade.is_linked("partner.school", student_id) is False
    assert facade.find_student(ATTRS_A) == student_id

    facade.link_student(student_id, ATTRS_A, consumer_id="partner.school", link_basis="guardian_authorized")
    assert facade.is_linked("partner.school", student_id) is True

    with pytest.raises(AttributeMismatchError):
        facade.link_student(student_id, ATTRS_B, consumer_id="partner.other")
