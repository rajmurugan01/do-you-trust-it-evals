---
title: "Part 1: Intent vs State. How AWS DevOps Agent Closes the Gap Between What Your System Is and What You Decided It Should Be"
description: "When something breaks at 3am, you look at logs, metrics, traces. You don't go and re-read the ADR your team wrote in January. AWS DevOps Agent does. Here's why that changes the first hour of an incident."
pubDate: 2026-04-30
tags: ["aws", "devopsagent", "mcp", "bedrock", "agentcore", "aiagents", "sre"]
pillars: ["agentic-ai", "enterprise-ai"]
series: "Building an AWS DevOps Agent that Knows Your Org"
seriesPart: 1
canonicalURL: "https://rajmurugan.com/blog/part-1-aws-devops-agent-intent-vs-state"
devToUrl: "https://dev.to/rajmurugan/part-1-intent-vs-state-how-aws-devops-agent-closes-the-gap-between-what-your-system-is-and-what-3epg"
readTime: 11
---

A few weeks ago I was at an AWS roundtable in Auckland. A dozen heads of platform around one table, every one of them shipping AI agents into production, every one of them describing the same gap.

Their agents could read AWS docs. They could call the AWS API. They could write Terraform. They could even, on a good day, propose a fix for a real incident.

What none of them could do: tell on-call whether the API exposure on the billing service is a regression, or a 30-day risk acceptance the team signed off on last quarter.

That gap is what this whole series is about.

I'll show you the AWS DevOps Agent setup I built to close it. The companion implementation is **Intent Guard**, a demo I'll publish alongside this series (anonymised; I can't share my employer's copy of it).

The thing that took me longest to internalise: **the hard part of an org-aware agent isn't the AI**. The hard part is figuring out where your org's actual decisions live, getting them in front of the model at the moment they matter, and giving the model enough metadata to know which ones still apply.

Let me start with the framing the rest of the series rests on.

---

## When something breaks at 3am, what do you actually look at?

![Three columns: I. Logs, metrics, traces: tell you what IS. II. ADRs, runbooks, planning docs: tell you what SHOULD BE. III. Nobody reads both in the first hour.](/images/blog/part-1/01-state-vs-intent.png)

There are two stacks of evidence about your system, and they get used very differently.

**The "what IS" stack.** Logs, metrics, traces, CloudTrail. This is the mature half. Every major incident tool (Datadog Watchdog, New Relic AI, PagerDuty AIOps) is excellent at this. They do anomaly detection, alert correlation, change attribution. By 2026 this is a solved-enough problem that the moment alerts fire, you have minutes of automated triage.

**The "what SHOULD BE" stack.** ADRs, runbooks, planning docs, incident write-ups, the meeting notes where someone agreed to defer the OAuth work until after launch. Your org wrote all this once. Then mostly nobody reads it again.

Here is the uncomfortable truth: **nobody reads both in the first hour.** The on-call pulls dashboards. They scroll logs. If they're senior, they ask in Slack: *"Did anything change?"* If nothing changed, they go deeper into the metrics.

What they almost never do, in hour one, is open the ADR repo and search for "circuit breaker" or "rate limit", because they don't have a reason to suspect the incident is about a decision the team made three months ago that quietly slipped past its deadline.

That's the gap. The worst incidents I've watched in the last few years weren't about recent changes. They were about decisions made months earlier that turned into debt while no alarm was watching.

If state is the mature half of the problem, **intent is the half that nobody has automated yet.**

---

## What changed when AWS DevOps Agent shipped

![AWS DevOps Agent. Autonomous incident response. Announced re:Invent 2025, GA March 2026. Runtime: Bedrock AgentCore + Claude. Triggered by CloudWatch, PagerDuty, Dynatrace, ServiceNow, or webhook. Reads telemetry, CloudTrail, code repos, and Knowledge Bases.](/images/blog/part-1/02-devops-agent-capability.png)

AWS DevOps Agent was announced at re:Invent 2025 and went GA on 31 March 2026. Underneath it, it runs on **Bedrock AgentCore** with Claude as the default model. From a builder's perspective, the interesting bits are:

- It accepts triggers from CloudWatch, PagerDuty, Dynatrace, ServiceNow, or any signed webhook.
- It runs an autonomous investigation across your telemetry, CloudTrail, code repos, and any Bedrock Knowledge Bases you've registered.
- It surfaces a finding with citations: log lines, trail events, KB document IDs.

The mental shift: it's not "another AIops tool". It's an SRE who has read every ADR you've ever written, and starts the runbook the moment your alarm fires.

That changes what's possible in hour one, but only if your org's decisions are actually *in* a Knowledge Base in a shape the agent can use. Most orgs' ADRs aren't. That's the work.

