#!/usr/bin/env python3
"""Run the same summary through the judge N times, unchanged, to check whether the verdict is
actually stable at temperature 0. Covers both an easy input (the clean round-1 baseline) and a
harder one (a round-2 corrupted case), since a clean input repeating cleanly is the least
informative case to test stability on."""
import json
import os
import re
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
JUDGE_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
N_RUNS = 4

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
SUMMARIES_PATH = ROOT / "data" / "results" / "summaries.json"
CALIBRATION_PATH = ROOT / "data" / "results" / "calibration.json"
OUT_PATH = ROOT / "data" / "results" / "self_consistency.json"

JUDGE_SYSTEM_PROMPT = """You are a strict fact-checking judge. You will be given SOURCE (a blog \
post) and SUMMARY (an AI-generated 2-3 sentence summary of it). Your job is to check whether the \
summary is faithful to the source, not whether it's well-written.

Score on:
- faithfulness (1-5): 5 = every claim in the summary is directly supported by the source. \
1 = the summary contains claims the source does not make.
- completeness (1-5): 5 = captures the post's main point and any key concrete detail (a number, \
a named technique, an outcome). 1 = misses the point entirely.
- hallucinations: a list of specific phrases/claims/numbers in the summary that are NOT present or \
NOT supported in the source. Empty list if none. Be strict: a paraphrase that changes the meaning \
counts, and so does a claim that quietly generalizes something conditional in the source into \
something absolute.
- verdict: "PASS" if faithfulness >= 4 AND hallucinations is empty, otherwise "FAIL".

Respond with ONLY a JSON object, no prose, no markdown fences, in this exact shape:
{"faithfulness": <int>, "completeness": <int>, "hallucinations": [<string>, ...], "verdict": \
"PASS"|"FAIL", "reasoning": "<one sentence>"}
"""

EASY_SLUG = "three-things-bedrock-workload"
HARD_SLUG = "agents-need-a-harness"  # modality-broadening corrupted case: borderline, not obvious


def load_body(slug: str) -> str:
    raw = (POSTS_DIR / f"{slug}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    return m.group(2) if m else raw


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def run_n_times(client, slug: str, summary: str, label: str) -> list:
    source = load_body(slug)
    user_msg = f"SOURCE:\n{source[:14000]}\n\nSUMMARY:\n{summary}"
    results = []
    for i in range(1, N_RUNS + 1):
        print(f"[{label} {i}/{N_RUNS}] re-judging identical input ...")
        resp = client.converse(
            modelId=JUDGE_MODEL_ID,
            system=[{"text": JUDGE_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.0},
        )
        raw_text = resp["output"]["message"]["content"][0]["text"]
        try:
            judged = extract_json(raw_text)
        except json.JSONDecodeError:
            judged = {"parse_error": True, "raw": raw_text}
        results.append({"run": i, **judged})
        time.sleep(0.3)
    return results


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    summaries = json.loads(SUMMARIES_PATH.read_text())
    easy_summary = [s for s in summaries if s["slug"] == EASY_SLUG][0]["summary"]
    easy_runs = run_n_times(client, EASY_SLUG, easy_summary, "easy")

    calibration = json.loads(CALIBRATION_PATH.read_text())
    hard_case = [c for c in calibration if c["slug"] == HARD_SLUG][0]
    hard_summary = hard_case["corrupted_summary"]
    hard_runs = run_n_times(client, HARD_SLUG, hard_summary, "hard")

    out = {
        "easy": {"slug": EASY_SLUG, "note": "round-1 clean baseline", "summary": easy_summary, "runs": easy_runs},
        "hard": {"slug": HARD_SLUG, "note": "round-2 corrupted case (modality broadening)", "summary": hard_summary, "runs": hard_runs},
    }
    OUT_PATH.write_text(json.dumps(out, indent=2))

    for label, block in out.items():
        verdicts = [r.get("verdict") for r in block["runs"]]
        faiths = [r.get("faithfulness") for r in block["runs"]]
        comps = [r.get("completeness") for r in block["runs"]]
        print(f"\n[{label}] verdicts: {verdicts}")
        print(f"[{label}] faithfulness: {faiths}")
        print(f"[{label}] completeness: {comps}")
        print(f"[{label}] stable: {len(set(verdicts)) == 1 and len(set(faiths)) == 1 and len(set(comps)) == 1}")


if __name__ == "__main__":
    main()
