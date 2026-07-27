#!/usr/bin/env python3
"""Stage 1 drafting harness: AI-drafted skill graphs, lint-gated.

Automates core design doc §3 Stage 1: an LLM drafts a strand's skill subgraph,
misconception registry entries, and (optionally) a country mapping from
syllabus text, and tools/graph_lint.py is the trust gate every draft must
pass. Failed drafts are bounced back to the LLM with the lint errors verbatim,
up to --max-retries times.

Usage:
    python tools/stage1_draft.py --strand NUMBER.FRACTIONS \
        --syllabus examples/ncert-fractions-excerpt.txt \
        --content-dir content/fractions --staging-dir staging/ \
        [--country IN --curriculum NCERT] [--backend cmd|file] [--max-retries 3]

Backends:
    cmd   runs $STAGE1_LLM_CMD with the prompt on stdin, YAML expected on stdout.
    file  writes prompts to <staging-dir>/prompt-<n>.md and consumes
          <staging-dir>/response-<n>.yaml, which a human or agent fills in.
          Responses are consumed in order, so a re-run resumes after the last
          consumed response. --print-prompts writes the first-round prompts
          and exits without calling any backend.

Accepted drafts land in <staging-dir>/accepted/ with a manifest; rejected
drafts are quarantined in <staging-dir>/rejected/ with the error report.
Nothing is ever written into the content dir — a human promotes accepted
drafts into content/ by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lint  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts" / "stage1"
SCHEMAS_DIR = REPO_ROOT / "schemas"

CONTENT_KEYS = ("skills", "misconceptions", "mappings")


@dataclass(frozen=True)
class DraftStep:
    """One draft in the pipeline: name, prompt template, schema to inline."""

    name: str
    template_file: str
    schema_file: str


DRAFT_STEPS = [
    DraftStep("strand", "draft_strand.md", "skill-node.schema.json"),
    DraftStep("misconceptions", "draft_misconceptions.md", "misconception.schema.json"),
    DraftStep("mapping", "draft_mapping.md", "mapping.schema.json"),
]


class Backend(Protocol):
    def complete(self, prompt: str) -> str: ...


class CmdBackend:
    """Runs $<env_var>: prompt on stdin, YAML response on stdout.

    env_var defaults to STAGE1_LLM_CMD for this module's own use; other
    harnesses (tools/stage2_draft.py) reuse this class with their own env
    var name rather than duplicating it.
    """

    def __init__(self, env_var: str = "STAGE1_LLM_CMD") -> None:
        cmd = os.environ.get(env_var)
        if not cmd:
            raise SystemExit(f"{env_var} is not set; required for --backend cmd")
        self.cmd = shlex.split(cmd)

    def complete(self, prompt: str) -> str:
        proc = subprocess.run(self.cmd, input=prompt, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"LLM command failed (exit {proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout


def _prompt_hash_path(prompt_path: Path) -> Path:
    return prompt_path.with_suffix(prompt_path.suffix + ".sha256")


def write_prompt(prompt_path: Path, prompt: str) -> None:
    """Write a prompt file plus its content-hash sidecar (see FileBackend)."""
    prompt_path.write_text(prompt, encoding="utf-8")
    _prompt_hash_path(prompt_path).write_text(
        hashlib.sha256(prompt.encode("utf-8")).hexdigest(), encoding="utf-8"
    )


class FileBackend:
    """Human/agent-in-the-loop: prompt-<n>.md out, response-<n>.yaml in.

    A resumed process replays every draft/lint decision from scratch (accepted
    drafts, staged ids, retry feedback all live only in this process's memory,
    §`run_draft`), so a fresh run must re-issue the *same* complete() calls in
    the *same* order as the run that got interrupted, transparently consuming
    each already-answered response along the way and pausing only at the
    first position that still lacks one. counter therefore starts at 0 and
    advances by exactly one position per call — it must never skip ahead by
    counting existing response files, or an already-answered position (e.g.
    response-1.yaml, filled in after --print-prompts) is silently never read
    back into run_draft, and every step after it proceeds with wrong state
    (missing staged ids from the skipped step).

    Each prompt is written with a content-hash sidecar; before consuming an
    existing response, the sidecar is checked against the hash of the prompt
    this run actually generated at that position, so a stale response — e.g.
    one staged by --print-prompts, whose prompts don't yet reflect any step's
    accepted draft, being reused once a real run reaches that position with
    different slots — fails loudly instead of silently pairing with the
    wrong prompt.
    """

    def __init__(self, staging_dir: Path) -> None:
        self.staging_dir = staging_dir
        self.counter = 0

    def complete(self, prompt: str) -> str:
        self.counter += 1
        prompt_path = self.staging_dir / f"prompt-{self.counter}.md"
        response_path = self.staging_dir / f"response-{self.counter}.yaml"
        expected_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if not response_path.exists():
            write_prompt(prompt_path, prompt)
            raise SystemExit(
                f"[file backend] wrote {prompt_path}\n"
                f"fill in {response_path} with the LLM's YAML response, "
                f"then re-run the same command"
            )
        hash_path = _prompt_hash_path(prompt_path)
        if hash_path.is_file() and hash_path.read_text(encoding="utf-8").strip() != expected_hash:
            raise SystemExit(
                f"[file backend] {response_path} was staged against a different prompt than "
                f"this run generates at position {self.counter} (flags changed, or an earlier "
                f"step's accepted draft now feeds different ids into this prompt). Remove the "
                f"stale staging files or re-run --print-prompts to regenerate them before "
                f"filling in responses again."
            )
        # Always (re-)write prompt+hash for this position, even though a
        # response already exists: keeps the sidecar honest for the *next*
        # resume, since this run's prompt content is the freshest truth.
        write_prompt(prompt_path, prompt)
        return response_path.read_text(encoding="utf-8")


@dataclass
class DraftOutcome:
    name: str
    accepted: bool
    attempts: int
    yaml_text: str
    prompt_sha256: str
    lint_errors: list[str]
    lint_warnings: list[str]


def render(template: str, slots: dict[str, str]) -> str:
    """Fill {{slot}} placeholders. Unknown {{...}} tokens (e.g. {{name}} in
    parent-summary examples) are left untouched."""
    for key, value in slots.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def load_existing_ids(content_dir: Path) -> tuple[list[str], list[str]]:
    """Skill and misconception ids defined in the content dir."""
    content = graph_lint.load_content(content_dir, graph_lint.LintResult())
    skill_ids = sorted(s["skill_id"] for s in content["skills"] if "skill_id" in s)
    misc_ids = sorted(m["misconception_id"] for m in content["misconceptions"] if "misconception_id" in m)
    return skill_ids, misc_ids


def staged_ids(accepted: dict[str, str]) -> tuple[list[str], list[str]]:
    """Skill and misconception ids introduced by this run's accepted drafts."""
    skill_ids: list[str] = []
    misc_ids: list[str] = []
    for yaml_text in accepted.values():
        data = yaml.safe_load(yaml_text) or {}
        for node in data.get("skills", []) or []:
            if isinstance(node, dict) and "skill_id" in node:
                skill_ids.append(node["skill_id"])
        for entry in data.get("misconceptions", []) or []:
            if isinstance(entry, dict) and "misconception_id" in entry:
                misc_ids.append(entry["misconception_id"])
    return sorted(skill_ids), sorted(misc_ids)


