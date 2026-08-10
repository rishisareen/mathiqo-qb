"""The adaptive engine: pure selection policy over (student model, bank,
graph, mappings) — student-model doc §5 (placement), core design doc §2.5
(diagnosis), §5.2 ("Adaptive engine ... Pure decision logic; no UI"), §5.4
(seen filter / pool exhaustion). Unit U5.

This module never does I/O beyond the one-time content/bank *loading*
helpers (`load_content`, `load_bank`) — loading a directory into an
in-memory index is a prerequisite for pure decision logic to run against,
not the decision logic itself. Everything downstream of a loaded
`ContentIndex` / `BankIndex` is pure functions over explicit arguments,
including an explicit `now` (KTD10 — no internal clock reads).

**Two kinds of state, one deliberately not here.** `SkillState` (theta, n,
certification — orchestrator/state.py, U1-U4) is the *student model*; this
module never constructs or mutates it directly except for the one narrow
exception KTD12 requires (see `consume_event`'s docstring). What lives here
is `EngineState` (KTD11): the *engine's own* derived state — the per-student
seen filter and the pending remediation/step-down/prerequisite-probe queues
and placement cursors. None of that belongs on `SkillState` (it isn't
mastery evidence), but it still must be re-derivable from the attempt log
alone, same as the student model is — `rebuild_engine_state` is that replay.

**The feedback interface (point 5 of the plan's Approach).** The engine
does not administer items — a caller (a session runner, U6) does that and
reports back what happened via two calls, always in this order:
  1. Apply the ordinary student-model update (`update.apply_attempt`, and
     `certification.maybe_certify` for single-skill non-review attempts,
     exactly as orchestrator/log.py's `_apply_event` composes them) and
     write the result into the `StudentModelStore`.
  2. Call `consume_event(engine_state, store, content, bank, outcome, now)`
     with an `AttemptOutcome` describing the same attempt. It reads the
     *already-updated* SkillState back out of `store` (so it can see the
     misconception count exactly as `record_attempt` just left it) and
     returns a new `EngineState` plus any `SkillState` patches the caller
     must additionally write back (only ever a misconception-counter reset,
     KTD12).
Live serving and log replay both funnel through this same `consume_event`
call — that is what KTD11 means by "the same code path."

Wrong-answer-to-distractor matching (`match_distractor`) lives here, not in
a grading module, because it is the one piece of diagnosis logic (core doc
§2.5's TAG node) that both this engine's own `consume_event` and U6's
response grading need identically — a shared helper avoids two
implementations of "what does this wrong answer match" (plan point 5).
"""

from __future__ import annotations

import copy
import functools
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from . import constants
from . import log as log_module
from .certification import due_reviews
from .state import SkillState, StudentModelStore, is_certified

# ---------------------------------------------------------------------------
# Content index: skill graph + misconception registry + country mappings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillNode:
    skill_id: str
    strand: str
    canonical_stage: int
    prerequisites: Tuple[str, ...]
    misconceptions: Tuple[str, ...]


@dataclass(frozen=True)
class MisconceptionEntry:
    misconception_id: str
    remediation_target: str
    # U9 (R18/AE8, KD10): child-facing instant-feedback text, keyed by
    # language (e.g. "en-IN") — schemas/misconception.schema.json's
    # child_feedback_i18n, authored in U3 but never previously carried onto
    # this dataclass (only remediation_target was, since nothing before U9
    # needed the feedback text itself, only the remediation skill it points
    # to). Defaulted to an empty dict so any earlier direct construction of
    # this frozen dataclass elsewhere keeps working unchanged (mirrors the
    # "new field defaults so existing construction sites don't break"
    # discipline `Session`'s own new fields already follow).
    child_feedback_i18n: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MappingRow:
    skill_id: str
    country: str
    curriculum: str
    grade: int
    depth_expectation: str
    learning_objective: Optional[str] = None
    terminology_overrides: Dict[str, str] = field(default_factory=dict)
    unit_code: Optional[str] = None  # KTD10: absent for curricula with no chapter granularity (Ontario)
    unit_name: Optional[str] = None


@dataclass(frozen=True)
class ContentIndex:
    """Everything the engine reads from content/<slice>/*.yaml, indexed for
    lookup. Loaded once per process/session — not re-parsed per decision."""

    content_version: str  # KTD9: from the pack's manifest.yaml; stamped on catalog/graph/recommendation responses
    skills: Dict[str, SkillNode]
    dependents: Dict[str, List[str]]  # skill_id -> sorted dependent skill_ids
    misconceptions: Dict[str, MisconceptionEntry]
    mappings: Dict[Tuple[str, str, str, int], MappingRow]  # (skill_id, country, curriculum, grade)
    mapped_skills_by_country_curriculum_grade: Dict[Tuple[str, str, int], List[str]]  # sorted


MANIFEST_FILENAME = "manifest.yaml"


def _load_manifest(content_dir: Path) -> str:
    """KTD9: every content pack declares its `content_version` in a
    manifest.yaml sitting alongside the pack's skills/misconceptions/mappings
    files. Content is assumed already graph_lint-clean (this loader doesn't
    re-validate — see load_content's docstring), but a missing/malformed
    manifest still fails loudly here rather than silently shipping an
    un-versioned ContentIndex, since graph_lint is a separate, skippable
    step from actually loading a pack."""
    manifest_path = content_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{content_dir}: missing {MANIFEST_FILENAME} (every content pack must declare content_version)"
        )
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    content_version = data.get("content_version") if isinstance(data, dict) else None
    if not content_version:
        raise ValueError(f"{manifest_path}: missing required 'content_version' field")
    return str(content_version)


def _load_yaml_list(content_dir: Path, key: str) -> List[dict]:
    """Merge one top-level YAML key (skills/misconceptions/mappings) across
    every *.yaml/*.yml file in content_dir — mirrors tools/graph_lint.py's
    load_content, but this module doesn't need graph_lint's linting, only
    the merge. manifest.yaml is excluded: it carries pack metadata (KTD9),
    not skills/misconceptions/mappings entries."""
    items: List[dict] = []
    paths = sorted(content_dir.glob("*.yaml")) + sorted(content_dir.glob("*.yml"))
    for path in paths:
        if path.name == MANIFEST_FILENAME:
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        value = data.get(key)
        if isinstance(value, list):
            items.extend(value)
    return items


