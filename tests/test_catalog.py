"""Tests for orchestrator/catalog.py — U7's student-independent discovery
layer (KD3): curriculum browse (`list_curricula`, `list_units`,
`list_skills`) and the raw skill graph (`get_skill_graph`). Also covers
`ServingFacade`'s thin passthroughs onto this module.

Uses the shared `content` session fixture (tests/conftest.py) — the real
content/fractions/*.yaml graph and mappings, not a synthetic stand-in, so
assertions are grounded in the actual shipped fixture (AE1: IN/NCERT grade
6's fractions chapter, Ontario's unit-less mapping rows).
"""

from __future__ import annotations

from orchestrator import catalog, engine, serving

# See AE1 / the U7 plan: IN/NCERT grade 6's fractions chapter is
# Ganita Prakash Ch. 7, unit_code "IN-NCERT-G6-CH7" — every FRAC.* skill
# mapped at that grade carries this unit_code (content/fractions/
# fractions-ncert.yaml, content/fractions/mappings.yaml).
NCERT_G6_FRACTIONS_UNIT = "IN-NCERT-G6-CH7"

# CA/ONTARIO's mapping rows carry no unit_code at all (KTD10: a curriculum
# with no chapter granularity) — content/fractions/mappings.yaml.
ONTARIO_GRADE = 4

# An (country, curriculum, grade) triple with no mapping rows anywhere in
# the fixture — the "uncovered scope" case (plan point 6).
UNCOVERED = ("ZZ", "NOWHERE", 99)


def test_list_curricula_covers_ncert_and_ontario(content: engine.ContentIndex):
    result = catalog.list_curricula(content)
    assert result.content_version == content.content_version

    by_country = {c.country: c for c in result.countries}
    assert "IN" in by_country
    assert "CA" in by_country

    in_curricula = {c.curriculum: c.grades for c in by_country["IN"].curricula}
    assert "NCERT" in in_curricula
    assert 6 in in_curricula["NCERT"]

    ca_curricula = {c.curriculum: c.grades for c in by_country["CA"].curricula}
    assert "ONTARIO" in ca_curricula
    assert ONTARIO_GRADE in ca_curricula["ONTARIO"]


def test_list_units_ncert_grade6_has_structured_chapters(content: engine.ContentIndex):
    """AE1's catalog half: IN/NCERT grade 6 returns a structured chapter
    list built from unit_code/unit_name (KTD10), not free-text parsing."""
    result = catalog.list_units(content, "IN", "NCERT", 6)
    assert result.content_version == content.content_version
    assert result.has_units is True
    assert result.skill_ids == []  # skill_ids only populated for unit-less scopes

    by_code = {u.unit_code: u for u in result.units}
    assert NCERT_G6_FRACTIONS_UNIT in by_code
    fractions_unit = by_code[NCERT_G6_FRACTIONS_UNIT]
    assert fractions_unit.unit_name == "Class 6, Ch. 7"
    assert "FRAC.SIMPLIFY" in fractions_unit.skill_ids
    assert "FRAC.EQUIV" in fractions_unit.skill_ids
    # Every unit's skill_ids actually belongs to that unit_code's own rows.
    for row_skill_id in fractions_unit.skill_ids:
        row = content.mappings[(row_skill_id, "IN", "NCERT", 6)]
        assert row.unit_code == NCERT_G6_FRACTIONS_UNIT


def test_list_units_ontario_is_unit_less(content: engine.ContentIndex):
    """The unit-less Ontario curriculum returns its skills directly, no
    chapter layer, per KTD10's "Ontario has none" note."""
    result = catalog.list_units(content, "CA", "ONTARIO", ONTARIO_GRADE)
    assert result.content_version == content.content_version
    assert result.has_units is False
    assert result.units == []
    assert result.skill_ids  # non-empty: real mapped skills at this grade
    assert "FRAC.UNIT" in result.skill_ids
    assert "FRAC.PARTWHOLE" in result.skill_ids
    # Confirms these rows genuinely carry no unit_code (grounding, not
    # just asserting the function's own output back at itself).
    for skill_id in result.skill_ids:
        row = content.mappings[(skill_id, "CA", "ONTARIO", ONTARIO_GRADE)]
        assert row.unit_code is None


def test_list_skills_carries_ncert_terminology_and_objectives(content: engine.ContentIndex):
    """AE1's skill-detail half: for a chapter, the mapped skills carry
    NCERT terminology ("lowest form") and learning objectives. Verified
    against the real fixture row rather than fabricated."""
    fixture_row = content.mappings[("FRAC.SIMPLIFY", "IN", "NCERT", 6)]
    assert fixture_row.terminology_overrides.get("simplest form") == "lowest form"  # ground the assertion

    result = catalog.list_skills(content, "IN", "NCERT", 6, unit_code=NCERT_G6_FRACTIONS_UNIT)
    assert result.content_version == content.content_version
    by_id = {s.skill_id: s for s in result.skills}
    assert "FRAC.SIMPLIFY" in by_id

    simplify = by_id["FRAC.SIMPLIFY"]
    assert simplify.terminology_overrides == {"simplest form": "lowest form"}
    assert simplify.learning_objective is not None
    assert "lowest form" in simplify.learning_objective
    assert simplify.depth_expectation == fixture_row.depth_expectation
    assert simplify.unit_code == NCERT_G6_FRACTIONS_UNIT

    # Narrowing to unit_code excludes skills from other units in the same
    # grade (e.g. NUM.PLACE.VALUE / NUM.ESTIMATE, mapped to a different
    # chapter, IN-NCERT-G6-CH3).
    assert "NUM.PLACE.VALUE" not in by_id


