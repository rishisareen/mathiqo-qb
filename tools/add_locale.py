#!/usr/bin/env python3
"""Derive a regional locale's authored text from an existing one.

Adds a sibling ``<target>`` entry next to every ``<source>`` entry of a
language-keyed field (``stem_i18n``, ``hints_i18n``, ``solution_steps_i18n``,
``label_i18n``, ``child_feedback_i18n``, ``parent_summary_i18n``, ...) in
content YAML files, applying the term substitutions declared by the target
locale pack's ``text_rules`` list. Text is edited in place (lines inserted,
nothing rewritten) so comments and formatting survive.

The result is a *draft*: the substitutions are mechanical (currency, a few
cultural references) and every changed line must still go through teacher
review before the target locale is served to children.

Usage:
    python3 tools/add_locale.py --source en-IN --target en-CA \
        --rules content/locales/en-CA.yaml content/templates content/fractions
    python3 tools/add_locale.py ... --check    # exit 1 if anything is missing
    python3 tools/add_locale.py ... --report   # list lines the rules changed

Idempotent: an existing ``<target>`` sibling is left untouched.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


def load_rules(path: Path) -> list[tuple[re.Pattern[str], str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = []
    for entry in data.get("text_rules", []):
        rules.append((re.compile(entry["pattern"]), entry["replace"]))
    return rules


def apply_rules(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def process(text: str, source: str, target: str, rules) -> tuple[str, int, list[tuple[str, str]]]:
    lines = text.split("\n")
    key_re = re.compile(rf"^(\s*){re.escape(source)}:(.*)$")
    target_re = re.compile(rf"^\s*{re.escape(target)}:")
    out: list[str] = []
    added = 0
    changes: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = key_re.match(line)
        if not match:
            out.append(line)
            i += 1
            continue
        indent = len(match.group(1))
        j = i + 1
        # YAML allows a block sequence's "- " items at the same indent as
        # their key, so those belong to this entry too.
        while j < len(lines) and (
            lines[j].strip() == ""
            or _indent(lines[j]) > indent
            or (_indent(lines[j]) == indent and lines[j].lstrip().startswith("- "))
        ):
            j += 1
        # Trailing blank lines belong to whatever follows, not to this block.
        end = j
        while end > i + 1 and lines[end - 1].strip() == "":
            end -= 1
        block = lines[i:end]
        out.extend(block)
        sibling_exists = end < len(lines) and _indent(lines[end]) == indent and bool(
            target_re.match(lines[end])
        )
        if not sibling_exists:
            new_block = [apply_rules(b, rules) for b in block]
            new_block[0] = re.sub(rf"^(\s*){re.escape(source)}:", rf"\1{target}:", new_block[0], count=1)
            # The substitution rules must not touch the key itself.
            for old, new in zip(block[1:], new_block[1:]):
                if old != new:
                    changes.append((old.strip(), new.strip()))
            first_old = block[0].split(":", 1)[1]
            first_new = new_block[0].split(":", 1)[1]
            if first_old != first_new:
                changes.append((first_old.strip(), first_new.strip()))
            out.extend(new_block)
            added += 1
        i = end
    return "\n".join(out), added, changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--rules", required=True, type=Path, help="locale pack YAML holding text_rules")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if entries are missing")
    parser.add_argument("--report", action="store_true", help="print every line the rules changed")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)

    rules = load_rules(args.rules)
    files: list[Path] = []
    for p in args.paths:
        files.extend(sorted(p.rglob("*.yaml")) if p.is_dir() else [p])

    total_added = 0
    for path in files:
        original = path.read_text(encoding="utf-8")
        updated, added, changes = process(original, args.source, args.target, rules)
        if added == 0:
            continue
        total_added += added
        if args.report:
            for old, new in changes:
                print(f"{path.name}\n  - {old}\n  + {new}")
        if not args.check:
            path.write_text(updated, encoding="utf-8")
            yaml.safe_load(updated)  # fail loudly if an edit broke the file
    print(f"{'missing' if args.check else 'added'} {total_added} {args.target} entries", file=sys.stderr)
    return 1 if (args.check and total_added) else 0


if __name__ == "__main__":
    raise SystemExit(main())
