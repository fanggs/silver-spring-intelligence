#!/usr/bin/env python3
"""Summarise questions CivicLens could not fully answer, grouped by reason.

Reads the local log (logs/question-gaps.jsonl) by default. It also reads a
saved copy of the Render logs: any line containing "CIVICLENS_GAP {...}".

    python -m evals.gaps
    python -m evals.gaps render-logs.txt

The reason says where to look:
  no_data_for_question  No installed table covers it. Candidate dataset to add.
  no_rows               A table exists but nothing matched. Check coverage or filters.
  query_error           The model wrote SQL that failed. Check the schema description.
  unsafe_query          The guard refused the SQL. Usually a prompt problem.
  trimmed_answer        Some sentences were dropped for unverified figures.
  no_verified_answer    No sentence could be verified; the page showed the fallback.
  dropped_next_step     The next step stated an unverified figure and was hidden.
  model_error           The language model could not be reached.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "logs" / "question-gaps.jsonl"
ORDER = ["no_data_for_question", "no_rows", "query_error", "unsafe_query",
         "no_verified_answer", "trimmed_answer", "dropped_next_step", "model_error"]


def read(path):
    records = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        start = line.find("{") if "CIVICLENS_GAP" in line or line.startswith("{") else -1
        if start < 0:
            continue
        try:
            records.append(json.loads(line[start:]))
        except ValueError:
            continue
    return records


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not path.exists():
        print(f"No gap log at {path}. Nothing has been logged yet.")
        return 0
    groups = defaultdict(lambda: defaultdict(list))
    for record in read(path):
        groups[record.get("reason", "unknown")][record.get("question", "").strip()].append(record)
    if not groups:
        print("No gaps logged.")
        return 0
    for reason in ORDER + sorted(set(groups) - set(ORDER)):
        if reason not in groups:
            continue
        questions = groups[reason]
        print(f"\n{reason}  ({sum(len(v) for v in questions.values())} times, {len(questions)} questions)")
        for question, hits in sorted(questions.items(), key=lambda item: -len(item[1])):
            print(f"  {len(hits)}x  {question}")
            detail = hits[-1].get("detail")
            if isinstance(detail, dict) and detail.get("dropped_sentences"):
                for sentence in detail["dropped_sentences"][:3]:
                    print(f"        dropped: {sentence}")
            elif detail:
                print(f"        detail: {str(detail)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
