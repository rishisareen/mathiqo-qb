"""U7: the student-independent discovery layer (KD3's first two layers —
curriculum catalog and raw graph reads) — pure functions over an already
loaded `engine.ContentIndex`. No session, no student, no consumer_id: R17's
link enforcement gates student-scoped operations on `ServingFacade`
(`open_session`, `submit_attempt`, `get_progress`, ...); this module is
explicitly the layer KD3 says stays open regardless (a partner asking "what
does Mathiqo cover" needs no student context to ask it, R1-R4).

Four read operations, each documented at its own definition:
  - `list_curricula`  — every (country, curriculum, grade) triple covered,
    grouped country -> curriculum -> grades (R1).
  - `list_units`      — the structured chapter/unit list for one
    (country, curriculum, grade), built from `MappingRow.unit_code`/
    `unit_name` (KTD10), never from `syllabus_ref` string-parsing (R2). A
    curriculum with no chapter granularity (Ontario, in the shipped
    fixture) returns its mapped skills directly instead (R1's "chapters
    ... within a grade" only where the mapping layer actually carries that
    structure).
  - `list_skills`     — the mapped skills for a curriculum scope (optionally
    narrowed to one unit), each with curriculum-local presentation:
    learning objective, depth expectation, terminology overrides (R3).
  - `get_skill_graph` — the canonical skill graph: skill node(s) with
    prerequisites, depth axis (`canonical_stage`), strand, and associated
    misconception ids (R4).

**content_version (KTD9).** Every response dataclass below carries
`content_version`, copied from `content.content_version` — a caller must
never receive a catalog/graph response with no version stamped, per this
unit's plan point 5.

**Unknown scope, not an error (plan point 6).** A (country, curriculum,
grade) with no matching mapping rows at all is a query that returns
nothing, not a failure: `list_units`/`list_skills` return their normal
result shape with empty lists and the version still stamped, never raise.
`get_skill_graph` treats an unknown skill_id the same way — silently
absent from the returned mapping, not an exception — for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Union

from .engine import ContentIndex

# ---------------------------------------------------------------------------
# list_curricula (R1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Curriculum:
    curriculum: str
    grades: List[int]  # sorted ascending


@dataclass(frozen=True)
class Country:
    country: str
    curricula: List[Curriculum]  # sorted by curriculum name


@dataclass(frozen=True)
class CatalogResult:
    content_version: str
    countries: List[Country]  # sorted by country code


def list_curricula(content: ContentIndex) -> CatalogResult:
    """Every distinct (country, curriculum, grade) triple with at least one
    mapping row, grouped country -> curriculum -> sorted grades — a caller
    can answer "what curricula does Mathiqo cover" by walking this
    structure directly, without re-deriving the grouping itself.

    Derived from `content.mapped_skills_by_country_curriculum_grade`'s own
    keys (already exactly this triple, one entry per non-empty scope) —
    not a fresh scan of `content.mappings`, which would give the same key
    set at extra cost.
    """
    by_country: Dict[str, Dict[str, List[int]]] = {}
    for country, curriculum, grade in content.mapped_skills_by_country_curriculum_grade:
        grades = by_country.setdefault(country, {}).setdefault(curriculum, [])
        grades.append(grade)

    countries = [
        Country(
            country=country,
            curricula=[
                Curriculum(curriculum=curriculum, grades=sorted(by_country[country][curriculum]))
                for curriculum in sorted(by_country[country])
            ],
        )
        for country in sorted(by_country)
    ]
    return CatalogResult(content_version=content.content_version, countries=countries)


# ---------------------------------------------------------------------------
# list_units (R1, R2, KTD10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    unit_code: str
    unit_name: Optional[str]
    skill_ids: List[str]  # sorted


@dataclass(frozen=True)
class UnitsResult:
    content_version: str
    country: str
    curriculum: str
    grade: int
    has_units: bool  # False for a unit-less curriculum (KTD10, e.g. Ontario) or an uncovered scope
    units: List[Unit]  # populated only when has_units is True
    skill_ids: List[str]  # this grade's mapped skills directly; populated only when has_units is False


def list_units(content: ContentIndex, country: str, curriculum: str, grade: int) -> UnitsResult:
    """KTD10: chapters are built from `MappingRow.unit_code`/`unit_name`,
    never from parsing `syllabus_ref` (R2). `has_units` is decided for the
    whole scope from whether any mapped row in it carries a `unit_code` —
    true of every IN/NCERT row in the shipped fixture, false of every
    CA/ONTARIO row (Ontario has no chapter granularity mapped at all). A
    scope with no mapping rows at all (uncovered) returns `has_units=False`
    with both `units` and `skill_ids` empty — the "not covered, not an
    error" shape (plan point 6), indistinguishable in shape from a
    genuinely unit-less curriculum with zero mapped skills, which does not
    occur in the shipped fixture.
    """
    skill_ids = content.mapped_skills_by_country_curriculum_grade.get((country, curriculum, grade), [])
    rows = [content.mappings[(skill_id, country, curriculum, grade)] for skill_id in skill_ids]

    if not any(row.unit_code for row in rows):
        return UnitsResult(
            content_version=content.content_version,
            country=country,
            curriculum=curriculum,
            grade=grade,
            has_units=False,
            units=[],
            skill_ids=list(skill_ids),
        )

    grouped: Dict[str, List[str]] = {}
    unit_names: Dict[str, Optional[str]] = {}
    for row in rows:
        if row.unit_code is None:
            # Not exercised by the shipped fixture (every row in a
            # unit-bearing curriculum's scope carries unit_code there) —
            # a skill mapped without chapter granularity inside an
            # otherwise unit-bearing scope has nowhere to sort into and is
            # skipped from the chapter list rather than fabricating one.
            continue
        grouped.setdefault(row.unit_code, []).append(row.skill_id)
        unit_names.setdefault(row.unit_code, row.unit_name)

    units = [
        Unit(unit_code=unit_code, unit_name=unit_names[unit_code], skill_ids=sorted(skill_ids_in_unit))
        for unit_code, skill_ids_in_unit in sorted(grouped.items())
    ]
    return UnitsResult(
        content_version=content.content_version,
        country=country,
        curriculum=curriculum,
        grade=grade,
        has_units=True,
        units=units,
        skill_ids=[],
    )


# ---------------------------------------------------------------------------
# list_skills (R3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedSkill:
    skill_id: str
    depth_expectation: str
    learning_objective: Optional[str]
    terminology_overrides: Dict[str, str]
    unit_code: Optional[str]
    unit_name: Optional[str]


@dataclass(frozen=True)
class SkillsResult:
    content_version: str
    country: str
    curriculum: str
    grade: int
    unit_code: Optional[str]  # the narrowing filter this result was built with, if any
    skills: List[MappedSkill]  # sorted by skill_id


def list_skills(
    content: ContentIndex,
    country: str,
    curriculum: str,
    grade: int,
    unit_code: Optional[str] = None,
) -> SkillsResult:
    """The mapped skills for a (country, curriculum, grade) scope, each
    carrying its curriculum-local presentation straight off the
    `MappingRow` (R3): `learning_objective`, `depth_expectation`,
    `terminology_overrides`. `unit_code`, when given, narrows to just that
    chapter's skills (AE1's "for a chapter receives the mapped ... skills");
    left `None` (the default), every mapped skill in the grade scope is
    returned regardless of its own unit_code (including the `None`
    unit_code every row in a unit-less curriculum carries).

    An unmatched scope or `unit_code` returns `skills=[]` with the version
    still stamped — never raises (plan point 6).
    """
    skill_ids = content.mapped_skills_by_country_curriculum_grade.get((country, curriculum, grade), [])
    skills: List[MappedSkill] = []
    for skill_id in skill_ids:
        row = content.mappings[(skill_id, country, curriculum, grade)]
        if unit_code is not None and row.unit_code != unit_code:
            continue
        skills.append(
            MappedSkill(
                skill_id=row.skill_id,
                depth_expectation=row.depth_expectation,
                learning_objective=row.learning_objective,
                terminology_overrides=dict(row.terminology_overrides),
                unit_code=row.unit_code,
                unit_name=row.unit_name,
            )
        )
    return SkillsResult(
        content_version=content.content_version,
        country=country,
        curriculum=curriculum,
        grade=grade,
        unit_code=unit_code,
        skills=skills,
    )


# ---------------------------------------------------------------------------
# get_skill_graph (R4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillGraphNode:
    skill_id: str
    strand: str
    canonical_stage: int  # the depth axis
    prerequisites: List[str]
    misconceptions: List[str]  # associated misconception ids


@dataclass(frozen=True)
class SkillGraphResult:
    content_version: str
    skills: Dict[str, SkillGraphNode]  # keyed by skill_id; requested-but-unknown ids are simply absent


def get_skill_graph(
    content: ContentIndex,
    skill_ids: Optional[Union[str, Iterable[str]]] = None,
) -> SkillGraphResult:
    """The canonical skill graph (R4): each requested skill's prerequisites,
    depth axis (`canonical_stage`), strand, and associated misconception
    ids, straight off `content.skills` (never the mapping layer — this is
    the graph, not a curriculum's local view of it).

    `skill_ids` accepts a single skill_id (str), an iterable of skill_ids,
    or `None` (default) — `None` returns the whole graph, every skill in
    `content.skills`, which both "give me this one skill's neighborhood"
    and "give me everything" callers can use by choosing what they pass.
    An id in `skill_ids` that names no skill in `content.skills` is left
    out of the returned `skills` mapping rather than raising (plan point
    6's "not covered" contract, extended to an unknown skill_id the same
    way it applies to an unknown curriculum scope).
    """
    if skill_ids is None:
        requested: Iterable[str] = content.skills.keys()
    elif isinstance(skill_ids, str):
        requested = [skill_ids]
    else:
        requested = skill_ids

    nodes: Dict[str, SkillGraphNode] = {}
    for skill_id in requested:
        node = content.skills.get(skill_id)
        if node is None:
            continue
        nodes[skill_id] = SkillGraphNode(
            skill_id=node.skill_id,
            strand=node.strand,
            canonical_stage=node.canonical_stage,
            prerequisites=list(node.prerequisites),
            misconceptions=list(node.misconceptions),
        )
    return SkillGraphResult(content_version=content.content_version, skills=nodes)
