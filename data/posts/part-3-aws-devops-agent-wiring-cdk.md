---
title: "Part 3: Wiring It Into AWS DevOps Agent: AgentSpace, register-service, and the IAM Trust Policy That Ate My Afternoon"
description: "The MCP server is done. Now we plug it into AWS DevOps Agent: three CDK stacks, the AgentSpace + register-service flow, the composite-principal trust policy that you will get wrong on the first try, and a real-world OIDC gotcha that broke my own blog deploy for a month."
pubDate: 2026-05-01
tags: ["aws", "devopsagent", "cdk", "iam", "bedrock", "agentcore"]
pillars: ["agentic-ai", "enterprise-ai"]
series: "Building an AWS DevOps Agent that Knows Your Org"
seriesPart: 3
canonicalURL: "https://rajmurugan.com/blog/part-3-aws-devops-agent-wiring-cdk"
devToUrl: "https://dev.to/rajmurugan/part-3-wiring-it-into-aws-devops-agent-agentspace-register-service-and-the-iam-trust-policy-3e8m"
readTime: 14
---

[Part 1](/blog/part-1-aws-devops-agent-intent-vs-state) framed why an org-aware DevOps agent has to bridge state and intent. [Part 2](/blog/part-2-aws-devops-agent-mcp-layer) built the MCP server that holds the intent half. This post is the integration story: the CDK that takes that Lambda from "callable with curl" to "AWS DevOps Agent calls it automatically when an alarm fires."

Most of what's interesting in Part 3 is the IAM. AWS DevOps Agent is new enough that the trust-policy ergonomics aren't documented well, and a few of the moves you have to make are non-obvious. I'll show the working CDK, then walk through the three places I burned an afternoon.


I'll also close with a real OIDC gotcha I hit while deploying *this very blog post*, not in the demo system, in the rajmurugan.com pipeline. Same family of failure mode, different surface. It's the kind of thing you only see in production.

---

## The three-stack split, and why

The whole system is three CDK stacks deployed in order:

![Three CDK stacks deployed in order: KnowledgeBaseStack (S3 bucket, VectorKnowledgeBase, S3DataSource) exports KbId/KbArn to McpServerStack (ECR repo, Secrets Manager, Lambda DockerImage, Function URL) which exports FunctionUrl/ApiKeySecretArn to DevOpsAgentStack (OperatorAppRole, CfnAgentSpace, CfnService, CfnAssociation).](/images/blog/part-3/01-three-stack-cdk.svg)

Three stacks not because there are three of anything special, but because **the deploy order matters and it's much easier to enforce that with stacks than with tags inside one stack.** The KB has to exist before the Lambda, because the Lambda's IAM policy and env var both reference the KB. The Lambda has to exist before AgentSpace, because the `register-service` call wants the Function URL and the API key value. CDK respects the order via `cdk.Fn.importValue()` between stacks.

I picked CFN Exports over SSM Parameter Store for the cross-stack refs. SSM works too, and it gives you nicer ergonomics for refactoring across stack boundaries; the downside is that SSM lookups happen at *deploy* time, so a bad lookup is a deploy failure with a confusing error. CFN Exports are validated at synthesis, which surfaces the failure earlier. For a three-stack demo, exports are right. For a 30-stack platform, switch to SSM.

---

## KnowledgeBaseStack: the easy one

There's not much to say here. The CDK Labs `generative-ai-cdk-constructs` package gives you a `VectorKnowledgeBase` construct that wires the OpenSearch Serverless backing store and the embeddings model in one shot.

