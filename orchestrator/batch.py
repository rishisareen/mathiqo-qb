"""U9: the ladder batch planner (R8-R11, R18, KD1, KD2, KTD5, KTD8).

Pure planning functions over explicit arguments (store, engine_state,
content, bank, ...) — mirroring `orchestrator/catalog.py`'s shape (U7): the
facade (`ServingFacade.fetch_batch`, orchestrator/serving.py) is a thin
orchestration layer that resolves a live `Session` down to these plain
values, calls in here, then folds the one piece of state this unit actually
mutates (`Session.delivered`) back in. Nothing here touches a `Session` or a
`ServingFacade` directly.

**What a batch is (KD1).** A server-planned, difficulty-laddered working set
for one (student, skill): a handful of "target" rung items (ordinary
continued practice at this skill's current theta-targeted depth), a smaller
number of "harder" rung items (pushed toward this skill's serving ceiling),
and a smaller number of "easier" rung items (R13-style depth step-downs,
pre-materialized so a wrong target-rung answer never needs a round-trip to
get its fallback item, R9). batch_size=1 degrades this to exactly the single
item ordinary frontier serving would pick right now — no ladder structure,
the same `plan_ladder` function, not a separate path (KD1, AE4).

**The context-assignment rule (the load-bearing part of this module).**
`orchestrator/engine.py`'s `consume_event` resolves a queued `StepDownProbe`
only when the attempt's own `context == "probe"` *and* the queue head's
`skill_id` matches. A batch's "target"/"harder" rung items are ordinary
practice (`context="practice"`): a correct answer never triggers diagnosis
regardless of context, so this is always safe. But if a "target" item is
answered wrong and untagged, `consume_event`'s fresh-diagnosis fallthrough
queues a real `StepDownProbe` for that skill *unconditionally* — it does not
check `context` at all. If the client then locally branches (per KD2's
policy object, `on_incorrect_untagged: easier`, zero round-trip) to this
batch's pre-materialized "easier" rung item for the same skill, and that
item's `context` were also `"practice"`, and it's *also* wrong+untagged,
`consume_event` would queue a *second* `StepDownProbe` on top of the first
— a real double-diagnosis bug. Tagging "easier"/step-down rung items with
`context="probe"` instead fixes this: submitting one now matches the queue
head `consume_event` just populated and resolves (pops) it, exactly as if a
server-issued probe had been served all along. See `tests/test_batch.py`'s
double-diagnosis test for this proven end-to-end against real
`fetch_batch`/`submit_attempt` calls, not just `consume_event` in isolation.

**KTD5's seen-union-delivered filter.** A batch's candidate pool excludes
`engine_state.seen_history[skill_id]` (durable — attempted) *and*
`Session.delivered` (in-memory only — delivered this session but not yet
attempted, R11). This module never writes `seen_history`; the caller
(`ServingFacade.fetch_batch`) is the one place `Session.delivered` gets
updated, after a successful plan.

**No repeats within one batch, ever (plan point e).** Every rung's picker
works off a single shrinking view of the same pre-filtered candidate pool
(`_available`, closed over by `_pick_target_or_harder`/the easier-rung loop)
— once an instance is picked for one rung it is immediately excluded from
every later pick in the same call, so a short pool yields a short batch
(`count_returned < count_requested`) rather than ever padding with a
repeat. This is deliberately *not* `engine.pick_instance`'s own contract:
that function's pool-exhaustion behavior is to fall back to a
least-recently-seen *repeat* (correct for live per-turn serving, R1) — the
wrong choice here, so this module calls it only through `_pick_unseen`,
which discards a repeat fallback as if nothing were found.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import constants
from . import engine
from .state import StudentModelStore


class BatchPlanningError(Exception):
    """AE8: a misconception referenced by a batch's own distractor has no
    child-facing feedback text in the registry. Raised, not silently
    skipped — see `build_feedback_sidecar`'s docstring."""


# ---------------------------------------------------------------------------
# Batch sizing (R8, KD1/AE4)
# ---------------------------------------------------------------------------


