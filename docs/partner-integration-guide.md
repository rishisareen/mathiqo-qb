# Mathiqo Partner Integration Guide

**v1, pre-stability.** `interface_version` (currently `1.0.0-pre`) is reported
by `server_info` on the MCP transport and as the app version on the REST
transport's OpenAPI document. This contract is expected to move before
general availability — bind against the declared version and design your
client to tolerate additive change, not assume today's shape is final.

This guide is for a partner AI-tutor orchestrator integrating with Mathiqo's
serving layer: the question bank and student mastery model, exposed so your
own tutoring intelligence can request work for a student and report back what
happened, without ever generating or scoring mathematics yourself.

---

## 1. What you're integrating with

Mathiqo owns two things your product doesn't have to build:

1. **A verified question bank** — every item is offline-authored, machine-
   solved, and tagged with the skill(s) it targets, its difficulty, and every
   distractor's associated misconception. You never construct or grade
   mathematics; you request items and report outcomes.
2. **A student mastery model** — per-skill ability estimates, certification
   status, and spaced-review scheduling, built from every attempt any linked
   partner has ever submitted for that student.

Both are reachable over two transports that expose the **same sixteen
operations** against the **same underlying state** (`orchestrator/serving.py`'s
`ServingFacade`):

| Transport | When to use it |
|---|---|
| **MCP** (`tools/mcp_server.py`) | Your orchestrator speaks MCP already, or you want one stdio process per partner deployment with server-side identity baked in at startup. |
| **REST** (`tools/rest_server.py`) | You need a shared, multi-tenant HTTP server fielding requests from several partner credentials, or your stack doesn't speak MCP. |

A session opened on one transport is immediately usable on the other, because
both are built from the same `ServingFacade` instance in the same process —
there is no separate state to keep in sync.

---

## 2. Authentication

### MCP

One MCP server process is started per partner, with your identity baked in at
launch:

```sh
python tools/mcp_server.py --banks-dir banks --logs-dir logs \
    --consumer-id partner.acme [--content content/fractions]
```

`--consumer-id` (your assigned partner identity) is fixed for the life of the
process. Nothing in any tool call can override it — every attempt you submit
is unforgeably attributed to you.

### REST

One HTTP server fields requests from many partners, authenticated per-request
via a bearer token resolved against a startup-configured table:

```sh
python tools/rest_server.py --banks-dir banks --logs-dir logs \
    --credentials-file credentials.json [--content content/fractions] \
    [--host 127.0.0.1] [--port 8000]
```

```json
// credentials.json — {token: consumer_id}
{
  "token-for-partner-acme": "partner.acme",
  "token-for-partner-beta": "partner.beta"
}
```

Every request must carry `Authorization: Bearer <token>`. A missing or
unrecognized token is refused with `401` before any route body — and
therefore any serving logic — runs. Your `consumer_id` is never a request
parameter on any route; it is always resolved server-side from your token.

Tokens are never echoed back anywhere: not in a response body, not in the
generated OpenAPI document (`/docs`, `/openapi.json`), not in a log line. The
bind default is loopback-only (`127.0.0.1`); production deployments put this
behind their own TLS termination.

**Out of scope for v1:** rate limiting and TLS termination are the
deploying operator's responsibility, not this server's. Treat both transports
as designed for a small set of known, credentialed partners in a trusted
deployment.

---

## 3. Student identity: registration and linking

A student exists **once** in Mathiqo, however many partner groups work with
them — a parent's app and a school's tutor share one mastery record rather
than each holding a private copy. Identity is Mathiqo-issued and opaque
(`stu-{32 hex chars}`); it is never derived from PII like an email address.

- **`register_student(attributes)`** — mint a new opaque `student_id` for a
  free-form `attributes` dict you choose (e.g. `guardian_email` +
  `family_code`). The registering consumer is auto-linked. The attributes
  themselves are stored only in Mathiqo's identity registry and are **never
  echoed back** over either transport — only the new id is returned.
- **`link_student(student_id, attributes, link_basis?)`** — a second consumer
  attaches to an existing student. This requires **re-supplying the exact
  attributes** the student was registered with — the id alone is never
  sufficient, because it circulates freely in payloads and logs and proves
  nothing about the caller's relationship to the student. `link_basis` is an
  optional, unverified provenance string you record for your own consent
  audit trail (e.g. `"guardian_authorized"`).