```typescript
import { bedrock } from '@cdklabs/generative-ai-cdk-constructs';

export class KnowledgeBaseStack extends cdk.Stack {
  public readonly bucket: s3.Bucket;
  public readonly knowledgeBase: bedrock.VectorKnowledgeBase;
  public readonly dataSource: bedrock.S3DataSource;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, 'KbDocs', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.knowledgeBase = new bedrock.VectorKnowledgeBase(this, 'Kb', {
      name: 'intent-guard-kb',
      embeddingsModel: bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_1024,
      instruction:
        'Intent Guard corpus: ADRs, incidents, planning docs, runbooks, ' +
        'and meeting notes. Return passages that document decisions, risk ' +
        'acceptances, and incidents relevant to the query.',
    });

    this.dataSource = new bedrock.S3DataSource(this, 'KbDocsDs', {
      bucket: this.bucket,
      knowledgeBase: this.knowledgeBase,
      dataSourceName: 'intent-guard-docs',
    });

    new cdk.CfnOutput(this, 'KbBucketName', { value: this.bucket.bucketName,
      exportName: 'IgKbBucketName' });
    new cdk.CfnOutput(this, 'KbId', { value: this.knowledgeBase.knowledgeBaseId,
      exportName: 'IgKbId' });
    new cdk.CfnOutput(this, 'KbArn', { value: this.knowledgeBase.knowledgeBaseArn,
      exportName: 'IgKbArn' });
  }
}
```

Two notes worth saying out loud:

**`removalPolicy: RETAIN`** on the bucket. The corpus is your org's institutional memory. Do not let `cdk destroy` take it with the stack. If you genuinely want to clean up, empty the bucket by hand first. A retained bucket on `cdk destroy` is the exact behaviour I want.

**The `instruction` string** ends up in the model's context when the agent reasons about whether to use this KB. Treat it like the docstrings in Part 2. Write it as if a model is reading it (because one is). I phrase mine as *what it has* and *what to return*, not *what kind of knowledge base this is*.

---

## McpServerStack: the chicken-and-egg, and the right Lambda shape

The MCP Lambda is a Docker image function, because the FastMCP + Mangum + boto3 stack is pip-installable but not zip-friendly at any reasonable size. Docker images on Lambda pull from ECR.

That sets up a chicken-and-egg problem you have to solve once: **CDK can create an ECR repo, but it can't deploy a Lambda that references an image that doesn't exist yet.** First-time deploy from a clean account fails with a vague "image not found" error.

