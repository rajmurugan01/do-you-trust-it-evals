#!/usr/bin/env python3
"""Round 7 analysis: does detectability depend on the input distribution?

Reads rounds 4-5 (full source, 12000 chars) and round 7 (2000 and 600 chars) and applies the SAME
two instruments to each, so the only thing varying down the table is how much source the summariser
was given:

  labelled     the round 4 gate. Judge verdict PASS/FAIL, Fisher exact on the counts. Needs a
               golden answer, which in production you do not have.
  unlabelled   the round 6 signals. Computed from (input, output) alone, no judge, no labels, so
               these are the ones that could actually run on live traffic.

Both instruments were built before this round and neither is tuned to it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from power import fisher_two_sided
from unlabelled_signals import (
    parse_frontmatter,
    perm_test_unclustered,
    sign_flip_exact,
    signals,
    mean,
)

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
ARMS = ("control", "prompt_regression")
SIGS = ("grounding", "trigram_grounding", "novel_words", "novel_numbers", "novel_caps")


def load_rows():
    """-> {truncation: [(arm, repeat, slug, summary, verdict)]}"""
    by_trunc = {}
    gate = json.loads((ROOT / "data/results/regression_gate.json").read_text())
    reps = json.loads((ROOT / "data/results/regression_repeats.json").read_text())
    full = []
    for arm in ARMS:
        for r in gate["arms"][arm]["results"]:
            full.append((arm, 0, r["slug"], r["summary"], r.get("verdict")))
    for run in reps["runs"]:
        if run["arm"] in ARMS:
            for r in run["results"]:
                full.append((run["arm"], run["repeat"], r["slug"], r["summary"], r.get("verdict")))
    by_trunc[12000] = full

    ood_path = ROOT / "data/results/ood_inputs.json"
    if ood_path.exists():
        ood = json.loads(ood_path.read_text())
        for run in ood["runs"]:
            by_trunc.setdefault(run["truncation"], []).extend(
                (run["arm"], run["repeat"], r["slug"], r["summary"], r.get("verdict"))
                for r in run["results"]
            )
    return by_trunc


def main():
    full_sources = {
        path.stem: parse_frontmatter(path.read_text())
        for path in sorted(POSTS_DIR.glob("*.md"))
    }

    by_trunc = load_rows()
    report = {}

    for trunc in sorted(by_trunc, reverse=True):
        rows = by_trunc[trunc]
        print(f"\n{'=' * 74}\nSOURCE GIVEN TO SUMMARISER: {trunc} chars   ({len(rows)} summaries)\n{'=' * 74}")
        entry = {"n_total": len(rows)}

        # --- labelled instrument (needs ground truth) ---
        counts = {}
        for arm in ARMS:
            arm_rows = [r for r in rows if r[0] == arm]
            counts[arm] = (sum(1 for r in arm_rows if r[4] == "FAIL"), len(arm_rows))
        (fc, fn), (rc, rn) = counts["control"], counts["prompt_regression"]
        p_lab = fisher_two_sided(fc, fn - fc, rc, rn - rc)
        print(f"\n  LABELLED (judge verdict, needs a golden answer)")
        print(f"    control {fc}/{fn} FAIL   regression {rc}/{rn} FAIL   Fisher exact p = {p_lab:.4f}")
        entry["labelled"] = {"control_fail": fc, "control_n": fn,
                             "regression_fail": rc, "regression_n": rn, "p": p_lab}

        # --- unlabelled instrument (runs on live traffic) ---
        sig_rows = [
            (arm, rep, slug, signals(summary, full_sources[slug][:trunc]))
            for arm, rep, slug, summary, _ in rows
        ]
        slugs = sorted({s for _, _, s, _ in sig_rows})
        print(f"\n  UNLABELLED (input+output only, no judge, no labels)")
        print(f"    {'signal':<20} {'control':>9} {'regress':>9} {'perm p':>9} {'exact p':>9}")
        entry["unlabelled"] = {}
        for sig in SIGS:
            ctl = [s[sig] for a, _, _, s in sig_rows if a == "control"]
            reg = [s[sig] for a, _, _, s in sig_rows if a == "prompt_regression"]
            if not ctl or not reg:
                continue
            _, p_u = perm_test_unclustered(reg, ctl)
            diffs = []
            for slug in slugs:
                c = [s[sig] for a, _, sl, s in sig_rows if a == "control" and sl == slug]
                g = [s[sig] for a, _, sl, s in sig_rows if a == "prompt_regression" and sl == slug]
                if c and g:
                    diffs.append(mean(g) - mean(c))
            _, p_c = sign_flip_exact(diffs) if diffs else (0, 1.0)
            star = "  <--" if p_c < 0.05 else ""
            print(f"    {sig:<20} {mean(ctl):>9.4f} {mean(reg):>9.4f} {p_u:>9.4f} {p_c:>9.4f}{star}")
            entry["unlabelled"][sig] = {
                "control_mean": mean(ctl), "regression_mean": mean(reg),
                "p_unclustered": p_u, "p_clustered_exact": p_c,
                "n_clusters": len(diffs),
            }
        report[trunc] = entry

    out = ROOT / "data/results/ood_analysis.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nWrote -> {out}")


if __name__ == "__main__":
    main()
