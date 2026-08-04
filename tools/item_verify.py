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
#
# FIGURE_KIND_RULES (below, after check_slots — it needs this module's rational
# evaluation helpers) is the same registry shape for figure integrity (KTD5).
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

    # KTD5's two kind-independent structural rules: a figure label map covers
    # every language the stem is authored in (bank_build materializes the
    # figure into the instance's single language through the same dict as the
    # stem, so a missing language is a crash at build time), and a label slot
    # resolves exactly the way a *stem* slot resolves — variable or locale
    # slot, never a derived value (a figure is stimulus, not exposition).
    stem_languages = set(template["stem_i18n"])  # type: ignore[arg-type]
    for index, figure in enumerate(figures(template)):
        for path, label_map in _figure_label_maps(figure):
            missing = stem_languages - set(label_map)
            if missing:
                raise HardFail(
                    f"figure[{index}] {path} is missing language(s) {sorted(missing)} "
                    "present in stem_i18n: every figure label covers every stem language"
                )
            undeclared = _slot_names(_i18n_strings(label_map)) - variables - locale_slots
            if undeclared:
                raise HardFail(
                    f"figure[{index}] {path} references undeclared slots {sorted(undeclared)}: "
                    "not in localization_slots and not a template variable"
                )


# ---------------------------------------------------------------------------
# Figure integrity (R9, KTD5): a per-kind rule registry in the
# DERIVED_VALUE_RULES shape — each kind declares the structural and
# value-consistency invariants that the JSON Schema cannot express (it knows
# required keys and enums, not that a half symbol needs an even key or that a
# cutout must fit inside its block). Every expression evaluates through
# evaluate_expression, so all figure arithmetic is exact sympy rationals and
# never a float (KTD4). Violations are HardFail, never FLAG: a FLAG still
# builds (tools/bank_build.py:487-492), so a figure-inconsistent template has
# to hard-fail to actually refuse the build.
#
# Label *wording* against stem prose stays Stage 4 human review; what is
# machine-checked here is well-formedness, consistency with the variable
# assignment, and the answer leak.
# ---------------------------------------------------------------------------

FigureRule = Callable[[Mapping[str, object], Mapping[str, int]], None]

# Symbols an expression field may use beyond the declared template variables.
# figArray's per_row_expr is evaluated once per row with "i" bound to the
# one-based row index ("i" builds a triangular pile, a constant a rectangle).
FIGURE_BOUND_SYMBOLS: dict[tuple[str, str], tuple[str, ...]] = {
    ("array", "per_row_expr"): ("i",),
}

# A label that renders to a bare number is compared against the answer; the
# answer pattern is the same ^-?\d+(/\d+)?$ shape the instance schema uses.
_NUMERIC_LABEL_RE = re.compile(r"^-?\d+(?:/\d+)?$")

# Shapes whose side count is fixed by the name (regular-ngon's comes from
# sides_expr; circle/prism/composite-l have no meaningful side count).
_SHAPE_SIDE_COUNTS = {
    "triangle": 3,
    "isosceles-triangle": 3,
    "rectangle": 4,
    "square": 4,
    "parallelogram": 4,
    "pentagon": 5,
}


def figures(template: Mapping[str, object]) -> list[Mapping[str, object]]:
    """The template's authored figures ("figure"), or [] when it declares none."""
    declared = template.get("figure")
    if not isinstance(declared, list):
        return []
    return [f for f in declared if isinstance(f, Mapping)]


