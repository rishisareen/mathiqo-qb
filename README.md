# Mathiqo

Adaptive K-10 math question-bank platform. This repo currently holds the first
authoring-side artifacts: the JSON Schemas that define the content contracts,
the graph linter, and the v1 fractions fixture. The specification is the two
design docs in the repo root (`mathiqo-qb-core-design.md`,
mathiqo-student-model.md); schema field names follow them exactly.

## Layout

- `schemas/` — JSON Schema (draft 2020-12), one file per artifact:
  skill nodes, misconceptions, curriculum mappings, item records
  (Generator→Orchestrator contract), attempt events, lesson-exposure events
  (a sibling event type, not an attempt), and a stub lesson schema.
- `content/fractions/` — the v1 fixture in YAML: the 10-node fractions
  cluster (core doc §1.3), the 4 misconceptions (§2.4), one NCERT mapping (§1.4).
- `content/templates/` — item templates; currently the §2.2 worked example
  as the Stage 3 fixture.
- `tools/graph_lint.py` — linter for skill-graph content directories.
- `tools/stage1_draft.py` — Stage 1 drafting harness (see below).
- `tools/item_verify.py` — Stage 3 template verification gate (see below).
- `prompts/stage1/` — prompt templates for the drafting harness.
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