A useful way to position this against existing tools:

```
                 │ Reads                     │ Misses
─────────────────┼───────────────────────────┼─────────────────────────
AIops incumbents │ Telemetry — anomaly       │ Your ADRs.
(Watchdog, New   │ detection, alert          │ Your runbooks.
Relic AI,        │ correlation.              │ Your decisions.
PagerDuty AIOps) │                           │
─────────────────┼───────────────────────────┼─────────────────────────
DevOps Agent +   │ Telemetry plus your       │ —
curated KB       │ documented intent.        │
```

I want to be careful here: the "what IS" tools are mature and good. This isn't replacement. It's the layer they don't see.

---

## A concrete scenario: 60 days past a commitment

Generic framing only goes so far. Let me make this real with the demo I'll use throughout the series.

**Northwind Logistics** is a fictional B2B SaaS. (Customer is fictional. The architecture and incident shape are real, drawn from work I've done.) They run on AWS, ECS Fargate, RDS, App Runner. They have an internal feature called `northwind-quote` that turns a customer brief into a costed proposal. The magic happens in a `/tweak` endpoint that calls Bedrock synchronously to apply natural-language adjustments like *"swap to Nova Pro"* or *"reduce to 10M tokens/day"*.

The team shipped `northwind-quote` in **January 2026**. They knew the synchronous Bedrock call was a risk. They captured that risk in an ADR:

```yaml
---
type: adr
id: ADR-004
title: Synchronous Bedrock call in /tweak — temporary
date: 2026-01-12
status: accepted
service: northwind-quote
expires: 2026-03-01
---

# ADR-004: Synchronous Bedrock call in /tweak

## Status
ACCEPTED (TEMPORARY) — circuit breaker due 2026-03-01

## Context
Launch deadline. Need /tweak working. Bedrock throttling rare in
test traffic; we accept the risk for one sprint.

## Decision
Call Bedrock synchronously from the request path.
Add a circuit breaker + degraded mode by 2026-03-01.

## Risk Acceptance
- 30-50K req/day. Spikes Mon mornings.
- If Bedrock throttles, /tweak errors are user-visible.
- DO NOT silently extend.
```

Two things to notice in that frontmatter, because the entire system depends on them:

1. `expires: 2026-03-01`. This is a structured field, not a sentence buried in prose. A retrieval tool can filter on it.
2. `service: northwind-quote`. Also structured. Filterable. Joinable to telemetry.

**March 1 came and went.** Other priorities took over. The circuit breaker never shipped. No alarm watched the deadline. No ticket got auto-created. The ADR sat in a repo, exactly where the team filed it, perfectly accurate, completely unread.

**Today is April 30.** Bedrock has a regional hiccup. Users clicking *Apply tweak* start seeing 5xx errors. Someone pages on-call.

The on-call's hour-one question is the right one: *"What changed?"*

The honest answer to that question, and the one a generic agent can never produce, is:

> **Nothing changed in the code. Sixty days of elapsed time changed.**
>
> ADR-004 (filed Jan 12) accepted synchronous Bedrock as a risk and committed to a circuit breaker by 2026-03-01. The deadline passed without the work landing. Today's symptom is exactly the failure mode the ADR predicted.

That answer doesn't come from telemetry. It comes from the ADR repo. And it has to come within the first ten minutes of the incident, or it doesn't matter.

---

## The architecture, end-to-end

Here is the whole setup on one page:

![End-to-end architecture: triggers (CloudWatch / PagerDuty / Dynatrace / ServiceNow) feed an HMAC-signed webhook forwarder (Lambda + Secrets Manager), which calls AWS DevOps Agent (Bedrock AgentCore + Claude, AgentSpace + Operator Web App). The agent reads state from AWS APIs (CloudWatch, CloudTrail, App Runner, Lambda) and intent from a custom MCP server (Lambda Function URL exposing four tools), which retrieves from a Bedrock Knowledge Base (Titan Embeddings V2 + S3 corpus of ADRs, runbooks, incidents, planning, architecture).](/images/blog/part-1/04-architecture.svg)

Five things that earned their place on this diagram, because they are the load-bearing decisions:

**1. The agent runtime is AWS DevOps Agent on Bedrock AgentCore, not raw Bedrock Agent.** I get AgentSpace (a per-operator session container), a built-in Operator Web App so I'm not shipping a frontend at 3am, native trigger inputs from CloudWatch/PagerDuty/Dynatrace/ServiceNow, and the AgentCore runtime properties (long-running sessions, JWT-validated invocations, streaming) underneath. For a system meant to be used by SREs under stress, "I don't ship a frontend" is not a small win.

**2. Triggers go through a signed webhook forwarder, not directly into the agent.** Lambda + HMAC-SHA256 + a secret in Secrets Manager. This sounds like over-engineering for a demo and is exactly right for production: every alarm source has a different payload shape, and you want one place to normalise + sign before the agent ever sees it. Replay attacks against agent endpoints are not theoretical.

**3. The org context lives behind a custom MCP server, not in the system prompt.** First instinct, when you start, is to paste your ADRs into the model's context. That falls apart inside a week: context bloats, costs rise, decisions go stale the moment they change, and you cannot filter. An MCP lets the agent decide *when* to retrieve, *what* to filter on, and pay the token cost only when the question is actually about org context. I'll go deep on the four tools in Part 2.

**4. Frontmatter is the contract, not the prose.** The MCP tools don't return whole documents. They return chunks filtered by `type`, `service`, `expires`, `signals`: fields I control via YAML frontmatter on every doc. That's why `check_risk_acceptance_status` can ask *"give me every ADR for `northwind-quote` where `expires` is in the past"* without the LLM having to parse free text.

**5. The agent reads both halves.** It pulls App Runner logs and CloudTrail for state, and queries the KB through MCP for intent, and *correlates them in the same turn*. That correlation (log line + ADR ID + elapsed-days math) is what produces an answer the on-call could not have produced from either side alone.

Two practical properties of this design worth calling out, because they are the things I get asked about every time I demo it:

> **Wrong citations are visible, not silent.** The agent quotes specific log lines and specific ADR IDs in its finding. If retrieval brings back the wrong document, you can see it on the screen. The failure mode I've actually hit is *stale KB content* (an ADR that should have been updated and wasn't), not invention.
>
> **Auto-remediation is not in this flow.** The agent surfaces a structured recommendation. A human runs it. You can wire it to automation later if you want. For incident response, the human-in-the-loop boundary is where I want it.

