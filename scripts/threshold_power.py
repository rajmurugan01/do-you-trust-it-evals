#!/usr/bin/env python3
"""Round 8: a continuous signal does not come with a threshold. Work out where the line goes.

Round 7 left trigram grounding as the one label-free signal worth replicating. That is not the
same as being able to alert on it. An alert needs a threshold, a threshold implies a false-page
rate, and neither has been computed anywhere in this series.

Part 2 ran a power calculation for a BINARY gate (Fisher exact on pass/fail counts). This is the
same question for a CONTINUOUS one, and the method has to change with it: the control
distribution is right-skewed with a hard floor at zero (one control output scores exactly 0.0000
and p05 is 0.017 against a median of 0.074), so mean-minus-k-sigma thresholds walk straight off
the bottom of the scale and catch nothing. Everything below is therefore resampled from the real
observed distribution rather than assuming a normal one.

The question the post has to answer: for a stated false-page budget, how many outputs does an
alerting window need before a real regression clears ordinary day-to-day variance?

    $ python3 scripts/threshold_power.py
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from unlabelled_signals import parse_frontmatter, signals, mean

ROOT = Path(__file__).parent.parent
TRUNC = 2000          # the tier where the label-free signal separates the arms at all
N_SIM = 20000
SEED = 20260908


def load(trunc):
    src = {p.stem: parse_frontmatter(p.read_text())
           for p in sorted((ROOT / "data/posts").glob("*.md"))}
    ood = json.loads((ROOT / "data/results/ood_inputs.json").read_text())
    out = {"control": [], "prompt_regression": []}
    for run in ood["runs"]:
        if run["truncation"] != trunc:
            continue
        for r in run["results"]:
            out[run["arm"]].append(
                signals(r["summary"], src[r["slug"]][:trunc])["trigram_grounding"])
    return out["control"], out["prompt_regression"]


def batch_means(pool, n, rng, k=N_SIM):
    """Resample the OBSERVED distribution. The control arm is skewed with a floor at zero, so a
    normal approximation would put the alert threshold below the range the signal can occupy."""
    return sorted(mean([pool[rng.randrange(len(pool))] for _ in range(n)]) for _ in range(k))


def main():
    rng = random.Random(SEED)
    ctl, reg = load(TRUNC)
    print(f"trigram grounding at {TRUNC} chars: control n={len(ctl)}, regression n={len(reg)}")
    print(f"  control mean {mean(ctl):.4f}   regression mean {mean(reg):.4f}   "
          f"drop {100*(1-mean(reg)/mean(ctl)):.1f}%\n")

    print("  A window of n outputs, thresholded at the false-page budget, catches the regression:")
    print(f"  {'window n':>9} | {'1 page a month (3%)':>21} | {'1 page a quarter (1%)':>22}")
    print(f"  {'-'*9}-+-{'-'*21}-+-{'-'*22}")
    answer = {}
    for n in (1, 4, 8, 16, 24, 32, 48, 64):
        null = batch_means(ctl, n, rng)
        alt = batch_means(reg, n, rng)
        row = []
        for alpha in (0.03, 0.01):
            thr = null[int(alpha * (len(null) - 1))]
            power = sum(1 for v in alt if v < thr) / len(alt)
            row.append(power)
            answer.setdefault(alpha, None)
            if answer[alpha] is None and power >= 0.80:
                answer[alpha] = n
        print(f"  {n:>9} | {row[0]:>20.0%} | {row[1]:>21.0%}")

    print()
    for alpha, label in ((0.03, "one false page a month"), (0.01, "one a quarter")):
        n = answer[alpha]
        print(f"  80% power at {label}: {'n = ' + str(n) if n else 'not reached by n = 64'}")

    print("\n  Why not mean minus k sigma, the reflex answer:")
    import statistics as st
    m, sd = mean(ctl), st.stdev(ctl)
    for k in (1.0, 1.5, 2.0):
        thr = m - k * sd
        fp = sum(1 for v in ctl if v < thr) / len(ctl)
        tp = sum(1 for v in reg if v < thr) / len(reg)
        note = "  <- below the floor of the scale" if thr < 0 else ""
        print(f"    mean - {k}sd = {thr:+.4f}: pages on {fp:.0%} of normal, catches {tp:.0%}{note}")


if __name__ == "__main__":
    main()
