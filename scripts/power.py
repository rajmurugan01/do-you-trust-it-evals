#!/usr/bin/env python3
"""Minimum detectable effect for the round 4/5 gates. Stdlib only, no scipy.

The post this repo backs is named after this calculation, so it belongs here rather than in a
footnote. It is the thing I should have run before spending anything on rounds 4 and 5.

Why exact rather than the usual two-proportion normal approximation: that approximation wants
roughly np >= 5 expected events per arm. Here the baseline rate is 1/64 and n is 64, so np = 1.0,
and at n = 16 it is 0.25. Out of range, not borderline. Running it anyway gives 15.4%, which is
what I originally published and had to correct; the standard methods disagree with each other by
about a factor of two at these rates, which is itself the tell that none of them applies.

So power is computed exactly: enumerate every (x, y) outcome pair under two independent binomials,
run the same two-sided Fisher exact test the post uses everywhere else, and sum the probability
mass where it would reject.

    $ python3 scripts/power.py

    n= 64 per arm   MDE 16.1%  (10.3x baseline)   expected events 1.00
    n= 16 per arm   MDE 42.6%  (27.3x baseline)   expected events 0.25

Both are reported because the right n is genuinely arguable. 64 counts every grading as
independent, which overstates it: the 64 are 4 repeats over the same 16 posts. 16 counts each post
once, which is the conservative bound. Miller (arXiv 2411.00640) is explicit that the truth sits on
a sliding scale between the two depending on within-post correlation, so a single number here would
be false precision either way.
"""
from math import comb

ALPHA = 0.05
POWER = 0.80
BASELINE = 1 / 64  # the observed per-grading hallucination rate, both arms, both gates


def fisher_two_sided(a, b, c, d):
    """Two-sided Fisher exact p by hypergeometric enumeration."""
    n = a + b + c + d
    prob = lambda A, B, C, D: comb(A + B, A) * comb(C + D, C) / comb(n, A + C)
    observed = prob(a, b, c, d)
    total = 0.0
    for i in range(0, min(a + c, a + b) + 1):
        A, B, C, D = i, (a + b) - i, (a + c) - i, (c + d) - ((a + c) - i)
        if B < 0 or C < 0 or D < 0:
            continue
        p = prob(A, B, C, D)
        if p <= observed + 1e-12:
            total += p
    return total


def exact_power(n, p0, p1, alpha=ALPHA):
    """P(reject) under independent Binomial(n, p0) and Binomial(n, p1)."""
    b0 = [comb(n, k) * p0**k * (1 - p0) ** (n - k) for k in range(n + 1)]
    b1 = [comb(n, k) * p1**k * (1 - p1) ** (n - k) for k in range(n + 1)]
    out = 0.0
    for x in range(n + 1):
        if b0[x] < 1e-12:
            continue
        for y in range(n + 1):
            if b1[y] < 1e-12:
                continue
            if fisher_two_sided(x, n - x, y, n - y) <= alpha:
                out += b0[x] * b1[y]
    return out


def mde(n, p0=BASELINE, power=POWER):
    """Smallest p1 > p0 this design detects with the requested power. Bisection."""
    lo, hi = p0, 0.99
    for _ in range(40):
        mid = (lo + hi) / 2
        if exact_power(n, p0, mid) >= power:
            hi = mid
        else:
            lo = mid
    return hi


if __name__ == "__main__":
    print(f"two-sided Fisher exact, alpha={ALPHA}, power={POWER}, baseline={BASELINE*100:.2f}%\n")
    for n in (64, 16):
        m = mde(n)
        print(f"n={n:>3} per arm   MDE {m*100:.1f}%  ({m/BASELINE:.1f}x baseline)"
              f"   expected events {n*BASELINE:.2f}")