def format_ids(ids: list[str]) -> str:
    return "\n".join(f"- {i}" for i in ids) if ids else "(none yet)"


def check_response_shape(yaml_text: str) -> str | None:
    """Cheap pre-lint gate: must parse to a mapping with a known content key."""
    try:
        data: Any = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return f"response is not valid YAML: {exc}"
    if not isinstance(data, dict):
        return "response must be a YAML mapping with a top-level content key (skills/misconceptions/mappings)"
    for key, value in data.items():
        if key not in CONTENT_KEYS:
            return f"unknown top-level key {key!r}; expected one of {CONTENT_KEYS}"
        if not isinstance(value, list):
            return f"top-level key {key!r} must hold a list of objects"
    if not any(data.get(k) for k in CONTENT_KEYS):
        return "response contains no content entries"
    return None


def lint_candidate(
    content_dir: Path,
    accepted: dict[str, str],
    candidate_name: str,
    candidate_yaml: str,
    schemas_dir: Path,
) -> graph_lint.LintResult:
    """Lint the candidate merged with the content dir and this run's accepted drafts."""
    with tempfile.TemporaryDirectory(prefix="stage1-lint-") as tmp:
        tmp_dir = Path(tmp)
        for src in sorted(content_dir.glob("*.yaml")) + sorted(content_dir.glob("*.yml")):
            shutil.copy(src, tmp_dir / src.name)
        for name, yaml_text in accepted.items():
            (tmp_dir / f"staged-{name}.yaml").write_text(yaml_text, encoding="utf-8")
        (tmp_dir / f"draft-{candidate_name}.yaml").write_text(candidate_yaml, encoding="utf-8")
        return graph_lint.lint_directory(tmp_dir, schemas_dir)


