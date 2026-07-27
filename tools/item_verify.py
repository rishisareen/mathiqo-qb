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

Language-level-vs-grade and embedding duplicate detection are also listed
under §3 Stage 3 but are out of scope for v1; see the stub hooks at the
bottom of this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
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

def load_template(path: Path) -> dict:
    """Load a template YAML and hard-fail unless it validates against the schema."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HardFail(f"{path}: top level must be a mapping")
    schema = json.loads(ITEM_RECORD_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise HardFail(f"{path}: schema violation(s): {details}")
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


def verify_template(template: Mapping[str, object]) -> VerifyReport:
    """Run the full Stage 3 gate on an already schema-valid template dict."""
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
        skipped_checks=["language_level_vs_grade", "embedding_duplicates"],
    )


# ---------------------------------------------------------------------------
# §3 Stage 3 stub hooks — future work, out of scope for v1
# ---------------------------------------------------------------------------

def check_language_level_vs_grade(template: Mapping[str, object]) -> list[str]:
    """Stub. §3 Stage 3 lists vocabulary/language-level-vs-grade checks
    ("off-grade vocabulary"); requires a graded wordlist per locale that v1
    does not have. Returns findings; always empty until implemented."""
    return []


def check_embedding_duplicates(
    template: Mapping[str, object], bank: Iterable[Mapping[str, object]] = ()
) -> list[str]:
    """Stub. §3 Stage 3 lists embedding-based duplicate detection of near-
    identical templates already in the bank; needs the embedding index from
    Stage 5 infrastructure. Returns findings; always empty until implemented."""
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
    args = parser.parse_args(argv)

    try:
        template = load_template(args.template)
        report = verify_template(template)
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
