# UC-01 Policy Conflict Agent — AgentCore Deployment Guide

End-to-end reference for deploying `itopsorchestrator_demo_uc01.py` as an Amazon Bedrock AgentCore Runtime workload, fronted by a Streamlit UI on AWS App Runner.

**Architecture**

```
[Browser]
   │  HTTPS + basic-auth (single shared password from Secrets Manager)
   ▼
[App Runner — Streamlit UI]        auto-pauses to $0 when idle
   │  boto3 + SigV4 (instance IAM role)
   ▼
[Bedrock AgentCore Runtime]        pay-per-invocation, ARM64 container
   │       │
   │       └──► Bedrock InvokeModel (Claude Sonnet 4.6 / Nova Lite)
   └────────► Bedrock KB Retrieve (existing KB)
                        │
                        └──► (optional) Teams Webhook ← Secrets Manager
```

**Locked-in decisions**

| Decision | Choice |
|---|---|
| LLM provider (deployed agent) | `bedrock_claude` only (drops Anthropic API key dependency) |
| UI auth | Basic-auth in Streamlit, password in Secrets Manager |
| Region | `us-east-1` (matches existing Bedrock KB) |

**Expected steady-state cost** for ≈10 invocations/day: **~$5–15/month** on top of existing Bedrock KB + per-token model charges.

---

## Phase 1 — Refactor for AgentCore entrypoint

Create `UC01-PolicyConflict/agentcore_app.py` that imports the existing CLI functions and wraps them in the AgentCore entrypoint pattern. The CLI script keeps working unchanged.

Key points:
- Force `LLM_PROVIDER=bedrock_claude` before importing the CLI module (its provider is resolved at import time).
- Optionally pull `TEAMS_WEBHOOK_URL` from AWS Secrets Manager at startup when `TEAMS_WEBHOOK_SECRET_NAME` is set.
- Entrypoint signature: `analyze(payload: dict) -> dict` with `ticket_text` (required) and `post_to_teams` (optional bool).

## Phase 2 — Containerize the agent

Add `UC01-PolicyConflict/Dockerfile` based on `public.ecr.aws/docker/library/python:3.12-slim`. Target ARM64 (AgentCore Runtime requirement).

```dockerfile
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt
COPY itopsorchestrator_demo_uc01.py agentcore_app.py ./
EXPOSE 8080
CMD ["python", "agentcore_app.py"]
```

Build on Apple Silicon: `docker buildx build --platform linux/arm64 -t policyconflict-agent .` (or let `agentcore launch` handle cross-compilation via CodeBuild).

## Phase 3 — Move secrets to Secrets Manager

```bash
aws secretsmanager create-secret --name policyconflict/teams-webhook \
  --secret-string "$TEAMS_WEBHOOK_URL" --region us-east-1

aws secretsmanager create-secret --name policyconflict/ui-password \
  --secret-string "$(openssl rand -base64 24)" --region us-east-1
```

Capture the second secret's plaintext from the create-secret response — that is the UI login password.

## Phase 4 — IAM execution role for the agent

Role `PolicyConflictAgentRole` with:

**Trust policy**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": "<ACCOUNT_ID>"}}
  }]
}
```

**Permissions policy**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "bedrock:Retrieve",
     "Resource": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:knowledge-base/<KB_ID>"},
    {"Effect": "Allow", "Action": "bedrock:InvokeModel",
     "Resource": "arn:aws:bedrock:us-east-1::foundation-model/*"},
    {"Effect": "Allow", "Action": "bedrock:ApplyGuardrail",
     "Resource": "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:guardrail/*"},
    {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
     "Resource": "arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:policyconflict/teams-webhook*"},
    {"Effect": "Allow",
     "Action": ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"],
     "Resource": "arn:aws:logs:us-east-1:<ACCOUNT_ID>:log-group:/aws/bedrock-agentcore/*"}
  ]
}
```

## Phase 5 — Deploy to AgentCore Runtime

```bash
pip install bedrock-agentcore-starter-toolkit

cd UC01-PolicyConflict

agentcore configure \
  --entrypoint agentcore_app.py \
  --execution-role arn:aws:iam::<ACCOUNT_ID>:role/PolicyConflictAgentRole \
  --region us-east-1 \
  --requirements-file requirements-agent.txt \
  --non-interactive

# Env vars are passed on launch, not configure — one --env flag per pair.
agentcore launch \
  --env BEDROCK_KB_ID=<KB_ID> \
  --env LLM_PROVIDER=bedrock_claude \
  --env BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6 \
  --env TEAMS_WEBHOOK_SECRET_NAME=policyconflict/teams-webhook \
  --env KB_TOP_K=5

agentcore invoke '{"ticket_text": "User INC0012345 cannot reach dropbox.com..."}'
```

Capture the **Agent Runtime ARN** from the launch output — the UI needs it.

## Phase 6 — Streamlit UI

Files under `UC01-PolicyConflict/ui/`:

- `streamlit_app.py` — paste-a-ticket form, optional "Post to Teams" checkbox, basic-auth wall, calls `bedrock-agentcore` runtime via boto3, renders the finding with the same Policy-A / Policy-B layout the CLI uses.
- `Dockerfile` — slim Python + Streamlit, runs on port 8080.
- `requirements.txt` — `streamlit`, `boto3`, `botocore`.

