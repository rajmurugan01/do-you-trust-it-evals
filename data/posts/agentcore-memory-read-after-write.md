---
title: "Field Notes: The AgentCore Memory write that returns success and reads back empty"
description: "AgentCore long-term memory has a read-after-write gotcha the docs skip: a direct BatchCreateMemoryRecords write returns 201 and stays unsearchable for 15 to 30 seconds. Measured, with the two-tier model that explains it."
pubDate: 2026-07-13
tags: ["aws", "bedrock", "agentcore", "memory", "agents"]
pillars: ["agentic-ai", "ai-operations"]
format: "Field Notes"
series: "Production AI, Honestly"
seriesPart: 4
canonicalURL: "https://rajmurugan.com/blog/agentcore-memory-read-after-write"
devToUrl: "https://dev.to/rajmurugan/field-notes-the-agentcore-memory-write-that-returns-success-and-stores-nothing-ng8"
coverImage: "/images/blog/2026-07-13_agentcore_memory_hero.png"
coverImageAlt: "A scorecard: BatchCreateMemoryRecords returns HTTP 201 Created, while a namespace read returns zero records for fifteen to thirty seconds. Three measured runs shown: sixteen, twenty-seven and fifteen seconds to first visible."
readTime: 8
---

I wired long-term memory into an agent on Amazon Bedrock AgentCore, wrote a record, got a `201`, and read back nothing. The record existed. The API said success. The read returned zero rows. It took me longer than I would like to admit to work out that all three of those were true at the same time.

AgentCore went GA on 13 October 2025, after a July preview. Memory is one of its newer pieces, and the docs are good on the happy path and quiet on the parts that bite. This is the operational truth of the write side, the bit you only learn by running it.

![BatchCreateMemoryRecords returns 201 Created while a namespace read returns zero records for fifteen to thirty seconds, across three measured runs.](/images/blog/2026-07-13_agentcore_memory_hero.png)

Two things bit me. One was an API that did not exist. The other was a write that succeeds and is not yet readable. Neither is in the tutorial.

## The API that never existed

The codebase had a helper for persisting a fact to memory. It called `client.ingest_memory_records(...)`. It read as correct. It had a docstring. It had a sensible name.

It had never run. It was written, never called, and so had never thrown. When I finally wired it into a real path, I checked the client first:

```python
import boto3
c = boto3.client("bedrock-agentcore")
hasattr(c, "ingest_memory_records")   # -> False
```

There is no `ingest_memory_records` operation on the AgentCore data plane. The helper would have raised `AttributeError` the first time anyone called it. Dead code that mirrors a real-sounding API is worse than no code, because it passes the eye test. A method name is not a fact. It is a claim, and an unrun claim is a guess wearing a fact's clothes.

The real operation is `BatchCreateMemoryRecords`. Confirm it against the SDK you actually ship, not against what the name suggests:

```python
ops = c.meta.service_model.operation_names
[o for o in ops if "Memory" in o or "Record" in o]
# BatchCreateMemoryRecords, BatchDeleteMemoryRecords, BatchUpdateMemoryRecords,
# DeleteMemoryRecord, GetMemoryRecord, ListMemoryRecords, RetrieveMemoryRecords
```

Lesson one, before any of the memory detail: verify the operation exists in your pinned SDK version. The write API and the read API are not symmetric in name, and one of the plausible names is a trap.

Better than checking one call by hand, make the build check every call. The service model is the ground truth for what exists, so a CI test can fail the build the moment source names an operation that does not:

```python
# CI: fail the build if the codebase calls a bedrock-agentcore method that
# does not exist in the pinned SDK. Kills the ingest_memory_records class,
# including the next hallucinated API someone commits.
import boto3
client = boto3.client("bedrock-agentcore", region_name="us-east-1")
called = {"batch_create_memory_records", "get_memory_record",
          "retrieve_memory_records", "ingest_memory_records"}  # grep these from source
missing = [m for m in called if not hasattr(client, m)]
assert not missing, f"no such AgentCore operation: {missing}"   # -> ['ingest_memory_records']
```

## Two tiers, and which one you are writing to

AgentCore Memory has two tiers, and the whole confusion comes from not knowing which one a given call touches.

Short-term memory is raw events. You write them with `CreateEvent`, one per turn or in batches, scoped to an actor and a session. This is conversation history. You do not semantically search it.

