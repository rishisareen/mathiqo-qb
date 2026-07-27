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
# weight; lesson attempts are full score at low weight.
MULTI_SKILL_WEIGHT: float = 0.5
LESSON_WEIGHT: float = 0.25

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
