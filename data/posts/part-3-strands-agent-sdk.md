---
title: "Part 3: Building the AI Agent with Strands Agents SDK, Prompt Caching, and AgentCore Memory"
description: "How to build the Python agent that runs inside AgentCore: Strands SDK setup, prompt caching that cuts costs by 90%, dual-model strategy, tool definitions, and AgentCore Memory integration."
pubDate: 2025-10-29
tags: ["python", "aws", "bedrock", "agentcore", "strands", "aiagents"]
pillars: ["agentic-ai"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 3
canonicalURL: "https://rajmurugan.com/blog/part-3-strands-agent-sdk"
devToUrl: "https://dev.to/rajmurugan/part-3-building-the-ai-agent-with-strands-agents-sdk-prompt-caching-and-agentcore-memory-134a"
readTime: 11
---

With the CDK infrastructure in place (Part 2), we need an actual agent to run inside it.

The agent is a Python application that:
1. Exposes an HTTP endpoint AgentCore can call
2. Uses the Strands Agents SDK to run a Bedrock-backed reasoning loop
3. Integrates with AgentCore Memory for persistent context
4. Uses Bedrock Guardrails on every invocation

The full source is in [apps/customer-service-agent/](https://github.com/rajmurugan01/bedrock-agentcore-starter/tree/main/apps/customer-service-agent) in the demo repo.

---

## Why Strands over LangChain or LlamaIndex?

When I started this project, LangChain was the default answer for "I need to build an agent." I used it, ran into friction, and switched to Strands. Here's why:

**Strands is AWS-native.** It's built to integrate directly with Bedrock services: prompt caching, guardrail configs, tool definitions. With LangChain, you write adapter code to bridge from LangChain abstractions down to raw Bedrock APIs. With Strands, you're calling the Bedrock API directly through a thin, intentional abstraction.

**Tool definitions are simpler.** In LangChain, you define tools with `StructuredTool.from_function()` or subclass `BaseTool`. In Strands, you decorate a function with `@tool` and the docstring becomes the description:

```python
# LangChain approach
from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

class OrderLookupInput(BaseModel):
    order_id: str = Field(description="The order ID")

def lookup_order_status(order_id: str) -> str:
    ...

tool = StructuredTool.from_function(
    func=lookup_order_status,
    name="lookup_order_status",
    description="Look up the current status of an order",
    args_schema=OrderLookupInput,
)

# Strands approach
from strands import tool

@tool
def lookup_order_status(order_id: str) -> str:
    """Look up the current status of a customer order by order ID."""
    ...
```

**Active development matches AgentCore.** Strands is developed at a cadence that tracks AgentCore releases. New AgentCore features show up in Strands before they make it to LangChain adapters.

---

## Project structure

```
customer_service_agent/
├── __init__.py
├── config.py       # Settings from env vars
├── prompts.py      # System prompt
├── tools.py        # @tool definitions
├── memory.py       # AgentCore Memory boto3 helpers
├── agent.py        # BedrockModel setup + streaming
└── main.py         # FastAPI app
```

---

## config.py: environment variables

Everything the agent needs is injected as environment variables by AgentCore. In production, you set `EnvironmentVariables` on the `CfnRuntime` resource in CDK. Locally, you use `.env.local`.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    agentcore_memory_id: str = ""
    bedrock_guardrail_id: str = ""
    bedrock_guardrail_version: str = "1"
    aws_region: str = "us-east-1"
    environment: str = "dev"

    # Primary: Claude Sonnet 4.5
    primary_model_id: str = "anthropic.claude-sonnet-4-5-20250929-v1:0"

    # Background: Nova Pro (~15x cheaper per token)
    background_model_id: str = "amazon.nova-pro-v1:0"

    class Config:
        env_file = ".env.local"

settings = Settings()
```

---

## The dual-model strategy

The agent uses two Bedrock models for different tasks:

**Claude Sonnet 4.5** for main conversations: best reasoning, multi-step tool use, nuanced responses. More expensive but worth it for the customer-facing output.

**Amazon Nova Pro** for background tasks: ~15x cheaper per token. Ideal for:
- Classifying intent before routing
- Summarising long conversation history
- Generating internal labels/tags
- Any task where "good enough" is sufficient

---

## Prompt caching: the 90% cost saving

This is the most impactful optimisation in the whole system.

Prompt caching works like this: you mark part of your prompt as a "cacheable prefix" by inserting a `cachePoint` block after it. Bedrock caches those tokens server-side for ~5 minutes. On subsequent calls that use the same prefix, you pay the **cache read price** instead of the full input token price. With the Strands `BedrockModel`, you don't hand-build the `cachePoint` block: two kwargs (`cache_prompt` and `cache_tools`) insert it for you, at the end of the `system` block and inside `toolConfig.tools` respectively.

For Claude Sonnet 4.5:
- Cache write: $3.00 per 1M input tokens (same as normal)
- Cache read: $0.30 per 1M input tokens
- **Saving: 90% on cached tokens**

The system prompt is the perfect candidate for caching. It's the same on every request in a session:

```python
from strands.models import BedrockModel
from botocore.config import Config

# Adaptive retry — Bedrock throttles hard under load
boto_config = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    read_timeout=120,
)

primary_model = BedrockModel(
    model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
    boto_config=boto_config,
    # Turn caching on. cache_prompt inserts a cachePoint at the end of the
    # system block (system prompt becomes a cacheable prefix); cache_tools
    # inserts one inside toolConfig.tools (the tool registry becomes the
    # next cacheable prefix). Both are off by default.
    cache_prompt="default",
    cache_tools="default",
    guardrail_config={
        "guardrailIdentifier": settings.bedrock_guardrail_id,
        "guardrailVersion": settings.bedrock_guardrail_version,
    },
)
```

For a 1,500-token system prompt at 100 requests/day with 5 turns each:
- Without caching: ~450,000 system prompt tokens/day × $3/1M = **$1.35/day** just for system prompts
- With caching: first turn normal price, turns 2-5 at cache read → **~$0.27/day**
- **Saving: ~$1/day, ~$365/year on just the system prompt**

The saving scales linearly with session length and request volume.

---

## Tool definitions

Strands tools are just decorated Python functions. The function signature defines the input schema, and the docstring is sent to the model as the tool description:

```python
from strands import tool

@tool
def lookup_order_status(order_id: str) -> str:
    """
    Look up the current status and details of a customer order by order ID.
    Use this when a customer asks about their order, delivery, or shipment.

    Args:
        order_id: The order ID (format: ORD-XXXXXX)

    Returns:
        Order details including status, items, and estimated delivery date.
    """
    # Your implementation here
    ...

@tool
def search_product_faq(query: str) -> str:
    """
    Search the product FAQ and policy knowledge base for answers to customer questions.
    ...
    """
    ...
```

Tools are passed to the `Agent` constructor. Strands handles the tool invocation loop: calling the tool when the model decides to use it, feeding the result back, and continuing the reasoning loop until the model produces a final response.

---

## AgentCore Memory integration

AgentCore Memory provides persistent storage across sessions without you building any of the storage infrastructure.

The three strategy types:
- **Semantic**: stores facts and user profile information. Consolidates information like "user prefers email contact", "user is on premium plan".
- **Summary**: stores compressed session history. "On 2025-03-15 user reported late delivery of order ORD-001234. Issue was resolved."
- **UserPreference**: stores interaction style. "User prefers brief responses without extra detail."

The memory client is a standard boto3 client:

```python
import boto3
from botocore.config import Config

memory_client = boto3.client(
    "bedrock-agentcore-memory",
    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=30),
)