def _walk_keyed(node: object, suffix: str, path: str = "") -> Iterable[tuple[str, str, object]]:
    """Yield (dotted path, key, value) for every mapping key ending in ``suffix``.

    Walking by key suffix rather than a per-kind field list means a new kind
    (or a new field on an existing kind) gets its expressions pre-parsed and
    its labels slot-checked for free — the naming convention (``*_expr``,
    ``*_i18n``) is the same across all twelve kinds in the item-record schema.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if str(key).endswith(suffix):
                yield child, str(key), value
            else:
                yield from _walk_keyed(value, suffix, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_keyed(item, suffix, f"{path}[{index}]")


def _figure_expressions(figure: Mapping[str, object]) -> list[tuple[str, str, str]]:
    return [(p, k, v) for p, k, v in _walk_keyed(figure, "_expr") if isinstance(v, str)]


def _figure_label_maps(figure: Mapping[str, object]) -> list[tuple[str, Mapping]]:
    return [(p, v) for p, _k, v in _walk_keyed(figure, "_i18n") if isinstance(v, Mapping)]


def _exact(expr: str, values: Mapping[str, int], label: str) -> sympy.Rational:
    """Evaluate a figure expression to an exact rational or hard-fail.

    Anything that is not a Rational at this variant — a leftover free symbol,
    a division by zero (sympy's ``zoo``), an irrational — cannot be rendered
    and cannot be serialized under the contract's rational-string rule (KTD4).
    """
    value = evaluate_expression(expr, values)
    if not isinstance(value, sympy.Rational):
        raise HardFail(
            f"{label}: expression {expr!r} did not evaluate to an exact rational "
            f"(got {value}) at {dict(values)}"
        )
    return value


def _expr_rat(
    node: Mapping[str, object], key: str, values: Mapping[str, int], prefix: str = "",
    *, required: bool = True,
) -> sympy.Rational | None:
    expr = node.get(key)
    if expr is None:
        if required:
            raise HardFail(f"{prefix}{key} is required")
        return None
    return _exact(str(expr), values, f"{prefix}{key}")


def _expr_int(
    node: Mapping[str, object], key: str, values: Mapping[str, int], prefix: str = "",
    *, required: bool = True,
) -> int | None:
    value = _expr_rat(node, key, values, prefix, required=required)
    if value is None:
        return None
    if value.q != 1:
        raise HardFail(
            f"{prefix}{key} ({node[key]!r}) must be a whole number, got {value} at {dict(values)}"
        )
    return int(value)


def _rule_bar_graph(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    scale = _expr_int(figure, "scale_expr", values)
    if scale is not None and scale <= 0:
        raise HardFail(f"scale_expr (value per gridline) must be positive, got {scale}")
    for index, bar in enumerate(figure.get("bars") or []):  # type: ignore[union-attr]
        count = _expr_int(bar, "gridlines_expr", values, f"bars[{index}].")
        if count is not None and count < 0:
            raise HardFail(f"bars[{index}].gridlines_expr must not be negative, got {count}")


def _rule_pictograph(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    key = _expr_int(figure, "key_expr", values)
    if key is not None and key <= 0:
        raise HardFail(f"key_expr (value of one whole symbol) must be positive, got {key}")
    rows = list(figure.get("rows") or [])  # type: ignore[arg-type]
    if key is not None and key % 2 != 0 and any(row.get("half") for row in rows):
        raise HardFail(
            f"a row drawn with a half symbol is worth key/2, which requires an even key, "
            f"but key_expr evaluates to {key} at {dict(values)}"
        )
    for index, row in enumerate(rows):
        whole = _expr_int(row, "whole_expr", values, f"rows[{index}].")
        if whole is not None and whole < 0:
            raise HardFail(f"rows[{index}].whole_expr must not be negative, got {whole}")


def _rule_tally_chart(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    for index, row in enumerate(figure.get("rows") or []):  # type: ignore[union-attr]
        prefix = f"rows[{index}]."
        bundles = _expr_int(row, "bundles_expr", values, prefix)
        loose = _expr_int(row, "loose_expr", values, prefix)
        if bundles is not None and bundles < 0:
            raise HardFail(f"{prefix}bundles_expr must not be negative, got {bundles}")
        if loose is not None and not 0 <= loose <= 4:
            raise HardFail(
                f"{prefix}loose_expr counts ungrouped strokes, which are always fewer "
                f"than five, got {loose}"
            )


def _rule_number_line(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    low = _expr_rat(figure, "min_expr", values)
    high = _expr_rat(figure, "max_expr", values)
    if low is not None and high is not None and low >= high:
        raise HardFail(f"min_expr must be less than max_expr, got [{low}, {high}]")
    divisions = _expr_int(figure, "divisions_expr", values)
    if divisions is not None and divisions < 1:
        raise HardFail(f"divisions_expr (sub-divisions per unit) must be at least 1, got {divisions}")
    for index, point in enumerate(figure.get("points") or []):  # type: ignore[union-attr]
        position = _expr_rat(point, "position_expr", values, f"points[{index}].")
        if position is None:
            continue
        if low is not None and high is not None and not low <= position <= high:
            raise HardFail(
                f"points[{index}].position_expr is {position}, outside the line [{low}, {high}]"
            )
        if divisions is not None and divisions >= 1 and (position * divisions).q != 1:
            raise HardFail(
                f"points[{index}].position_expr is {position}, which lands between ticks "
                f"on a line divided into {divisions}ths — an unreadable point"
            )


def _rule_angle(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    degrees = _expr_rat(figure, "degrees_expr", values)
    context = str(figure.get("context"))
    if degrees is not None:
        if degrees <= 0:
            raise HardFail(f"degrees_expr must be positive, got {degrees}")
        if context == "reflex" and not 180 < degrees < 360:
            raise HardFail(
                f"the reflex context draws the major arc, so degrees_expr must be strictly "
                f"between 180 and 360, got {degrees}"
            )
        if context == "clock" and degrees > 360:
            raise HardFail(f"a turn on a clock face cannot exceed 360 degrees, got {degrees}")
        if context in {"bare", "protractor"} and degrees > 180:
            raise HardFail(
                f"degrees_expr is {degrees}: an angle above 180 needs the reflex context "
                f"(or the clock face), not {context!r}"
            )
    if "scale" in figure and context != "protractor":
        raise HardFail(
            f"scale names which protractor scale the item reads, so it belongs to the "
            f"protractor context only, not {context!r}"
        )
    count = _expr_int(figure, "count_expr", values, required=False)
    if count is not None and count < 1:
        raise HardFail(f"count_expr repeats the angle, so it must be at least 1, got {count}")
    arm = _expr_rat(figure, "arm_length_expr", values, required=False)
    if arm is not None and arm <= 0:
        raise HardFail(f"arm_length_expr must be positive, got {arm}")


def _rule_circle(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    has_part = "length_part" in figure
    has_length = "length_expr" in figure
    if has_part != has_length:
        raise HardFail(
            "length_part and length_expr are given together or not at all — a length "
            "without the part it measures is unrenderable"
        )
    length = _expr_rat(figure, "length_expr", values, required=False)
    if length is not None and length <= 0:
        raise HardFail(f"length_expr must be positive, got {length}")
    for key in ("points_on_expr", "points_off_expr"):
        count = _expr_int(figure, key, values, required=False)
        if count is not None and count < 0:
            raise HardFail(f"{key} must not be negative, got {count}")


def _rule_point_line(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    for index, element in enumerate(figure.get("elements") or []):  # type: ignore[union-attr]
        count = _expr_int(element, "count_expr", values, f"elements[{index}].")
        if count is not None and count < 0:
            raise HardFail(f"elements[{index}].count_expr must not be negative, got {count}")
        labels = element.get("labels")
        if isinstance(labels, list) and count is not None and len(labels) < count:
            raise HardFail(
                f"elements[{index}] draws {count} {element.get('element')}(s) but declares "
                f"only {len(labels)} label(s) at {dict(values)}"
            )


def _rule_shape(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    shape = str(figure.get("shape"))
    sides = _expr_int(figure, "sides_expr", values, required=False)
    if shape == "regular-ngon":
        if sides is None:
            raise HardFail("a regular-ngon takes its side count from sides_expr, which is absent")
        if sides < 3:
            raise HardFail(f"a regular-ngon needs at least 3 sides, got {sides}")
    elif shape in _SHAPE_SIDE_COUNTS:
        expected = _SHAPE_SIDE_COUNTS[shape]
        if sides is not None and sides != expected:
            raise HardFail(f"a {shape} has {expected} sides, but sides_expr evaluates to {sides}")
        sides = expected
    for index, side_label in enumerate(figure.get("side_labels") or []):  # type: ignore[union-attr]
        position = side_label.get("side")
        if sides is not None and isinstance(position, int) and position >= sides:
            raise HardFail(
                f"side_labels[{index}] labels side {position} (zero-based) of a "
                f"{sides}-sided {shape}"
            )
        value = _expr_rat(side_label, "value_expr", values, f"side_labels[{index}].")
        if value is not None and value <= 0:
            raise HardFail(f"side_labels[{index}].value_expr must be positive, got {value}")


def _rule_region(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    parts = list(figure.get("parts") or [])  # type: ignore[arg-type]
    # Each authored entry is a *group* of identical pieces (count_expr,
    # defaulting to 1), so the shading and total-piece checks below count
    # drawn pieces rather than authored entries — a group whose count
    # evaluates to zero on this variant contributes nothing to either.
    drawn_shaded = 0
    drawn_total = 0
    for index, part in enumerate(parts):
        prefix = f"parts[{index}]."
        weight = _expr_rat(part, "weight_expr", values, prefix)
        if weight is not None and weight <= 0:
            raise HardFail(f"parts[{index}].weight_expr (relative size) must be positive, got {weight}")
        if "count_expr" in part:
            count = _expr_int(part, "count_expr", values, prefix)
            if count is None or count < 0:
                raise HardFail(
                    f"parts[{index}].count_expr must be a whole piece count, got {count}"
                )
        else:
            count = 1
        drawn_total += count
        if bool(part.get("shaded")):
            drawn_shaded += count
    if drawn_total < 2:
        raise HardFail(
            f"a divided whole needs at least two pieces, got {drawn_total}"
        )
    if drawn_shaded == 0:
        raise HardFail("a part-whole region needs at least one shaded piece, got none")


def _rule_array(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    bound = FIGURE_BOUND_SYMBOLS[("array", "per_row_expr")][0]
    if bound in values:
        raise HardFail(
            f"an array figure evaluates per_row_expr with {bound!r} bound to the row index, "
            f"so the template must not also declare a variable named {bound!r}"
        )
    rows = _expr_int(figure, "rows_expr", values)
    if rows is None:
        return
    if rows < 1:
        raise HardFail(f"rows_expr must be at least 1, got {rows}")
    per_row = str(figure.get("per_row_expr"))
    for row in range(1, rows + 1):
        marks = _exact(per_row, {**values, bound: row}, f"per_row_expr (row {row})")
        if marks.q != 1 or int(marks) < 1:
            raise HardFail(
                f"per_row_expr must be a positive whole number of marks, got {marks} "
                f"in row {row} at {dict(values)}"
            )


def _rule_grid_region(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    cols = _expr_int(figure, "cols_expr", values)
    rows = _expr_int(figure, "rows_expr", values)
    if cols is None or rows is None:
        return
    if cols < 1 or rows < 1:
        raise HardFail(f"a grid needs at least one cell, got {cols}x{rows}")
    fill = str(figure.get("fill"))
    cutout_cols = _expr_int(figure, "cutout_cols_expr", values, required=False)
    cutout_rows = _expr_int(figure, "cutout_rows_expr", values, required=False)
    half_cells = _expr_int(figure, "half_cells_expr", values, required=False)

    if fill == "block-minus-corner":
        if cutout_cols is None or cutout_rows is None:
            raise HardFail(
                "the block-minus-corner fill needs both cutout_cols_expr and cutout_rows_expr"
            )
        if not (1 <= cutout_cols < cols and 1 <= cutout_rows < rows):
            raise HardFail(
                f"the cutout ({cutout_cols}x{cutout_rows}) must be strictly smaller than the "
                f"{cols}x{rows} block in both dimensions at {dict(values)}"
            )
    elif cutout_cols is not None or cutout_rows is not None:
        raise HardFail(f"cutout_* belongs to the block-minus-corner fill, not {fill!r}")

    if fill == "half-squares":
        if half_cells is None:
            raise HardFail("the half-squares fill needs half_cells_expr")
        if not 1 <= half_cells <= cols * rows:
            raise HardFail(
                f"half_cells_expr is {half_cells}, outside the {cols * rows} cells of the grid"
            )
    elif half_cells is not None:
        raise HardFail(f"half_cells_expr belongs to the half-squares fill, not {fill!r}")


def _rule_ruler(figure: Mapping[str, object], values: Mapping[str, int]) -> None:
    low = _expr_rat(figure, "min_expr", values)
    high = _expr_rat(figure, "max_expr", values)
    if low is None or high is None:
        return
    if low >= high:
        raise HardFail(f"min_expr must be less than max_expr on a ruler scale, got [{low}, {high}]")
    major = _expr_rat(figure, "major_expr", values)
    minor = _expr_rat(figure, "minor_expr", values)
    if major is not None and major <= 0:
        raise HardFail(f"major_expr (numbered tick interval) must be positive, got {major}")
    if minor is not None and minor <= 0:
        raise HardFail(f"minor_expr must be positive, got {minor}")
    if major is not None and minor is not None and minor > major:
        raise HardFail(
            f"the minor tick interval ({minor}) must not exceed the numbered interval ({major})"
        )
    for index, obj in enumerate(figure.get("objects") or []):  # type: ignore[union-attr]
        prefix = f"objects[{index}]."
        start = _expr_rat(obj, "start_expr", values, prefix)
        end = _expr_rat(obj, "end_expr", values, prefix)
        if start is None or end is None:
            continue
        if start >= end:
            raise HardFail(f"{prefix}start_expr ({start}) must be less than end_expr ({end})")
        if start < low or end > high:
            raise HardFail(
                f"objects[{index}] spans [{start}, {end}], which must lie within the ruler "
                f"scale [{low}, {high}]"
            )


FIGURE_KIND_RULES: dict[str, tuple[FigureRule, ...]] = {
    "bar-graph": (_rule_bar_graph,),
    "pictograph": (_rule_pictograph,),
    "tally-chart": (_rule_tally_chart,),
    "number-line": (_rule_number_line,),
    "angle": (_rule_angle,),
    "circle": (_rule_circle,),
    "point-line": (_rule_point_line,),
    "shape": (_rule_shape,),
    "region": (_rule_region,),
    "array": (_rule_array,),
    "grid-region": (_rule_grid_region,),
    "ruler": (_rule_ruler,),
}


def check_figure_expressions(
    template: Mapping[str, object], sorted_var_names: tuple[str, ...]
) -> None:
    """Pre-parse every figure expression, mirroring the answer/distractor pass.

    An expression over an undeclared variable fails here — before deep
    enumeration — rather than surviving substitution as a leftover free symbol
    and being caught one variant at a time.
    """
    declared = set(sorted_var_names)
    for index, figure in enumerate(figures(template)):
        kind = str(figure.get("kind"))
        if kind not in FIGURE_KIND_RULES:
            raise HardFail(
                f"figure[{index}]: no figure rules are registered for kind {kind!r} — "
                "a new kind must declare its integrity rules in FIGURE_KIND_RULES"
            )
        for path, key, expr in _figure_expressions(figure):
            parsed = _parse(expr, sorted_var_names)
            allowed = declared | set(FIGURE_BOUND_SYMBOLS.get((kind, key), ()))
            undeclared = sorted(s.name for s in parsed.free_symbols if s.name not in allowed)
            if undeclared:
                raise HardFail(
                    f"figure[{index}] ({kind}) {path}: expression {expr!r} references "
                    f"undeclared variables {undeclared}"
                )


def _check_label_leak(
    figure: Mapping[str, object], substitutions: Mapping[str, str], answer_key: tuple
) -> None:
    """One general answer-leak rule, in place of a per-kind one (KTD5).

    A label that renders to this variant's answer value gives the answer away
    — a number-line read item labelling the queried tick, a gridline-value
    item labelling the queried bar. Only the *variable* slots are substituted
    (locale dressing is chosen at build time and always renders to words, never
    to a number), and only a label that renders to a bare rational is compared:
    label wording against stem prose is Stage 4 human review, not this gate.
    """
    for path, label_map in _figure_label_maps(figure):
        for language, text in label_map.items():
            rendered = _SLOT_RE.sub(
                lambda m: substitutions.get(m.group(1), m.group(0)), str(text)
            ).strip()
            if not _NUMERIC_LABEL_RE.fullmatch(rendered):
                continue
            if _rational_key(sympy.Rational(rendered)) == answer_key:
                raise HardFail(
                    f"{path}[{language}] renders to {rendered!r}, which is this variant's "
                    "answer: the label leaks the answer"
                )


def _check_region_matches_answer(
    figure: Mapping[str, object], values: Mapping[str, int], answer_key: tuple
) -> None:
    """A part-whole region's shaded area must equal the fraction it asks for.

    This is the strongest consistency rule any kind has, and it is the one
    that matters: these items ask "what fraction is shaded?", so a figure
    whose shaded area disagrees with the answer marks the child wrong for
    reading the picture correctly. It also subsumes shading heuristics — a
    fully-shaded whole is fine precisely when the answer is 1.
    """
    shaded_area = sympy.Rational(0)
    total_area = sympy.Rational(0)
    for index, part in enumerate(figure.get("parts") or []):  # type: ignore[union-attr]
        prefix = f"parts[{index}]."
        weight = _expr_rat(part, "weight_expr", values, prefix)
        count = _expr_int(part, "count_expr", values, prefix) if "count_expr" in part else 1
        if weight is None or count is None:
            return  # the kind rule already reported the unevaluable expression
        total_area += weight * count
        if bool(part.get("shaded")):
            shaded_area += weight * count
    if total_area == 0:
        raise HardFail("a part-whole region has zero total area")
    if _rational_key(shaded_area / total_area) != answer_key:
        raise HardFail(
            f"the figure shades {shaded_area}/{total_area} of the whole, which is not this "
            f"variant's answer — the drawn region and the expected answer disagree"
        )


def check_figures(template: Mapping[str, object], values: Mapping[str, int]) -> None:
    """Run every figure rule for one variant; any violation is a HardFail."""
    declared = figures(template)
    if not declared:
        return
    answer_key = _rational_key(evaluate_expression(str(template["answer"]), values))
    substitutions = {name: str(value) for name, value in values.items()}
    for index, figure in enumerate(declared):
        kind = str(figure.get("kind"))
        try:
            for path, key, expr in _figure_expressions(figure):
                if (kind, key) in FIGURE_BOUND_SYMBOLS:
                    continue  # evaluated by its kind rule, which binds the extra symbol
                _exact(expr, values, path)
            for rule in FIGURE_KIND_RULES[kind]:
                rule(figure, values)
            if kind == "region":
                _check_region_matches_answer(figure, values, answer_key)
            _check_label_leak(figure, substitutions, answer_key)
        except HardFail as exc:
            raise HardFail(f"figure[{index}] ({kind}) at {dict(values)}: {exc}") from exc


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
    check_figure_expressions(template, sorted_var_names)

    # One exhaustive pass over the full Cartesian product (§3 Stage 3).
    has_figures = bool(figures(template))
    total = valid = dropped_variants = 0
    drops_by_distractor: dict[str, int] = {}
    for combo in product(*domains.values()):
        total += 1
        values = dict(zip(var_names, combo))
        if not all(evaluate_condition(c, values) for c in constraints):
            continue
        valid += 1
        if has_figures:
            check_figures(template, values)
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
