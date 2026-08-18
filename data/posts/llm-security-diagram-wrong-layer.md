---
title: "Your LLM security diagram defends the wrong layer"
description: "The LLM security diagram you have seen a dozen times is a threat map. Read as a defence it makes you patch every box at the layer the attacker controls. The fix is one deterministic boundary the diagram leaves out, in code the model never touches."
pubDate: 2026-07-21
tags: ["genai", "security", "agents", "prompt-injection", "architecture"]
pillars: ["agentic-ai", "enterprise-ai"]
format: "Field Notes"
series: "Production AI, Honestly"
seriesPart: 6
canonicalURL: "https://rajmurugan.com/blog/llm-security-diagram-wrong-layer"
coverImage: "/images/blog/2026-07-21_wrong_layer.png"
coverImageAlt: "Dark infographic: four red attack boxes (prompt injection, retrieval poisoning, context poisoning, output injection), each over a dashed amber probabilistic patch, and one solid blue bar beneath them, the deterministic boundary the diagram omits, keeping the data safe."
readTime: 9
---

Search "LLM security architecture" and you will meet the same diagram again and again. A tidy left-to-right pipeline, user input to retrieval to the model to output to tools, and hanging underneath it four red boxes: prompt injection, retrieval poisoning, context poisoning, output injection. It is a good diagram. I have drawn versions of it myself. And if you build your defences the way it is laid out, you will spend a year patching the wrong layer.

I argued in the last post that [the LLM is not a security boundary](/blog/llm-is-not-a-security-boundary), that the controls which actually hold are deterministic and live in code the model never touches. This is the same idea from the other side. The four-box diagram is not wrong about the threats. It is wrong about where the defence goes, and the error is baked so far into the layout that it is hard to see.

![Dark infographic titled Four attack names, one wrong reflex. Across the top, four red boxes: prompt injection, retrieval poisoning, context poisoning, output injection, each with a dashed amber probabilistic patch beneath it. Below them a single solid blue bar labelled the deterministic boundary the diagram omits, with the sensitive data safe underneath.](/images/blog/2026-07-21_wrong_layer.png)

### What the diagram gets right

The four attacks are real, and the diagram names them well. Prompt injection: an attacker hides an instruction in text your model reads and steers it. Retrieval poisoning: a malicious document lands in the corpus and gets pulled into context. Context poisoning: retrieved data carries an embedded instruction. Output injection: an unvalidated model response gets executed as a command downstream. Every one of these has put a real system on an incident call. Naming them is useful.

The diagram is also comprehensive-feeling, which is most of its appeal. It walks the pipeline stage by stage and hangs a threat under each stage, so it looks like a complete accounting. Nothing is missing. That completeness is exactly what makes the next step feel obvious, and the next step is the trap.

### The trap is in the layout

Read the diagram as a to-do list and it hands you one mitigation per box. Worse, the obvious mitigation for each box sits at the same stage as the threat, which is the stage the attacker controls.

- Prompt injection sits at the input, so you reach for input scanning and prompt hardening. The attacker writes the input.
- Retrieval poisoning sits at the corpus, so you reach for document scanning. The attacker writes the document.
- Context poisoning sits at the model's reading of the context, so you reach for a grounding or relevance score. You are now scoring meaning.
- Output injection sits at the output, so you reach for an output filter. That filter is a classifier.

Every one of those is a probabilistic classifier you tune against an adversary. Thresholds, false negatives, a curve you push toward zero and never reach. Build all four and you have not built a boundary. You have built four leaky nets stacked on top of each other and called the result secure. A determined injection that scores just under every threshold walks the whole length of the pipeline untouched.

And there is a tell that the list itself is the problem: it grows. Tool poisoning, memory injection, confused-deputy attacks across agents. Next quarter there is a fifth box, and a defence organised as one-patch-per-named-attack is permanently a step behind the naming. It is an open set sold to you as a closed one.

### The layer the diagram leaves out

Here is the fact the layout hides. All four attacks are harmless right up until model output crosses into a consequence: a tool call, a database query, a retrieval, or an answer leaving the building. That crossing is a single surface. It is finite, it is enumerable, and it is the one layer the attacker does not control, because it is your code.

The reflex the diagram trains looks like this, and it is quietly futile:

```python
def handle(user_input, ctx):
    if injection_score(user_input) > 0.9:        # tuned threshold
        raise Blocked()
    chunks = retrieve(user_input)
    chunks = [c for c in chunks if malice_score(c) < 0.9]   # tuned threshold
    answer = model(user_input, chunks)
    if output_score(answer) > 0.9:               # tuned threshold
        raise Blocked()
    return answer
```

Three gates, three thresholds, and an injection tuned to score 0.89 everywhere sails through all of them. Now the boundary version, at the layer the diagram skips:

```python
# Model output is a proposal, never an instruction. Every proposal is
# checked against the finite set of things allowed to happen.
def act_on(proposal, ctx):
    if proposal.tool not in TOOLS:          # enumerated capability, not a classifier
        raise NotAllowed(proposal.tool)
    check_authz(ctx.principal, proposal)    # token-derived principal, per call
    return TOOLS[proposal.tool](proposal.args, ctx)
```

