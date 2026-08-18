#!/usr/bin/env python3
"""Calibration check: does the judge actually discriminate, or does it rubber-stamp PASS?

Feeds the same judge five deliberately corrupted summaries (each with one injected error of a
different kind) against their real source posts, and checks whether it catches each one.
"""
import json
import os
import re
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
JUDGE_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "calibration.json"

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

# Each case: a real post + a hand-corrupted summary, one distinct injection type each.
CASES = [
    {
        "slug": "three-things-bedrock-workload",
        "injection_type": "fabricated exact number",
        "corrupted_summary": (
            "This post documents three production lessons from diagnosing an AWS Bedrock AI "
            "workload: load tests masked throttling exceptions for 45 minutes before they became "
            "visible in CloudWatch, latency decomposition reveals that time-to-first-token "
            "accounts for significant delays beyond model generation speed, and prompt caching is "
            "rarely enabled despite being a simple configuration change that dramatically reduces "
            "costs."
        ),
        "injected_claim": "throttling exceptions masked for 45 minutes (invented duration, not in source)",
    },
    {
        "slug": "agents-need-a-harness",
        "injection_type": "modality broadening (conditional source claim stated as universal law)",
        "corrupted_summary": (
            "This post describes a costly failure mode where an AI agent ran in an infinite loop "
            "for days while returning successful responses, remaining invisible to standard "
            "monitoring. The author proves that every production AI agent will eventually run away "
            "without a hard iteration bound, and recommends custom instrumentation to catch these "
            "failures."
        ),
        "injected_claim": "'every production AI agent will eventually run away' (source describes five "
        "specific trigger conditions, not an inevitability claim)",
    },
    {
        "slug": "part-6-cost-performance-prompt-caching",
        "injection_type": "misattributed real number (correct figure, wrong source post)",
        "corrupted_summary": (
            "This post covers cost optimization strategies for production AgentCore deployments on "
            "AWS, emphasizing that Bedrock model invocations are the dominant cost driver. The "
            "author measures a 99.9% prompt-cache hit ratio and a 78% billing reduction on system-"
            "prefix tokens, and recommends using cheaper models like Amazon Nova Pro for "
            "classification and summarization tasks instead of Claude Sonnet 4.5 for everything."
        ),
        "injected_claim": "'99.9% hit ratio / 78% billing reduction' (these numbers are real, but "
        "measured in a different post, prompt-caching-bedrock-strands, not this one)",
    },
    {
        "slug": "llm-is-not-a-security-boundary",
        "injection_type": "fabricated named entity",
        "corrupted_summary": (
            "This post explains how to architect an AI agent system handling sensitive data by "
            "treating the language model as fundamentally untrustworthy and building deterministic "
            "security controls outside the model, illustrated with a comparison to how Okta "
            "implements zero-trust policy enforcement in its own agent products."
        ),
        "injected_claim": "'Okta' and its zero-trust agent products (never mentioned in source)",
    },
    {
        "slug": "part-2-cdk-infrastructure-bedrock-agentcore",
        "injection_type": "count inflation",
        "corrupted_summary": (
            "This post documents twelve specific pitfalls when deploying AWS AgentCore using CDK, "
            "including naming restrictions (no hyphens), ECR repository timing issues, and missing "
            "CDK constructs for logging configuration. The author provides code examples and "
            "recovery procedures for each gotcha."
        ),
        "injected_claim": "'twelve' pitfalls (source documents exactly nine)",
    },
]


def load_body(slug: str) -> str:
    raw = (POSTS_DIR / f"{slug}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    return m.group(2) if m else raw


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    results = []
    for i, case in enumerate(CASES, 1):
        slug = case["slug"]
        source = load_body(slug)
        summary = case["corrupted_summary"]

        print(f"[{i}/{len(CASES)}] calibration case: {slug} ({case['injection_type']}) ...")
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

        caught = judged.get("verdict") == "FAIL"
        results.append(
            {
                "slug": slug,
                "injection_type": case["injection_type"],
                "injected_claim": case["injected_claim"],
                "corrupted_summary": summary,
                "judge_verdict": judged.get("verdict"),
                "judge_hallucinations": judged.get("hallucinations"),
                "judge_reasoning": judged.get("reasoning"),
                "caught": caught,
            }
        )
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2))

    caught_n = sum(1 for r in results if r["caught"])
    print(f"\n{caught_n}/{len(results)} injected errors caught (verdict=FAIL)")
    for r in results:
        mark = "CAUGHT" if r["caught"] else "MISSED"
        print(f"  [{mark}] {r['slug']} — {r['injection_type']}")
    print(f"\nWrote calibration results -> {OUT_PATH}")


if __name__ == "__main__":
    main()
