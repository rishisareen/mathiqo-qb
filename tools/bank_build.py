#!/usr/bin/env python3
"""Stage 5 bank builder (core design doc §3 Stage 5, §5.1).

Materializes promoted item templates (schemas/item-record.schema.json,
content/templates/*.yaml) into an atomic, versioned bank of concrete,
dressed, schema-valid item instances (schemas/item-instance.schema.json,
KTD4). This is the boundary that makes §5.1's rule real: "Everything a
child can see is materialized offline ... the moment the runtime 'just
tweaks a number,' generation has silently re-entered the serving path."
Everything a served item carries — its rendered stem, exact answer, and full
tagged distractor set — is computed once here; the Orchestrator only ever
selects and looks up (R11).

Per template this tool:
  1. loads and re-verifies it (schema + tools/item_verify.py's Stage 3 gate),
     hard-refusing anything that isn't mcq_4 or numeric_entry (v1 scope,
     core doc §4) or that HARD_FAILs verification, naming the template.
     A FLAGGED_FOR_REGENERATION verdict does NOT refuse the build — Stage 4
     human review already promoted the template into content/templates/
     despite the flag (the shipped TPL.FRAC.ADD.UNLIKE.WORD.001 fixture is
     itself FLAGGED by its own 82% constraint-rejection rate; refusing to
     build it would contradict the promoted-content reality this tool reads
     from). Only a HardFail — schema violation, unparseable expression,
     unsatisfiable constraints, unresolvable slot — refuses the build.
  2. enumerates every valid, non-colliding variant by importing
     tools/item_verify.py's primitives directly (KTD5) — build_domains,
     evaluate_condition, check_variant — rather than re-implementing
     "what is a valid variant". A variant is dropped from the pool exactly
     when Stage 3 would drop it (an offered distractor colliding with the
     answer or another offered distractor).
  3. caps the per-template pool at --pool-cap with a deterministic,
     evenly-spaced sample seeded from template_id + bank_version (KTD6) —
     same inputs, same bank, byte-identical rebuilds. There is currently no
     explicit "tier" field on templates (each carries one difficulty_prior
     band and one constraints array), so v1 treats each template as exactly
     one tier: the cap is per-template, not per (template, tier) as core
     §5.1 describes for a future multi-tier template. Do not invent a tier
     field the schema doesn't have.
  4. dresses each sampled variant: locale-pack slot substitution (rotating
     deterministically across the sampled pool), variable and derived-value
     substitution into stem/hints/solution steps (reusing
     item_verify.DERIVED_VALUE_RULES — the same lcm/a2/c2/sum/final formulas
     Stage 3 already verifies against, KTD5), and exact symbolic computation
     of the canonical answer string and the full distractor value set.
  5. normalizes accepted_answer_forms into simplest_form_only (KTD8): true
     iff the template's free-text field starts with "simplest form only"
     (case-insensitive), false when absent or phrased otherwise.
  6. validates every instance against schemas/item-instance.schema.json and
     writes banks/<bank_version>/ (instances/<template_id>.json plus a
     manifest) atomically: built into a staging directory under --out, then
     renamed into place in one step (stage2_draft.py's staging-dir
     discipline; §3 Stage 5 "banks are never patched in place" — refuses to
     overwrite an existing bank_version).

Usage:
    python tools/bank_build.py --templates-dir content/templates \
        --out banks --bank-version 2026.07-dev [--pool-cap 40] \
        [--locale-pack content/locales/en-IN.yaml]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Mapping

import jsonschema
import sympy
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import item_verify  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ITEM_INSTANCE_SCHEMA = REPO_ROOT / "schemas" / "item-instance.schema.json"
DEFAULT_LOCALE_PACK = REPO_ROOT / "content" / "locales" / "en-IN.yaml"

# v1 bank-build scope (core doc §4): the only two §2.7 types this slice
# ships. Kept as its own constant (not imported from stage2_draft.py) since
# it is this tool's own hard-refusal boundary, not a drafting-harness detail.
ALLOWED_QUESTION_TYPES = frozenset({"mcq_4", "numeric_entry"})

DEFAULT_POOL_CAP = 40

# KTD1: content identity for what the student actually sees (template +
# concrete variable assignment), stable across bank rebuilds (KD4, R8, R9).
VARIANT_SIGNATURE_ALGORITHM = "sha256"

# Mirrors tools/item_verify.py's _SLOT_RE (a private module attribute) —
# duplicated rather than imported because this is presentation-layer text
# substitution, not "what is a valid variant" logic (the thing KTD5 says not
# to duplicate); the two independently matching the same {{slot}} convention
# is fine.
_SLOT_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# A slot named "name", "name1", "name2", ... draws from the locale pack's
# "names" category; every other slot name is looked up in the pack verbatim
# (e.g. "food" -> pack["food"]). Only these two categories are actually
# declared by any v1 template today (see content/locales/en-IN.yaml).
_NAME_SLOT_RE = re.compile(r"^name\d*$")


class BuildError(Exception):
    """A build-time failure naming the offending template; refuses the build."""


# ---------------------------------------------------------------------------
# Locale pack
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalePack:
    locale: str
    language: str
    categories: Mapping[str, list]
    source_path: Path

    @classmethod
    def load(cls, path: Path) -> "LocalePack":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "locale" not in data:
            raise BuildError(f"{path}: locale pack must be a mapping with a top-level 'locale' key")
        categories = {k: v for k, v in data.items() if isinstance(v, list)}
        return cls(
            locale=str(data["locale"]),
            language=str(data.get("language", data["locale"])),
            categories=categories,
            source_path=path,
        )

    def category_for(self, slot: str) -> str:
        return "names" if _NAME_SLOT_RE.match(slot) else slot

    def fill(self, slot: str, offset: int) -> str:
        category = self.category_for(slot)
        pool = self.categories.get(category)
        if not pool:
            raise BuildError(
                f"locale pack {self.source_path} has no values for localization slot "
                f"{slot!r} (category {category!r})"
            )
        return str(pool[offset % len(pool)])


def dress_slots(slot_names: list[str], pack: LocalePack, base_index: int) -> dict[str, str]:
    """Assign a locale value to every declared localization slot.

    Rotates by base_index (an instance's rank within its template's sampled
    pool) so dressing sweeps the pack's pools across the served instances.
    Multiple slots of the same category within one instance (name1, name2)
    get consecutive offsets so they never coincide, as long as the pack's
    pool for that category has at least as many entries as same-category
    slots in one template (true for every v1 template: at most 2 name slots).
    """
    category_counters: dict[str, int] = {}
    values: dict[str, str] = {}
    for slot in slot_names:
        category = pack.category_for(slot)
        offset = category_counters.get(category, 0)
        category_counters[category] = offset + 1
        values[slot] = pack.fill(slot, base_index + offset)
    return values


# ---------------------------------------------------------------------------
# Exact-value formatting (reuses item_verify's evaluation, not its own math)
# ---------------------------------------------------------------------------

def _fmt_rational(value: sympy.Basic) -> str:
    """Canonical reduced rational string: "p/q" (q > 1) or "p" (whole)."""
    if not isinstance(value, sympy.Rational):
        raise BuildError(f"expected an exact rational value, got {value!r} ({type(value).__name__})")
    p, q = int(value.p), int(value.q)
    return str(p) if q == 1 else f"{p}/{q}"


def _fmt_unsimplified_distractor(expr: str, values: Mapping[str, int]) -> str:
    """Literal unreduced numerator/denominator split for an unsimplified
    distractor. Reuses item_verify._unsimplified_key (KTD5) — the exact same
    cross-checked split Stage 3 already compares collisions against — rather
    than re-deriving "how is this distractor displayed" a second time."""
    _kind, n, d = item_verify._unsimplified_key(expr, values)  # noqa: SLF001
    return f"{n}/{d}"


def compute_derived(values: Mapping[str, int]) -> dict[str, sympy.Basic]:
    """Derived solution-step/hint values (lcm, a2, c2, sum, final, ...)
    available for this variant's declared variables — reuses
    item_verify.DERIVED_VALUE_RULES (KTD5) rather than re-deriving the same
    lcm/a2/c2/sum/final formulas Stage 3's check_slots already validated
    against."""
    var_names = set(values)
    return {
        name: fn(values)
        for name, (required, fn) in item_verify.DERIVED_VALUE_RULES.items()
        if required <= var_names
    }


def render_text(text: str, substitutions: Mapping[str, str]) -> str:
    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in substitutions:
            raise BuildError(f"unresolved slot {{{{{name}}}}} in {text!r} — not a variable, derived value, or localization slot")
        return substitutions[name]

    return _SLOT_RE.sub(_sub, text)


def compute_simplest_form_only(template: Mapping[str, object]) -> bool:
    """KTD8: true iff accepted_answer_forms starts with "simplest form only"
    (case-insensitive); false when absent or phrased otherwise. Grading
    downstream reads this flag, never the template's free-text field."""
    forms = template.get("accepted_answer_forms")
    if not isinstance(forms, str):
        return False
    return forms.strip().lower().startswith("simplest form only")