def load_content(content_dir: Path) -> ContentIndex:
    """Load skills.yaml / misconceptions.yaml / mappings.yaml (real field
    names, not the design docs' illustrative snippets — see the module
    docstring) into a ContentIndex. Content is assumed already
    graph_lint-clean (references resolve, no cycles); this loader does not
    re-validate."""
    content_version = _load_manifest(content_dir)
    raw_skills = _load_yaml_list(content_dir, "skills")
    raw_misc = _load_yaml_list(content_dir, "misconceptions")
    raw_mappings = _load_yaml_list(content_dir, "mappings")

    skills: Dict[str, SkillNode] = {}
    for s in raw_skills:
        skills[s["skill_id"]] = SkillNode(
            skill_id=s["skill_id"],
            strand=s.get("strand", ""),
            canonical_stage=int(s.get("canonical_stage", 0)),
            prerequisites=tuple(s.get("prerequisites") or []),
            misconceptions=tuple(s.get("misconceptions") or []),
        )

    dependents: Dict[str, List[str]] = {skill_id: [] for skill_id in skills}
    for node in skills.values():
        for prereq in node.prerequisites:
            dependents.setdefault(prereq, []).append(node.skill_id)
    for skill_id in dependents:
        dependents[skill_id].sort()

    misconceptions: Dict[str, MisconceptionEntry] = {
        m["misconception_id"]: MisconceptionEntry(
            misconception_id=m["misconception_id"],
            remediation_target=m["remediation_target"],
            child_feedback_i18n=dict(m.get("child_feedback_i18n") or {}),
        )
        for m in raw_misc
    }

    mappings: Dict[Tuple[str, str, str, int], MappingRow] = {}
    mapped_skills_by_country_curriculum_grade: Dict[Tuple[str, str, int], List[str]] = {}
    for m in raw_mappings:
        row = MappingRow(
            skill_id=m["skill_id"],
            country=m["country"],
            curriculum=m["curriculum"],
            grade=int(m["grade"]),
            depth_expectation=m["depth_expectation"],
            learning_objective=m.get("learning_objective"),
            terminology_overrides=dict(m.get("terminology_overrides") or {}),
            unit_code=m.get("unit_code"),
            unit_name=m.get("unit_name"),
        )
        # Last-in-file wins on an exact (skill, country, curriculum, grade)
        # collision — graph_lint already flags true duplicate mapping rows
        # as an error, so this only matters for content that hasn't been
        # linted yet.
        mappings[(row.skill_id, row.country, row.curriculum, row.grade)] = row
        key = (row.country, row.curriculum, row.grade)
        bucket = mapped_skills_by_country_curriculum_grade.setdefault(key, [])
        if row.skill_id not in bucket:
            bucket.append(row.skill_id)
    for bucket in mapped_skills_by_country_curriculum_grade.values():
        bucket.sort()

    return ContentIndex(
        content_version=content_version,
        skills=skills,
        dependents=dependents,
        misconceptions=misconceptions,
        mappings=mappings,
        mapped_skills_by_country_curriculum_grade=mapped_skills_by_country_curriculum_grade,
    )


# ---------------------------------------------------------------------------
# Bank index: a built banks/<version>/ directory (tools/bank_build.py output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BankIndex:
    bank_version: str
    instances_by_skill: Dict[str, List[dict]]
    by_id: Dict[str, dict]


def bank_pool_for_skill(bank: BankIndex, skill_id: str) -> List[dict]:
    """The raw, unfiltered instance pool `bank` holds for `skill_id` — no
    format or seen-status filtering. Shared by `rank_serve_candidates`'s
    remediation/stepdown/prereq_probe/placement branches (`not pool` is
    their `unservable` verdict) and `ServingFacade.list_skills`'s per-skill
    `servable` annotation: one predicate, two callers, so the two surfaces
    read the same raw-pool truth for every reason except `rank_serve_candidates`'s
    review branch (which layers an additional certified-format filter on
    top) and frontier (which uses a separately ceiling/mastery/prerequisite-
    filtered pool, never this raw one)."""
    return bank.instances_by_skill.get(skill_id, [])


def load_bank(bank_dir: Path) -> BankIndex:
    """Load one built bank (schemas/item-instance.schema.json shape,
    tools/bank_build.py's manifest.json + instances/<template_id>.json).

    Only single-skill instances (`len(skill_ids) == 1`) are indexed for
    serving: prerequisite gating and nearest-difficulty targeting are both
    defined per single skill (R12, KTD9), and v1's own template content has
    no multi-skill items to begin with (confirmed against the built dev
    bank) — a documented v1 scope limit, not an oversight, should
    multi-skill templates appear later.
    """
    manifest = json.loads((bank_dir / "manifest.json").read_text(encoding="utf-8"))
    instances_by_skill: Dict[str, List[dict]] = {}
    by_id: Dict[str, dict] = {}
    for entry in manifest["templates"]:
        template_id = entry["template_id"]
        instances = json.loads((bank_dir / "instances" / f"{template_id}.json").read_text(encoding="utf-8"))
        for instance in instances:
            by_id[instance["item_instance_id"]] = instance
            if len(instance["skill_ids"]) != 1:
                continue
            skill_id = instance["skill_ids"][0]
            instances_by_skill.setdefault(skill_id, []).append(instance)
    for pool in instances_by_skill.values():
        pool.sort(key=lambda inst: (inst["difficulty"], inst["item_instance_id"]))
    return BankIndex(
        bank_version=manifest["bank_version"],
        instances_by_skill=instances_by_skill,
        by_id=by_id,
    )


# ---------------------------------------------------------------------------
# KTD7: bank lookup by bank_version — any retained build, not just ACTIVE
# ---------------------------------------------------------------------------

# Resolves a bank_version to its BankIndex. Distinct from KTD3's ACTIVE
# pointer (which only answers "what do new sessions pin?"): replay (and,
# from U5, ingest) must resolve *each event* against its own stamped build,
# since a log can span builds and the diagnosis path consults the bank per
# event (`_find_easier_instance`) — a cross-build log would otherwise replay
# decisions the live session never made.
BankLookup = Callable[[str], BankIndex]


