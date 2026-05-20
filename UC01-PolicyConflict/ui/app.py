"""
FastAPI UI for the UC-01 Policy Conflict agent on Bedrock AgentCore.

Replaces the Streamlit UI (which has WebSocket compatibility issues behind
App Runner's TLS-terminating Envoy proxy). Single-file app:
  1. Basic-auth wall — password fetched from Secrets Manager (or env).
  2. Ticket paste / upload / sample-tab form.
  3. POSTs to AgentCore Runtime via boto3 and renders the finding.

Environment variables:
  AGENT_RUNTIME_ARN          required — ARN from `agentcore launch`
  AWS_REGION                 default us-east-1
  UI_PASSWORD_SECRET_NAME    optional — Secrets Manager secret holding the UI password
  UI_PASSWORD                optional — fallback plaintext password for local dev
"""
from __future__ import annotations

import json
import os
import secrets
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

import boto3
import botocore
from fastapi import Cookie, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse


REGION = os.getenv("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN", "").strip()
UI_PASSWORD_SECRET_NAME = os.getenv("UI_PASSWORD_SECRET_NAME", "").strip()
UI_PASSWORD_ENV = os.getenv("UI_PASSWORD", "").strip()

SAMPLE_DIR = Path("/app/sample-tickets")

# In-memory session store. Single-instance App Runner, fine for POC.
SESSIONS: set[str] = set()

app = FastAPI(title="ITOpsOrchestrator UC-01")


# ---------- auth ----------

@lru_cache(maxsize=1)
def expected_password() -> str:
    if UI_PASSWORD_SECRET_NAME:
        sm = boto3.client("secretsmanager", region_name=REGION)
        return sm.get_secret_value(SecretId=UI_PASSWORD_SECRET_NAME)["SecretString"].strip()
    if UI_PASSWORD_ENV:
        return UI_PASSWORD_ENV
    raise RuntimeError("No UI password configured. Set UI_PASSWORD_SECRET_NAME or UI_PASSWORD.")


def authenticated(session: str | None) -> bool:
    return bool(session and session in SESSIONS)


# ---------- agent invocation ----------

def invoke_agent(ticket_text: str, post_to_teams: bool) -> dict[str, Any]:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    payload = {"ticket_text": ticket_text, "post_to_teams": post_to_teams}
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read()
    return json.loads(body)


# ---------- HTML ----------

CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f4f6f8; color: #1f2933; }
  header { background: #1f2937; color: #fff; padding: 1rem 2rem; }
  header h1 { margin: 0; font-size: 1.25rem; }
  header .meta { font-size: 0.8rem; opacity: 0.7; margin-top: 0.25rem; }
  main { max-width: 1100px; margin: 1.5rem auto; padding: 0 1rem; }
  .card { background: #fff; border-radius: 8px; padding: 1.25rem 1.5rem; margin-bottom: 1rem;
          box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
  textarea, input[type=password], input[type=text] { width: 100%; padding: 0.6rem; border: 1px solid #d1d5db;
                                                      border-radius: 6px; font-family: inherit; font-size: 0.95rem; }
  textarea { min-height: 200px; resize: vertical; }
  button { background: #2563eb; color: #fff; border: 0; padding: 0.6rem 1.2rem; border-radius: 6px;
           font-weight: 600; cursor: pointer; font-size: 0.95rem; }
  button:hover { background: #1d4ed8; }
  button[disabled] { opacity: 0.5; cursor: not-allowed; }
  .row { display: flex; gap: 1rem; align-items: center; margin-top: 1rem; }
  .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; border-bottom: 1px solid #d1d5db; }
  .tabs a { padding: 0.5rem 1rem; text-decoration: none; color: #4b5563; border-bottom: 2px solid transparent; }
  .tabs a.active { color: #1f2937; border-bottom-color: #2563eb; font-weight: 600; }
  .badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.8rem;
           font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .badge.critical, .badge.high { background: #fecaca; color: #991b1b; }
  .badge.medium { background: #fde68a; color: #92400e; }
  .badge.low { background: #bfdbfe; color: #1e40af; }
  .badge.informational, .badge.no_conflict { background: #bbf7d0; color: #065f46; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  .policy { background: #f9fafb; border-left: 4px solid #6b7280; padding: 1rem; border-radius: 4px; }
  .policy.permits { border-left-color: #10b981; }
  .policy.blocks  { border-left-color: #ef4444; }
  .policy h3 { margin: 0 0 0.25rem 0; font-size: 1rem; }
  .policy .src { color: #6b7280; font-size: 0.85rem; margin-bottom: 0.5rem; }
  .policy .excerpt { background: #fff; padding: 0.75rem; border-radius: 4px; border: 1px solid #e5e7eb;
                     font-size: 0.9rem; white-space: pre-wrap; }
  .policy .interp { color: #4b5563; font-size: 0.85rem; margin-top: 0.5rem; font-style: italic; }
  .rec { background: #ecfdf5; border-left: 4px solid #10b981; padding: 1rem; border-radius: 4px;
         white-space: pre-wrap; line-height: 1.45; }
  details { margin-top: 1rem; }
  details summary { cursor: pointer; color: #2563eb; font-weight: 600; }
  pre { background: #1f2937; color: #f9fafb; padding: 1rem; border-radius: 6px; overflow-x: auto;
        font-size: 0.8rem; }
  .err { background: #fee2e2; color: #991b1b; padding: 0.75rem; border-radius: 6px; margin-bottom: 1rem; }
  label.check { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; color: #4b5563; }
"""

def page(body: str, title: str = "ITOpsOrchestrator UC-01") -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head><body>
<header>
  <h1>🛡️ ITOpsOrchestrator — Policy Conflict Detection</h1>
  <div class="meta">Region: {escape(REGION)} · Agent: {escape(AGENT_RUNTIME_ARN or '(not set)')}</div>
</header>
<main>{body}</main>
</body></html>"""


def login_page(error: str = "") -> str:
    err_html = f'<div class="err">{escape(error)}</div>' if error else ""
    return page(f"""
      <div class="card" style="max-width:420px;margin:2rem auto;">
        <h2 style="margin-top:0">🔐 Sign in</h2>
        {err_html}
        <form method="post" action="/login">
          <label>Password<br><input type="password" name="password" autofocus required></label>
          <div class="row"><button type="submit">Sign in</button></div>
        </form>
      </div>""", title="Sign in")


def sample_list() -> list[str]:
    if not SAMPLE_DIR.exists():
        return []
    return sorted(p.name for p in SAMPLE_DIR.glob("*.txt"))


def form_page(prefill: str = "", active_tab: str = "paste", error: str = "", finding_html: str = "") -> str:
    tabs = {"paste": "Paste ticket", "upload": "Upload .txt", "sample": "Sample ticket"}
    tabs_html = '<div class="tabs">' + "".join(
        f'<a href="?tab={k}" class="{"active" if k == active_tab else ""}">{escape(v)}</a>'
        for k, v in tabs.items()
    ) + "</div>"

    if active_tab == "upload":
        body_tab = """
          <form method="post" action="/upload" enctype="multipart/form-data">
            <input type="file" name="file" accept=".txt" required>
            <div class="row"><button type="submit">Load file</button></div>
          </form>"""
    elif active_tab == "sample":
        opts = "".join(f'<option value="{escape(s)}">{escape(s)}</option>' for s in sample_list())
        body_tab = f"""
          <form method="post" action="/load-sample">
            <label>Bundled samples<br>
              <select name="name" required>
                <option value="">— pick one —</option>{opts}
              </select>
            </label>
            <div class="row"><button type="submit">Load sample</button></div>
          </form>"""
    else:
        body_tab = f"""
          <form method="post" action="/analyze">
            <label>Helpdesk ticket text<br>
              <textarea name="ticket_text" placeholder="Paste the ticket body here..." required>{escape(prefill)}</textarea>
            </label>
            <div class="row">
              <label class="check"><input type="checkbox" name="post_to_teams"> Also post to MS Teams</label>
              <button type="submit">Analyze</button>
            </div>
          </form>"""

    err = f'<div class="err">{escape(error)}</div>' if error else ""
    return page(f"""
      <div class="card">
        {tabs_html}
        {err}
        {body_tab}
      </div>
      {finding_html}
      <p style="text-align:right"><a href="/logout">Sign out</a></p>""")


def render_finding(result: dict[str, Any]) -> str:
    finding = result.get("finding") or {}
    severity = (finding.get("severity") or "informational").lower()
    conflict = bool(finding.get("conflict_detected"))
    confidence = float(finding.get("confidence") or 0.0)
    badge_class = severity if conflict else "no_conflict"
    title = "CONFLICT DETECTED" if conflict else "NO CONFLICT DETECTED"
    ticket_id = result.get("ticket_id") or ""

    conflict_html = ""
    if conflict:
        pa = finding.get("policy_a") or {}
        pb = finding.get("policy_b") or {}
        conflict_html = f"""
          <div class="grid">
            <div class="policy permits">
              <h3>📄 Policy A — permits</h3>
              <div class="src">{escape(pa.get("source", ""))}</div>
              <div class="excerpt">{escape(pa.get("excerpt", ""))}</div>
              <div class="interp">{escape(pa.get("interpretation", ""))}</div>
            </div>
            <div class="policy blocks">
              <h3>🛑 Policy B — blocks</h3>
              <div class="src">{escape(pb.get("source", ""))}</div>
              <div class="excerpt">{escape(pb.get("excerpt", ""))}</div>
              <div class="interp">{escape(pb.get("interpretation", ""))}</div>
            </div>
          </div>"""

    teams_err = finding.get("_teams_post_error")
    teams_err_html = f'<div class="err">Teams post failed: {escape(teams_err)}</div>' if teams_err else ""

    chunks_html = ""
    for i, c in enumerate(result.get("chunks") or [], start=1):
        score = c.get("score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
        chunks_html += f"""
          <div style="margin-bottom:0.75rem">
            <strong>Chunk {i}</strong> — score {escape(score_s)} — <code>{escape(c.get("source", ""))}</code>
            <pre style="white-space:pre-wrap;max-height:200px">{escape((c.get("text") or "")[:1500])}</pre>
          </div>"""

    return f"""
      <div class="card">
        <h2 style="margin-top:0">
          <span class="badge {badge_class}">{escape(title)}</span>
          <span style="margin-left:0.5rem;color:#6b7280">
            Severity: {escape(severity.upper())} · Confidence: {confidence:.0%}
            {f' · Ticket: <code>{escape(ticket_id)}</code>' if ticket_id else ''}
          </span>
        </h2>
        {teams_err_html}
        <h3>Summary</h3><p>{escape(finding.get("summary", ""))}</p>
        <h3>User intent</h3><p>{escape(finding.get("ticket_user_intent", ""))}</p>
        {conflict_html}
        <h3>✅ Recommended action</h3>
        <div class="rec">{escape(finding.get("recommendation", ""))}</div>
        <details>
          <summary>🔍 Retrieval details ({len(result.get("chunks") or [])} chunks)</summary>
          <p><strong>Query:</strong> <code>{escape(result.get("retrieval_query", ""))}</code></p>
          {chunks_html}
        </details>
        <details>
          <summary>🧾 Raw JSON</summary>
          <pre>{escape(json.dumps(result, indent=2))}</pre>
        </details>
      </div>"""


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index(tab: str = "paste", session: str | None = Cookie(None)):
    if not authenticated(session):
        return HTMLResponse(login_page())
    return HTMLResponse(form_page(active_tab=tab))


@app.post("/login", response_class=HTMLResponse)
def login(password: str = Form(...)):
    try:
        if password and password == expected_password():
            token = secrets.token_urlsafe(32)
            SESSIONS.add(token)
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie("session", token, httponly=True, secure=True, samesite="strict")
            return resp
    except Exception as e:
        return HTMLResponse(login_page(error=f"Could not verify password: {e}"))
    return HTMLResponse(login_page(error="Incorrect password."))


@app.get("/logout")
def logout(session: str | None = Cookie(None)):
    if session:
        SESSIONS.discard(session)
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.post("/upload", response_class=HTMLResponse)
async def upload(file: UploadFile = File(...), session: str | None = Cookie(None)):
    if not authenticated(session):
        return RedirectResponse(url="/", status_code=303)
    content = (await file.read()).decode("utf-8", errors="replace")
    return HTMLResponse(form_page(prefill=content, active_tab="paste"))


@app.post("/load-sample", response_class=HTMLResponse)
def load_sample(name: str = Form(...), session: str | None = Cookie(None)):
    if not authenticated(session):
        return RedirectResponse(url="/", status_code=303)
    path = SAMPLE_DIR / name
    if not path.exists() or ".." in name or "/" in name:
        return HTMLResponse(form_page(active_tab="sample", error=f"Sample not found: {name}"))
    return HTMLResponse(form_page(prefill=path.read_text(encoding="utf-8"), active_tab="paste"))


@app.post("/analyze", response_class=HTMLResponse)
def analyze(
    ticket_text: str = Form(...),
    post_to_teams: str | None = Form(None),
    session: str | None = Cookie(None),
):
    if not authenticated(session):
        return RedirectResponse(url="/", status_code=303)
    if not AGENT_RUNTIME_ARN:
        return HTMLResponse(form_page(prefill=ticket_text, error="AGENT_RUNTIME_ARN is not set."))
    try:
        result = invoke_agent(ticket_text, post_to_teams is not None)
    except botocore.exceptions.ClientError as e:
        return HTMLResponse(form_page(prefill=ticket_text, error=f"AgentCore invocation failed: {e}"))
    except Exception as e:
        return HTMLResponse(form_page(prefill=ticket_text, error=f"Unexpected error: {e}"))
    return HTMLResponse(form_page(prefill=ticket_text, finding_html=render_finding(result)))


@app.get("/healthz")
def healthz():
    return {"ok": True}