def run_draft(
    step: DraftStep,
    slots: dict[str, str],
    backend: Backend,
    content_dir: Path,
    accepted: dict[str, str],
    schemas_dir: Path,
    max_retries: int,
) -> DraftOutcome:
    """Draft → lint → bounce loop for one step. First try plus max_retries re-prompts."""
    template = (PROMPTS_DIR / step.template_file).read_text(encoding="utf-8")
    feedback = ""
    last_errors: list[str] = []
    last_response = ""
    last_hash = ""

    for attempt in range(1, max_retries + 2):
        prompt = render(template, {**slots, "lint_feedback": feedback})
        last_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        last_response = backend.complete(prompt)

        shape_error = check_response_shape(last_response)
        if shape_error is None:
            result = lint_candidate(content_dir, accepted, step.name, last_response, schemas_dir)
            if result.ok:
                return DraftOutcome(
                    name=step.name,
                    accepted=True,
                    attempts=attempt,
                    yaml_text=last_response,
                    prompt_sha256=last_hash,
                    lint_errors=[],
                    lint_warnings=result.warnings,
                )
            last_errors = result.errors
        else:
            last_errors = [shape_error]

        feedback = (
            "\n## Previous draft failed these checks\n\n"
            + "\n".join(f"- {e}" for e in last_errors)
            + "\n\nReturn a corrected full draft fixing every error above. "
            "Output ONLY YAML, no prose.\n"
        )

    return DraftOutcome(
        name=step.name,
        accepted=False,
        attempts=max_retries + 1,
        yaml_text=last_response,
        prompt_sha256=last_hash,
        lint_errors=last_errors,
        lint_warnings=[],
    )


