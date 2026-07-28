#!/usr/bin/env python3
"""Lint a Mathiqo skill-graph content directory.

Usage:
    python tools/graph_lint.py content/fractions/ [--warnings-as-errors] [--schemas DIR]

Checks, each reported with the offending node/id:
  1. Schema validation of every skill node, misconception, and mapping
     against the JSON Schemas in schemas/.
  2. Reference resolution: prerequisites, node misconception lists,
     misconception remediation targets, and mapping skill_ids must all
     resolve to defined objects.
  3. Acyclicity of the prerequisite DAG (reports the cycle path).
  4. Orphan detection: nodes with no prerequisites and no dependents that
     are not strand roots (warning).
  5. Near-duplicate skill-name detection (warning).

Exit code is non-zero if any errors were found, or if --warnings-as-errors
is passed and any warnings were found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMAS_DIR = REPO_ROOT / "schemas"

# Top-level YAML key -> (schema filename, id field used in error messages)
CONTENT_KINDS = {
    "skills": ("skill-node.schema.json", "skill_id"),
    "misconceptions": ("misconception.schema.json", "misconception_id"),
    "mappings": ("mapping.schema.json", "skill_id"),
}

# Fields that make an object's identity unique, for duplicate-id detection.
# skill_id/misconception_id are true unique identifiers; a mapping ROW's
# identity is the (skill, country, curriculum, grade, unit) it targets —
# skill_id alone is NOT unique across mappings (core doc §1.4: several rows
# per skill, one per country and one per grade that touches it). KTD10 folds
# the structured unit_code into this identity too: two rows for the same
# (skill, country, curriculum, grade) that name two different units would
# otherwise collide as a false duplicate.
DEDUP_KEY_FIELDS = {
    "skills": ("skill_id",),
    "misconceptions": ("misconception_id",),
    "mappings": ("skill_id", "country", "curriculum", "grade", "unit_code"),
}

# Fields inside a DEDUP_KEY_FIELDS tuple that are optional on the object
# itself (KTD10: unit_code is absent for curricula with no chapter
# granularity, e.g. Ontario). A missing optional field contributes None to
# the key rather than excluding the object from dedup checking entirely —
# only a missing *required* field (schema validation already flags those)
# skips the object, same as before this field was added.
OPTIONAL_DEDUP_KEY_FIELDS = {"unit_code"}

# Normalized skill-name similarity at or above this ratio is flagged.
# Simple string similarity is a v1 stand-in; embedding-based duplicate
# detection (core doc §3 Stage 3) replaces this once the pipeline exists.
NEAR_DUPLICATE_THRESHOLD = 0.85


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


MANIFEST_FILENAME = "manifest.yaml"


def check_manifest(content_dir: Path, result: LintResult) -> None:
    """KTD9: every content pack must declare `content_version` in a
    manifest.yaml alongside its content files — mirrors the check
    orchestrator/engine.py's `load_content` enforces at load time (there, by
    raising), but here as a lint error naming the missing/malformed file so
    a content author sees it before it ever reaches a loader."""
    manifest_path = content_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        result.errors.append(
            f"{content_dir}: missing {MANIFEST_FILENAME} (every content pack must declare content_version)"
        )
        return
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        result.errors.append(f"{MANIFEST_FILENAME}: YAML parse error: {exc}")
        return
    if not isinstance(data, dict) or not data.get("content_version"):
        result.errors.append(f"{MANIFEST_FILENAME}: missing required 'content_version' field")


def load_content(content_dir: Path, result: LintResult) -> dict[str, list[dict[str, Any]]]:
    """Load every YAML file in content_dir and merge the known top-level
    keys. manifest.yaml is excluded — it carries pack metadata (KTD9,
    checked separately by `check_manifest`), not skills/misconceptions/
    mappings entries, so folding it into this scan would only produce
    spurious "unknown top-level key" warnings for its `pack`/
    `content_version` fields."""
    content: dict[str, list[dict[str, Any]]] = {kind: [] for kind in CONTENT_KINDS}
    yaml_files = sorted(p for p in content_dir.glob("*.yaml") if p.name != MANIFEST_FILENAME) + sorted(
        p for p in content_dir.glob("*.yml") if p.name != MANIFEST_FILENAME
    )
    if not yaml_files:
        result.errors.append(f"{content_dir}: no .yaml/.yml content files found")
        return content

    for path in yaml_files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            result.errors.append(f"{path.name}: YAML parse error: {exc}")
            continue
        if data is None:
            continue
        if not isinstance(data, dict):
            result.errors.append(f"{path.name}: top level must be a mapping of content-kind keys")
            continue
        for key, value in data.items():
            if key not in CONTENT_KINDS:
                result.warnings.append(f"{path.name}: ignoring unknown top-level key {key!r}")
                continue
            if not isinstance(value, list):
                result.errors.append(f"{path.name}: {key!r} must be a list of objects")
                continue
            for obj in value:
                if isinstance(obj, dict):
                    obj.setdefault("__source__", path.name)
                    content[key].append(obj)
                else:
                    result.errors.append(f"{path.name}: entry in {key!r} is not an object")
    return content


def load_schemas(schemas_dir: Path) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for schema_file, _ in CONTENT_KINDS.values():
        with (schemas_dir / schema_file).open(encoding="utf-8") as fh:
            schemas[schema_file] = json.load(fh)
    return schemas


def validate_against_schemas(
    content: dict[str, list[dict[str, Any]]],
    schemas: dict[str, dict[str, Any]],
    result: LintResult,
) -> None:
    for kind, (schema_file, id_field) in CONTENT_KINDS.items():
        schema = schemas[schema_file]
        validator = jsonschema.Draft202012Validator(schema)
        for obj in content[kind]:
            # __source__ is linter bookkeeping, not content; exclude it from validation.
            payload = {k: v for k, v in obj.items() if k != "__source__"}
            label = obj.get(id_field, "<missing id>")
            for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
                where = ".".join(str(p) for p in error.path) or "<root>"
                result.errors.append(
                    f"{obj.get('__source__', '?')}: schema violation in {kind[:-1]} {label!r} "
                    f"at {where}: {error.message}"
                )


def check_duplicate_ids(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    for kind, fields in DEDUP_KEY_FIELDS.items():
        required_fields = [f for f in fields if f not in OPTIONAL_DEDUP_KEY_FIELDS]
        seen: dict[tuple, str] = {}
        for obj in content[kind]:
            if any(field not in obj for field in required_fields):
                continue  # missing required field(s); schema validation already flags this
            key = tuple(obj.get(field) for field in fields)
            label = "/".join(str(part) for part in key if part is not None) or str(key[0])
            if key in seen:
                result.errors.append(
                    f"duplicate {kind[:-1]} {label!r} "
                    f"(in {seen[key]} and {obj.get('__source__', '?')})"
                )
            else:
                seen[key] = obj.get("__source__", "?")


def check_unit_name_consistency(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    """KTD10: a unit_code identifies one curriculum unit/chapter, shared
    across every skill mapped into it — many mapping rows legitimately share
    one unit_code. What they must NOT do is disagree on unit_name: two rows
    naming the same unit_code with two different display names would make
    "the chapter list" ambiguous for a consumer reading structured units
    instead of free-text syllabus_ref (R2/R3)."""
    seen: dict[str, tuple[str | None, str]] = {}
    for obj in content["mappings"]:
        unit_code = obj.get("unit_code")
        if unit_code is None:
            continue
        unit_name = obj.get("unit_name")
        source = obj.get("__source__", "?")
        prior = seen.get(unit_code)
        if prior is None:
            seen[unit_code] = (unit_name, source)
            continue
        prior_name, prior_source = prior
        if unit_name != prior_name:
            result.errors.append(
                f"mapping unit {unit_code!r}: inconsistent unit_name {unit_name!r} "
                f"(in {source}) vs {prior_name!r} (in {prior_source})"
            )


def check_references(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    skill_ids = {s["skill_id"] for s in content["skills"] if "skill_id" in s}
    misc_ids = {m["misconception_id"] for m in content["misconceptions"] if "misconception_id" in m}

    for node in content["skills"]:
        node_id = node.get("skill_id", "<missing id>")
        for prereq in node.get("prerequisites", []):
            if prereq not in skill_ids:
                result.errors.append(f"skill {node_id}: prerequisite {prereq!r} is not a defined skill")
        for misc in node.get("misconceptions", []):
            if misc not in misc_ids:
                result.errors.append(f"skill {node_id}: misconception {misc!r} is not in the registry")

    for misc in content["misconceptions"]:
        misc_id = misc.get("misconception_id", "<missing id>")
        target = misc.get("remediation_target")
        if target is not None and target not in skill_ids:
            result.errors.append(f"misconception {misc_id}: remediation target {target!r} is not a defined skill")

    for mapping in content["mappings"]:
        skill_id = mapping.get("skill_id", "<missing id>")
        label = f"{mapping.get('country', '?')}/{mapping.get('curriculum', '?')} grade {mapping.get('grade', '?')}"
        if skill_id not in skill_ids:
            result.errors.append(f"mapping ({label}): skill_id {skill_id!r} is not a defined skill")


def check_acyclic(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    """DFS over prerequisite edges; reports one concrete cycle path per cycle found."""
    skill_ids = {s["skill_id"] for s in content["skills"] if "skill_id" in s}
    graph = {
        node["skill_id"]: [p for p in node.get("prerequisites", []) if p in skill_ids]
        for node in content["skills"]
        if "skill_id" in node
    }

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node_id: WHITE for node_id in graph}
    stack: list[str] = []
    reported: set[frozenset[str]] = set()

    def visit(node_id: str) -> None:
        color[node_id] = GRAY
        stack.append(node_id)
        for prereq in graph.get(node_id, []):
            if color[prereq] == GRAY:
                cycle = stack[stack.index(prereq):] + [prereq]
                members = frozenset(cycle)
                if members not in reported:
                    reported.add(members)
                    result.errors.append(f"prerequisite cycle detected: {' -> '.join(cycle)}")
            elif color[prereq] == WHITE:
                visit(prereq)
        stack.pop()
        color[node_id] = BLACK

    for node_id in graph:
        if color[node_id] == WHITE:
            visit(node_id)


def check_orphans(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    nodes = {n["skill_id"]: n for n in content["skills"] if "skill_id" in n}
    dependents: dict[str, set[str]] = {skill_id: set() for skill_id in nodes}
    for node in content["skills"]:
        for prereq in node.get("prerequisites", []):
            if prereq in dependents:
                dependents[prereq].add(node.get("skill_id", "?"))

    # A strand root is the lowest canonical_stage node in its strand.
    stage_by_strand: dict[str, int] = {}
    for node in nodes.values():
        strand = node.get("strand")
        stage = node.get("canonical_stage")
        if strand is None or stage is None:
            continue
        stage_by_strand[strand] = min(stage, stage_by_strand.get(strand, stage))

    for skill_id, node in sorted(nodes.items()):
        if node.get("prerequisites") or dependents[skill_id]:
            continue
        is_root = (
            node.get("strand") in stage_by_strand
            and node.get("canonical_stage") == stage_by_strand[node["strand"]]
        )
        if not is_root:
            result.warnings.append(
                f"skill {skill_id}: orphan node — no prerequisites and no dependents, "
                f"and not a strand root of {node.get('strand', '?')}"
            )


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", name.lower()).strip()


def check_near_duplicates(content: dict[str, list[dict[str, Any]]], result: LintResult) -> None:
    nodes = sorted(
        (n for n in content["skills"] if "skill_id" in n and "name" in n),
        key=lambda n: n["skill_id"],
    )
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            ratio = SequenceMatcher(None, normalize_name(a["name"]), normalize_name(b["name"])).ratio()
            if ratio >= NEAR_DUPLICATE_THRESHOLD:
                result.warnings.append(
                    f"near-duplicate skill names ({ratio:.2f}): "
                    f"{a['skill_id']} ({a['name']!r}) vs {b['skill_id']} ({b['name']!r})"
                )


def lint_directory(content_dir: Path, schemas_dir: Path = DEFAULT_SCHEMAS_DIR) -> LintResult:
    result = LintResult()
    if not content_dir.is_dir():
        result.errors.append(f"{content_dir}: not a directory")
        return result
    try:
        schemas = load_schemas(schemas_dir)
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"failed to load schemas from {schemas_dir}: {exc}")
        return result

    check_manifest(content_dir, result)
    content = load_content(content_dir, result)
    validate_against_schemas(content, schemas, result)
    check_duplicate_ids(content, result)
    check_unit_name_consistency(content, result)
    check_references(content, result)
    check_acyclic(content, result)
    check_orphans(content, result)
    check_near_duplicates(content, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint a Mathiqo skill-graph content directory.")
    parser.add_argument("content_dir", type=Path, help="directory of YAML content files")
    parser.add_argument(
        "--schemas",
        type=Path,
        default=DEFAULT_SCHEMAS_DIR,
        help=f"directory of JSON Schemas (default: {DEFAULT_SCHEMAS_DIR})",
    )
    parser.add_argument(
        "--warnings-as-errors",
        action="store_true",
        help="exit non-zero if any warnings were found",
    )
    args = parser.parse_args(argv)

    result = lint_directory(args.content_dir, args.schemas)

    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}")
    print(f"graph_lint: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")

    if result.errors or (args.warnings_as_errors and result.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
