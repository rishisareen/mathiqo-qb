# Mathiqo

Adaptive K-10 math question-bank platform. This repo currently holds the
authoring-side pipeline: the JSON Schemas that define the content contracts,
the graph linter and verification gates, the Stage 1/2 AI-drafting harnesses,
the v1 fractions fixture, and the beginnings of the serve-time student model.
The specification is the two design docs in the repo root
(`mathiqo-qb-core-design.md`, `mathiqo-student-model.md`); schema field names
follow them exactly.

## Layout

- `schemas/` — JSON Schema (draft 2020-12), one file per artifact:
  skill nodes, misconceptions, curriculum mappings, item records
  (Generator→Orchestrator contract), attempt events, lesson-exposure events
  (a sibling event type, not an attempt), and a stub lesson schema.
- `content/fractions/` — the v1 fixture in YAML: the fractions skill graph
  (14 nodes — the original 10-node §1.3 cluster plus Stage 1-drafted
  multiplication and division extensions), 12 misconceptions (§2.4), one
  NCERT mapping (§1.4). Still short of the §4 build-order target (25–40
  nodes, ~15–20 misconceptions, two countries) — in progress.
- `content/templates/` — item templates: the §2.2 worked example (still
  `FLAGGED_FOR_REGENERATION` by design, see below) plus 22 Stage 2-drafted
  templates. Every one of the 14 skills has at least one template in a
  low-guess format (`numeric_entry`) — the only formats that can certify
  mastery (student-model doc §7) — so certification is exercisable
  end-to-end across the whole graph, not just diagnosis via MCQ. Still short
  of the §4 target (~150–300 templates, 5–15 per skill) — in progress.
- `tools/graph_lint.py` — linter for skill-graph content directories.
- `tools/stage1_draft.py` — Stage 1 drafting harness (see below).
- `tools/stage2_draft.py` — Stage 2 template-drafting harness (see below).
- `tools/item_verify.py` — Stage 3 template verification gate (see below).
- `prompts/stage1/` — prompt templates for the Stage 1 drafting harness.
- `prompts/stage2/` — prompt template for the Stage 2 drafting harness.
- `orchestrator/` — the serve-time student model (student-model doc):
  `state.py` holds `SkillState`/`StudentModelStore` and the §7 certification
  status machine, `constants.py` collects the v1 placeholder tuning
  constants. The §3 update rule (`θ ← θ + K·(s − E)`) is not yet
  implemented — this is state and status-machine only, in progress.
- `tests/` — pytest suite for the linter, the harnesses, the student-model
  state container, plus fixture-vs-schema validation.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run the linter

```sh
python tools/graph_lint.py content/fractions/
# treat warnings (orphans, near-duplicates) as failures:
python tools/graph_lint.py content/fractions/ --warnings-as-errors
```

Exit code is non-zero on any error; zero when clean.

## Stage 1 drafting harness

`tools/stage1_draft.py` automates Stage 1 of the content pipeline (core doc
§3): an LLM drafts a strand's skill subgraph, misconception registry entries,
and (optionally) a country mapping from syllabus text, and `graph_lint.py` is
the trust gate every draft must pass. Failed drafts are bounced back to the
LLM with the lint errors verbatim, up to `--max-retries` times.

Prompt templates live in `prompts/stage1/` with `{{placeholder}}` slots; the
driver fills in the syllabus text, the strand, the ids already in the content
dir (so drafts reference rather than redefine existing objects), and the
relevant JSON Schema inline.

Two backends, both dependency-free:

- `--backend cmd` — runs the shell command in `STAGE1_LLM_CMD` with the prompt
  on stdin and expects YAML on stdout. Wire any CLI/API wrapper this way.
- `--backend file` — writes prompts to `<staging-dir>/prompt-<n>.md` and
  consumes `<staging-dir>/response-<n>.yaml`, which a human or agent fills in
  (re-runs resume after the last consumed response). `--print-prompts` writes
  the first-round prompts and exits without calling any backend.

```sh
# fully automatic, via a wired-up LLM CLI:
export STAGE1_LLM_CMD="my-llm-cli --model xyz"
python tools/stage1_draft.py --strand NUMBER.FRACTIONS \
    --syllabus examples/ncert-fractions-excerpt.txt \
    --content-dir content/fractions --staging-dir staging/ \
    --country IN --curriculum NCERT --backend cmd

# human/agent-in-the-loop:
python tools/stage1_draft.py ... --backend file --print-prompts
# fill staging/response-1.yaml, response-2.yaml, ... then re-run without --print-prompts
```

