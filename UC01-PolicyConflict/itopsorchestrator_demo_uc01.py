"""
ITOpsOrchestrator POC — UC-01 Policy Conflict Detection (helpdesk-ticket RAG agent).

The agent receives a helpdesk ticket about a user being blocked from a tool,
retrieves the most relevant policy chunks from a Bedrock Knowledge Base built
over uploaded policy documents (SharePoint AUP, Zscaler ZIA policy export),
asks an LLM to reason over the ticket + retrieved chunks, and returns a
structured finding that identifies whether the block is due to a cross-domain
policy conflict.

Two commands:
  python itopsorchestrator_demo_uc01.py kb-check                   # quick check the KB is reachable
  python itopsorchestrator_demo_uc01.py analyze --ticket FILE.txt  # run the full agent on a ticket

LLM provider selection (same pattern as UC-02 script):
  LLM_PROVIDER=anthropic       (default)  Anthropic API direct
  LLM_PROVIDER=bedrock_nova               Amazon Nova on Bedrock
  LLM_PROVIDER=bedrock_claude             Anthropic Claude on Bedrock

Required environment variables:
  BEDROCK_KB_ID              Bedrock Knowledge Base ID (e.g. ABCDEF1234)
  TEAMS_WEBHOOK_URL          required to post the finding to Teams
  AWS_REGION                 default us-east-1
  AWS_PROFILE                optional; uses default profile otherwise
  ANTHROPIC_API_KEY          required when LLM_PROVIDER=anthropic

Optional:
  ANTHROPIC_MODEL            default claude-sonnet-4-6
  BEDROCK_MODEL_ID           default anthropic.claude-sonnet-4-6
  BEDROCK_NOVA_MODEL_ID      default amazon.nova-lite-v1:0
  KB_TOP_K                   default 5 (chunks to retrieve)
  GUARDRAIL_ID               Bedrock guardrail identifier; if set, applied to Bedrock converse calls
  GUARDRAIL_VERSION          Bedrock guardrail version; default DRAFT

IAM additions required (on top of UC-02 policy):
  bedrock:Retrieve           on arn:aws:bedrock:<region>:<acct>:knowledge-base/<kb-id>
  bedrock:InvokeModel        (already in UC-02; reused here)

Author: ITOpsOrchestrator POC build
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
import botocore
import requests

REGION = os.getenv("AWS_REGION", "us-east-1")

_VALID_PROVIDERS = {"anthropic", "bedrock_nova", "bedrock_claude"}
_provider = os.getenv("LLM_PROVIDER", "").lower().strip()
if not _provider:
    _provider = "anthropic" if os.getenv("USE_ANTHROPIC_API", "").lower() == "true" else "anthropic"
if _provider not in _VALID_PROVIDERS:
    sys.exit(f"ERROR: invalid LLM_PROVIDER '{_provider}'. Must be one of: {sorted(_VALID_PROVIDERS)}")
PROVIDER = _provider

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-6")
BEDROCK_NOVA_MODEL_ID = os.getenv("BEDROCK_NOVA_MODEL_ID", "amazon.nova-lite-v1:0")

BEDROCK_KB_ID = os.getenv("BEDROCK_KB_ID", "")
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "DRAFT")
KB_TOP_K = int(os.getenv("KB_TOP_K", "5"))
TEAMS_WEBHOOK = os.getenv("TEAMS_WEBHOOK_URL", "")


# --- RETRIEVAL -------------------------------------------------------------

def retrieve_policy_chunks(query: str) -> list[dict[str, Any]]:
    """Call Bedrock Knowledge Base retrieve API for top-K policy chunks."""
    if not BEDROCK_KB_ID:
        sys.exit("ERROR: BEDROCK_KB_ID env var is not set. Cannot retrieve from Knowledge Base.")
    client = boto3.client("bedrock-agent-runtime", region_name=REGION)
    resp = client.retrieve(
        knowledgeBaseId=BEDROCK_KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": KB_TOP_K},
        },
    )
    chunks = []
    for r in resp.get("retrievalResults", []):
        chunks.append({
            "text": r.get("content", {}).get("text", ""),
            "score": r.get("score"),
            "source": r.get("location", {}).get("s3Location", {}).get("uri", "unknown"),
            "metadata": r.get("metadata", {}),
        })
    return chunks


# --- REASONING -------------------------------------------------------------

SYSTEM_PROMPT = """You are ITOpsOrchestrator, an IT policy reconciliation analyst at Nayak Fictional Insurance.