Long-term memory is extracted records, organised into namespaces. Normally these are produced asynchronously: a memory strategy runs in the background, reads your short-term events, and extracts or consolidates records into a namespace. The AWS docs are explicit that this generation is an async background process. You read long-term records with `RetrieveMemoryRecords`, a semantic search scoped to a namespace.

So if you want an agent to recall a durable fact on the next turn, it has to live in a long-term namespace that `RetrieveMemoryRecords` reads. Writing a `CreateEvent` and hoping the strategy extracts the right fields is slow and non-deterministic. There is a better path.

`BatchCreateMemoryRecords` writes directly into a long-term namespace. It is the bring-your-own-extraction door: you have already structured the fact, so you skip the strategy and put the record where the reader will look. The request is straightforward:

```python
c.batch_create_memory_records(
    memoryId=MEMORY_ID,
    clientToken=token,   # batch idempotency; a retried identical batch dedupes
    records=[{
        "requestIdentifier": "seed-1",   # correlation key, NOT idempotency
        "namespaces": ["/orgs/<tenant>/user/<user>/preferences/"],
        "content": {"text": "Role: AE, mid-market SaaS. Prefers blunt feedback."},
        "timestamp": now,
        # memoryStrategyId is optional; see below
    }],
)
```

Note there is no `actorId` argument. The actor is encoded into the namespace string. Get the namespace wrong and the write goes somewhere the reader never queries, which is its own quiet failure.