def clamp_batch_size(requested: int) -> int:
    """R8: a partner-tunable batch size between 1 and
    `constants.MAX_BATCH_SIZE`, silently clamped rather than rejected — the
    same posture `tools/bank_build.py`'s `--pool-cap` and this codebase's
    other server-enforced-cap knobs take."""
    return max(1, min(int(requested), constants.MAX_BATCH_SIZE))


def compute_mix(batch_size: int) -> Tuple[int, int, int]:
    """Scale the `BATCH_MIX_TARGET`/`_EASIER`/`_HARDER` (6/2/2 out of
    `DEFAULT_BATCH_SIZE`=10) ladder mix proportionally to any clamped
    `batch_size`. Returns `(target_n, easier_n, harder_n)`, always summing to
    `batch_size` exactly (never more — `plan_ladder` may still return fewer
    than this if the pool can't fill it, per plan point e).

    `batch_size <= 1` is KD1/AE4's degenerate per-turn case: exactly one
    target-rung pick, no ladder structure at all — the same `plan_ladder`
    function as every other batch_size, just handed a mix of `(1, 0, 0)`
    rather than a special-cased alternate code path.
    """
    if batch_size <= 1:
        return (1, 0, 0)
    target_n = round(batch_size * constants.BATCH_MIX_TARGET / constants.DEFAULT_BATCH_SIZE)
    easier_n = round(batch_size * constants.BATCH_MIX_EASIER / constants.DEFAULT_BATCH_SIZE)
    harder_n = batch_size - target_n - easier_n
    if harder_n < 0:
        # Only reachable for a very small clamped batch_size where rounding
        # both target_n and easier_n up overshoots batch_size itself (e.g.
        # batch_size=2 -> target round(1.2)=1, easier round(0.4)=0, already
        # fine; kept as a defensive floor rather than asserting it can't
        # happen for the actual constants in play).
        harder_n = 0
        target_n = batch_size - easier_n
    return target_n, easier_n, harder_n


# ---------------------------------------------------------------------------
# Ladder composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedItem:
    """One batch item: the full rendered instance dict verbatim (KD6 — stem,
    answer, misconception-tagged distractors, hints; nothing stripped),
    which ladder rung it fills, and the attempt-event `context` it must be
    submitted under (the context-assignment rule, see module docstring)."""

    instance: dict
    skill_id: str
    rung: str  # "target" / "easier" / "harder"
    context: str  # "practice" / "probe"


def _pick_unseen(pool: List[dict], target_depth: float) -> Optional[dict]:
    """Nearest-difficulty pick against `pool` with no seen items to filter
    (the caller already excluded everything ineligible — session seen ∪
    delivered ∪ already picked in this batch — from `pool` itself), so
    `engine.pick_instance`'s own seen-filter is a guaranteed no-op and its
    pool-exhaustion repeat-fallback branch can never fire: `pool` empty ->
    `pick_instance` returns `(None, None)` directly; `pool` non-empty ->
    every candidate is "unseen" (seen_history=[]) and the first branch
    always wins. Reuses `pick_instance`'s exact nearest-difficulty tie-break
    (module docstring's "no repeats, ever" note) rather than a hand-rolled
    equivalent."""
    instance, _warning = engine.pick_instance(pool, target_depth, [])
    return instance


def _pick_easier(pool: List[dict], skill_id: str, bank_version: str, current_depth: float) -> Optional[dict]:
    """R13 step-down via `engine._find_easier_instance`, restricted to
    `pool` (already excludes session seen ∪ delivered ∪ everything picked so
    far this batch) by wrapping it in a scratch single-skill `BankIndex` —
    that function's own signature takes a `BankIndex`, not a bare list, and
    this module deliberately reuses it rather than re-deriving "closest
    depth strictly below current_depth" a second time (module docstring).

    `by_id={}` rather than a real `item_instance_id -> instance` mapping (a
    code-review efficiency finding): `_find_easier_instance`, the only
    consumer of this scratch `BankIndex`, reads only `instances_by_skill`
    and never touches `by_id` — building the real mapping here would cost
    O(pool) work purely to satisfy the dataclass's required field, for a
    value nothing downstream ever reads."""
    scratch_bank = engine.BankIndex(
        bank_version=bank_version,
        instances_by_skill={skill_id: pool} if pool else {},
        by_id={},
    )
    return engine._find_easier_instance(scratch_bank, skill_id, current_depth)  # noqa: SLF001


