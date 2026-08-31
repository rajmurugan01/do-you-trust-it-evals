#!/usr/bin/env python3
"""LLM-as-judge: score each generated summary against its source post for faithfulness."""
import json
import os
import re
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
JUDGE_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
SUMMARIES_PATH = ROOT / "data" / "results" / "summaries.json"
OUT_PATH = ROOT / "data" / "results" / "judged.json"

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


def load_body(slug: str) -> str:
    raw = (POSTS_DIR / f"{slug}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    return m.group(2) if m else raw


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    summaries = json.loads(SUMMARIES_PATH.read_text())
    results = []

    for i, item in enumerate(summaries, 1):
        slug = item["slug"]
        source = load_body(slug)
        summary = item["summary"]

        print(f"[{i}/{len(summaries)}] judging {slug} ...")
        user_msg = f"SOURCE:\n{source[:14000]}\n\nSUMMARY:\n{summary}"
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

        results.append(
            {
                "slug": slug,
                "title": item["title"],
                "summary": summary,
                "judge_model": JUDGE_MODEL_ID,
                **judged,
            }
        )
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2))

    n = len(results)
    passed = sum(1 for r in results if r.get("verdict") == "PASS")
    failed = [r for r in results if r.get("verdict") == "FAIL"]
    print(f"\n{passed}/{n} PASS")
    for r in failed:
        print(f"  FAIL {r['slug']}: {r.get('hallucinations')}")
    print(f"\nWrote judged results -> {OUT_PATH}")


if __name__ == "__main__":
    main()