def make_bank_lookup(banks_dir: Path, cache_size: int = 8) -> BankLookup:
    """KTD7's disk-backed lookup: loads `banks_dir/<bank_version>/` on first
    use, LRU-cached (small — replay/ingest touch at most a handful of
    distinct builds in practice)."""

    @functools.lru_cache(maxsize=cache_size)
    def lookup(bank_version: str) -> BankIndex:
        return load_bank(banks_dir / bank_version)

    return lookup


def make_static_bank_lookup(*banks: BankIndex) -> BankLookup:
    """A bank_lookup over already-loaded BankIndex objects, keyed by their
    own `bank_version` — for callers (a live session holding its one pinned
    bank; tests) that already have every build the log could reference in
    memory and don't need `make_bank_lookup`'s disk I/O."""
    by_version = {bank.bank_version: bank for bank in banks}

    def lookup(bank_version: str) -> BankIndex:
        return by_version[bank_version]

    return lookup


# ---------------------------------------------------------------------------
# Depth targeting: tier conversion, placement prior, serving ceiling (KTD9)
# ---------------------------------------------------------------------------


def placement_prior_depth(depth_expectation: Optional[str]) -> float:
    """SM §5 point 1 + KTD9: one tier below `depth_expectation`, floored at
    the easy band rather than underflowing the scale. `None` (no mapping
    row for this skill) uses the easy-tier default."""
    if depth_expectation is None:
        return constants.TIER_DEPTH["easy"]
    idx = constants.TIER_ORDER.index(depth_expectation)
    below = constants.TIER_ORDER[max(idx - 1, 0)]
    return constants.TIER_DEPTH[below]


def serving_ceiling(skill_id: str, country: str, curriculum: str, grade: int, content: ContentIndex) -> float:
    """KTD9's serving ceiling: the mapping's depth_expectation converted via
    TIER_DEPTH where a mapping row exists for this exact (skill, country,
    curriculum, grade); the top tier otherwise (core doc §1.4's "default
    ceiling", not the doc's separate self-extending-band mechanic, which
    this v1 engine does not implement — see the module report for that
    scope call)."""
    row = content.mappings.get((skill_id, country, curriculum, grade))
    if row is None:
        return constants.TIER_DEPTH["hard"]
    return constants.TIER_DEPTH[row.depth_expectation]


def target_depth(theta: float, ceiling: float) -> float:
    """KTD9: nearest-difficulty targeting aims at theta + CHALLENGE_OFFSET,
    capped by the serving ceiling."""
    return min(theta + constants.CHALLENGE_OFFSET, ceiling)


def starting_depth_for(skill_id: str, country: str, curriculum: str, grade: int, content: ContentIndex) -> float:
    """The depth to start probing a skill placement/prerequisite-probing
    first encounters — the mapping's one-tier-below prior (SM §5 point 1)
    when a mapping row exists for this exact (skill, country, curriculum,
    grade), the easy-tier default otherwise. Applied uniformly to every
    skill placement walks onto (not just a strand's very first skill) — a
    deliberate generalization of SM §5 point 1 documented in the module
    report."""
    row = content.mappings.get((skill_id, country, curriculum, grade))
    return placement_prior_depth(row.depth_expectation if row is not None else None)


# ---------------------------------------------------------------------------
# Instance selection: nearest-difficulty + seen filter + LRU fallback (§5.4)
# ---------------------------------------------------------------------------


def pick_instance(
    candidates: List[dict], target: float, seen_history: List[str]
) -> Tuple[Optional[dict], Optional[str]]:
    """KTD9 nearest-difficulty targeting plus core doc §5.4's seen filter:
    never repeat an instance while alternatives exist. Returns
    (instance_or_None, warning_or_None) — the warning fires exactly when the
    pool is exhausted and a least-recently-seen repeat is served instead
    (§5.4's pool-utilization signal; documented here as a returned
    structured value rather than a stderr print, so a headless caller/test
    can assert on it directly).

    KTD4: `seen_history` is a list of `variant_signature`s, not instance ids
    — "seen" means "this content", which survives a bank rebuild that
    re-samples the same variant under a new instance id (AE3)."""
    if not candidates:
        return None, None
    seen = set(seen_history)
    unseen = [c for c in candidates if c["variant_signature"] not in seen]
    if unseen:
        best = min(unseen, key=lambda c: (abs(c["difficulty"] - target), c["item_instance_id"]))
        return best, None

    # Pool exhausted: least-recently-seen fallback. last_seen_at maps each
    # variant signature to the index of its most recent appearance in
    # seen_history — the smallest such index among candidates is the one
    # that has gone the longest without being served again.
    last_seen_at: Dict[str, int] = {}
    for idx, signature in enumerate(seen_history):
        last_seen_at[signature] = idx
    ranked = sorted(
        candidates,
        key=lambda c: (
            last_seen_at.get(c["variant_signature"], -1),
            abs(c["difficulty"] - target),
            c["item_instance_id"],
        ),
    )
    warning = (
        f"pool exhausted near depth {target:.3f}: repeating least-recently-seen "
        f"instance {ranked[0]['item_instance_id']!r}"
    )
    return ranked[0], warning


# ---------------------------------------------------------------------------
# Wrong-answer-to-distractor matching (shared with U6's grading, see module
# docstring)
# ---------------------------------------------------------------------------


def match_distractor(instance: dict, response_value: Optional[str]) -> Optional[str]:
    """core doc §2.5's TAG node: does this wrong response match a stored
    distractor, and if so, which misconception does it tag? Matching is
    against the instance's stored `distractors[].value` regardless of
    question_type (R13) — exact string equality only. A response matching a
    distractor whose own `misconception_id` is null, or matching no
    distractor at all, both return None: both are "untagged" for diagnosis
    purposes (§2.5) even though only the second is literally "no match".
    Normalizing differently-formatted-but-equivalent responses (e.g. "2/4"
    vs "1/2") is response-parsing, owned by the caller (U6), not this
    lookup — see the module report for this call.
    """
    if response_value is None:
        return None
    for distractor in instance.get("distractors", []):
        if distractor["value"] == response_value:
            return distractor.get("misconception_id")
    return None


# ---------------------------------------------------------------------------
# Engine-derived state (KTD11) — queues and the seen filter
# ---------------------------------------------------------------------------


