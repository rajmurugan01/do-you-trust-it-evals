---
title: "Part 2: The MCP Server: Turning ADRs and Incidents into a Queryable Org-Knowledge Surface"
description: "The agent doesn't read your wiki. It calls four tools that pull frontmatter-filtered chunks out of a Bedrock Knowledge Base. Here's the contract, the code, and the small decisions that make the difference between an agent that reads your docs and one that knows your org."
pubDate: 2026-05-01
tags: ["aws", "devopsagent", "mcp", "bedrock", "fastmcp", "python"]
pillars: ["agentic-ai", "enterprise-ai"]
series: "Building an AWS DevOps Agent that Knows Your Org"
seriesPart: 2
canonicalURL: "https://rajmurugan.com/blog/part-2-aws-devops-agent-mcp-layer"
devToUrl: "https://dev.to/rajmurugan/part-2-the-mcp-server-turning-adrs-and-incidents-into-a-queryable-org-knowledge-surface-26ni"
readTime: 13
---

In [Part 1](/blog/part-1-aws-devops-agent-intent-vs-state) I argued that an org-aware DevOps agent has to see two things at once: **state** (what your infrastructure currently is) and **intent** (what your team decided it should be). The first half is solved by mature observability. The second is what this series is actually about.

This post is the deep dive on the half I built. The MCP server. Four tools, one Bedrock Knowledge Base, and a small but load-bearing decision about where the structure lives.

The thing I want you to take away: **the MCP isn't a wrapper around a search bar. It's a typed query layer over your org's documented decisions, with metadata as the contract.**

Let's get into it.

---