def plan_ladder(
    store: StudentModelStore,
    engine_state: engine.EngineState,
    content: engine.ContentIndex,
    bank: engine.BankIndex,
    student_id: str,
    skill_id: str,
    country: str,
    curriculum: str,
    grade: int,
    batch_size: int,
    already_delivered: Set[str],
) -> List[PlannedItem]:
    """The pure ladder-composition core (KTD2: the caller must pass a
    *scratch* `engine_state` — this function only reads it, never mutates
    or returns a new one, unlike `select_next`/`consume_event`'s own
    no-aliasing contract, because it has nothing to hand back: no seen-mark
    ever happens during planning, R11).

    Candidate pool (KTD5): `bank.instances_by_skill[skill_id]` minus
    `engine_state.seen_history[skill_id]` (durable — attempted) minus
    `already_delivered` (this session's in-memory delivered-set, R11 —
    delivered-but-unattempted items return to the pool once the session
    ends, since nothing about `already_delivered` is durable). Every pick
    made while composing the ladder is additionally excluded from every
    later pick in the same call (module docstring's "no repeats, ever").

    Order: target rung, then harder rung, then easier rung — this is also
    the policy object's `floor: serve_in_listed_order` fallback sequence
    for a future client that doesn't recognize a later policy_version
    (KTD8), so it's a genuinely servable plain sequence on its own, not
    just an arbitrary concatenation order.
    """
    pool = bank.instances_by_skill.get(skill_id, [])
    seen = set(engine_state.seen_history.get(skill_id, []))
    excluded = seen | set(already_delivered)
    available = [inst for inst in pool if inst["variant_signature"] not in excluded]

    theta = store.peek(student_id, skill_id).theta
    ceiling = engine.serving_ceiling(skill_id, country, curriculum, grade, content)
    target = engine.target_depth(theta, ceiling)

    target_n, easier_n, harder_n = compute_mix(batch_size)

    picked: List[PlannedItem] = []
    picked_signatures: Set[str] = set()

    def _remaining() -> List[dict]:
        return [inst for inst in available if inst["variant_signature"] not in picked_signatures]

    for _ in range(target_n):
        inst = _pick_unseen(_remaining(), target)
        if inst is None:
            break
        picked.append(PlannedItem(instance=inst, skill_id=skill_id, rung="target", context="practice"))
        picked_signatures.add(inst["variant_signature"])

    # "Harder-or-follow-up" rung (Planning Contract Assumptions): aimed at
    # this skill's own serving ceiling — the hardest depth its mapping
    # allows — rather than at target + a further offset, so it stays a
    # well-defined pick regardless of how many discrete difficulty tiers a
    # given skill's authored templates actually span (v1's fixture content
    # has at most two tiers per skill; when theta is already near the
    # ceiling, "harder" naturally draws from the same tier as "target" —
    # still distinct item instances, just not a strictly higher difficulty
    # value, a content-driven fact rather than a planner bug). context
    # stays "practice": a correct answer never triggers diagnosis regardless
    # of context, so this rung needs no probe tagging (module docstring).
    for _ in range(harder_n):
        inst = _pick_unseen(_remaining(), ceiling)
        if inst is None:
            break
        picked.append(PlannedItem(instance=inst, skill_id=skill_id, rung="harder", context="practice"))
        picked_signatures.add(inst["variant_signature"])

    # Easier/step-down rung: one step-down per target-rung item actually
    # picked (up to easier_n), each computed from *that* target item's own
    # served depth via _find_easier_instance — context="probe" is the fix
    # for the double-diagnosis risk (module docstring).
    target_items = [item for item in picked if item.rung == "target"]
    for i in range(min(easier_n, len(target_items))):
        current_depth = target_items[i].instance["difficulty"]
        inst = _pick_easier(_remaining(), skill_id, bank.bank_version, current_depth)
        if inst is None:
            break
        picked.append(PlannedItem(instance=inst, skill_id=skill_id, rung="easier", context="probe"))
        picked_signatures.add(inst["variant_signature"])

    return picked


# ---------------------------------------------------------------------------
# Feedback sidecar (R18, AE8)
# ---------------------------------------------------------------------------

