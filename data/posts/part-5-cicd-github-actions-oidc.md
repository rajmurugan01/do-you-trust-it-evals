---
title: "Part 5: CI/CD for Bedrock AgentCore with GitHub Actions and AWS OIDC (No Stored Credentials)"
description: "How to build a complete CI/CD pipeline for AgentCore using GitHub Actions OIDC: no stored AWS keys, dual-tag ECR strategy, automated Runtime updates, and multi-environment promotion."
pubDate: 2025-11-05
tags: ["github-actions", "aws", "cicd", "oidc", "ecr", "bedrock", "agentcore"]
pillars: ["agentic-ai"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 5
canonicalURL: "https://rajmurugan.com/blog/part-5-cicd-github-actions-oidc"
devToUrl: "https://dev.to/rajmurugan/part-5-cicd-for-bedrock-agentcore-with-github-actions-and-aws-oidc-no-stored-credentials-3cc1"
readTime: 10
---

Storing AWS access keys in GitHub Secrets is the wrong approach. They rotate, they get leaked, and they're a compliance headache.

The correct approach in 2025 is OIDC: GitHub Actions proves its identity to AWS using a short-lived token, assumes an IAM role, and gets temporary credentials. No stored keys, no rotation, no secrets to leak.

This post walks through the complete CI/CD setup for AgentCore: OIDC config, the build/push/deploy pipeline, and the dual-tag ECR strategy that makes rollback practical.

---

## Why OIDC over stored credentials

With stored `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`:
- Keys are long-lived (you rotate them, right? right?)
- Rotation requires updating secrets in every affected repo
- A leak (accidental commit, log output, third-party action) gives an attacker permanent access until rotated
- Keys are attached to an IAM user, so you need a separate user per CI/CD system

With OIDC:
- GitHub generates a short-lived OIDC token per workflow run
- AWS validates the token against the trusted identity provider
- IAM role is assumed: credentials expire in 1 hour maximum
- No secrets to rotate, no keys to leak
- Trust policy is scoped to specific repos and branches

---

## Setting up OIDC

### Step 1: Create the IAM OIDC provider (once per AWS account)

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

This tells AWS to trust tokens from `token.actions.githubusercontent.com`.

### Step 2: Create the deploy IAM role

The trust policy scopes the OIDC trust to your specific repo:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub":
            "repo:rajmurugan01/bedrock-agentcore-starter:*"
        }
      }
    }
  ]
}
```

The `StringLike` condition with `*` allows any branch. For production deployments, lock it down:

```json
"StringEquals": {
  "token.actions.githubusercontent.com:sub":
    "repo:rajmurugan01/bedrock-agentcore-starter:ref:refs/heads/main"
}
```

### Step 3: Attach permissions to the deploy role

The role needs:
- `ecr:GetAuthorizationToken`: login to ECR
- `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:PutImage`, etc.: push to ECR
- `bedrock-agentcore-control:UpdateAgentRuntime`: update the Runtime after pushing a new image
- `ssm:GetParameter`: read Runtime ID and other config from SSM

---

## The deploy workflow

The full file is [.github/workflows/deploy-agent.yml](https://github.com/rajmurugan01/bedrock-agentcore-starter/blob/main/.github/workflows/deploy-agent.yml).

Key sections:

### Trigger

```yaml
on:
  push:
    branches: [main]
    paths:
      - 'apps/customer-service-agent/**'
  workflow_dispatch:
    inputs:
      environment:
        type: choice
        options: [dev, stg, prd]
```

The `paths` filter means the workflow only triggers when agent code changes, not on every push to main. Infrastructure changes (CDK) run in a separate workflow.

### OIDC credential configuration

```yaml
permissions:
  id-token: write   # Required to receive the OIDC token
  contents: read

steps:
  - name: Configure AWS credentials
    uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
      aws-region: us-east-1
```

The `id-token: write` permission is what enables OIDC. Without it, GitHub doesn't generate the OIDC token and the step fails.

### Build for linux/amd64

```yaml
- name: Build Docker image
  working-directory: apps/customer-service-agent
  run: |
    docker build \
      --platform linux/amd64 \
      -t ${{ env.ECR_URI }}:latest \
      -t ${{ env.ECR_URI }}:${{ env.GIT_SHA }} \
      .
```

This produces two tags simultaneously in one build, no rebuilding.

### The dual-tag ECR strategy

```yaml
- name: Push to ECR
  run: |
    docker push ${{ env.ECR_URI }}:latest
    docker push ${{ env.ECR_URI }}:${{ env.GIT_SHA }}