# Store a conversation turn
memory_client.create_event(
    memoryId=settings.agentcore_memory_id,
    actorId=actor_id,         # Identifies the user (e.g., user ID from JWT)
    sessionId=session_id,     # Identifies the conversation session
    messages=[
        {"role": "USER", "content": [{"text": user_message}]},
        {"role": "ASSISTANT", "content": [{"text": assistant_message}]},
    ],
)

# Retrieve relevant memories before each invocation
response = memory_client.retrieve_memory_records(
    memoryId=settings.agentcore_memory_id,
    actorId=actor_id,
    searchQuery=user_message,  # Semantic search over stored memories
    maxResults=5,
)
memories = response.get("memoryRecords", [])
```

The retrieved memories are prepended to the user message as context:

```python
memory_context = "\n".join(
    f"- {record['content']['text']}"
    for record in memories
    if record.get("content", {}).get("text")
)

enriched_message = f"[Past context:]\n{memory_context}\n\n{user_message}"
```

---

## The streaming agent loop

The agent produces a streaming response via the Strands `stream_async` method. Each chunk is forwarded as an SSE event:

```python
async def stream_agent_response(user_message, actor_id, session_id):
    # 1. Retrieve memories
    memories = retrieve_relevant_memories(actor_id=actor_id, query=user_message)
    enriched_message = prepend_memory_context(memories, user_message)

    # 2. Build agent with tools
    agent = Agent(
        model=primary_model,
        system_prompt=SYSTEM_PROMPT,
        tools=[lookup_order_status, search_product_faq],
    )

    # 3. Stream response
    full_response_parts = []
    async for chunk in agent.stream_async(enriched_message):
        if chunk.get("type") == "text":
            text = chunk.get("text", "")
            if text:
                full_response_parts.append(text)
                yield f"data: {text}\n\n"  # SSE format

    # 4. Store turn in memory
    store_conversation_turn(
        actor_id=actor_id,
        session_id=session_id,
        user_message=user_message,
        assistant_message="".join(full_response_parts),
    )

    yield "data: [DONE]\n\n"
