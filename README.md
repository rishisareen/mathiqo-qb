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
  multiplication and division extensions), 12 misconceptions (§2.4), and
  two country mappings (§1.4) — NCERT/India (1 row) and Ontario/Canada (13
  rows spanning grades 4–8, drafted via `--mapping-only`, see below) — the
  second country the §4 build order calls for, to force the
  localization/mapping discipline early. Still short of the §4 node/
  misconception target (25–40 nodes, ~15–20 misconceptions) — in progress.
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
- `tools/bank_build.py` — Stage 5 bank builder (see below).
- `tools/serve.py` — interactive terminal session runner (see below).
- `prompts/stage1/` — prompt templates for the Stage 1 drafting harness.
- `prompts/stage2/` — prompt template for the Stage 2 drafting harness.
- `orchestrator/` — the serve-time student model and adaptive engine
  (student-model doc; core design doc §5): `state.py` holds `SkillState`/
  `StudentModelStore` and the §7 certification status machine; `update.py`
  is the §3 θ update rule; `certification.py` is §6/§7 certification and
  review scheduling; `log.py` is the JSONL attempt log and replay;
  `engine.py` is the adaptive selection policy (content/bank loading, depth
  targeting, the R12 priority chain, diagnosis-queue consumption, replay);
  `session.py` is the interactive session loop the `serve` CLI drives (see
  below); `constants.py` collects the v1 placeholder tuning constants.
- `tests/` — pytest suite for the linter, the harnesses, the student-model
  state container, the update/certification/log/engine/session units, plus
  fixture-vs-schema validation.

## Setup

Requires Python ≥3.10 (needed by the `mcp` SDK used in `tools/mcp_server.py`).

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

# a new country/curriculum whose syllabus only touches skills the graph
# already has (the common case once a strand's canonical nodes exist):
python tools/stage1_draft.py --strand NUMBER.FRACTIONS \
    --syllabus examples/ontario-fractions-excerpt.txt \
    --content-dir content/fractions --staging-dir staging/ \
    --country CA --curriculum ONTARIO --mapping-only --backend cmd
