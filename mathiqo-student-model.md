# Mathiqo — Student Model

**Scope of this document:** what the platform knows about a student and how every answer changes it — the state variables, the update rule, placement, forgetting, certification, migrations, and the attempt event schema everything else logs against. Companion to *Mathiqo Question Bank — Core Design* v0.6 ("the core doc"; its §5 names this document's runtime host: the student model is the state the Orchestrator's adaptive engine reads and writes); section references (§2.3, §2.5…) point there.

**Status:** Draft v0.2.3 · July 2026
**Changelog:** v0.2 review fixes: multi-skill success updates at half-weight with full score, not half score — the v0.1 rule lowered θ on clean successes whenever E > 0.5 (§3); logistic scale set to S = 0.25 so the mastery margin (0.602·S ≈ 0.15) is reachable on the D ∈ [0,1] axis — at S = 1 no depth above ≈0.4 was certifiable (§2); certification generalized from numeric entry to low-guess response formats per core doc v0.4.3 (§2, §7); abandonment guard on timeouts + `session_id` added to the attempt schema (§3, §9). v0.2.1 review fixes: lesson attempts specified as full score at weight w = 0.25, never certifying (§3); guessing-adjustment prose synced to low-guess formats (§3); lesson-exposure event type added so core §6.3's efficacy/A-B readout is computable (§9); `bank_version` added per core §5.3 (§9); θ declared unbounded above the [0,1] difficulty scale (§2); mid-session no-interaction timeouts marked deliberate (§3); companion version bumped to core v0.6. v0.2.2 review fix: explicit precedence — `context: lesson` suspends the hint-assisted scoring row (s = 1 for correct regardless of hint use; the w = 0.25 weight already prices in scaffolding), preventing double-penalty if the table were implemented literally (§3). v0.2.3 review fixes: §1's state table given a footnote naming the implementation bookkeeping fields it doesn't list (review interval, lapsed flag, the misconception window, graph_version) and why they don't break the "nothing else" claim (§1); §7 states explicitly that a `due` certification satisfies prerequisite checks the same as `certified` (§6); §10 flags the MCQ-only guessing adjustment as undecided beyond v1's MCQ + numeric-entry scope (§3).

**Design principle:** the model is small, explicit, and inspectable. Every number it stores must be reconstructible from the attempt log alone — no hidden state, no black-box inference — because parents, schools, and regulators will all eventually ask "why does the app think my child can't do fractions?", and the answer must be an audit trail, not a shrug.

---

## 1. State: what is stored per (student, skill)

| Field | Meaning |
|---|---|
| `θ` | ability estimate, on the same scale as item difficulty (core doc §2.3) |
| `n` | effective evidence count (weighted attempts) — drives confidence |
| `certified_depth` | the depth at which mastery is currently certified, if any |
| `review_due` | next review date for that certification |
| `misc_counters` | per-misconception evidence counters (core doc §2.4) |

Nothing else *the model reasons about*. Misconceptions are *evidence*, not state — a counter, not a hidden psychological variable. Exam-context performance is not separate state either: an Olympiad item updates the same canonical θ (core doc §2.10). One scale, one estimate per skill.