@dataclass
class RemediationDose:
    """KTD12: a bounded dose of practice on the misconception's mapped
    prerequisite, queued once and counted down; never re-derived from the
    (now-reset) misconception counter while it's active."""

    skill_id: str  # the remediation_target being practiced
    trigger_skill_id: str  # the skill whose counter fired
    misconception_id: str
    remaining: int = constants.REMEDIATION_DOSE_SIZE


@dataclass
class StepDownProbe:
    """R13: a single easier-depth item from the same skill's template
    family, queued after an untagged deep failure."""

    skill_id: str
    target_depth: float


@dataclass
class PrereqProbe:
    """R13/SM §5: a single diagnostic probe of one prerequisite, queued
    when a step-down probe isn't available or itself fails."""

    skill_id: str
    trigger_skill_id: str


@dataclass
class EngineState:
    """The engine's own persistent-but-log-derivable state (KTD11) — never
    stored on SkillState. `seen_history[skill_id]` is every variant_signature
    served for that skill (KTD4 — content-based identity, not instance id, so
    it survives a bank rebuild that re-samples the same variant under a new
    instance id, AE3), in serve order (may contain repeats once the pool is
    exhausted); `placement_cursor[strand]` is the skill currently
    being probed for that strand's placement walk (absent once that
    strand's frontier is found or placement never applied to it)."""

    seen_history: Dict[str, List[str]] = field(default_factory=dict)
    remediation_queue: List[RemediationDose] = field(default_factory=list)
    stepdown_queue: List[StepDownProbe] = field(default_factory=list)
    prereq_probe_queue: List[PrereqProbe] = field(default_factory=list)
    placement_cursor: Dict[str, str] = field(default_factory=dict)
    placement_initialized: bool = False


def _copy_engine_state(engine_state: EngineState) -> EngineState:
    """The no-aliasing copy `select_next`/`consume_event` return per their
    own "never mutates engine_state in place" contract — WITHOUT a plain
    `copy.deepcopy`'s cost. `seen_history` grows by one entry per attempt,
    unbounded, across a student's whole history; deep-copying the entire
    dict-of-lists on every single call re-copies every skill's full history
    every time, making replay (and per-item live serving, which calls this
    once per served item) quadratic in total attempt count — confirmed
    empirically during code review (500 -> 8000 events traced a clean
    quadratic curve).

    Only the `seen_history` dict's top-level structure is copied here —
    O(skills touched), not O(total attempts); the per-skill lists inside it
    stay shared with `engine_state`'s own lists until `_mark_served`
    actually appends to one, at which point it replaces just that one list
    (copy-on-write), never mutates a shared one in place. The queue/cursor
    fields are small and bounded (a handful of entries at most), so a plain
    deep copy of those is cheap and not worth the same treatment.
    """
    return EngineState(
        seen_history=dict(engine_state.seen_history),
        remediation_queue=copy.deepcopy(engine_state.remediation_queue),
        stepdown_queue=copy.deepcopy(engine_state.stepdown_queue),
        prereq_probe_queue=copy.deepcopy(engine_state.prereq_probe_queue),
        placement_cursor=dict(engine_state.placement_cursor),
        placement_initialized=engine_state.placement_initialized,
    )


def _init_placement(
    engine_state: EngineState, content: ContentIndex, country: str, curriculum: str, grade: int
) -> None:
    """Lazily seed one placement cursor per strand (SM §5 points 1-2) the
    first time selection is asked for this student. Starting skills are the
    skills with a mapping row at exactly (country, curriculum, grade) —
    "this grade's frontier skills" (SM §5 point 1) — one per strand, the
    lowest canonical_stage among that grade's mapped skills in the strand (a
    reading of "mid-chain start"; see the module report). A
    country/curriculum/grade with no mapping rows at all (not exercised by
    the shipped fixture, but possible) falls back to the graph's own roots,
    one per strand, using the default easy-tier prior — the per-skill
    "unmapped -> easy" default generalized to the starting-point choice
    itself."""
    if engine_state.placement_initialized:
        return
    mapped = content.mapped_skills_by_country_curriculum_grade.get((country, curriculum, grade), [])
    by_strand: Dict[str, List[str]] = {}
    for skill_id in mapped:
        strand = content.skills[skill_id].strand
        by_strand.setdefault(strand, []).append(skill_id)
    if not by_strand:
        roots = sorted(sid for sid, node in content.skills.items() if not node.prerequisites)
        for skill_id in roots:
            strand = content.skills[skill_id].strand
            by_strand.setdefault(strand, []).append(skill_id)
    for strand, skill_ids in by_strand.items():
        start = min(skill_ids, key=lambda sid: (content.skills[sid].canonical_stage, sid))
        engine_state.placement_cursor[strand] = start
    engine_state.placement_initialized = True


def _first_uncertified(skill_ids: List[str], store: StudentModelStore, student_id: str) -> Optional[str]:
    for skill_id in skill_ids:
        if store.peek(student_id, skill_id).certified_depth is None:
            return skill_id
    return None


def _queue_prereq_probes(engine_state: EngineState, content: ContentIndex, skill_id: str) -> None:
    """R13's prerequisite-walk fallback: probe every prerequisite not
    already queued (dedup by skill_id — a skill implicated by two different
    triggers only needs probing once)."""
    already = {p.skill_id for p in engine_state.prereq_probe_queue}
    for prereq in sorted(content.skills[skill_id].prerequisites):
        if prereq not in already:
            engine_state.prereq_probe_queue.append(PrereqProbe(skill_id=prereq, trigger_skill_id=skill_id))
            already.add(prereq)


def _find_easier_instance(bank: BankIndex, skill_id: str, current_depth: float) -> Optional[dict]:
    """R13 step-down: the same skill's ("template family's" — see module
    report) instances at the closest depth strictly below current_depth."""
    pool = bank.instances_by_skill.get(skill_id, [])
    easier_depths = {inst["difficulty"] for inst in pool if inst["difficulty"] < current_depth}
    if not easier_depths:
        return None
    step_depth = max(easier_depths)
    at_depth = [inst for inst in pool if inst["difficulty"] == step_depth]
    return min(at_depth, key=lambda inst: inst["item_instance_id"])


