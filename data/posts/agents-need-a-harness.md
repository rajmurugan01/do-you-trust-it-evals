---
title: "Every dashboard was green while the agent burned six figures a year"
description: "The most expensive AI agent failures don't throw an error, they hide. One ran at a six-figure-a-year rate for days while every dashboard stayed green, because the signals that catch it, per-session cost and anomalies, are the ones nobody watches. Why agent loops run away, and the two cost instruments your monitoring is missing."
pubDate: 2026-07-07
tags: ["ai-operations", "agents", "agentcore", "bedrock", "finops", "observability", "reliability", "strands"]
pillars: ["ai-operations"]
format: "Pattern"
series: "Production AI, Honestly"
seriesPart: 3
canonicalURL: "https://rajmurugan.com/blog/agents-need-a-harness"
readTime: 6
coverImage: "/images/blog/2026-07-07_agents-need-a-harness.png"
coverImageAlt: "Every dashboard was green while the agent burned six figures a year. A stuck AI agent ran at a six-figure-a-year cost rate for days while every operational dashboard stayed healthy, because per-session cost is the signal traditional monitoring never watches."
draft: false
---

The most expensive failure I've seen from an AI agent didn't throw a single error. No 5xx, no failed health check, no red tick in a channel. For days it ran at a rate that annualised into six figures, and every dashboard we had was green the whole time.

That's the part worth sitting with. Not that an agent can be expensive, everyone knows that. That it can be expensive and invisible at the same time.

I've spent this year operating LLM agents on AWS Bedrock, with real users at the other end, and this is the failure mode I'd want every team shipping agents to design against *before* they meet it. I'm not going to name the company, and I've changed the numbers to protect them. The shape is what matters, and the shape is real.

## Your dashboards answer the wrong question

Traditional monitoring answers one question: is it up? HTTP 200 on every call. p99 latency, normal. CPU and memory, fine. Error rate, zero. Uptime, 100%. If you'd paged the on-call engineer, they'd have glanced at the dashboards and gone back to bed.

None of those metrics has an opinion on the question that actually mattered: is the agent quietly doing something insane? The signals that *were* moving, tokens per turn, dollars per hour, tool calls per request, were on nobody's dashboard. They're not on the default Datadog view, they're not what your APM watches, and they're not what wakes anyone up.

So the agent returned a perfect 200 every single time, all the way to the poorhouse.

![The blind spot: the pipeline looks healthy the whole way through. Prompt goes into the agent loop (think, act, observe), which returns 200 OK on every call, but it never exits. Underneath, invisibly: the loop keeps going, so it re-bills the model, so cost climbs hour by hour, while dashboards stay green the whole time. Healthy on every dashboard, except the bill.](/images/blog/2026-07-07_the-blind-spot.png)

## What it was doing

An agent loop that wouldn't terminate. You give it a question, it loops, think, act, observe, until it decides it's done. On one input it never decided it was done. It kept working the problem, re-invoking the model every lap, and every lap was another charge. No exception, because nothing was broken. It was doing exactly what it was told, keep going until you're satisfied, and it never got satisfied.

It was found days later, on the finance bill. Not by anyone watching the system, because the thing worth watching was not on a screen. And to be honest about the number: caught after days, at that rate, the actual damage was a few thousand dollars, not the six figures a year it was trending towards. But nothing was designed to catch it. Someone happened to read the bill. It was luck that it was day nine and not day ninety, and luck is not a control.

## It doesn't loop at random

This is the part most write-ups skip, and it's the part that makes the failure feel less like a freak accident and more like something you can design against. Agents terminate fine, almost always. A runaway needs a specific condition to defeat the exit. Five of them keep showing up.

