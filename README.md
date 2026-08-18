# Do You Trust It? — an LLM-as-judge calibration experiment

A small, real experiment run against my own blog (16 published posts on rajmurugan.com), built to
answer one question before writing about it: when an LLM grades another LLM's output as
"faithful," how do you actually know that grade is trustworthy?

Everything here ran against real Amazon Bedrock calls in my own AWS account on 2026-08-18. No
numbers below are invented for the writeup.

## Setup

- **Summarizer:** `us.anthropic.claude-haiku-4-5-20251001-v1:0` writes a 2-3 sentence summary of
  each post, instructed to use only claims present in the source.
- **Judge:** `us.anthropic.claude-sonnet-4-5-20250929-v1:0` scores each summary against its source
  on faithfulness (1-5), completeness (1-5), and a list of any unsupported claims, with a strict
  rubric that treats scope-broadening ("sometimes" becoming "always") as a hallucination, not just
  invented facts.

Scripts: [`generate_summaries.py`](scripts/generate_summaries.py),
[`judge.py`](scripts/judge.py), [`judge_calibration.py`](scripts/judge_calibration.py),
[`self_consistency_check.py`](scripts/self_consistency_check.py). Raw output in
[`data/results/`](data/results/).

## Round 1: baseline

16/16 summaries passed, all scored faithfulness=5. Spot-checked several by hand against source
(specific counts, dollar figures, percentages) and confirmed the summaries really were faithful:
this wasn't a rubber stamp catching nothing because there was nothing to catch. On tightly-written,
information-dense source posts, a small model with a narrow instruction produced clean summaries.

A 100% pass rate on its own proves the eval didn't break on the easy case. It proves nothing about
whether the judge would catch a hard one. So round 2 calibrates the judge itself.

## Round 2: judge calibration

Five single-variable corrupted summaries, one distinct injection type each. Every corrupted summary
is the round-1 baseline summary with exactly one change; everything else is byte-identical to the
original, so a FAIL verdict can be attributed to that one change and nothing else:

| Injection type | Post | Change | Caught? |
|---|---|---|---|
| Fabricated exact number | three-things-bedrock-workload | One sentence inserted | FAIL (caught) |
| Modality broadening (conditional → universal) | agents-need-a-harness | One clause replaced | FAIL (caught) |
| Misattributed real number (correct figure, wrong post) | part-6-cost-performance-prompt-caching | One clause replaced | FAIL (caught) |
| Fabricated named entity | llm-is-not-a-security-boundary | One clause appended | FAIL (caught) |
| Count inflation (nine → twelve) | part-2-cdk-infrastructure-bedrock-agentcore | One word changed | FAIL (caught) |

5/5 caught, and in every case the judge's own reported `hallucinations` list named exactly the
injected error (word for word or near enough), not something else in the summary. On the CDK case
it went a step further than intended: alongside the injected "twelve", it also flagged that the
corrupted summary compressed "`Match.arrayWith` being order-sensitive" into a vaguer "array matching
order sensitivity", a real precision loss I hadn't deliberately engineered. The two hardest
categories in the table (modality broadening and the misattributed-but-real number) are the classic
blind spots for LLM-as-judge setups, because nothing in the summary is a "fake fact" in isolation.

## Round 3: self-consistency

Calibration tells you the judge can discriminate a known-bad input from a known-good one. It
doesn't tell you whether the judge is *stable*: whether grading the exact same, unmodified input
twice gives you the same answer. Published research on LLM-as-judge setups documents real drift
here, e.g. Lau, ["Same Input, Different Scores: A Multi Model Study on the Inconsistency of LLM
Judge"](https://arxiv.org/abs/2603.04417) (2026), which found substantial score variability across
models even at temperature 0, with completeness scoring showing the largest fluctuations of the
metrics tested.

A clean input repeating cleanly is the least informative thing to test, so this checks two inputs,
not one: the round-1 baseline summary for `three-things-bedrock-workload` (unmodified, already
passing), and the round-2 corrupted case for `agents-need-a-harness` (the modality-broadening one,
a genuinely borderline call). Each went back through the same judge four more times, temperature 0,
tracking both faithfulness and completeness this time, since that's the metric the cited paper
flags as shakiest.

| Input | Verdict (4 runs) | Faithfulness (4 runs) | Completeness (4 runs) |
|---|---|---|---|
| Easy (round-1 clean baseline) | PASS ×4 | 5, 5, 5, 5 | 5, 5, 5, 5 |
| Hard (round-2 corrupted, modality broadening) | FAIL ×4 | 2, 2, 2, 2 | 3, 3, 3, 3 |

Both stable, on both inputs, on this judge, on this day, on the metric the paper says wobbles most.

## Why this matters for evals in production

A judge that passes a clean baseline hasn't been calibrated, it's been flattered. Calibrating it
means testing it against known-bad inputs across more than one failure category (a fabricated fact,
a scope-broadened claim, a misattributed-but-real number, an invented entity, an inflated count all
fail differently), and testing whether it holds the same grade, including completeness, on repeated
runs of both an easy and a hard input. On this small experiment, the judge passed all three bars.
Published research says that's not guaranteed to generalise to other models, prompts, or batch
sizes, so re-run your own version of rounds 2 and 3 before wiring an LLM judge into anything that
blocks a deploy. And note what this doesn't test: every Round 2 injection is a benign, accidental
error, not an adversarial one. Whether the judge (or the summarizer feeding it) can be manipulated
by adversarial content in the source document is a different, harder experiment.

## Reproduce it

```bash
export AWS_PROFILE=<a profile with Bedrock access>
pip install boto3
python3 scripts/generate_summaries.py
python3 scripts/judge.py
python3 scripts/judge_calibration.py
python3 scripts/self_consistency_check.py
```

Source posts are copied from [rajmurugan.com](https://rajmurugan.com)'s public blog content for
convenience; see `data/posts/`.

---

Companion write-up: [Do You Trust It? — Part 1](https://rajmurugan.com/blog/) (rajmurugan.com),
part of the *Do You Trust It?* series on evals for production AI.