Your job: given a user helpdesk ticket and a set of retrieved policy excerpts (from SharePoint Acceptable Use Policy, Zscaler URL filtering rules, AWS configuration documents, and similar sources), determine whether the issue is a CROSS-DOMAIN POLICY CONFLICT, and if so, identify the two documents that disagree.

A cross-domain policy conflict exists when written policy permits or requires an action while a technical control silently prevents it (or vice versa).

Return STRICT JSON with this exact schema, no commentary:

{
  "conflict_detected": true | false,
  "confidence": 0.0 to 1.0,
  "summary": "one-sentence summary of what is happening",
  "ticket_user_intent": "what the user was trying to do",
  "policy_a": {
    "source": "filename or identifier of the policy that PERMITS the action",
    "excerpt": "verbatim quoted clause from the retrieved chunks",
    "interpretation": "one sentence on what this clause asserts"
  },
  "policy_b": {
    "source": "filename or identifier of the policy that BLOCKS the action",
    "excerpt": "verbatim quoted clause or rule from the retrieved chunks",
    "interpretation": "one sentence on what this clause asserts"
  },
  "recommendation": "concrete next step for the IT Service Desk agent and the policy owner",
  "severity": "critical | high | medium | low | informational"
}

Severity guidance:
- critical: a compliance-breaking conflict (regulated data, NAIC, PCI-DSS)
- high: business-disrupting conflict affecting many users
- medium: a single user impact with a clear workaround
- low: documentation ambiguity, not a true conflict
- informational: no conflict; another root cause likely

If you cannot find clear evidence of a conflict in the retrieved chunks, set conflict_detected=false and explain the most likely alternative root cause in the recommendation field. Set confidence accordingly.