And retrieval filtered before the model, keyed off the verified principal, so a chunk the principal is not cleared to see is never loaded in the first place:

```python
chunks = vector.search(q, k=20, filter={"acl": ctx.principal.entitlements})
```

Be precise about what this filter does and does not do. It is a confidentiality control, not an anti-poisoning one: it stops chunks outside the principal's entitlements, but a poisoned document that sits inside their authorised scope carries a valid ACL and passes. That residue is caught downstream, at the action boundary and by the grounding backstop, not here. What the filter does buy you, and it is the reason the fourth threat surface (an answer leaving the building) never needed its own allow-list, is that the model can only ever repeat data the principal was already cleared to retrieve. The answer is bounded on the way in, not policed on the way out.

None of this asks what the attack was called. Prompt injection, context poisoning, some technique that does not have a name yet: they all arrive at the same door, and the door checks the action against a list, not the intent against a classifier. The model can be fooled into proposing anything. It cannot be fooled into a proposal that the door has no entry for.

### Enumerate the actions, not the attacks

That is the whole reframe. You cannot enumerate the ways a model can be fooled. That set is open, adversarial, and growing while you read this. You can enumerate the actions your system is allowed to take: the tools in the registry, the tables in the grant, the entitlements on the index, the destinations on the egress allow-list. That set is small, closed, and yours to write down and review.

This is why the deterministic boundary is load-bearing and the classifiers are not. One defends a finite set you control. The other chases an infinite set the attacker controls. When people say defence in depth, they usually mean "more layers." The layer that matters is the one where an infinite problem becomes a finite one.

### Where the four boxes still earn their place

I am not telling you to throw the diagram out, and I want to be precise about the limits of my own argument, because the reframe oversells if you let it.

The boundary stops security failures, not correctness ones. If a poisoned document convinces the model to give a wrong but fully authorised answer, no allow-list catches that. The action is within the user's rights. It is just wrong. Grounding checks, provenance, and citation-of-source are the right tools there, and those are exactly the probabilistic layer I just spent five paragraphs demoting. Demoted, not deleted. They move from load-bearing to backstop, which is where they belong.

The boundary also constrains which actions and whose, not what rides inside them. An allowed tool called with attacker-shaped arguments is still an allowed tool: talk the model into calling a permitted `send_report` with an exfiltrating recipient, and a registry check that only asks "is this tool allowed" waves it through. This is why the action boundary is not just the tool list. It is the tool list, plus the grant, plus what each action is allowed to carry and where it is allowed to send it. Enumerate the arguments and the destinations too, not only the verbs. The egress allow-list is doing as much work as the tool registry.

Input and output scanning also earn a place. They raise the cost of the low-effort attacks and, more usefully, they give you signal to detect the attempt. Keep them. Just never let a tuned classifier be the only thing standing between the model and the data.

So the correction is not "the diagram is wrong." The threats are real and the diagram names them cleanly. The correction is "stop reading a threat map as a defence architecture." Put the deterministic boundary in first. Then let the classifiers backstop it, labelled honestly as backstops.

The companion post walks through where each of these lives in code. Here is the shape of the boundary itself, control by control:

![Dark infographic, six numbered rows, each a control in the deterministic boundary. 01 Propose never dispose: prompt to LLM to proposed action, not yet executed. 02 Tool allow-list: proposed call to allow-list check, named tool passes, anything else rejected. 03 Read-only SQL plus grant: query to read-only grant, SELECT only, writes rejected. 04 Per-call authz: each call checked individually, not cached once at session start. 05 ACL pre-filter: all rows to ACL filter, permitted rows only reach the model. 06 Probabilistic backstop: model output to content filter, response, boundary already held upstream.](/images/blog/2026-07-21_controls-recap.png)

### What I now do

- Before drawing a single mitigation, list the actions the system can take: tools, tables, entitlements, egress destinations. That list, not the attack list, is the security surface.
- Put a deterministic check on each action, at the point of the action, keyed off a verified principal, never off anything the model produced.
- Treat every input scanner, grounding check, and output filter as a backstop, and give it a name that says so in the design doc. Budget for it and tune it. Do not let it hold the line alone.
- When a new attack name starts trending, ask one question before building anything: does my action boundary already stop it? Most of the time the answer is yes, and the correct amount of new work is none.

The four-box diagram will keep circulating, because it is a genuinely good map of where the water comes in. Just remember that a map of the leaks is not a plan for the wall. The wall goes lower than the diagram draws it, at the layer the attacker cannot reach, and it is made of boring deterministic code that does not care what the flood is called.

---

*Part 6, and the close, of **Production AI, Honestly**: the series on the AWS AI work between the demo and something you'd trust a customer behind. It runs from measuring where the probabilistic layer lies (cost, caching, memory) to building the boundary that holds ([the LLM is not a security boundary](/blog/llm-is-not-a-security-boundary) walks those controls in detail) to this, the reframe of the diagram that had everyone patching the wrong layer. If you are building agents over sensitive data and want a second set of eyes on where your real boundary sits, [get in touch](/contact).*

**Next series, _Do You Trust It?_:** evals for production AI. This series measured and caged the probabilistic layer. The next one asks the question that comes after: how you actually know an agent is good once it is live, and why most eval dashboards measure the wrong thing.
