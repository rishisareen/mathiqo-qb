#!/usr/bin/env python3
"""Fake LLM for stage1_draft tests, driven via the cmd backend.

Usage: fake_llm.py <scenario-dir>

Serves canned YAML responses from <scenario-dir>/NN.yaml in call order
(01.yaml, 02.yaml, ...), tracking position in <scenario-dir>/.counter.
Every prompt received on stdin is saved to <scenario-dir>/received-prompt-N.md
so tests can assert on what the driver sent (e.g. lint-error feedback).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    scenario = Path(sys.argv[1])
    prompt = sys.stdin.read()

    counter_file = scenario / ".counter"
    n = int(counter_file.read_text(encoding="utf-8")) + 1 if counter_file.exists() else 1
    counter_file.write_text(str(n), encoding="utf-8")

    (scenario / f"received-prompt-{n}.md").write_text(prompt, encoding="utf-8")

    response = scenario / f"{n:02d}.yaml"
    if not response.exists():
        print(f"fake_llm: no canned response {response}", file=sys.stderr)
        return 1
    sys.stdout.write(response.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
