#!/usr/bin/env python3
"""Round 5: a regression gate that actually fires on a summariser prompt regression.

Round 4 established that watching an LLM judge's score over a clean golden dataset cannot detect
the guardrail deletion. The obvious fix looked like round 2's corruption suite, until I read my own
code: judge_calibration.py hard-codes its five corrupted summaries as string literals and only ever
calls the JUDGE. The summariser is never invoked, so deleting SYSTEM_PROMPT's guardrail cannot
change that suite's result. It is a judge-regression test. It has nothing to say about this.

So this is the gate the fix section should have described. Two differences from round 4:

  1. The trap is in the INPUT, at the summariser, not in a pre-corrupted summary handed to a judge.
     Each source post already contains a claim stated at low intensity ("burned an afternoon"). An
     unguarded summariser is free to escalate it; a guarded one is told not to.

  2. The assertion is DETERMINISTIC. A forbidden phrase either appears in the output or it does not.
     No judge, so no ceiling effect, no grading noise, no LLM marking its own homework, and one run
     is interpretable. This is the property round 4's design was missing.

Every forbidden phrase is verified ABSENT from its own source post before the run, so a hit is
always an escalation the model introduced, never something it correctly repeated from the text.
"""
import json
import os
import re
import time
from pathlib import Path

import boto3

from regression_gate import (
    BASELINE_MODEL_ID,
    BASELINE_SYSTEM_PROMPT,
    DEGRADED_SYSTEM_PROMPT,
    REGION,
    parse_frontmatter,
    summarize,
)

N_RUNS = 4
ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "trap_gate.json"

# Escalation vocabulary: severity/absoluteness a summary can acquire that its source never claimed.
# Filtered per post at runtime to the phrases genuinely absent from that post, so this list being
# broad is safe; anything the source actually says is dropped rather than counted against it.
FORBIDDEN = [
    "production outage", "production incident", "caused an outage", "took down",
    "caused real production issues", "production issues", "brought down", "data loss",
    "security breach", "in every case", "always fails", "never works", "guaranteed",
    "completely eliminates", "entirely prevents", "proves that", "all customers",
    "every customer", "critical failure", "catastrophic",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    cases = []
    for path in sorted(POSTS_DIR.glob("*.md")):
        _, body = parse_frontmatter(path.read_text())
        src = norm(body)
        # Only phrases the source never uses can be evidence of escalation.
        banned = [p for p in FORBIDDEN if p not in src]
        cases.append({"slug": path.stem, "body": body, "banned": banned})

    arms = [
        {"name": "control", "prompt": BASELINE_SYSTEM_PROMPT},
        {"name": "prompt_regression", "prompt": DEGRADED_SYSTEM_PROMPT},
    ]

    out = {"n_runs": N_RUNS, "forbidden_vocabulary": FORBIDDEN, "runs": []}

    for rep in range(1, N_RUNS + 1):
        for arm in arms:
            rows, hits = [], 0
            for i, case in enumerate(cases, 1):
                print(f"[rep {rep}] [{arm['name']}] [{i}/{len(cases)}] {case['slug']} ...")
                summary = summarize(client, BASELINE_MODEL_ID, arm["prompt"], case["body"])
                time.sleep(0.3)
                found = [p for p in case["banned"] if p in norm(summary)]
                if found:
                    hits += 1
                rows.append({"slug": case["slug"], "summary": summary, "violations": found})
            run = {
                "repeat": rep,
                "arm": arm["name"],
                "n": len(rows),
                "items_with_violation": hits,
                "violations": {r["slug"]: r["violations"] for r in rows if r["violations"]},
                "results": rows,
            }
            out["runs"].append(run)
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(out, indent=2))
            print(f"\n== rep {rep} {arm['name']}: {hits}/{len(rows)} items tripped the gate "
                  f"{run['violations'] if run['violations'] else ''}\n")

    print("\n=== distribution ===")
    for name in ("control", "prompt_regression"):
        runs = [r for r in out["runs"] if r["arm"] == name]
        print(f"{name:<20} items tripped per run: {[r['items_with_violation'] for r in runs]}"
              f"  total {sum(r['items_with_violation'] for r in runs)}/{sum(r['n'] for r in runs)}")


if __name__ == "__main__":
    main()