The fix: bootstrap the ECR repo *before* `cdk deploy`. I do it in CI (a `bootstrap.sh` step the deploy workflow runs once if the repo isn't present), and I import the existing repo in the stack rather than creating it:

```typescript
this.repository = ecr.Repository.fromRepositoryName(this, 'McpImageRepo', 'intent-guard-mcp');
```

The image gets built and pushed to that ECR repo by the CI workflow, *then* `cdk deploy` runs. The CDK references the image by tag (`latest` for synth tests, `${git-sha}` for real deploys via `-c imageTag=...`).

The rest of the stack is reasonably standard:

```typescript
this.apiKeySecret = new secretsmanager.Secret(this, 'McpApiKey', {
  description: 'API key presented by the DevOps Agent to the MCP Lambda.',
  generateSecretString: {
    passwordLength: 48,
    excludePunctuation: true,
    excludeCharacters: '/@" \\',  // keep it URL-safe and shell-safe
  },
  removalPolicy: cdk.RemovalPolicy.DESTROY,
});

const kbId = cdk.Fn.importValue('IgKbId');
const kbArn = cdk.Fn.importValue('IgKbArn');

this.lambdaFunction = new lambda.DockerImageFunction(this, 'McpLambda', {
  functionName: 'intent-guard-mcp',
  code: lambda.DockerImageCode.fromEcr(this.repository, { tagOrDigest: imageTag }),
  architecture: lambda.Architecture.ARM_64,
  memorySize: 1024,
  timeout: cdk.Duration.seconds(30),
  environment: {
    KB_ID: kbId,
    MCP_API_KEY_SECRET_ARN: this.apiKeySecret.secretArn,
  },
});

this.apiKeySecret.grantRead(this.lambdaFunction);
this.lambdaFunction.addToRolePolicy(
  new iam.PolicyStatement({
    actions: ['bedrock:Retrieve', 'bedrock:RetrieveAndGenerate'],
    resources: [kbArn],
  }),
);

this.functionUrl = this.lambdaFunction.addFunctionUrl({
  authType: lambda.FunctionUrlAuthType.NONE,
  invokeMode: lambda.InvokeMode.BUFFERED,
});
```

A few opinionated choices:

**`ARM_64`** because it's cheaper at the same performance for boto3 + FastMCP workloads. Tested both architectures before settling.

**1024 MB memory.** Not because I need the memory, but because CPU on Lambda is allocated proportionally, and the FastMCP cold start at 512 MB was painful enough to notice. 1024 brought it down to about 800ms.

**30-second timeout.** Bedrock KB retrieval is fast (sub-second on a small corpus), but I want headroom for a slow API call without paging the user. Lambda's max is 15 minutes; tune to your retrieval p99.

**`generateSecretString` excludes `/`, `@`, `"`, space, `\`.** When AgentSpace builds the auth header, those characters cause grief: `@` gets URL-encoded inconsistently, slashes confuse some loggers. Restricting the alphabet costs you ~2 bits of entropy across 48 characters. Worth it.

**`grantRead` on the secret**, not just `secretsmanager:GetSecretValue`. The CDK helper sets up the resource policy on the secret too, which catches the case where someone else's stack creates the secret and your Lambda tries to read it. Always use the helper, never the raw policy statement.

---

## DevOpsAgentStack: where the real work happens

This is the stack that took me three days to get right. The CFN schema for AWS DevOps Agent (`AWS::DevOpsAgent::AgentSpace`, `AWS::DevOpsAgent::Service`, `AWS::DevOpsAgent::Association`) is straightforward when you know what to write. Knowing what to write is the hard part.

I'll show you the whole thing, then walk through the three gotchas.

```typescript
import * as devopsagent from 'aws-cdk-lib/aws-devopsagent';

export class DevOpsAgentStack extends cdk.Stack {
  public readonly agentSpace: devopsagent.CfnAgentSpace;
  public readonly mcpService: devopsagent.CfnService;
  public readonly association: devopsagent.CfnAssociation;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const mcpFunctionUrl = cdk.Fn.importValue('IgMcpFunctionUrl');
    const mcpApiKeySecretArn = cdk.Fn.importValue('IgMcpApiKeySecretArn');

    // ── Gotcha 1: composite trust policy ─────────────────────────
    const agentSpaceArnPattern = cdk.Stack.of(this).formatArn({
      service: 'aidevops',
      resource: 'agentspace',
      resourceName: '*',
      arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
    });

    const operatorAppRole = new iam.Role(this, 'OperatorAppRole', {
      roleName: 'intent-guard-operator-app',
      assumedBy: new iam.CompositePrincipal(
        new iam.PrincipalWithConditions(
          new iam.ServicePrincipal('aidevops.amazonaws.com'),
          {
            StringEquals: { 'aws:SourceAccount': cdk.Stack.of(this).account },
            ArnLike:      { 'aws:SourceArn': agentSpaceArnPattern },
          },
        ),
        new iam.AccountRootPrincipal(),
      ),
      description: 'Operator App role: aidevops service + account users.',
    });

    // ── Gotcha 2: explicit chat actions on the operator role ─────
    operatorAppRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ['aidevops:ListChats', 'aidevops:CreateChat', 'aidevops:SendMessage'],
        resources: ['*'],
      }),
    );

    this.agentSpace = new devopsagent.CfnAgentSpace(this, 'AgentSpace', {
      name: 'intent-guard-northwind',
      description: 'Intent Guard demo for the northwind-quote service.',
      operatorApp: { iam: { operatorAppRoleArn: operatorAppRole.roleArn } },
    });

    // ── Gotcha 3: secret resolved at deploy time, not in template ─
    const apiKeyValue = cdk.Token.asString(
      cdk.Fn.join('', [
        '{{resolve:secretsmanager:', mcpApiKeySecretArn, ':SecretString}}',
      ]),
    );

    this.mcpService = new devopsagent.CfnService(this, 'McpService', {
      serviceType: 'mcpserver',
      serviceDetails: {
        mcpServer: {
          name: 'intent-guard-mcp',
          endpoint: mcpFunctionUrl,
          description: 'Intent Guard MCP — ADRs, incidents, planning, meeting notes.',
          authorizationConfig: {
            apiKey: {
              apiKeyName: 'intent-guard-mcp-key',
              apiKeyValue,
              apiKeyHeader: 'X-API-Key',
            },
          },
        },
      },
    });
    this.mcpService.addDependency(this.agentSpace);

    this.association = new devopsagent.CfnAssociation(this, 'McpAssociation', {
      agentSpaceId: this.agentSpace.attrAgentSpaceId,
      serviceId:    this.mcpService.attrServiceId,
      configuration: {
        mcpServer: {
          name: 'intent-guard-mcp',
          endpoint: mcpFunctionUrl,
          tools: [
            'search_architectural_decisions_tool',
            'get_decision_details_tool',
            'check_risk_acceptance_status_tool',
            'get_related_incidents_tool',
          ],
        },
      },
    });
  }
}
```

Now the three gotchas, in the order they hit me.

---

### Gotcha 1: the composite trust policy

The first version of the OperatorAppRole I wrote had this trust policy:

```typescript
assumedBy: new iam.ServicePrincipal('aidevops.amazonaws.com'),
```

Deploy fails with:

```
Resource handler returned message: "SourceArn and SourceAccount Role
validation failed for OperatorAppRole. The trust policy doesn't include
either SourceArn or SourceAccount."
```

That's AWS's standard confused-deputy protection talking. When a service principal can be invoked across accounts, you have to constrain *which* resource is allowed to invoke it, and from *which* account. The condition keys for that are `aws:SourceArn` and `aws:SourceAccount`. AWS DevOps Agent rejects roles that don't have both.

Fine, except the `aws:SourceArn` you want is the AgentSpace's ARN, and the AgentSpace doesn't exist yet at the time the role is being created. The role *is a property* of the AgentSpace. Chicken meet egg.

The escape hatch: `aws:SourceArn` accepts wildcards, and `ArnLike` is a valid condition operator. So you write the condition against `agentspace/*` in your account:

```typescript
const agentSpaceArnPattern = cdk.Stack.of(this).formatArn({
  service: 'aidevops',
  resource: 'agentspace',
  resourceName: '*',
});

new iam.PrincipalWithConditions(
  new iam.ServicePrincipal('aidevops.amazonaws.com'),
  {
    StringEquals: { 'aws:SourceAccount': cdk.Stack.of(this).account },
    ArnLike:      { 'aws:SourceArn': agentSpaceArnPattern },
  },
)
```

This says: "the aidevops service can assume this role, but only when the source is an agentspace in this account." Which is what you want; it stops a different account's AgentSpace from somehow assuming your role. The `aws:SourceAccount` belt-and-braces is required by the service even though the `SourceArn` already implies it.

Then the *second* half of the trust: the IAM users who actually log into the Operator Web App. They sign in with their own credentials and the role is assumed on their behalf. So the trust policy also has to allow account principals:

```typescript
assumedBy: new iam.CompositePrincipal(
  new iam.PrincipalWithConditions(/* aidevops with conditions, above */),
  new iam.AccountRootPrincipal(),
),
```

`CompositePrincipal` ORs the principals together. The role can be assumed *either* by the aidevops service (with the source conditions) *or* by any IAM principal in this account. Both are needed. Drop either and the Operator Web App breaks in a different way.

This is the kind of thing that is one line in the docs once you know to look for it, and three days of poking at CloudTrail when you don't.

---

### Gotcha 2: the three explicit chat actions

The Operator Web App has a chat experience baked in. To use it, the role assumed by the operator needs three actions explicitly granted:

```typescript
operatorAppRole.addToPrincipalPolicy(
  new iam.PolicyStatement({
    actions: ['aidevops:ListChats', 'aidevops:CreateChat', 'aidevops:SendMessage'],
    resources: ['*'],
  }),
);
```

Without these, the Operator Web App loads, the role is assumable, but the chat panel just hangs and eventually shows an opaque "couldn't load chats" error. CloudTrail shows the `aidevops:ListChats` deny.

These three are *not* implied by any of the AWS-managed policies I tried (`AdministratorAccess` works, but you don't want operators running with that). There's no `AIDevOpsAgentChatUser` managed policy at the time of writing. Bake the three actions into your inline policy and move on.

---

### Gotcha 3: the secret has to be resolved at deploy time, not synth time

The `apiKeyValue` field on `CfnService.serviceDetails.mcpServer.authorizationConfig.apiKey` is a string. The naive thing to do is read the secret value and pass it in:

```typescript
// DON'T DO THIS
const secret = secretsmanager.Secret.fromSecretCompleteArn(
  this, 'McpApiKey', mcpApiKeySecretArn,
);
const apiKeyValue = secret.secretValue.unsafeUnwrap();  // returns a Token
```

The problem is the wording: "unsafeUnwrap". CDK is warning you that `secretValue` is a *Token*, and synthesising a Token into a string usually means it ends up as a plaintext value in your CloudFormation template. CFN templates land in S3 as part of the deploy. Having a plaintext API key in there is a leak.

The right pattern is a CFN dynamic reference. Dynamic references are special strings of the form `{{resolve:secretsmanager:<arn>:SecretString}}` that CloudFormation expands *at deploy time*, server-side, not at synth time. The plaintext never enters the template.

```typescript
const apiKeyValue = cdk.Token.asString(
  cdk.Fn.join('', [
    '{{resolve:secretsmanager:', mcpApiKeySecretArn, ':SecretString}}',
  ]),
);
```

`cdk.Fn.join` is used instead of plain string concatenation because `mcpApiKeySecretArn` is itself a Token (it came from `cdk.Fn.importValue('IgMcpApiKeySecretArn')`). Concatenating it with `+` would synthesise the Token in the wrong context.

When CFN deploys this stack, it sees the dynamic reference, calls Secrets Manager itself, and inlines the plaintext only into the final resource, not into the template. The synth output and CloudTrail both see only the dynamic-reference string. The plaintext is never written down anywhere it shouldn't be.

This pattern is general: it works for any field that takes a string and shouldn't have a plaintext secret in it. Worth keeping in your back pocket for any CDK code that touches credentials.

---

## The webhook forwarder: turning alarms into agent calls

There's one more component I haven't shown, because it's small enough to fit in a sidebar: the webhook forwarder.

When you wire up Cloudwatch / PagerDuty / Dynatrace / ServiceNow as triggers for the agent, they each speak a different webhook payload format. AWS DevOps Agent expects a specific shape. The webhook forwarder is a 50-line Lambda with a Function URL that:

1. Validates an `X-Signature` header (HMAC-SHA256 with a secret from Secrets Manager) so the endpoint can't be replayed by random internet traffic.
2. Normalises the upstream payload into the agent's expected shape.
3. Posts to the agent's runtime endpoint.

I'll write this up as its own short post. It's not specific to Intent Guard, it's a useful building block whenever you want to plug N event sources into one downstream consumer.

The break-glass pattern is also here: the webhook forwarder reads an SSM parameter on every invocation. Set the parameter to `paused` and the forwarder drops events on the floor. Set it to `live` and it forwards. Operators can flip the switch without a redeploy, which is the entire point of break-glass.

---

## A real OIDC gotcha I hit on this very blog

I'll close with a story that's not from Intent Guard, but is exactly the same family of failure you'll hit when wiring DevOps Agent up to your own infrastructure. It happened on the rajmurugan.com pipeline that's hosting this very post.

The site deploys via GitHub Actions to S3 + CloudFront, using a GitHub OIDC role for AWS auth (no stored credentials). The role's trust policy was fine:

```json
{
  "Effect": "Allow",
  "Principal": { "Federated": "arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com" },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
    "StringLike":   { "token.actions.githubusercontent.com:sub":
                      "repo:rajmurugan01/rajmurugan-site:ref:refs/heads/main" }
  }
}
```

Every deploy since the role was created had failed with:

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

I'd assumed the OIDC provider was busted, or the role had a typo. Neither. The actual cause was one line in `.github/workflows/deploy.yml`:

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production    # ← this line
```

