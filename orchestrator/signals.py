"""KTD7: one closed error-signal taxonomy, shared by both current and future
transports (MCP today; REST in a later unit).

This is genuinely the one definition site for each of the ten signal names
below. Every raise/return site elsewhere in this codebase that needs to name
one of these signals imports this module and references the matching
`SIGNAL_*`-style constant rather than hardcoding the string a second time —
a grep for any of these literal strings (e.g. `"session_conflict"`) should
find it defined here exactly once, with every other occurrence a reference
to the constant (`signals.SESSION_CONFLICT`), not a redefinition.

Before this unit, several of these already existed as ad hoc string literals
scattered across `tools/mcp_server.py` (e.g. `f"not_linked: {exc}"` repeated
at every `ConsumerNotLinkedError` catch site) and as separately-named
exception classes in `orchestrator/serving.py` whose messages happened to
embed the signal name inline (`SessionConflictError`, `SessionUnknownError`,
`SessionExpiredError`). This module does not change any of those exception
classes' behavior or semantics — it only gives the string each one embeds a
single canonical source.

Plain module-level string constants (mirroring `orchestrator/state.py`'s
`STATUS_*` pattern) rather than an Enum: every current call site wants the
bare string itself (embedded in an f-string message, or as a dataclass
field's value), not an Enum member that would need `.value` unwrapped at
every one of those sites.
"""

from __future__ import annotations

# --- Pool / candidate-selection signals (R13) -------------------------------

#: The requested skill_id isn't in the loaded content graph at all.
UNKNOWN_SKILL = "unknown_skill"

#: A known skill_id, but the pinned bank has no instances for it.
EMPTY_POOL = "empty_pool"

#: Every instance in the pool has already been seen; one least-recently-seen
#: repeat is returned in its place (R1, KD5).
EXHAUSTED = "exhausted"

#: A refill's freshly planned batch came back with zero new items — the pool
#: for this skill (seen ∪ delivered) is fully consumed. Distinct from
#: `EXHAUSTED`: this is a batch-planning outcome (U9/U10), not a single-item
#: candidate read (`get_candidates`).
NO_FURTHER_ITEMS = "no_further_items"

# --- Session lifecycle signals (KTD4, R20/AE9) ------------------------------

#: `open_session` found a live session for this student already open under a
#: *different* consumer (R20/AE9).
SESSION_CONFLICT = "session_conflict"

#: A session_id this registry has no record of ever issuing.
SESSION_UNKNOWN = "session_unknown"

#: A session_id this registry issued and remembers idle-evicting via TTL.
SESSION_EXPIRED = "session_expired"

# --- Identity / linking signal (R17) ----------------------------------------

#: The calling consumer has no link to the named student.
NOT_LINKED = "not_linked"

# --- Ingest signals (KTD5) ---------------------------------------------------

#: An attempt failed KTD5 ingest validation; a structured reason accompanies
#: this signal (`IngestReport.rejection_reason` / `SubmitOutcome.rejection_reason`).
INGEST_REJECTED = "ingest_rejected"

#: An attempt was accepted by the §3 abandonment guard's check but
#: deliberately not logged; a reason string accompanies this signal
#: (`SubmitOutcome.discard_reason`).
DISCARDED = "discarded"

#: All ten signals, for the "defined in exactly one place" self-check
#: (tests/test_serving.py's taxonomy-consolidation test).
ALL = (
    UNKNOWN_SKILL,
    EMPTY_POOL,
    EXHAUSTED,
    NO_FURTHER_ITEMS,
    SESSION_CONFLICT,
    SESSION_UNKNOWN,
    SESSION_EXPIRED,
    NOT_LINKED,
    INGEST_REJECTED,
    DISCARDED,
)