- **`find_student(attributes)`** — exact-match lookup only (no fuzzy
  matching); returns `found` + `student_id` if a match exists, never the
  attributes.

```python
# Parent's orchestrator (consumer_id="partner.acme") registers the child.
result = await session.call_tool("register_student", {
    "attributes": {"guardian_email": "parent@example.com", "family_code": "ABC123"},
})
student_id = result["student_id"]          # e.g. "stu-3f9a..." — opaque, safe to log

# Usable immediately by partner.acme:
await session.call_tool("open_session", {
    "student_id": student_id, "country": "IN", "curriculum": "NCERT", "grade": 6,
})

# A school's orchestrator (consumer_id="partner.schoolco") links the same
# student by re-supplying the exact registration attributes.
await session.call_tool("link_student", {
    "student_id": student_id,
    "attributes": {"guardian_email": "parent@example.com", "family_code": "ABC123"},
    "link_basis": "guardian_authorized",
})
# partner.schoolco can now open sessions and read progress/recommendations
# for this student too — one shared mastery record, per-attempt attribution.
```

**Linking is enforced everywhere it matters.** Once an identity registry is
configured, every student-scoped operation (`open_session`,
`get_candidates`, `submit_attempt`, `close_session`,
`close_session_by_student`, `get_student_progress`, `recommend_next`,
`fetch_batch`, `refill_batch`) is refused with the `not_linked` signal unless
your `consumer_id` is linked to the target student. Identity and catalog
operations (`register_student`, `link_student`, `find_student`,
`list_curricula`, `list_units`, `list_skills`, `get_skill_graph`) are the
entry point to linking and the student-independent discovery layer
respectively, so they carry no link check.

---

## 4. Curriculum discovery

Discovery is entirely **student-independent** — no session, no link check —
so you can build a curriculum picker (e.g. "Class 6 NCERT chapters") before
you know which student you're serving.

1. **`list_curricula()`** — every `(country, curriculum, grade)` triple
   Mathiqo covers, grouped `country -> curriculum -> sorted grades`.
2. **`list_units(country, curriculum, grade)`** — the chapter/unit list for
   one scope, built from structured `unit_code`/`unit_name` fields (never
   parsed out of free text). `has_units=false` for a unit-less curriculum or
   an uncovered scope — in that case `skill_ids` carries the grade's mapped
   skills directly instead. This is a **result, never an error** — an
   unrecognized scope simply comes back empty.
3. **`list_skills(country, curriculum, grade, unit_code?)`** — the mapped
   skills for a scope (optionally narrowed to one chapter), each with its
   **curriculum-local presentation**: `learning_objective`,
   `depth_expectation`, and `terminology_overrides` (curriculum-specific term
   substitutions, e.g. a different name for the same operation across
   syllabi).
4. **`get_skill_graph(skill_ids?)`** — the **canonical, curriculum-neutral**
   skill graph: `prerequisites`, `strand`, `canonical_stage` (depth axis),
   and associated `misconceptions`. Omit `skill_ids` for the whole graph. An
   unknown id is silently absent from the result rather than raising.