def _advance_placement(
    engine_state: EngineState,
    store: StudentModelStore,
    content: ContentIndex,
    student_id: str,
    skill_id: str,
    correct: bool,
    depth_served: float,
    now: datetime,
) -> None:
    """SM §5 points 2-3, run after the student-model update for a
    `context == "placement"` attempt: check the frontier stop condition,
    else walk pass -> dependents / fail -> prerequisites."""
    strand = content.skills[skill_id].strand
    if engine_state.placement_cursor.get(strand) != skill_id:
        return  # stale event for an already-advanced or already-finished cursor

    state = store.peek(student_id, skill_id)
    prereqs = content.skills[skill_id].prerequisites
    prereqs_certified = all(is_certified(store.peek(student_id, p), depth_served, now) for p in prereqs)
    is_frontier = state.certified_depth is None and prereqs_certified and state.n >= constants.PLACEMENT_FRONTIER_N
    if is_frontier:
        del engine_state.placement_cursor[strand]
        return

    if correct:
        next_skill = _first_uncertified(content.dependents.get(skill_id, []), store, student_id)
    else:
        next_skill = _first_uncertified(sorted(prereqs), store, student_id)

    if next_skill is None:
        del engine_state.placement_cursor[strand]  # dead end (leaf, or root exhausted): stop this strand's walk
    else:
        engine_state.placement_cursor[strand] = next_skill


# ---------------------------------------------------------------------------
# consume_event: KTD12/R13 diagnosis-queue effects + SM §5 placement advance
# ---------------------------------------------------------------------------


@dataclass
class AttemptOutcome:
    """The scoring-relevant subset of one attempt, shaped to match both a
    live caller's just-scored attempt and a replayed attempt-event row
    (`from_event`) — see the module docstring's feedback-interface section."""

    student_id: str
    skill_ids: List[str]
    item_instance_id: str
    variant_signature: str
    correct: bool
    misconception_id: Optional[str]
    context: str
    depth_served: Optional[float]


def attempt_outcome_from_event(event: dict) -> AttemptOutcome:
    return AttemptOutcome(
        student_id=event["student_id"],
        skill_ids=list(event["skill_ids"]),
        item_instance_id=event["item_instance_id"],
        variant_signature=event["variant_signature"],
        correct=event["correct"],
        misconception_id=event["misconception_id"],
        context=event["context"],
        depth_served=event["depth_served"],
    )


@dataclass
class ConsumeResult:
    engine_state: EngineState
    skill_state_patches: Dict[str, SkillState] = field(default_factory=dict)


def consume_event(
    engine_state: EngineState,
    store: StudentModelStore,
    content: ContentIndex,
    bank: BankIndex,
    outcome: AttemptOutcome,
    now: datetime,
) -> ConsumeResult:
    """Turn one already-scored attempt into the engine-state delta (KTD11's
    "same code path" for both live selection and replay). The caller must
    have already applied `outcome` through `update.apply_attempt` /
    `certification.maybe_certify` and written the result into `store` —
    this function only *reads* `store` (for the current misconception count
    and prerequisite certification), it never calls the update rule itself.

    Never mutates `engine_state` in place; returns a new one, mirroring
    update.py's own no-aliasing discipline.

    `skill_state_patches` is the one narrow exception to "the engine never
    writes SkillState": KTD12 requires resetting the just-fired
    misconception's counter on the *triggering* skill (its own attempts
    don't touch that skill again during the dose, so nothing else would
    ever clear it — see KTD12). The caller must write these patches via
    `apply_consume_patches` immediately after calling this function, same
    as it wrote the apply_attempt result before calling in.
    """
    new_state = _copy_engine_state(engine_state)
    patches: Dict[str, SkillState] = {}

    for skill_id in outcome.skill_ids:
        _mark_served(new_state, skill_id, outcome.variant_signature)

    # Multi-skill items never carry single-skill diagnosis (mirrors
    # maybe_certify's own guard — there is no one unambiguous skill to
    # diagnose against); v1's bank has none anyway (see load_bank).
    #
    # Known, deliberate v1 gap: core §2.10's multi-skill failure rule
    # ("queue single-skill probes of each component") is signaled by
    # update.py's UpdateResult.queue_probes but not consumed here or
    # anywhere else — this early return is exactly where that queuing
    # would need to happen. Left unimplemented on purpose, matching R4's
    # framing for the update-rule side of the same gap: v1's content has
    # zero multi-skill templates (every content/templates/*.yaml skill_ids
    # is length 1), so this is inert today, not a live bug. Wiring it
    # requires a design call the plan didn't make (which of several failed
    # skills' prerequisites to walk, and in what order) — a follow-up when
    # multi-skill content actually ships, not a silent gap: this comment is
    # the record of it.
    if len(outcome.skill_ids) != 1:
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)
    skill_id = outcome.skill_ids[0]

    if outcome.context == "placement":
        depth = outcome.depth_served if outcome.depth_served is not None else 0.0
        _advance_placement(new_state, store, content, outcome.student_id, skill_id, outcome.correct, depth, now)
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    if outcome.context == "review":
        # A review probe's pass/fail is already fully handled by
        # certification.py's apply_review_outcome (interval growth/reset,
        # lapse — see log.py's compose_attempt) before this function is
        # ever called. Diagnosis below is for ordinary practice-context
        # wrong answers (core doc §2.5); without this early return, a
        # failed review that isn't already the head of some other skill's
        # queue would fall through to "fresh diagnosis" and could spawn a
        # step-down probe or misconception dose *in addition to* the
        # certification-side lapse — a review isn't a diagnostic probe and
        # must never feed the diagnosis queues.
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    # --- Resolve a pending diagnostic item, if this attempt was one. Each
    # resolution short-circuits further diagnosis on this same attempt: an
    # attempt already explained by an active queue entry shouldn't also
    # spawn a fresh diagnosis chain on top of it (see module report).
    if (
        new_state.stepdown_queue
        and new_state.stepdown_queue[0].skill_id == skill_id
        and outcome.context == "probe"
    ):
        new_state.stepdown_queue.pop(0)
        if not outcome.correct:
            _queue_prereq_probes(new_state, content, skill_id)
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    if (
        new_state.prereq_probe_queue
        and new_state.prereq_probe_queue[0].skill_id == skill_id
        and outcome.context == "probe"
    ):
        new_state.prereq_probe_queue.pop(0)
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    if new_state.remediation_queue and new_state.remediation_queue[0].skill_id == skill_id:
        dose = new_state.remediation_queue[0]
        dose.remaining -= 1
        if dose.remaining <= 0:
            new_state.remediation_queue.pop(0)
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    # --- Fresh diagnosis (core doc §2.5's flowchart) ---------------------
    if outcome.correct:
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    if outcome.misconception_id is not None:
        skill_state = store.peek(outcome.student_id, skill_id)
        if skill_state.misc_count(outcome.misconception_id) >= constants.MISC_TRIGGER_COUNT:
            misc_entry = content.misconceptions.get(outcome.misconception_id)
            if misc_entry is not None:
                new_state.remediation_queue.append(
                    RemediationDose(
                        skill_id=misc_entry.remediation_target,
                        trigger_skill_id=skill_id,
                        misconception_id=outcome.misconception_id,
                    )
                )
                patched = copy.deepcopy(skill_state)
                patched.misc_counters.pop(outcome.misconception_id, None)  # KTD12: consumed, not standing
                patches[skill_id] = patched
        return ConsumeResult(engine_state=new_state, skill_state_patches=patches)

    # Untagged wrong: depth-step-down probe if an easier instance exists,
    # else straight to the prerequisite walk (R13).
    depth = outcome.depth_served if outcome.depth_served is not None else 0.0
    easier = _find_easier_instance(bank, skill_id, depth)
    if easier is not None:
        new_state.stepdown_queue.append(StepDownProbe(skill_id=skill_id, target_depth=easier["difficulty"]))
    else:
        _queue_prereq_probes(new_state, content, skill_id)
    return ConsumeResult(engine_state=new_state, skill_state_patches=patches)


