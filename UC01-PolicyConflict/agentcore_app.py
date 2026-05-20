"""
AgentCore Runtime entrypoint for UC-01 Policy Conflict Detection.

Wraps itopsorchestrator_demo_uc01.py so the same analysis logic runs inside an
Amazon Bedrock AgentCore Runtime container.

Payload schema (JSON):
    {
        "ticket_text":  "<required: full ticket body>",
        "ticket_id":    "<optional: auto-extracted if omitted>",
        "post_to_teams": false
    }

Return schema:
    {
        "ticket_id":       "INC0012345" | null,
        "retrieval_query": "<query sent to Bedrock KB>",
        "chunks":          [ {text, score, source, metadata}, ... ],
        "finding":         { <full finding JSON per UC-01 schema> }
    }

Environment variables (set in `agentcore configure --environment`):
    BEDROCK_KB_ID                  required
    LLM_PROVIDER                   defaults to bedrock_claude here
    BEDROCK_MODEL_ID               default anthropic.claude-sonnet-4-6
    KB_TOP_K                       default 5
    AWS_REGION                     default us-east-1
    TEAMS_WEBHOOK_SECRET_NAME      optional; if set, pulled from Secrets Manager
    TEAMS_WEBHOOK_URL              optional fallback (env var)
    GUARDRAIL_ID / GUARDRAIL_VERSION  optional
"""
from __future__ import annotations

import json
import os
import sys

# Force the bedrock_claude provider BEFORE importing the CLI module, because
# itopsorchestrator_demo_uc01 resolves LLM_PROVIDER at import time.
os.environ.setdefault("LLM_PROVIDER", "bedrock_claude")

import boto3
import botocore

# Pull the Teams webhook from Secrets Manager (if configured) before importing
# the CLI module — that module reads TEAMS_WEBHOOK_URL into a module global.
_TEAMS_SECRET_NAME = os.getenv("TEAMS_WEBHOOK_SECRET_NAME", "").strip()
if _TEAMS_SECRET_NAME and not os.getenv("TEAMS_WEBHOOK_URL"):
    try:
        _sm = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
        _val = _sm.get_secret_value(SecretId=_TEAMS_SECRET_NAME)["SecretString"]
        os.environ["TEAMS_WEBHOOK_URL"] = _val
    except botocore.exceptions.ClientError as e:
        print(f"WARN: could not load Teams webhook secret '{_TEAMS_SECRET_NAME}': {e}",
              file=sys.stderr)

import itopsorchestrator_demo_uc01 as uc01

from bedrock_agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
def analyze(payload: dict) -> dict:
    ticket_text = (payload or {}).get("ticket_text", "").strip()
    if not ticket_text:
        return {"error": "Missing required field 'ticket_text' in payload."}

    ticket_id = payload.get("ticket_id") or uc01._extract_ticket_id(ticket_text)
    query = uc01._build_retrieval_query(ticket_text)
    chunks = uc01.retrieve_policy_chunks(query)
    finding = uc01.reason_with_llm(ticket_text, chunks)

    if payload.get("post_to_teams"):
        try:
            uc01.post_finding_to_teams(finding, ticket_id)
        except Exception as e:
            finding["_teams_post_error"] = str(e)

    return {
        "ticket_id": ticket_id,
        "retrieval_query": query,
        "chunks": chunks,
        "finding": finding,
    }


if __name__ == "__main__":
    app.run()