The distinction matters: `list_skills` is what you show a student/parent
("Multiplying fractions" in this curriculum's own words); `get_skill_graph`
is what you use to reason about prerequisites and sequencing, independent of
how any one curriculum presents a skill.

**Example flow — "show Class 6 NCERT chapters for an Indian student":**

```
list_curricula() -> confirm {"IN": {"NCERT": [6, 7, 8, ...]}} is covered
list_units(country="IN", curriculum="NCERT", grade=6) -> chapter list (unit_code/unit_name/skill_ids)
list_skills(country="IN", curriculum="NCERT", grade=6, unit_code="<chosen chapter>") -> skills to render
```

Every catalog/graph/recommendation response is stamped with `content_version`
— the content-pack build it was served from. Compare it across calls if you
cache catalog data client-side.

---

## 5. Serving loop: sessions, candidates, and batches

### 5.1 Sessions

`open_session(student_id, country, curriculum, grade)` pins a session to the
**currently active bank build** and to that curriculum context **for the
session's whole life** — every response from that session carries the same
`bank_version`. It returns:

```json
{ "session_id": "sess-...", "student_id": "stu-...", "bank_version": "..." }
```

Session lifecycle rules:

- **Conflict:** if a *different* consumer already has a live session open for
  this student, `open_session` is refused with `session_conflict` — the
  error names `close_session_by_student` as the recovery path.
- **Takeover:** the *same* consumer reopening for a student silently closes
  the prior session and opens a fresh one.
- **Idle eviction:** a session with no activity for **30 minutes**
  (`SESSION_TTL_SECONDS`) is evicted. A subsequent call against it gets
  `session_expired`, distinct from `session_unknown` (a `session_id` this
  server never issued at all).
- **Recovery:** `close_session_by_student(student_id)` closes whatever
  session is currently live for a student, keyed by student rather than
  `session_id` — the path a client that crashed and lost its `session_id`
  needs. `get_student_progress` and `recommend_next` are session-less by
  design, so they remain readable by any linked consumer even while a
  *different* consumer holds the one active serving session.

### 5.2 Two ways to get items: candidates vs. batches

**`get_candidates(session_id, skill_id)`** — every unseen instance in the
pinned bank for one `(student, skill)` pair, in a single call, no batching
logic. Returns a `signal` that is `null` in the ordinary case, or one of:

| `signal` | Meaning |
|---|---|
| `unknown_skill` | `skill_id` isn't in the loaded content graph. |
| `empty_pool` | A known skill, but the pinned bank has no instances for it. |
| `exhausted` | Every instance has already been seen; `instances` holds one least-recently-seen repeat. |

**`fetch_batch(session_id, skill_id, batch_size?)`** — the recommended path
for interactive use (iPad/tablet-facing tutoring, where round-trip latency
matters): a **server-planned, difficulty-laddered working set**, not just a
flat list. `batch_size` defaults to `10`, silently clamped to `[1, 20]`
(`DEFAULT_BATCH_SIZE` / `MAX_BATCH_SIZE`) — never rejected outright.

Each returned item carries:

- `rung`: `target` / `easier` / `harder` — which difficulty tier of the
  ladder this item fills.
- `context`: `practice` or `probe` — **the value you must submit this exact
  item back under.** `target`/`harder` rungs are always `practice`;
  `easier`/step-down rungs are always `probe`. This is a deliberate
  double-diagnosis guard: the server's own adaptive engine runs a step-down
  diagnosis internally, and tagging client-side "give an easier one" items as
  `probe` keeps your local branching from duplicating that server-side
  diagnosis.

The batch also carries a **client-side branching policy** so you don't need a
round-trip after every single answer:

```json
{
  "policy_version": "v1",
  "floor": "<fallback sequence a client that doesn't recognize policy_version should use>",
  "low_water": 3,
  "rules": {
    "on_correct": "...", "on_incorrect_tagged": "...",
    "on_incorrect_untagged": "...", "on_buffer_low": "..."
  }
}
```

`low_water` is the client-side signal for "ask for a refill soon" — it is
**not enforced server-side**; your client decides when to call
`refill_batch`.

Alongside the batch, `feedback_sidecar` maps every misconception reachable
from any served distractor in this batch to **child-facing feedback text**,
keyed by `misconception_id` (pattern `MISC.*`) — so an incorrect answer with
a tagged misconception can show feedback immediately, no extra round-trip.

**`refill_batch(session_id, skill_id, attempts_flushed)`** — re-plans a batch
against the student's *current* live state (i.e., after you've submitted the
attempts from the prior batch). `attempts_flushed` is your own protocol-level
assertion that every attempt you intend to submit before this refill already
has been — it's logged, not verified; a `false` value is not refused.

- When the freshly planned batch has **zero new items**, the response is
  `{"batch": null, "signal": "no_further_items", "supersede": ...}` instead
  of an empty batch you might mistake for "try again shortly."
- `supersede` is set on **every** refill response (`true` when the plan for
  this skill materially changed since your last batch — e.g. the student
  just certified at/above the serving ceiling, or a remediation dose is now
  queued) — check it even when `signal` is `null`.

### 5.3 Submitting attempts