def test_list_skills_whole_grade_scope_when_unit_code_omitted(content: engine.ContentIndex):
    result = catalog.list_skills(content, "IN", "NCERT", 6)
    skill_ids = {s.skill_id for s in result.skills}
    # Skills from more than one chapter are present when no unit_code
    # narrows the scope.
    assert "FRAC.SIMPLIFY" in skill_ids
    assert "NUM.PLACE.VALUE" in skill_ids


def test_uncovered_scope_returns_empty_not_exception(content: engine.ContentIndex):
    country, curriculum, grade = UNCOVERED

    units_result = catalog.list_units(content, country, curriculum, grade)
    assert units_result.content_version == content.content_version
    assert units_result.has_units is False
    assert units_result.units == []
    assert units_result.skill_ids == []

    skills_result = catalog.list_skills(content, country, curriculum, grade)
    assert skills_result.content_version == content.content_version
    assert skills_result.skills == []

    # A (country, curriculum, grade) triple absent from list_curricula's
    # own output confirms this scope really is uncovered, not a typo in
    # the test's own fixture assumptions.
    catalog_result = catalog.list_curricula(content)
    covered = {
        (c.country, curr.curriculum, grade)
        for c in catalog_result.countries
        for curr in c.curricula
        for grade in curr.grades
    }
    assert UNCOVERED not in covered


def test_get_skill_graph_matches_content_directly(content: engine.ContentIndex):
    """Cross-checked against orchestrator.engine.ContentIndex.skills
    directly, not re-derived assumptions."""
    fixture_node = content.skills["FRAC.SIMPLIFY"]

    result = catalog.get_skill_graph(content, "FRAC.SIMPLIFY")
    assert result.content_version == content.content_version
    assert set(result.skills.keys()) == {"FRAC.SIMPLIFY"}

    node = result.skills["FRAC.SIMPLIFY"]
    assert node.strand == fixture_node.strand
    assert node.canonical_stage == fixture_node.canonical_stage
    assert node.prerequisites == list(fixture_node.prerequisites)
    assert node.misconceptions == list(fixture_node.misconceptions)
    assert node.prerequisites == ["FRAC.EQUIV"]
    assert node.misconceptions == ["MISC.FRAC.NO-SIMPLIFY"]


def test_get_skill_graph_list_of_ids_and_unknown_id_dropped(content: engine.ContentIndex):
    result = catalog.get_skill_graph(content, ["FRAC.SIMPLIFY", "FRAC.EQUIV", "NOT.A.REAL.SKILL"])
    assert set(result.skills.keys()) == {"FRAC.SIMPLIFY", "FRAC.EQUIV"}
    assert result.content_version == content.content_version


def test_get_skill_graph_none_returns_whole_graph(content: engine.ContentIndex):
    result = catalog.get_skill_graph(content)
    assert set(result.skills.keys()) == set(content.skills.keys())
    assert result.content_version == content.content_version


def test_every_response_stamps_content_version(content: engine.ContentIndex):
    assert catalog.list_curricula(content).content_version == content.content_version
    assert catalog.list_units(content, "IN", "NCERT", 6).content_version == content.content_version
    assert catalog.list_skills(content, "IN", "NCERT", 6).content_version == content.content_version
    assert catalog.get_skill_graph(content, "FRAC.SIMPLIFY").content_version == content.content_version
    # And for the uncovered-scope path too.
    assert catalog.list_units(content, *UNCOVERED).content_version == content.content_version
    assert catalog.list_skills(content, *UNCOVERED).content_version == content.content_version


# ---------------------------------------------------------------------------
# ServingFacade passthroughs — thin delegation, no session/consumer_id
# required (this layer stays open per KD3/R17).
# ---------------------------------------------------------------------------


def _facade(content: engine.ContentIndex) -> serving.ServingFacade:
    # No banks_dir/logs_dir needed: none of the U7 methods touch sessions
    # or the bank, so a facade constructed purely for catalog reads never
    # exercises them.
    return serving.ServingFacade(banks_dir=None, logs_dir=None, content=content, bank_lookup=lambda v: None)


def test_facade_passthroughs_delegate_to_catalog_module(content: engine.ContentIndex):
    facade = _facade(content)

    assert facade.list_curricula() == catalog.list_curricula(content)
    assert facade.list_units("IN", "NCERT", 6) == catalog.list_units(content, "IN", "NCERT", 6)
    assert facade.list_skills("IN", "NCERT", 6, unit_code=NCERT_G6_FRACTIONS_UNIT) == catalog.list_skills(
        content, "IN", "NCERT", 6, unit_code=NCERT_G6_FRACTIONS_UNIT
    )
    assert facade.get_skill_graph("FRAC.SIMPLIFY") == catalog.get_skill_graph(content, "FRAC.SIMPLIFY")