Environment variables the UI expects:
- `AGENT_RUNTIME_ARN` — from Phase 5
- `UI_PASSWORD_SECRET_NAME=policyconflict/ui-password`
- `AWS_REGION=us-east-1`

## Phase 7 — Deploy UI to App Runner

Build, push to ECR, then create an App Runner service.

```bash
# Create the UI ECR repo
aws ecr create-repository --repository-name policyconflict-ui --region us-east-1

# Build & push (App Runner accepts x86 or arm64)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin \
    <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
# Build context = UC01-PolicyConflict/ so sample-tickets/ is reachable
docker buildx build --platform linux/amd64 \
  -f ui/Dockerfile \
  -t <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/policyconflict-ui:latest \
  --push .
```

App Runner needs two roles:
- **Access role** (`AppRunnerECRAccessRole`) — trust `build.apprunner.amazonaws.com`, attach `AWSAppRunnerServicePolicyForECRAccess`.
- **Instance role** (`AppRunnerInvokeAgentRole`) — trust `tasks.apprunner.amazonaws.com`, with:
  ```json
  {"Effect":"Allow","Action":"bedrock-agentcore:InvokeAgentRuntime",
   "Resource":"<AGENT_RUNTIME_ARN>"},
  {"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
   "Resource":"arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:policyconflict/ui-password*"}
  ```

Create the service:
```bash
aws apprunner create-service \
  --service-name policyconflict-ui \
  --source-configuration file://apprunner-source.json \
  --instance-configuration '{"Cpu":"0.25 vCPU","Memory":"0.5 GB",
    "InstanceRoleArn":"arn:aws:iam::<ACCOUNT_ID>:role/AppRunnerInvokeAgentRole"}' \
  --health-check-configuration '{"Protocol":"HTTP","Path":"/_stcore/health"}' \
  --region us-east-1
```

`apprunner-source.json` template:
```json
{
  "ImageRepository": {
    "ImageIdentifier": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/policyconflict-ui:latest",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "AGENT_RUNTIME_ARN": "<AGENT_RUNTIME_ARN>",
        "UI_PASSWORD_SECRET_NAME": "policyconflict/ui-password",
        "AWS_REGION": "us-east-1"
      }
    },
    "ImageRepositoryType": "ECR"
  },
  "AuthenticationConfiguration": {
    "AccessRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/AppRunnerECRAccessRole"
  },
  "AutoDeploymentsEnabled": false
}
```

The service URL (`https://<random>.us-east-1.awsapprunner.com`) is your demo endpoint.

## Phase 8 — End-to-end verification

1. Open the App Runner URL.
2. Enter the UI password from `policyconflict/ui-password`.
3. Paste a ticket from [UC01-PolicyConflict/sample-tickets/](UC01-PolicyConflict/sample-tickets/).
4. Confirm the formatted finding appears.
5. Toggle "Post to Teams" and re-submit — confirm the Adaptive Card lands in Teams.
6. Inspect CloudWatch log groups:
   - `/aws/bedrock-agentcore/<runtime-name>`
   - `/aws/apprunner/policyconflict-ui/...`

## Phase 9 — Teardown

```bash
agentcore destroy

aws apprunner delete-service --service-arn <APP_RUNNER_ARN> --region us-east-1
aws ecr delete-repository --repository-name policyconflict-ui --force --region us-east-1
aws ecr delete-repository --repository-name policyconflict-agent --force --region us-east-1
aws secretsmanager delete-secret --secret-id policyconflict/teams-webhook --region us-east-1
aws secretsmanager delete-secret --secret-id policyconflict/ui-password   --region us-east-1
aws iam delete-role --role-name PolicyConflictAgentRole
aws iam delete-role --role-name AppRunnerInvokeAgentRole
aws iam delete-role --role-name AppRunnerECRAccessRole
```

Verify in the AWS console that no orphan CodeBuild projects or CloudWatch log groups remain.

---

## Cost summary (POC, low-traffic)

| Resource | Pricing model | Estimated monthly |
|---|---|---|
| AgentCore Runtime | Per-invocation compute + memory | $1–5 (≈10 invokes/day) |
| App Runner (idle-to-zero) | $0.007/vCPU-hr + $0.0008/GB-hr active | $1–5 |
| ECR storage (2 images) | $0.10/GB/mo | <$1 |
| Secrets Manager (2 secrets) | $0.40/secret/mo | $0.80 |
| CloudWatch Logs | $0.50/GB ingested | <$1 |
| Bedrock KB Retrieve | Per-query | usage-based |
| Bedrock InvokeModel | Per token | usage-based |
| **Subtotal (excl. token cost)** | | **~$5–15/mo** |

## Reference materials

- AgentCore Runtime: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html
- Starter toolkit: https://pypi.org/project/bedrock-agentcore-starter-toolkit/
- App Runner pricing: https://aws.amazon.com/apprunner/pricing/
- Secrets Manager pricing: https://aws.amazon.com/secrets-manager/pricing/