`submit_attempt(session_id, attempt_id, skill_id, item_instance_id,
response_raw, response_parsed, correct, misconception_id, hints_used,
hint_auto_offered, time_taken_sec, timed_out, engaged, session_last_event,
context)` validates and logs one attempt.

**`attempt_id` is a required, client-supplied idempotency key.** Resubmitting
the same `attempt_id` — even across a session restart, once you reopen a
session and retry — returns the **original** `logged`/`repeat`/`event_id`
result unchanged rather than double-applying the attempt. Always generate a
fresh, stable id per attempt (e.g. a UUID) on the client and keep resubmitting
that same id on retry.

`item_instance_id` resolves first against your session's own pinned bank,
then — only if not found there — against whatever retained bank build it was
actually served from. This matters if the active bank build was repointed
mid-session (e.g. content team ships a new build) between when you fetched an
item and when you submit it: the item is still validated correctly against
the build it came from, rather than failing as "unresolvable."

`context` must match what the item was served under (`practice` for
target/harder-rung or `get_candidates` items; `probe` for easier/step-down
items — see §5.2).

Response shape:

```json
{ "logged": true, "repeat": false, "event_id": "..." }
```

`repeat: true` is not an error — a re-answer of an already-seen item is
accepted, just flagged. A rejected attempt raises `ingest_rejected` with a
structured reason (see §7). An attempt that passes validation but is
deliberately not logged (an abandonment-guard case) returns
`{"logged": false, "repeat": false, "signal": "discarded", "discard_reason": "..."}`
instead of an error.

### 5.4 Progress and recommendations (session-less)

- **`get_student_progress(student_id)`** — per-skill `theta` (ability
  estimate), `n` (attempts seen), `certified_depth`,
  `certification_status`, `seen_count`. Readable by any linked consumer
  regardless of who holds the active session.
- **`recommend_next(student_id, country, curriculum, grade, scope?)`** — a
  ranked, reasoned list of ready-to-learn skills, sharing the **exact same
  selection intelligence** the live serving loop uses internally — so your
  recommendations can never pedagogically diverge from what Mathiqo would
  serve directly. Each entry carries `reason`
  (`review`/`remediation`/`stepdown`/`prereq_probe`/`placement`/`frontier`),
  `context`, `target_depth`, and `unservable` (`true` when the checked bank
  has no instances for that skill yet — annotated, never silently dropped).
  `scope`, when given, narrows to one `unit_code` without changing the
  ranking rules.

---

## 6. Recommended client flow

```
list_curricula / list_units / list_skills     -- render a chapter/skill picker (no session needed)
register_student / link_student                -- establish identity once
open_session(student_id, country, curriculum, grade)
fetch_batch(session_id, skill_id, batch_size=10)
  for each answer:
    submit_attempt(..., context=<item's own context>)
    branch locally using the batch's policy object + feedback_sidecar
    when low_water is reached (or attempts_flushed is true):
      refill_batch(session_id, skill_id, attempts_flushed=true)
      if signal == "no_further_items": move to the next skill (recommend_next)
close_session(session_id)                       -- or let it idle-evict after 30 min
```

Use `recommend_next` (session-less) to decide which skill to serve next
between batches, and `get_student_progress` to render mastery UI without
needing an open session.

---

## 7. Error signal taxonomy

Every domain error maps to exactly one of **ten closed signals**
(`orchestrator/signals.py`) — the same set on both transports. On MCP, a
tool error's message is `"<signal>: <detail>"`. On REST,
`_STATUS_BY_SIGNAL` maps each to an HTTP status; the JSON body is
`{"signal": "...", "reason": "..."}` (attached to the exception detail for
non-2xx statuses).

| Signal | REST status | When |
|---|---|---|
| `unknown_skill` | 404 | `skill_id` isn't in the loaded content graph. |
| `empty_pool` | 200 (in body) | A known skill with no bank instances. |
| `exhausted` | 200 (in body) | Candidate pool fully seen; one repeat returned. |
| `no_further_items` | 200 (in body) | A refill's freshly planned batch has zero new items. |
| `session_conflict` | 409 | `open_session` found a live session for this student under a *different* consumer. |
| `session_unknown` | 404 | `session_id` was never issued by this server. |
| `session_expired` | 410 | `session_id` was issued but idle-evicted (30 min TTL). |
| `not_linked` | 403 | Your `consumer_id` has no link to the target student. |
| `ingest_rejected` | 422 | An attempt failed validation; `reason` names the failing check. |
| `discarded` | 200 (in body) | An attempt was accepted by the abandonment guard but deliberately not logged. |

