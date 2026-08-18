# Do You Trust It? — an LLM-as-judge calibration experiment

A small, real experiment run against my own blog (16 published posts on rajmurugan.com), built to
answer one question honestly before writing about it: when an LLM grades another LLM's output as
"faithful," is that grade actually trustworthy?

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
[`judge.py`](scripts/judge.py), [`judge_calibration.py`](scripts/judge_calibration.py). Raw output
in [`data/results/`](data/results/).

## Round 1: baseline

16/16 summaries passed, all scored faithfulness=5. Spot-checking several by hand (specific counts,
dollar figures, and percentages against source) confirmed the summaries really were faithful — this
wasn't a rubber stamp catching nothing because there was nothing to catch. On tightly-written,
information-dense source posts, a small model with a narrow instruction produced clean summaries.

A 100% pass rate on the first run is exactly the result that should make you suspicious of the eval,
not proud of the system. So round 2 tests the judge itself, not the summarizer.

## Round 2: judge calibration

Five deliberately corrupted summaries, each with one injected error of a different kind, run back
through the same judge:

| Injection type | Post | Caught? |
|---|---|---|
| Fabricated exact number | three-things-bedrock-workload | FAIL (caught) |
| Modality broadening (conditional → universal) | agents-need-a-harness | FAIL (caught) |
| Misattributed real number (correct figure, wrong post) | part-6-cost-performance-prompt-caching | FAIL (caught) |
| Fabricated named entity | llm-is-not-a-security-boundary | FAIL (caught) |
| Count inflation (nine → twelve) | part-2-cdk-infrastructure-bedrock-agentcore | FAIL (caught) |

5/5 caught, including the two hardest categories in that list — modality broadening and
misattributed-but-real numbers are the classic blind spots for LLM-as-judge setups, because nothing
in the summary is a "fake fact" in isolation. The judge did not rubber-stamp.

## The actual finding

Diffing the two rounds surfaced something neither round shows on its own. The round-1 baseline
summary for `three-things-bedrock-workload` contains this sentence, judged faithfulness=5,
zero hallucinations:

> "latency decomposition reveals that time-to-first-token accounts for significant delays beyond
> model generation speed"

The round-2 corrupted summary for the *same post* contains the *identical* sentence, unchanged,
sitting next to one injected fabrication (a made-up "45 minutes" figure). The judge's response this
time flags that same TTFT sentence as a hallucination too: *"mischaracterizes TTFT as causing
'significant delays beyond model generation speed' when the source shows TTFT is a component of
total latency, not something beyond it."*

Same claim, same source, same judge, same rubric, temperature 0. Different verdict, depending on
what else was in the summary next to it. That's not a hallucination-detection failure — the judge's
critique of the TTFT phrasing is arguably fair on a strict read either time. It's a **consistency**
failure: one plainly bad neighboring claim seems to have made the judge more skeptical of a claim it
had just passed cleanly on its own. A contagion effect, not a blind spot.

## Why this matters for evals in production

A per-item LLM-as-judge score is not a fixed property of the item. It moved for the same input
depending on what else the model was asked to grade in the same call. If you are using an LLM judge
to gate deploys or score a golden dataset, a single pass/fail number per item is not enough —
grade the same claim twice, in different company, before trusting the verdict enough to block a
release on it.

## Reproduce it

```bash
export AWS_PROFILE=<a profile with Bedrock access>
pip install boto3
python3 scripts/generate_summaries.py
python3 scripts/judge.py
python3 scripts/judge_calibration.py
```

Source posts are copied from [rajmurugan.com](https://rajmurugan.com)'s public blog content for
convenience; see `data/posts/`.

---

Companion write-up: [Do You Trust It? — Part 1](https://rajmurugan.com/blog/) (rajmurugan.com),
part of the *Do You Trust It?* series on evals for production AI.
