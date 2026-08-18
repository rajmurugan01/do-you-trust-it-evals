---
title: "Part 1: Why I Chose Amazon Bedrock AgentCore (And What Lambda Gets Wrong for AI Agents)"
description: "Before writing a single line of agent code, I spent a week figuring out where to run it. Here's the architecture decision that changed everything, and the Lambda limitations that forced my hand."
pubDate: 2025-10-20
tags: ["aws", "bedrock", "agentcore", "aiagents", "architecture"]
pillars: ["agentic-ai"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 1
canonicalURL: "https://rajmurugan.com/blog/part-1-why-bedrock-agentcore"
devToUrl: "https://dev.to/rajmurugan/part-1-why-i-chose-amazon-bedrock-agentcore-and-what-lambda-gets-wrong-for-ai-agents-jm3"
readTime: 9
---

I built a production AI agent on AWS. Not a demo, not a proof of concept, but a real system with persistent memory, guardrails, CI/CD pipelines, and users who depend on it not going down at 2am.

The thing nobody tells you: **the hard part isn't the AI**. The hard part is the infrastructure around it.

This series is my attempt to document everything I had to figure out the hard way, from the architecture decisions in Part 1 all the way to cost optimisation in Part 6. The companion demo repo is at [github.com/rajmurugan01/bedrock-agentcore-starter](https://github.com/rajmurugan01/bedrock-agentcore-starter).

Let's start at the beginning: why Amazon Bedrock AgentCore, and why not the "obvious" serverless approach.

---

## The obvious approach: Lambda + Bedrock

If you've shipped anything serverless on AWS, your first instinct is Lambda. You know it, it has great tooling, CDK support is mature, and it scales to zero.

For a simple Bedrock wrapper (get a message, call `InvokeModel`, return a response), Lambda is fine. But the moment you add **conversational state**, it starts to crack.

Here's what a real conversational AI agent needs:

1. **Session state**: the agent needs to remember what happened earlier in the conversation
2. **Long-running processing**: LLMs can take 30-90 seconds for complex multi-tool chains
3. **Memory across sessions**: the agent should know who the user is from previous conversations
4. **Streaming responses**: users expect tokens to appear progressively, not wait 60 seconds for a blob

Let's look at how Lambda handles each of these.

### Problem 1: Lambda's 15-minute timeout

Lambda has a hard maximum execution timeout of 15 minutes. For a simple Q&A, that's fine. But for an agentic loop, where the model calls tools, processes results, calls more tools, and reasons over everything, you can easily hit 5-10 minutes per complex interaction.

And I haven't even mentioned the user's session. If a user comes back after 20 minutes and continues the conversation, that's a new Lambda invocation with zero context.

### Problem 2: Session state storage

Lambda is stateless by design. Every invocation is independent. For conversational state, you need to:

1. Store session state somewhere (DynamoDB, ElastiCache, S3)
2. Load it at the start of every Lambda invocation
3. Save it at the end of every invocation
4. Handle the edge case where the Lambda times out mid-session
5. Build a session expiry and cleanup mechanism
6. Handle concurrent invocations for the same session

That's a lot of undifferentiated infrastructure for a problem that isn't your core business.

### Problem 3: Cross-session memory

Beyond session state, real assistants need **memory**: the ability to remember that a user's preferred contact method is email, that they're a premium customer, that they had a billing dispute last month.

With Lambda, you'd need to build this yourself: a vector database for semantic recall, a summarisation pipeline to consolidate old sessions, a retrieval step before each invocation. Entirely custom, entirely your problem to maintain.

### Problem 4: VPC cold starts

If your agent needs to call internal services (internal APIs, private databases, internal tooling), Lambda needs to run inside a VPC. Lambda's VPC cold start used to be genuinely painful (15-30 seconds). It improved significantly, but it's still non-zero, and it becomes particularly noticeable when users have been idle for a few minutes.

---

## What AgentCore actually does

Amazon Bedrock AgentCore is AWS's managed infrastructure for running AI agents. Released in 2025, it's designed specifically for the workload pattern that Lambda handles poorly.

Here's the mental model: AgentCore is **a managed container orchestrator for long-running, stateful AI agent sessions**. You ship a Docker container with your agent code. AgentCore handles:

- **Container lifecycle**: starts, stops, scales, and restarts containers
- **Session routing**: routes each user session to the right container instance
- **Memory persistence**: built-in Semantic, Summary, and UserPreference memory strategies
- **JWT validation**: validates Cognito (or custom) JWTs before your code even runs
- **VPC networking**: runs your containers inside your VPC without cold start penalties
- **SSE streaming**: handles the HTTP connection and SSE protocol for you

The architectural difference is significant:

```
Lambda approach:
  User message → API Gateway → Lambda (cold start?) → load session from DynamoDB →
  call Bedrock → save session to DynamoDB → return response → Lambda exits

AgentCore approach:
  User message → AgentCore Runtime (JWT validated) → your container (already warm) →
  call Bedrock → response streams back → container stays warm for next message
```

---

## The key trade-offs

**AgentCore is not free.** Unlike Lambda, you pay for container runtime even when idle (though the idle timeout pauses execution). For high-volume workloads, the maths shifts. But for the workload pattern of multi-turn AI agent sessions with memory, it's dramatically simpler and often cheaper overall once you factor in the DynamoDB and custom session management you'd otherwise build.

**AgentCore is newer.** CDK support exists via `CfnRuntime` and `CfnMemory` constructs, but some things need raw `CfnResource` calls (more on this in Part 2). The developer experience is rougher than Lambda's.

**AgentCore abstracts session management.** You don't write session routing logic. This is mostly good, but it means you need to understand how AgentCore thinks about sessions (actor IDs, session IDs, lifecycle events) rather than building your own.

---

## The architecture we're building

Here's the full architecture for the Customer Service Agent in this series:

```
┌────────────────────────────────────────────────────────────────┐
│  GitHub Actions (OIDC)                                         │
│  ├── Build Docker (linux/amd64)                                │
│  ├── Push to ECR (:latest + :<sha>)                           │
│  └── update-agent-runtime CLI                                  │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                    CDK v2 TypeScript deploys:
                               │
┌──────────────────────────────▼─────────────────────────────────┐
│  AWS Infrastructure (us-east-1)                                │
│                                                                │
│  AgentCore Runtime                                             │
│  ├── Cognito JWT authoriser (validates tokens before invoke)  │
│  ├── AG-UI HTTP protocol (SSE streaming)                      │
│  ├── Container: your Python agent on port 8080                │
│  ├── Lifecycle: 15min idle timeout, 8hr max session           │
│  └── Network: VPC private subnets, restricted SG              │
│                                                                │
│  AgentCore Memory                                              │
│  ├── Semantic strategy (facts + user profile)                 │
│  ├── Summary strategy (session history)                       │
│  └── UserPreference strategy (interaction style)             │
│                                                                │
│  Bedrock Guardrail                                             │
│  ├── PROMPT_ATTACK protection (HIGH)                          │
│  ├── PII anonymisation (email, phone)                         │
│  └── Harmful content filtering                                │
│                                                                │
│  Supporting resources                                          │
│  ├── CloudWatch alarms (token count + latency)                │
│  ├── SNS cost alert topic                                      │
│  ├── KMS key (key rotation enabled)                           │
│  └── SSM Parameters (all ARNs/IDs exported)                  │
└────────────────────────────────────────────────────────────────┘
```

**Primary model**: Claude Sonnet 4.5 with prompt caching + guardrails
**Background model**: Amazon Nova Pro (for cheap classification/summarisation)
**CI/CD**: GitHub Actions OIDC, no stored AWS credentials anywhere

---

## What you'll learn in this series

| Part | What you'll build |
|---|---|
| **Part 1** (this post) | Architecture decisions + why AgentCore |
| **[Part 2](/blog/part-2-cdk-infrastructure-bedrock-agentcore)** | Full CDK stack + every deployment gotcha |
| **[Part 3](/blog/part-3-strands-agent-sdk)** | Python agent with Strands SDK, prompt caching, memory |
| **[Part 4](/blog/part-4-local-dev-docker)** | Docker dev loop, .env pattern, curl testing |
| **[Part 5](/blog/part-5-cicd-github-actions-oidc)** | GitHub Actions OIDC, ECR push, Runtime updates |
| **[Part 6](/blog/part-6-cost-performance-prompt-caching)** | Cost breakdown, prompt caching savings, alarms |

The full demo repo is at [github.com/rajmurugan01/bedrock-agentcore-starter](https://github.com/rajmurugan01/bedrock-agentcore-starter). Every concept in this series maps to real code in that repo.

In Part 2, we get into the CDK stack. There are 9 specific gotchas that will cost you hours of debugging if you don't know about them. Let's get into it.

→ **[Continue to Part 2: CDK Infrastructure for Amazon Bedrock AgentCore](/blog/part-2-cdk-infrastructure-bedrock-agentcore)**