`unknown_skill` on `get_candidates` is the one pool/candidate signal that is
a genuine `404` on REST rather than 200-with-signal-in-body — check the
status code, not just the body, for that one route.

A small number of errors are **not** part of this closed taxonomy (no
`signal` field; REST returns a plain `400` with `{"reason": "..."}`):
malformed/invalid identifiers, no identity registry configured, an
unregistered `student_id` or mismatched attributes passed to `link_student`,
and batch-planning errors (e.g. a served distractor with no child-facing
misconception feedback text — a content-authoring gap, not a caller-facing
signal).

---

## 8. Versioning fields

Four independent version fields appear across responses — none of them are
interchangeable, and all are "pre-stability" (expected to move before general
availability):

| Field | Where | What it tracks |
|---|---|---|
| `interface_version` | `server_info` (MCP only) | The MCP/REST contract itself — this guide's shape. |
| `content_version` | Catalog, skill-graph, recommendation, and batch responses | The content-pack manifest build (curriculum mappings, skill graph). |
| `bank_version` | Session-open, batch, and candidate responses | The specific item bank build a session is pinned to. |
| `policy_version` | The batch policy object | The client-side branching-policy shape (`rules`/`floor`/`low_water`). |

Cache aggressively on `content_version`/`bank_version` where it helps your
client, but always re-check them rather than assuming a cached shape stays
valid indefinitely — any of the four can advance independently as Mathiqo's
content and bank pipelines ship new builds.

---

## 9. Full operation reference

Every operation below is exposed identically on both transports (same
request semantics, same response shape) — MCP tool name and parameters on
the left, REST route on the right.

| Operation | MCP tool | REST route | Session? | Link check? |
|---|---|---|---|---|
| Open a session | `open_session(student_id, country, curriculum, grade)` | `POST /sessions` | — | ✓ |
| Get candidates | `get_candidates(session_id, skill_id)` | `GET /sessions/{session_id}/candidates?skill_id=` | ✓ | ✓ |
| Submit an attempt | `submit_attempt(session_id, attempt_id, skill_id, item_instance_id, ...)` | `POST /sessions/{session_id}/attempts` | ✓ | ✓ |
| Close a session | `close_session(session_id)` | `DELETE /sessions/{session_id}` | ✓ | ✓ |
| Close by student | `close_session_by_student(student_id)` | `DELETE /students/{student_id}/session` | — | ✓ |
| Get progress | `get_student_progress(student_id)` | `GET /students/{student_id}/progress` | — | ✓ |
| Recommend next | `recommend_next(student_id, country, curriculum, grade, scope?)` | `GET /students/{student_id}/recommendations?country=&curriculum=&grade=&scope=` | — | ✓ |
| Register student | `register_student(attributes)` | `POST /students` | — | — |
| Link student | `link_student(student_id, attributes, link_basis?)` | `POST /students/{student_id}/link` | — | — |
| Find student | `find_student(attributes)` | `POST /students/lookup` | — | — |
| List curricula | `list_curricula()` | `GET /catalog/curricula` | — | — |
| List units | `list_units(country, curriculum, grade)` | `GET /catalog/units?country=&curriculum=&grade=` | — | — |
| List skills | `list_skills(country, curriculum, grade, unit_code?)` | `GET /catalog/skills?country=&curriculum=&grade=&unit_code=` | — | — |
| Get skill graph | `get_skill_graph(skill_ids?)` | `GET /catalog/skill-graph?skill_ids=` | — | — |
| Fetch batch | `fetch_batch(session_id, skill_id, batch_size?)` | `POST /sessions/{session_id}/batches?skill_id=&batch_size=` | ✓ | ✓ |
| Refill batch | `refill_batch(session_id, skill_id, attempts_flushed)` | `POST /sessions/{session_id}/batches/refill?skill_id=&attempts_flushed=` | ✓ | ✓ |
| Server info | `server_info()` | *(see `/openapi.json`)* | — | — |

