#!/usr/bin/env python3
"""Round 7: move the INPUT, not the instrument.

Where rounds 4-6 leave it: the guardrail deletion is undetectable by an LLM judge over the golden
dataset (round 4, p = 1.000), undetectable by a judge-free deterministic assertion (round 5,
p = 1.000), and undetectable by six label-free signals computed on those same outputs (round 6,
nothing below p = 0.17 clustered). Three different instruments, one answer.

At some point the instrument stops being the suspect. Every one of those rounds graded the same 16
posts, and those 16 posts are a task this summariser finds easy: dense, tightly-written technical
sources that already contain every number a 2-3 sentence summary would want. An anti-hallucination
guardrail is worth nothing on an input that offers no temptation to hallucinate. Delete it and the
output barely moves, because the guardrail was not doing any work in the first place.

That is not a claim about evals in general. It is a claim about THIS golden dataset, and it is
testable: hold the summariser, the prompts, the judge, the rubric and the posts fixed, and move only
how much source the model gets.

  full   12000 chars   rounds 4-6, already on disk. Every headline number is present in the source.
  2000   2000 chars    intro plus a section. Some claims the summary wants are now missing.
  600    600 chars     opening paragraph only. The model is asked for a summary of a post it has
                       mostly not been shown, and has to either hedge or invent.

Truncation is a PROXY for "traffic you did not anticipate", not the thing itself, and it is worth
being exact about what it does and does not simulate. It holds domain, topic, style and vocabulary
constant and moves only the density of grounding material, which is the cleanest single variable
available without leaving the corpus. It does not simulate a genuinely novel domain, an adversarial
user, or a shifted language register. A production drift can be all of those at once.

Same 4 repeats per arm as round 4, so n is comparable and no new sample-size argument is smuggled in.
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
    JUDGE_MODEL_ID,
    REGION,
    judge,
    parse_frontmatter,
    summarize,
)

N_REPEATS = 4
TRUNCATIONS = [2000, 600]
ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "ood_inputs.json"

ARMS = [
    {"name": "control", "prompt": BASELINE_SYSTEM_PROMPT},
    {"name": "prompt_regression", "prompt": DEGRADED_SYSTEM_PROMPT},
]


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    sources = {}
    for path in sorted(POSTS_DIR.glob("*.md")):
        raw = path.read_text()
        fm, body = parse_frontmatter(raw)
        sources[path.stem] = {"title": fm.get("title", path.stem), "body": body}

    out = {
        "summariser_model": BASELINE_MODEL_ID,
        "judge_model": JUDGE_MODEL_ID,
        "n_repeats": N_REPEATS,
        "truncations": TRUNCATIONS,
        "runs": [],
    }
    if OUT_PATH.exists():  # resume: 512 calls is long enough that a mid-run failure must not restart it
        out = json.loads(OUT_PATH.read_text())
    done = {(r["truncation"], r["arm"], r["repeat"]) for r in out["runs"]}

    total = len(TRUNCATIONS) * len(ARMS) * N_REPEATS
    for trunc in TRUNCATIONS:
        for arm in ARMS:
            for rep in range(N_REPEATS):
                key = (trunc, arm["name"], rep)
                if key in done:
                    print(f"skip (done) {key}")
                    continue
                rows = []
                for i, (slug, src) in enumerate(sorted(sources.items()), 1):
                    body = src["body"][:trunc]
                    summary = summarize(client, BASELINE_MODEL_ID, arm["prompt"], body)
                    time.sleep(0.25)
                    # The judge sees the SAME truncated source the summariser saw. Judging against
                    # the full post would score the model for omitting text it was never given.
                    verdict = judge(client, body, summary)
                    time.sleep(0.25)
                    rows.append({"slug": slug, "summary": summary, **verdict})
                    print(f"  [{trunc}/{arm['name']}/r{rep}] {i}/16 {slug}", flush=True)

                fails = sum(1 for r in rows if r.get("verdict") == "FAIL")
                faith = [r["faithfulness"] for r in rows if isinstance(r.get("faithfulness"), int)]
                out["runs"].append(
                    {
                        "truncation": trunc,
                        "arm": arm["name"],
                        "repeat": rep,
                        "n": len(rows),
                        "fail_count": fails,
                        "mean_faithfulness": sum(faith) / len(faith) if faith else None,
                        "results": rows,
                    }
                )
                OUT_PATH.write_text(json.dumps(out, indent=2))
                print(
                    f"== trunc={trunc} {arm['name']} r{rep}: {fails}/16 FAIL · "
                    f"mean faith {sum(faith)/len(faith):.2f} · {len(out['runs'])}/{total} runs\n",
                    flush=True,
                )

    print(f"Wrote -> {OUT_PATH}")


if __name__ == "__main__":
    main()
