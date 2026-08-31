#!/usr/bin/env python3
"""The BROKEN first version of round 6's extractors, kept so the post's claim is auditable.

The post says this version reported 96 of 128 summaries containing a fabricated number or
entity, and that 86 of those hits were the word "Summary" from the markdown header the
summariser emits. That claim was originally only in a code comment, which is not good enough
for a post whose whole argument is "read what your signal flagged". So here is the code that
produced it, verbatim in behaviour, plus a runner that prints the two numbers.

What is wrong with it, all four of which the fixed version handles:
  1. No markdown stripping, so the "# Summary" header is parsed as a fabricated entity.
  2. Any capitalised token counts as an entity, so sentence-initial words do too.
  3. No stemming, so "Macs"/"LLMs"/"ACLs" are novel against a source saying Mac/LLM/ACL.
  4. Exact number matching, so "99%+" is novel against a source saying "99.8%".

Run: python3 scripts/unlabelled_signals_v1.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
SOURCE_CHAR_LIMIT = 12000

STOPWORDS = {
    "that", "this", "with", "from", "have", "here", "into", "what", "when", "which", "their",
    "there", "them", "then", "they", "than", "your", "yours", "about", "would", "could", "should",
    "these", "those", "been", "being", "were", "will", "more", "most", "some", "such", "only",
    "also", "just", "even", "over", "under", "after", "before", "between", "because", "while",
    "does", "doing", "done", "each", "both", "same", "other", "much", "many", "very", "post",
    "author", "summary", "article", "blog", "writes", "describes", "explains", "discusses",
}

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*%?")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
CAP_RE = re.compile(r"\b[A-Z][A-Za-z0-9_.]{2,}\b")


def parse_frontmatter(raw: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    return m.group(2) if m else raw


def norm_num(tok: str) -> str:
    return tok.replace(",", "").rstrip(".").rstrip("%")


def signals_v1(summary: str, source: str) -> dict:
    src_nums = {norm_num(t) for t in NUM_RE.findall(source)}
    novel_nums = [norm_num(t) for t in NUM_RE.findall(summary) if norm_num(t) not in src_nums]

    src_lower = source.lower()
    src_caps = {c for c in CAP_RE.findall(source)}
    novel_caps = [c for c in CAP_RE.findall(summary)
                  if c not in src_caps and c.lower() not in src_lower]
    return {"novel_numbers_list": novel_nums, "novel_caps_list": novel_caps}


def main():
    import collections

    sources = {p.stem: parse_frontmatter(p.read_text())[:SOURCE_CHAR_LIMIT]
               for p in sorted(POSTS_DIR.glob("*.md"))}
    gate = json.loads((ROOT / "data/results/regression_gate.json").read_text())
    reps = json.loads((ROOT / "data/results/regression_repeats.json").read_text())

    rows = []
    for arm in ("control", "prompt_regression"):
        rows += [(arm, r["slug"], r["summary"]) for r in gate["arms"][arm]["results"]]
    for run in reps["runs"]:
        if run["arm"] in ("control", "prompt_regression"):
            rows += [(run["arm"], r["slug"], r["summary"]) for r in run["results"]]

    caps = collections.Counter()
    flagged = 0
    for arm, slug, summary in rows:
        s = signals_v1(summary, sources[slug])
        caps.update(s["novel_caps_list"])
        if s["novel_numbers_list"] or s["novel_caps_list"]:
            flagged += 1

    print(f"v1 flagged {flagged}/{len(rows)} summaries as containing a fabricated number or entity")
    print(f"'Summary' alone accounts for {caps['Summary']} of the entity hits")
    print("top entity hits:", caps.most_common(6))


if __name__ == "__main__":
    main()