"Link check" applies only when the deployment has an identity registry
configured at all — otherwise every operation runs unchecked.

---

## 10. Schema reference

Every response shape below is a formal JSON Schema (Draft 2020-12) under
[`schemas/api/`](../schemas/api/), `additionalProperties: false`, `$id`
under `https://mathiqo.dev/schemas/api/...`. Treat these as the canonical
contract — response fields are exactly what's declared, no more, no less.

### `open_session` response

```json
{ "session_id": "string", "student_id": "string", "bank_version": "string" }
```

### `get_candidates` response

```json
{
  "bank_version": "string",
  "instances": [ /* full item-instance objects, see schemas/item-instance.schema.json */ ],
  "exhausted": false,
  "signal": null,
  "warning": null
}
```

### `submit_attempt` response

```json
{ "logged": true, "repeat": false, "event_id": "string" }
```

or, on a deliberate non-log:

```json
{ "logged": false, "repeat": false, "signal": "discarded", "discard_reason": "string" }
```

### `close_session` / `close_session_by_student` response

```json
{ "closed": true }
```

### `get_student_progress` response — schema: `schemas/api/` *(progress shape, keyed by skill_id)*

```json
{
  "student_id": "string",
  "skills": {
    "<skill_id>": {
      "skill_id": "string", "theta": 0.0, "n": 0,
      "certified_depth": 0.0, "certification_status": "string", "seen_count": 0
    }
  }
}
```

### `recommend_next` response — [`schemas/api/recommendation-result.schema.json`](../schemas/api/recommendation-result.schema.json)

```json
{
  "student_id": "string",
  "bank_version": "string",
  "content_version": "string",
  "recommendations": [
    {
      "skill_id": "string",
      "reason": "review | remediation | stepdown | prereq_probe | placement | frontier",
      "context": "placement | practice | review | probe",
      "target_depth": 0.0,
      "unservable": false
    }
  ]
}
```

### `register_student` response — [`schemas/api/identity-registration.schema.json`](../schemas/api/identity-registration.schema.json)

```json
{ "student_id": "stu-<32 hex>" }
```

### `link_student` response — [`schemas/api/identity-link.schema.json`](../schemas/api/identity-link.schema.json)

```json
{ "linked": true, "student_id": "stu-<32 hex>", "consumer_id": "string", "link_basis": "string | null" }
```

### `find_student` response — [`schemas/api/identity-lookup.schema.json`](../schemas/api/identity-lookup.schema.json)

```json
{ "found": true, "student_id": "stu-<32 hex> | null" }
```

### `list_curricula` response — [`schemas/api/catalog-curricula.schema.json`](../schemas/api/catalog-curricula.schema.json)

```json
{
  "content_version": "string",
  "countries": [
    { "country": "string", "curricula": [ { "curriculum": "string", "grades": [6, 7, 8] } ] }
  ]
}
```

### `list_units` response — [`schemas/api/catalog-units.schema.json`](../schemas/api/catalog-units.schema.json)

```json
{
  "content_version": "string", "country": "string", "curriculum": "string", "grade": 6,
  "has_units": true,
  "units": [ { "unit_code": "string", "unit_name": "string | null", "skill_ids": ["string"] } ],
  "skill_ids": ["string"]
}
```

`units` is populated only when `has_units` is `true`; `skill_ids` (top-level)
is populated only when `has_units` is `false`.

### `list_skills` response — [`schemas/api/catalog-skills.schema.json`](../schemas/api/catalog-skills.schema.json)

```json
{
  "content_version": "string", "country": "string", "curriculum": "string", "grade": 6,
  "unit_code": "string | null",
  "skills": [
    {
      "skill_id": "string", "depth_expectation": "string",
      "learning_objective": "string | null",
      "terminology_overrides": { "<canonical_term>": "<curriculum_local_term>" },
      "unit_code": "string | null", "unit_name": "string | null"
    }
  ]
}
```

### `get_skill_graph` response — [`schemas/api/catalog-skill-graph.schema.json`](../schemas/api/catalog-skill-graph.schema.json)

