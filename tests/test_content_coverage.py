"""U8: end-to-end closure proof for the Class 6 servable-coverage plan
(R2, R3, R10, R11, KD1, KD2) — machine-checks the plan's Definition of
Done: the full 46-skill Class 6 (IN/NCERT grade 6) prerequisite closure
(34 skills mapped directly to a grade-6 chapter, plus 12 below-grade
foundation skills reached transitively via `prerequisites`) is fully
servable end-to-end against the FULL bank built from every real template
in `content/templates/` — not a narrow fixture slice.

Tests, each independent and named for the finding it proves:
  - `test_every_class6_closure_skill_has_a_template_and_a_numeric_entry_template`
    (R2/KTD6): template-level coverage — every closure skill has >=1
    template, and >=1 of those is `question_type: numeric_entry` (the
    low-guess certification format).
  - `test_foundation_skills_have_at_least_two_difficulty_tiers` (R3, AE3):
    each of the 12 foundation skills spans >=2 distinct authored
    `difficulty` values across its own templates.
  - `test_recommend_next_zero_unservable_for_class6` (AE2/R11): a fresh
    student's `recommend_next` for (IN, NCERT, 6) never flags a
    recommended skill unservable.
  - `test_prereq_probe_stepdown_into_foundation_skill_serves_a_real_instance`
    (AE1): a queued prerequisite-probe step-down into a foundation skill
    resolves to a real, non-empty served instance, not `empty_pool`.
  - `test_fetch_batch_spans_multiple_difficulty_tiers_for_a_foundation_skill`
    (AE3): a foundation skill's planned batch spans more than one
    difficulty tier (the target/harder ladder isn't collapsed to one).
  - `test_catalog_advertised_skills_are_all_servable` (R11's
    "advertised-versus-servable gap is zero"): every skill
    `catalog.list_skills` advertises for (IN, NCERT, 6) independently
    resolves to a real, non-empty candidate pool.
  - `test_every_non_null_distractor_misconception_id_resolves` (R5):
    every template distractor's `misconception_id` is either null or
    resolves to a real entry in the merged misconception registry.
  - `test_every_diagram_required_template_declares_a_figure` (figure
    plan U5, AE5): the forward half of the figure-coverage guarantee —
    every `diagram_required: true` template carries structured `figure`
    data.
  - `test_every_template_declaring_a_figure_is_diagram_required` (figure
    plan U5, AE5): the reverse half, unconditional — no allowlist, never
    had one.
  - `test_the_not_yet_annotated_allowlist_has_not_gone_stale` (figure
    plan U5): keeps the allowlist a shrinking ledger rather than a
    permanent exemption.

Builds its own small full-corpus bank per test that needs one (mirroring
`tests/test_serving.py`'s `full_banks_dir`/`full_facade` pattern) rather
than reusing conftest's session-scoped `bank` fixture for those, since a
couple of these need `bank_activate.activate` (recommend_next/get_candidates
resolve the ACTIVE pointer) which the shared fixture never calls. The two
tests that only need a loaded `BankIndex` directly (no ACTIVE pointer)
reuse conftest's session-scoped `content`/`bank` fixtures instead.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

import bank_activate
import bank_build
import graph_lint  # tools/graph_lint.py; conftest puts tools/ on sys.path
from conftest import REPO_ROOT, TEMPLATES_DIR
from orchestrator import catalog as catalog_module, constants, engine, serving, signals
from orchestrator.state import StudentModelStore

FIXTURE_DIR = REPO_ROOT / "content" / "fractions"
LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"

# The 12 below-grade foundation skills (R3, AE3) — already verified to need
# >=2 distinct difficulty values by earlier authoring units; this module
# asserts that as a regression, not re-deriving it.
FOUNDATION_SKILLS = frozenset(
    {
        "NUM.COUNT",
        "NUM.ADD.SUB",
        "NUM.CMP.ORDER",
        "NUM.MULT",
        "NUM.DIV",
        "NUM.PATTERN.EXTEND",
        "GEO.SHAPES.2D",
        "GEO.POLYGON.PROPS",
        "MEAS.LENGTH.UNITS",
        "FRAC.PARTWHOLE",
        "FRAC.UNIT",
        "NUM.MULT.TABLES",
    }
)


def _class6_closure() -> set:
    """The 46-skill Class 6 prerequisite closure: every skill with a
    mapping row at exactly (country=IN, curriculum=NCERT, grade=6), plus
    every skill transitively reachable by walking `prerequisites` from
    those — computed straight off `tools/graph_lint.py`'s own
    `load_content` (the same merged skills/misconceptions/mappings view
    that tool lints and `orchestrator/engine.py` loads), not re-derived
    from a hardcoded list."""
    result = graph_lint.LintResult()
    content = graph_lint.load_content(FIXTURE_DIR, result)
    mapped = {
        m["skill_id"]
        for m in content["mappings"]
        if m.get("country") == "IN" and m.get("curriculum") == "NCERT" and m.get("grade") == 6
    }
    skills_by_id = {s["skill_id"]: s for s in content["skills"] if "skill_id" in s}
    closure = set(mapped)
    stack = list(mapped)
    while stack:
        skill_id = stack.pop()
        node = skills_by_id.get(skill_id)
        if node is None:
            continue
        for prereq in node.get("prerequisites", []):
            if prereq not in closure:
                closure.add(prereq)
                stack.append(prereq)
    return closure


def _load_all_templates() -> dict:
    """Every `content/templates/*.yaml` file, read directly (not through a
    built bank — a template that never materializes an instance would
    otherwise slip through a bank-only coverage check), keyed by filename."""
    templates = {}
    for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
        templates[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return templates


# ---------------------------------------------------------------------------
# 1. Coverage (R2/KTD6): every closure skill has a template, and a
#    numeric_entry one.
# ---------------------------------------------------------------------------


def test_every_class6_closure_skill_has_a_template_and_a_numeric_entry_template():
    closure = _class6_closure()
    assert len(closure) == 46, (
        f"Class 6 (IN/NCERT grade 6) prerequisite closure changed size: expected 46 "
        f"(34 mapped + 12 foundation), got {len(closure)}: {sorted(closure)}"
    )

    templates = _load_all_templates()
    covering_skill_ids: set = set()
    numeric_entry_skill_ids: set = set()
    for template in templates.values():
        skill_ids = template.get("skill_ids", []) or []
        is_numeric_entry = template.get("question_type") == "numeric_entry"
        for skill_id in skill_ids:
            covering_skill_ids.add(skill_id)
            if is_numeric_entry:
                numeric_entry_skill_ids.add(skill_id)

    missing_any_template = closure - covering_skill_ids
    assert not missing_any_template, (
        f"Class 6 closure skills with zero covering templates: {sorted(missing_any_template)}"
    )

    missing_numeric_entry = closure - numeric_entry_skill_ids
    assert not missing_numeric_entry, (
        "Class 6 closure skills with no numeric_entry template (the low-guess "
        f"certification format, R2/KTD6): {sorted(missing_numeric_entry)}"
    )


# ---------------------------------------------------------------------------
# 2. Foundation difficulty-tier regression (R3, AE3).
# ---------------------------------------------------------------------------


def test_foundation_skills_have_at_least_two_difficulty_tiers():
    templates = _load_all_templates()
    difficulties_by_skill: dict = {}
    for template in templates.values():
        difficulty = template.get("difficulty")
        for skill_id in template.get("skill_ids", []) or []:
            difficulties_by_skill.setdefault(skill_id, set()).add(difficulty)

    single_tier = {
        skill_id: sorted(difficulties_by_skill.get(skill_id, set()))
        for skill_id in sorted(FOUNDATION_SKILLS)
        if len(difficulties_by_skill.get(skill_id, set())) < 2
    }
    assert not single_tier, (
        f"foundation skills authored with fewer than 2 distinct difficulty values: {single_tier}"
    )


# ---------------------------------------------------------------------------
# Shared fixtures for the facade-level tests below (AE1-AE3, catalog
# honesty): a full bank built from every content/templates/*.yaml, small
# pool_cap for build speed, activated so recommend_next/get_candidates can
# resolve the ACTIVE pointer -- mirrors tests/test_serving.py's
# full_banks_dir/full_facade fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_banks_dir(tmp_path: Path) -> Path:
    out = tmp_path / "full_banks"
    out.mkdir()
    manifest, per_template = bank_build.run_build(TEMPLATES_DIR, LOCALE_PACK, "coverage-build", pool_cap=3)
    bank_build.write_bank(out, "coverage-build", manifest, per_template)
    bank_activate.activate(out, "coverage-build")
    return out


@pytest.fixture()
def full_facade(full_banks_dir: Path, tmp_path: Path, content: engine.ContentIndex) -> serving.ServingFacade:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return serving.ServingFacade(full_banks_dir, logs_dir, content)


# ---------------------------------------------------------------------------
# 3. AE2 - recommend_next: zero unservable entries for a fresh Class 6
#    student.
# ---------------------------------------------------------------------------


def test_recommend_next_zero_unservable_for_class6(full_facade: serving.ServingFacade) -> None:
    now = datetime(2026, 7, 29, 12, 0, 0)
    result = full_facade.recommend_next(
        "stu-ae2-fresh", "IN", "NCERT", 6, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    assert result.recommendations, "recommend_next returned nothing at all for a fresh Class 6 student"

    unservable = [r.skill_id for r in result.recommendations if r.unservable]
    assert not unservable, (
        f"recommend_next flagged unservable skill(s) for a fresh Class 6 student: {unservable}"
    )


# ---------------------------------------------------------------------------
# 4. AE1 - a prerequisite-probe step-down into a foundation skill serves a
#    real, non-empty instance.
# ---------------------------------------------------------------------------


def test_prereq_probe_stepdown_into_foundation_skill_serves_a_real_instance(
    content: engine.ContentIndex, bank: engine.BankIndex
) -> None:
    """Directly seeds an EngineState with a queued prerequisite probe onto
    a foundation skill (SM Sec5's "prerequisite-walk fallback", R13) --
    the lighter-weight idiom this unit's own guidance prefers over a full
    stochastic SimulatedStudent run for proving the *serving* half of a
    step-down: `engine.select_next`'s priority chain checks the
    prereq_probe_queue head (priority 4) before placement/frontier, so a
    pre-queued probe is the direct, non-flaky way to exercise it. Reuses
    conftest's session-scoped `content`/`bank` fixtures -- already a real
    ContentIndex/BankIndex built from every content/templates/*.yaml
    template, no ACTIVE pointer needed since this calls engine.select_next
    directly rather than going through ServingFacade."""
    foundation_skill_id = "NUM.ADD.SUB"
    assert foundation_skill_id in FOUNDATION_SKILLS

    engine_state = engine.EngineState(
        placement_cursor={},
        placement_initialized=True,
        prereq_probe_queue=[
            engine.PrereqProbe(skill_id=foundation_skill_id, trigger_skill_id="INT.ADD.SUB")
        ],
    )
    store = StudentModelStore()
    now = datetime(2026, 7, 29, 12, 0, 0)

    result = engine.select_next(
        store, engine_state, content, bank, "stu-ae1-stepdown", "IN", "NCERT", 6, now,
    )

    assert result.serve is not None, (
        "prerequisite-probe step-down into a foundation skill served nothing "
        "(empty pool / unservable) instead of a real instance"
    )
    assert result.serve.skill_id == foundation_skill_id
    assert result.serve.reason == "prereq_probe"
    assert result.serve.context == "probe"
    assert not result.warnings


# ---------------------------------------------------------------------------
# 5. AE3 - fetch_batch tier spread for a foundation skill.
# ---------------------------------------------------------------------------


def test_fetch_batch_spans_multiple_difficulty_tiers_for_a_foundation_skill(
    full_facade: serving.ServingFacade,
) -> None:
    skill_id = "NUM.ADD.SUB"
    assert skill_id in FOUNDATION_SKILLS
    now = datetime(2026, 7, 29, 12, 0, 0)

    session = full_facade.open_session(
        "sess-ae3", "stu-ae3", "IN", "NCERT", 6, constants.CONSUMER_ID_INTERNAL,
    )
    result = full_facade.fetch_batch(session, skill_id, constants.CONSUMER_ID_INTERNAL, batch_size=10)

    assert result.count_returned > 0, f"fetch_batch returned nothing for foundation skill {skill_id!r}"
    difficulties = {item.instance["difficulty"] for item in result.items}
    assert len(difficulties) > 1, (
        f"fetch_batch for foundation skill {skill_id!r} collapsed to a single difficulty "
        f"tier: {difficulties} -- the target/harder ladder rungs never diverged"
    )


# ---------------------------------------------------------------------------
# 6. Catalog-honesty invariant: every advertised Class 6 skill is servable.
# ---------------------------------------------------------------------------


def test_catalog_advertised_skills_are_all_servable(full_facade: serving.ServingFacade) -> None:
    """R11's "advertised-versus-servable gap is zero" success criterion, as
    a durable regression test.

    Investigated first (see this unit's own verification): for a *fresh*
    student, `recommend_next` is a ranked priority-chain slice -- one
    placement candidate per strand still walking, plus zero-prerequisite
    frontier skills -- not every skill `catalog.list_skills` advertises
    for the scope (confirmed: it returns far fewer than the 34 advertised
    IN/NCERT grade-6 skills for a student with no attempt history at all,
    since most of those skills' prerequisites aren't certified yet and so
    never qualify for the frontier step). Subset-equality between
    `list_skills` and a fresh `recommend_next` would therefore be the wrong
    check -- it would fail for reasons that have nothing to do with
    servability. Instead, two direct checks:

    (a) no advertised skill_id that DOES appear in recommend_next's ranked
        list is ever flagged unservable there, and
    (b) every advertised skill_id independently resolves to a real,
        non-empty candidate pool via `get_candidates` -- the single-skill
        servability primitive, unaffected by ranking/priority-chain
        eligibility.
    """
    now = datetime(2026, 7, 29, 12, 0, 0)
    advertised = catalog_module.list_skills(full_facade.content, "IN", "NCERT", 6).skills
    advertised_ids = {mapped.skill_id for mapped in advertised}
    assert advertised_ids, "fixture assumption: IN/NCERT grade 6 maps at least one skill"

    recommended = full_facade.recommend_next(
        "stu-ae-catalog-rank", "IN", "NCERT", 6, consumer_id=constants.CONSUMER_ID_INTERNAL, now=now,
    )
    unservable_and_advertised = {
        r.skill_id for r in recommended.recommendations if r.unservable and r.skill_id in advertised_ids
    }
    assert not unservable_and_advertised, (
        f"recommend_next flagged advertised Class 6 skill(s) unservable: {unservable_and_advertised}"
    )

    session = full_facade.open_session(
        "sess-ae-catalog", "stu-ae-catalog-servability", "IN", "NCERT", 6, constants.CONSUMER_ID_INTERNAL,
    )
    unresolvable = {}
    for skill_id in sorted(advertised_ids):
        candidates = full_facade.get_candidates(session, skill_id, constants.CONSUMER_ID_INTERNAL)
        if candidates.signal in (signals.UNKNOWN_SKILL, signals.EMPTY_POOL):
            unresolvable[skill_id] = candidates.signal
    assert not unresolvable, f"advertised Class 6 skill(s) with no servable instance at all: {unresolvable}"


# ---------------------------------------------------------------------------
# 7. Diagnostic integrity (R5): every distractor's misconception_id is
#    either null or resolves.
# ---------------------------------------------------------------------------


def test_every_non_null_distractor_misconception_id_resolves() -> None:
    result = graph_lint.LintResult()
    content = graph_lint.load_content(FIXTURE_DIR, result)
    misconception_ids = {
        m["misconception_id"] for m in content["misconceptions"] if "misconception_id" in m
    }

    templates = _load_all_templates()
    unresolved = []
    for name, template in templates.items():
        for distractor in template.get("distractors", []) or []:
            assert "misconception_id" in distractor, (
                f"{name}: distractor missing the misconception_id key entirely (a null value is a "
                f"reviewed null, R5; a missing key is not): {distractor}"
            )
            value = distractor["misconception_id"]
            if value is not None and value not in misconception_ids:
                unresolved.append((name, value))

    assert not unresolved, f"distractors with a non-null misconception_id that doesn't resolve: {unresolved}"


# ---------------------------------------------------------------------------
# 8. Figure coverage (partner-figure plan U5, AE5): `diagram_required` and
#    the structured `figure` block agree in both directions.
#
#    Both directions are unconditional. A shrinking NOT_YET_ANNOTATED ledger
#    carried the forward direction while the two content waves landed; the
#    census is now fully annotated, so the ledger and its hygiene test are
#    gone and a figure-dependent template simply ships with its figure.
# ---------------------------------------------------------------------------


_ANNOTATE_HINT = (
    "author a `figure:` block on it (schemas/item-record.schema.json's twelve-kind "
    "union; the kind each census template renders as is in "
    "docs/plans/2026-08-03-001-figure-census.md)"
)


def _figure_annotation() -> tuple[set, set]:
    """(template_ids with `diagram_required: true`, template_ids declaring a
    non-empty `figure`) — read straight off `content/templates/`, since a
    template that never materializes an instance must still be caught."""
    diagram_required: set = set()
    declares_figure: set = set()
    for name, template in _load_all_templates().items():
        template_id = template.get("template_id") or name
        if template.get("diagram_required") is True:
            diagram_required.add(template_id)
        if template.get("figure"):
            declares_figure.add(template_id)
    return diagram_required, declares_figure


def test_every_diagram_required_template_declares_a_figure() -> None:
    """AE5, forward: a figure-dependent template ships with the machine-
    renderable figure data a partner needs, not just prose in the stem."""
    diagram_required, declares_figure = _figure_annotation()

    unannotated = diagram_required - declares_figure
    assert not unannotated, (
        f"template(s) marked diagram_required: true with no `figure` block: "
        f"{sorted(unannotated)} — {_ANNOTATE_HINT}."
    )


def test_every_template_declaring_a_figure_is_diagram_required() -> None:
    """AE5, reverse — unconditional, no allowlist: a template carrying a
    figure is by definition figure-dependent, so `diagram_required: false`
    next to a `figure` block means one of the two is wrong."""
    diagram_required, declares_figure = _figure_annotation()

    mismatched = declares_figure - diagram_required
    assert not mismatched, (
        f"template(s) declaring a `figure` block but not marked diagram_required: true: "
        f"{sorted(mismatched)} — set `diagram_required: true` on them, or drop the "
        "`figure` block if the item really doesn't depend on a visual"
    )
