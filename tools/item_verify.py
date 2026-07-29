"""Stage 3 verification gate (core design doc §3 Stage 3).

Loads an item template (the Generator→Orchestrator contract, §5.3), validates
it against ``schemas/item-record.schema.json``, then EXHAUSTIVELY enumerates
the Cartesian product of the declared variable domains — v1 domains are small
integers, so one pass proves constraints satisfiable, verifies every answer
key symbolically, and measures the exact distractor-collision rate (no
sampling error, §3 Stage 3).

Per valid variant:
  - the answer expression is evaluated symbolically with exact rational
    arithmetic (``a/b`` is the fraction a/b, never float division);
  - each distractor expression is evaluated (guarded distractors only when
    their ``only_if`` holds for that combo);
  - the variant is DROPPED when a distractor's displayed form equals the
    answer's, or two distractors' displayed forms coincide.

"Displayed form" convention: an ordinary distractor's displayed form is its
*reduced* rational value (``_rational_key``) — the answer is always shown
simplified, so a distractor value-equal to it is ambiguous no matter how the
distractor itself would render, and comparing reduced values is the safe
direction there. An ``unsimplified: true`` distractor's displayed form is its
literal, unreduced numerator/denominator split (``_unsimplified_key``) —
that distractor's whole point is a value that coincides with the answer while
looking different, so it must be compared by what a child would actually see.
One consequence: two ordinary distractors that display differently but reduce
to the same value are still flagged as colliding — conservative (never misses
a real collision), occasionally over-flags relative to literal display text.

Verdicts (CLI exit codes):
  PASS                      (0)
  HARD_FAIL                 (1) schema violation, unparseable expression,
                                unsatisfiable constraints, unresolvable slots
  FLAGGED_FOR_REGENERATION  (2) constraint-rejection rate or collision rate
                                above 30% — heavy dropping silently skews the
                                variant distribution (§3 Stage 3)

Language-level-vs-grade detection is out of scope for v1 (see the stub hook
near the bottom of this file). Embedding-based duplicate detection (KTD2) IS
implemented, but deliberately not as a literal embedding index: this repo
has zero ML dependencies, so the duplicate gate is deterministic composite
text-similarity — the same SequenceMatcher-at-0.85 convention
tools/graph_lint.py:68-71 already uses as the sanctioned stand-in for this
exact design-doc §3 check. See ``check_embedding_duplicates`` below.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Callable, Iterable, Mapping

import jsonschema
import sympy
import yaml
from sympy.core.function import AppliedUndef
from sympy.logic.boolalg import Boolean
from sympy.parsing.sympy_parser import parse_expr

REPO_ROOT = Path(__file__).resolve().parent.parent
ITEM_RECORD_SCHEMA = REPO_ROOT / "schemas" / "item-record.schema.json"

# §3 Stage 3: "flag the whole template for regeneration when the collision or
# constraint-rejection rate is high (say >30%)".
REJECTION_FLAG_THRESHOLD = 0.30
COLLISION_FLAG_THRESHOLD = 0.30

PASS = "PASS"
FLAGGED = "FLAGGED_FOR_REGENERATION"
HARD_FAIL = "HARD_FAIL"

EXIT_CODES = {PASS: 0, HARD_FAIL: 1, FLAGGED: 2}

# Whitelisted functions usable in template expressions. Parsed as *undefined*
# (lazy) sympy functions so nothing evaluates at parse time — sympy.gcd of two
# symbols is 1 and Ne(b, d) of two symbols is True, both wrong here. The lazy
# calls are materialized only after integer values are substituted.
_FUNCTION_IMPLS: dict[str, Callable[..., sympy.Basic]] = {
    "gcd": sympy.gcd,
    "lcm": sympy.lcm,
    "simplify": sympy.simplify,
}

_SLOT_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class HardFail(Exception):
    """A verification failure no amount of regeneration retries fixes inline."""


# ---------------------------------------------------------------------------
# Derived values (solution-step/hint slots that are neither template variables
# nor locale slots, e.g. {{lcm}} in §2.2). Each rule names the variables it
# needs; a derived name is available only when all of them are declared.
# v1 ships the unlike-fraction-addition family used by §2.2 — new template
# families extend this registry.
# ---------------------------------------------------------------------------
DerivedRule = tuple[frozenset[str], Callable[[Mapping[str, int]], object]]

DERIVED_VALUE_RULES: dict[str, DerivedRule] = {
    "lcm": (
        frozenset({"b", "d"}),
        lambda v: sympy.lcm(v["b"], v["d"]),
    ),
    "a2": (
        frozenset({"a", "b", "d"}),
        lambda v: v["a"] * (sympy.lcm(v["b"], v["d"]) // v["b"]),
    ),
    "c2": (
        frozenset({"b", "c", "d"}),
        lambda v: v["c"] * (sympy.lcm(v["b"], v["d"]) // v["d"]),
    ),
    "sum": (
        frozenset({"a", "b", "c", "d"}),
        lambda v: (
            v["a"] * (sympy.lcm(v["b"], v["d"]) // v["b"])
            + v["c"] * (sympy.lcm(v["b"], v["d"]) // v["d"])
        ),
    ),
    "final": (
        frozenset({"a", "b", "c", "d"}),
        lambda v: sympy.Rational(v["a"], v["b"]) + sympy.Rational(v["c"], v["d"]),
    ),
}


@dataclass
class VerifyReport:
    template_id: str
    total_combos: int
    valid_combos: int
    rejected_combos: int
    rejection_rate: float
    dropped_variants: int
    drops_by_distractor: dict[str, int]
    collision_rate: float
    verdict: str
    reasons: list[str] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Safe expression parsing / exact rational evaluation
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _parse(expr: str, var_names: tuple[str, ...]) -> sympy.Basic:
    """Parse a template expression without executing arbitrary code.

    Uses sympy's AST-based parser (never ``eval``) with only the declared
    variable symbols and whitelisted functions in scope, ``evaluate=False``
    so relationals (``==``, ``!=``, ``<`` …) survive parsing unevaluated.
    """
    local_dict: dict[str, object] = {name: sympy.Symbol(name) for name in var_names}
    local_dict.update({name: sympy.Function(name) for name in _FUNCTION_IMPLS})
    try:
        return parse_expr(expr, local_dict=local_dict, evaluate=False)
    except Exception as exc:  # SympifyError, SyntaxError, TokenError, ...
        raise HardFail(f"unparseable expression {expr!r}: {exc}") from exc


def _materialize(expr: sympy.Basic) -> sympy.Basic:
    """Replace lazy whitelisted function calls with their sympy implementations."""
    return expr.replace(
        lambda e: isinstance(e, AppliedUndef) and e.func.__name__ in _FUNCTION_IMPLS,
        lambda e: _FUNCTION_IMPLS[e.func.__name__](*e.args),
    )


def evaluate_expression(expr: str, values: Mapping[str, int]) -> sympy.Basic:
    """Evaluate a template expression over integer values, exactly.

    Substitution produces sympy Integers, so ``a/b`` is the Rational a/b:
    ``1/2 + 1/3`` is exactly 5/6, never a float.
    """
    parsed = _parse(expr, tuple(sorted(values)))
    subs = {sympy.Symbol(name): sympy.Integer(value) for name, value in values.items()}
    try:
        # .doit() forces evaluation of operations kept lazy by evaluate=False
        # (matters for variable-free expressions, where subs substitutes nothing).
        return _materialize(parsed.subs(subs)).doit()
    except HardFail:
        raise
    except Exception as exc:
        raise HardFail(f"expression {expr!r} failed at {dict(values)}: {exc}") from exc


def evaluate_condition(expr: str, values: Mapping[str, int]) -> bool:
    """Evaluate a constraint/guard expression to a Python bool over Rationals."""
    result = evaluate_expression(expr, values)
    if isinstance(result, Boolean):
        return bool(result)
    raise HardFail(f"condition {expr!r} did not evaluate to a boolean at {dict(values)}")


def _rational_key(value: sympy.Basic) -> tuple:
    """Displayed-form key of a reduced value: ("rat", p, q) for p/q, q > 0."""
    if isinstance(value, sympy.Rational):  # includes Integer (q == 1)
        return ("rat", int(value.p), int(value.q))
    return ("raw", str(value))


def _unsimplified_key(expr: str, values: Mapping[str, int]) -> tuple:
    """Displayed-form key of an ``unsimplified: true`` distractor.

    The numerator and denominator are evaluated separately as integers so the
    fraction is NOT reduced: (a*d + c*b)/(b*d) at a=2,b=4,c=1,d=3 keys as
    10/12, which is distinct from the simplified answer key 5/6 — that
    distractor's whole point (MISC.FRAC.NO-SIMPLIFY) is that the *displayed
    form* differs while the value coincides.

    ``sympy.fraction`` only splits a single num/den quotient correctly; on a
    non-quotient shape (e.g. a sum of two fractions) it returns the whole
    expression as the "numerator" over denominator 1, which would silently
    key as the wrong value instead of raising. Cross-check the split against
    the expression's own evaluated value and hard-fail on mismatch rather
    than shipping a fabricated displayed form.
    """
    parsed = _materialize(_parse(expr, tuple(sorted(values))))
    subs = {sympy.Symbol(name): sympy.Integer(value) for name, value in values.items()}
    try:
        num, den = sympy.fraction(parsed)
        n, d = int(num.subs(subs)), int(den.subs(subs))
    except (TypeError, ValueError) as exc:
        raise HardFail(
            f"unsimplified distractor {expr!r} is not a single numerator/denominator "
            f"quotient sympy can split at {dict(values)}: {exc}"
        ) from exc
    if d < 0:
        n, d = -n, -d
    plain_value = evaluate_expression(expr, values)
    if not (isinstance(plain_value, sympy.Rational) and sympy.Rational(n, d) == plain_value):
        raise HardFail(
            f"unsimplified distractor {expr!r} numerator/denominator split ({n}/{d}) "
            f"does not match its evaluated value ({plain_value}) at {dict(values)}: "
            "unsimplified distractors must be a single num/den quotient expression"
        )
    return ("rat", n, d)


def _distractor_key(distractor: Mapping[str, object], values: Mapping[str, int]) -> tuple:
    if distractor.get("unsimplified"):
        return _unsimplified_key(str(distractor["expr"]), values)
    return _rational_key(evaluate_expression(str(distractor["expr"]), values))


# ---------------------------------------------------------------------------
# Distractor offering (only_if guards) and per-variant collision check
# ---------------------------------------------------------------------------

def distractor_offered(distractor: Mapping[str, object], values: Mapping[str, int]) -> bool:
    """True when the distractor is constructible for this variant (§2.2 only_if)."""
    guard = distractor.get("only_if")
    if guard is None:
        return True
    return evaluate_condition(str(guard), values)


def check_variant(template: Mapping[str, object], values: Mapping[str, int]) -> list[int]:
    """Indices of offered distractors that collide for this variant.

    A variant is dropped when any offered distractor's displayed form equals
    the answer's, or two offered distractors' displayed forms coincide
    (§3 Stage 3). Empty list means the variant is kept.
    """
    answer_key = _rational_key(evaluate_expression(str(template["answer"]), values))
    offered: list[tuple[int, tuple]] = []
    for i, distractor in enumerate(template["distractors"]):  # type: ignore[index]
        if distractor_offered(distractor, values):
            offered.append((i, _distractor_key(distractor, values)))

    colliding: set[int] = set()
    for i, key in offered:
        if key == answer_key:
            colliding.add(i)
    for pos, (i, key_i) in enumerate(offered):
        for j, key_j in offered[pos + 1 :]:
            if key_i == key_j:
                colliding.update((i, j))
    return sorted(colliding)


# ---------------------------------------------------------------------------
# Locale slot completeness and step/hint slot resolution
# ---------------------------------------------------------------------------

def _slot_names(texts: Iterable[str]) -> set[str]:
    return {name for text in texts for name in _SLOT_RE.findall(text)}


def _i18n_strings(node: object) -> list[str]:
    """Flatten stem_i18n / solution_steps_i18n / hints_i18n to plain strings."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [s for value in node.values() for s in _i18n_strings(value)]
    if isinstance(node, list):
        return [s for value in node for s in _i18n_strings(value)]
    return []


