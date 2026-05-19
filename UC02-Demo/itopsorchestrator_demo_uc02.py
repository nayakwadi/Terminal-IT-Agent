"""
ITOpsOrchestrator POC — UC-02 Public Exposure Scan (demo build).

Three commands:
  python itopsorchestrator_demo_uc02.py seed       # create three demo security groups
  python itopsorchestrator_demo_uc02.py scan       # enumerate 0.0.0.0/0 rules + LLM severity + Teams alert
  python itopsorchestrator_demo_uc02.py teardown   # delete the seeded demo security groups

LLM provider selection (pick one with LLM_PROVIDER; anthropic is the default):
  LLM_PROVIDER=anthropic       (default)  Anthropic API direct, no AWS dependency for the LLM
  LLM_PROVIDER=bedrock_nova               Amazon Nova on Bedrock (Converse API)
  LLM_PROVIDER=bedrock_claude             Anthropic Claude on Bedrock (Converse API)
  USE_ANTHROPIC_API=true                  legacy alias for LLM_PROVIDER=anthropic

Environment variables:
  TEAMS_WEBHOOK_URL          required for scan
  AWS_REGION                 default us-east-1
  AWS_PROFILE                optional, uses default profile otherwise
  ANTHROPIC_API_KEY          required when LLM_PROVIDER=anthropic
  ANTHROPIC_MODEL            default claude-sonnet-4-6 (Anthropic API)
  BEDROCK_MODEL_ID           default anthropic.claude-sonnet-4-6 (Bedrock Claude)
  BEDROCK_NOVA_MODEL_ID      default amazon.nova-lite-v1:0 (Bedrock Nova)
  DEMO_VPC_ID                optional; if unset, uses the default VPC in the region

IAM policy must include bedrock:InvokeModel on the chosen foundation-model
ARNs when using either Bedrock path. The Converse API runs under the same
bedrock:InvokeModel permission; there is no separate bedrock:Converse action.

Author: ITOpsOrchestrator POC build
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
import botocore
import requests

REGION = os.getenv("AWS_REGION", "us-east-1")

# Provider resolution. LLM_PROVIDER wins; USE_ANTHROPIC_API is a legacy alias.
# Default is "anthropic" so a fresh checkout works without AWS Bedrock model access.
_VALID_PROVIDERS = {"anthropic", "bedrock_nova", "bedrock_claude"}
_provider = os.getenv("LLM_PROVIDER", "").lower().strip()
if not _provider:
    if os.getenv("USE_ANTHROPIC_API", "").lower() == "true" or os.getenv("ANTHROPIC_API_KEY"):
        _provider = "anthropic"
    elif os.getenv("BEDROCK_NOVA_MODEL_ID"):
        _provider = "bedrock_nova"
    elif os.getenv("BEDROCK_MODEL_ID"):
        _provider = "bedrock_claude"
    else:
        _provider = "bedrock_nova"
if _provider not in _VALID_PROVIDERS:
    sys.exit(
        f"ERROR: invalid LLM_PROVIDER '{_provider}'. "
        f"Must be one of: {sorted(_VALID_PROVIDERS)}"
    )
PROVIDER = _provider

# Model identifiers (overridable per provider).
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
BEDROCK_NOVA_MODEL_ID = os.getenv("BEDROCK_NOVA_MODEL_ID", "amazon.nova-lite-v1:0")

TEAMS_WEBHOOK = os.getenv("TEAMS_WEBHOOK_URL", "")

DEMO_TAG_KEY = "itopsorchestrator-demo"
DEMO_TAG_VALUE = "uc02"

DEMO_GROUPS = [
    {
        "name": "itopsorchestrator-demo-ssh-open",
        "description": "ITOpsOrchestrator demo: SSH open to world on a production-tagged SG (expect HIGH)",
        "rules": [
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "demo: should flag high"}]},
        ],
        "tags": [
            {"Key": DEMO_TAG_KEY, "Value": DEMO_TAG_VALUE},
            {"Key": "env", "Value": "production"},
            {"Key": "data-class", "Value": "tier-1"},
            {"Key": "role", "Value": "app-server"},
        ],
    },
    {
        "name": "itopsorchestrator-demo-alb-https",
        "description": "ITOpsOrchestrator demo: 443 open on an ALB-tagged SG (expect INFORMATIONAL)",
        "rules": [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "demo: expected"}]},
        ],
        "tags": [
            {"Key": DEMO_TAG_KEY, "Value": DEMO_TAG_VALUE},
            {"Key": "env", "Value": "production"},
            {"Key": "role", "Value": "alb-public"},
            {"Key": "data-class", "Value": "tier-3"},
        ],
    },
    {
        "name": "itopsorchestrator-demo-db-open",
        "description": "ITOpsOrchestrator demo: MySQL open to world (expect CRITICAL)",
        "rules": [
            {"IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "demo: should flag critical"}]},
        ],
        "tags": [
            {"Key": DEMO_TAG_KEY, "Value": DEMO_TAG_VALUE},
            {"Key": "env", "Value": "production"},
            {"Key": "data-class", "Value": "tier-1"},
            {"Key": "role", "Value": "database"},
        ],
    },
]


def get_vpc_id(ec2) -> str:
    if os.getenv("DEMO_VPC_ID"):
        return os.environ["DEMO_VPC_ID"]
    resp = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])
    if not resp["Vpcs"]:
        raise SystemExit("No default VPC found in this region. Set DEMO_VPC_ID env var.")
    return resp["Vpcs"][0]["VpcId"]


# --- SEED -------------------------------------------------------------------

def cmd_seed() -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id = get_vpc_id(ec2)
    print(f"Seeding demo SGs in VPC {vpc_id} (region {REGION})...")

    for spec in DEMO_GROUPS:
        try:
            resp = ec2.create_security_group(
                GroupName=spec["name"],
                Description=spec["description"],
                VpcId=vpc_id,
                TagSpecifications=[{"ResourceType": "security-group", "Tags": spec["tags"]}],
            )
            sg_id = resp["GroupId"]
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=spec["rules"])
            print(f"  created {sg_id}  {spec['name']}")
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "InvalidGroup.Duplicate":
                print(f"  already exists: {spec['name']} (skipping)")
            else:
                raise
    print("Seed complete.")


def cmd_teardown() -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    print(f"Deleting demo SGs in region {REGION}...")
    resp = ec2.describe_security_groups(
        Filters=[{"Name": f"tag:{DEMO_TAG_KEY}", "Values": [DEMO_TAG_VALUE]}]
    )
    for sg in resp["SecurityGroups"]:
        try:
            ec2.delete_security_group(GroupId=sg["GroupId"])
            print(f"  deleted {sg['GroupId']}  {sg['GroupName']}")
        except botocore.exceptions.ClientError as e:
            print(f"  could not delete {sg['GroupId']}: {e}")
    print("Teardown complete.")


# --- SCAN -------------------------------------------------------------------

def list_public_rules() -> list[dict[str, Any]]:
    ec2 = boto3.client("ec2", region_name=REGION)
    sgs = ec2.describe_security_groups()["SecurityGroups"]
    findings: list[dict[str, Any]] = []
    for sg in sgs:
        tags = {t["Key"]: t["Value"] for t in sg.get("Tags", [])}
        for rule in sg.get("IpPermissions", []):
            for ip_range in rule.get("IpRanges", []):
                if ip_range.get("CidrIp") == "0.0.0.0/0":
                    findings.append({
                        "group_id": sg["GroupId"],
                        "group_name": sg["GroupName"],
                        "vpc_id": sg.get("VpcId"),
                        "protocol": rule.get("IpProtocol"),
                        "from_port": rule.get("FromPort"),
                        "to_port": rule.get("ToPort"),
                        "description": ip_range.get("Description", ""),
                        "tags": tags,
                    })
    # Enrich with attached resources
    for f in findings:
        f["attached"] = list_attached(ec2, f["group_id"])
    return findings


def list_attached(ec2, sg_id: str) -> list[dict[str, Any]]:
    enis = ec2.describe_network_interfaces(
        Filters=[{"Name": "group-id", "Values": [sg_id]}]
    )["NetworkInterfaces"]
    out = []
    for e in enis:
        out.append({
            "eni_id": e["NetworkInterfaceId"],
            "description": e.get("Description", ""),
            "instance_id": e.get("Attachment", {}).get("InstanceId"),
            "interface_type": e.get("InterfaceType"),
        })
    return out


SYSTEM_PROMPT = """You are a cloud security analyst reviewing AWS security group rules that allow inbound traffic from 0.0.0.0/0.

