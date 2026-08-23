#!/usr/bin/env python3
"""Is the control-vs-prompt_regression difference a signal, or one run's luck?

regression_gate.py found the prompt regression moved the gate by exactly one item out of sixteen,
against a control that moved zero. One item, on one run, is exactly the kind of result that looks
like a finding and isn't: the round-2 "contagion" result in this repo's own history did not survive
a controlled rerun.

So repeat both arms N times each and look at the distribution of FAIL counts. If control ever fails
an item, or prompt_regression sometimes fails none, the one-item difference is noise and a gate set
to trip on it would both miss real regressions and fire on clean builds.
"""
import json
import os
import time
from pathlib import Path

import boto3

from regression_gate import (
    BASELINE_MODEL_ID,
    BASELINE_SYSTEM_PROMPT,
    DEGRADED_SYSTEM_PROMPT,
    REGION,
    judge,
    parse_frontmatter,
    summarize,
)

N_REPEATS = 3
ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "regression_repeats.json"

ARMS = [
    {"name": "control", "prompt": BASELINE_SYSTEM_PROMPT},
    {"name": "prompt_regression", "prompt": DEGRADED_SYSTEM_PROMPT},
]


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    sources = []
    for path in sorted(POSTS_DIR.glob("*.md")):
        fm, body = parse_frontmatter(path.read_text())
        sources.append({"slug": path.stem, "body": body})

    out = {"n_repeats": N_REPEATS, "runs": []}

    for rep in range(1, N_REPEATS + 1):
        for arm in ARMS:
            rows = []
            for i, src in enumerate(sources, 1):
                print(f"[rep {rep}] [{arm['name']}] [{i}/{len(sources)}] {src['slug']} ...")
                summary = summarize(client, BASELINE_MODEL_ID, arm["prompt"], src["body"])
                time.sleep(0.3)
                v = judge(client, src["body"], summary)
                time.sleep(0.3)
                rows.append({"slug": src["slug"], "summary": summary, **v})

            fails = [r["slug"] for r in rows if r.get("verdict") == "FAIL"]
            faith = [r["faithfulness"] for r in rows if isinstance(r.get("faithfulness"), int)]
            run = {
                "repeat": rep,
                "arm": arm["name"],
                "n": len(rows),
                "fail_count": len(fails),
                "failed_slugs": fails,
                "mean_faithfulness": sum(faith) / len(faith),
                "results": rows,
            }
            out["runs"].append(run)
            OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUT_PATH.write_text(json.dumps(out, indent=2))
            print(
                f"\n== rep {rep} {arm['name']}: {len(rows)-len(fails)}/{len(rows)} PASS · "
                f"mean faithfulness {run['mean_faithfulness']:.2f} · fails={fails}\n"
            )

    print("\n=== distribution ===")
    for name in ("control", "prompt_regression"):
        runs = [r for r in out["runs"] if r["arm"] == name]
        counts = [r["fail_count"] for r in runs]
        means = [r["mean_faithfulness"] for r in runs]
        print(f"{name:<20} fail counts {counts}  mean faith {[f'{m:.2f}' for m in means]}")


if __name__ == "__main__":
    main()