Workflow: draft → lint-bounce loop → accepted drafts land in
`staging/accepted/` with a manifest (prompt hash, retries used, lint output);
drafts that exhaust retries are quarantined in `staging/rejected/` with the
error report. The harness never writes into `content/` — a human reviews and
promotes accepted drafts into `content/` by hand, then re-runs
`tools/graph_lint.py` on the merged result. `examples/ncert-fractions-excerpt.txt`
is a placeholder; replace it with the real syllabus text before a genuine run.

## Stage 2 drafting harness

`tools/stage2_draft.py` automates Stage 2 of the content pipeline (core doc
§3): for one skill, an LLM drafts item templates — stem, variables,
constraints, answer, tagged distractors, solution steps, hints (§2.2) — and
`tools/item_verify.py` is the trust gate every draft must pass. Unlike Stage
1's binary lint result, `item_verify` has three verdicts; both `HARD_FAIL`
and `FLAGGED_FOR_REGENERATION` bounce the draft back to the LLM, and a
`FLAGGED` bounce carries the actual rejection/collision rate (and which
distractor is colliding), not just a verdict, so a regeneration can narrow
ranges or fix distractor logic instead of guessing blindly.

The LLM drafts only the template's *content*; the harness fills in
publishing scaffolding no author invents by hand — `template_version`,
`bank_version`, `graph_version`, `tts`, the numeric `difficulty` scalar
(derived from the authored `difficulty_prior` band), and `provenance`.
`provenance.reviewer` records who ran the batch, not a completed Stage 4
human review — that gate is separate and still manual; accepted drafts land
in `staging/accepted/`, never in `content/templates/`.

v1 scope restrictions this harness enforces beyond the bare item-record
schema (core doc §4): `question_type` must be `mcq_4` or `numeric_entry`;
`skill_ids` must be exactly the one skill being drafted for (single-skill
items only, no exam-context multi-skill drafting here).

Same two backends as Stage 1 (reused directly), except the cmd backend reads
`STAGE2_LLM_CMD`:

```sh
# fully automatic, via a wired-up LLM CLI:
export STAGE2_LLM_CMD="my-llm-cli --model xyz"
python tools/stage2_draft.py --skill FRAC.ADD.UNLIKE \
    --content-dir content/fractions --templates-dir content/templates \
    --staging-dir staging/ --count 5 --reviewer your-name --backend cmd

# human/agent-in-the-loop:
python tools/stage2_draft.py ... --backend file --print-prompts
# fill staging/response-1.yaml, response-2.yaml, ... then re-run without --print-prompts
```

Workflow: draft → verify-bounce loop → accepted drafts land in
`staging/accepted/template-<n>.yaml` with a manifest (prompt hash, retries
used, the full verify report); drafts that exhaust retries are quarantined
in `staging/rejected/` with the error report. The harness never writes into
`content/templates/` — a human reviews and promotes accepted drafts by hand,
then re-runs `tools/item_verify.py` on the promoted result.

## Stage 3 verification harness

`tools/item_verify.py` is the trust gate for item templates (core doc §3
Stage 3): it schema-validates a template, then exhaustively enumerates every
variable combination, checks constraint satisfiability, solves every answer
key symbolically with exact rational arithmetic (sympy), and measures the
exact distractor-collision rate. Distractors whose `only_if` guard doesn't
fire are never offered; variants where a distractor collides with the answer
(or another distractor) are dropped.

```sh
python tools/item_verify.py content/templates/TPL.FRAC.ADD.UNLIKE.WORD.001.yaml [--report out.json]
```

Exit codes: `0` PASS, `1` HARD_FAIL (schema violation, unsatisfiable
constraints, undeclared slots), `2` FLAGGED_FOR_REGENERATION (constraint-
rejection or collision rate over 30% — heavy dropping skews the variant
distribution). Language-level and embedding-duplicate checks are v1 stubs
(cited as future work in §3 Stage 3).

Note: the §2.2 example fixture currently verifies as FLAGGED — its five
constraints reject 82% of the 6400 combinations (1,148 valid variants
remain, 0 collisions). The gate is working as specified; whether to narrow
the ranges or accept the flag is a content decision, not a code one.

## Run the tests

```sh
.venv/bin/python -m pytest tests/ -q
```

The broken-fixture tests (cycle, dangling refs) operate on copies of
`content/fractions/` in a temp dir — the real fixture is never modified.
