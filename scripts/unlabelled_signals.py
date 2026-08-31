#!/usr/bin/env python3
"""Round 6: can a signal with no golden answer catch what the labelled offline gate missed?

Rounds 4-5 established that a judge grading a golden dataset could not detect the deleted
guardrail: control 0/64 vs prompt_regression 1/64, Fisher exact p = 1.000, and power.py put the
minimum detectable effect at 16-43%. The reading I published was "sample size". That is true but it
is not the whole mechanism, and this round is the part I could not see from inside round 4.

A PASS/FAIL gate throws away almost everything it measured. Each summary is reduced to one bit, and
at a 1.6% base rate one bit per item carries almost no information. The judge was never the
bottleneck and neither was the dataset. The ENCODING was.

So this round changes nothing about the data and only changes what is computed from it. Same 64
control summaries, same 64 prompt_regression summaries, already on disk from rounds 4-5. No new
Bedrock calls, no judge, no labels. Every signal below is computable on live production traffic,
where you have the input and the output and nothing else:

  novel_numbers    numeric tokens in the summary that do not appear in the source
  grounding        fraction of the summary's content words that appear in the source
  novel_caps       capitalised tokens in the summary absent from the source (fabricated entities)
  length           characters

The comparison is deliberately not the same shape as round 4's. These are continuous, so the test
is a difference in means, not a difference in event counts.

Two tests per signal, for the same reason power.py reports two values of n. The 128 summaries are
4 repeats over 16 posts, so they are not independent:

  unclustered   permutation test over all 128, treating every summary as its own observation.
                Overstates n, same as n=64 did in round 4.
  clustered     one paired difference per post (mean of that post's control repeats minus mean of
                its regression repeats), then an EXACT sign-flip test enumerating all 2^16 = 65,536
                sign assignments. Conservative bound, and exact rather than approximated, for the
                same reason power.py refuses the normal approximation at these rates.

If a signal only survives the unclustered test it is not a finding, it is the clustering error
round 4 already made once.
"""
import json
import re
from itertools import product
from pathlib import Path

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
OUT_PATH = ROOT / "data" / "results" / "unlabelled_signals.json"

# The summariser saw body[:12000], not the whole post. Grounding has to be measured against what
# the model actually had in front of it, or a "novel" token is just one that sat past the cutoff.
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
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# An entity has to look like one. A bare Capitalised word is usually just a sentence start, which
# is what sank v1 of this instrument: "Summary", "Instead", "Rather", "Yes" outnumbered every real
# term 20 to 1 because the summariser emits a "# Summary" markdown header.
ENTITY_RE = re.compile(r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z]*|[A-Z]{2,}[a-z]*|[A-Z][a-zA-Z]*\d+\w*)\b")


