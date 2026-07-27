"""Model constants for the student model (student-model doc §2–§8).

Every value here is a v1 placeholder awaiting pilot telemetry (student-model
doc §10: "all placeholders awaiting pilot telemetry"). They are named and
collected in this one module precisely so the pilot's tuning pass has a
single place to touch. S is the exception: it is a *scale choice*, not a
tuning constant — change it only together with the difficulty scale (§2).
"""

from __future__ import annotations

# --- §2: the logistic scale -------------------------------------------------
# E(θ, b) = 1 / (1 + 10^(−(θ − b)/S)) on the D ∈ [0,1] axis.
S: float = 0.25

# Mastery at depth d ⟺ E(θ, d) ≥ 0.8 ⟺ θ ≥ d + 0.602·S ≈ d + 0.15 (§2).
MASTERY_E_THRESHOLD: float = 0.8
MASTERY_MARGIN: float = 0.602 * S  # ≈ 0.15; 0.602 ≈ log10(4)

# --- §3: the update rule ----------------------------------------------------
# K = K₀ / (1 + n/n₀), floored at K_min.
K0: float = 0.15
N0: float = 4.0
K_MIN: float = 0.02

# Effective scores (§3 outcome table).
SCORE_CLEAN: float = 1.0
SCORE_HINT_ASSISTED: float = 0.5
SCORE_WRONG: float = 0.0

# Evidence weights (§3): multi-skill clean success is full score at half
# weight; lesson attempts are full score at low weight; a single-skill
# hint-assisted attempt also weighs half — same number as MULTI_SKILL_WEIGHT
# by coincidence of the doc's placeholders, not by definition, so it gets
# its own name.
MULTI_SKILL_WEIGHT: float = 0.5
LESSON_WEIGHT: float = 0.25
HINT_ASSISTED_WEIGHT: float = 0.5

# Engine identity (§9 engine_version): stamped into every attempt event so a
# later scoring-rule change is visible in the log, not silently reinterpreted
# on replay (KTD7).
ENGINE_VERSION: str = "1.0.0"

# MCQ guessing adjustment (§3): for 4-option MCQ, E′ = 0.25 + 0.75·E.
MCQ4_QUESTION_TYPE: str = "mcq_4"
MCQ4_GUESS_FLOOR: float = 0.25

# Low-guess response formats (§3, §7; core doc §2.3 as amended in v0.4.3):
# unadjusted E, and the only formats that can certify.
LOW_GUESS_FORMATS: frozenset = frozenset(
    {"numeric_entry", "drag_and_drop", "visual_counting"}
)

# --- §6: forgetting and review ----------------------------------------------
# Spacing-literature convention, not our data (§6 honest caveat).
REVIEW_INITIAL_INTERVAL_DAYS: float = 7.0
REVIEW_INTERVAL_MULTIPLIER: float = 2.5
REVIEW_MAX_INTERVAL_DAYS: float = 90.0

# --- §4: misconception trigger ----------------------------------------------
# A counter reaching MISC_TRIGGER_COUNT within the last MISC_WINDOW attempts
# on the skill triggers remediation.
MISC_TRIGGER_COUNT: int = 2
MISC_WINDOW: int = 5

# --- §8: migrations -----------------------------------------------------------
# Node split: the above-threshold node is re-scored from historical attempts
# and seeded with n = min(n_historical, MIGRATION_SEED_N_CAP) — "the prior
# exists but barely argues" (§4).
MIGRATION_SEED_N_CAP: float = 2.0

# --- U5: adaptive engine (SM §5 cold-start placement; core doc §1.2a/§2.5) ---
# Tier label -> D on the difficulty scale (core doc §1.2a/§2.3), aligned with
# the authored template difficulties (content/templates/*.yaml: 0.2 for easy,
# 0.5 for medium; there is no v1 "hard" template yet, so 0.8 is a placeholder
# used only as a ceiling/ordering value, not something any template targets).
TIER_DEPTH: dict = {"easy": 0.2, "medium": 0.5, "hard": 0.8}
TIER_ORDER: tuple = ("easy", "medium", "hard")

# KTD9: nearest-difficulty targeting serves the un-seen instance closest to
# theta + CHALLENGE_OFFSET (capped by the serving ceiling) — a small nudge
# into "slightly harder than current mastery" territory. A placeholder like
# every other tuning constant in this file, awaiting pilot telemetry.
CHALLENGE_OFFSET: float = 0.1

# KTD12: a confirmed misconception (2-in-MISC_WINDOW on the triggering skill)
# fires a bounded dose of practice on the registry's mapped prerequisite
# rather than open-ended remediation — "micro-explainer first, full
# re-teaching only if the pattern persists" (core doc §2.4).
REMEDIATION_DOSE_SIZE: int = 3

# SM §5 point 3: placement stops walking a strand once the skill currently
# being probed has certified prerequisites and has itself accumulated this
# many attempts — that skill becomes the frontier (the starting practice
# queue), even if it hasn't certified.
PLACEMENT_FRONTIER_N: float = 3.0
