# ITOpsOrchestrator POC — UC-01: Policy Conflict Detection

An AI-powered helpdesk agent that detects **cross-domain policy conflicts** — cases where a written policy permits an action that a technical control silently prevents (or vice versa).

Given a helpdesk ticket, the agent:
1. Retrieves the most relevant policy excerpts from an Amazon Bedrock Knowledge Base (SharePoint AUP, Zscaler ZIA rules, etc.)
2. Sends the ticket + retrieved chunks to an LLM for structured reasoning
3. Returns a JSON finding identifying the conflicting documents, severity, and recommended action
4. Posts the finding as an Adaptive Card to a Microsoft Teams channel

---

## Architecture

```
Helpdesk ticket (text file)
        │
        ▼
 Build retrieval query
        │
        ▼
 Bedrock Knowledge Base  ──►  Top-K policy chunks
        │
        ▼
 LLM reasoning  (Anthropic API  │  Bedrock Nova  │  Bedrock Claude)
        │
        ▼
 Structured JSON finding
        │
        ▼
 MS Teams Adaptive Card
```

**Bedrock Guardrails** are applied on the Bedrock inference path to intercept tickets that contain sensitive or restricted content (e.g. credential sharing requests, security policy bypass attempts) before the model responds.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | `python --version` |
| AWS account | With Bedrock model access enabled in `us-east-1` |
| AWS CLI configured | Named profile with Bedrock + Knowledge Base permissions |
| Bedrock Knowledge Base | Pre-built with policy documents (SharePoint AUP, Zscaler export) |
| Bedrock Guardrail (optional) | For sensitive-content interception |
| MS Teams Workflows webhook | For posting findings |

### Required IAM permissions (on the AWS profile used)

```
bedrock:InvokeModel        arn:aws:bedrock:<region>::foundation-model/*
bedrock:Retrieve           arn:aws:bedrock:<region>:<account>:knowledge-base/<KB_ID>
bedrock:GetKnowledgeBase   arn:aws:bedrock:<region>:<account>:knowledge-base/<KB_ID>
bedrock:ApplyGuardrail     arn:aws:bedrock:<region>:<account>:guardrail/<GUARDRAIL_ID>  (if using guardrails)
```

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd UC01-PolicyConflict
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note**: Bedrock guardrail support requires `boto3 >= 1.34.131`. Verify with `pip show boto3`.

### 4. Configure AWS profile

Add the named profile to `~/.aws/config`:

```ini
[profile <your-aws-profile>]
region = us-east-1
output = json
```

Add credentials to `~/.aws/credentials`:

```ini
[<your-aws-profile>]
aws_access_key_id     = <your-access-key-id>
aws_secret_access_key = <your-secret-access-key>
```

Verify:

```bash
AWS_PROFILE=<your-aws-profile> aws sts get-caller-identity
```

### 5. Configure environment variables

Copy the sample and fill in your values:

```bash
cp .env.example .env   # or edit .env directly
```

Edit `.env`:

```bash
# LLM Provider — options: anthropic | bedrock_nova | bedrock_claude
export LLM_PROVIDER=bedrock_nova

# Anthropic (required when LLM_PROVIDER=anthropic)
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL=claude-sonnet-4-6

# AWS
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_PROFILE=<your-aws-profile>

# Bedrock Knowledge Base
export BEDROCK_KB_ID=<your-kb-id>        # e.g. N9PXXMAZBY
export KB_TOP_K=5

# Guardrail (optional — remove both lines to disable)
export GUARDRAIL_ID=<your-guardrail-id>  # e.g. heke8qyn3f2o
export GUARDRAIL_VERSION=1

# Bedrock model IDs
export BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6
export BEDROCK_NOVA_MODEL_ID=amazon.nova-lite-v1:0

# MS Teams Workflows webhook
export TEAMS_WEBHOOK_URL="https://..."
```

Load the variables into your shell:

```bash
source .env
```

---

## LLM Provider Selection

| `LLM_PROVIDER` value | Model used | Auth required |
|---|---|---|
| `anthropic` (default) | Anthropic API — `ANTHROPIC_MODEL` | `ANTHROPIC_API_KEY` |
| `bedrock_nova` | Amazon Nova on Bedrock — `BEDROCK_NOVA_MODEL_ID` | AWS profile / IAM |
| `bedrock_claude` | Anthropic Claude on Bedrock — `BEDROCK_MODEL_ID` | AWS profile / IAM |

> Bedrock Guardrails are only applied on the `bedrock_nova` and `bedrock_claude` paths.

---

## Usage

### Check Knowledge Base connectivity

Run this first to confirm the KB is reachable and returning chunks:

```bash
python itopsorchestrator_demo_uc01.py kb-check
```

Expected output:

```
Knowledge Base:  <kb-name>
  id:            N9PXXMAZBY
  status:        ACTIVE
  created:       2026-05-10 ...
  storage:       OPENSEARCH_SERVERLESS

Probe retrieval for 'Is Dropbox approved for business use?':
  returned 5 chunks
  chunk 1: score=0.821, source=s3://...
  ...
```

### Analyze a helpdesk ticket

```bash
python itopsorchestrator_demo_uc01.py analyze --ticket sample-tickets/ticket-INC0012345-dropbox-blocked.txt
```