An implementation additionally carries a few bookkeeping fields the rules above need in order to be computed incrementally rather than replayed from the log on every read: a review interval and a `lapsed` flag (§6's status machine, formalized in §7), the sliding window of recent event ids the `misc_counters` 2-in-5 trigger reads (§4), and the `graph_version` each record was earned under (§8). None of this is a new variable the model reasons about — every one of these fields is itself reconstructible from the attempt log alone (§9) — but the table above names the model's variables, not a store's schema, so it's worth being explicit that the two aren't the same list.

## 2. θ and the mastery margin

θ and item difficulty b live on one scale. Expected probability of an unassisted correct response:

```
E(θ, b) = 1 / (1 + 10^(−(θ − b)/S))
```

v1 uses this Elo-style form with **S = 0.25** on a D-aligned scale (b and θ both expressed in difficulty units, core doc §2.3); graduate to fitted 1PL/IRT once Stage 6 telemetry supports it. The form matters more than the letter: logistic, monotone, one parameter per side, cheap to update online.

**Why S = 0.25 and not 1:** the margin below is 0.602·S, and S must be small relative to the scale's tier width (≈0.33 on D ∈ [0,1]). At S = 1 the margin is 0.602 — certifying depth 0.5 would require θ = 1.102, off the top of the scale, and a student a full tier stronger than d would sit at E ≈ 0.68, still below the bar. At S = 0.25 the margin is ≈ 0.15 and a one-tier gap gives E ≈ 0.95 — both sane. S is a scale choice, not a tuning constant: change it only together with the scale. (Equivalent alternative: S = 1 with θ, b on a 0–10 axis; v1 picks the [0,1] axis + S = 0.25 and states it once, here.)

**Mastery at depth d** ⟺ E(θ, d) ≥ 0.8, i.e. θ ≥ d + 0.602·S ≈ d + 0.15 — the core doc's margin (§2.3), with its two qualifications (unassisted only; certified on low-guess response formats) made operational in §7 below. θ is expressed in difficulty units but is **not bounded by [0,1]**: the margin is headroom by design — certifying the hardest authored depth (say d = 0.9) requires θ ≈ 1.05, and that overshoot is exactly what "certified" means. b stays in [0,1]; θ does not. Note what this buys: mastery claims are *checkable*. The model predicts ≥80% success on unseen items at d, and the telemetry loop can test exactly that prediction. That is the seed of the pilot's efficacy measurement.

## 3. The update rule

Every attempt updates θ for the served skill:

```
θ ← θ + K · (s − E)
```

where `s` is the *effective score*:

| Outcome | s | Rationale |
|---|---|---|
| Clean correct (unassisted) | 1.0 | full evidence |
| Hint-assisted correct | 0.5 | half-credit: better than wrong, worse than clean — the core doc's three-way update (§2.3) as one number |
| Wrong / timeout | 0.0 | full negative evidence; timeouts additionally flagged for the §2.5 step-down path |

**Abandonment guard.** A timeout only scores 0.0 if the student was *engaged*: any interaction on the item (an entered digit, a selected option, a hint opened). A timeout with no interaction that is also the session's last event is **discarded** — the child closed the app for dinner; that is not evidence about fractions. The guard needs `session_id` (§9) to evaluate "last event of session." A *mid-session* no-interaction timeout does score 0.0 — the auto-hint at ~1.5× expected time (core doc §2.3) fired and was ignored, which is evidence — and that default is deliberate and revisitable with data.

The fractional score collapses the three-way update into a single rule with no special cases — and produces the right behavior at the margins: if E = 0.9 and the student needed hints, then s − E < 0 and θ *falls slightly*. Needing scaffolding on an item you were expected to solve cleanly is negative evidence, and the model should say so.

**Guessing adjustment (MCQ).** For 4-option MCQ, replace E with E′ = 0.25 + 0.75·E: a pure guesser earns s − E′ = 0 on average, so correct guesses are discounted automatically rather than by a separate mechanism. Low-guess formats — numeric entry, drag-to-construct, visual counting — use unadjusted E (no guess floor). This is the mechanical half of the core doc's rule that MCQ moves θ but low-guess formats certify (§2.3, §2.7 as amended in v0.4.3); §7 is the other half.

**Multi-skill items** (core doc §2.10): on clean success, each listed skill updates with **full score at half weight** — θ ← θ + 0.5·K·(1 − E), n += 0.5. Evidence *weight* and effective *score* are different axes: a clean success is full-quality evidence that happens to be shared across skills. (The v0.1 rule — s = 0.5 as a score — lowered θ on clean successes whenever E > 0.5, i.e. punished competent students for succeeding. Hint-assisted s = 0.5 is different and stands: there the intermediate score *is* the intended semantics — scaffolded success is genuinely intermediate evidence.) On failure nothing updates and single-skill probes are queued, per the credit-assignment rule. Diagnosis still happens only on single-skill items.

**Lesson attempts** (core doc §6.1; `context: lesson`): full score at low weight — θ ← θ + w·K·(s − E) with **w = 0.25**, n += w. Same axes as the multi-skill rule: guided practice inside a lesson is full-quality evidence in deliberately small quantity, because the answer was just taught. **Precedence:** in `context: lesson` the hint-assisted row of the effective-score table is suspended — a correct answer scores s = 1 regardless of hint use, because lesson hints are pre-opened by design and the low weight already prices in the scaffolding. (Implementing the table as written would double-penalize every lesson attempt.) Lesson attempts never certify (§7) — certification requires unassisted performance.

**Depth-step-down probes need no special case.** A deep failure (θ moves down) followed by a shallow pass (θ moves up) settles θ between the two depths — the "reliable up to here" diagnosis *emerges* from the update rule. The §2.5 flowchart decides *which* items to serve; this rule decides what serving them *means*.

**K: how fast θ moves.** K = K₀ / (1 + n/n₀), floored at K_min. Early attempts move θ a lot (placement, new skills); established estimates move slowly and shrug off single outliers. v1 starts at K₀ = 0.15, n₀ = 4, K_min = 0.02 — placeholders to be tuned on pilot data, flagged as such, not defended. (K reads against the scale: on D ∈ [0,1] with tier width ≈0.33, K₀ = 0.15 means an early attempt moves θ up to half a tier — fast placement without single-attempt tier jumps. The v0.1 value of 0.3 predated the S fix in §2 and was nearly a full tier per attempt.)

`n` increments by the update's weight: 1.0 for clean or wrong unassisted attempts, 0.5 for hint-assisted and multi-skill updates. This keeps n an honest "how much do we actually know" counter rather than an attempt counter.

## 4. Confidence is just n

v1 carries no separate uncertainty variable; n plays that role everywhere it is needed:

- **Slip vs. pattern** (core doc §2.5): a misconception counter must reach 2 within the last 5 attempts on the skill before remediation triggers. Tunable, but the point is the rule is written down.
- **Migration seeding** (core doc §3 Stage 6): "seeded, not assumed" = recompute θ from historical above-threshold attempts, then set n = min(n_historical, 2) — the prior exists but barely argues.
- **Placement** (§5): probing in a strand stops once n ≥ 3 at the frontier skill.

If telemetry later shows n is too blunt — it can't express "30 attempts, but a year ago" — the fix is recency-weighting n, which §6's review machinery largely covers already.

## 5. Cold start: placement

New students need initial (θ, n) per skill. Rules:

1. **Prior from the mapping.** A student enrolled at grade g starts probing each strand one tier *below* their grade's `depth_expectation` for its frontier skills (core doc §1.4). Cheap, usually close, and self-correcting within a few items because K is large when n ≈ 0.
2. **Walk the graph, not the syllabus.** Per strand: serve a mid-chain skill at the prior depth. Pass → move to its dependents; fail → walk back along prerequisite edges. This is the same machinery as §2.5 diagnosis, running in forward gear.
3. **Stop at the frontier.** Per strand, placement ends when the first uncertified skill with certified prerequisites is found (n ≥ 3 there). That frontier *is* the starting practice queue.
4. Placement items are ordinary items (MCQ + numeric entry), flagged `context: placement` so Stage 6 can exclude them from item-difficulty calibration if their selection effects warrant it.

Placement is per-strand by design: a child ahead in geometry and behind in fractions is the normal case, and the graph is what makes that representable.

## 6. Forgetting and review

Without decay, "mastered" is a claim about the past presented as a claim about the present. v1 models forgetting as **expiring certification**, not decaying θ — simpler, and it routes through machinery that already exists:

1. Certification at depth d comes with a review interval, initially 7 days.
2. On the due date, one probe: unassisted, low-guess format (§7), at the certified depth. **Pass** → interval ×2.5 (cap 90 days), θ unchanged. **Fail** → treated as a wrong attempt (θ updates, interval resets to 7 days) and the skill re-enters the practice queue.
3. **Dependents block, they don't revoke.** A lapsed prerequisite stops its dependents from being *served* (the §1.3 sequencing rule reads certification state, which has now lapsed) but their certifications are untouched — the child re-earns the prerequisite and resumes where they were. No cascade of revoked progress in the UI, ever.
4. Review probes are served *before* new skills in a session. Review is the rent on mastery; new learning doesn't jump the queue.

Honest caveat: the interval schedule (7d, ×2.5, 90d cap) is spacing-literature convention, not our data. Stage 6 telemetry on probe pass rate by interval is how it earns its numbers.

## 7. Certification: the status machine

θ is a continuous estimate; **certification** is the derived status the rest of the system actually consults:

```
uncertified → certified(d) → due → lapsed
```

- **Set only by** unassisted outcomes on **low-guess response formats** — numeric entry, drag-to-construct, visual counting (core doc §2.3 as amended in v0.4.3; keeps Classes 1–3 certifiable through their §2.9 interactive types) — at depth ≥ d with E(θ, d) ≥ 0.8.
- **Consulted by:** prerequisite checks ("certified at the required depth"), the self-extending ceiling (certification at the top of a band opens the next band, §1.4), review scheduling (§6), and parent/teacher summaries — a certified skill is reported as "secure"; θ is never shown to humans.
- **A `due` certification satisfies a prerequisite check the same as `certified`.** Only `lapsed` blocks dependents from being served (§6, point 3); `due` means a review probe is pending, which is not itself a lapse. So prerequisite checks read `certified` and `due` as equivalent — the status distinction matters for review scheduling (§6), not for gating.
- **Never** set by MCQ, never by multi-skill items, never inherited across a graph migration (§8).

## 8. Migrations

The core doc's rules (§3 Stage 6), restated in state terms. Every (student, skill) record carries `graph_version`. On a **node split**: the below-threshold node keeps (θ, n, certification); the above-threshold node gets θ re-scored from historical attempts, n = min(n, 2), certification cleared — probed on next encounter. On a **merge**: the surviving node inherits the lower child state. Migrations apply lazily, on the student's next session touching the affected skills — no batch rewrites of live student state.

## 9. The attempt event schema — the load-bearing artifact

Everything above is computable from this log; anything not in this log is uncomputable later. One row per attempt:

| Field | Why it exists |
|---|---|
| `event_id`, `student_id` (pseudonymous), `session_id`, `timestamp` | identity & ordering; `session_id` powers the abandonment guard (§3) and session-level analyses |
| `item_instance_id`, `template_id`, `template_version`, `bank_version`, `engine_version` | provenance & reproducibility (core doc §3 Stage 5; `bank_version` per §5.3) |
| `skill_ids`, `question_type`, `depth_served`, `b_prior_at_serve` | what was measured |
| `exam_context`, `language`, `locale` | slicing; context effects |
| `response_raw`, `response_parsed`, `correct` | the outcome |
| `misconception_id` (nullable) | distractor tag matched (§2.4) |
| `hints_used`, `hint_auto_offered`, `time_taken_sec`, `timed_out` | assistance & load evidence |
| `context`: placement / practice / review / probe / lesson | selection-effect bookkeeping; `lesson` attempts (guided practice inside a lesson, core doc §6) update θ at low weight and never certify |
| `θ_before`, `θ_after`, `n_before` | model auditability — every update replayable |
| `graph_version` | migration correctness (§8) |

**Lesson exposure is a sibling event type, not an attempt.** Lesson views carry `lesson_id`, `lesson_variant`, `segments_completed`, `timestamp`, and the A/B assignment arm; practice attempts join against the most recent exposure for their skill. Without it, the core doc's lesson-efficacy loop (§6.3 — post-lesson first-attempt success, script A/B, no-lesson holdout) is uncomputable, and exposure assignment is the one datum no later cleverness can reconstruct.

Compliance note: pseudonymous student IDs, no free-text fields that could carry PII, retention and consent per COPPA / DPDP / UK AADC. That work is owned outside this document but constrains it: nothing in the schema requires identifying a child.

## 10. Deliberately undecided

- **Item selection policy** beyond prerequisite gating (rule-based vs. bandit vs. simpler — choose honestly once pilot data exists).
- **Exact constants** (K₀, n₀, K_min, hint weight 0.5, lesson weight 0.25, interval schedule) — all placeholders awaiting pilot telemetry.
- **Forgetting of θ itself** — v1 decays certification only; if review-pass data shows stale θ, revisit.
- **Cross-skill transfer** — does practicing `NUM.LCM` raise `FRAC.ADD.UNLIKE` θ directly? v1: no, only through certification gating. Revisit with data.
- **Efficacy metrics and pilot design** — separate plan; §2's "mastery claims are checkable" is the seed of it.
- **Guessing adjustment beyond MCQ** — §3 defines the E → E′ discount only for 4-option MCQ. Other guessable-but-not-low-guess formats (e.g. true/false, ~50% floor) have no adjustment defined. Unexercised while v1 is scoped to MCQ + numeric entry (core doc §4); revisit before the type mix widens, or a guessable format will move θ unadjusted.