---

## What you'll build across this series

| Part | What you'll build |
|---|---|
| **Part 1** (this post) | The Intent-vs-State framing and the system architecture |
| **Part 2** | The MCP layer: turning ADRs, runbooks, and incidents into a queryable org-knowledge surface, with frontmatter as the contract |
| **Part 3** | Wiring it into AWS DevOps Agent: webhook forwarder, AgentSpace, register-service, IAM, and the gotchas I hit |

By the end of Part 3, the on-call from the Northwind scenario does not type anything. The CloudWatch alarm fires. Two minutes later there's a finding in the channel, citing ADR-004 and the App Runner log line that triggered it, with the recommendation pulled directly from the ADR's break-glass section: flip the SSM parameter that puts `/tweak` into degraded mode. Sixty days of unread decision turns into a two-minute hour-one answer.

That's the bar.

---

## A short FAQ before we go deeper

The same handful of questions came up at the roundtable and most rooms I've shown this in. Here's the short version. Parts 2 and 3 fill in the detail.

![Things worth citing if asked. Cost: under $50/mo for a demo account. Data privacy: inference runs in your AWS account via Bedrock; KB data stays in your S3. Auto-remediation: not in this flow. Agent surfaces a structured recommendation, a human runs it. Hallucinations: agent quotes log lines and ADR IDs, wrong citation is visible not silent; failure mode has been stale KB content, not invention. Non-AWS systems: agent runs on AWS but can ingest external data sources. Fallback: Q&A cheatsheet on a second screen for live demos.](/images/blog/part-1/03-things-worth-citing.png)

## What I deliberately left out

**Skills.** The first version of this build had no Claude/agent Skills layer. ADRs and runbooks are doing all the work for now. The natural next layer is process (escalation order, ticketing etiquette, "always page X before Y"), and Skills are how I'd encode that. I'll write about Skills once I've actually shipped that layer in anger, not before.

**Multi-account org graph.** A real org has dozens of AWS accounts. The version I'm describing here is single-account on purpose so the moving parts are visible. The pattern generalises and I'll come back to it.

**Eval harness.** The agent's answers are good enough to demo and to surface the right ADR roughly nine times in ten on the question shapes I tested. That is *not* the same as good enough to trust unattended. Evals are a separate post.

**Cost.** Under fifty dollars a month for a demo account with light traffic. Real numbers depend on investigation volume and KB size. I'll benchmark properly in a later post; cost was not the bottleneck for me.

---

In Part 2 we get into the MCP server itself. Four tools, four KB filters, and the small but load-bearing decision to parse YAML frontmatter inside the retrieval client rather than in the agent's prompt. That decision is the difference between an agent that *reads your docs* and an agent that *knows your org*.

→ **Continue to Part 2: The MCP Server. Turning ADRs and incidents into a queryable org-knowledge surface** *(coming this week)*
