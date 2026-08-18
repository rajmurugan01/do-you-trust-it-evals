---
title: "Part 6: Cost & Performance for Bedrock AgentCore: Prompt Caching, Model Selection, and CloudWatch Alarms"
description: "Real cost breakdown of running an AgentCore agent: prompt caching savings, when to use Nova Pro vs Claude Sonnet, PriceClass_100, idle timeouts, and how to set alarms before your bill surprises you."
pubDate: 2025-11-09
tags: ["aws", "bedrock", "agentcore", "cost", "performance", "cloudwatch"]
pillars: ["agentic-ai", "ai-operations"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 6
canonicalURL: "https://rajmurugan.com/blog/part-6-cost-performance-prompt-caching"
devToUrl: "https://dev.to/rajmurugan/part-6-cost-performance-for-bedrock-agentcore-prompt-caching-model-selection-and-cloudwatch-282k"
readTime: 9
---

You've deployed the agent. It works. Now let's make sure it doesn't cost you a surprise at the end of the month.

This is the part that most tutorials skip. Real production systems need cost visibility before incidents, not after. Here's everything I've done to keep costs predictable and to save money where it counts.

---

## The cost components

An AgentCore deployment has several cost drivers:

| Component | Pricing model |
|---|---|
| Bedrock model invocations | Per token (input + output) |
| AgentCore Runtime | Per container-hour (when active) |
| AgentCore Memory | Per memory operation |
| ECR | Per GB stored + data transfer |
| CloudWatch Logs | Per GB ingested |
| S3 (if used) | Negligible for this setup |

The dominant cost is almost always **Bedrock model invocations**. Everything else is small by comparison.

---

## Prompt caching: the biggest lever

If you haven't read Part 3 carefully, go back and re-read the prompt caching section. It's the highest-impact optimisation in the system.

Quick recap: setting `cache_prompt="default"` on the Strands `BedrockModel` inserts a `cachePoint` after your system prompt, so Bedrock caches those tokens and charges the cache read price on subsequent calls.

For Claude Sonnet 4.5:
- Cache write: $3.00 / 1M input tokens
- Cache read: **$0.30 / 1M input tokens** (10x cheaper)
- Output tokens: $15.00 / 1M output tokens (not cached)

For a 1,500-token system prompt:

| Scenario | Cost per turn |
|---|---|
| Without caching | $0.0045 (system prompt) + output tokens |
| With caching (turns 2+) | $0.00045 (system prompt) + output tokens |
| **Saving per turn** | **~$0.004** |

That sounds small. Scale it:
- 100 users × 10 conversations/day × 5 turns each = 5,000 turns/day
- 4,000 of those turns are turns 2+ (caching applies)
- Saving: 4,000 × $0.004 = **$16/day → $480/month on system prompt tokens alone**

The saving scales linearly with session depth and volume.

**Enable prompt caching:**
```python
primary_model = BedrockModel(
    model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
    cache_prompt="default",   # cachePoint at the end of the system block
    cache_tools="default",    # cachePoint inside toolConfig.tools
)
```

---

## Model selection strategy

Not every task needs Claude Sonnet 4.5. Using the right model for each task type dramatically reduces costs.

| Task | Recommended model | Reason |
|---|---|---|
| Main conversation | Claude Sonnet 4.5 | Best reasoning, multi-turn, complex tool use |
| Intent classification | Amazon Nova Pro | Simple classification, ~15x cheaper |
| Session summarisation | Amazon Nova Pro | Structured output, no complex reasoning needed |
| FAQ matching | Amazon Nova Pro or embedding model | Simple retrieval pattern |
| Billing dispute analysis | Claude Sonnet 4.5 | Complex reasoning required |

Current pricing comparison (us-east-1):

| Model | Input ($/1M) | Output ($/1M) |
|---|---|---|
| Claude Sonnet 4.5 | $3.00 | $15.00 |
| Amazon Nova Pro | $0.80 | $3.20 |
| Amazon Nova Lite | $0.06 | $0.24 |

For a classification task that returns 1-2 tokens and processes 500 input tokens:
- Claude Sonnet 4.5: $0.0015 per call
- Amazon Nova Pro: $0.0004 per call
- **Saving: ~75% just by routing to the right model**

In `agent.py`, the Nova model is available alongside the primary model:

```python
nova_model = BedrockModel(
    model_id="amazon.nova-pro-v1:0",
    boto_config=boto_config,
)
```

Use it when you need a cheap background task before or after the main conversation.

---

## AgentCore lifecycle configuration

AgentCore has two lifecycle settings that affect cost:

**Idle timeout** (`IdleTimeoutInSeconds`): how long AgentCore waits before pausing a container instance after the last request. Set in the CDK stack:

```typescript
LifecycleConfiguration: {
  IdleTimeoutInSeconds: 900,       // 15 minutes
  MaxSessionDurationInSeconds: 28800, // 8 hours
}
```

- Lower idle timeout = containers paused sooner = lower cost for bursty workloads
- Higher idle timeout = containers stay warm longer = better latency for returning users
- The sweet spot depends on your session gap pattern. 15 minutes is a reasonable default.

**Max session duration**: the hard limit per session. 8 hours is appropriate for a long-running assistant. For short transactional interactions, you could reduce this.

---

## CloudFront PriceClass_100

For the blog/portfolio site, using `PriceClass.PRICE_CLASS_100` restricts CloudFront distribution to US and European edge locations only. This cuts CF cost by ~50% compared to the global price class.

For a personal portfolio with mostly English-speaking traffic, the 95th percentile of users are in the US and Europe anyway.

```typescript
// infra/lib/hosting-stack.ts
priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
```

For the AgentCore endpoint itself, there's no CloudFront in front. AgentCore is a regional service.

---

## CloudWatch alarms: catch runaway costs before they hit your bill

Two alarms are critical for an AgentCore deployment.

### Alarm 1: OutputTokenCount spike

An agentic loop that gets stuck (tool keeps failing, model keeps retrying) can generate thousands of output tokens per minute. This alarm fires when output tokens per 5 minutes exceed a threshold:

```typescript
new cloudwatch.Alarm(this, 'OutputTokenAlarm', {
  alarmName: `customerServiceAgent-OutputTokenCount-dev`,
  metric: new cloudwatch.Metric({
    namespace: 'AWS/Bedrock',
    metricName: 'OutputTokenCount',
    dimensionsMap: { ModelId: 'anthropic.claude-sonnet-4-5-20250929-v1:0' },
    statistic: 'Sum',
    period: cdk.Duration.minutes(5),
  }),
  threshold: 50_000,    // Tune to your expected usage
  evaluationPeriods: 2,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
});
```

Set the threshold to 2-3x your normal peak. Monitor for a week after launch to establish a baseline, then tune.

### Alarm 2: InvocationLatency P99

High P99 latency indicates your agent is taking too long, possibly waiting on a tool timeout, or the model is iterating excessively:

```typescript
new cloudwatch.Alarm(this, 'LatencyAlarm', {
  metric: new cloudwatch.Metric({
    namespace: 'AWS/Bedrock',
    metricName: 'InvocationLatency',
    statistic: 'p99',
    period: cdk.Duration.minutes(5),
  }),
  threshold: 30_000,   // 30 seconds
  evaluationPeriods: 3,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
});
```

Both alarms publish to the SNS topic (also in the CDK stack), which sends you an email. For production, replace email with a PagerDuty or Slack notification via SNS → Lambda → webhook.

---

## Actual cost estimates

For a moderately used customer service agent at ~500 conversations/day, 5 turns each:

| Component | Monthly estimate |
|---|---|
| Bedrock (Claude Sonnet 4.5, with caching) | $120-180 |
| Bedrock (Nova Pro for classification) | $5-10 |
| AgentCore Runtime | $15-30 (depends on idle config) |
| AgentCore Memory operations | $5-10 |
| ECR storage | $1-2 |
| CloudWatch Logs | $3-5 |
| **Total** | **~$150-240/month** |

Without prompt caching: add ~$60-80/month to the Bedrock line.

Without the dual-model strategy (Claude Sonnet 4.5 for everything): add ~$20-30/month to the Bedrock line.

These numbers will vary significantly based on your conversation length and output token counts. The alarms will tell you when something is outside the expected range.

---

## Quick optimisation checklist

Before going to production:

- [ ] Prompt caching enabled (`cache_prompt="default"` on the BedrockModel)
- [ ] Tool registry cached on Anthropic-family models (`cache_tools="default"`)
- [ ] Nova Pro used for background tasks (not Claude for everything)
- [ ] Idle timeout set appropriately (900s is a good default)
- [ ] OutputTokenCount alarm configured and tested
- [ ] InvocationLatency alarm configured and tested
- [ ] SNS topic with email subscription (or PagerDuty) set up
- [ ] CloudFront PriceClass_100 set (blog site)
- [ ] Model invocation logging enabled (for debugging cost spikes)

---

## Wrapping up the series

Over 6 parts, we built a complete production AI agent on AWS:

1. **Part 1**: Why AgentCore: the Lambda limitations and what AgentCore solves
2. **Part 2**: CDK infrastructure: the full stack + 9 gotchas documented
3. **Part 3**: The Python agent: Strands SDK, prompt caching, AgentCore Memory
4. **Part 4**: Local dev loop: Docker, platform flags, .env pattern
5. **Part 5**: CI/CD: GitHub Actions OIDC, ECR dual-tag strategy, Runtime updates
6. **Part 6** (this post): Cost and performance: prompt caching savings, model selection, alarms

The full demo repo is at **[github.com/rajmurugan01/bedrock-agentcore-starter](https://github.com/rajmurugan01/bedrock-agentcore-starter)**. Every pattern in this series maps to real code in that repo.

If this series saved you some debugging time (or a surprise AWS bill), star the repo and share it. If I got something wrong or you've found a better pattern, open an issue. I'll update the posts.

← **[Back to Part 5: CI/CD with GitHub Actions OIDC](/blog/part-5-cicd-github-actions-oidc)**
