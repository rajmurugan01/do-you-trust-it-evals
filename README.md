# do-you-trust-it-evals — LLM-as-judge calibration, and what it could not detect

A small, real experiment run against my own blog (16 published posts on rajmurugan.com), built to
answer one question before writing about it: when an LLM grades another LLM's output as
"faithful," how do you actually know that grade is trustworthy?

Everything here ran against real Amazon Bedrock calls in my own AWS account: rounds 1-3 on
2026-08-18, rounds 4-5 on 2026-08-24. No numbers below are invented for the writeup.

Rounds 1-3 calibrate the judge and it passes. Rounds 4-5 point the same rig at a regression gate,
where it fails, and the second half of this README is mostly about why the first half is not
sufficient.

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

## Round 4: can it gate a regression?

Rounds 1-3 ask whether the judge can be trusted. A CI regression gate asks something different:
nobody injected an error, nobody knows what changed, and the job is to notice a quality drop.

One variable changes: three anti-hallucination sentences deleted from the *summariser's* system
prompt (`DEGRADED_SYSTEM_PROMPT` in `regression_gate.py`), task sentence untouched. Two controls
run alongside: a **control arm** with the prompt byte-identical to baseline (the summariser runs at
temperature 0.3, so the baseline is not deterministic), and a **positive control** swapping the
summariser to Amazon Nova Micro.

| Arm | Verdict | Mean faithfulness |
|---|---|---|
| Control (nothing changed) | 16/16 PASS | 5.00 |
| Prompt regression (guardrail deleted) | 15/16 PASS | 4.94 |
| Model regression (Nova Micro) | 11/16 PASS | 4.19 |

One item moved. That is the shape of result that does not survive repetition, so
`regression_repeats.py` runs both Haiku arms three more times each:

| Arm | Failures per run | Total |
|---|---|---|
| Control | 0, 0, 0, 0 | 0 / 64 |
| Prompt regression | 1, 0, 0, 0 | 1 / 64 |

Two-sided Fisher exact: **p = 1.000**. Not a gradient, one event.

Two things worth pulling out of `regression_gate.json` directly. First, the one prompt-regression
failure is genuine: the judge flagged "caused real production issues" against a source that says
"burned an afternoon", and named the over-claim correctly. Second, the model-regression arm has
**11 PASS, 4 FAIL and one row with no `verdict` key at all** (faithfulness 4, completeness 3, a
populated hallucinations list, correct reasoning, no verdict). By the rubric that is a FAIL. A gate
asserting `passed == total` catches it; `failures == 0` and `passed / (passed + failed)` both
silently drop it.

## Round 5: was the judge the bottleneck?

The obvious explanation for round 4 is the judge: faithfulness pinned at 5.00 everywhere, a binary
verdict over a continuous change, no headroom. That is testable, so `trap_gate.py` tests it by
removing the judge entirely.

The trap moves to the **input**. Each source already states things at low intensity. An escalation
vocabulary ("production outage", "data loss", "guaranteed", "catastrophic", and similar) is filtered
per post to the phrases **verified absent from that post's own source**, so a hit can only be
something the model introduced. Then a substring check. No rubric, no scores, no ceiling.

| Gate | Control | Prompt regression | Fisher exact |
|---|---|---|---|
| LLM judge over golden dataset | 0 / 64 | 1 / 64 | p = 1.000 |
| Deterministic, no judge | 1 / 64 | 1 / 64 | p = 1.000 |

Removing the judge changed nothing, so "the judge cannot see small drops" does not stand on its own.
Two blunt instruments returning the same null cannot tell you which one is blunt.

Caveat worth stating: the vocabulary is **not independent** of round 4. It contains "production
issues", the phrase the judge flagged there. This is a weaker replication than two independent
designs would be.

## The calculation that should have come first

`power.py` computes the minimum detectable effect for this design. Exact, because the usual
two-proportion normal approximation wants roughly five expected events per arm and there is **one**
at n=64 (0.25 at n=16). Out of range, not borderline; run it anyway and the standard approximations
disagree with each other by nearly a factor of two.

```
n= 64 per arm   MDE 16.1%  (10.3x baseline)   expected events 1.00
n= 16 per arm   MDE 42.6%  (27.3x baseline)   expected events 0.25
```

Both are reported because the right n is arguable: 64 treats every grading as independent when it is
4 repeats over the same 16 posts; 16 is the conservative bound. Miller,
["Adding Error Bars to Evals"](https://arxiv.org/abs/2411.00640) (2024), is explicit that the truth
sits on a sliding scale between them.

So the sample size alone explains both nulls, whatever the instruments were doing. That is the
finding: **the gate could not have detected this regression, and one calculation would have said so
before any of it ran.**

## Why this matters for evals in production

A judge that passes a clean baseline hasn't been calibrated, it's been flattered. Calibrating it
means testing it against known-bad inputs across more than one failure category (a fabricated fact,
a scope-broadened claim, a misattributed-but-real number, an invented entity, an inflated count all
fail differently), and testing whether it holds the same grade, including completeness, on repeated
runs of both an easy and a hard input. On this small experiment, the judge passed all three bars.
Published research says that's not guaranteed to generalise to other models, prompts, or batch
sizes, so re-run your own version of rounds 2 and 3 before wiring an LLM judge into anything that
blocks a deploy.

And then note what rounds 4-5 add, because clearing rounds 1-3 turned out not to be sufficient: a
calibrated, stable judge still could not detect a real prompt regression, and neither could a
judge-free deterministic gate. Compute the minimum detectable effect for your design *before* you
trust a null result from it. An eval that cannot resolve the effect you care about does not return
"no regression", it returns nothing, in a format that looks exactly like "no regression". And note what this doesn't test: every Round 2 injection is a benign, accidental
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

# rounds 4-5, and the calculation that should have preceded them
python3 scripts/regression_gate.py       # three arms, one judge
python3 scripts/regression_repeats.py    # repeat both Haiku arms
python3 scripts/analyse_regression.py    # reconcile the numbers
python3 scripts/trap_gate.py             # judge-free deterministic gate
python3 scripts/power.py                 # no AWS calls, stdlib only
```

Source posts are copied from [rajmurugan.com](https://rajmurugan.com)'s public blog content for
convenience; see `data/posts/`.

---

Companion write-ups, part of the *Evals for Production AI* series on rajmurugan.com:

- Part 1, rounds 1-3: [A clean pass rate is not calibration](https://rajmurugan.com/blog/clean-pass-rate-is-not-calibration)
- Part 2, rounds 4-5: [Your regression gate needs a power calculation](https://rajmurugan.com/blog/regression-gate-needs-a-power-calculation)