When a workflow declares an `environment:`, GitHub's OIDC provider issues a JWT with `sub` set to **`repo:.../environment:production`**, *not* `repo:.../ref:refs/heads/main`. The two are mutually exclusive: you get one or the other depending on whether the workflow scopes itself to an environment.

My trust policy expected the ref form. The JWT had the environment form. They never matched. Every deploy failed for a month before I caught it.

The fix was a one-line workflow edit (drop the `environment: production` line, since I wasn't using environment-scoped secrets yet) and the next push deployed cleanly.

The reason I'm telling you this in a Part 3 about wiring DevOps Agent: **OIDC trust policies are JWT-claim-matching, and the JWT shape depends on configuration you don't always notice.** If you're plugging an external service into your AWS account and authentication is silently failing, the answer is almost never "the role doesn't exist". It's "the trust policy condition doesn't match the actual claim shape." Dump the JWT (you can do that in a CI step before the assume-role attempt), look at what's actually in `sub` and `aud`, and adjust.

This shape of debugging applies as much to the AWS DevOps Agent service principal trust as it does to GitHub OIDC. Same pattern, different surface.

---

## Where this leaves you

Put the three stacks together and you have:

- A Bedrock Knowledge Base that ingests `data/**/*.md` from S3.
- An MCP Lambda exposing four tools (Part 2) over Streamable HTTP with API-key auth.
- An AWS DevOps Agent AgentSpace bound to that MCP via `register-service`, with the four tools whitelisted in the Association config.
- An Operator Web App at `https://aidevops.console.aws.amazon.com/...` that an SRE can sign into to ask questions.
- A webhook forwarder that turns CloudWatch / PagerDuty / Dynatrace alarms into agent invocations directly, no human click required.

![Four-step trace: 01 Alarm fired (log-metric filter counting ADR-004 error pattern against App Runner logs). 02 Webhook delivered (HMAC-signed; Lambda forwarder signs and routes; no human clicked anything). 03 Investigation auto-started (correlated App Runner logs, CloudTrail, Bedrock Knowledge Base: ADR-004, 60 days overdue). 04 Recommended the break-glass the ADR itself documented (cited the ADR ID and the log line; wrong citation would be visible, not silent).](/images/blog/part-3/02-what-just-happened.png)

The Northwind scenario from Part 1 plays out end to end:

1. CloudWatch alarm fires on the `/tweak` endpoint's error rate.
2. Webhook forwarder signs and forwards to the agent.
3. Agent calls `check_risk_acceptance_status_tool(service="northwind-quote")`.
4. MCP returns the structured finding for ADR-004, sixty days overdue.
5. Agent calls `get_related_incidents_tool(query="bedrock throttling", signals=["bedrock_throttling"])`.
6. MCP returns matching incident reports.
7. Agent composes a response citing the ADR ID, the days_overdue figure, and the relevant incident, with the break-glass recommendation lifted directly from the runbook the ADR linked to.
8. Operator sees this in chat about ninety seconds after the alarm fired, with no human in the path between alarm and answer.

That's the bar Part 1 set. We're there.

---

## Where this *doesn't* leave you

Three things I'm not pretending this system does, before someone takes it to production and gets bitten.

**Auto-remediation is not in the loop.** The agent surfaces a recommendation; a human runs it. You can wire it to automation, and the SSM-driven break-glass pattern is exactly the right hook for that, but the demo I built keeps the human in. For incident response, that's the boundary I want.

**Multi-account org graph isn't here.** A real org has dozens of accounts, and your ADRs probably reference resources across them. The version I've shown is single-account. The pattern generalises (you make the MCP tools cross-account by the role they assume, not by the data they hold) but the demo doesn't show it.

**Eval harness isn't here.** The agent's answers are right roughly nine times in ten on the question shapes I tested. *Nine times in ten is not good enough for unattended automation.* You want a proper eval harness that scores retrieval quality and citation accuracy on a held-out test set before this thing runs without supervision. I'll write that up separately; it's its own post.

---

## Closing

The thing I want you to take away from the whole series:

Generic AI agents see *state*. Org-aware AI agents see *intent and state*, and the bridge between them is a typed query layer over your team's documented decisions. Your monitoring tools handle the state half. They always have. The intent half is the work.

The architecture I've shown (Bedrock KB + frontmatter-aware MCP + AWS DevOps Agent + signed webhook forwarder) is one way to do that bridge. There are others. The specifics matter less than the principle: **do not stuff your wiki into the system prompt. Build a typed retrieval surface, make metadata the contract, and let the agent ask.**

If you build something similar, I'd love to see it. I'm at [github.com/rajmurugan01](https://github.com/rajmurugan01) and on [dev.to](https://dev.to/rajmurugan).

That's the series. Thanks for reading.
