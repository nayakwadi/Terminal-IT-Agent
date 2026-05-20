# ITOpsOrchestrator — UC-01 Policy Conflict Detection Agent

> A cloud-native, serverless AI agent that flags conflicts between **written policies** and **technical controls**, with an HTTPS web UI and a Microsoft Teams notification path.

## What it does — in 60 seconds

A help-desk ticket lands ("I can't reach Dropbox"). The system retrieves the most relevant excerpts from a policy knowledge base (SharePoint AUP, Zscaler URL-filtering rules, etc.) using vector search, then asks Claude Sonnet 4.6 to reason over the ticket + excerpts and return a **structured JSON finding**: is there a cross-domain conflict, which two documents disagree, what's the recommended action. The finding is rendered in a web UI and (optionally) pushed to a Teams channel as an Adaptive Card.

**Single-line value proposition:** turns ambiguous help-desk friction into evidence-backed policy escalations in seconds, with citations to the disagreeing documents.

---

## System Architecture

```
                           ┌────────────────┐
                           │   End user     │
                           │   (Browser)    │
                           └───────┬────────┘
                                   │  HTTPS · cookie session · basic-auth
                                   ▼
                       ┌─────────────────────────┐
                       │   AWS App Runner        │   FastAPI + Uvicorn (port 8080)
                       │   policyconflict-ui     │   ECR image · scales to zero
                       │   (Envoy / TLS term)    │   instance role: AppRunnerInvokeAgentRole
                       └───────────┬─────────────┘
                                   │  boto3 SigV4
                                   │  bedrock-agentcore:InvokeAgentRuntime
                                   ▼
                       ┌─────────────────────────┐
                       │  Bedrock AgentCore      │   ARM64 container · pay-per-invocation
                       │  Runtime                │   ECR image: bedrock-agentcore-agentcore_app
                       │  (agentcore_app)        │   execution role: PolicyConflictAgentRole
                       └────┬───────────┬────────┘
                            │           │
              bedrock:Retrieve    bedrock:Converse / InvokeModel
                            │           │
                            ▼           ▼
           ┌──────────────────┐  ┌──────────────────────────────┐
           │ Bedrock KB       │  │  Bedrock Inference Profile   │
           │ R1IAWLXIMD       │  │  us.anthropic.claude-sonnet- │
           │ + OpenSearch     │  │  4-6  (cross-region routed)  │
           │   Serverless     │  └──────────────────────────────┘
           │ + S3 source docs │
           └──────────────────┘

   Side channels:
     Secrets Manager  ──► UI password (FastAPI auth)
                          Teams webhook URL (loaded by agent at boot)
     Teams Webhook    ◄── (optional) Adaptive Card from agent
     CloudWatch Logs  ◄── /aws/bedrock-agentcore/runtimes/...
                          /aws/apprunner/policyconflict-ui-debug/...
```

---

## Components & Services Used

### AWS managed services

| Service | Purpose in this system | Resource ID(s) |
|---|---|---|
| **Bedrock AgentCore Runtime** | Hosts the agent container; auto-scales to zero between invocations | `agentcore_app-OQ5p2Y5Z2i` |
| **Bedrock Knowledge Base** | Vector retrieval over policy docs (semantic search) | KB `R1IAWLXIMD` |
| **Bedrock Foundation Model** | Reasoning over ticket + chunks | `us.anthropic.claude-sonnet-4-6` (inference profile) |
| **OpenSearch Serverless** | Vector index backing the KB | created with the KB |
| **S3** | Source documents ingested into the KB | `agent-itops-demo` bucket |
| **AWS App Runner** | Hosts the UI container; managed TLS + Envoy ingress; idle-to-zero | service `policyconflict-ui-debug` |
| **ECR** | Container image registry for both agent + UI images | `bedrock-agentcore-agentcore_app`, `policyconflict-ui` |
| **CodeBuild** | Builds the ARM64 agent image during `agentcore launch` | `bedrock-agentcore-agentcore_app-builder` |
| **AWS Secrets Manager** | Stores Teams webhook URL + UI password | `policyconflict/teams-webhook`, `policyconflict/ui-password` |
| **IAM** | Roles + permission boundaries (see next table) | – |
| **CloudWatch Logs** | Centralized logs for agent + UI runtimes | `/aws/bedrock-agentcore/...`, `/aws/apprunner/...` |

### IAM roles