```

`--mapping-only` skips the strand/misconceptions steps and drafts just the
mapping — added after discovering that without it, mapping a second country
onto an already-complete strand forced fabricating filler skill/misconception
content just to satisfy the non-empty-response shape check (there was
nothing new to draft; the graph is country-agnostic, core doc §1.1).

Workflow: draft → lint-bounce loop → accepted drafts land in
`staging/accepted/` with a manifest (prompt hash, retries used, lint output);
drafts that exhaust retries are quarantined in `staging/rejected/` with the
error report. The harness never writes into `content/` — a human reviews and
promotes accepted drafts into `content/` by hand, then re-runs
`tools/graph_lint.py` on the merged result. `examples/ncert-fractions-excerpt.txt`
and `examples/ontario-fractions-excerpt.txt` are placeholders; replace them
with the real syllabus text before a genuine run.

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

## Stage 5 bank builder

`tools/bank_build.py` materializes promoted item templates
(`content/templates/*.yaml`) into an atomic, versioned bank of concrete,
dressed, schema-valid item instances (core doc §3 Stage 5, §5.1): every
template is re-verified against the Stage 3 gate, every valid non-colliding
variable combination is enumerated, the pool is capped and sampled
deterministically (same inputs → byte-identical rebuilds), and each sampled
variant is dressed with a locale pack and fully computed — rendered stem,
exact answer, and the complete tagged distractor set — offline. This is the
boundary that makes the Generator→Orchestrator split real: "everything a
child can see is materialized offline," so serving is always a lookup, never
composition (§5.1's boundary rule, R11).

```sh
python tools/bank_build.py --templates-dir content/templates \
    --out banks --bank-version 2026.07-dev [--pool-cap 40] \
    [--locale-pack content/locales/en-IN.yaml]
```

Banks are never patched in place: `--bank-version` refuses to overwrite an
existing build, and the build writes to a staging directory under `--out`
before renaming it into place atomically. Only a `HardFail` (schema
violation, unsatisfiable constraints, unresolvable slot) refuses the build;
a `FLAGGED_FOR_REGENERATION` template (already promoted despite the flag —
see the Stage 3 section above) still builds.

## Session runner

`tools/serve.py` is the interactive terminal session runner (core doc §5.2,
KTD1: chosen over a minimal local web page) that closes the
Generator→Orchestrator loop: it serves real bank instances end to end —
renders the item (MCQ options shuffled deterministically per instance, or a
numeric-entry prompt), runs the hint ladder with an automatic offer at
~1.5× the item's expected time (§2.3), times out an unanswered item, parses
the response (integers, `a/b` fractions, and `W n/d` mixed numbers — exact
rational comparison), matches a wrong pick to its distractor's misconception
tag, shows feedback with solution steps, and writes the attempt event. All
the loop logic lives in `orchestrator/session.py`, built on the U1–U5
student-model/engine modules (`orchestrator/update.py`,
`orchestrator/certification.py`, `orchestrator/log.py`,
`orchestrator/engine.py`); `tools/serve.py` itself is wiring only.

```sh
# resolves the bank via --banks-dir's ACTIVE pointer (default: banks/):
python tools/serve.py --banks-dir banks --student stu-1 \
    --country IN --grade 6 --log logs/stu-1.jsonl \
    [--session-time 2026-07-27T12:00:00]

# --bank is an explicit override, bypassing ACTIVE entirely:
python tools/serve.py --bank banks/2026.07-dev --student stu-1 \
    --country IN --grade 6 --log logs/stu-1.jsonl
```

`--log` is both appended to (one JSONL line per scored attempt) and replayed
at startup, so re-running the same command resumes the student exactly where
they left off — including any pending review/remediation/step-down/
prerequisite probe the engine had queued (KTD11). `--session-time` overrides
the session's single "now" (KTD10) with a fixed ISO 8601 timestamp for
reproducible runs; omitted, it defaults to real wall-clock time. Typing
`hint` always shows the next hint; `quit` ends the session immediately
without logging or applying whatever item was on screen.

Known v1 limitation: `estimated_time_sec` lives on the item *template*
(`schemas/item-record.schema.json`) but is not carried onto bank instances
by `tools/bank_build.py` (`schemas/item-instance.schema.json` has no such
field) — the session runner falls back to a fixed default
(`orchestrator/constants.py`'s `DEFAULT_ESTIMATED_TIME_SEC`) for every
item's hint-offer and timeout thresholds rather than extending U3's bank
builder/schema, which is out of this unit's scope.

Under the hood, `orchestrator/session.py` routes both selection (the
single-skill steps of the R12 priority chain) and every attempt submission
through `orchestrator/serving.py`'s transport-free facade — the same
surface the MCP server (below) exposes to external orchestrators.

## Bank activation

`tools/bank_activate.py` (KTD3) atomically points a banks directory's
`ACTIVE` file at a built version — what `open_session` (the facade,
`tools/serve.py`, the MCP server) pins new sessions to. A session already
open keeps whatever build it originally pinned; only sessions opened after
the repoint see the change (R4).

```sh
python tools/bank_activate.py --banks banks --bank-version 2026.07-dev
```

Refuses (exit 1) to activate a version not on disk under `--banks`, leaving
`ACTIVE` unchanged.

## Replay audit

`tools/replay_audit.py` (U7) is the serving-integrity contract's acceptance
bar as a runnable check (R10): for every event in a student's attempt log,
it resolves the stamped `bank_version` against any retained build (KTD7,
not only the currently active one), verifies the instance and its
`variant_signature` against that build's own record (reusing
`orchestrator/ingest.py`'s KTD5 checks), then reports the student's
reproduced theta/certification/seen-count summary — the same shape a live
`get_student_progress` call returns.

```sh
python tools/replay_audit.py --log logs/stu-1.jsonl --banks-dir banks
```

Exits non-zero (naming every failing event) if any event fails to resolve;
zero and prints the summary on a clean audit.

## MCP server

`tools/mcp_server.py` (U8) is the external product surface: the same
`orchestrator/serving.py` facade the reference orchestrator uses, exposed
over MCP so external AI-tutor orchestrators can serve Mathiqo's question
bank and student model directly (STRATEGY.md's infra-first bet). It is a
thin adapter — every tool call delegates to exactly one facade operation,
and no tool reaches past the facade to touch a bank directory or log file
directly.

```sh
python tools/mcp_server.py --banks-dir banks --logs-dir logs \
    --consumer-id partner.acme [--content content/fractions]
```

**Tool surface** (KTD6's parity rule: this is the same set of operations
the reference orchestrator itself uses):

| Tool | Delegates to | Notes |
|---|---|---|
| `open_session(student_id)` | `ServingFacade.open_session` | Pins the session to the currently active bank build (R3); returns `session_id` and `bank_version`. |
| `get_candidates(session_id, skill_id)` | `ServingFacade.get_candidates` | Every unseen instance for (student, skill); on exhaustion, the one least-recently-seen repeat plus an explicit signal (R1). |
| `submit_attempt(session_id, skill_id, item_instance_id, ...)` | `ServingFacade.submit_attempt` | Validates and logs the attempt (R5, R6); `consumer_id` is this server's own `--consumer-id`, never a tool parameter — a caller cannot forge attribution. |
| `close_session(session_id)` | `ServingFacade.close_session` | Drops the session from this server's in-memory registry. |
| `get_student_progress(session_id)` | `ServingFacade.get_progress` | The declared summary shape: theta, certification status, per-skill seen count. |
| `server_info()` | — | Reports the declared interface version and the facade-operation surface. |

**v1 is pre-stable.** `server_info`'s `interface_version` may still change
before general availability — bind against the declared version and expect
it to move, not assume today's shape is final. Auth/transport hardening
(credentials, rate limiting, TLS termination) is explicitly out of scope
for v1: this server assumes a small set of known, credentialed partners in
a trusted deployment, one `--consumer-id` per server process.

## Run the tests

```sh
.venv/bin/python -m pytest tests/ -q
```

The broken-fixture tests (cycle, dangling refs) operate on copies of
`content/fractions/` in a temp dir — the real fixture is never modified.
