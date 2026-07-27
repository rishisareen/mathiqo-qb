# Stage 1 draft — misconception registry entries for strand {{strand}}

You are seeding entries for the Mathiqo misconception registry (core design
doc §2.4) covering the strand below, drawing on published math-education
research on student misconceptions. Your output will be machine-linted; a
human reviews it only after it passes.

## What a misconception entry is (§2.4)

Misconceptions are first-class objects in a shared registry. Each one is a
specific, observable faulty rule a student appears to hold — not a vague
"struggles with fractions". Each entry points back into the skill graph: a
wrong answer is evidence for a specific broken prerequisite, not a −1 on a
score.

Draft only misconceptions that are well-attested in the math-ed literature or
directly implied by the skills in scope. Quality over quantity: three sharp,
diagnosable entries beat ten generic ones.

## Remediation targets

Every `remediation_target` must be the skill_id of a node in the graph —
either an existing skill or one drafted for this strand in the same Stage 1
run. The target is the node that gets re-taught when the misconception is
confirmed (usually the conceptual prerequisite whose absence produces the
error, not the skill where the error surfaces). Never point at a skill that
does not exist.

## Skill ids in scope (valid remediation targets)

{{existing_skill_ids}}

## Existing misconception ids (do not redefine or near-duplicate these)

{{existing_misconception_ids}}

## Output contract

Output a single YAML mapping with a top-level `misconceptions:` key holding a
list of registry entries. Every entry must validate against this JSON Schema:

```json
{{schema_json}}
```

Field-by-field:

- `misconception_id` — `MISC.<AREA>.<SHORT-NAME>`, e.g.
  `MISC.FRAC.ADD-ACROSS`, `MISC.FRAC.BIGGER-DENOM`. Pattern:
  `^MISC\.[A-Z0-9][A-Z0-9.-]*$`. Internal vocabulary — humans only ever see
  `parent_summary_i18n`.
- `belief` — the faulty rule the student appears to hold, quoted as the
  student might think it, e.g. "Add tops, add bottoms".
- `signature_error` — the characteristic wrong output that exposes the belief,
  e.g. "1/2 + 1/3 = 2/5". It must be something a distractor can be built from.
- `remediation_target` — a skill_id from the in-scope list above.
- `remediation_asset` — pointer to the ~30-second micro-explainer addressing
  exactly this misconception, by convention
  `micro-explainers/<misconception_id>.v1`.
- `parent_summary_i18n` — plain-language rendering for parent/teacher reports,
  keyed by locale (at least `en-IN`). Use {{name}} for the child's name, e.g.
  "{{name}} understands adding fractions, but tends to add the denominators
  when they don't match." Write it as a kind, specific observation, not a
  verdict.
- No other fields. The schema forbids additional properties.

{{lint_feedback}}

Output ONLY YAML, no prose, no markdown fences, no commentary. Your output
will be machine-linted against the schema and the merged graph; if it fails,
it is bounced back to you with the lint errors attached.