| Role | Trusted by | Purpose |
|---|---|---|
| `PolicyConflictAgentRole` | `bedrock-agentcore.amazonaws.com` | Agent execution: KB Retrieve, model Invoke, Secrets read, ECR pull, CW logs |
| `AppRunnerECRAccessRole` | `build.apprunner.amazonaws.com` | Lets App Runner pull the UI image from ECR |
| `AppRunnerInvokeAgentRole` | `tasks.apprunner.amazonaws.com` | Lets the running UI container call `bedrock-agentcore:InvokeAgentRuntime` + read UI password |
| `AWSServiceRoleForAppRunner` | `apprunner.amazonaws.com` | App Runner service-linked role; manages log groups, ECR access |
| user policy: `AppRunnerPassRolePolicy` | (inline on caller user) | One-time grant so the deploying user can `iam:PassRole` to App Runner |

### Application stack

| Layer | Stack | Where it runs |
|---|---|---|
| **Agent** | Python 3.12 + `bedrock-agentcore` SDK + `boto3` + `requests` | AgentCore Runtime container (ARM64) |
| **UI** | Python 3.12 + FastAPI + Uvicorn + `boto3` | App Runner container (x86_64) |
| **Container base** | `public.ecr.aws/docker/library/python:3.12-slim` | both |
| **Local CLI** | Same agent code, runnable directly with `python itopsorchestrator_demo_uc01.py` | developer laptop |

---

## Request lifecycle (what happens when a user clicks "Analyze")

```
1.  Browser → POST /analyze   ──► App Runner Envoy
2.  Envoy → uvicorn → FastAPI /analyze handler
3.  FastAPI reads UI session cookie; rejects if not authenticated
4.  boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=..., payload={"ticket_text": "...", "post_to_teams": false})
5.  AgentCore wakes the runtime container (cold start ~3–5s; warm ~50ms)
6.  agentcore_app.analyze() runs:
        a.  _build_retrieval_query()       — extract keywords from ticket text
        b.  retrieve_policy_chunks()       — bedrock-agent-runtime.retrieve()
        c.  reason_with_llm()              — bedrock-runtime.converse() to Claude
        d.  (optional) post_finding_to_teams()
7.  Return JSON: {ticket_id, retrieval_query, chunks, finding}
8.  FastAPI renders the Policy A / Policy B / Recommendation cards in HTML
9.  Browser receives the rendered page (no JS round-trip, no WebSocket)
```

Cold start path is the slowest segment (~3–5 seconds). Warm path is <2 seconds end-to-end including the model call.

---

## Build & deploy lifecycle (chronological summary)

In the order operations were performed:

| # | Phase | What was created |
|---|---|---|
| 1 | KB validation | Confirmed `R1IAWLXIMD` is `ACTIVE` with policy docs already ingested |
| 2 | Secrets | `policyconflict/teams-webhook`, `policyconflict/ui-password` |
| 3 | Agent IAM | `PolicyConflictAgentRole` + inline policy (KB Retrieve, model Invoke, Secrets, CW Logs, ECR pull) |
| 4 | Agent image | `agentcore configure` → `agentcore launch` → CodeBuild built ARM64 image → ECR push → AgentCore Runtime created |
| 5 | Agent fix #1 | Switched model ID to `us.anthropic.claude-sonnet-4-6` (cross-region inference profile; raw model ID rejected on-demand) |
| 6 | Agent fix #2 | Added IAM permission for `bedrock:InvokeModel` on inference-profile ARN + foundation-model ARNs in all routed regions |
| 7 | Agent smoke test | `agentcore invoke` returned full structured finding |
| 8 | UI image (v1) | Built Streamlit UI image, pushed to `policyconflict-ui` ECR repo |
| 9 | App Runner IAM | `AppRunnerECRAccessRole` + `AppRunnerInvokeAgentRole` |
| 10 | User PassRole | `iam:PassRole` inline policy attached to deploying user |
| 11 | Service-linked role | Admin added `iam:CreateServiceLinkedRole`, then `AWSServiceRoleForAppRunner` created |
| 12 | App Runner service | `policyconflict-ui-debug` created from ECR image |
| 13 | UI fix | Streamlit's WebSocket layer failed behind App Runner's TLS-terminating Envoy — replaced with FastAPI (HTTP-only, no WebSockets) |
| 14 | UI fix #2 | Granted instance role `InvokeAgentRuntime` on the runtime *endpoint* sub-resource (`/runtime-endpoint/*`), not just the runtime root |
| 15 | End-to-end | Browser → UI → agent → finding rendered with citations |

The full step-by-step playbook with commands lives in [AgentCore-Deployment-Guide.md](AgentCore-Deployment-Guide.md).

---

