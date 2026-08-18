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

# Each case is a SINGLE-VARIABLE edit of the real round-1 baseline summary: every word of the
# original is preserved byte-for-byte except the one injected error. This matters - an earlier
# pass at these cases also trimmed/reworded unrelated parts of the summary at the same time,
# which meant a FAIL verdict couldn't be attributed to the injected error alone. Fixed here so
# each case tests exactly one thing.
CASES = [
    {
        "slug": "three-things-bedrock-workload",
        "injection_type": "fabricated exact number (one sentence inserted, nothing else changed)",
        "corrupted_summary": (
            "This post documents three production lessons from diagnosing an AWS Bedrock AI "
            "workload: load tests can mask throttling exceptions invisible to higher-level "
            "metrics, latency decomposition reveals that time-to-first-token accounts for "
            "significant delays beyond model generation speed, and prompt caching is rarely "
            "enabled despite being a simple configuration change that dramatically reduces costs. "
            "The throttling stayed invisible for approximately 45 minutes before the load test "
            "surfaced it. The author provides specific CloudWatch metrics and measurements to "
            "support each lesson and explains how to detect and fix each issue."
        ),
        "injected_claim": "'invisible for approximately 45 minutes' (single inserted sentence; every "
        "other word is byte-identical to the round-1 baseline summary)",
    },
    {
        "slug": "agents-need-a-harness",
        "injection_type": "modality broadening (one clause replaced, nothing else changed)",
        "corrupted_summary": (
            "This post describes a costly failure mode where an AI agent ran in an infinite loop "
            "for days while returning successful responses, remaining invisible to standard "
            "monitoring. The author proves that every production AI agent will eventually run away "
            "without a hard iteration bound, and recommends both hard operational bounds on loop "
            "iterations and custom instrumentation to catch these failures—particularly "
            "per-session metrics that would surface a single stuck session against normal "
            "baselines."
        ),
        "injected_claim": "'proves that every production AI agent will eventually run away' replacing "
        "only the 'five specific conditions' clause; source describes five specific trigger "
        "conditions, not an inevitability claim; every other clause is byte-identical to round 1",
    },
    {
        "slug": "part-6-cost-performance-prompt-caching",
        "injection_type": "misattributed real number (one clause replaced, nothing else changed)",
        "corrupted_summary": (
            "This post covers cost optimization strategies for production AgentCore deployments on "
            "AWS, emphasizing that Bedrock model invocations are the dominant cost driver. The "
            "author measures a 99.9% prompt-cache hit ratio and a 78% billing reduction on "
            "system-prefix tokens, and recommends using cheaper models like Amazon Nova Pro for "
            "classification and summarization tasks instead of Claude Sonnet 4.5 for everything. "
            "The post also provides CloudWatch alarms to catch runaway costs, lifecycle "
            "configuration guidance, and estimates total monthly costs at $150-240 for a "
            "500-conversation-per-day agent."
        ),
        "injected_claim": "'99.9% hit ratio / 78% billing reduction' replacing only the '$480/month' "
        "clause; these numbers are real, but measured in a different post "
        "(prompt-caching-bedrock-strands), not this one; every other clause is byte-identical to "
        "round 1",
    },
    {
        "slug": "llm-is-not-a-security-boundary",
        "injection_type": "fabricated named entity (one sentence appended, nothing else changed)",
        "corrupted_summary": (
            "This post explains how to architect an AI agent system handling sensitive data by "
            "treating the language model as fundamentally untrustworthy and building deterministic "
            "security controls outside the model itself. The author argues that no single control "
            "is flawless, so the solution is layered defense-in-depth with ranked, load-bearing "
            "deterministic controls closest to the data and probabilistic safeguards as a backstop. "
            "The post details specific implementation patterns for identity verification, ACL "
            "filtering at retrieval time, tool allowlisting, and query validation to prevent both "
            "accidental model errors and prompt injection attacks from compromised data, "
            "illustrated with a comparison to how Okta implements zero-trust policy enforcement in "
            "its own agent products."
        ),
        "injected_claim": "'illustrated with a comparison to how Okta implements zero-trust policy "
        "enforcement in its own agent products' appended as a trailing clause; Okta is never "
        "mentioned in source; every other word is byte-identical to round 1",
    },
    {
        "slug": "part-2-cdk-infrastructure-bedrock-agentcore",
        "injection_type": "count inflation (one word changed, nothing else changed)",
        "corrupted_summary": (
            "This post documents twelve specific pitfalls when deploying AWS AgentCore using CDK, "
            "including naming restrictions (no hyphens), ECR repository timing issues, missing CDK "
            "constructs for logging configuration, VPC endpoint conflicts, KMS key policy "
            "requirements, security group rule representation in tests, mandatory parameters for "
            "runtime updates, Memory resource rollback failures, and array matching order "
            "sensitivity in CDK assertions. The author provides code examples and recovery "
            "procedures for each gotcha, along with a testing strategy using snapshot tests and "
            "targeted assertions."
        ),
        "injected_claim": "'twelve' replacing only 'nine' (source documents exactly nine); every "
        "other word is byte-identical to round 1",
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
