# ITOpsOrchestrator POC — UC-02: Public Exposure Scan

An AI-powered AWS security scanner that enumerates security group rules open to `0.0.0.0/0`, asks an LLM to score each finding by severity using port number, resource tags, and attached instances as context, and posts a color-coded Adaptive Card to Microsoft Teams.

> **Demo time**: ~10 minutes end-to-end.
> **Cost**: < $5 in Bedrock invocations for a typical demo run.

---

## What It Does

```
AWS EC2 API
    │
    ▼  describe_security_groups + describe_network_interfaces
Findings (SG id, port, protocol, tags, attached resources)
    │
    ▼
LLM scoring  (Anthropic API  │  Bedrock Nova  │  Bedrock Claude)
    │
    ▼  JSON array: severity + one-sentence reason per finding
Terminal output  +  MS Teams Adaptive Card
```

Three seeded security groups produce a predictable demo:

| Security Group | Port | Expected Severity |
|---|---|---|
| `itopsorchestrator-demo-db-open` | 3306/tcp | **CRITICAL** — MySQL open to world on a tier-1 database SG |
| `itopsorchestrator-demo-ssh-open` | 22/tcp | **HIGH** — SSH from 0.0.0.0/0 on a production app-server SG |
| `itopsorchestrator-demo-alb-https` | 443/tcp | **INFORMATIONAL** — HTTPS on an SG explicitly tagged `role=alb-public` |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | `python --version` |
| AWS account | With Bedrock model access enabled in `us-east-1` |
| AWS CLI configured | Named profile with EC2 + Bedrock permissions (see below) |
| Bedrock model access | Enable in AWS Console → Amazon Bedrock → Model access |
| MS Teams Workflows webhook | For posting findings to a channel |
| Anthropic API key (optional) | Fallback if Bedrock access is not yet approved |

### Required IAM permissions

```
ec2:DescribeSecurityGroups          *
ec2:DescribeNetworkInterfaces       *
ec2:DescribeVpcs                    *
ec2:CreateSecurityGroup             *  (seed command only)
ec2:AuthorizeSecurityGroupIngress   *  (seed command only)
ec2:DeleteSecurityGroup             *  (teardown command only)
bedrock:InvokeModel                 arn:aws:bedrock:<region>::foundation-model/*
```

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd UC02-Demo
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

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

Verify the profile and permissions:

```bash
AWS_PROFILE=<your-aws-profile> aws sts get-caller-identity
AWS_PROFILE=<your-aws-profile> aws ec2 describe-security-groups --region us-east-1
```

### 5. Enable Bedrock model access

In the AWS Console:
**Amazon Bedrock → Model access → Anthropic → Amazon Nova** → request access for the model you intend to use. Access is typically granted within minutes.

### 6. Create a Teams Workflows webhook

In Microsoft Teams:
1. Open the target channel → **···** (More options) → **Workflows**
2. Search for **"Post to a channel when a webhook request is received"** → Add
3. Follow the wizard, copy the generated webhook URL

### 7. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

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

# Bedrock model IDs
export BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6      # used when LLM_PROVIDER=bedrock_claude
export BEDROCK_NOVA_MODEL_ID=amazon.nova-2-lite-v1:0     # used when LLM_PROVIDER=bedrock_nova

# MS Teams Workflows webhook
export TEAMS_WEBHOOK_URL="https://..."

# Optional: override the VPC used for seeding (defaults to the default VPC)
# export DEMO_VPC_ID=vpc-xxxxxxxxxxxxxxxxx
```

Load variables into your shell:

```bash
source .env
```

---

## LLM Provider Selection

Set `LLM_PROVIDER` in `.env` to choose the inference backend:

| Value | Model | Auth needed |
|---|---|---|
| `anthropic` (default) | Anthropic API direct — `ANTHROPIC_MODEL` | `ANTHROPIC_API_KEY` |
| `bedrock_nova` | Amazon Nova on Bedrock — `BEDROCK_NOVA_MODEL_ID` | AWS profile / IAM |
| `bedrock_claude` | Anthropic Claude on Bedrock — `BEDROCK_MODEL_ID` | AWS profile / IAM |

> Use `LLM_PROVIDER=anthropic` with an `ANTHROPIC_API_KEY` as a fallback if Bedrock model access is not yet approved for your account.

---

## Running the Demo

### Step 1 — Seed demo security groups

Creates three security groups tagged `itopsorchestrator-demo=uc02` in your account. Safe to run multiple times (duplicate names are skipped).

```bash
python itopsorchestrator_demo_uc02.py seed
```

Expected output:

```
Seeding demo SGs in VPC vpc-xxxxxxxxxx (region us-east-1)...
  created sg-aaaaaaaaaa  itopsorchestrator-demo-ssh-open
  created sg-bbbbbbbbbb  itopsorchestrator-demo-alb-https
  created sg-cccccccccc  itopsorchestrator-demo-db-open
