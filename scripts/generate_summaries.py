#!/usr/bin/env python3
"""Generate a 2-3 sentence AI summary for each blog post using Bedrock Claude Haiku 4.5."""
import json
import os
import re
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

POSTS_DIR = Path(__file__).parent.parent / "data" / "posts"
OUT_PATH = Path(__file__).parent.parent / "data" / "results" / "summaries.json"

SYSTEM_PROMPT = (
    "You summarize technical blog posts in 2-3 sentences for a reader deciding whether to "
    "click through. Base the summary ONLY on the text provided. Do not add claims, numbers, "
    "product names, or examples that are not explicitly present in the source text. If the "
    "post does not state a specific number or outcome, do not invent one."
)


def parse_frontmatter(raw: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw
    fm_text, body = m.group(1), m.group(2)
    title_m = re.search(r'^title:\s*"?(.*?)"?\s*$', fm_text, re.MULTILINE)
    title = title_m.group(1) if title_m else "(untitled)"
    return {"title": title}, body


def main():
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "personal"))
    client = session.client("bedrock-runtime", region_name=REGION)

    results = []
    files = sorted(POSTS_DIR.glob("*.md"))
    for i, path in enumerate(files, 1):
        slug = path.stem
        raw = path.read_text()
        fm, body = parse_frontmatter(raw)
        title = fm.get("title", slug)

        print(f"[{i}/{len(files)}] summarizing {slug} ...")
        resp = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": body[:12000]}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0.3},
        )
        summary = resp["output"]["message"]["content"][0]["text"].strip()
        usage = resp.get("usage", {})

        results.append(
            {
                "slug": slug,
                "title": title,
                "summary": summary,
                "model": MODEL_ID,
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
            }
        )
        time.sleep(0.3)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {len(results)} summaries -> {OUT_PATH}")


if __name__ == "__main__":
    main()
