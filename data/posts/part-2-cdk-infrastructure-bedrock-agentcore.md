---
title: "Part 2: CDK Infrastructure for Amazon Bedrock AgentCore (And Every Gotcha You'll Hit)"
description: "A complete CDK v2 TypeScript stack for Bedrock AgentCore, with inline comments for every deployment trap: naming constraints, ECR bootstrap, missing L1 constructs, VPC endpoint conflicts, and more."
pubDate: 2025-10-26
tags: ["aws", "cdk", "typescript", "bedrock", "agentcore"]
pillars: ["agentic-ai"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 2
canonicalURL: "https://rajmurugan.com/blog/part-2-cdk-infrastructure-bedrock-agentcore"
devToUrl: "https://dev.to/rajmurugan/part-2-cdk-infrastructure-for-amazon-bedrock-agentcore-and-every-gotcha-youll-hit-393h"
readTime: 14
---

This is the post I wish had existed when I was debugging my first AgentCore CDK deploy at midnight.

AgentCore is a relatively new service and CDK support is still catching up to the API. There are at least 9 specific traps that will silently fail, throw cryptic errors, or leave your CloudFormation stack in an unrecoverable state if you don't know about them.

I'm going to walk through the complete CDK stack and call out every one of them.

The full source is in [infra/lib/agentcore-stack.ts](https://github.com/rajmurugan01/bedrock-agentcore-starter/blob/main/infra/lib/agentcore-stack.ts) in the demo repo.

---

## The stack: what we're creating

```
CustomerServiceAgentStack-dev
├── KMS Key (rotation enabled, RETAIN on delete)
├── CloudWatch LogGroup — /aws/bedrock-agentcore/runtimes/...
├── CloudWatch LogGroup — /aws/bedrock/model-invocations/...
├── IAM Role (InvocationLogging) — bedrock.amazonaws.com principal
├── CfnResource — AWS::Bedrock::ModelInvocationLoggingConfiguration ← Gotcha #3
├── SNS Topic — cost alarm notifications
├── CloudWatch Alarm — OutputTokenCount
├── CloudWatch Alarm — InvocationLatency
├── Bedrock CfnGuardrail + CfnGuardrailVersion
├── CfnResource — AWS::BedrockAgentCore::Memory (3 strategies)  ← Gotcha #8
├── ECR Repository (IMPORTED, not created)                       ← Gotcha #2
├── IAM Role (ExecutionRole) — bedrock-agentcore.amazonaws.com
├── Security Group (allowAllOutbound: false)                     ← Gotcha #6
├── CfnResource — AWS::BedrockAgentCore::AgentRuntime
└── SSM Parameters (7 parameters for all ARNs/IDs)
```

Let's go through each section and the gotchas that come with it.

---

## Project setup

```
infra/
├── bin/app.ts
├── lib/agentcore-stack.ts
├── test/agentcore-stack.test.ts
├── jest.config.js
├── package.json
├── tsconfig.json
└── cdk.json
```

`package.json` needs `aws-cdk-lib` and `constructs` as dependencies, plus `jest` + `ts-jest` as devDependencies for the unit tests.

---

## Gotcha #1: AgentCore naming, NO HYPHENS

This one is not in the documentation in any obvious place. AgentCore Runtime names, Memory names, and Memory strategy names must match this regex:

```
^[a-zA-Z][a-zA-Z0-9_]{0,47}$
```

Hyphens fail at deploy time with a cryptic CloudFormation validation error:

```
Value 'customer-service-agent-dev' at 'agentRuntimeName' failed to satisfy
constraint: Member must satisfy regular expression pattern: [a-zA-Z][a-zA-Z0-9_]{0,47}
```

The fix is simple: use camelCase or underscores:

```typescript
// ❌ This will fail at deploy time
const runtimeName = `customer-service-agent-${env}`;

// ✅ This works
const runtimeName = `customerServiceAgent_${env}`;
const memoryName  = `customerServiceAgentMemory_${env}`;
```

This applies to **every** naming field in AgentCore: Runtime names, Memory names, and the names of individual Memory strategies.

---

## Gotcha #2: ECR chicken-and-egg

This one will waste a full deploy cycle if you don't know about it.

AgentCore Runtime requires a **valid container image at `:latest`** when the `CfnRuntime` resource is created. The timing problem: CDK creates both the ECR repo and the Runtime in the same deploy → the Runtime creation fails because the ECR repo is empty.

**The fix** is a two-step process:

**Step 1**: Run the bootstrap script once before the first `cdk deploy`:

```bash
# infra/scripts/bootstrap-ecr.sh

aws ecr create-repository \
  --repository-name "customerserviceagent/runtime-dev" \
  --region us-east-1

# Push a placeholder (any valid linux/amd64 image)
docker pull --platform linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2
docker tag public.ecr.aws/amazonlinux/amazonlinux:2 \
  <account>.dkr.ecr.us-east-1.amazonaws.com/customerserviceagent/runtime-dev:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/customerserviceagent/runtime-dev:latest
```

**Step 2**: In CDK, **import** the repo, never create it:

```typescript
// ✅ Import (repo already exists from bootstrap script)
const agentRepo = ecr.Repository.fromRepositoryName(this, 'AgentRepo', ecrRepoName);

// ❌ Don't do this — CDK would create it empty and the Runtime deploy would fail
const agentRepo = new ecr.Repository(this, 'AgentRepo', { ... });
```

---

## Gotcha #3: No CDK L1 for `ModelInvocationLoggingConfiguration`

If you try to enable Bedrock model invocation logging, you'll find that `aws-cdk-lib` (up to 2.245.0) has no L1 construct for `AWS::Bedrock::ModelInvocationLoggingConfiguration`.

Searching the CDK docs or autocomplete for `CfnModelInvocationLoggingConfiguration` returns nothing. You must use raw `CfnResource`:

```typescript
// ❌ This doesn't exist in aws-cdk-lib
import { CfnModelInvocationLoggingConfiguration } from 'aws-cdk-lib/aws-bedrock';

// ✅ Use raw CfnResource with the CloudFormation type string
new cdk.CfnResource(this, 'ModelInvocationLogging', {
  type: 'AWS::Bedrock::ModelInvocationLoggingConfiguration',
  properties: {
    LoggingConfig: {
      CloudWatchConfig: {
        LogGroupName: invocationLogGroup.logGroupName,
        RoleArn: invocationLoggingRole.roleArn,
      },
      TextDataDeliveryEnabled: true,
      ImageDataDeliveryEnabled: false,
      EmbeddingDataDeliveryEnabled: false,
    },
  },
});
```

The IAM role for this must have `logs:CreateLogStream` and `logs:PutLogEvents` on the log group ARN, and it must be assumed by `bedrock.amazonaws.com`.

---

## Gotcha #4: VPC endpoints, don't recreate existing ones

AgentCore runs inside a VPC. It needs to reach Bedrock, ECR, and SSM without going through the public internet (for both performance and security).

The trap: if your VPC was provisioned by Terraform or another CDK stack, it may already have interface endpoints for Bedrock, ECR, and S3. Creating duplicate interface endpoints with the same private DNS name fails with:

```
private-dns-enabled cannot be set because there is already a conflicting
DNS domain for bedrock-runtime.us-east-1.amazonaws.com in this VPC
```

**For managed VPCs**: Use `Vpc.fromLookup()` and **skip creating VPC endpoints**. Assume they already exist.

**For the demo (default VPC)**: No pre-existing endpoints, so we create the minimum needed:

```typescript
const vpc = ec2.Vpc.fromLookup(this, 'DefaultVpc', { isDefault: true });

// Only add endpoints if they don't already exist in your VPC
vpc.addInterfaceEndpoint('BedrockRuntimeEndpoint', {
  service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
  securityGroups: [agentSg],
});

vpc.addGatewayEndpoint('S3Endpoint', {
  service: ec2.GatewayVpcEndpointAwsService.S3,
});
```

---

## Gotcha #5: KMS + CloudWatch LogGroup key policy

If you want to encrypt a CloudWatch LogGroup with a customer-managed KMS key, the key policy **must** explicitly grant `logs.amazonaws.com` permission to use the key:

```typescript
// The key policy must include this:
kmsKey.addToResourcePolicy(new iam.PolicyStatement({
  principals: [new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
  actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
  resources: ['*'],
}));
```

Without this, CloudWatch silently fails to encrypt logs (or the log group creation fails with a KMS access denied error).

**For most use cases, skip the CMK entirely.** CloudWatch uses AWS-managed encryption by default. The only reason to add a CMK is if you have a compliance requirement that mandates customer-controlled key rotation.

---

## Gotcha #6: Security Group egress is inline, not a separate resource

This one catches you in CDK unit tests, not in the actual deployment.

When using `allowAllOutbound: false` and calling `addEgressRule(Peer.ipv4(cidr), Port.tcp(443))`, CDK embeds the egress rule **inside** the `SecurityGroup` resource's `SecurityGroupEgress` array:

```typescript
const agentSg = new ec2.SecurityGroup(this, 'AgentSg', {
  vpc,
  allowAllOutbound: false,  // Disables default egress-all rule
});

agentSg.addEgressRule(
  ec2.Peer.ipv4(vpc.vpcCidrBlock),
  ec2.Port.tcp(443),
  'HTTPS to VPC CIDR',
);
```

There is **no separate `AWS::EC2::SecurityGroupEgress` resource** created for this. In CDK assertions tests:

```typescript
// ❌ This will find 0 resources — it doesn't exist as a separate resource
template.hasResource('AWS::EC2::SecurityGroupEgress', {});

// ✅ Check the inline array inside the SecurityGroup resource
template.hasResourceProperties('AWS::EC2::SecurityGroup', {
  SecurityGroupEgress: Match.arrayWith([
    Match.objectLike({ IpProtocol: 'tcp', FromPort: 443, ToPort: 443 }),
  ]),
});
```

Note: a **separate** `AWS::EC2::SecurityGroupEgress` resource IS created when you reference another security group as the peer (cross-SG rules). This only applies to IP/CIDR-based rules.

---

## Gotcha #7: `update-agent-runtime` requires `--role-arn` and `--network-configuration`

After deployment, when you push a new Docker image and want AgentCore to pick it up, you call `update-agent-runtime`. Both `--role-arn` and `--network-configuration` are now **mandatory**:

```bash
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id <runtime-id> \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ecr>:latest"}}' \
  --role-arn arn:aws:iam::<account>:role/customerServiceAgentExecutionRole-dev \
  --network-configuration '{
    "networkMode": "VPC",
    "networkModeConfig": {
      "securityGroups": ["sg-xxx"],
      "subnets": ["subnet-aaa", "subnet-bbb"]
    }
  }' \
  --region us-east-1
```

Omitting either `--role-arn` or `--network-configuration` gives:
```
ValidationException: Missing required field: roleArn
```

The `--role-arn` here is the **execution role** (the role the Runtime assumes to pull from ECR and call Bedrock), not the deploy role your CLI is using.

---

## Gotcha #8: Memory stuck in `CREATING` during rollback

If your CDK deploy fails **after** the AgentCore Memory resource starts creating, the CloudFormation rollback will also fail. The Memory resource is in `CREATING` state and CloudFormation can't delete it.

You'll see this error in the CloudFormation events:
```
DELETE_FAILED: Cannot delete resource while it is in CREATING state
```

Recovery steps:
```bash
# 1. Find the stuck memory
aws bedrock-agentcore-control list-memories --region us-east-1

# 2. Wait for it to finish creating (usually a few minutes), then delete
aws bedrock-agentcore-control delete-memory --memory-id <id> --region us-east-1

# 3. Delete the stuck CloudFormation stack
aws cloudformation delete-stack --stack-name CustomerServiceAgentStack-dev --region us-east-1

# 4. Retry
cdk deploy
```

---

## Gotcha #9: `arrayWith` in CDK assertions is order-sensitive

This one only matters if you write CDK unit tests, but it will confuse you when it does.

`Match.arrayWith([patternA, patternB])` requires the elements to appear in the **same order** as in the synthesised template:

```typescript
// The template has filtersConfig: [PROMPT_ATTACK, HATE, INSULTS, SEXUAL, VIOLENCE]

// ✅ Works — PROMPT_ATTACK before HATE
Match.arrayWith([
  Match.objectLike({ Type: 'PROMPT_ATTACK' }),
  Match.objectLike({ Type: 'HATE' }),
])

// ❌ Fails — even though both are present, order doesn't match the template
Match.arrayWith([
  Match.objectLike({ Type: 'HATE' }),
  Match.objectLike({ Type: 'PROMPT_ATTACK' }),
])
```

The fix: write your `Match.arrayWith` patterns in the same order as the properties appear in your CDK code.

---

## The CDK unit test strategy

With all these gotchas, testing matters. Here's the approach from [infra/test/agentcore-stack.test.ts](https://github.com/rajmurugan01/bedrock-agentcore-starter/blob/main/infra/test/agentcore-stack.test.ts):

1. **Snapshot test**: the primary safety net. Any change to the synthesised template fails CI until explicitly updated.
2. **Naming regex test**: verify Runtime and Memory names match the no-hyphens regex.
3. **IAM trust test**: verify `bedrock-agentcore.amazonaws.com` is the principal on the execution role.
4. **Security Group inline egress test**: verify the pattern from Gotcha #6.
5. **SSM parameter tests**: verify all 7 parameters exist with the correct paths.
6. **Protocol test**: verify `ServerProtocol: 'HTTP'`.
7. **Guardrail filter order test**: verify filters appear in the correct order (Gotcha #9).

Run tests with:
```bash
cd infra && npm test
```

For deterministic synthesis without AWS credentials, commit `cdk.context.json` (the VPC lookup cache). Without it, CDK would try to call the AWS API during `cdk synth`, breaking CI.

---

## Deploying

```bash
cd infra
npm install

# First time only
./scripts/bootstrap-ecr.sh    # Gotcha #2 — must run before cdk deploy

# Deploy
export CDK_DEFAULT_ACCOUNT=<your-account-id>
export ENVIRONMENT=dev
cdk deploy
```

The deploy takes 5-10 minutes. The Memory resource is the slowest to provision (~3-4 minutes).

In Part 3, we write the Python agent that runs inside the container AgentCore manages.

→ **[Continue to Part 3: Building the Agent with Strands SDK](/blog/part-3-strands-agent-sdk)**