def apply_consume_patches(store: StudentModelStore, student_id: str, patches: Dict[str, SkillState]) -> None:
    """Convenience for callers: write consume_event's skill_state_patches
    into `store` via `StudentModelStore.write_records` — the same
    merge-into-existing-records primitive orchestrator/log.py's
    `apply_event` uses for its own per-event writes."""
    store.write_records(student_id, patches)


# ---------------------------------------------------------------------------
# rank_serve_candidates: the R12 priority chain, as a ranked list (U8, R7)
# ---------------------------------------------------------------------------


@dataclass
class RankedCandidate:
    """One entry of `rank_serve_candidates`'s full R12-priority-ordered
    candidate list. `reason` reuses `Serve.reason`'s own vocabulary
    (review/remediation/stepdown/prereq_probe/placement/frontier);
    `context` is the attempt-event context this candidate would serve
    under if chosen. `bank_candidates` is this candidate's own pool read
    directly against the `bank` passed into `rank_serve_candidates` —
    raw (`bank.instances_by_skill.get(skill_id, [])`, no format/seen
    filtering) for every reason except frontier, whose pool is already the
    exact ceiling/mastery/prerequisite-eligible set the frontier scan
    computes (mirroring `select_next`'s own former `by_skill[skill_id]`).
    `select_next` uses `bank_candidates` only for frontier — every other
    reason keeps reading through its own `provide`/`candidates_for_skill`
    for the pool it actually serves from (U6's routing contract,
    unchanged by this refactor). `recommend_next`
    (orchestrator/serving.py, U8) has no such override — it always reads
    directly against the bank it resolved, so it uses `bank_candidates`
    for every reason. `unservable` (`not bank_candidates`) is precomputed
    here so both callers see the identical bank-servability verdict for a
    given candidate rather than each deriving their own."""

    skill_id: str
    reason: str  # review/remediation/stepdown/prereq_probe/placement/frontier
    context: str  # attempt-event context: placement / practice / review / probe
    target_depth: float
    bank_candidates: List[dict]
    unservable: bool


