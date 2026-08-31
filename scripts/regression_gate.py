#!/usr/bin/env python3
"""Regression gate check: if summary quality silently drops, does the eval gate actually catch it?

Round 1-3 (judge.py, judge_calibration.py, self_consistency_check.py) established that the judge
discriminates a deliberately broken input from a clean one and holds its grade on a re-run. That is
a different job from the one a regression gate does in CI: catch a SMALL drop across a whole
dataset, when nobody injected an obvious error and nobody knows what changed.

Three arms, each a single-variable change from the round-1 baseline, all graded by the identical
judge (same model, same rubric, temperature 0):

  control            same summariser model, same system prompt. Nothing changed on purpose. This is
                     the noise floor: the summariser runs at temperature 0.3, so the baseline is not
                     deterministic, and any gate movement here is movement a real regression has to
                     beat to be detectable at all.
  prompt_regression  same model, but the anti-hallucination guardrail sentences are deleted from the
                     system prompt. Simulates the classic silent regression: someone trims a prompt.
  model_regression   same prompt, but a much weaker/cheaper summariser model. Simulates the
                     "swap to a cheaper model to cut cost" regression. NOTE: this arm crosses model
                     families (Nova vs Claude), so unlike the other two it is NOT a clean single
                     variable — tokenizer, training and instruction-following all move at once. It
                     is here as a positive control (does the gate detect ANY drop?), not as an
                     attributable one. The intended within-family swap to Claude 3 Haiku could not
                     be run: Bedrock now refuses it as Legacy for accounts that have not called it
                     in 30 days.

Without the control arm a difference between baseline and a regression arm is unattributable, which
is the same single-variable discipline round 2 used.
"""
import json
import os
import re
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
BASELINE_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
WEAKER_MODEL_ID = "us.amazon.nova-micro-v1:0"
JUDGE_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "regression_gate.json"

# Byte-identical to generate_summaries.py's SYSTEM_PROMPT. The baseline arm must not differ from
# round 1 by so much as a word, or the control stops being a control.
BASELINE_SYSTEM_PROMPT = (
    "You summarize technical blog posts in 2-3 sentences for a reader deciding whether to "
    "click through. Base the summary ONLY on the text provided. Do not add claims, numbers, "
    "product names, or examples that are not explicitly present in the source text. If the "
    "post does not state a specific number or outcome, do not invent one."
)

# The single variable for the prompt arm: the three guardrail sentences are gone, the task
# sentence is untouched. This is what a prompt looks like after someone "tidied it up".
DEGRADED_SYSTEM_PROMPT = (
    "You summarize technical blog posts in 2-3 sentences for a reader deciding whether to "
    "click through."
)

# Byte-identical to judge.py's JUDGE_SYSTEM_PROMPT. The judge is the instrument; it does not vary.
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

ARMS = [
    {"name": "control", "model": BASELINE_MODEL_ID, "prompt": BASELINE_SYSTEM_PROMPT},
    {"name": "prompt_regression", "model": BASELINE_MODEL_ID, "prompt": DEGRADED_SYSTEM_PROMPT},
    {"name": "model_regression", "model": WEAKER_MODEL_ID, "prompt": BASELINE_SYSTEM_PROMPT},
]


def parse_frontmatter(raw: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw
    fm_text, body = m.group(1), m.group(2)
    title_m = re.search(r'^title:\s*"?(.*?)"?\s*$', fm_text, re.MULTILINE)
    return {"title": title_m.group(1) if title_m else "(untitled)"}, body


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def summarize(client, model_id: str, system_prompt: str, body: str) -> str:
    resp = client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": body[:12000]}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0.3},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def judge(client, source: str, summary: str) -> dict:
    user_msg = f"SOURCE:\n{source[:14000]}\n\nSUMMARY:\n{summary}"
    resp = client.converse(
        modelId=JUDGE_MODEL_ID,
        system=[{"text": JUDGE_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0.0},
    )
    raw_text = resp["output"]["message"]["content"][0]["text"]
    try:
        return extract_json(raw_text)
    except json.JSONDecodeError:
        return {"parse_error": True, "raw": raw_text}


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    files = sorted(POSTS_DIR.glob("*.md"))
    sources = []
    for path in files:
        fm, body = parse_frontmatter(path.read_text())
        sources.append({"slug": path.stem, "title": fm.get("title", path.stem), "body": body})

    out = {"judge_model": JUDGE_MODEL_ID, "arms": {}}

    for arm in ARMS:
        rows = []
        for i, src in enumerate(sources, 1):
            print(f"[{arm['name']}] [{i}/{len(sources)}] {src['slug']} ...")
            summary = summarize(client, arm["model"], arm["prompt"], src["body"])
            time.sleep(0.3)
            verdict = judge(client, src["body"], summary)
            time.sleep(0.3)
            rows.append(
                {
                    "slug": src["slug"],
                    "title": src["title"],
                    "summary": summary,
                    "summariser_model": arm["model"],
                    **verdict,
                }
            )
        out["arms"][arm["name"]] = {
            "summariser_model": arm["model"],
            "system_prompt": arm["prompt"],
            "results": rows,
        }
        # Write after each arm: the first run of this script lost two completed arms when the
        # third died on a model-access error before the single save at the end.
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2))

        n = len(rows)
        passed = sum(1 for r in rows if r.get("verdict") == "PASS")
        faith = [r["faithfulness"] for r in rows if isinstance(r.get("faithfulness"), int)]
        compl = [r["completeness"] for r in rows if isinstance(r.get("completeness"), int)]
        print(
            f"\n== {arm['name']}: {passed}/{n} PASS · "
            f"mean faithfulness {sum(faith)/len(faith):.2f} · "
            f"mean completeness {sum(compl)/len(compl):.2f}\n"
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote -> {OUT_PATH}")


if __name__ == "__main__":
    main()