![Why agents run away: agents finish fine, but a runaway needs one of five conditions to defeat the exit. Context bloat (tool results balloon the context until the model loses the thread and keeps calling). Unsatisfiable goal (the task can't be finished, so the model keeps trying). Model regeneration (malformed output re-invoked, the same reasoning again). A broken or looping tool (a tool keeps failing, the agent keeps calling it). A poison-pill re-drive (a crash before "done", and a retrier that never gives up). Any of them: every lap re-bills the model, and every call still returns a 200. The exit was a judgement call, so give it a hard bound.](/images/blog/2026-07-07_why-agents-run-away.png)

- **Context bloat.** Tool results, often thousands of characters each, pile up until the context is so large the model loses the thread and keeps calling tools without noticing it already has the answer. This is the one teams hit most.
- **Unsatisfiable goal.** The input pushes the model toward a stop condition it can't reach, so it keeps acting. A "how did you actually verify that?" follow-up can send an agent into hundreds of retrieval and search calls, trying to be sure of something it can never be fully sure of.
- **Model regeneration.** The model returns malformed or empty output, the SDK re-invokes it with the same context, and it emits the identical reasoning again. A loop inside the model, not the tools.
- **A broken or oscillating tool.** A tool keeps failing, or two tools quietly undo each other, and the agent keeps calling with no check that it's making progress.
- **A poison-pill re-drive.** This one isn't the agent at all. Something outside it re-invokes the work (a queue, a scheduler, a retry) and the work crashes before it's marked done. It never reaches a terminal state, so the re-driver picks it up again next tick and re-bills the model, forever.

None of these is "someone forgot an exit". The exit was there. It was a judgement call, the model's or the happy path's, and a rare input defeated it. That's why it passes every normal test and only bites in production, on the one weird input.

## Stopping it is the easy half

There's a word going around for the fix, a harness, and it's a good one, though you'll see it in a lot of places now. The stopping half is table stakes: put a hard bound on the loop so it can't run away regardless of what the model decides. Cap the iterations, and because "iteration" means more than one thing, cap more than one: tool calls, reasoning restarts, and wall-clock, per turn, tripping on whichever comes first.

![Bound the loop: cap more than one thing and trip on whichever comes first. Three tripwires, tool calls (max 12 per turn), reasoning restarts (max 6, which catches the no-tool regeneration loop), and wall-clock (max 120 seconds per turn), all converge on one action: stop and hand back what you have. Better still, a per-session token budget gives a hard dollar ceiling per request. One cap breeds false confidence, so bound more than one.](/images/blog/2026-07-07_bound-the-loop.png)

```python
def loop_guard_tripped(tool_calls, elapsed_s, reasoning_blocks,
                       max_tool_calls=12, max_seconds=120, max_reasoning=6):
    if tool_calls > max_tool_calls:
        return "tool_calls"
    if reasoning_blocks > max_reasoning:   # catches the no-tool regeneration loop
        return "reasoning"
    if elapsed_s > max_seconds:
        return "elapsed"
    return None
```

One caveat on that reasoning cap: it only helps if your instrumentation actually surfaces SDK regenerations as separate blocks within the turn. Many SDKs don't by default, so check that the counter can see them before you rely on it.

Set the ceilings from your own traffic, above the heaviest *legitimate* turn, so the guard only ever fires on genuine runaways. When it trips, stop and hand back what you have. Better still, bound the money directly: a hard per-session token budget gives you a deterministic dollar ceiling per request, so the worst case is a known number rather than an open-ended one. And for anything with a retry (a queue, a poller, a sweeper), give it a give-up: a counter that flips the work to a terminal state after N attempts, so a stuck item costs a dollar instead of a runaway.

Here's the trap, though. Any competent engineer can add a max-iterations cap, which is exactly why people ship one and stop. A single cap breeds false confidence. It doesn't touch the poison-pill re-drive, which lives outside the loop entirely, and it does nothing about an agent that stays inside every limit while quietly getting more expensive per answer. The bound stops the acute bleed. It does nothing to help you *see* the next one coming.

## Seeing it is the half that matters

The reason the runaway lived for days isn't that it lacked a bound. It's that the signals that would have caught it were on nobody's dashboard. So put them there. And it takes two, because there are two different failures to catch, and the instrument for one is blind to the other.

![Watch the spend two ways: two failures, two instruments, and one is blind to the other. Per session, emitted every cycle, catches the single-session loop: tool calls and tokens per session against a p99, because one stuck session is invisible in the fleet total but a klaxon against one normal session. Fleet token-rate catches what no single session shows: the diffuse regression, and the poison-pill re-drive smeared across many cheap sessions. Alarm on the raw counts, not synthesised dollars, which lie under prompt caching. Point it at a channel a human reads: an alarm with no subscriber is a diary entry.](/images/blog/2026-07-07_watch-the-spend.png)

The acute runaway is a per-session problem, so watch it per session. Tool calls per session against a p99, tokens per session against that budget: one bad session spikes these immediately, and it stands out precisely because you're comparing it against a single normal session, not burying it in the fleet total. This is the signal that would have caught my story in minutes instead of days. A single stuck session adding twenty-odd dollars an hour is invisible against a whole fleet's gross spend. Against the profile of one normal session, it's a klaxon. If you reach for a single instrument, reach for this one.

Bedrock won't hand you that per-session number, though: its stock CloudWatch metrics are dimensioned by model, not by session. So emit it yourself, and emit it from inside the event loop, on every cycle, from the same instruction that increments the loop guard's counter. Not per session and not per turn: a turn only ends when the model returns `end_turn`, and the regeneration loop and the in-turn tool storm are exactly the turns that never do. The loop body is the only code guaranteed to run during a runaway, so that is where the emit has to live. Anywhere higher and you are back to a green dashboard.

```python
# Inside the event loop, every cycle — beside the loop guard's counter.
tool_calls_this_session += used_a_tool
tokens_this_session     += cycle_in_tokens + cycle_out_tokens

# Emit as EMF (a structured log line), not PutMetricData. Each instance only
# sees its own sessions, and PutMetricData throttles under a high-frequency
# runaway, exactly when you need the signal. CloudWatch builds the metric from
# the lines; Contributor Insights ranks the worst session across every instance.
emit_emf({"session_id": sid,
          "ToolCallsPerSession": tool_calls_this_session,
          "TokensPerSession":    tokens_this_session})

# Alarm on the raw counts, not synthesised dollars. Tokens-to-dollars is wrong
# under prompt caching (a re-drive loop is mostly cache reads) and meaningless
# under provisioned throughput. Counts are the un-launderable signal; keep the
# dollars for the write-up and the CFO.
```

The threshold on top is fragile, and it is worth saying why it is fine anyway. Agentic load is bimodal, quick answers and legitimate long research runs, so any static p99 either misses a slow runaway or pages on a real heavy session. That is tolerable because the alarm is not the backstop; the hard cap from the stopping half is. The alarm's only job is to make a silent trip loud: the cap fired, or a legal session is running hot, and either way a human should see it. Stopping silently isn't seeing.

The second instrument watches the fleet, and it catches two failures the per-session alarm is blind to. One is the slow bleed: no single session misbehaves, every cycle stays inside every cap, but the whole fleet is quietly getting more expensive per answer, a prompt that grew, a retrieval that got chattier, a model swap nobody costed. The other is your own fifth trigger, the poison-pill re-drive: something external keeps resubmitting the failing request, and if each attempt mints a fresh session id, the runaway is smeared across N short, cheap sessions that each look normal. Per-session cost stays flat on every one. Only the aggregate climbs. So the honest mapping is not fast-versus-slow: per-session catches the single-session loop, and the fleet catches the diffuse regression and the distributed re-drive both.

At the fleet level the per-model aggregate is exactly the right lens, because that is where both of those show up. Bedrock emits `InputTokenCount` and `OutputTokenCount` per model, so track the token rate per model and alarm when it drifts above your steady-state ceiling. Translate to dollars for the humans, but price it honestly or not at all:

```python
# Per model: m1 = InputTokenCount, m2 = OutputTokenCount  (Sum, 1-hour period).
# If prompt caching is on, split out cache-read tokens — they bill near ~10%,
# and a re-drive loop is mostly cache reads, so folding them in at full price
# overstates the rate in the exact case you're trying to catch.
in_rate  = m1 / 1e6         # millions of input tokens/hour  — the raw trigger
out_rate = m2 / 1e6         # millions of output tokens/hour

# Alarm on the token rate; convert to dollars only for the write-up.
# rate = (in_rate * in_price) + (out_rate * out_price), summed across models.
# Fire when the token rate holds above your steady-state ceiling for 3 of 3 hours.
```

Set the ceiling from a normal week, not zero, so the alarm fires on a regression and not on healthy growth. When it trips, the question to ask is "what got more expensive per answer since last week", and the usual answer is a prompt, a retrieval step, or a model swap that shipped without anyone costing it.

Two instruments, two jobs, though not the tidy fast-and-slow split it looks like. Per-cycle counts catch the single-session loop the moment one session runs hot. Fleet token-rate catches what no single session reveals: the diffuse regression, and the re-drive storm spread thin across many cheap ones. And whichever fires, point it at a channel a human actually reads, because an alarm with no subscriber is a diary entry.

## The takeaway

You can't prevent every weird input. A model-judged exit will occasionally meet an input it can never satisfy, and one of the five triggers will catch you eventually. What you *can* decide is whether the next one shows up in minutes, on a per-session alarm you wired up on purpose, or in six months, on a bill someone happens to read.

Bound the loop so it can't bankrupt you. Then watch the spend two ways, per cycle for the loop you can catch one session at a time, and per fleet for the ones you can't, because your uptime dashboards won't do either.

---

*I write about operating AI in production: one lesson a week. If this was useful, the rest of the series is on [rajmurugan.com](https://rajmurugan.com/blog).*

**Next in _Production AI, Honestly_:** [Part 4: the AgentCore Memory write that returns success and reads back empty](/blog/agentcore-memory-read-after-write). The gap between a 201 and a record a reader can actually find.
