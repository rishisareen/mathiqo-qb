# Stage 1 draft — curriculum mapping for {{country}} / {{curriculum}}

You are drafting rows of the Mathiqo country mapping layer (core design doc
§1.4) for country `{{country}}`, curriculum `{{curriculum}}`, from the
syllabus text below. Your output will be machine-linted; a human reviews it
only after it passes.

## Syllabus text (authoritative source)

<syllabus>
{{syllabus_text}}
</syllabus>

## What a mapping row is (§1.4)

The canonical skill graph is country-agnostic; the mapping layer says WHEN and
HOW FAR each curriculum takes each skill. The same skill node (and the same
template pool) serves every country — only the mapping and the locale pack
differ. Curricula spiral: a skill may have several mapping rows per country,
one per grade that touches it, with `depth_expectation` recording how far that
grade actually takes it. `depth_expectation` is the engine's default serving
ceiling, not an absolute one.

## Verifiability rule (hard constraint)

Every grade placement, `syllabus_ref`, and `learning_objective` must be
verifiable against the syllabus text provided above. NEVER invent placements
not supported by the input — no "typically grade 5" from general knowledge, no
chapter numbers you cannot see in the text. If the text does not support a
placement for a skill, omit that row entirely. An incomplete mapping is
acceptable; an unverifiable one is not.

## Skill ids in scope (only these may be mapped)

{{existing_skill_ids}}

## Output contract

Output a single YAML mapping with a top-level `mappings:` key holding a list
of mapping rows. Every row must validate against this JSON Schema:

```json
{{schema_json}}
```

Field-by-field:

- `skill_id` — a skill id from the in-scope list above.
- `country` — `{{country}}` (ISO 3166-1 alpha-2).
- `curriculum` — `{{curriculum}}`.
- `grade` — integer 0–10 (0 = Kindergarten, K-10 platform), the grade of
  treatment stated or clearly implied by the syllabus text.
- `depth_expectation` — one of `easy`, `medium`, `hard`: how far this grade
  takes the skill, judged from the syllabus text's expectations.
- `syllabus_ref` — citation into the provided text, e.g. "Class 6, Maths,
  Ch. 7" or a standard code like "5.NF.A.1" if the text uses one.
- `learning_objective` — the curriculum's own phrasing of the outcome, quoted
  or minimally paraphrased from the syllabus text.
- `exam_contexts` (optional) — rigor wrappers (§2.10), UPPER_SNAKE, e.g.
  SCHOOL_EXAM, OLYMPIAD_IMO_L1. Only include what the curriculum context
  justifies.
- `terminology_overrides` (optional) — canonical term → local term, e.g.
  {"simplest form": "lowest form"}, only for phrasing visible in the text.
- `pedagogy_notes` (optional) — how this curriculum teaches the method, per
  the text.
- No other fields. The schema forbids additional properties.

{{lint_feedback}}

Output ONLY YAML, no prose, no markdown fences, no commentary. Your output
will be machine-linted against the schema and the merged graph; if it fails,
it is bounced back to you with the lint errors attached.
