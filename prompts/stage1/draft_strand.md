# Stage 1 draft — skill subgraph for strand {{strand}}

You are drafting a slice of the canonical skill graph for the Mathiqo adaptive
math platform (core design doc §1.2–§1.3) from the syllabus text below. Your
output will be machine-linted; a human reviews it only after it passes.

## Syllabus text (authoritative source)

<syllabus>
{{syllabus_text}}
</syllabus>

Draft ONLY skills that the syllabus text supports. Do not import skills from
general knowledge that the text does not treat.

## What a skill node is (§1.2)

A skill is atomic when a student can be meaningfully diagnosed as having it or
not: a single question can test it, and a student's command of it can be
tracked as one mastery state. "Fractions" is not a skill — it is a chapter.
"Add two fractions with unlike denominators" is a skill.

## Node vs. depth: the boundary rule (§1.2a)

There are two ways to make math "harder", and you must not confuse them:

- A **new node** gets its own mastery state, prerequisite edges, mappings, and
  template pool.
- **Greater depth within a node** is a harder constraint set on the same
  templates (larger numbers, more steps, more parts, word-problem framing).

The decision rule:

> A new node requires NEW CONCEPTUAL APPARATUS — a new representation, a new
> idea, or a new conceptual prerequisite the easier version did not have.
> Purely quantitative scaling of the same procedure (larger numbers, more
> steps, more parts) is depth within the node, NOT a new node.

Worked examples: adding mixed numbers is a new node (it requires
mixed↔improper conversion — new conceptual apparatus). Adding fractions with
larger denominators, or three fractions instead of two, is more depth of the
same node. Bare computation vs. word problems is same-node: word problems sit
higher on the depth axis, they are not a new skill.

**Default to depth when unsure.** Over-splitting is the worse failure mode:
a conceptual boundary hidden inside a depth band shows up cleanly in telemetry
and can be promoted to a node later, but a node that should have been a depth
band just sits there duplicating templates and cluttering diagnosis — and both
directions of migration are lossy for stored mastery. When in doubt, merge.

## Prerequisite edges

- Prerequisites are CONCEPTUAL prerequisites only. Do not add an edge for a
  range/fluency demand (e.g. "needs multi-digit multiplication to handle large
  denominators") — fluency lives on the depth axis, not in edges.
- You may reference prerequisite skills OUTSIDE this strand by id (see the
  existing-id list below). Never redefine an existing skill; reference it.
- The graph must stay acyclic.

## Misconception references

Each node's `misconceptions` list holds registry ids of its known failure
modes. Every id you reference must either already exist (see list below) or be
declared in the SAME output under an optional top-level `misconceptions:` key
(full registry entries, matching the misconception schema).

## Existing ids (reference, never redefine)

Existing skill ids:
{{existing_skill_ids}}

Existing misconception ids:
{{existing_misconception_ids}}

## Output contract

Output a single YAML mapping with a top-level `skills:` key holding a list of
skill-node objects (and optionally a `misconceptions:` key, see above). Every
skill node must validate against this JSON Schema:

```json
{{schema_json}}
```

Field-by-field:

- `skill_id` — canonical dotted identifier, UPPERCASE segments separated by
  dots: `<AREA>.<CONCEPT>[.<QUALIFIER>...]`, e.g. `FRAC.ADD.UNLIKE`,
  `FRAC.PARTWHOLE`, `NUM.LCM`. Pattern: `^[A-Z][A-Z0-9]*(\.[A-Z0-9][A-Z0-9-]*)*$`.
  Hyphens are allowed inside non-first segments (e.g. `MISC.FRAC.ADD-ACROSS`
  style), but prefer readable dot segments. Pick an id that will still make
  sense when other strands exist; do not mint a near-duplicate of an existing id.
- `name` — short human-readable name, e.g. "Add fractions with unlike
  denominators". Distinct enough from other nodes that a reviewer can tell
  them apart at a glance.
- `description` — precise definition of what the skill covers, in one or two
  sentences. This is the diagnostic contract: what can the student reliably do?
- `strand` — `{{strand}}` for nodes in this strand; use the owning strand for
  any genuinely out-of-strand node you must introduce.
- `canonical_stage` — abstract ordering index (integer ≥ 0), NOT a grade.
  Grade placement lives in the country mapping layer. Keep stages consistent
  with the prerequisite order within the strand.
- `prerequisites` — list of skill_ids (see rules above; `[]` for strand roots).
- `misconceptions` — list of registry ids (see rules above; `[]` if none known).
- No other fields. The schema forbids additional properties.

{{lint_feedback}}

Output ONLY YAML, no prose, no markdown fences, no commentary. Your output
will be machine-linted against the schema and the merged graph; if it fails,
it is bounced back to you with the lint errors attached.