def rank_serve_candidates(
    store: StudentModelStore,
    engine_state: EngineState,
    content: ContentIndex,
    bank: BankIndex,
    student_id: str,
    country: str,
    curriculum: str,
    grade: int,
    now: datetime,
) -> List[RankedCandidate]:
    """R12's full priority chain — due review -> confirmed-misconception
    remediation -> queued step-down probes -> queued prerequisite probes ->
    placement -> frontier practice — as an ORDERED, RANKED list of every
    candidate at that priority position, not just the single best pick.
    R7: this is the *only* place this logic lives (verify by grep: exactly
    two call sites, `select_next` below and `ServingFacade.recommend_next`)
    — the partner recommendation path and the internal serving path share
    it, so they cannot diverge pedagogically.

    `select_next` walks this list top to bottom, trying to serve each
    candidate in turn via its own `provide`/`_try_serve`, falling through
    on an empty pool exactly as it always has (this function changes none
    of that fallthrough behavior — see `select_next`'s own docstring for
    why precomputing the *whole* chain up front, instead of stopping at
    the first step that succeeds, is still behaviorally identical: nothing
    below reads `engine_state.seen_history`, the only thing `_try_serve`
    mutates). `ServingFacade.recommend_next` (U8, R6) returns this list
    directly (mapped onto its own response shape), unfiltered by
    servability — a caller decides what to do with an `unservable` entry,
    this function never drops one.

    Mutates `engine_state` in place exactly once: lazily seeds the
    placement cursor via `_init_placement` if it hasn't been already (the
    same lazy init `select_next` has always performed at this point in its
    own priority chain) — no other mutation. Never touches
    `engine_state.seen_history` (only `_try_serve` does that, and only
    when a caller actually serves an instance) and never writes `store`.
    """
    candidates: List[RankedCandidate] = []

    def _bank_pool(skill_id: str) -> List[dict]:
        return bank_pool_for_skill(bank, skill_id)

    # 1. Due review — only the single next-due skill (matching
    # select_next's own historical single-candidate read at this step: a
    # caller that can't serve it falls through to remediation, never to
    # the *second* most-due skill).
    due = due_reviews(store.records_for(student_id), now)
    if due:
        skill_id, skill_state = due[0]
        pool = [inst for inst in _bank_pool(skill_id) if inst["question_type"] in constants.LOW_GUESS_FORMATS]
        candidates.append(
            RankedCandidate(skill_id, "review", "review", skill_state.certified_depth, pool, not pool)
        )

    # 2. Confirmed-misconception remediation dose (queue head only).
    if engine_state.remediation_queue:
        dose = engine_state.remediation_queue[0]
        skill_state = store.peek(student_id, dose.skill_id)
        ceiling = serving_ceiling(dose.skill_id, country, curriculum, grade, content)
        target = target_depth(skill_state.theta, ceiling)
        pool = _bank_pool(dose.skill_id)
        candidates.append(RankedCandidate(dose.skill_id, "remediation", "practice", target, pool, not pool))

    # 3. Queued depth-step-down probe (queue head only).
    if engine_state.stepdown_queue:
        probe = engine_state.stepdown_queue[0]
        pool = _bank_pool(probe.skill_id)
        candidates.append(
            RankedCandidate(probe.skill_id, "stepdown", "probe", probe.target_depth, pool, not pool)
        )

    # 4. Queued prerequisite probe (queue head only).
    if engine_state.prereq_probe_queue:
        probe = engine_state.prereq_probe_queue[0]
        target = starting_depth_for(probe.skill_id, country, curriculum, grade, content)
        pool = _bank_pool(probe.skill_id)
        candidates.append(
            RankedCandidate(probe.skill_id, "prereq_probe", "probe", target, pool, not pool)
        )

    # 5. Placement: one candidate per strand still walking, sorted by
    # strand — matching select_next's own per-strand fallback order (a
    # strand whose cursor skill can't be served doesn't stop the others).
    _init_placement(engine_state, content, country, curriculum, grade)
    for strand in sorted(engine_state.placement_cursor):
        skill_id = engine_state.placement_cursor[strand]
        target = starting_depth_for(skill_id, country, curriculum, grade, content)
        pool = _bank_pool(skill_id)
        candidates.append(RankedCandidate(skill_id, "placement", "placement", target, pool, not pool))

    # 6. Frontier practice: ceiling-capped, prerequisite-gated, every
    # eligible skill (not just the least-attempted winner select_next used
    # to stop at), ranked by the same least-attempted-first tie-break.
    by_skill: Dict[str, List[dict]] = {}
    for skill_id, node in content.skills.items():
        skill_state = store.peek(student_id, skill_id)
        ceiling = serving_ceiling(skill_id, country, curriculum, grade, content)
        # See select_next's historical comment (preserved in spirit here):
        # `lapsed` blocks dependents from being served downstream but never
        # revokes certified_depth, so a lapsed skill must stay eligible for
        # frontier practice itself or it could never re-enter the queue.
        mastered_to_ceiling = (
            skill_state.certified_depth is not None
            and skill_state.certified_depth >= ceiling
            and not skill_state.lapsed
        )
        if mastered_to_ceiling:
            continue
        # Deliberately the raw bank, not any candidates_for_skill override:
        # this scan's eligibility checks (ceiling, mastered-at-depth,
        # prerequisite certification) are independent of seen-status and
        # need the full pool at every depth, exactly as select_next always
        # required for this step.
        for instance in bank.instances_by_skill.get(skill_id, []):
            depth = instance["difficulty"]
            if depth > ceiling:
                break  # pool is sorted ascending by difficulty (load_bank) — nothing later qualifies either
            mastered_at_depth = (
                skill_state.certified_depth is not None
                and skill_state.certified_depth >= depth
                and not skill_state.lapsed
            )
            if mastered_at_depth:
                continue
            if not all(is_certified(store.peek(student_id, p), depth, now) for p in node.prerequisites):
                continue
            by_skill.setdefault(skill_id, []).append(instance)

    def _priority(skill_id: str) -> Tuple[float, str]:
        return (store.peek(student_id, skill_id).n, skill_id)

    for skill_id in sorted(by_skill, key=_priority):
        skill_state = store.peek(student_id, skill_id)
        ceiling = serving_ceiling(skill_id, country, curriculum, grade, content)
        target = target_depth(skill_state.theta, ceiling)
        # Already ceiling/mastery/prerequisite-filtered above — never empty,
        # so `unservable` is always False for a frontier candidate.
        candidates.append(RankedCandidate(skill_id, "frontier", "practice", target, by_skill[skill_id], False))

    return candidates


# ---------------------------------------------------------------------------
# select_next: the R12 priority chain
# ---------------------------------------------------------------------------


@dataclass
class Serve:
    item_instance_id: str
    skill_id: str
    difficulty: float
    context: str  # attempt-event enum value: placement / practice / review / probe
    reason: str  # engine-internal tag: review/remediation/stepdown/prereq_probe/placement/frontier


@dataclass
class SelectResult:
    engine_state: EngineState
    serve: Optional[Serve]
    warnings: List[str] = field(default_factory=list)


def _mark_served(engine_state: EngineState, skill_id: str, variant_signature: str) -> None:
    """Copy-on-write append: replaces `skill_id`'s list with a new one
    rather than mutating it in place. `_copy_engine_state`'s `seen_history`
    shares list objects with the state it was copied from until the first
    write to each key — mutating in place here would corrupt that shared
    list (and thus the caller's own copy) instead of only ever touching
    the copy this function was actually asked to update.

    KTD4: the list holds `variant_signature`s, not instance ids."""
    engine_state.seen_history[skill_id] = engine_state.seen_history.get(skill_id, []) + [variant_signature]


def _try_serve(
    new_state: EngineState,
    warnings: List[str],
    skill_id: str,
    candidates: List[dict],
    target: float,
    context: str,
    reason: str,
) -> Optional[Serve]:
    """The shape every select_next branch shares: pick_instance against
    `candidates`, mark it served, and build the Serve record — or return
    None so the caller falls through to the next priority-chain branch
    (R12's exact dispatch order and short-circuit behavior live in
    select_next, not here). Mutates `new_state`/`warnings` in place,
    matching select_next's own local-mutation style — both are already
    select_next's private copies by the time this is called."""
    instance, warning = pick_instance(candidates, target, new_state.seen_history.get(skill_id, []))
    if instance is None:
        return None
    if warning:
        warnings.append(warning)
    _mark_served(new_state, skill_id, instance["variant_signature"])
    return Serve(instance["item_instance_id"], skill_id, instance["difficulty"], context, reason)