def write_accepted(staging_dir: Path, outcome: DraftOutcome, meta: dict[str, str]) -> None:
    accepted_dir = staging_dir / "accepted"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    (accepted_dir / f"{outcome.name}.yaml").write_text(outcome.yaml_text, encoding="utf-8")
    manifest = {
        **meta,
        "draft": outcome.name,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "attempts": outcome.attempts,
        "retries_used": outcome.attempts - 1,
        "prompt_sha256": outcome.prompt_sha256,
        "lint": {"errors": outcome.lint_errors, "warnings": outcome.lint_warnings},
    }
    (accepted_dir / f"{outcome.name}.manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def write_rejected(staging_dir: Path, outcome: DraftOutcome, meta: dict[str, str]) -> None:
    rejected_dir = staging_dir / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    (rejected_dir / f"{outcome.name}.yaml").write_text(outcome.yaml_text, encoding="utf-8")
    report = {
        **meta,
        "draft": outcome.name,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "attempts": outcome.attempts,
        "prompt_sha256": outcome.prompt_sha256,
        "lint_errors": outcome.lint_errors,
    }
    (rejected_dir / f"{outcome.name}.errors.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )


def planned_steps(args: argparse.Namespace) -> list[DraftStep]:
    steps = [DRAFT_STEPS[0], DRAFT_STEPS[1]]
    if args.country or args.curriculum:
        if not (args.country and args.curriculum):
            raise SystemExit("--country and --curriculum must be given together")
        steps.append(DRAFT_STEPS[2])
    return steps


def build_slots(args: argparse.Namespace, step: DraftStep, syllabus_text: str,
                skill_ids: list[str], misc_ids: list[str]) -> dict[str, str]:
    schema = json.loads((SCHEMAS_DIR / step.schema_file).read_text(encoding="utf-8"))
    return {
        "strand": args.strand,
        "syllabus_text": syllabus_text,
        "existing_skill_ids": format_ids(skill_ids),
        "existing_misconception_ids": format_ids(misc_ids),
        "schema_json": json.dumps(schema, indent=2),
        "country": args.country or "",
        "curriculum": args.curriculum or "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 drafting harness (see module docstring).")
    parser.add_argument("--strand", required=True, help="strand being drafted, e.g. NUMBER.FRACTIONS")
    parser.add_argument("--syllabus", required=True, type=Path, help="syllabus text file")
    parser.add_argument("--content-dir", required=True, type=Path, help="existing content directory (read-only)")
    parser.add_argument("--staging-dir", required=True, type=Path, help="where drafts, manifests, and rejections go")
    parser.add_argument("--country", help="ISO country code; with --curriculum, also draft a mapping")
    parser.add_argument("--curriculum", help="curriculum id, e.g. NCERT")
    parser.add_argument("--max-retries", type=int, default=3, help="re-prompts per draft after the first try")
    parser.add_argument("--backend", choices=["cmd", "file"], default="file")
    parser.add_argument("--print-prompts", action="store_true",
                        help="write first-round prompts to the staging dir and exit")
    parser.add_argument("--schemas", type=Path, default=SCHEMAS_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not args.content_dir.is_dir():
        print(f"error: content dir {args.content_dir} does not exist", file=sys.stderr)
        return 2
    if not args.syllabus.is_file():
        print(f"error: syllabus file {args.syllabus} does not exist", file=sys.stderr)
        return 2

    steps = planned_steps(args)
    syllabus_text = args.syllabus.read_text(encoding="utf-8")
    args.staging_dir.mkdir(parents=True, exist_ok=True)
    base_skill_ids, base_misc_ids = load_existing_ids(args.content_dir)

    if args.print_prompts:
        for i, step in enumerate(steps, start=1):
            slots = build_slots(args, step, syllabus_text, base_skill_ids, base_misc_ids)
            prompt = render((PROMPTS_DIR / step.template_file).read_text(encoding="utf-8"),
                            {**slots, "lint_feedback": ""})
            path = args.staging_dir / f"prompt-{i}.md"
            write_prompt(path, prompt)
            print(f"wrote {path}")
        print("prompts written; provide response-<n>.yaml per prompt and re-run without --print-prompts")
        return 0

    backend: Backend
    if args.backend == "cmd":
        backend = CmdBackend()
    else:
        backend = FileBackend(args.staging_dir)

    meta = {
        "strand": args.strand,
        "syllabus": str(args.syllabus),
        "backend": args.backend,
        "max_retries": args.max_retries,
    }
    accepted: dict[str, str] = {}
    for step in steps:
        staged_skill_ids, staged_misc_ids = staged_ids(accepted)
        slots = build_slots(
            args, step, syllabus_text,
            base_skill_ids + staged_skill_ids,
            base_misc_ids + staged_misc_ids,
        )
        outcome = run_draft(step, slots, backend, args.content_dir, accepted,
                            args.schemas, args.max_retries)
        if outcome.accepted:
            write_accepted(args.staging_dir, outcome, meta)
            accepted[step.name] = outcome.yaml_text
            print(f"[{step.name}] accepted after {outcome.attempts} attempt(s) "
                  f"-> {args.staging_dir}/accepted/{step.name}.yaml")
            for warning in outcome.lint_warnings:
                print(f"[{step.name}] WARNING: {warning}")
        else:
            write_rejected(args.staging_dir, outcome, meta)
            print(f"[{step.name}] REJECTED after {outcome.attempts} attempt(s) "
                  f"-> {args.staging_dir}/rejected/{step.name}.yaml", file=sys.stderr)
            for error in outcome.lint_errors:
                print(f"[{step.name}] ERROR: {error}", file=sys.stderr)
            print("stopping; fix or re-draft before running later steps", file=sys.stderr)
            return 1

    print("all drafts accepted; a human reviews and promotes "
          f"{args.staging_dir}/accepted/*.yaml into {args.content_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