# ---------------------------------------------------------------------------
# Deterministic pool-cap sampling (KTD6)
# ---------------------------------------------------------------------------

def derive_seed(template_id: str, bank_version: str) -> int:
    digest = hashlib.sha256(f"{template_id}:{bank_version}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def compute_variant_signature(
    template_id: str, template_version: str, variables: Mapping[str, int]
) -> str:
    """KTD1: sha256 over canonical JSON of (template_id, template_version,
    variables) — the identity for what the student actually sees. Excludes
    stem, locale, dressing, language, rank, and bank_version: dressing rotates
    by rank (dress_slots' base_index) and rank shifts across builds, so
    rendered text is unstable across rebuilds while the mathematical content
    (this signature) is not — the fact that lets seen-state survive a weekly
    rebuild (KD4)."""
    canonical = json.dumps(
        {
            "template_id": template_id,
            "template_version": template_version,
            "variables": dict(variables),
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sample_variant_indices(total: int, cap: int, seed: int) -> list[int]:
    """Deterministic evenly-spaced sample of `cap` positions out of `total`
    (KTD6). The seed (derived from template_id + bank_version) only picks
    the *phase* of the spacing: two builds of the same inputs pick the same
    pool (reproducible), but different templates don't all sample the same
    low-index slice of their enumeration order."""
    if cap >= total:
        return list(range(total))
    rng = random.Random(seed)
    offset = rng.randrange(total)
    step = total / cap
    seen: set[int] = set()
    chosen: list[int] = []
    i = 0
    while len(chosen) < cap and i < total:
        idx = int(offset + i * step) % total
        if idx not in seen:
            seen.add(idx)
            chosen.append(idx)
        i += 1
    if len(chosen) < cap:  # pathological fallback; keeps output exactly `cap` long
        for idx in range(total):
            if idx not in seen:
                seen.add(idx)
                chosen.append(idx)
                if len(chosen) == cap:
                    break
    return sorted(chosen)


# ---------------------------------------------------------------------------
# Instance materialization
# ---------------------------------------------------------------------------

def materialize_instance(
    template: Mapping[str, object],
    values: Mapping[str, int],
    rank: int,
    pack: LocalePack,
    bank_version: str,
    language: str,
) -> dict:
    """Dress and fully compute one concrete instance (§5.1, R11: everything a
    child could see is materialized here, offline, once)."""
    derived = compute_derived(values)
    slot_names = list(template.get("localization_slots") or [])
    locale_values = dress_slots(slot_names, pack, base_index=rank)

    substitutions: dict[str, str] = {name: str(value) for name, value in values.items()}
    substitutions.update({name: _fmt_rational(value) for name, value in derived.items()})
    substitutions.update(locale_values)

    stem = render_text(str(template["stem_i18n"][language]), substitutions)  # type: ignore[index]
    hints = [render_text(h, substitutions) for h in template["hints_i18n"][language]]  # type: ignore[index]
    solution_steps = {
        variant: [render_text(step, substitutions) for step in steps]
        for variant, steps in template["solution_steps_i18n"][language].items()  # type: ignore[index]
    }

    answer_value = item_verify.evaluate_expression(str(template["answer"]), values)
    answer = _fmt_rational(answer_value)

    distractors = []
    for distractor in template["distractors"]:  # type: ignore[index]
        if not item_verify.distractor_offered(distractor, values):
            continue
        expr = str(distractor["expr"])
        if distractor.get("unsimplified"):
            value_str = _fmt_unsimplified_distractor(expr, values)
        else:
            value_str = _fmt_rational(item_verify.evaluate_expression(expr, values))
        distractors.append({
            "value": value_str,
            "misconception_id": distractor.get("misconception_id"),
            "unsimplified": bool(distractor.get("unsimplified", False)),
        })

    instance_id = f"{template['template_id']}#{bank_version}#{rank:05d}"
    variant_signature = compute_variant_signature(
        str(template["template_id"]), str(template["template_version"]), values
    )

    return {
        "item_instance_id": instance_id,
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "bank_version": bank_version,
        "variant_signature": variant_signature,
        "graph_version": template["graph_version"],
        "skill_ids": list(template["skill_ids"]),  # type: ignore[index]
        "question_type": template["question_type"],
        "difficulty": template["difficulty"],
        "language": language,
        "locale": pack.locale,
        "variables": dict(values),
        "stem": stem,
        "answer": answer,
        "simplest_form_only": compute_simplest_form_only(template),
        "distractors": distractors,
        "hints": hints,
        "solution_steps": solution_steps,
    }


def build_template_instances(
    template: Mapping[str, object],
    pack: LocalePack,
    bank_version: str,
    pool_cap: int,
) -> tuple[list[dict], int]:
    """Enumerate every valid, non-colliding variant (reusing item_verify's
    primitives, KTD5), sample the pool cap (KTD6), and materialize each
    selected variant into a dressed instance. Returns (instances,
    total_valid_variant_count)."""
    template_id = str(template["template_id"])
    question_type = template.get("question_type")
    if question_type not in ALLOWED_QUESTION_TYPES:
        raise BuildError(
            f"{template_id}: question_type {question_type!r} is outside v1 bank-build scope; "
            f"must be one of {sorted(ALLOWED_QUESTION_TYPES)}"
        )

    language = pack.language
    if language not in (template.get("stem_i18n") or {}):
        raise BuildError(
            f"{template_id}: no stem_i18n[{language!r}] to render for locale pack "
            f"{pack.locale!r} ({pack.source_path})"
        )

    domains = item_verify.build_domains(template)
    var_names = tuple(domains)
    constraints = [str(c) for c in template["constraints"]]  # type: ignore[index]

    valid_values: list[dict[str, int]] = []
    for combo in product(*domains.values()):
        values = dict(zip(var_names, combo))
        if not all(item_verify.evaluate_condition(c, values) for c in constraints):
            continue
        if item_verify.check_variant(template, values):
            continue  # collision variant dropped entirely (§3 Stage 3, R8)
        valid_values.append(values)

    total = len(valid_values)
    if total == 0:
        raise BuildError(f"{template_id}: zero non-colliding valid variants to materialize")

    seed = derive_seed(template_id, bank_version)
    selected_positions = sample_variant_indices(total, pool_cap, seed)

    instances = [
        materialize_instance(template, valid_values[pos], rank, pack, bank_version, language)
        for rank, pos in enumerate(selected_positions)
    ]
    return instances, total


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_instance(data: dict, validator: "jsonschema.Draft202012Validator", label: str) -> None:
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
        )
        raise BuildError(f"{label}: item-instance schema violation(s): {details}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_build(
    templates_dir: Path,
    locale_pack_path: Path,
    bank_version: str,
    pool_cap: int,
) -> tuple[dict, dict[str, list[dict]]]:
    """Build the manifest and per-template instance lists in memory (no I/O
    beyond reading inputs). Raises BuildError naming the offending template
    on any refusal."""
    pack = LocalePack.load(locale_pack_path)
    schema = json.loads(ITEM_INSTANCE_SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    template_paths = sorted(templates_dir.glob("*.yaml")) + sorted(templates_dir.glob("*.yml"))
    if not template_paths:
        raise BuildError(f"no templates found in {templates_dir}")

    manifest_templates = []
    per_template_instances: dict[str, list[dict]] = {}

    # Load every template up front so the all-templates loop below can pass
    # each one, as it verifies, the rest of this same walk as its
    # near-duplicate corpus (KTD2) — the corpus for bank_build's dedup pass
    # is naturally every other template already loaded in this walk.
    loaded = [(path, item_verify.load_template(path)) for path in template_paths]
    all_templates = [template for _, template in loaded]

    for path, template in loaded:
        corpus = [t for t in all_templates if t is not template]
        try:
            report = item_verify.verify_template(template, corpus)
        except item_verify.HardFail as exc:
            raise BuildError(f"{path.name}: Stage 3 verification hard-failed: {exc}") from exc

        if report.verdict == item_verify.FLAGGED:
            print(
                f"warning: {template['template_id']} is FLAGGED_FOR_REGENERATION by Stage 3 "
                f"({'; '.join(report.reasons)}) — building anyway (already promoted content)",
                file=sys.stderr,
            )

        instances, total_valid = build_template_instances(template, pack, bank_version, pool_cap)
        for instance in instances:
            validate_instance(instance, validator, instance["item_instance_id"])

        template_id = str(template["template_id"])
        per_template_instances[template_id] = instances
        manifest_templates.append({
            "template_id": template_id,
            "template_version": template["template_version"],
            "graph_version": template["graph_version"],
            "question_type": template["question_type"],
            "total_valid_variants": total_valid,
            "pool_size": len(instances),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    manifest = {
        "bank_version": bank_version,
        "pool_cap": pool_cap,
        "graph_versions": sorted({t["graph_version"] for t in manifest_templates}),
        "locale_pack": {
            "locale": pack.locale,
            "path": str(locale_pack_path),
            "sha256": hashlib.sha256(locale_pack_path.read_bytes()).hexdigest(),
        },
        "instance_count": sum(t["pool_size"] for t in manifest_templates),
        "templates": sorted(manifest_templates, key=lambda t: t["template_id"]),
    }
    return manifest, per_template_instances


def write_bank(out_dir: Path, bank_version: str, manifest: dict, per_template_instances: dict[str, list[dict]]) -> Path:
    """Write the build to a staging directory under out_dir, then rename it
    into place in one atomic step (§3 Stage 5: "banks are never patched in
    place"; stage2_draft.py's staging-dir discipline)."""
    target_dir = out_dir / bank_version
    if target_dir.exists():
        raise BuildError(
            f"bank version {bank_version!r} already exists at {target_dir} — banks are never "
            "patched in place; choose a new --bank-version"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / f".build-{bank_version}-{uuid.uuid4().hex[:8]}"
    instances_dir = staging / "instances"
    instances_dir.mkdir(parents=True)
    try:
        for template_id, instances in per_template_instances.items():
            (instances_dir / f"{template_id}.json").write_text(
                json.dumps(instances, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staging.rename(target_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--templates-dir", required=True, type=Path, help="dir of promoted item-record template YAMLs")
    parser.add_argument("--locale-pack", type=Path, default=DEFAULT_LOCALE_PACK, help="locale pack YAML (default content/locales/en-IN.yaml)")
    parser.add_argument("--out", required=True, type=Path, help="parent directory; the build writes <out>/<bank-version>/")
    parser.add_argument("--bank-version", required=True, help="version stamped on every instance in this build; refuses to overwrite an existing one")
    parser.add_argument("--pool-cap", type=int, default=DEFAULT_POOL_CAP, help=f"max instances materialized per template (default {DEFAULT_POOL_CAP}); KTD6")
    args = parser.parse_args(argv)

    if not args.templates_dir.is_dir():
        print(f"error: templates dir {args.templates_dir} does not exist", file=sys.stderr)
        return 2
    if not args.locale_pack.is_file():
        print(f"error: locale pack {args.locale_pack} does not exist", file=sys.stderr)
        return 2
    if args.pool_cap < 1:
        print("error: --pool-cap must be >= 1", file=sys.stderr)
        return 2

    try:
        manifest, per_template_instances = run_build(
            args.templates_dir, args.locale_pack, args.bank_version, args.pool_cap
        )
        target_dir = write_bank(args.out, args.bank_version, manifest, per_template_instances)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"bank {args.bank_version} built: {manifest['instance_count']} instance(s) across "
        f"{len(manifest['templates'])} template(s) -> {target_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