For each finding, return a JSON object with:
- group_id, group_name, port, protocol
- severity: one of critical, high, medium, low, informational
- reason: one sentence in plain English explaining the score

Severity guidance:
- critical: database or admin ports open to world (3306, 5432, 1433, 27017, 6379, 9200), or any port on resources tagged data-class=tier-1 that are not load balancers
- high: remote-admin ports open to world (22, 3389, 5985, 5986), or 0.0.0.0/0 on production-tagged instances that aren't ALB/NLB
- medium: uncommon high ports open with no clear business purpose, or 443 on an EC2 that isn't a load balancer
- low: 80 or 443 on an SG attached to an ALB / NLB / CloudFront but missing the role=public-lb tag
- informational: 443 or 80 on an SG explicitly tagged role=alb-public or role=public-lb

Use the tags (env, role, data-class) and the attached resource type (LoadBalancer, EC2 instance) to drive the score. Be decisive. Return ONLY a JSON array, no commentary."""


def reason_with_llm(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score findings via the configured LLM provider.

    Three providers, one return shape (parsed JSON array). Bedrock paths
    use the Converse API so Claude and Nova share the same client code,
    differing only by modelId.
    """
    user_msg = (
        "Findings:\n"
        + json.dumps(findings, indent=2)
        + "\n\nReturn the JSON array now."
    )

    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        client = Anthropic()
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = msg.content[0].text
    else:
        # Bedrock path. Converse API normalizes Claude and Nova body shapes.
        model_id = (
            BEDROCK_NOVA_MODEL_ID if PROVIDER == "bedrock_nova" else BEDROCK_MODEL_ID
        )
        bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        resp = bedrock.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]

    # Strip code fences if the model wrapped the JSON
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