That has a security edge too. Because the actor is just part of a string, your tenant isolation is only as strong as the code that builds it, and a bug there is a cross-tenant read. The same reasoning behind [the LLM is not a security boundary](https://rajmurugan.com/blog/llm-is-not-a-security-boundary) applies to your own string formatting: do not let it be the only thing standing between tenants. `RetrieveMemoryRecords` honours the `bedrock-agentcore:namespace` (exact) and `bedrock-agentcore:namespacePath` (subtree) IAM condition keys, so a policy can pin a principal to its own `/orgs/<tenant>/` prefix and the service refuses an off-tenant namespace whatever the code passes.

## Success is not retrievability

Here is the part that cost me the afternoon. The write returns `201` with a `successfulRecords` entry and a `memoryRecordId`. Fetch that id directly and the record is there immediately:

```python
r = c.batch_create_memory_records(memoryId=MEMORY_ID, records=[rec])
rid = r["successfulRecords"][0]["memoryRecordId"]

c.get_memory_record(memoryId=MEMORY_ID, memoryRecordId=rid)  # exists, right away
```

Now query the namespace the way an agent actually would, and at five seconds it is empty:

```python
c.retrieve_memory_records(
    memoryId=MEMORY_ID,
    namespace="/orgs/<tenant>/user/<user>/preferences/",
    searchCriteria={"searchQuery": "role preferences", "topK": 10},
)  # +5s -> 0 records
c.list_memory_records(memoryId=MEMORY_ID, namespace=NS)  # +5s -> 0 records too
```

Both the semantic read and the plain namespace list returned nothing, while the record was fetchable by id the whole time. Poll for longer and the rows appear. So the record was created, addressable, and not yet indexed for namespace or semantic retrieval.

I ran that write-then-poll loop three times, fresh namespace each time, checking every three seconds. The semantic read first returned the record at 16, 27 and 15 seconds. The namespace list was similar, at 16, 23 and 15 seconds. Same account, same region, one sitting, so treat it as a rough ballpark, not a benchmark. And treat the fresh-namespace part as a confound, not a control: some of that time may be the namespace itself warming up rather than the record indexing, so a warm namespace already holding thousands of records could behave differently. I have not measured that steady state yet, and it is the number production would actually care about. Three samples also cannot see a tail, and the tail is the whole operational question, so the right move is not to trust the ballpark at all.

The docs tell you long-term *generation* from events is asynchronous. They do not tell you that a *direct* `BatchCreateMemoryRecords` write also indexes asynchronously. I went looking, in the API reference and the memory guide, and could not find the read-after-write behaviour stated anywhere. You would reasonably assume the direct door skips the wait, because you did the extraction yourself. It does not skip the indexing.

The mental model that would have saved me the afternoon: `201` means accepted, not queryable. Read-after-write on a namespace is eventually consistent, on the order of tens of seconds. If you need certainty that a specific record landed, read it by id with `GetMemoryRecord`, which is immediate. If you need it to appear in a namespace search, poll until it does rather than sleep on a fixed guess (more on that below).

One consequence follows straight from that lag: because the write returns before it is searchable, a retry inside the window is easy, and `requestIdentifier` will not save you. It is a correlation key, so two writes with the same one create two distinct records. The idempotency control is `clientToken` on the batch call. Retry the identical batch with the same token and the service dedupes it. If your write path retries on timeout, set it.

## The one that was simpler than the docs implied

A smaller finding while I was in there. Long-term records carry an optional `memoryStrategyId`. I assumed retrieval might require the record's strategy to match the strategy that owns the namespace. It does not. I wrote two records to the same namespace, one with a `memoryStrategyId` and one without, and `RetrieveMemoryRecords` returned both. So an unfiltered retrieve does not gate on strategy id: give it a namespace and a `searchQuery` and it returns matching records whether or not they carry a `memoryStrategyId`. You can add that gate yourself, `searchCriteria` also takes a `memoryStrategyId` and `metadataFilters`, but retrieval does not impose it by default. The strategy id is for associating a record with a strategy's consolidation, not a mandatory gate on reads. One less thing to get exactly right.

That association is the part worth thinking about, and it outranks the read behaviour. Consolidation exists precisely to merge and dedupe the records a strategy owns, so a directly-written "durable fact" sitting under a built-in strategy is not something I would assume stays byte-for-byte as I wrote it. The documented pairing for bring-your-own extraction is a [self-managed strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-self-managed-strategies.html): direct `BatchCreateMemoryRecords` writes bypass the extraction pipeline entirely, and a self-managed strategy leaves extraction and consolidation to code you control rather than a built-in pass you did not write. If you are seeding durable facts by hand, that is the strategy to put them under.

## What I now do

- Treat `201` from `BatchCreateMemoryRecords` as accepted, not queryable. The record is addressable by id immediately and searchable by namespace tens of seconds later.
- If I need read-after-write certainty on a specific record, read it by id with `GetMemoryRecord`, never by a namespace search.
- Keep my own durable map of record ids. `GetMemoryRecord` is only immediate because I already hold the `memoryRecordId`, which means I persisted it somewhere (an actor-to-ids table in DynamoDB, say). So AgentCore Memory is the semantic-recall layer, not the system of record: if a fact has to be readable the instant it is written, my store is the source of truth and AgentCore is the index that catches up.
- Do not gate a "saved, now ask me" experience on instant recall. If a user saves a profile and immediately asks the agent what it knows about them, the honest answer for a few tens of seconds is nothing. For an onboarding flow this is fine, because there is natural delay before the first real turn. For a health check that writes then reads a namespace, it is a flake generator.
- Gate on a readiness probe, never a timer. A fixed `sleep(30)` is both slow and a p99 flake generator, and it hides the silent namespace-typo failure behind a wait that looks deliberate. Poll `ListMemoryRecords`, or the `RetrieveMemoryRecords` the reader actually uses, until the record appears or a timeout fires, and alarm on the timeout. That converts "I guessed thirty seconds" into a measured, monitored wait, and turns a wrong namespace into a real error instead of a silently empty read.
- If the write path can retry, and a timeout inside the index-lag window is the obvious case, set `clientToken` on the batch so a replay dedupes. `requestIdentifier` will not, it is a correlation key and two identical ones make two records.
- Enforce tenant isolation on the namespace in IAM, not just in the code that builds the string. Pin the principal with a `bedrock-agentcore:namespacePath` condition so an off-tenant read is refused by the service, not by a code review.
- Verify the operation exists in the pinned SDK before trusting a helper. A method name, a docstring, and a green diff are not evidence that a call is real.

None of this is a reason to avoid direct writes. They are the right call when you already have a structured fact, because the alternative is waiting on async extraction that may drop or reshape the fields you care about. It just means you design around two facts the docs bury: the name might not be a real operation, and the `201` means the service took your record, not that a reader can find it. Both looked like success. Neither was, until I actually read it back.

The hard part was never the memory model. It was the gap between what the API reports and what is true a second later.

---

*Latency measured in a dev account, one region, three runs, polled at three-second granularity, so the real figure sits a little under each number. Retention and consolidation behaviour of directly-created long-term records I have not fully characterised yet. If your lag numbers differ, I would like to hear them.*

**Next in _Production AI, Honestly_:** [Part 5: the LLM is not a security boundary](/blog/llm-is-not-a-security-boundary). This is the turn from measuring the probabilistic layer to refusing to trust it, with the controls that actually hold in code the model never touches.
