---
title: "Part 4: Running Your AgentCore Agent Locally with Docker (The Right Way)"
description: "How to build and run your AgentCore container locally with real AWS credentials, the correct linux/amd64 platform flag, the .env.local pattern, and how to test with curl."
pubDate: 2025-11-02
tags: ["docker", "aws", "bedrock", "agentcore", "python", "localdev"]
pillars: ["agentic-ai"]
series: "Ultimate Guide to Building AI Agents on AWS with Bedrock AgentCore"
seriesPart: 4
canonicalURL: "https://rajmurugan.com/blog/part-4-local-dev-docker"
devToUrl: "https://dev.to/rajmurugan/part-4-running-your-agentcore-agent-locally-with-docker-the-right-way-1cc8"
readTime: 7
---

You've written the agent code. Before pushing it to ECR and waiting for AgentCore to pull it, you want to run it locally and confirm it actually works.

This part covers the local Docker dev loop, including a critical flag that's easy to miss and silently produces the wrong result.

---

## The `--platform linux/amd64` requirement

This is the most important thing in this entire post.

Amazon Bedrock AgentCore runtime is **x86_64 only**. If you build your Docker image without specifying the platform, Docker uses your host architecture:

- On an Intel Mac or Linux x86_64 machine: builds `linux/amd64` ✅
- On an Apple Silicon Mac (M1/M2/M3/M4): builds `linux/arm64` ❌

The arm64 image will work perfectly in your local Docker test because your Mac is arm64. But when AgentCore pulls `:latest` and tries to run it on an x86_64 host, the container exits immediately with an exec format error, and AgentCore reports the Runtime as `FAILED` with a cryptic message.

**Always build with `--platform linux/amd64`:**

```bash
docker build --platform linux/amd64 -t customer-service-agent:local .
```

This forces Docker to produce an x86_64 image regardless of your host architecture. On an Apple Silicon Mac, Docker uses QEMU emulation to run the build. It's a bit slower but produces the correct output.

---

## The Dockerfile

The `Dockerfile` is in `apps/customer-service-agent/`:

```dockerfile
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY customer_service_agent/ ./customer_service_agent/

# AgentCore always calls port 8080 — this is not configurable
EXPOSE 8080

# Health check — AgentCore probes GET /health before routing traffic
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["uvicorn", "customer_service_agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Two things to note:
1. `--platform=linux/amd64` in the `FROM` line ensures the base image is also x86_64
2. Port 8080 is hardcoded: AgentCore doesn't let you configure this

---

## The `.env.local` pattern

In production, AgentCore injects environment variables from the `EnvironmentVariables` block you set in the CDK `CfnRuntime` resource. Locally, we replicate this with a `.env.local` file.

Copy `.env.local.example` to `.env.local` and fill in the values from your CDK stack outputs:

```bash
# .env.local
AGENTCORE_MEMORY_ID=xxxxxxxxxxxxxxxxxxxx
BEDROCK_GUARDRAIL_ID=xxxxxxxxxxxxxxxx
BEDROCK_GUARDRAIL_VERSION=1
AWS_REGION=us-east-1
ENVIRONMENT=dev
LOG_LEVEL=DEBUG
```

Get the values from CDK outputs or SSM:
```bash
aws ssm get-parameter --name /customerServiceAgent/dev/memory-id --query Parameter.Value --output text
aws ssm get-parameter --name /customerServiceAgent/dev/guardrail-id --query Parameter.Value --output text
```

---

## Running the container locally

```bash
cd apps/customer-service-agent

# Build for linux/amd64 (even on an Apple Silicon Mac)
docker build --platform linux/amd64 -t customer-service-agent:local .

# Run with real AWS dev credentials
docker run --rm \
  --platform linux/amd64 \
  -p 8080:8080 \
  --env-file .env.local \
  -v "$HOME/.aws:/root/.aws:ro" \
  customer-service-agent:local
```

The `-v "$HOME/.aws:/root/.aws:ro"` mounts your local AWS credentials into the container as read-only. This lets the agent call Bedrock and AgentCore Memory using your dev credentials, exactly as it would with the execution role in production.

**Don't do this in production.** In production, the execution role is attached to the container by AgentCore. Mounting credentials is a local-only pattern.

---

## Verifying the health check

Once the container starts, verify the health endpoint:

```bash
curl http://localhost:8080/health
# → {"status":"healthy","environment":"dev"}
```

AgentCore probes `GET /health` before routing any traffic to a container instance. If the health check fails, AgentCore marks the instance as unhealthy and doesn't send requests to it.

---

## Testing with curl

The agent responds to `POST /invoke` with a Server-Sent Events stream. The `--no-buffer` flag is important: without it, curl buffers the response and you don't see streaming:

```bash
curl -X POST http://localhost:8080/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "What is the status of order ORD-001234?"}
    ],
    "sessionId": "test-session-1",
    "actorId": "user-test-123"
  }' \
  --no-buffer
```

You should see SSE events streaming back:

```
data: I'll look up order ORD-001234 for you.

data: **Order ORD-001234:**
data: - Status: In Transit
data: - Items: Wireless Headphones (x1), Phone Case (x2)
data: - Estimated delivery: April 5, 2025
data: - Tracking: UPS-9876543210

data: [DONE]
```

---

## Common local dev errors

**`exec format error`**: You built an arm64 image and are running it on an x86_64 host (or vice versa). Add `--platform linux/amd64` to both `docker build` and `docker run`.

**`Connection refused on port 8080`**: Container hasn't started yet or the health check is failing. Check `docker logs <container-id>`.

**`NoCredentialsError`**: The `.aws` mount isn't working or the profile in `.env.local` doesn't match a profile in `~/.aws/credentials`. Try `AWS_PROFILE=default` or remove the profile and let boto3 use instance metadata chain.

**`ResourceNotFoundException` on memory client**: `AGENTCORE_MEMORY_ID` is empty or wrong. Check the value against the SSM parameter. The memory module gracefully falls back (skips memory operations) if the ID is empty, so this shouldn't crash the agent.

**Slow response on Apple Silicon**: You're running an x86_64 container under QEMU emulation. This is ~3-5x slower than native and is expected for local testing. The deployed version on AgentCore's x86_64 hosts will be much faster.

---

## The local dev loop

```
1. Edit Python code
   ↓
2. docker build --platform linux/amd64 -t customer-service-agent:local .
   ↓
3. docker run --rm --platform linux/amd64 -p 8080:8080 --env-file .env.local \
     -v "$HOME/.aws:/root/.aws:ro" customer-service-agent:local
   ↓
4. curl -X POST http://localhost:8080/invoke ... --no-buffer
   ↓
5. Iterate until response is correct, then push to ECR
```

In Part 5, we automate steps 2-5 via GitHub Actions with OIDC, so every push to `main` builds the image, pushes it to ECR, and updates the AgentCore Runtime.

→ **[Continue to Part 5: CI/CD with GitHub Actions OIDC](/blog/part-5-cicd-github-actions-oidc)**
