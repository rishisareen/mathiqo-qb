"""Student identity registry (U4): Mathiqo-issued opaque student ids,
registration attributes, and attribute-proven per-consumer links, derived
by replaying an append-only JSONL registry log (KTD11).

**Why a registry separate from the attempt log (KD9, R16).** Lookup
attributes (guardian email, family code, school reference) must never
reach the serving path or the attempt log — only the opaque `student_id`
may. This module is the one place those attributes are held at rest, in
their own file, deliberately placed as a *sibling* of the per-student
attempt-logs directory (not a child of it) so any tooling that walks the
attempt-logs tree (backup, replay audit, partner export) never encounters
it by accident. See `orchestrator/serving.py`'s docstring for the logs
directory's own layout (`{logs_dir}/{student_id}.jsonl`); a caller wiring
this module up should point `registry_path` at `logs_dir.parent /
"identity" / "registry.jsonl"` — a directory next to `logs_dir`, not
inside it.

**Two event kinds, replay-derived state (KTD11).** `student_registered`
(student_id, attributes, registering consumer_id, timestamp) and
`consumer_linked` (student_id, consumer_id, optional link_basis,
timestamp) are the only two events ever appended. State — which
attributes belong to which student, and which consumers are linked to
which student — is never mutated in place; on every load it is derived by
replaying the file from the top, mirroring `orchestrator/state.py`'s
`StudentModelStore` / `orchestrator/log.py`'s attempt-log replay pattern:
a missing file is simply empty state (a fresh deployment), never an
error.

**Atomic-append discipline.** Matches `orchestrator/log.py`'s
`append_attempt`: one `f.write` call of a single JSON line per event, file
opened in append mode — no read-modify-write of the file, so a partial
write (process killed mid-append) can only ever corrupt the last line,
never earlier content, exactly like the attempt log.

**File permissions.** Because this file is the one place lookup
attributes live at rest, both the registry file (0600) and its
containing directory (0700, only when this module is the one that
creates it) are locked to owner-read/write immediately after creation —
least-privilege for a file that exists specifically to keep PII out of
every other path in the system.

**`link_basis` (KTD11).** `consumer_linked` carries an optional
caller-supplied `link_basis` string (e.g. `"guardian_authorized"`),
recorded verbatim and unverified in v1 — a consent-provenance field the
deferred consent UX can populate later without an event-shape change.
`register_student`'s auto-link for the registering consumer always passes
`link_basis=None`: registration itself is the provenance for that first
link.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

STUDENT_REGISTERED = "student_registered"
CONSUMER_LINKED = "consumer_linked"

# Owner-only: this file/directory holds the lookup attributes KD9/R16 keeps
# out of every other path (serving, attempt logs).
_FILE_MODE = 0o600
_DIR_MODE = 0o700


class IdentityError(Exception):
    """Base class for this module's own domain errors."""


class UnknownStudentError(IdentityError):
    """`link_student` (or any lookup) named a student_id this registry has
    no `student_registered` event for."""


class AttributeMismatchError(IdentityError):
    """`link_student` was called with attributes that do not exactly match
    the student's registered attributes (R17: linking is attribute-proven,
    not id-alone) — no fuzzy/partial matching, ever."""


class IdentityRegistry:
    """Dict-backed, replay-derived repository for v1 — no database, mirrors
    `orchestrator/state.py`'s `StudentModelStore` shape: an in-memory index
    folded from an append-only JSONL log, reconstructible from the file
    alone on every process start."""

    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        # student_id -> registered attributes dict.
        self._attributes: Dict[str, dict] = {}
        # student_id -> {consumer_id: link_basis or None}.
        self._links: Dict[str, Dict[str, Optional[str]]] = {}
        self._load()

    # -- replay ---------------------------------------------------------

    def _load(self) -> None:
        """Fold every event in `registry_path` into the in-memory index, in
        file order. A missing file is empty state, not an error — mirrors
        `orchestrator/log.py`'s `iter_events` treatment of a fresh
        student's absent attempt log."""
        if not self.registry_path.exists():
            return
        with self.registry_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                self._apply(json.loads(line))

    def _apply(self, event: dict) -> None:
        kind = event["event_kind"]
        if kind == STUDENT_REGISTERED:
            self._attributes[event["student_id"]] = event["attributes"]
            self._links.setdefault(event["student_id"], {})
        elif kind == CONSUMER_LINKED:
            self._links.setdefault(event["student_id"], {})[event["consumer_id"]] = event.get("link_basis")
        else:
            raise IdentityError(f"{self.registry_path}: unknown event_kind {kind!r}")

    # -- append -----------------------------------------------------------

    def _append(self, event: dict) -> None:
        """One JSON line, one `f.write` call, append mode — see the module
        docstring's atomic-append discipline. Sets restrictive permissions
        immediately after this call is the one that creates the directory
        or the file, never on an append to an already-existing one."""
        parent = self.registry_path.parent
        created_dir = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if created_dir:
            os.chmod(parent, _DIR_MODE)

        created_file = not self.registry_path.exists()
        with self.registry_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True))
            f.write("\n")
        if created_file:
            os.chmod(self.registry_path, _FILE_MODE)

        self._apply(event)

    # -- operations ---------------------------------------------------------

    def register_student(self, attributes: dict, consumer_id: str) -> str:
        """Mint a new opaque student_id (`stu-{uuid4().hex}`, the same
        cryptographically-random pattern `tools/mcp_server.py` uses for
        `sess-{uuid4().hex}`), append `student_registered`, then
        auto-append a `consumer_linked` event for `consumer_id` with no
        `link_basis` (registration itself is that link's provenance).
        Returns only the opaque id — never the attributes dict, which
        exists solely inside the registry file from this point on."""
        student_id = f"stu-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        self._append(
            {
                "event_kind": STUDENT_REGISTERED,
                "student_id": student_id,
                "attributes": attributes,
                "consumer_id": consumer_id,
                "timestamp": now,
            }
        )
        self._append(
            {
                "event_kind": CONSUMER_LINKED,
                "student_id": student_id,
                "consumer_id": consumer_id,
                "link_basis": None,
                "timestamp": now,
            }
        )
        return student_id

    def link_student(
        self,
        student_id: str,
        attributes: dict,
        consumer_id: str,
        link_basis: Optional[str] = None,
    ) -> None:
        """Link `consumer_id` to `student_id` — refused unless `attributes`
        exactly matches (dict `==`, the same equality `find_student` uses;
        no fuzzy matching, R17) the attributes `student_id` was registered
        with. Raises `UnknownStudentError` for an unregistered id,
        `AttributeMismatchError` for a mismatch; appends `consumer_linked`
        with the given `link_basis` on success."""
        registered = self._attributes.get(student_id)
        if registered is None:
            raise UnknownStudentError(f"student_id {student_id!r} is not registered")
        if registered != attributes:
            raise AttributeMismatchError(
                f"attributes for {student_id!r} do not match the registered attributes"
            )
        self._append(
            {
                "event_kind": CONSUMER_LINKED,
                "student_id": student_id,
                "consumer_id": consumer_id,
                "link_basis": link_basis,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def find_student(self, attributes: dict) -> Optional[str]:
        """Exact-match lookup only (dict `==`) — a near-miss (one differing
        field, extra/missing key) never matches. Returns the student_id or
        None."""
        for student_id, registered in self._attributes.items():
            if registered == attributes:
                return student_id
        return None

    def is_linked(self, consumer_id: str, student_id: str) -> bool:
        return consumer_id in self._links.get(student_id, {})