Seed complete.
```

### Step 2 — Run the scan

This is the single command you run during the demo.

```bash
python itopsorchestrator_demo_uc02.py scan
```

Expected output:

```
Region:   us-east-1
Provider: Bedrock Nova (amazon.nova-2-lite-v1:0)

Step 1/3  Enumerating security group rules open to 0.0.0.0/0...
  Found 3 public-facing rules.

Step 2/3  Scoring with the model...

=== Results ===
  [     CRITICAL] sg-cccccccccc       port 3306: MySQL open to the internet on a tier-1 database SG.
  [         HIGH] sg-aaaaaaaaaa       port   22: SSH from 0.0.0.0/0 on a production-tagged app-server SG.
  [INFORMATIONAL] sg-bbbbbbbbbb       port  443: HTTPS open on an SG explicitly tagged as a public ALB.

Step 3/3  Posting to MS Teams...
Posted to Teams (HTTP 202).

Elapsed: 8.3s
```

### Step 3 — Confirm the Teams card

Switch to the Teams channel. You should see one Adaptive Card titled:
**"ITOpsOrchestrator public exposure scan: 3 findings"**

The card header color reflects the top severity (red for CRITICAL). Each finding shows the SG ID, port, and one-sentence reason.

### Step 4 — Teardown

Delete all seeded security groups to leave the account clean.

```bash
python itopsorchestrator_demo_uc02.py teardown
```

Expected output:

```
Deleting demo SGs in region us-east-1...
  deleted sg-aaaaaaaaaa  itopsorchestrator-demo-ssh-open
  deleted sg-bbbbbbbbbb  itopsorchestrator-demo-alb-https
  deleted sg-cccccccccc  itopsorchestrator-demo-db-open
Teardown complete.
```

---

## Preflight Checklist

Run through this before the demo, not during it:

- [ ] `aws sts get-caller-identity` returns the expected account and IAM user
- [ ] `aws ec2 describe-security-groups --region us-east-1` returns without error
- [ ] Bedrock model access is granted for the chosen model in `us-east-1`
- [ ] Teams webhook URL is set and working (`source .env && echo $TEAMS_WEBHOOK_URL`)
- [ ] Full dry run: `seed` → `scan` → `teardown` completes without errors
- [ ] Teardown run after dry run to reset to clean state

---

## Project Structure

```
UC02-Demo/
├── itopsorchestrator_demo_uc02.py            # Main script (recommended — all three providers)
├── itopsorchestrator_demo_uc02_Anthropic.py  # Anthropic API-only variant
├── itopsorchestrator_demo_uc02_bedrock_nova.py  # Bedrock Nova-only variant
├── requirements.txt                # Python dependencies
├── .env                            # Environment variables (do NOT commit)
└── .env.example                    # Safe template to commit
```

> Use `itopsorchestrator_demo_uc02.py` for all new work. The `_Anthropic` and `_bedrock_nova` variants are kept for reference only.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AccessDeniedException` on `bedrock:InvokeModel` | Model access not granted, or IAM missing `bedrock:InvokeModel` | Enable model access in Bedrock console; or set `LLM_PROVIDER=anthropic` and use `ANTHROPIC_API_KEY` as fallback |
| `No default VPC found in this region` | Region has no default VPC | Set `export DEMO_VPC_ID=vpc-xxxxxxxxx` in `.env` |
| `Found 0 public-facing rules` | Seed not run, or wrong region | Run `seed` first; confirm `AWS_REGION` matches |
| Teams card never appears | Webhook URL wrong or expired | Test with: `curl -H "Content-Type: application/json" -d '{"type":"message","attachments":[]}' "$TEAMS_WEBHOOK_URL"` — expect HTTP 202 |
| `NoCredentialsError` | `.env` not sourced, or profile missing | Run `source .env`; verify `aws sts get-caller-identity` works |
| `JSONDecodeError` or missing key in LLM output | Model returned non-JSON response | Re-run the scan; set `temperature=0` is already in the code — if it persists, switch providers |
| Teardown leaves an SG behind | SG is attached to a running ENI | Detach the ENI or stop the instance, then re-run teardown |
| `InvalidGroup.Duplicate` during seed | SGs already exist from a previous run | Safe to ignore — seed skips duplicates. Run teardown first if you want a clean slate |

---

## Security Notes

- **Never commit `.env`** — it contains your AWS credentials and API keys. Ensure `.env` is listed in `.gitignore`.
- **Use `.env.example`** as the committed template — it contains no real values.
- **Rotate credentials** after the demo if the IAM access key was shared or used in an insecure environment.
- **Teardown after every run** — the seeded security groups contain intentionally dangerous rules (`0.0.0.0/0` on port 3306). Do not leave them in place.
- **Least-privilege IAM** — scope `bedrock:InvokeModel` to specific model ARNs rather than `*` for production use.

---

## Related

- [UC-01 Policy Conflict Detection](../UC01-PolicyConflict/README.md) — sibling use case that detects cross-domain policy conflicts from helpdesk tickets using a Bedrock Knowledge Base