![Comparison table: AIops incumbents (Datadog Watchdog, New Relic AI, PagerDuty AIOps) read telemetry and miss your ADRs, runbooks, decisions. DevOps Agent + curated KB (Bedrock AgentCore + your org's ADRs) reads telemetry plus your documented intent.](/images/blog/part-2/01-aiops-vs-org-aware.png)

## Why MCP, not prompt-stuffing

The first version of this build, like everyone's first version, had every ADR pasted into the system prompt. It worked great for a week. Then I added a second service. Then a third. By the time I had a real corpus, four things had broken:

1. **Cost.** Every turn re-pays for the same context window. With Claude Sonnet at the rates I was running, putting fifty ADRs in the system prompt added a few cents per call. Multiply by an on-call rotation answering thirty alarms a week and the maths gets uncomfortable.

2. **Freshness.** When a team updated an ADR, the system prompt didn't update. The agent kept citing decisions that had been superseded a month ago.

3. **No filtering.** The model has to *read* the whole corpus every turn to figure out which decision applies to the alarm in front of it. That works for ten documents and fails for two hundred.

4. **The model gets lazy with prose.** With everything in context, it tends to summarise rather than retrieve specific clauses. You ask "what was the expiry date on the IP allowlist?" and you get a paraphrase, not the date.

MCP solves all four. The agent decides *when* to retrieve, the tool returns *only* the chunks that matched, and the org's source of truth lives in S3 + a Bedrock Knowledge Base where it can be updated by anyone with a markdown editor and a `git push`.

The trade is one round trip per query. For a 3am incident response, that round trip is well worth it.

---

## The four-tool API

The MCP server exposes exactly four tools. I tried five and six before settling here; this is the smallest set that lets the agent answer the questions I actually want it to answer.

![Four tools: search_architectural_decisions for semantic search across ADRs/planning/meeting-notes; get_decision_details for fetch by id; check_risk_acceptance_status for date-filtered ADRs (expired/expiring_soon/active per service); get_related_incidents for signal-intersect across post-incident reviews.](/images/blog/part-2/02-mcp-tools.svg)

That last argument, `signals: list[str]`, is the one that earns its keep. Incidents in my corpus have a frontmatter `signals: [bedrock_throttling, latency_spike]` and the tool intersects requested signals with each incident's set. That turns "find similar incidents" from a vibes-based semantic search into a structured filter the agent can actually trust.

The agent picks which of these to call. I do not script the order; the system prompt just says *"you have these four tools, here's when each is appropriate."* In practice the model uses `search_architectural_decisions` first ~70% of the time, `check_risk_acceptance_status` when the alarm is service-tagged, and the others as follow-ups.

---

## Anatomy of one tool: check_risk_acceptance_status

This is the tool that does the most distinctive work. It's the one that turns *"there's an ADR about an IP allowlist"* into *"that allowlist's 30-day risk acceptance expired ten days ago."* Date math, structured filter, no LLM hallucination.

Here's the whole thing, anonymised but otherwise unchanged from the production code:

```python
"""Tool: check_risk_acceptance_status."""
from __future__ import annotations
from datetime import date
from typing import Any
from pydantic import BaseModel, Field, field_validator

from src.clients.knowledge_base import KnowledgeBaseClient
from src.tools._common import as_of_date, parse_iso_date

EXPIRING_WINDOW_DAYS = 14


class CheckRiskInput(BaseModel):
    service: str = Field(..., min_length=1, max_length=128)
    as_of: str | None = None

    @field_validator("service")
    @classmethod
    def _strip_service(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("service must be non-empty")
        return stripped


def classify(expires: date, as_of: date) -> tuple[str, int]:
    """Return (status, days_overdue). days_overdue is negative if not yet expired."""
    days_overdue = (as_of - expires).days
    if days_overdue > 0:
        return "expired", days_overdue
    if abs(days_overdue) <= EXPIRING_WINDOW_DAYS:
        return "expiring_soon", days_overdue
    return "active", days_overdue


def check_risk_acceptance_status(
    client: KnowledgeBaseClient,
    service: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    args = CheckRiskInput(service=service, as_of=as_of)
    ref_date = as_of_date(args.as_of)

    hits = client.retrieve(f"{args.service} risk acceptance expires", top_k=20)
    seen: set[str] = set()
    findings: list[dict[str, Any]] = []
    for hit in hits:
        if hit.type != "adr":
            continue
        if hit.service != args.service:
            continue
        expires_raw = hit.frontmatter.get("expires")
        if not expires_raw:
            continue
        try:
            expires = parse_iso_date(expires_raw)
        except ValueError:
            continue
        if not hit.id or hit.id in seen:
            continue
        seen.add(hit.id)
        status, days_overdue = classify(expires, ref_date)
        findings.append({
            "id": hit.id,
            "title": hit.title,
            "expires": expires.isoformat(),
            "days_overdue": days_overdue,
            "status": status,
            "s3_uri": hit.s3_uri,
        })
    findings.sort(key=lambda f: f["days_overdue"], reverse=True)
    return {
        "service": args.service,
        "as_of": ref_date.isoformat(),
        "findings": findings,
    }
```

Three things to notice, because they explain a lot of design choices that come up later in this post.

**1. The retrieval query is semantic, the filter is exact.** The KB call uses the natural-language string `"northwind-quote risk acceptance expires"`. That gets us in the right neighbourhood. Bedrock's HYBRID search picks up ADRs about risk and expiry. The structured filter (`hit.type != "adr"`, `hit.service != args.service`, `expires_raw` present and parseable as a date) then *guarantees* we only return ADRs for the right service with a real expiry date. You do not let the model freelance on this. You make the tool deterministic.

**2. The `classify` function is deliberately boring.** Three statuses, one constant for the "expiring soon" window, no LLM in the loop. Date math should never be model-driven. This is where I've watched other teams put a Bedrock Converse call in to "interpret" the date, and that is exactly the wrong place for it.

**3. The output shape is structured JSON.** The agent does not get prose; it gets a list of findings with `id`, `title`, `expires`, `days_overdue`, `status`. When the agent then writes its response to the on-call, it cites these fields. That's why "wrong citations are visible, not silent" actually holds. There is no hidden text the model can paraphrase wrong; the model can only quote what the tool returned.

A typical run for the Northwind ADR-004 from Part 1, sixty days past the March 1 deadline, returns:

```json
{
  "service": "northwind-quote",
  "as_of": "2026-04-30",
  "findings": [
    {
      "id": "ADR-004",
      "title": "Synchronous Bedrock call in /tweak — temporary",
      "expires": "2026-03-01",
      "days_overdue": 60,
      "status": "expired",
      "s3_uri": "s3://intent-guard-kb-docs/adrs/ADR-004-sync-bedrock.md"
    }
  ]
}
```

The agent then cites `ADR-004` and the sixty-day overdue figure verbatim in its response. That's the loop.

---

## Frontmatter is the contract

You cannot do what `check_risk_acceptance_status` does if `expires` lives in prose like "the team agreed this exception would be reviewed by early March." The structured filter only works if `expires: 2026-03-01` is a YAML field at the top of the document.

So the corpus convention is: every doc has frontmatter, every frontmatter has a controlled set of fields.

```yaml
---
type: adr                # adr | runbook | incident | planning | meeting_notes | architecture
id: ADR-004
title: Synchronous Bedrock call in /tweak — temporary
date: 2026-01-12
status: accepted          # accepted | superseded | rejected
service: northwind-quote
expires: 2026-03-01
---
```

Six fields. None of them optional except `expires` (only ADRs that are *temporary risk acceptances* set it). `service` is the join key that makes everything else cross-correlatable: it's how `check_risk_acceptance_status(service="northwind-quote")` finds documents about the right service, and how a future incident report can be matched to the ADR it might have been predicted by.

Two things this convention costs me, and one thing it buys me.

**Cost 1.** Every doc has to be authored by someone who knows the schema. I document it in a README inside `data/` and reject PRs that don't follow it. For a small team this is fine. For a large org you'd want a template + a CI check that validates frontmatter, about 30 lines of Python. I'll publish that separately.

**Cost 2.** Bedrock KB doesn't have native support for "metadata-aware retrieval" the way some vector stores do. The KB ignores my YAML frontmatter at index time: it gets indexed as plain text alongside the document body. That means I can't use Bedrock's metadata filters; I have to parse the frontmatter back out *after* retrieval, in the MCP tool. More on that below.

**Buy.** Once a field is structured, *any* tool can filter on it. I added `signals` to my incident frontmatter purely so the agent could ask "have we seen this combination of symptoms before?" That feature took ten lines in `get_related_incidents.py`, because the contract was already in place.

---

## The retrieval client, and the bug Bedrock KB chunking gives you for free

Here's the bug. Bedrock Knowledge Base, in its default chunking strategy, sometimes collapses your perfectly valid YAML frontmatter onto a single line.

What you write in S3:

```
---
type: adr
id: ADR-004
service: northwind-quote
expires: 2026-03-01
---

# ADR-004: Synchronous Bedrock call...
```

What you get back from the Retrieve API, *sometimes*, depending on the chunk boundary:

```
--- type: adr id: ADR-004 service: northwind-quote expires: 2026-03-01 ---

# ADR-004: Synchronous Bedrock call...
```

Notice the lack of newlines between the keys. PyYAML refuses to parse that. It's not valid YAML. So you cannot just `yaml.safe_load(chunk_text)` and expect frontmatter to come back.

I lost an evening to this before realising what was happening. The fix is a three-tier parser that handles each shape:

```python
def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Three-tier fallback because Bedrock KB chunking is inconsistent
    about preserving whitespace in the frontmatter fence."""

    # Tier 1: standard multi-line fenced YAML
    m = _FRONTMATTER_MULTILINE.search(text)
    if m:
        try:
            parsed = yaml.safe_load(m.group("body")) or {}
            if isinstance(parsed, dict):
                return _stringify(parsed), text[m.end():].lstrip()
        except yaml.YAMLError:
            pass

    # Tier 2: fenced frontmatter collapsed onto one line (Bedrock's quirk)
    m_inline = _FRONTMATTER_INLINE.match(text)
    if m_inline:
        parsed = _split_inline_frontmatter(m_inline.group("body"))
        if parsed:
            return _stringify(parsed), text[m_inline.end():].lstrip()

    # Tier 3: loose key:value scan over leading lines
    loose: dict[str, Any] = {}
    for line in text.splitlines():
        ...
```

Tier 1 handles the standard case. Tier 2 catches the collapsed-fence form by splitting `"key: value key: value"` runs at identifier-colon boundaries (with bracket-depth awareness so inline arrays like `signals: [a, b]` don't get split inside the brackets). Tier 3 is a paranoid loose scan for documents that came back with no fences at all.

The reason I'm walking you through this in detail: **if you're building anything similar, you will hit this.** Bedrock KB is a managed service, the chunking is not configurable to the level you'd want, and your retrieval-time parser has to be robust to the wire format the API actually returns. Plan for it.

The full retrieval client wraps `bedrock-agent-runtime:Retrieve` with HYBRID search (vector + keyword) and parses frontmatter on the way out:

```python
class KnowledgeBaseClient:
    def retrieve(self, query: str, *, top_k: int = 5) -> list[Retrieval]:
        resp = self._client.retrieve(
            knowledgeBaseId=self.kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": top_k,
                    "overrideSearchType": "HYBRID",
                }
            },
        )
        return [self._to_retrieval(raw) for raw in resp.get("retrievalResults", [])]

    @staticmethod
    def _to_retrieval(raw: dict[str, Any]) -> Retrieval:
        text = str((raw.get("content") or {}).get("text") or "")
        s3 = (raw.get("location") or {}).get("s3Location") or {}
        score = float(raw.get("score") or 0.0)
        frontmatter, _ = parse_frontmatter(text)
        return Retrieval(score=score, s3_uri=str(s3.get("uri") or ""),
                         content=text, frontmatter=frontmatter)
```

HYBRID search matters here. Pure vector search alone routinely misses on document IDs (the strings `ADR-004`, `SEC-2024-09-12` aren't semantically anchored; they're tokens). Keyword search alone misses on phrasing (`"the bedrock retry decision"` vs `"synchronous Bedrock call"`). HYBRID does both. For a small corpus of decisions and incidents, the difference is the difference between "agent finds the right ADR every time" and "agent occasionally hallucinates an ADR-007 that doesn't exist."

---

## The transport: FastMCP on Lambda + Function URL

The MCP server itself is small. FastMCP, four `@app.tool` decorators, an ASGI handler that Lambda runs through Mangum.

```python
"""FastMCP server — registers the four Intent Guard tools over Streamable HTTP."""
from functools import lru_cache
from fastmcp import FastMCP

from src.clients.knowledge_base import KnowledgeBaseClient
from src.tools.check_risk import check_risk_acceptance_status
from src.tools.get_decision import DecisionNotFoundError, get_decision_details
from src.tools.get_incidents import get_related_incidents
from src.tools.search_decisions import search_architectural_decisions

app: FastMCP = FastMCP("intent-guard")


@lru_cache(maxsize=1)
def get_client() -> KnowledgeBaseClient:
    """Lazily instantiate the KB client — reads KB_ID/AWS_REGION at first use."""
    return KnowledgeBaseClient()


@app.tool
def search_architectural_decisions_tool(
    query: str,
    service: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Semantic search across ADRs, planning docs, and meeting notes.

    Use this to find architectural decisions, deferred work, and discussions
    related to a query. Filter by `service` (e.g. "northwind-quote") when you
    know which service is affected.
    """
    return search_architectural_decisions(
        get_client(), query=query, service=service, top_k=top_k
    )


# get_decision_details_tool, check_risk_acceptance_status_tool,
# get_related_incidents_tool all follow the same shape.
```

Two design choices that ride along with this.

**The tool docstring is the API contract.** AgentSpace introspects each tool's docstring at registration time and presents that text to the model as the tool's description. Whatever you write in the docstring is what the model sees when deciding whether to call this tool. *"Filter by `service` (e.g. 'northwind-quote') when you know which service is affected"* is how I get the model to actually pass the `service` parameter. Without it the model frequently calls the tool with `service=None` and over-fetches. Treat your docstrings as prompt engineering surface, not internal documentation.

**`@lru_cache` on `get_client()`.** The Lambda container is reused across invocations. Without the cache, every cold-ish invocation re-instantiates the boto3 client (~200ms). With it, the first invocation pays the cost and the rest reuse. This is the right shape for any per-Lambda singleton (config, clients, secrets) that you want to outlive a single request.

The Lambda itself is wired up via Mangum's ASGI adapter, deployed as a Docker image, and exposed through a Lambda Function URL with `AuthType=NONE`. Why no Lambda-native auth? Because AgentSpace's "register-service" flow expects to authenticate to MCP servers via an `X-API-Key` header it presents itself, not via IAM SigV4. So the Function URL is open at the network layer, and the API key is enforced *inside* the handler.

---

## API-key auth without the leaks

Three things I wanted from the auth layer:

1. The expected key never lives in the synthesised CloudFormation template (so it's not visible to anyone with read access to CFN).
2. The key is rotatable without redeploying the Lambda.
3. The comparison is constant-time so no timing oracle.

The pattern that gives you all three: store the key in Secrets Manager, fetch it on Lambda cold start, compare with `hmac.compare_digest` per request.

```python
import hmac, json, os, boto3

class ApiKeyMiddleware:
    """ASGI middleware that enforces X-API-Key against a Secrets Manager value."""

    def __init__(self, app, *, secret_arn=None, region=None):
        self._app = app
        self._secret_arn = secret_arn or os.environ["MCP_API_KEY_SECRET_ARN"]
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._expected_key: str | None = None
        self._client = boto3.client("secretsmanager", region_name=self._region)

    def _load_key(self) -> str:
        if self._expected_key is not None:
            return self._expected_key
        resp = self._client.get_secret_value(SecretId=self._secret_arn)
        raw = resp["SecretString"]
        # Accept plain string OR a JSON blob like {"api_key": "..."}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "api_key" in parsed:
                raw = str(parsed["api_key"])
        except json.JSONDecodeError:
            pass
        self._expected_key = raw
        return raw

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("path") == "/health":           # smoke tests bypass auth
            await self._app(scope, receive, send)
            return
        provided = _header(scope, b"x-api-key")
        expected = self._load_key()
        if provided is None or not hmac.compare_digest(provided, expected):
            await _send_401(send)
            return
        await self._app(scope, receive, send)
```

A few things worth calling out for production work:

- **`hmac.compare_digest`** is the line that matters. A naive `==` comparison is timing-attacking; it short-circuits on first mismatch. With a constant-time compare, you don't leak how many leading bytes were correct.

- **`/health` bypass.** I want to be able to `curl` the Function URL from a CI job without providing the key, just to check the Lambda is alive. That's strictly *less* powerful than a normal request, but it has saved me debugging time often enough to be worth the allowance.

- **Module-cached key.** The `_expected_key` field on the middleware instance lives as long as the Lambda container does, which is up to a few hours. If you rotate the key in Secrets Manager, in-flight Lambdas will keep the old key until they cycle. For a demo that's fine; for production you want a tighter TTL or a versioned secret with overlap.

- **JSON-or-plain-string.** Secrets Manager has a `generateSecretString` mode that produces JSON like `{"password": "..."}`. If your secret was generated that way, you have to fish the value out. Accepting both shapes makes the middleware portable across deploy paths.

---

## What lands in S3, and how

The corpus is a directory tree of markdown files:

```
data/
├── adrs/
│   ├── ADR-003-temporary-ip-allowlist.md
│   ├── ADR-004-sync-bedrock-tweak.md
│   └── ADR-007-cost-controls-bedrock.md
├── runbooks/
│   ├── RB-002-bedrock-throttling.md
│   └── RB-005-incident-response.md
├── incidents/
│   ├── SEC-2024-09-12.md
│   └── SEC-2025-11-03.md
├── planning/
│   └── 2026-Q1-platform-roadmap.md
├── meeting-notes/
│   └── platform-sync-2026-04-15.md
└── architecture/
    └── northwind-quote-overview.md
```

A small ingestion script does `data/**/*.md → s3://<bucket>/`, preserving the folder structure. Bedrock KB's S3 connector picks up the changes and re-indexes. The whole sync is idempotent: re-running on the same content is a no-op.

I keep this script outside CDK on purpose. CDK is for infrastructure; the corpus is *content*. You want a non-engineer to be able to commit a markdown file and have it ingested without a stack deploy. The pattern that works: corpus lives in the same repo, a GitHub Action on push to `main` runs the sync into the demo bucket, and nothing else needs to know.

---

## Summary, before we wire it up

What you have at the end of Part 2 is a Lambda you can `curl` directly:

```bash
curl -sX POST "$FUNCTION_URL" \
  -H "X-API-Key: $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","id":1,
       "params":{"name":"check_risk_acceptance_status_tool",
                 "arguments":{"service":"northwind-quote"}}}' | jq .
```

…and get back the structured findings JSON I showed earlier. No agent, no AgentSpace, no Operator Web App. Just a typed surface over your org's documented decisions.

That alone is a useful thing to have. You can plug it into anything that speaks MCP: Claude Desktop, the Claude Agent SDK, any other MCP-aware host.

In Part 3 we wire it into AWS DevOps Agent so that the agent calls these tools automatically when an alarm fires. CDK for the AgentSpace, the register-service flow, the IAM trust policy gotcha that ate me alive (composite principal + SourceArn confused-deputy condition), and the webhook forwarder that turns CloudWatch / PagerDuty / Dynatrace events into agent invocations. That post is where the full system finally answers a 3am page.

→ **Continue to Part 3: Wiring it into AWS DevOps Agent: AgentSpace, register-service, and the IAM trust policy that ate my afternoon** *(coming this week)*