```

**`:latest`**. AgentCore always pulls `:latest` when you call `update-agent-runtime`. This tag must always point to the most recent image.

**`:<git-sha>`** (e.g., `:a1b2c3d4`): pinned to a specific commit. If `:latest` introduces a regression, you can roll back by pushing the previous SHA tag as `:latest`:

```bash
# Rollback to a previous image
docker pull <ecr-uri>:a1b2c3d4
docker tag <ecr-uri>:a1b2c3d4 <ecr-uri>:latest
docker push <ecr-uri>:latest
# Then trigger update-agent-runtime again
```

### Updating the AgentCore Runtime

After pushing the image, we tell AgentCore to pull the new `:latest`:

```yaml
- name: Update AgentCore Runtime
  run: |
    RUNTIME_ID=$(aws ssm get-parameter \
      --name "/customerServiceAgent/${{ env.ENVIRONMENT }}/runtime-id" \
      --query Parameter.Value --output text)

    aws bedrock-agentcore-control update-agent-runtime \
      --agent-runtime-id "${RUNTIME_ID}" \
      --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"${{ env.ECR_URI }}:latest"}}' \
      --role-arn "${{ secrets.EXECUTION_ROLE_ARN }}" \
      --network-configuration '{"networkMode":"VPC","networkModeConfig":{"securityGroups":["${{ secrets.AGENT_SECURITY_GROUP_ID }}"],"subnets":["${{ secrets.AGENT_SUBNET_IDS }}"]}}' \
      --region us-east-1
```

Remember Gotcha #7 from Part 2: `--role-arn` and `--network-configuration` are both mandatory. The `--role-arn` is the **execution role** (the role AgentCore uses at runtime), not the deploy role the workflow is running as.

---

## The CI workflow

Runs on every push and pull request:

```yaml
# .github/workflows/ci.yml
jobs:
  lint-python:
    steps:
      - run: pip install ruff black
      - run: ruff check customer_service_agent/
      - run: black --check customer_service_agent/

  test-infra:
    steps:
      - run: npm ci
      - run: npm test              # Jest CDK unit tests
      - run: npm run synth         # CDK synth smoke test
```

The CDK synth must succeed without AWS credentials. This works as long as `cdk.context.json` is committed to the repo: it contains the VPC lookup cache that CDK needs for deterministic synthesis.

If `cdk.context.json` is missing (or the VPC lookup context changed), CDK will try to call the AWS API during synth and fail in CI. Regenerate it locally: `cdk context --clear && cdk synth`.

---

## Multi-environment promotion

The `workflow_dispatch` trigger lets you manually promote a build:

```yaml
on:
  workflow_dispatch:
    inputs:
      environment:
        required: true
        type: choice
        options: [dev, stg, prd]
```

Combined with GitHub Environments (configured in repository Settings → Environments), you can require manual approval before deploying to `stg` or `prd`:

1. Push to `main` → auto-deploys to `dev`
2. Manually trigger workflow → select `stg` → GitHub requires approval from reviewers
3. After approval → deploys to `stg`
4. Manual trigger → select `prd` → same approval gate

The `environment:` key in the job declaration activates the GitHub Environment's protection rules:

```yaml
jobs:
  deploy:
    environment: ${{ inputs.environment || 'dev' }}
```

---

## GitHub Secrets to configure

| Secret | Where it comes from |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | ARN of the OIDC role you created |
| `EXECUTION_ROLE_ARN` | CDK output `ExecutionRole` ARN |
| `AGENT_SECURITY_GROUP_ID` | CDK output Security Group ID |
| `AGENT_SUBNET_IDS` | CDK output subnet IDs (comma-separated) |

These are repo-level secrets (Settings → Secrets and variables → Actions). For multi-environment setups, use environment-level secrets to have different values per environment.

---

## End-to-end flow

```
Developer pushes to main
    ↓
GitHub Actions: ci.yml runs (lint + CDK tests, ~2 min)
    ↓
GitHub Actions: deploy-agent.yml triggers (paths: apps/**)
    ↓
Configure AWS credentials (OIDC, ~10s)
    ↓
docker build --platform linux/amd64 (~3-5 min)
    ↓
docker push :latest + :<sha> to ECR (~1-2 min)
    ↓
update-agent-runtime CLI (~30s)
    ↓
AgentCore pulls new image, restarts container instances
    ↓
New code is live
```

Total time from push to live: ~8-10 minutes.

In the final part, we look at cost: how much this system actually costs to run, where prompt caching saves the most, and how to set CloudWatch alarms before your bill surprises you.

→ **[Continue to Part 6: Cost & Performance](/blog/part-6-cost-performance-prompt-caching)**