def check_slots(template: Mapping[str, object]) -> None:
    """§3 Stage 3 locale slot completeness (cheap static check).

    Every {{slot}} in the stem must be a declared variable or a declared
    localization slot. Steps and hints may additionally reference derived
    values computable from the declared variables (DERIVED_VALUE_RULES).
    Anything else is a hard fail — an unrenderable slot ships broken text.
    """
    variables = set(template["variables"])  # type: ignore[index]
    locale_slots = set(template.get("localization_slots") or [])
    derived = {
        name
        for name, (required, _fn) in DERIVED_VALUE_RULES.items()
        if required <= variables
    }

    stem_slots = _slot_names(_i18n_strings(template["stem_i18n"]))
    undeclared = stem_slots - variables - locale_slots
    if undeclared:
        raise HardFail(
            f"stem references undeclared slots {sorted(undeclared)}: "
            "not in localization_slots and not a template variable"
        )

    body_slots = _slot_names(
        _i18n_strings(template["solution_steps_i18n"])
        + _i18n_strings(template["hints_i18n"])
    )
    unresolvable = body_slots - variables - locale_slots - derived
    if unresolvable:
        raise HardFail(
            f"solution_steps/hints reference unresolvable slots {sorted(unresolvable)}: "
            "not a variable, localization slot, or computable derived value"
        )


