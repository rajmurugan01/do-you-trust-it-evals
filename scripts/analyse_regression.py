#!/usr/bin/env python3
"""Summarise regression_gate.json: what a CI gate built on these numbers would actually have seen.

Reads the round-1 baseline (judged.json) and the three regression arms, and prints, per arm, the
things a real gate is built on: the pass count, the mean scores, and how many individual items
moved relative to baseline. The control arm's movement is the noise floor — a regression arm has
to beat it to be a detection rather than a coincidence.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
BASELINE_PATH = ROOT / "data" / "results" / "judged.json"
ARMS_PATH = ROOT / "data" / "results" / "regression_gate.json"


def stats(rows):
    n = len(rows)
    faith = [r["faithfulness"] for r in rows if isinstance(r.get("faithfulness"), int)]
    compl = [r["completeness"] for r in rows if isinstance(r.get("completeness"), int)]
    return {
        "n": n,
        "passed": sum(1 for r in rows if r.get("verdict") == "PASS"),
        "failed": [r["slug"] for r in rows if r.get("verdict") == "FAIL"],
        "mean_faith": sum(faith) / len(faith) if faith else None,
        "mean_compl": sum(compl) / len(compl) if compl else None,
        "by_slug": {r["slug"]: r for r in rows},
    }


def main():
    baseline = stats(json.loads(BASELINE_PATH.read_text()))
    arms = json.loads(ARMS_PATH.read_text())["arms"]

    print(f"{'arm':<20} {'PASS':>7}  {'faith':>6}  {'compl':>6}  {'items moved vs baseline':>24}")
    print("-" * 74)
    print(
        f"{'baseline (round 1)':<20} {baseline['passed']:>3}/{baseline['n']:<3} "
        f"{baseline['mean_faith']:>6.2f}  {baseline['mean_compl']:>6.2f}  {'-':>24}"
    )

    for name, arm in arms.items():
        s = stats(arm["results"])
        moved = 0
        for slug, row in s["by_slug"].items():
            b = baseline["by_slug"].get(slug)
            if b and (row.get("faithfulness") != b.get("faithfulness")
                      or row.get("verdict") != b.get("verdict")):
                moved += 1
        print(
            f"{name:<20} {s['passed']:>3}/{s['n']:<3} "
            f"{s['mean_faith']:>6.2f}  {s['mean_compl']:>6.2f}  {moved:>24}"
        )

    print("\nPer-arm failures (what the gate would actually have flagged):")
    for name, arm in arms.items():
        s = stats(arm["results"])
        print(f"\n  {name} — {len(s['failed'])} FAIL")
        for slug in s["failed"]:
            row = s["by_slug"][slug]
            halls = row.get("hallucinations") or []
            print(f"    {slug}  faith={row.get('faithfulness')}")
            for h in halls[:2]:
                print(f"      - {h[:150]}")

    print("\nGate-threshold sweep (mean faithfulness), baseline = 5.00:")
    for name, arm in arms.items():
        s = stats(arm["results"])
        delta = s["mean_faith"] - baseline["mean_faith"]
        print(f"  {name:<20} mean {s['mean_faith']:.2f}  delta {delta:+.2f}")




def compare_summary_text():
    """Did the summaries actually change, independent of what the judge said about them?

    This is the check that separates the two readings of a flat gate result. If the judge reports
    no movement AND the summaries are materially identical, the gate is right and nothing
    regressed. If the summaries visibly changed and the judge still reports no movement, the gate
    is blind. Without this, a flat result is unattributable — the same trap round 2 avoided by
    diffing corrupted inputs against the original before running them.
    """
    baseline = {r["slug"]: r for r in json.loads(BASELINE_PATH.read_text())}
    arms = json.loads(ARMS_PATH.read_text())["arms"]

    print("\n\n=== Did the summary TEXT actually move? ===")
    print(f"{'arm':<20} {'identical':>10} {'mean chars':>12} {'baseline':>10} {'mean words':>12}")
    print("-" * 68)
    b_chars = sum(len(r["summary"]) for r in baseline.values()) / len(baseline)
    for name, arm in arms.items():
        rows = arm["results"]
        identical = sum(
            1 for r in rows
            if r["slug"] in baseline and r["summary"].strip() == baseline[r["slug"]]["summary"].strip()
        )
        chars = sum(len(r["summary"]) for r in rows) / len(rows)
        words = sum(len(r["summary"].split()) for r in rows) / len(rows)
        print(f"{name:<20} {identical:>7}/{len(rows):<3} {chars:>12.0f} {b_chars:>10.0f} {words:>12.1f}")

    print("\nSample: same post, baseline vs each arm (first 300 chars)")
    slug = "three-things-bedrock-workload"
    print(f"\n  [{slug}]")
    print(f"\n  baseline:\n    {baseline[slug]['summary'][:300]}")
    for name, arm in arms.items():
        row = next((r for r in arm["results"] if r["slug"] == slug), None)
        if row:
            print(f"\n  {name} (faith={row.get('faithfulness')}, {row.get('verdict')}):\n    {row['summary'][:300]}")


if __name__ == "__main__":
    main()
    compare_summary_text()