```

---

## The adaptive retry config

Bedrock throttles hard when you exceed your model's TPS (tokens per second) limit. Without retry logic, throttled requests fail immediately.

`mode: "adaptive"` uses a token bucket algorithm. It monitors the throttle rate and automatically backs off when it detects pressure:

```python
from botocore.config import Config

boto_config = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    read_timeout=120,   # Streaming responses can take 30-90s for complex tool chains
    connect_timeout=10,
)
```

The difference between `"standard"` and `"adaptive"` retry modes:
- `standard`: fixed exponential backoff between retries
- `adaptive`: adjusts retry rate based on observed throttling, converges to a sustainable rate faster

For agentic workloads that run multi-step tool chains (and thus make many Bedrock calls in sequence), `"adaptive"` consistently outperforms `"standard"`.

---

## Putting it all together

The `agent.py` file wires everything together:

```python
primary_model = BedrockModel(
    model_id=settings.primary_model_id,
    boto_config=boto_config,
    cache_prompt="default",
    cache_tools="default",
    guardrail_config={"guardrailIdentifier": settings.bedrock_guardrail_id, ...},
)

async def stream_agent_response(user_message, actor_id, session_id):
    memories = retrieve_relevant_memories(actor_id, user_message)
    enriched = prepend_memory_context(memories, user_message)

    agent = Agent(
        model=primary_model,
        system_prompt=SYSTEM_PROMPT,
        tools=[lookup_order_status, search_product_faq],
    )

    full_response = []
    async for chunk in agent.stream_async(enriched):
        if chunk.get("type") == "text" and chunk.get("text"):
            full_response.append(chunk["text"])
            yield f"data: {chunk['text']}\n\n"

    store_conversation_turn(actor_id, session_id, user_message, "".join(full_response))
    yield "data: [DONE]\n\n"
```

In Part 4, we set up the local Docker dev environment so you can iterate on the agent code without deploying to AWS on every change.

→ **[Continue to Part 4: Running Locally with Docker](/blog/part-4-local-dev-docker)**