Return ONLY the JSON object."""


def reason_with_llm(ticket_text: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    chunks_for_prompt = []
    for i, c in enumerate(chunks, start=1):
        chunks_for_prompt.append(
            f"--- Chunk {i} ---\n"
            f"Source: {c['source']}\n"
            f"Retrieval score: {c.get('score')}\n"
            f"Text:\n{c['text']}\n"
        )
    user_msg = (
        "Helpdesk ticket:\n"
        "---------------\n"
        + ticket_text.strip()
        + "\n---------------\n\n"
        + "Retrieved policy chunks (top "
        + str(len(chunks))
        + "):\n\n"
        + "\n".join(chunks_for_prompt)
        + "\n\nReturn the JSON finding now."
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
        model_id = BEDROCK_NOVA_MODEL_ID if PROVIDER == "bedrock_nova" else BEDROCK_MODEL_ID
        br = boto3.client("bedrock-runtime", region_name=REGION)
        converse_kwargs: dict[str, Any] = dict(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            system=[{"text": SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0},
        )
        if GUARDRAIL_ID:
            converse_kwargs["guardrailConfig"] = {
                "guardrailIdentifier": GUARDRAIL_ID,
                "guardrailVersion": GUARDRAIL_VERSION,
            }
        resp = br.converse(**converse_kwargs)
        if resp.get("stopReason") == "guardrail_intervened":
            actions = (
                resp.get("trace", {})
                    .get("guardrail", {})
                    .get("outputAssessments", {})
            )
            print(f"  [GUARDRAIL] Response blocked by guardrail {GUARDRAIL_ID} v{GUARDRAIL_VERSION}. "
                  f"Actions: {actions}")
            return {
                "conflict_detected": True,
                "confidence": 1.0,
                "summary": (
                    f"Bedrock guardrail {GUARDRAIL_ID} (v{GUARDRAIL_VERSION}) blocked the model "
                    "response — the ticket contains content that violates a configured guardrail policy."
                ),
                "ticket_user_intent": "See original ticket.",
                "policy_a": {
                    "source": "Bedrock Guardrail",
                    "excerpt": f"Guardrail ID {GUARDRAIL_ID} version {GUARDRAIL_VERSION}",
                    "interpretation": "The guardrail determined this request violates a restricted topic or contains sensitive information.",
                },
                "policy_b": {
                    "source": "Submitted helpdesk ticket",
                    "excerpt": "See original ticket text.",
                    "interpretation": "The ticket contains a request that triggered the guardrail intervention.",
                },
                "recommendation": (
                    "Escalate to the IT Security team. Do not fulfill this request without explicit "
                    "approval from the CISO or designated security officer. Review guardrail trace "
                    "in CloudWatch for the specific policy that was triggered."
                ),
                "severity": "critical",
            }
        text = resp["output"]["message"]["content"][0]["text"]

    # Strip code fences if the model wrapped its JSON
    text = text.strip()
    if not text:
        raise ValueError("LLM returned an empty response — cannot parse finding.")
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


# --- OUTPUT (TERMINAL + TEAMS) --------------------------------------------

def _provider_label() -> str:
    if PROVIDER == "anthropic":
        return f"Anthropic API ({ANTHROPIC_MODEL})"
    if PROVIDER == "bedrock_nova":
        return f"Bedrock Nova ({BEDROCK_NOVA_MODEL_ID})"
    return f"Bedrock Claude ({BEDROCK_MODEL_ID})"


STYLE_MAP = {
    "critical": "attention",
    "high": "attention",
    "medium": "warning",
    "low": "accent",
    "informational": "good",
}


# ANSI colors — disabled automatically when stdout is not a TTY.
_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR", "") == ""

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "blue": "\033[34m",
    "white": "\033[97m",
}

_SEVERITY_COLOR = {
    "critical": "red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "informational": "green",
}


def _c(text: str, *styles: str) -> str:
    if not _USE_COLOR:
        return text
    prefix = "".join(_ANSI.get(s, "") for s in styles)
    return f"{prefix}{text}{_ANSI['reset']}"


def _wrap(text: str, width: int = 92, indent: str = "  ") -> str:
    import textwrap
    if not text:
        return f"{indent}{_c('(empty)', 'dim')}"
    paragraphs = text.split("\n")
    out = []
    for p in paragraphs:
        if not p.strip():
            out.append("")
            continue
        out.append(textwrap.fill(
            p, width=width,
            initial_indent=indent, subsequent_indent=indent,
            break_long_words=False, break_on_hyphens=False,
        ))
    return "\n".join(out)


def _hr(char: str = "─", width: int = 92) -> str:
    return _c(char * width, "dim")


def _section(title: str) -> str:
    return _c(f"▸ {title}", "bold", "cyan")


def print_finding_to_terminal(finding: dict[str, Any], ticket_id: str | None) -> None:
    """Render the finding as a clean, human-readable block on stdout."""
    severity = (finding.get("severity") or "informational").lower()
    conflict = bool(finding.get("conflict_detected", False))
    confidence = float(finding.get("confidence") or 0.0)
    sev_color = _SEVERITY_COLOR.get(severity, "white")

    status_label = "CONFLICT DETECTED" if conflict else "NO CONFLICT DETECTED"
    status_color = "red" if conflict else "green"

    print()
    print(_hr("═"))
    header = _c("  ITOpsOrchestrator — UC-01 Policy Conflict Finding", "bold", "white")
    print(header)
    if ticket_id:
        print(_c(f"  Ticket: {ticket_id}", "dim"))
    print(_hr("═"))

    print(f"  {_c('Status      :', 'bold')} {_c(status_label, 'bold', status_color)}")
    print(f"  {_c('Severity    :', 'bold')} {_c(severity.upper(), 'bold', sev_color)}")
    print(f"  {_c('Confidence  :', 'bold')} {confidence:.0%}")
    print(f"  {_c('Scored by   :', 'bold')} {_provider_label()}")
    print(f"  {_c('Region      :', 'bold')} {REGION}")
    print(_hr())

    print(_section("Summary"))
    print(_wrap(finding.get("summary", "")))
    print()

    print(_section("User intent"))
    print(_wrap(finding.get("ticket_user_intent", "")))
    print()

    if conflict:
        pa = finding.get("policy_a", {}) or {}
        pb = finding.get("policy_b", {}) or {}

        print(_section("Policy A  (permits the action)"))
        print(_wrap(_c(f"Source: {pa.get('source', '(unknown)')}", "magenta")))
        print(_wrap(f'"{pa.get("excerpt", "")}"'))
        print(_wrap(_c(pa.get("interpretation", ""), "dim")))
        print()

        print(_section("Policy B  (blocks the action)"))
        print(_wrap(_c(f"Source: {pb.get('source', '(unknown)')}", "magenta")))
        print(_wrap(f'"{pb.get("excerpt", "")}"'))
        print(_wrap(_c(pb.get("interpretation", ""), "dim")))
        print()

    print(_section("Recommended action"))
    print(_wrap(finding.get("recommendation", "")))
    print(_hr("═"))
    print()


def post_finding_to_teams(finding: dict[str, Any], ticket_id: str | None) -> None:
    if not TEAMS_WEBHOOK:
        print("TEAMS_WEBHOOK_URL not set; skipping post.")
        return

    severity = finding.get("severity", "informational")
    conflict = finding.get("conflict_detected", False)
    confidence = finding.get("confidence", 0.0)
    style = STYLE_MAP.get(severity, "default")

    title_text = (
        f"ITOpsOrchestrator policy conflict {'DETECTED' if conflict else 'not detected'}"
        + (f"  ·  Ticket {ticket_id}" if ticket_id else "")
    )

    body_items: list[dict[str, Any]] = [
        {
            "type": "Container",
            "style": style,
            "bleed": True,
            "items": [
                {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title_text, "wrap": True},
                {"type": "TextBlock", "spacing": "Small", "isSubtle": True,
                 "text": f"Severity: {severity.upper()}  ·  Confidence: {confidence:.0%}  ·  Region: {REGION}",
                 "wrap": True},
                {"type": "TextBlock", "spacing": "None", "isSubtle": True, "size": "Small",
                 "text": f"Scored by {_provider_label()}", "wrap": True},
            ],
        },
        {
            "type": "TextBlock", "weight": "Bolder", "spacing": "Medium",
            "text": "Summary", "wrap": True,
        },
        {"type": "TextBlock", "text": finding.get("summary", ""), "wrap": True, "spacing": "Small"},
        {"type": "TextBlock", "weight": "Bolder", "spacing": "Medium",
         "text": "User intent", "wrap": True},
        {"type": "TextBlock", "text": finding.get("ticket_user_intent", ""), "wrap": True, "spacing": "Small"},
    ]

    if conflict:
        pa = finding.get("policy_a", {}) or {}
        pb = finding.get("policy_b", {}) or {}
        body_items.extend([
            {"type": "TextBlock", "weight": "Bolder", "spacing": "Medium",
             "text": "Policy A (permits)", "wrap": True},
            {"type": "TextBlock", "isSubtle": True, "size": "Small",
             "text": pa.get("source", ""), "wrap": True, "spacing": "None"},
            {"type": "TextBlock", "text": pa.get("excerpt", ""), "wrap": True, "spacing": "Small"},
            {"type": "TextBlock", "isSubtle": True, "size": "Small",
             "text": pa.get("interpretation", ""), "wrap": True, "spacing": "None"},

            {"type": "TextBlock", "weight": "Bolder", "spacing": "Medium",
             "text": "Policy B (blocks)", "wrap": True},
            {"type": "TextBlock", "isSubtle": True, "size": "Small",
             "text": pb.get("source", ""), "wrap": True, "spacing": "None"},
            {"type": "TextBlock", "text": pb.get("excerpt", ""), "wrap": True, "spacing": "Small"},
            {"type": "TextBlock", "isSubtle": True, "size": "Small",
             "text": pb.get("interpretation", ""), "wrap": True, "spacing": "None"},
        ])

    body_items.extend([
        {"type": "TextBlock", "weight": "Bolder", "spacing": "Medium",
         "text": "Recommended action", "wrap": True},
        {"type": "TextBlock", "text": finding.get("recommendation", ""), "wrap": True, "spacing": "Small"},
    ])

    adaptive_card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body_items,
    }
    payload = {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": adaptive_card},
        ],
    }
    r = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print(f"Posted finding to Teams (HTTP {r.status_code}).")


# --- COMMANDS --------------------------------------------------------------

def cmd_kb_check() -> None:
    if not BEDROCK_KB_ID:
        sys.exit("ERROR: BEDROCK_KB_ID env var is not set.")
    client = boto3.client("bedrock-agent", region_name=REGION)
    try:
        resp = client.get_knowledge_base(knowledgeBaseId=BEDROCK_KB_ID)
        kb = resp["knowledgeBase"]
        print(f"Knowledge Base:  {kb['name']}")
        print(f"  id:            {kb['knowledgeBaseId']}")
        print(f"  status:        {kb['status']}")
        print(f"  created:       {kb.get('createdAt', 'unknown')}")
        print(f"  storage:       {kb.get('storageConfiguration', {}).get('type', 'unknown')}")
    except botocore.exceptions.ClientError as e:
        sys.exit(f"ERROR getting KB: {e}")

    # Probe a sample retrieval
    sample = retrieve_policy_chunks("Is Dropbox approved for business use?")
    print(f"\nProbe retrieval for 'Is Dropbox approved for business use?':")
    print(f"  returned {len(sample)} chunks")
    for i, c in enumerate(sample, start=1):
        print(f"  chunk {i}: score={c.get('score')}, source={c['source']}")


def _extract_ticket_id(text: str) -> str | None:
    import re
    m = re.search(r"\b(INC\d{6,}|REQ\d{6,}|TKT\d{4,})\b", text)
    return m.group(1) if m else None


def _build_retrieval_query(ticket_text: str) -> str:
    """Heuristic: pull the obvious nouns out of the ticket; fall back to a fixed prompt."""
    text = ticket_text.lower()
    keywords = []
    for kw in ["dropbox", "onedrive", "sharepoint", "box.com", "google drive",
               "zscaler", "blocked", "cloud storage", "access denied", "vpn"]:
        if kw in text:
            keywords.append(kw)
    if not keywords:
        return "Why is this user blocked? Look for relevant policies and Zscaler URL filtering rules."
    return (
        f"Cross-reference policy on these topics: {', '.join(keywords)}. "
        "Include both the SharePoint Acceptable Use Policy stance and the Zscaler URL filtering rule."
    )


def cmd_analyze(ticket_path: str, post_to_teams: bool = False, show_json: bool = False) -> None:
    print(f"Provider: {_provider_label()}")
    print(f"KB:       {BEDROCK_KB_ID or '(not set)'}")
    print(f"Region:   {REGION}")

    ticket_text = Path(ticket_path).read_text(encoding="utf-8")
    ticket_id = _extract_ticket_id(ticket_text)
    print(f"\nTicket file: {ticket_path}")
    print(f"Ticket id:   {ticket_id or 'not detected'}")

    print("\nStep 1/3  Building retrieval query from the ticket...")
    query = _build_retrieval_query(ticket_text)
    print(f"  query: {query}")

    print("\nStep 2/3  Retrieving from Bedrock Knowledge Base...")
    chunks = retrieve_policy_chunks(query)
    print(f"  retrieved {len(chunks)} chunks")
    for i, c in enumerate(chunks, start=1):
        print(f"  chunk {i}: score={c.get('score'):.3f}  source={c['source']}")

    print("\nStep 3/3  Reasoning with the model...")
    finding = reason_with_llm(ticket_text, chunks)

    print_finding_to_terminal(finding, ticket_id)

    if show_json:
        print(_c("Raw JSON finding:", "bold", "dim"))
        print(json.dumps(finding, indent=2))
        print()

    if post_to_teams:
        print("Posting to MS Teams...")
        post_finding_to_teams(finding, ticket_id)
    else:
        print(_c("Teams post skipped (pass --teams to enable).", "dim"))
    print("Done.")


# --- ENTRY -----------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="ITOpsOrchestrator UC-01 policy-conflict agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("kb-check")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("--ticket", required=True, help="path to a helpdesk ticket text file")
    analyze.add_argument(
        "--teams", action="store_true",
        help="also post the finding to MS Teams via TEAMS_WEBHOOK_URL (off by default)",
    )
    analyze.add_argument(
        "--json", dest="show_json", action="store_true",
        help="additionally print the raw JSON finding after the formatted view",
    )
    args = ap.parse_args()

    t0 = time.time()
    try:
        if args.cmd == "kb-check":
            cmd_kb_check()
        elif args.cmd == "analyze":
            cmd_analyze(args.ticket, post_to_teams=args.teams, show_json=args.show_json)
    except botocore.exceptions.NoCredentialsError:
        sys.exit("ERROR: AWS credentials not found. Run `aws configure` or set AWS_PROFILE.")
    except FileNotFoundError as e:
        sys.exit(f"ERROR: ticket file not found: {e}")
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    print(f"\nElapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