The agent runs three steps and prints progress:

```
Provider: Bedrock Nova (amazon.nova-lite-v1:0)
KB:       N9PXXMAZBY
Region:   us-east-1

Ticket file: sample-tickets/ticket-INC0012345-dropbox-blocked.txt
Ticket id:   INC0012345

Step 1/3  Building retrieval query from the ticket...
  query: Cross-reference policy on these topics: dropbox, blocked, ...

Step 2/3  Retrieving from Bedrock Knowledge Base...
  retrieved 5 chunks
  chunk 1: score=0.821  source=s3://...

Step 3/3  Reasoning with the model...

=== Finding ===
{
  "conflict_detected": true,
  "confidence": 0.92,
  "summary": "...",
  ...
}

Posting to MS Teams...
Posted finding to Teams (HTTP 202).
```

---

## Sample Tickets

Four sample tickets are included under `sample-tickets/`:

| File | Scenario | Expected finding |
|---|---|---|
| `ticket-INC0012345-dropbox-blocked.txt` | User blocked from Dropbox; policy permits it | `conflict_detected: true` — Zscaler vs SharePoint AUP |
| `ticket-INC0012789-dropbox-sync-error.txt` | Dropbox desktop client not syncing | `conflict_detected: false` — likely sync issue, not policy |
| `ticket-INC0013021-modify-zscaler-ssl-policy.txt` | DevOps requests SSL inspection bypass + URL policy change | Guardrail intervention (security policy modification) |
| `ticket-INC0013055-share-env-credentials.txt` | New joiner requests AWS keys + `.env` secrets over email/Slack | Guardrail intervention (credential sharing / sensitive info) |

The last two tickets are specifically designed to trigger Bedrock Guardrails. When a guardrail fires, the finding is posted to Teams with `severity: critical` and a recommendation to escalate to the IT Security team.

---

## Bedrock Guardrails

When `GUARDRAIL_ID` and `GUARDRAIL_VERSION` are set, every Bedrock Converse call includes a `guardrailConfig`. If the guardrail blocks a response:

- `stopReason` is `guardrail_intervened`
- The agent returns a structured `critical` finding rather than crashing
- The guardrail's `outputAssessments` trace is printed to stdout
- The finding is posted to Teams with an escalation recommendation

To disable guardrails, remove or unset both `GUARDRAIL_ID` and `GUARDRAIL_VERSION` from `.env`.

To inspect what was blocked, check **CloudWatch Logs** under the Bedrock guardrail log group for the `outputAssessments` detail.

---

## Project Structure

```
UC01-PolicyConflict/
├── itopsorchestrator_demo_uc01.py          # Main agent script
├── requirements.txt              # Python dependencies
├── .env                          # Environment variables (do not commit)
├── .env.example                  # Safe template to commit
├── sample-tickets/
│   ├── ticket-INC0012345-dropbox-blocked.txt
│   ├── ticket-INC0012789-dropbox-sync-error.txt
│   ├── ticket-INC0013021-modify-zscaler-ssl-policy.txt
│   └── ticket-INC0013055-share-env-credentials.txt
├── zscaler-zia-url-filtering-policy.json   # Sample Zscaler policy export
├── Sample-SharePoint-AUP.docx              # Sample Acceptable Use Policy
└── ITOps-Example01-Design-Doc.docx            # Design document
```

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `BEDROCK_KB_ID env var is not set` | `.env` not sourced | Run `source .env` before executing |
| `NoCredentialsError` | AWS profile missing or not exported | Verify `AWS_PROFILE` is exported and `aws sts get-caller-identity` works |
| `AccessDenied` on KB retrieve | IAM missing `bedrock:Retrieve` | Attach the permission to the IAM user/role |
| `JSONDecodeError: Expecting value` | Guardrail blocked response (empty content) | Fixed in current version — update to latest script |
| `InvalidClientTokenId` | Expired or wrong access key | Rotate credentials in `~/.aws/credentials` |
| Teams post returns non-2xx | Webhook URL expired or malformed | Regenerate the Teams Workflows webhook URL |

---

## Security Notes

- **Never commit `.env`** — it contains API keys and webhook URLs. Add `.env` to `.gitignore`.
- **Rotate credentials regularly** — the `<your-aws-profile>` IAM user should follow the principle of least privilege.
- **Guardrail versions** — when you publish a new guardrail version in the AWS Console, update `GUARDRAIL_VERSION` in `.env` to match.

---

## Related

- [UC-02 Public Exposure Scan](../UC02-Demo/README.md) — sibling use case that scans AWS security groups for public exposure


## New commands
# Default — pretty terminal output, no Teams call:
python3 itopsorchestrator_demo_uc01.py analyze \
  --ticket sample-tickets/ticket-INC0012345-dropbox-blocked.txt

# Add a Teams post:
python3 itopsorchestrator_demo_uc01.py analyze \
  --ticket sample-tickets/ticket-INC0012345-dropbox-blocked.txt --teams

# Show raw JSON in addition to the pretty view:
python3 itopsorchestrator_demo_uc01.py analyze \
  --ticket sample-tickets/ticket-INC0012345-dropbox-blocked.txt --json