```json
{
  "content_version": "string",
  "skills": {
    "<skill_id>": {
      "skill_id": "string", "strand": "string", "canonical_stage": 0,
      "prerequisites": ["string"], "misconceptions": ["MISC.string"]
    }
  }
}
```

### `fetch_batch` response — [`schemas/api/batch-result.schema.json`](../schemas/api/batch-result.schema.json)

```json
{
  "batch_id": "batch-<32 hex>",
  "student_id": "string", "skill_id": "string",
  "items": [
    {
      "instance": { /* full item-instance object */ },
      "skill_id": "string",
      "rung": "target | easier | harder",
      "context": "practice | probe"
    }
  ],
  "count_requested": 10, "count_returned": 10,
  "policy": {
    "policy_version": "string", "floor": "string", "low_water": 3,
    "rules": {
      "on_correct": "string", "on_incorrect_tagged": "string",
      "on_incorrect_untagged": "string", "on_buffer_low": "string"
    }
  },
  "feedback_sidecar": { "MISC.<id>": "child-facing feedback text" },
  "bank_version": "string", "content_version": "string"
}
```

`items` may hold fewer than `count_requested` if the candidate pool ran out —
never padded with a repeat.

### `refill_batch` response — [`schemas/api/batch-refill-result.schema.json`](../schemas/api/batch-refill-result.schema.json)

```json
{ "batch": { /* same shape as fetch_batch, or null */ }, "signal": "no_further_items | null", "supersede": true }
```

### Item instance (nested in `instances` / batch `items[].instance`)

Full contract at [`schemas/item-instance.schema.json`](../schemas/item-instance.schema.json).
Key fields: `item_instance_id`, `template_id`, `bank_version`,
`variant_signature` (identity for "what the student actually sees", stable
across bank rebuilds of the same rendered item), `skill_ids`, `question_type`
(`mcq_4` | `numeric_entry`), `difficulty` (0-1), `language`, `locale`,
`variables`, `stem` (fully rendered, nothing left to resolve client-side),
`answer`, `distractors` (each tagged with its `misconception_id` where
applicable), `hints`, `solution_steps`.

---

## 11. Reference client snippets

### MCP (Python, `mcp` SDK)

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="python",
    args=["tools/mcp_server.py", "--banks-dir", "banks", "--logs-dir", "logs",
          "--consumer-id", "partner.acme"],
)

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        opened = await session.call_tool("open_session", {
            "student_id": student_id, "country": "IN", "curriculum": "NCERT", "grade": 6,
        })
        batch = await session.call_tool("fetch_batch", {
            "session_id": opened["session_id"], "skill_id": "NUM.FRAC.ADD", "batch_size": 10,
        })
```

### REST (curl)

```bash
curl -X POST http://localhost:8000/sessions \
  -H "Authorization: Bearer token-for-partner-acme" \
  -H "Content-Type: application/json" \
  -d '{"student_id": "stu-...", "country": "IN", "curriculum": "NCERT", "grade": 6}'

curl -X POST "http://localhost:8000/sessions/sess-.../batches?skill_id=NUM.FRAC.ADD&batch_size=10" \
  -H "Authorization: Bearer token-for-partner-acme"
```

Full auto-generated OpenAPI documentation (schemas, try-it-out, no
credentials embedded) is available at `/docs` and `/openapi.json` once your
REST server is running.

---

## 12. Known v1 limitations

Documented here so you can plan around them, not because they'll surprise
you in testing:

- **No rate limiting** on `find_student` (or any operation) — deployments
  should sit behind their own gateway-level rate limiting until a future
  version adds this natively.
- **No TLS termination** — put the REST server behind your own reverse
  proxy/load balancer for any non-loopback deployment.
- **OpenAPI schemas are auto-derived by FastAPI**, not yet wired to the
  `schemas/api/*.schema.json` files as the single source of truth — the two
  are consistent today but validated independently; prefer the
  `schemas/api/` files if you need a formal contract to validate against.
- Every field and enum in this guide is **pre-stability** — expect additive
  changes (new optional fields, new enum values) before general
  availability, and build your client to tolerate unrecognized values
  gracefully (e.g. an unrecognized `policy_version` should fall back to the
  batch's own `floor` sequence).