## Design decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Agent runtime | **Bedrock AgentCore Runtime** | Managed container, ARM64, scales to zero, native AWS auth. Alternative considered: Bedrock Agents (too opinionated for a custom multi-step flow). |
| Model | **Claude Sonnet 4.6 via `us.` inference profile** | Best reasoning quality for structured JSON extraction; cross-region profile is required since on-demand is disabled for Claude 4.x. |
| Vector store | **Bedrock KB + OpenSearch Serverless** | Re-used the existing KB; OpenSearch Serverless gives auto-scaling vector search without managing nodes. |
| UI hosting | **AWS App Runner** | One-command container deploy, idle-to-zero, managed TLS, simplest path. Considered: ECS Fargate (more knobs, harder), EC2 t4g.nano (always-on cost). |
| UI framework | **FastAPI + HTML forms** (originally Streamlit) | Streamlit's WebSocket layer was rejected behind App Runner's Envoy proxy. FastAPI uses plain HTTP form posts → zero compatibility risk. |
| Auth | **Single-password basic-auth** | POC scope. Password lives in Secrets Manager. Upgrade path: Cognito + JWT on App Runner if it goes multi-user. |
| Secrets | **AWS Secrets Manager** | $0.40/secret/month, native IAM integration, easy rotation later. |
| Region | **`us-east-1`** | Co-located with the existing KB; broadest Bedrock model availability. |
| Region routing | **Cross-region inference profile (`us.`)** | Mandatory for Claude 4.x; transparent to the application code. |

---

## Repository layout

```
Terminal-IT-Agent/
├── README.md                              ← you are here (system overview)
├── AgentCore-Deployment-Guide.md          ← 9-phase playbook with exact commands
├── AWS_Infrastructure/                    ← (older CloudFormation/CLI for original KB)
└── UC01-PolicyConflict/
    ├── README.md                          ← CLI-mode usage doc (original)
    ├── itopsorchestrator_demo_uc01.py     ← core agent logic (also runnable as CLI)
    ├── agentcore_app.py                   ← BedrockAgentCoreApp wrapper (deployed entrypoint)
    ├── requirements.txt                   ← CLI requirements
    ├── requirements-agent.txt             ← agent container requirements (slim)
    ├── Dockerfile                         ← agent container
    ├── sample-tickets/                    ← 4 sample helpdesk tickets
    ├── ui/
    │   ├── app.py                         ← FastAPI UI (current)
    │   ├── streamlit_app.py               ← Streamlit UI (replaced, kept for reference)
    │   ├── requirements.txt               ← UI container requirements
    │   └── Dockerfile                     ← UI container
    └── deploy/
        ├── agent-trust-policy.json        ← IAM trust for PolicyConflictAgentRole
        ├── agent-permissions-policy.json  ← inline perms for PolicyConflictAgentRole
        ├── apprunner-access-trust-policy.json
        ├── apprunner-instance-trust-policy.json
        ├── apprunner-instance-permissions.json
        ├── apprunner-source.json          ← App Runner source-configuration
        └── user-passrole-policy.json      ← one-time PassRole grant for caller
```

---

## Operating the system

### Tail logs

```bash
# Agent
aws logs tail /aws/bedrock-agentcore/runtimes/agentcore_app-OQ5p2Y5Z2i-DEFAULT \
  --since 10m --region us-east-1 --follow

# UI
aws logs tail /aws/apprunner/policyconflict-ui-debug/<SVC_ID>/application \
  --since 10m --region us-east-1 --follow
```

### Smoke test (skipping the UI)

```bash
agentcore invoke "$(jq -n --arg t "$(cat sample-tickets/ticket-INC0012345-dropbox-blocked.txt)" \
  '{ticket_text: $t}')"
```

### Re-deploy after code changes

```bash
# Agent code change
agentcore launch --auto-update-on-conflict   # rebuilds + redeploys

# UI code change
docker buildx build --platform linux/amd64 -f ui/Dockerfile \
  -t 865983313794.dkr.ecr.us-east-1.amazonaws.com/policyconflict-ui:latest --push .
aws apprunner start-deployment --region us-east-1 --service-arn <SVC_ARN>
```

---

## Cost model (steady-state POC)

| Resource | Pricing model | Est. monthly (≈10 invokes/day) |
|---|---|---|
| AgentCore Runtime | per-invocation compute + memory | $1–5 |
| App Runner | $0.007/vCPU-hr + $0.0008/GB-hr **only when active** | $1–5 |
| ECR storage (2 images) | $0.10/GB/mo | <$1 |
| Secrets Manager (2 secrets) | $0.40/secret/mo | $0.80 |
| CloudWatch Logs | $0.50/GB ingested | <$1 |
| Bedrock KB Retrieve | per-query | usage-based |
| Bedrock InvokeModel | per token (input + output) | usage-based |
| **Total (excluding token cost)** | | **~$5–15/mo** |

