# Stage 2 draft — one item template for {{skill_id}}

You are drafting ONE item template for the Mathiqo adaptive math platform
(core design doc §2.1–§2.2) for the skill below. Your output will be
machine-verified (schema validation, then exhaustive enumeration of every
variable combination with symbolic answer-checking, core doc §3 Stage 3); a
human samples and reviews it only after it passes.

## The skill this template measures

```
skill_id: {{skill_id}}
{{skill_definition}}
```

## Known misconceptions for this skill (design distractors against these)

{{skill_misconceptions}}

Distractors should be *designed*, not incidental: where a wrong answer would
plausibly result from one of the misconceptions above, write the distractor
expression to produce exactly that wrong value and tag it with the
misconception's id. An untagged distractor (plausible wrong answer with no
known cause) is allowed but carries no diagnostic value (core doc §2.4–§2.5).
Never invent a `misconception_id` that isn't in the list above.

## Exam contexts on file for this skill (from the mapping layer, if any)

{{exam_contexts_hint}}

Pick `exam_contexts` from this list where it fits; if nothing is on file,
default to `["SCHOOL_EXAM"]` — standard procedural application, not an
Olympiad-style wrapper (core doc §2.10).

## v1 scope restrictions (hard constraints)

- `question_type` MUST be `mcq_4` or `numeric_entry` — v1 restricts to these
  two types (core doc §4). Prefer `mcq_4` unless the skill is explicitly
  suited to a numeric-entry mastery check (§2.7).
- `skill_ids` MUST be exactly `["{{skill_id}}"]` — single-skill only. Multi-skill
  (exam-context) items are out of scope for this harness.
- Difficulty tiers are controlled by swapping the constraint set, not by
  writing a new stem (§2.3): pick ONE tier for this draft and set
  `difficulty_prior` to match it (`easy`, `medium`, or `hard`).
- Variable domains must be small integers (v1 Stage 3 enumerates exhaustively,
  §3 Stage 3) — keep ranges narrow enough that the full Cartesian product is
  well under 10⁶ combinations (a handful of variables over single-digit-to-
  low-double-digit ranges, matching the worked example in core doc §2.2).
- English only (`en-IN`) for `stem_i18n`, `solution_steps_i18n`, `hints_i18n`
  — v1 ships English-only (core doc §4), though the schema is i18n-keyed from
  day one.
- `solution_steps_i18n` is keyed by pedagogy variant; use `canonical` unless
  you have a specific reason to key by curriculum.

## Existing template ids (never reuse; propose a new one)

{{existing_template_ids}}

`template_id` pattern: `TPL.<SKILL-ID-WITH-DOTS>.<SHORT-TAG>.<NNN>`, e.g.
`TPL.FRAC.ADD.UNLIKE.WORD.001` for a word-problem template on
`FRAC.ADD.UNLIKE`. The tag should hint at the item's flavor (WORD, DIRECT,
CMP, …); the running number disambiguates multiple templates with the same
tag on the same skill.

## What you draft vs. what the pipeline fills in

You draft the CONTENT of the template — everything a question actually needs
to exist. The pipeline fills in publishing scaffolding (`template_version`,
`bank_version`, `graph_version`, `tts`, numeric `difficulty`, `provenance`)
after your draft passes verification — do not include those fields.

## Output contract

Output a single YAML mapping with exactly these keys, each validating against
the corresponding property of this JSON Schema (core doc §2.2, §5.3):

```json
{{schema_json}}
```

Field-by-field:

- `template_id` — see the id rules above.
- `skill_ids` — exactly `["{{skill_id}}"]` (see v1 restrictions above).
- `question_type` — `mcq_4` or `numeric_entry` (see v1 restrictions above).
- `cognitive_level` — e.g. `"application"`, `"recall"`, `"procedural"` (§2.2).
- `difficulty_prior` — `easy`, `medium`, or `hard` — the tier this draft's
  constraint set targets (§2.3).
- `estimated_time_sec` — integer ≥ 1; realistic time for a student at this
  skill and tier.
- `calculator_allowed`, `diagram_required` — booleans.
- `accepted_answer_forms` — optional free text, e.g. `"simplest form only"`
  (§2.2), when the skill has a canonical answer form worth stating.
- `context_tags` — surface contexts for locale-pack rotation, e.g.
  `["food", "sharing"]` (§2.7). Do not name specific people, places, or
  currencies here — that's the locale pack's job; use `{{name1}}`,
  `{{name2}}`, `{{currency}}`-style slots in the stem instead, declared in
  `localization_slots`.
- `exam_contexts` — see the hint above; array of `UPPER_SNAKE` ids.
- `variables` — object of `{name: {type: "int", range: [lo, hi]}}` (v1 Stage 3
  only enumerates typed int variables with a range, §3 Stage 3).
- `constraints` — array of expression strings over the variables (e.g.
  `"a < b"`, `"gcd(b, d) == 1"`) that together define this draft's difficulty
  tier (§2.3). Whitelisted functions: `gcd`, `lcm`, `simplify`.
- `stem_i18n` — `{"en-IN": "..."}`. Use `{{a}}`-style slots for variables and
  `{{name1}}`-style slots for localization slots.
- `localization_slots` — array of slot names the stem uses that are NOT
  template variables (names, food, currency, …).
- `answer` — a machine-evaluable expression over the variables, e.g.
  `"simplify((a*d + c*b) / (b*d))"`. Whitelisted functions: `gcd`, `lcm`,
  `simplify`.
- `distractors` — array of `{expr, misconception_id, only_if?, unsimplified?}`
  (see the misconception rules above). `expr` must be a machine-evaluable
  expression; for an `unsimplified: true` distractor, `expr` must be a single
  numerator/denominator quotient (not a sum) so its displayed form is
  well-defined. `only_if` is an optional guard expression — the distractor is
  only offered when it evaluates true for a given variable combination (§2.2).
  `numeric_entry` items may use an empty `distractors: []` if there's nothing
  to tag; `mcq_4` items need exactly 3 distractors (1 answer + 3 distractors
  = 4 options).
- `solution_steps_i18n` — `{"en-IN": {"canonical": ["step 1", "step 2", ...]}}`
  — ordered, one string per step (§2.2).
- `hints_i18n` — `{"en-IN": ["hint 1", "hint 2", ...]}` — ordered lightest to
  strongest (§2.3).

{{verify_feedback}}

Output ONLY YAML, no prose, no markdown fences, no commentary. Your output
will be schema-validated and then exhaustively verified (core doc §3 Stage 3:
every variable combination enumerated, every answer and distractor solved
symbolically); if it fails, it is bounced back to you with the failure
reasons attached — including, when your draft is merely *flagged* for a high
constraint-rejection or distractor-collision rate rather than hard-failed,
the specific rate and which distractor is colliding, so you can narrow ranges
or fix distractor logic rather than redraft blindly.
