"""Score deterministic closed-question resolution on the 150-case suite."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from applyd.apply.deterministic_questions import resolve_question
try:
    from observed_application_question_eval_fixtures import fixtures
except ModuleNotFoundError:
    from scripts.observed_application_question_eval_fixtures import fixtures


def run() -> Path:
    cases = fixtures()
    results = []
    for case in cases:
        state = case["state"]
        answer = resolve_question(
            state["question"], state["profile"], case["options"],
            company=case.get("observed_company", ""),
        )
        results.append({
            "id": case["id"], "group": case["group"],
            "severity": case.get("severity", "normal"),
            "expected": case["expected"], "choice": answer.choice,
            "correct": answer.choice == case["expected"],
            "predicate": answer.predicate, "reason": answer.reason,
            "automatic": bool(answer.choice and answer.choice != "review"),
        })
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path("data/benchmarks/deterministic-questions-" + stamp + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "cases": len(results),
        "correct": sum(row["correct"] for row in results),
        "correct_automatic": sum(row["correct"] and row["automatic"] for row in results),
        "wrong_automatic": sum(not row["correct"] and row["automatic"] for row in results),
        "appropriate_review": sum(row["correct"] and not row["automatic"] for row in results),
        "avoidable_review": sum(not row["correct"] and not row["automatic"] for row in results),
        "by_group": {},
        "predicate_counts": dict(Counter(row["predicate"] for row in results)),
    }
    for group in dict.fromkeys(row["group"] for row in results):
        rows = [row for row in results if row["group"] == group]
        summary["by_group"][group] = {
            "cases": len(rows), "correct": sum(row["correct"] for row in rows),
            "automatic": sum(row["automatic"] for row in rows),
            "wrong_automatic": sum(not row["correct"] and row["automatic"] for row in rows),
        }
    path.write_text(json.dumps({"summary": summary, "results": results}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Report: {path}")
    return path


if __name__ == "__main__":
    run()