Token cost dominates at scale; with Sonnet 4.6 and ~2K input + 1K output tokens per analysis, each analysis is roughly $0.01–0.02.

---

## Teardown

```bash
# Stop App Runner (continues to incur ECR storage until images deleted)
aws apprunner delete-service --region us-east-1 --service-arn <SVC_ARN>

# Stop the agent
agentcore destroy

# Free supporting resources
aws ecr delete-repository --repository-name policyconflict-ui --force --region us-east-1
aws ecr delete-repository --repository-name bedrock-agentcore-agentcore_app --force --region us-east-1
aws secretsmanager delete-secret --secret-id policyconflict/teams-webhook --region us-east-1
aws secretsmanager delete-secret --secret-id policyconflict/ui-password   --region us-east-1
aws iam delete-role --role-name PolicyConflictAgentRole
aws iam delete-role --role-name AppRunnerInvokeAgentRole
aws iam delete-role --role-name AppRunnerECRAccessRole
```

The Bedrock KB + OpenSearch Serverless collection + the S3 source bucket are left in place — they predate this deployment and are shared.

---

## Solution-engineer talking points

**Q: Why AgentCore instead of Lambda or Bedrock Agents?**
Lambda has a 15-minute hard cap and no streaming model support. Bedrock Agents enforces a particular ReAct loop; we needed a single-shot RAG + structured-JSON pattern. AgentCore Runtime gives us a managed serverless container with **scale-to-zero**, the right concurrency model, and native Bedrock auth.

**Q: Why a custom UI on App Runner instead of Bedrock's prompt playground?**
The audience is **non-technical ITSM staff**, not data scientists. The UI prefills sample tickets, hides the JSON, and renders the conflict as two side-by-side citation cards. Authentication and audit logging are also on the UI's terms, not the model console's.

**Q: Why FastAPI not Streamlit?**
Streamlit relies on persistent WebSocket connections. App Runner terminates TLS at Envoy and a chain of subtle handshake-header mismatches caused the WebSocket upgrade to be rejected. FastAPI uses standard HTTP form posts → no WebSocket, no compatibility risk. Same UI features, smaller image, faster page loads.

**Q: How does the agent know *which* two documents conflict?**
It doesn't, structurally. The KB returns the top-5 most semantically similar chunks; the LLM then identifies the two whose stances disagree and extracts verbatim excerpts. The model is instructed (via system prompt) to refuse to invent a conflict if the chunks don't disagree — in which case it returns `conflict_detected: false` with a different recommendation.

**Q: How does this stay cheap when idle?**
Both runtimes scale to zero. AgentCore Runtime only charges per-invocation. App Runner pauses the instance when there are no requests. There's no permanent EC2 or RDS layer. The only flat cost is two Secrets Manager secrets ($0.80/mo).

**Q: What's the failure mode if Bedrock is down or rate-limits us?**
The agent returns a structured error to the UI, which renders an inline error banner. The Teams notification is gated on a successful finding, so a partial failure doesn't post misleading data. Retry policy is at the client (UI) level, not the server.

**Q: Where would I add a guardrail?**
The agent already supports Bedrock Guardrails via `GUARDRAIL_ID` / `GUARDRAIL_VERSION` environment variables. Set them on the AgentCore launch command and the `converse` call will route through the guardrail; intervention-triggered responses return a `severity: critical` finding flagged for IT Security escalation.

**Q: How would you scale this to multiple use cases (UC-02, UC-03)?**
Each use case becomes its own AgentCore Runtime with its own execution role; the UI either adds tabs (one per use case) or routes between agents based on ticket classification. The retrieval/reasoning/notify pattern is identical — only the system prompt + KB content differ.

---

## At-a-glance system summary

| Aspect | Detail |
|---|---|
| **Pattern** | RAG (retrieval-augmented generation) over policy docs, with structured JSON output |
| **Compute** | Two scale-to-zero serverless containers (agent + UI) |
| **State** | Stateless agent; UI keeps cookie-session in memory (single-instance POC) |
| **Auth** | Basic-auth at UI; SigV4 (IAM role) UI → AgentCore; SigV4 (execution role) agent → Bedrock |
| **Observability** | CloudWatch Logs for both runtimes; structured JSON logging in the agent |
| **Cost shape** | ~$0/month idle + per-invocation token cost when used |
| **Failure containment** | Agent errors return structured JSON; UI renders them; no Teams post on failure |
| **Operator workflow** | Code change → rebuild/push → `agentcore launch` *or* `apprunner start-deployment` |