def parse_frontmatter(raw: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    return m.group(2) if m else raw


def strip_markdown(text: str) -> str:
    """The summariser sometimes wraps output in a '# Summary' header and backticks. Format is not
    content, and v1 of this script spent 86 of its 96 'fabricated entity' hits on that header."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+.*$", " ", text, flags=re.MULTILINE)
    text = text.replace("`", " ").replace("*", " ")
    return text


def stem(w: str) -> str:
    """Crude, deliberately. 'Macs'/'LLMs'/'ACLs' were flagged as fabricated in v1 because the
    source says Mac, LLM, ACL. Plural-stripping fixes that class without pulling in a stemmer."""
    w = w.lower()
    for suf in ("'s", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def norm_num(tok: str) -> str:
    return tok.replace(",", "").rstrip(".").rstrip("%")


def num_grounded(n: str, src_nums: set) -> bool:
    """A summary number counts as grounded if the source states it, or states something it is a
    faithful rounding of. '99%+' against a source saying '99.9%' is a true restatement, not a
    fabrication, and counting it as one was v1's other big false-positive source."""
    if n in src_nums:
        return True
    try:
        v = float(n)
    except ValueError:
        return False
    for s in src_nums:
        try:
            sv = float(s)
        except ValueError:
            continue
        if sv == 0:
            continue
        # Rounded to a whole number, or to one fewer decimal place, or within 1%.
        if abs(sv - v) <= max(0.5, abs(sv) * 0.01):
            return True
        if str(s).startswith(str(n)) or str(n).startswith(str(s).split(".")[0]):
            return True
    return False


def ngrams(tokens, n=3):
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def signals(summary: str, source: str) -> dict:
    clean = strip_markdown(summary)

    src_nums = {norm_num(t) for t in NUM_RE.findall(source)}
    sum_nums = [norm_num(t) for t in NUM_RE.findall(clean)]
    novel_nums = [n for n in sum_nums if not num_grounded(n, src_nums)]

    src_lower = source.lower()
    src_words = {stem(w) for w in WORD_RE.findall(source)}
    sum_words = [w.lower() for w in WORD_RE.findall(clean) if w.lower() not in STOPWORDS]
    grounded = [w for w in sum_words if stem(w) in src_words]
    novel_words = [w for w in sum_words if stem(w) not in src_words]

    src_ents = {stem(c) for c in ENTITY_RE.findall(source)}
    sum_ents = ENTITY_RE.findall(clean)
    novel_ents = [c for c in sum_ents if stem(c) not in src_ents and c.lower() not in src_lower]

    src_tok = [t.lower() for t in TOKEN_RE.findall(source)]
    sum_tok = [t.lower() for t in TOKEN_RE.findall(clean)]
    src_tri = ngrams(src_tok)
    sum_tri = ngrams(sum_tok)
    tri_grounded = (len(sum_tri & src_tri) / len(sum_tri)) if sum_tri else 1.0

    return {
        "novel_numbers": len(novel_nums),
        "novel_numbers_list": novel_nums,
        "grounding": (len(grounded) / len(sum_words)) if sum_words else 1.0,
        "novel_words": len(novel_words),
        "novel_words_list": novel_words,
        "trigram_grounding": tri_grounded,
        "novel_caps": len(novel_ents),
        "novel_caps_list": novel_ents,
        "length": len(clean.strip()),
    }


def mean(xs):
    return sum(xs) / len(xs)


def perm_test_unclustered(a, b, n_perm=50000, seed=20260901):
    """Two-sided permutation test on the difference in means. Fixed seed: the number in the post
    has to be the number anyone re-running this gets."""
    import random

    rng = random.Random(seed)
    observed = mean(a) - mean(b)
    pool = list(a) + list(b)
    na = len(a)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if abs(mean(pool[:na]) - mean(pool[na:])) >= abs(observed) - 1e-12:
            hits += 1
    return observed, (hits + 1) / (n_perm + 1)


def sign_flip_exact(diffs):
    """Exact two-sided sign-flip test over paired per-cluster differences.

    Under the null the sign of each cluster's difference is arbitrary, so enumerate all 2^k
    assignments rather than sampling them. k = 16 here, so 65,536 cases: exact is affordable and
    there is no reason to approximate it."""
    k = len(diffs)
    observed = abs(mean(diffs))
    hits = 0
    for signs in product((1, -1), repeat=k):
        if abs(mean([s * d for s, d in zip(signs, diffs)])) >= observed - 1e-12:
            hits += 1
    return mean(diffs), hits / (2 ** k)


def main():
    sources = {}
    for path in sorted(POSTS_DIR.glob("*.md")):
        sources[path.stem] = parse_frontmatter(path.read_text())[:SOURCE_CHAR_LIMIT]

    # Rebuild the 4 repeats per arm from the two files rounds 4-5 wrote.
    gate = json.loads((ROOT / "data/results/regression_gate.json").read_text())
    reps = json.loads((ROOT / "data/results/regression_repeats.json").read_text())

    rows = []  # (arm, repeat, slug, signals)
    for arm in ("control", "prompt_regression"):
        for r in gate["arms"][arm]["results"]:
            rows.append((arm, 0, r["slug"], signals(r["summary"], sources[r["slug"]])))
    for run in reps["runs"]:
        for r in run["results"]:
            rows.append(
                (run["arm"], run["repeat"], r["slug"], signals(r["summary"], sources[r["slug"]]))
            )

    arms = sorted({a for a, _, _, _ in rows})
    slugs = sorted({s for _, _, s, _ in rows})
    print(f"{len(rows)} summaries · arms {arms} · {len(slugs)} posts\n")

    out = {
        "note": "Round 6. No new Bedrock calls: recomputed from rounds 4-5 outputs already on disk.",
        "n_per_arm": {a: sum(1 for x in rows if x[0] == a) for a in arms},
        "signals": {},
    }

    for sig in ("novel_numbers", "grounding", "trigram_grounding", "novel_words",
                "novel_caps", "length"):
        ctl = [s[sig] for a, _, _, s in rows if a == "control"]
        reg = [s[sig] for a, _, _, s in rows if a == "prompt_regression"]

        obs_u, p_u = perm_test_unclustered(reg, ctl)

        diffs = []
        for slug in slugs:
            c = [s[sig] for a, _, sl, s in rows if a == "control" and sl == slug]
            g = [s[sig] for a, _, sl, s in rows if a == "prompt_regression" and sl == slug]
            diffs.append(mean(g) - mean(c))
        obs_c, p_c = sign_flip_exact(diffs)

        out["signals"][sig] = {
            "control_mean": mean(ctl),
            "regression_mean": mean(reg),
            "unclustered": {"n_per_arm": len(ctl), "diff": obs_u, "p": p_u},
            "clustered": {"n_clusters": len(diffs), "mean_diff": obs_c, "p_exact": p_c},
            "per_cluster_diffs": diffs,
        }

        print(f"== {sig}")
        print(f"   control {mean(ctl):.4f}   regression {mean(reg):.4f}   diff {obs_u:+.4f}")
        print(f"   unclustered (n=64/arm, permutation)   p = {p_u:.4f}")
        print(f"   clustered   (k=16 posts, exact sign-flip) p = {p_c:.4f}")
        print()

    # What actually got invented, for the post. A p-value with no example behind it is unreadable.
    invented = [
        {"arm": a, "repeat": rp, "slug": sl, "numbers": s["novel_numbers_list"],
         "caps": s["novel_caps_list"]}
        for a, rp, sl, s in rows
        if s["novel_numbers_list"] or s["novel_caps_list"]
    ]
    out["invented_examples"] = invented
    print(f"{len(invented)}/{len(rows)} summaries contain at least one novel number or entity")

    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote -> {OUT_PATH}")


if __name__ == "__main__":
    main()