FEEDBACK_LOCALE = "en-IN"  # matches the convention every content pack authors under (see content/*/misconceptions.yaml)


def build_feedback_sidecar(
    content: engine.ContentIndex, items: List[PlannedItem], locale: str = FEEDBACK_LOCALE
) -> Dict[str, str]:
    """R18/AE8: every `misconception_id` reachable from any served
    distractor of any item in `items` must resolve to child-facing feedback
    text — unconditionally, so this raises `BatchPlanningError` rather than
    skipping a misconception with no registry entry or no `locale` text (a
    real content-authoring gap, not something a caller should be able to
    silently ship to a child)."""
    sidecar: Dict[str, str] = {}
    for item in items:
        for distractor in item.instance.get("distractors", []):
            misconception_id = distractor.get("misconception_id")
            if misconception_id is None or misconception_id in sidecar:
                continue
            entry = content.misconceptions.get(misconception_id)
            text = entry.child_feedback_i18n.get(locale) if entry is not None else None
            if not text:
                raise BatchPlanningError(
                    f"misconception {misconception_id!r} (served by a distractor of instance "
                    f"{item.instance['item_instance_id']!r}) has no {locale!r} child_feedback_i18n "
                    "text in the misconception registry — AE8 requires every served distractor's "
                    "misconception to resolve to child-facing feedback"
                )
            sidecar[misconception_id] = text
    return sidecar


# ---------------------------------------------------------------------------
# Branching policy object (KD2, KTD8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchPolicy:
    policy_version: str
    floor: str
    low_water: int
    rules: Dict[str, str]


def build_policy_v1() -> BatchPolicy:
    """KTD8: the fixed v1.0 branching-policy object every batch ships
    (small enough for a client to execute trivially, per KD2) — a pure
    function so a future v1.1 has an obvious second function to add next to
    this one rather than a mutated version of it."""
    return BatchPolicy(
        policy_version=constants.BATCH_POLICY_VERSION,
        floor="serve_in_listed_order",
        low_water=constants.BATCH_LOW_WATER,
        rules={
            "on_correct": "next_at_or_above",
            "on_incorrect_tagged": "show_feedback_then_easier",
            "on_incorrect_untagged": "easier",
            "on_buffer_low": "refill_in_background",
        },
    )


# ---------------------------------------------------------------------------
# The assembled result (R8-R11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    student_id: str
    skill_id: str
    items: List[PlannedItem]
    count_requested: int  # the clamped batch_size actually planned against (clamp_batch_size's output)
    count_returned: int  # len(items) -- may be < count_requested on a small pool (plan point e), never padded
    policy: BatchPolicy
    feedback_sidecar: Dict[str, str]
    bank_version: str
    content_version: str


def new_batch_id() -> str:
    """Mirrors the `sess-`/`stu-`/`evt-` id patterns already used across
    this codebase (orchestrator/serving.py, orchestrator/log.py)."""
    return f"batch-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Refill (U10: R12, KTD5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefillResult:
    """`ServingFacade.refill_batch`'s own response shape (orchestrator/
    serving.py) — a refill is just `fetch_batch` re-planned against the
    session's current live state (R12), but its response must distinguish
    two things a plain `BatchResult` can't carry on its own:

    - `batch` is `None` and `signal` is `signals.NO_FURTHER_ITEMS` when the
      freshly planned batch came back with zero new items (R13: an explicit
      signal, not a `BatchResult` with `count_returned=0` a caller could
      mistake for "nothing yet, try again" instead of "nothing is coming
      for this skill"). Otherwise `batch` is the planned `BatchResult` and
      `signal` is `None`.
    - `supersede` (KTD5) is set independently of whether new items were
      found — `True` when the plan for this skill materially changed since
      the client's last batch (skill now certified at/above its serving
      ceiling, or a remediation dose now queued for it) — see
      `ServingFacade._compute_supersede`'s docstring for the exact rule.
      A client sees this on every refill response, including a
      `no_further_items` one, since discarding a stale delivered-but-
      unattempted buffer is meaningful either way.
    """

    batch: Optional[BatchResult]
    signal: Optional[str]
    supersede: bool