def select_next(
    store: StudentModelStore,
    engine_state: EngineState,
    content: ContentIndex,
    bank: BankIndex,
    student_id: str,
    country: str,
    curriculum: str,
    grade: int,
    now: datetime,
    candidates_for_skill: Optional[Callable[[str], List[dict]]] = None,
) -> SelectResult:
    """R12's priority chain: due review -> confirmed-misconception
    remediation -> queued step-down probes -> queued prerequisite probes ->
    placement -> frontier practice. Returns a fresh EngineState with the
    chosen instance already marked seen (selection-time marking, not
    result-time — see the module report on why this is safe for replay).

    `candidates_for_skill`, when given, replaces the candidate-pool read
    for steps 1-5 (due review, remediation, step-down, prerequisite probe,
    placement — each a single-skill, single-target read) with a call to
    it instead of `bank.instances_by_skill` (U6: the reference orchestrator
    passes a provider backed by `orchestrator/serving.py`'s `get_candidates`
    — the same seen-filtered primitive external MCP consumers use, KTD6's
    parity rule for those five steps). Step 6 (frontier practice)
    deliberately keeps reading `bank` directly regardless of this
    parameter: its cross-skill ceiling scan needs every instance at every
    depth to reason about eligibility (ceiling, mastered-at-depth,
    prerequisite certification) independent of seen-status, and a
    seen-filtered, collapse-to-one-on-exhaustion pool (get_candidates' own
    R1 contract) can starve it — once a depth tier is mastered and
    permanently excluded from selection, that tier's leftover unseen
    instances keep the pool-wide exhaustion check from ever firing while
    frontier itself has nothing left to offer at the tiers still relevant.
    `pick_instance`'s own seen-filter (unchanged) still runs on top of
    whatever pool either path supplies, so passing an already seen-filtered
    pool into steps 1-5 composes correctly — the second filter there is a
    no-op, not a conflict.

    U8/R7: the priority chain itself — which skill is a candidate at each
    step, in what order, ceiling-capped and prerequisite-gated for the
    frontier step — is computed once by `rank_serve_candidates`, the one
    place this logic lives (also called by `ServingFacade.recommend_next`).
    This function only adds the *serving* half on top: for each ranked
    candidate in turn, build its actual pool (honoring `candidates_for_skill`
    for every reason except frontier, which always reads the pool
    `rank_serve_candidates` already computed against the real `bank`) and
    try to serve it, falling through to the next candidate on an empty
    pool — reproducing the exact step-by-step fallthrough this function
    used to implement inline."""
    provide = candidates_for_skill or (lambda skill_id: bank.instances_by_skill.get(skill_id, []))
    new_state = _copy_engine_state(engine_state)
    warnings: List[str] = []

    ranked = rank_serve_candidates(store, new_state, content, bank, student_id, country, curriculum, grade, now)

    for candidate in ranked:
        if candidate.reason == "review":
            pool = [inst for inst in provide(candidate.skill_id) if inst["question_type"] in constants.LOW_GUESS_FORMATS]
        elif candidate.reason == "frontier":
            # Always the real-bank, ceiling/mastery/prerequisite-filtered
            # pool rank_serve_candidates already computed — never routed
            # through candidates_for_skill (see this function's own
            # docstring on why frontier stays bank-direct).
            pool = candidate.bank_candidates
        else:
            pool = provide(candidate.skill_id)
        serve = _try_serve(new_state, warnings, candidate.skill_id, pool, candidate.target_depth, candidate.context, candidate.reason)
        if serve is not None:
            return SelectResult(engine_state=new_state, serve=serve, warnings=warnings)

    return SelectResult(engine_state=new_state, serve=None, warnings=warnings)


# ---------------------------------------------------------------------------
# rebuild_engine_state: KTD11's replay
# ---------------------------------------------------------------------------


def rebuild_session_state(
    log_path: Path, content: ContentIndex, bank_lookup: BankLookup
) -> Tuple[StudentModelStore, EngineState]:
    """Re-derive both the StudentModelStore and EngineState from scratch by
    replaying `log_path` once (KTD11) — the one full pass a session-startup
    caller needs both values from. `rebuild_engine_state` below is a thin
    wrapper for callers (and the existing test suite) that only want the
    EngineState half; calling this function directly instead of pairing
    `log.replay(log_path)` with `rebuild_engine_state(log_path, ...)` avoids
    reading and re-applying the whole log file twice for the same result —
    a cost that grows with the log's own size, i.e. with a real student's
    entire attempt history, at every session launch.

    Reuses orchestrator/log.py's own public per-event student-model
    application (`iter_events`, `apply_event`) rather than re-implementing
    the apply_attempt/maybe_certify/apply_review_outcome composition a
    second time here — the plan is explicit that this unit must not
    duplicate that logic, only add what's new (the engine-state
    derivation). The alternative of calling `log.replay()` once per growing
    log-file prefix was rejected: O(n^2) and no clearer.

    Applies every event in file order, exactly as `log.replay()` does,
    interleaving calls to `consume_event` immediately after each event's
    student-model update — so `consume_event` always reads the same
    just-updated SkillState a live caller would have seen at that point in
    the session, and any misconception-counter-reset patches it returns are
    folded back into the store before continuing, same as a live caller
    must do (see consume_event's docstring).

    KTD7: `bank_lookup` resolves *each event's own* stamped `bank_version`
    (via `make_bank_lookup`/`make_static_bank_lookup`) rather than replaying
    the whole log against one fixed bank — a log spanning builds must
    reproduce the pool `_find_easier_instance` actually consulted live at
    each event, not whichever bank the caller happens to hold today.
    """
    store = StudentModelStore()
    engine_state = EngineState()
    for event in log_module.iter_events(log_path):
        log_module.apply_event(store, event)
        store.append_event(event)
        now = log_module.parse_timestamp(event["timestamp"])
        outcome = attempt_outcome_from_event(event)
        bank = bank_lookup(event["bank_version"])
        result = consume_event(engine_state, store, content, bank, outcome, now)
        engine_state = result.engine_state
        apply_consume_patches(store, event["student_id"], result.skill_state_patches)
    return store, engine_state


def rebuild_engine_state(log_path: Path, content: ContentIndex, bank_lookup: BankLookup) -> EngineState:
    """EngineState-only view of `rebuild_session_state`, for callers (and
    tests) that don't also need the rebuilt StudentModelStore — e.g. a
    caller that already has its own live store and only wants to compare
    against a freshly-replayed EngineState. A caller that needs both should
    call `rebuild_session_state` directly rather than pairing this with a
    separate `log.replay(log_path)` call, which would replay the same log
    file twice for no additional information (see that function's
    docstring)."""
    _store, engine_state = rebuild_session_state(log_path, content, bank_lookup)
    return engine_state