# Backwards-compatible alias for anything that imports the old name.
reason_with_claude = reason_with_llm


# --- TEAMS POST -------------------------------------------------------------

SEVERITY_ORDER = ["critical", "high", "medium", "low", "informational"]
SEVERITY_EMOJI = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[LOW]",
    "informational": "[INFO]",
}
THEME_COLORS = {
    "critical": "C8102E",
    "high": "E08300",
    "medium": "C8B600",
    "low": "5C8FB0",
    "informational": "8E8E8E",
}


def post_to_teams(scored: list[dict[str, Any]]) -> None:
    """Post findings to a Teams Workflows webhook as a full Adaptive Card.

    The Teams Workflows trigger validates the incoming payload against a
    fixed schema that requires the canonical Bot Framework envelope:
    type=message, attachments[].contentType=application/vnd.microsoft.card.adaptive,
    attachments[].content as a complete Adaptive Card. The card itself is
    built here, not inside the flow, so the flow can stay untouched.
    """
    if not TEAMS_WEBHOOK:
        print("TEAMS_WEBHOOK_URL not set; skipping post.")
        return
    scored_sorted = sorted(scored, key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    top_sev = scored_sorted[0]["severity"] if scored_sorted else "informational"

    style_map = {
        "critical": "attention",
        "high": "attention",
        "medium": "warning",
        "low": "accent",
        "informational": "good",
    }

    body_items: list[dict[str, Any]] = [
        {
            "type": "Container",
            "style": style_map.get(top_sev, "default"),
            "bleed": True,
            "items": [
                {
                    "type": "TextBlock",
                    "size": "Large",
                    "weight": "Bolder",
                    "text": f"ITOpsOrchestrator public exposure scan: {len(scored_sorted)} findings",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "spacing": "Small",
                    "isSubtle": True,
                    "text": f"Top severity: {top_sev.upper()}  ·  Region: {REGION}",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "spacing": "None",
                    "isSubtle": True,
                    "size": "Small",
                    "text": f"Scored by {_provider_label()}",
                    "wrap": True,
                },
            ],
        }
    ]

    for f in scored_sorted:
        port = f.get("port") or f.get("from_port")
        proto = f.get("protocol", "tcp")
        body_items.append({
            "type": "Container",
            "style": style_map.get(f["severity"], "default"),
            "spacing": "Medium",
            "items": [
                {
                    "type": "TextBlock",
                    "weight": "Bolder",
                    "text": f"[{f['severity'].upper()}]  {f['group_id']} ({f['group_name']})",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "spacing": "None",
                    "isSubtle": True,
                    "text": f"port {port}/{proto}",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "spacing": "Small",
                    "text": f["reason"],
                    "wrap": True,
                },
            ],
        })

    adaptive_card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_items,
    }

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": adaptive_card,
            }
        ],
    }
    r = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print(f"Posted to Teams (HTTP {r.status_code}).")


def _provider_label() -> str:
    if PROVIDER == "anthropic":
        return f"Anthropic API ({ANTHROPIC_MODEL})"
    if PROVIDER == "bedrock_nova":
        return f"Bedrock Nova ({BEDROCK_NOVA_MODEL_ID})"
    return f"Bedrock Claude ({BEDROCK_MODEL_ID})"


def cmd_scan() -> None:
    print(f"Region:   {REGION}")
    print(f"Provider: {_provider_label()}")

    print("\nStep 1/3  Enumerating security group rules open to 0.0.0.0/0...")
    findings = list_public_rules()
    print(f"  Found {len(findings)} public-facing rules.")
    if not findings:
        print("  Nothing to score. Run `seed` first or check the region.")
        return

    print("\nStep 2/3  Scoring with the model...")
    scored = reason_with_llm(findings)
    print("\n=== Results ===")
    scored_sorted = sorted(scored, key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    for f in scored_sorted:
        port = f.get("port") or f.get("from_port")
        print(f"  [{f['severity'].upper():>13}] {f['group_id']:<22} port {port}: {f['reason']}")

    print("\nStep 3/3  Posting to MS Teams...")
    post_to_teams(scored_sorted)
    print("\nDone.")


# --- ENTRY ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="ITOpsOrchestrator UC-02 demo")
    ap.add_argument("cmd", choices=["seed", "scan", "teardown"])
    args = ap.parse_args()
    t0 = time.time()
    try:
        {"seed": cmd_seed, "scan": cmd_scan, "teardown": cmd_teardown}[args.cmd]()
    except botocore.exceptions.NoCredentialsError:
        sys.exit("ERROR: AWS credentials not found. Run `aws configure` or set AWS_PROFILE.")
    except KeyError as e:
        sys.exit(f"ERROR: missing key {e} in LLM output. Re-run; if it persists, lower temperature.")
    print(f"\nElapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
