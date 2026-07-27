#!/usr/bin/env python3
"""Stage 2 drafting harness: AI-drafted item templates, verify-gated.

Automates core design doc §3 Stage 2: for one skill, an LLM drafts N item
templates (stem, variables, constraints, answer, tagged distractors,
solution steps, hints — §2.2), and tools/item_verify.py is the trust gate
every draft must pass. Unlike tools/stage1_draft.py's binary lint result,
item_verify has three verdicts (PASS / FLAGGED_FOR_REGENERATION /
HARD_FAIL) — both non-PASS verdicts bounce the draft back to the LLM, with
FLAGGED bounces carrying the actual rejection/collision rates and which
distractor is colliding, not just a pass/fail signal, so a regeneration can
narrow ranges or fix distractor logic rather than guess blindly.

Usage:
    python tools/stage2_draft.py --skill FRAC.ADD.UNLIKE \
        --content-dir content/fractions --templates-dir content/templates \
        --staging-dir staging/ --count 5 --reviewer rishi \
        [--backend cmd|file] [--max-retries 3]

Backends: same two as stage1_draft.py (reused directly — see that module),
except the cmd backend reads $STAGE2_LLM_CMD rather than $STAGE1_LLM_CMD.

The LLM drafts only the *content* of a template (everything §2.2 actually
authors); the harness fills in publishing scaffolding the schema requires
but no author invents by hand — template_version, bank_version,
graph_version, tts, the numeric difficulty scalar (derived from the
authored difficulty_prior band), and provenance. There is no automated
Stage 5 bank-build yet, so --bank-version is a placeholder string, same as
the hand-authored fixture template. provenance.reviewer is stamped from
--reviewer as whoever is accountable for running this batch — it is NOT a
claim that Stage 4 human review (§3 Stage 4, still separate, still manual)
has happened; accepted drafts land in <staging-dir>/accepted/, never in
content/templates/, and a human promotes them by hand exactly as in
stage1_draft.py.

v1 scope restrictions enforced by this harness (core doc §4), stricter than
the bare item-record schema allows: question_type must be mcq_4 or
numeric_entry; skill_ids must be exactly the one skill being drafted for
(single-skill items only — no exam-context multi-skill drafting here);
early-grade `render` props (§2.9) are out of scope, since the v1 slice
targets canonical stages ≈ Classes 3-6, not Classes 1-3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lint  # noqa: E402
import item_verify  # noqa: E402
import stage1_draft  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts" / "stage2"
SCHEMAS_DIR = REPO_ROOT / "schemas"

GENERATOR_VERSION = "stage2-gen/0.1.0"

# v1 restriction (core doc §4): only these two question types.
ALLOWED_QUESTION_TYPES = frozenset({"mcq_4", "numeric_entry"})

# Named E/M/H bands mapped to a scalar midpoint (§2.3: "named bands over
# [the] scalar"), pending Stage 6 empirical calibration — same placeholder
# convention as the hand-authored fixture's `difficulty: 0.5  # placeholder
# scalar in the medium band; Stage 6 IRT/Elo calibration moves it (§2.3)`.
DIFFICULTY_BAND_MIDPOINT = {"easy": 0.2, "medium": 0.5, "hard": 0.8}

# The fields the LLM drafts (§2.2 content) vs. the fields the harness fills
# in afterward (publishing scaffolding — see module docstring).
CONTENT_FIELDS = frozenset({
    "template_id", "skill_ids", "question_type", "cognitive_level",
    "difficulty_prior", "estimated_time_sec", "calculator_allowed",
    "diagram_required", "accepted_answer_forms", "context_tags",
    "exam_contexts", "variables", "constraints", "stem_i18n",
    "localization_slots", "answer", "distractors", "solution_steps_i18n",
    "hints_i18n",
})
# accepted_answer_forms is optional per the item-record schema; everything
# else in CONTENT_FIELDS is required *of the LLM's draft* by this harness
# (difficulty_prior is optional in the bare schema but required here, since
# the harness's difficulty-scalar fill depends on an explicit E/M/H choice).
REQUIRED_CONTENT_FIELDS = CONTENT_FIELDS - {"accepted_answer_forms"}
SCAFFOLD_FIELDS = frozenset({
    "template_version", "bank_version", "graph_version", "tts",
    "difficulty", "provenance",
})


def load_skill_context(content_dir: Path, skill_id: str) -> tuple[dict, list[dict], list[str]]:
    """The skill node, its registered misconceptions, and any exam_contexts
    already on file for it in the mapping layer — everything the prompt
    inlines about the skill (core doc §3 Stage 2's prompt-context list)."""
    content = graph_lint.load_content(content_dir, graph_lint.LintResult())
    skills_by_id = {s["skill_id"]: s for s in content["skills"] if "skill_id" in s}
    if skill_id not in skills_by_id:
        raise SystemExit(f"error: skill {skill_id!r} not found in {content_dir}")
    skill_node = skills_by_id[skill_id]

    misc_by_id = {m["misconception_id"]: m for m in content["misconceptions"] if "misconception_id" in m}
    skill_miscs = [
        misc_by_id[mid] for mid in skill_node.get("misconceptions", []) if mid in misc_by_id
    ]

    exam_contexts = sorted({
        ec
        for mapping in content["mappings"]
        if mapping.get("skill_id") == skill_id
        for ec in (mapping.get("exam_contexts") or [])
    })
    return skill_node, skill_miscs, exam_contexts


def load_existing_template_ids(templates_dir: Path) -> list[str]:
    """template_ids already published, so a draft never collides with one."""
    ids: list[str] = []
    for path in sorted(templates_dir.glob("*.yaml")) + sorted(templates_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SystemExit(f"error: {path}: not valid YAML: {exc}")
        if isinstance(data, dict) and isinstance(data.get("template_id"), str):
            ids.append(data["template_id"])
    return sorted(ids)


def format_skill_definition(skill_node: dict) -> str:
    return (
        f"name: {skill_node.get('name', '')}\n"
        f"description: {str(skill_node.get('description', '')).strip()}\n"
        f"strand: {skill_node.get('strand', '')}\n"
        f"canonical_stage: {skill_node.get('canonical_stage', '')}\n"
        f"prerequisites: {skill_node.get('prerequisites', [])}"
    )


def format_misconceptions(miscs: list[dict]) -> str:
    if not miscs:
        return "(none registered for this skill)"
    return "\n".join(
        f"- {m.get('misconception_id')}: \"{m.get('belief')}\" "
        f"(signature error: {m.get('signature_error')})"
        for m in miscs
    )


def format_exam_contexts_hint(exam_contexts: list[str]) -> str:
    return "\n".join(f"- {ec}" for ec in exam_contexts) if exam_contexts else "(none on file)"


def build_slots(
    skill_id: str,
    skill_node: dict,
    skill_miscs: list[dict],
    exam_contexts_hint: list[str],
    known_template_ids: list[str],
) -> dict[str, str]:
    schema = json.loads((SCHEMAS_DIR / "item-record.schema.json").read_text(encoding="utf-8"))
    return {
        "skill_id": skill_id,
        "skill_definition": format_skill_definition(skill_node),
        "skill_misconceptions": format_misconceptions(skill_miscs),
        "exam_contexts_hint": format_exam_contexts_hint(exam_contexts_hint),
        "existing_template_ids": stage1_draft.format_ids(known_template_ids),
        "schema_json": json.dumps(schema, indent=2),
    }


def check_response_shape(yaml_text: str) -> tuple[Optional[dict], Optional[str]]:
    """Cheap pre-verify gate: parses to a mapping of exactly the content
    fields (no pipeline scaffolding, nothing unrecognized, nothing missing).
    Deep validation (types, patterns, references) is item_verify's job."""
    try:
        data: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return None, f"response is not valid YAML: {exc}"
    if not isinstance(data, dict):
        return None, "response must be a YAML mapping of the content fields listed in the prompt"

    unknown = set(data) - CONTENT_FIELDS
    scaffold_leak = unknown & SCAFFOLD_FIELDS
    if scaffold_leak:
        return None, (
            f"response includes pipeline-filled scaffolding field(s) {sorted(scaffold_leak)}: "
            "the harness fills these in after verification; do not draft them"
        )
    if unknown:
        return None, f"response has unknown top-level key(s) {sorted(unknown)}; expected only content fields"

    missing = REQUIRED_CONTENT_FIELDS - set(data)
    if missing:
        return None, f"response is missing required content field(s) {sorted(missing)}"
    return data, None


def check_v1_restrictions(content: dict, skill_id: str) -> list[str]:
    """Restrictions this harness enforces beyond the bare schema (§4)."""
    errors: list[str] = []
    question_type = content.get("question_type")
    if question_type not in ALLOWED_QUESTION_TYPES:
        errors.append(
            f"question_type {question_type!r} is outside v1 scope; "
            f"must be one of {sorted(ALLOWED_QUESTION_TYPES)}"
        )
    skill_ids = content.get("skill_ids")
    if skill_ids != [skill_id]:
        errors.append(
            f"skill_ids must be exactly [{skill_id!r}] (single-skill only for this "
            f"harness); got {skill_ids!r}"
        )
    difficulty_prior = content.get("difficulty_prior")
    if difficulty_prior not in DIFFICULTY_BAND_MIDPOINT:
        errors.append(
            f"difficulty_prior {difficulty_prior!r} must be one of "
            f"{sorted(DIFFICULTY_BAND_MIDPOINT)}"
        )
    return errors


def check_template_id_collision(template_id: object, known_ids: set[str]) -> list[str]:
    if not isinstance(template_id, str) or not template_id:
        return ["template_id is missing or not a non-empty string"]
    if template_id in known_ids:
        return [f"template_id {template_id!r} already exists; propose a new, unused id"]
    return []


def fill_scaffolding(content: dict, **scaffold: Any) -> dict:
    """Merge the LLM's content draft with harness-filled publishing fields
    (see module docstring) into a full item-record dict."""
    difficulty = DIFFICULTY_BAND_MIDPOINT[content["difficulty_prior"]]
    return {
        **content,
        "template_version": scaffold["template_version"],
        "bank_version": scaffold["bank_version"],
        "graph_version": scaffold["graph_version"],
        "difficulty": difficulty,
        "tts": scaffold["tts"],
        "provenance": {
            "generator_version": scaffold["generator_version"],
            "reviewer": scaffold["reviewer"],
            "date": scaffold["date"],
        },
    }


@dataclass
class DraftOutcome:
    slot: int
    accepted: bool
    attempts: int
    template_dict: Optional[dict]
    yaml_text: str
    prompt_sha256: str
    errors: list[str]
    verify_report: Optional[dict]


def run_template_draft(
    slot: int,
    template_prompt: str,
    slots: dict[str, str],
    backend: stage1_draft.Backend,
    known_ids: set[str],
    skill_id: str,
    scaffold: dict[str, Any],
    max_retries: int,
) -> DraftOutcome:
    """Draft -> verify -> bounce loop for one template. First try plus
    max_retries re-prompts. Both HARD_FAIL and FLAGGED_FOR_REGENERATION
    verdicts bounce (§3 Stage 3); only PASS accepts."""
    feedback = ""
    last_errors: list[str] = []
    last_response = ""
    last_hash = ""
    last_report: Optional[dict] = None

    for attempt in range(1, max_retries + 2):
        prompt = stage1_draft.render(template_prompt, {**slots, "verify_feedback": feedback})
        last_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        last_response = backend.complete(prompt)
        last_report = None

        content, shape_error = check_response_shape(last_response)
        if shape_error is not None:
            last_errors = [shape_error]
        else:
            assert content is not None
            last_errors = check_v1_restrictions(content, skill_id)
            last_errors += check_template_id_collision(content.get("template_id"), known_ids)

        if not last_errors:
            assert content is not None
            full = fill_scaffolding(content, **scaffold)
            try:
                item_verify.validate_against_item_schema(full, str(content["template_id"]))
                report = item_verify.verify_template(full)
            except item_verify.HardFail as exc:
                last_errors = [str(exc)]
            else:
                if report.verdict == item_verify.PASS:
                    return DraftOutcome(
                        slot=slot, accepted=True, attempts=attempt,
                        template_dict=full, yaml_text=last_response,
                        prompt_sha256=last_hash, errors=[],
                        verify_report=report.to_dict(),
                    )
                last_errors = list(report.reasons)
                last_report = report.to_dict()

        feedback = (
            "\n## Previous draft failed these checks\n\n"
            + "\n".join(f"- {e}" for e in last_errors)
            + "\n\nReturn a corrected full draft fixing every issue above. "
            "Output ONLY YAML, no prose.\n"
        )

    return DraftOutcome(
        slot=slot, accepted=False, attempts=max_retries + 1, template_dict=None,
        yaml_text=last_response, prompt_sha256=last_hash, errors=last_errors,
        verify_report=last_report,
    )


def write_accepted(staging_dir: Path, outcome: DraftOutcome, meta: dict[str, Any]) -> Path:
    assert outcome.template_dict is not None
    accepted_dir = staging_dir / "accepted"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    out_path = accepted_dir / f"template-{outcome.slot}.yaml"
    out_path.write_text(yaml.safe_dump(outcome.template_dict, sort_keys=False), encoding="utf-8")
    manifest = {
        **meta,
        "slot": outcome.slot,
        "template_id": outcome.template_dict["template_id"],
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "attempts": outcome.attempts,
        "retries_used": outcome.attempts - 1,
        "prompt_sha256": outcome.prompt_sha256,
        "verify_report": outcome.verify_report,
    }
    (accepted_dir / f"template-{outcome.slot}.manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return out_path


def write_rejected(staging_dir: Path, outcome: DraftOutcome, meta: dict[str, Any]) -> Path:
    rejected_dir = staging_dir / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    out_path = rejected_dir / f"template-{outcome.slot}.yaml"
    out_path.write_text(outcome.yaml_text, encoding="utf-8")
    report = {
        **meta,
        "slot": outcome.slot,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "attempts": outcome.attempts,
        "prompt_sha256": outcome.prompt_sha256,
        "errors": outcome.errors,
        "last_verify_report": outcome.verify_report,
    }
    (rejected_dir / f"template-{outcome.slot}.errors.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 template-drafting harness (see module docstring).")
    parser.add_argument("--skill", required=True, help="skill_id to draft templates for, e.g. FRAC.ADD.UNLIKE")
    parser.add_argument("--content-dir", required=True, type=Path, help="content dir holding the skill graph + registry (read-only)")
    parser.add_argument("--templates-dir", required=True, type=Path, help="existing item-template dir, for id collision checks (read-only)")
    parser.add_argument("--staging-dir", required=True, type=Path, help="where drafts, manifests, and rejections go")
    parser.add_argument("--count", type=int, default=1, help="templates to draft for this skill (default 1)")
    parser.add_argument("--max-retries", type=int, default=3, help="re-prompts per template after the first try")
    parser.add_argument("--backend", choices=["cmd", "file"], default="file")
    parser.add_argument("--print-prompts", action="store_true", help="write first-round prompts to the staging dir and exit")
    parser.add_argument("--template-version", default="1.0.0", help="template_version stamped on accepted drafts")
    parser.add_argument("--bank-version", default="dev", help="bank_version stamped on accepted drafts (placeholder; no automated Stage 5 build exists yet)")
    parser.add_argument("--graph-version", default="1.0.0", help="graph_version this draft is authored against")
    parser.add_argument("--tts", action="store_true", help="mark drafted templates as needing TTS audio (early-grade skills, core doc §2.9)")
    # Whoever is accountable for this batch — NOT a claim that Stage 4 human
    # review has happened (that gate is separate and still manual; accepted
    # drafts land in staging/accepted/, never in content/templates/).
    parser.add_argument("--reviewer", required=True, help="who is running/accountable for this batch; stamped into provenance.reviewer")
    parser.add_argument("--generator-version", default=GENERATOR_VERSION, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not args.content_dir.is_dir():
        print(f"error: content dir {args.content_dir} does not exist", file=sys.stderr)
        return 2
    if not args.templates_dir.is_dir():
        print(f"error: templates dir {args.templates_dir} does not exist", file=sys.stderr)
        return 2
    if args.count < 1:
        print("error: --count must be >= 1", file=sys.stderr)
        return 2

    skill_node, skill_miscs, exam_contexts_hint = load_skill_context(args.content_dir, args.skill)
    known_ids = set(load_existing_template_ids(args.templates_dir))
    args.staging_dir.mkdir(parents=True, exist_ok=True)
    template_prompt = (PROMPTS_DIR / "draft_template.md").read_text(encoding="utf-8")

    if args.print_prompts:
        slots = build_slots(args.skill, skill_node, skill_miscs, exam_contexts_hint, sorted(known_ids))
        for i in range(1, args.count + 1):
            prompt = stage1_draft.render(template_prompt, {**slots, "verify_feedback": ""})
            path = args.staging_dir / f"prompt-{i}.md"
            stage1_draft.write_prompt(path, prompt)
            print(f"wrote {path}")
        print(f"prompts written; provide response-<n>.yaml for n in 1..{args.count} "
              "and re-run without --print-prompts")
        return 0

    backend: stage1_draft.Backend
    if args.backend == "cmd":
        backend = stage1_draft.CmdBackend("STAGE2_LLM_CMD")
    else:
        backend = stage1_draft.FileBackend(args.staging_dir)

    scaffold = {
        "template_version": args.template_version,
        "bank_version": args.bank_version,
        "graph_version": args.graph_version,
        "tts": args.tts,
        "generator_version": args.generator_version,
        "reviewer": args.reviewer,
        "date": datetime.now(timezone.utc).date().isoformat(),
    }
    meta = {"skill": args.skill, "backend": args.backend, "max_retries": args.max_retries}

    accepted_count = 0
    for slot in range(1, args.count + 1):
        slots = build_slots(args.skill, skill_node, skill_miscs, exam_contexts_hint, sorted(known_ids))
        outcome = run_template_draft(
            slot, template_prompt, slots, backend, known_ids, args.skill, scaffold, args.max_retries,
        )
        if outcome.accepted:
            out_path = write_accepted(args.staging_dir, outcome, meta)
            assert outcome.template_dict is not None
            known_ids.add(outcome.template_dict["template_id"])
            accepted_count += 1
            print(f"[slot {slot}] accepted after {outcome.attempts} attempt(s) "
                  f"-> {out_path} ({outcome.template_dict['template_id']})")
        else:
            write_rejected(args.staging_dir, outcome, meta)
            print(f"[slot {slot}] REJECTED after {outcome.attempts} attempt(s) "
                  f"-> {args.staging_dir}/rejected/template-{slot}.yaml", file=sys.stderr)
            for error in outcome.errors:
                print(f"[slot {slot}] ERROR: {error}", file=sys.stderr)

    print(f"{accepted_count}/{args.count} template(s) accepted; a human reviews and promotes "
          f"{args.staging_dir}/accepted/*.yaml into {args.templates_dir} — provenance.reviewer "
          "records who ran this batch, not a completed Stage 4 approval")
    return 0 if accepted_count == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
