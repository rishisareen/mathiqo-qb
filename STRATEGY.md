---
last_updated: 2026-07-28
---

# Mathiqo — Strategy

## Target problem

Teams building math learning products — especially agentic AI tutors — have no
trustworthy math layer to build on: LLM-generated content can't be allowed to
reach children unverified, hand-built banks don't diagnose *why* a child is
wrong, and mastery scores are black boxes no parent, school, or regulator can
audit. Children get marked wrong; nobody finds the broken prerequisite.

## Our approach

Be the trust layer for AI-era math education: generation happens offline behind
a 100%-verification gate, serving is select-only (nothing generative in the
child-facing path, ever), and every mastery claim is reconstructible from the
attempt log. The question bank and student model are the product, exposed to
outside orchestrators as versioned, schema-backed interfaces (MCP + API); our
own app is the reference consumer that proves the loop.

## Who it's for

**Primary:** builders of agentic AI tutors, who hire Mathiqo — via the MCP
server — to ground their tutor: fetch verified items, read/write per-student
mastery state, and keep generation out of the child-facing path.
**Secondary:** our own consumer app (the proof), and through it parents and
children mastering the syllabus.

## Key metrics

1. **Diagnosis yield** (headline, weekly) — share of wrong answers matching a
   misconception-tagged distractor; untagged wrongs are diagnostic noise.
   Computed from the attempt log.
2. **Mastery calibration accuracy** (monthly) — observed success on unseen
   items at certified depth vs the predicted ≥80%. The trust promise, tested.
3. **External attempt volume** (weekly) — attempt events logged by orchestrators
   other than our own app. Adoption of the interface surface.

## Tracks

1. **Content & graph coverage** — canonical skill graphs, misconception
   registry, templates, verified bank builds (Stage 1–5). Class 6 NCERT map
   landed; templates per strand next.
2. **The interface surface** — MCP server + API over the two contracts
   (item-instance read path, attempt-event/student-model write path), bank
   versioning, external-consumer requirements (seen-filter, version pinning).
3. **Serving & telemetry loop** — reference orchestrator, diagnosis flows,
   Stage 6 calibration; the in-house proof that the infra teaches.
4. **Trust & compliance** — verification hardening, audit trail, human review
   gates, COPPA / DPDP / UK AADC; the track B2B buyers ask about by name.