# ---------------------------------------------------------------------------
# Template loading and the exhaustive enumeration
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _item_record_schema() -> dict:
    return json.loads(ITEM_RECORD_SCHEMA.read_text(encoding="utf-8"))


def validate_against_item_schema(data: dict, label: str) -> None:
    """Hard-fail unless ``data`` validates against ``schemas/item-record.schema.json``.

    Factored out of ``load_template`` so callers with an in-memory draft dict
    — e.g. tools/stage2_draft.py, validating an LLM-drafted template before
    ever writing it to disk — can reuse the same check ``load_template`` does
    for a file. ``label`` identifies the subject in the error message (a
    path for ``load_template``, a template_id or draft slot for a harness).
    """
    validator = jsonschema.Draft202012Validator(_item_record_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise HardFail(f"{label}: schema violation(s): {details}")


def load_template(path: Path) -> dict:
    """Load a template YAML and hard-fail unless it validates against the schema."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HardFail(f"{path}: top level must be a mapping")
    validate_against_item_schema(data, str(path))
    return data


def build_domains(template: Mapping[str, object]) -> dict[str, list[int]]:
    """Variable domains as inclusive integer lists ([lo, hi] per §2.2)."""
    domains: dict[str, list[int]] = {}
    for name, spec in template["variables"].items():  # type: ignore[index]
        if spec.get("type") != "int" or "range" not in spec:
            raise HardFail(
                f"variable {name!r}: v1 Stage 3 enumerates only int variables with a range"
            )
        lo, hi = (int(x) for x in spec["range"])
        if lo > hi:
            raise HardFail(f"variable {name!r}: empty range [{lo}, {hi}]")
        domains[name] = list(range(lo, hi + 1))
    return domains


def verify_template(
    template: Mapping[str, object],
    corpus: Iterable[Mapping[str, object]] = (),
) -> VerifyReport:
    """Run the full Stage 3 gate on an already schema-valid template dict.

    ``corpus`` is the bank of other templates to check for near-duplicates
    against (KTD2's ``check_embedding_duplicates``) — e.g. every other
    template in the same ``--templates-dir`` walk, or the ``--corpus`` CLI
    flag's directory. Default ``()`` (no corpus) preserves exact prior
    behavior for every existing caller that verifies a single template in
    isolation: the embedding-duplicate check is skipped (listed in
    ``skipped_checks``, matching the pre-KTD2 stub's always-empty result)
    rather than silently running against nothing.
    """
    template_id = str(template["template_id"])
    domains = build_domains(template)
    var_names = tuple(domains)

    check_slots(template)

    # Parse every expression up front so malformed expressions hard-fail
    # before enumeration, not midway through it. Keyed by sorted var names to
    # match evaluate_expression's _parse cache key below — otherwise these
    # warm-up parses never hit the cache and every expression gets parsed
    # twice (harmless, just wasted work).
    sorted_var_names = tuple(sorted(var_names))
    constraints = [str(c) for c in template["constraints"]]  # type: ignore[index]
    for expr in constraints:
        _parse(expr, sorted_var_names)
    _parse(str(template["answer"]), sorted_var_names)
    for distractor in template["distractors"]:  # type: ignore[index]
        _parse(str(distractor["expr"]), sorted_var_names)
        if distractor.get("only_if") is not None:
            _parse(str(distractor["only_if"]), sorted_var_names)

    # One exhaustive pass over the full Cartesian product (§3 Stage 3).
    total = valid = dropped_variants = 0
    drops_by_distractor: dict[str, int] = {}
    for combo in product(*domains.values()):
        total += 1
        values = dict(zip(var_names, combo))
        if not all(evaluate_condition(c, values) for c in constraints):
            continue
        valid += 1
        colliding = check_variant(template, values)
        if colliding:
            dropped_variants += 1
        for i in colliding:
            tag = template["distractors"][i].get("misconception_id") or f"distractor[{i}]"  # type: ignore[index]
            drops_by_distractor[tag] = drops_by_distractor.get(tag, 0) + 1

    if valid == 0:
        raise HardFail(
            f"{template_id}: constraints unsatisfiable — 0 of {total} combinations valid"
        )

    rejected = total - valid
    rejection_rate = rejected / total
    collision_rate = dropped_variants / valid

    reasons: list[str] = []
    if rejection_rate > REJECTION_FLAG_THRESHOLD:
        reasons.append(
            f"constraint-rejection rate {rejection_rate:.2%} > {REJECTION_FLAG_THRESHOLD:.0%} "
            f"({rejected}/{total}): heavy rejection skews the variant distribution"
        )
    if collision_rate > COLLISION_FLAG_THRESHOLD:
        reasons.append(
            f"distractor-collision rate {collision_rate:.2%} > {COLLISION_FLAG_THRESHOLD:.0%} "
            f"({dropped_variants}/{valid} variants dropped): heavy dropping skews the "
            "variant distribution"
        )

    skipped_checks = ["language_level_vs_grade"]
    corpus_list = list(corpus)
    if corpus_list:
        reasons.extend(check_embedding_duplicates(template, corpus_list))
    else:
        # No corpus supplied (default single-template call) — same
        # back-compat stub result as before KTD2, not "ran and found
        # nothing" (an empty --corpus dir would be a different, deliberate
        # case a caller doesn't hit via the default).
        skipped_checks.append("embedding_duplicates")

    return VerifyReport(
        template_id=template_id,
        total_combos=total,
        valid_combos=valid,
        rejected_combos=rejected,
        rejection_rate=round(rejection_rate, 6),
        dropped_variants=dropped_variants,
        drops_by_distractor=drops_by_distractor,
        collision_rate=round(collision_rate, 6),
        verdict=FLAGGED if reasons else PASS,
        reasons=reasons,
        skipped_checks=skipped_checks,
    )


# ---------------------------------------------------------------------------
# §3 Stage 3 stub hooks — future work, out of scope for v1
# ---------------------------------------------------------------------------

def check_language_level_vs_grade(template: Mapping[str, object]) -> list[str]:
    """Stub. §3 Stage 3 lists vocabulary/language-level-vs-grade checks
    ("off-grade vocabulary"); requires a graded wordlist per locale that v1
    does not have. Returns findings; always empty until implemented."""
    return []


# KTD2: deterministic composite text-similarity is the sanctioned stand-in
# for the design doc's §3 embedding-duplicate check (this repo has zero ML
# dependencies) — same SequenceMatcher-at-0.85 convention graph_lint.py:71
# already established for near-duplicate skill names.
EMBEDDING_DUP_STEM_THRESHOLD = 0.85

_STEM_SLOT_TOKEN = "@@slot@@"  # collapses every {{name}} to one token


def _stem_locale_text(template: Mapping[str, object]) -> str | None:
    """The stem text of some declared locale — "en-IN" preferred, else the
    lexicographically first locale present (deterministic when it isn't)."""
    stem_i18n = template.get("stem_i18n")
    if not isinstance(stem_i18n, Mapping) or not stem_i18n:
        return None
    if "en-IN" in stem_i18n:
        return str(stem_i18n["en-IN"])
    return str(stem_i18n[sorted(stem_i18n)[0]])


def _normalized_stem(template: Mapping[str, object]) -> str | None:
    """Lowercased stem text with every ``{{slot}}`` — variable or
    localization slot alike — canonicalized to one placeholder token, so two
    templates with the same stem *shape* but different slot names compare
    equal: ``"{{a}}/{{b}} + {{c}}/{{b}}"`` and ``"{{x}}/{{y}} + {{z}}/{{y}}"``
    both normalize to the identical token sequence (KTD2)."""
    text = _stem_locale_text(template)
    if text is None:
        return None
    collapsed = _SLOT_RE.sub(_STEM_SLOT_TOKEN, text.lower())
    return re.sub(r"\s+", " ", collapsed).strip()


def _canonical_answer(template: Mapping[str, object]) -> str | None:
    """Sympy-simplified answer expression, with its free symbols alpha-
    renamed (name-sorted -> ``_v0``, ``_v1``, ...) so two templates whose
    answer is the same formula shape under different variable *names*
    compare equal — mirroring ``_normalized_stem``'s slot canonicalization.
    Parses via this module's own AST-based ``_parse`` (never a fresh unsafe
    eval, per KTD2). Returns ``None`` rather than raising when a template
    can't supply one (missing/malformed fields, unparseable expression) —
    this hook must never HARD_FAIL a template on account of a *different*
    template's malformed corpus entry; an unparseable case just drops out of
    duplicate comparison instead of participating."""
    variables = template.get("variables")
    answer = template.get("answer")
    if not isinstance(variables, Mapping) or not isinstance(answer, str):
        return None
    var_names = tuple(sorted(variables))
    try:
        parsed = _materialize(_parse(answer, var_names))
        symbols = sorted(parsed.free_symbols, key=lambda s: s.name)
        renamed = parsed.subs({s: sympy.Symbol(f"_v{i}") for i, s in enumerate(symbols)})
        simplified = sympy.simplify(renamed)
    except Exception:
        return None
    return str(simplified)


def check_embedding_duplicates(
    template: Mapping[str, object], bank: Iterable[Mapping[str, object]] = ()
) -> list[str]:
    """§3 Stage 3 near-duplicate-template detection (KTD2).

    Flags a corpus entry when its normalized stem shape is highly similar
    (``SequenceMatcher`` ratio >= ``EMBEDDING_DUP_STEM_THRESHOLD``, matching
    graph_lint.py's 0.85 convention), its canonical (sympy-simplified,
    symbol-renamed) answer expression matches exactly, AND its
    ``question_type`` matches. That combination catches genuine
    near-duplicates (same shape, same answer structure, same served format)
    without false-positiving on two deliberately-real patterns confirmed
    against the actual content/templates/ corpus while tuning this check:
      - legitimate tier siblings — same skill, different stem wording/
        numbers, different difficulty band — whose stems are not actually
        similar once slots are canonicalized (stem-similarity gate handles
        this on its own);
      - deliberate certification-format pairs — e.g.
        TPL.FRAC.DIV.FRAC.DIRECT.001 (mcq_4) and TPL.FRAC.DIV.FRAC.NUM.001
        (numeric_entry): same skill, same stem, same answer, promoted on
        purpose to serve the identical math in both a guessable and a
        low-guess format (student-model doc §7). Without the
        question_type gate these are text-identical and would always
        flag; requiring a matching question_type is what tells a real
        near-duplicate apart from an intentional format twin.

    ``skill_ids`` overlap is reported inside the finding text as corroborating
    context but is deliberately NOT a required gate: an identical stem shape
    with a matching answer and question_type under a *different* skill_id is
    still flagged (catches mis-tagged copies) rather than waved through for
    lacking skill overlap.

    Returns findings (never raises) — template_id of the colliding bank
    entry plus why. ``verify_template`` folds these into ``reasons`` and the
    verdict becomes FLAGGED_FOR_REGENERATION, never a hard refusal: Stage 4
    human review adjudicates every finding (KTD2)."""
    my_id = template.get("template_id")
    my_stem = _normalized_stem(template)
    my_answer = _canonical_answer(template)
    if my_stem is None or my_answer is None:
        return []
    my_skills = set(template.get("skill_ids") or [])
    my_question_type = template.get("question_type")

    findings: list[str] = []
    for other in bank:
        other_id = other.get("template_id")
        if other_id == my_id:
            continue
        if other.get("question_type") != my_question_type:
            continue
        other_stem = _normalized_stem(other)
        other_answer = _canonical_answer(other)
        if other_stem is None or other_answer is None:
            continue
        if other_answer != my_answer:
            continue
        ratio = SequenceMatcher(None, my_stem, other_stem).ratio()
        if ratio < EMBEDDING_DUP_STEM_THRESHOLD:
            continue
        skill_overlap = bool(my_skills & set(other.get("skill_ids") or []))
        note = "" if skill_overlap else " — DIFFERENT skill_ids, possible mis-tagged copy"
        findings.append(
            f"{my_id} is a near-duplicate of {other_id}: normalized-stem similarity "
            f"{ratio:.2%} >= {EMBEDDING_DUP_STEM_THRESHOLD:.0%} with a matching canonical "
            f"answer expression and question_type{note}"
        )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_corpus(corpus_dir: Path, exclude_path: Path) -> list[dict]:
    """Every other template YAML in ``corpus_dir``, as the duplicate-
    detection corpus for the ``--corpus`` flag (KTD2) — excludes the
    template under test itself, matched first by resolved path (the common
    case: the template under test lives inside ``corpus_dir``) and also by
    ``template_id`` (a copy of it at a different path shouldn't self-flag
    either). A corpus entry that isn't valid YAML or isn't a mapping is
    skipped rather than hard-failing the whole run — a malformed *other*
    file shouldn't block verifying the template actually under test."""
    exclude_resolved = exclude_path.resolve()
    exclude_id = None
    try:
        loaded = yaml.safe_load(exclude_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            exclude_id = loaded.get("template_id")
    except (OSError, yaml.YAMLError):
        pass

    corpus: list[dict] = []
    for path in sorted(corpus_dir.glob("*.yaml")) + sorted(corpus_dir.glob("*.yml")):
        if path.resolve() == exclude_resolved:
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        if exclude_id is not None and data.get("template_id") == exclude_id:
            continue
        corpus.append(data)
    return corpus


def _print_summary(report: VerifyReport) -> None:
    print(f"{report.template_id} — Stage 3 verification")
    print(f"  combinations enumerated : {report.total_combos}")
    print(f"  constraint-satisfying   : {report.valid_combos}")
    print(f"  constraint-rejected     : {report.rejected_combos} ({report.rejection_rate:.2%})")
    print(f"  variants dropped        : {report.dropped_variants} (collision rate {report.collision_rate:.2%})")
    if report.drops_by_distractor:
        for tag, count in sorted(report.drops_by_distractor.items()):
            print(f"    dropped by {tag}: {count}")
    if report.skipped_checks:
        print(f"  skipped (v1 stubs)      : {', '.join(report.skipped_checks)}")
    print(f"  verdict                 : {report.verdict}")
    for reason in report.reasons:
        print(f"    - {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("template", type=Path, help="path to the item template YAML")
    parser.add_argument("--report", type=Path, help="optional path to write a JSON report")
    parser.add_argument(
        "--corpus", type=Path,
        help="dir of other template YAMLs to check for near-duplicates against "
        "(§3 Stage 3 embedding-duplicate check, KTD2); omit to skip that check",
    )
    args = parser.parse_args(argv)

    try:
        template = load_template(args.template)
        corpus = load_corpus(args.corpus, args.template) if args.corpus else ()
        report = verify_template(template, corpus)
    except HardFail as exc:
        print(f"HARD_FAIL: {exc}")
        if args.report:
            payload = {"verdict": HARD_FAIL, "reasons": [str(exc)]}
            args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return EXIT_CODES[HARD_FAIL]

    _print_summary(report)
    if args.report:
        args.report.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return EXIT_CODES[report.verdict]


if __name__ == "__main__":
    sys.exit(main())
