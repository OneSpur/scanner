#!/usr/bin/env python3
"""1Spur Scanner - entry point"""

import asyncio
import base64 as _base64
import json
import os
import secrets as _secrets
import uuid
from pathlib import Path
from urllib.parse import quote as _urlquote

import httpx
import uvicorn
from fastapi import FastAPI, Request, Form, File, UploadFile
from typing import List
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from scanner.github import fetch_repo_data as _gh_fetch_repo_data, _get_all_pages, _fetch_pr_details, _estimate_rework_hours, fetch_commits_with_diffs, fetch_repo_files
from scanner.gitlab import fetch_repo_data as _gl_fetch_repo_data, _get_all_pages as _gl_get_all_pages, _fetch_mr_details, _estimate_rework_hours as _gl_estimate_rework_hours, _parse_project_info
from scanner.analyzer import analyze_drift, analyze_spec
from scanner.report import generate_report
from scanner.jira import enrich_pairs_with_jira, expand_jira_ids, fetch_jira_ticket, test_jira_connection
from scanner.linear import enrich_pairs_with_linear, extract_linear_ids, fetch_linear_issue
from scanner.slack import enrich_pairs_with_slack

# Knowledge-graph pipeline (optional — gracefully skipped if deps missing)
try:
    from scanner.pipeline import run_pipeline as _run_kg_pipeline
    _KG_AVAILABLE = True
except ImportError:
    _KG_AVAILABLE = False

BASE = Path(__file__).parent
app = FastAPI()

# Session middleware — required for OAuth CSRF state storage
_SESSION_SECRET = os.getenv("SESSION_SECRET") or _secrets.token_hex(32)
app.add_middleware(SessionMiddleware, secret_key=_SESSION_SECRET, same_site="lax", https_only=False)

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

# In-memory job store + disk persistence
jobs: dict[str, dict] = {}

# Prefetch cache: key → {status, repo_data, estimate, error}
_prefetch_cache: dict[str, dict] = {}
_JOBS_DIR = BASE / ".." / "reports"
_JOBS_DIR.mkdir(exist_ok=True)

def _save_job(job_id: str, job: dict) -> None:
    try:
        payload = {"repo_data": job["repo_data"], "analysis": job["analysis"]}
        (_JOBS_DIR / f"{job_id}.json").write_text(json.dumps(payload))
    except Exception:
        pass

def _load_job(job_id: str) -> dict | None:
    path = _JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        return {"status": "done", "repo_data": payload["repo_data"], "analysis": payload["analysis"], "report": None}
    except Exception:
        return None

# ── OAuth (auto-mode) ─────────────────────────────────────────────────────────
# When GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET etc. are set as env vars,
# 1Spur Scanner handles OAuth directly (no relay needed).
# If not set, falls back to the 1spur relay.

_BASE_URL  = os.getenv("SCANNER_BASE_URL", "http://localhost:7433").rstrip("/")
_RELAY_URL = os.getenv("SCANNER_RELAY_URL", "https://relay.1spur.app").rstrip("/")

# Per-provider OAuth configs.  Only active when client_id + client_secret are set.
_OAUTH_CONFIGS: dict[str, dict] = {
    "github": {
        "client_id":     os.getenv("GITHUB_CLIENT_ID", ""),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
        "auth_url":      "https://github.com/login/oauth/authorize",
        "token_url":     "https://github.com/login/oauth/access_token",
        "scope":         "repo read:org",
    },
    "gitlab": {
        "client_id":     os.getenv("GITLAB_CLIENT_ID", ""),
        "client_secret": os.getenv("GITLAB_CLIENT_SECRET", ""),
        "auth_url":      "https://gitlab.com/oauth/authorize",
        "token_url":     "https://gitlab.com/oauth/token",
        "scope":         "read_api",
    },
    "jira": {
        "client_id":     os.getenv("JIRA_CLIENT_ID", ""),
        "client_secret": os.getenv("JIRA_CLIENT_SECRET", ""),
        "auth_url":      "https://auth.atlassian.com/authorize",
        "token_url":     "https://auth.atlassian.com/oauth/token",
        "scope":         "read:jira-work offline_access",
        # Atlassian-specific: audience + prompt required
        "extra_auth_params": {"audience": "api.atlassian.com", "prompt": "consent"},
    },
    "linear": {
        "client_id":     os.getenv("LINEAR_CLIENT_ID", ""),
        "client_secret": os.getenv("LINEAR_CLIENT_SECRET", ""),
        "auth_url":      "https://linear.app/oauth/authorize",
        "token_url":     "https://api.linear.app/oauth/token",
        "scope":         "read",
    },
    "slack": {
        "client_id":     os.getenv("SLACK_CLIENT_ID", ""),
        "client_secret": os.getenv("SLACK_CLIENT_SECRET", ""),
        "auth_url":      "https://slack.com/oauth/v2/authorize",
        "token_url":     "https://slack.com/api/oauth.v2.access",
        # user_scope — search:read requires user token (xoxp-), not bot token
        "user_scope":    "search:read,channels:history,groups:history,channels:read,groups:read",
    },
}

def _oauth_configured(service: str) -> bool:
    cfg = _OAUTH_CONFIGS.get(service, {})
    return bool(cfg.get("client_id") and cfg.get("client_secret"))



async def _extract_file_text(upload: UploadFile) -> str:
    """Extract plain text from uploaded context files (.txt, .md, .pdf, .docx)."""
    data = await upload.read()
    name = (upload.filename or "").lower()
    try:
        if name.endswith(".pdf"):
            try:
                import pypdf, io
                reader = pypdf.PdfReader(io.BytesIO(data))
                return "\n".join(p.extract_text() or "" for p in reader.pages)
            except ImportError:
                return data.decode("utf-8", errors="ignore")
        if name.endswith(".docx"):
            try:
                import docx, io
                doc = docx.Document(io.BytesIO(data))
                return "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                return data.decode("utf-8", errors="ignore")
        # .txt, .md, and anything else — decode as utf-8
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _detect_platform(repo_url: str) -> str:
    """Return 'github' or 'gitlab' based on the repo URL host."""
    url_lower = repo_url.lower()
    if "github.com" in url_lower:
        return "github"
    return "gitlab"  # gitlab.com or any self-hosted GitLab instance


def _normalize_jira_url(raw: str) -> str:
    """
    Accept any Jira URL the user pastes (board URL, issue URL, or base URL)
    and return just the scheme+host needed for REST API calls.
    e.g. https://company.atlassian.net/jira/software/projects/QP/boards/1
         → https://company.atlassian.net

    Exception: Atlassian cloud API URLs produced by OAuth auto-mode
    (containing /ex/jira/{cloudId}) are returned unchanged because the
    cloud ID path segment is required for API calls.
    """
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    # Cloud API URL from OAuth — preserve the full path
    if "api.atlassian.com/ex/jira/" in raw:
        return raw
    try:
        from urllib.parse import urlparse
        p = urlparse(raw)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return raw


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/analyze")
async def start_analysis(
    request: Request,
    repo_url: str = Form(...),
    github_token: str = Form(""),
    gitlab_token: str = Form(""),
    period_days: int = Form(30),
    provider: str = Form("ollama"),
    model: str = Form("llama3.2:3b"),
    api_key: str = Form(""),
    jira_url: str = Form(""),
    jira_email: str = Form(""),
    jira_token: str = Form(""),
    jira_refresh_token: str = Form(""),
    linear_token: str = Form(""),
    slack_token: str = Form(""),
    slack_channels: str = Form(""),
    context_files: List[UploadFile] = File(default=[]),
):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": [], "report": None}

    _repo_url = repo_url.strip().rstrip("/")
    _platform = _detect_platform(_repo_url)

    # Extract text from uploaded context files
    extra_context: str | None = None
    if context_files:
        texts = []
        for f in context_files:
            if f.filename:
                text = await _extract_file_text(f)
                if text.strip():
                    texts.append(f"=== {f.filename} ===\n{text}")
        if texts:
            extra_context = "\n\n".join(texts)

    # Kick off background analysis
    asyncio.create_task(run_analysis(
        job_id=job_id,
        repo_url=_repo_url,
        platform=_platform,
        github_token=github_token.strip() or None,
        gitlab_token=gitlab_token.strip() or None,
        period_days=period_days,
        provider=provider,
        model=model,
        api_key=api_key.strip() or None,
        jira_url=_normalize_jira_url(jira_url) or None,
        jira_email=jira_email.strip() or None,
        jira_token=jira_token.strip() or None,
        jira_refresh_token=jira_refresh_token.strip() or None,
        linear_token=linear_token.strip() or None,
        slack_token=slack_token.strip() or None,
        slack_channels=[c.strip().lstrip("#") for c in slack_channels.split(",") if c.strip()] or None,
        extra_context=extra_context,
    ))

    return templates.TemplateResponse(request, "analyzing.html", context={
        "job_id": job_id,
        "repo_url": repo_url.strip().rstrip("/"),
    })


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    """SSE stream — sends progress events to the analyzing page."""
    async def event_stream():
        last_sent = 0
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            messages = job["progress"]
            for msg in messages[last_sent:]:
                yield f"data: {json.dumps(msg)}\n\n"
                last_sent = len(messages)

            if job["status"] == "done":
                yield f"data: {json.dumps({'done': True, 'job_id': job_id})}\n\n"
                break
            if job["status"] == "error":
                yield f"data: {json.dumps({'error': job.get('error', 'Unknown error')})}\n\n"
                break

            await asyncio.sleep(0.4)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/prefetch")
async def api_prefetch(request: Request):
    """
    Start a background prefetch of repo data for estimate purposes.
    Body: {repo_url, github_token?, period_days?, has_pdf?, pdf_chars?,
           has_jira?, has_linear?, has_slack?}
    Returns: {prefetch_id}
    """
    try:
        body = await request.json()
    except Exception:
        return {"error": "invalid json"}

    repo_url    = (body.get("repo_url") or "").strip().rstrip("/")
    github_token = (body.get("github_token") or "").strip() or None
    period_days = int(body.get("period_days") or 30)
    has_pdf     = bool(body.get("has_pdf"))
    pdf_chars   = int(body.get("pdf_chars") or 0)   # rough size of uploaded spec text
    has_jira    = bool(body.get("has_jira"))
    has_linear  = bool(body.get("has_linear"))
    has_slack   = bool(body.get("has_slack"))

    if not repo_url or "github.com" not in repo_url:
        return {"error": "only github repos supported for prefetch"}

    prefetch_id = str(uuid.uuid4())
    _prefetch_cache[prefetch_id] = {"status": "pending"}

    async def _do_prefetch():
        try:
            CHARS_PER_TOKEN = 3.5

            # Fetch repo data (issues + PRs) — always needed to detect mode
            repo_data = await _gh_fetch_repo_data(
                repo_url, github_token, period_days,
                lambda msg, **kw: None,  # silent log
                None,
            )

            pairs = repo_data.get("pairs") or []
            n_pairs = len(pairs)
            has_any_issues = bool(n_pairs) or bool(repo_data.get("unlinked_issues"))

            # ── Determine analysis mode ───────────────────────────────────────
            # Mirrors the logic in run_analysis so the estimate matches reality.

            if has_pdf and not has_any_issues and not has_jira and not has_linear:
                # ── Spec-scan mode ────────────────────────────────────────────
                # PDF is split into ~5000-char chunks; one LLM call per chunk.
                # Evidence per call: ~20k chars files + ~8k chars commits.
                CHUNK_SIZE = 5000
                n_chunks = max(1, (pdf_chars or 30_000) // CHUNK_SIZE)

                evidence_chars = 20_000 + 8_000   # files + commits budget
                system_chars   = 2_000             # SPEC_SCAN_SYSTEM
                per_call_input = int((system_chars + CHUNK_SIZE + evidence_chars) / CHARS_PER_TOKEN)
                per_call_output = 600              # ~8 findings × ~75 tokens

                total_input  = per_call_input  * n_chunks
                total_output = per_call_output * n_chunks
                time_est = int(total_input / 800 + total_output / 200) + 20  # +20s file fetch

                mode_label = "spec-scan"
                n_units = n_chunks  # "sections" in this mode

            elif not has_any_issues and (has_jira or has_linear):
                # ── Jira-first / Linear-first mode ───────────────────────────
                # We don't fetch tickets here to keep prefetch fast, so assume
                # a rough estimate of ~8 pairs from matched tickets.
                n_units = max(n_pairs, 8)

                system_chars  = 1_500
                spec_chars    = 2_000   # ticket title + description
                code_chars    = 3_000   # PR diff
                extra_per_pair = 500 if has_slack else 0   # Slack thread
                pdf_extra = int(min(pdf_chars, 12_000) / CHARS_PER_TOKEN) if has_pdf else 0

                per_call_input = int((system_chars + spec_chars + code_chars + extra_per_pair) / CHARS_PER_TOKEN) + pdf_extra
                per_call_output = 500

                total_input  = per_call_input  * n_units
                total_output = per_call_output * n_units
                time_est = int(total_input / 800 + total_output / 200) + 10

                mode_label = "jira-first" if has_jira else "linear-first"

            else:
                # ── Normal drift mode (issue-PR pairs) ───────────────────────
                if not n_pairs and not has_jira and not has_linear:
                    # Will fall back to PR-description mode
                    n_units = max(10, n_pairs)
                else:
                    n_units = max(n_pairs, 1)

                system_chars  = 1_500
                spec_chars    = 2_000
                code_chars    = 3_000
                extra_per_pair = 500 if has_slack else 0
                pdf_extra = int(min(pdf_chars, 12_000) / CHARS_PER_TOKEN) if has_pdf else 0

                if pairs:
                    sample  = pairs[0]
                    issue   = sample.get("issue", {})
                    pr_list = sample.get("prs", [])
                    spec_chars = len((issue.get("title", "") + "\n" + (issue.get("body") or ""))[:2000])
                    code_chars = sum(len(pr.get("diff", "")[:1500]) for pr in pr_list[:2])

                per_call_input  = int((system_chars + spec_chars + code_chars + extra_per_pair) / CHARS_PER_TOKEN) + pdf_extra
                per_call_output = 450

                total_input  = per_call_input  * n_units
                total_output = per_call_output * n_units
                time_est = int(total_input / 800 + total_output / 200) + 5 + n_units // 2

                mode_label = "drift"

            total_tokens = total_input + total_output

            result = {
                "status": "done",
                "repo_data": repo_data,
                "estimate": {
                    "mode":          mode_label,
                    "pairs":         n_units,
                    "tokens_input":  total_input,
                    "tokens_output": total_output,
                    "tokens_total":  total_tokens,
                    "time_seconds":  time_est,
                },
            }
            _prefetch_cache[prefetch_id] = result
            _prefetch_cache[f"url:{repo_url}"] = result
        except Exception as e:
            err = {"status": "error", "error": str(e)}
            _prefetch_cache[prefetch_id] = err
            _prefetch_cache[f"url:{repo_url}"] = err

    asyncio.create_task(_do_prefetch())
    return {"prefetch_id": prefetch_id}


@app.get("/api/prefetch/{prefetch_id}")
async def api_prefetch_status(prefetch_id: str):
    """Poll prefetch status. Returns {status, estimate?}"""
    entry = _prefetch_cache.get(prefetch_id)
    if not entry:
        return {"status": "not_found"}
    if entry["status"] == "done":
        return {"status": "done", "estimate": entry["estimate"]}
    if entry["status"] == "error":
        return {"status": "error", "error": entry.get("error", "")}
    return {"status": "pending"}


@app.get("/report/latest", response_class=HTMLResponse)
async def report_latest():
    # Try in-memory first, then fall back to most recent on disk
    completed = [j for j in jobs.values() if j.get("status") == "done"]
    if not completed:
        disk_files = sorted(_JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not disk_files:
            return HTMLResponse("<h2>No completed report yet.</h2>", status_code=404)
        job = _load_job(disk_files[-1].stem)
        if not job:
            return HTMLResponse("<h2>No completed report yet.</h2>", status_code=404)
    else:
        job = completed[-1]
    try:
        import importlib, scanner.report as _r
        importlib.reload(_r)
        return HTMLResponse(_r.generate_report(job["repo_data"], job["analysis"]))
    except Exception as e:
        return HTMLResponse(f"<h2>Error generating report: {e}</h2>", status_code=500)


@app.get("/report/{job_id}", response_class=HTMLResponse)
async def report(request: Request, job_id: str):
    job = jobs.get(job_id) or _load_job(job_id)
    if not job:
        return HTMLResponse("<h2>Report not ready yet.</h2>", status_code=404)
    try:
        import importlib, scanner.report as _r
        importlib.reload(_r)
        return HTMLResponse(_r.generate_report(job["repo_data"], job["analysis"]))
    except Exception as e:
        if job.get("report"):
            return HTMLResponse(job["report"])
        return HTMLResponse(f"<h2>Error generating report: {e}</h2>", status_code=500)


@app.post("/api/slack/channels")
async def api_slack_channels(request: Request):
    """
    List Slack channels matching a query.
    Expects JSON body: {token: str, query: str}
    Returns [{id, name, is_private, num_members}]
    """
    try:
        body = await request.json()
    except Exception:
        return []
    token = (body.get("token") or "").strip()
    query = (body.get("query") or "").strip().lower().lstrip("#")
    if not token:
        return []

    async with httpx.AsyncClient(timeout=10) as client:
        params: dict = {
            "exclude_archived": "true",
            "limit": "200",
            "types": "public_channel,private_channel",
        }
        if query:
            # Slack doesn't support server-side name filtering for conversations.list,
            # so we fetch and filter client-side. Limit to 1000 to stay fast.
            params["limit"] = "1000"

        resp = await client.get(
            "https://slack.com/api/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        data = resp.json()

    if not data.get("ok"):
        error = data.get("error", "unknown")
        if error in ("missing_scope", "not_allowed_token_type"):
            return {"error": "missing_scope"}
        if error in ("invalid_auth", "token_revoked", "account_inactive", "not_authed"):
            return {"error": "invalid_auth"}
        return {"error": error}

    channels = data.get("channels") or []
    results = []
    for ch in channels:
        name = ch.get("name", "")
        if query and query not in name.lower():
            continue
        results.append({
            "id":          ch.get("id", ""),
            "name":        name,
            "is_private":  ch.get("is_private", False),
            "num_members": ch.get("num_members", 0),
        })

    # Sort: exact prefix match first, then by member count desc
    results.sort(key=lambda c: (not c["name"].startswith(query), -c["num_members"]))
    return results[:40]


@app.get("/debug/{job_id}")
async def debug_job(job_id: str):
    """Return raw progress log for a job — useful for diagnosing 0-pair results."""
    job = jobs.get(job_id)
    if not job:
        return {"error": "job not found"}
    return {"status": job["status"], "progress": job["progress"]}


# ── Jira token refresh ────────────────────────────────────────────────────────

def _refresh_jira_token(refresh_token: str) -> str | None:
    """
    Use an Atlassian refresh token to get a new access token.
    Returns the new access token, or None on failure.
    Only works when JIRA_CLIENT_ID / JIRA_CLIENT_SECRET are configured.
    """
    cfg = _OAUTH_CONFIGS.get("jira", {})
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")
    if not client_id or not client_secret or not refresh_token:
        return None
    import requests as _requests
    try:
        resp = _requests.post(
            "https://auth.atlassian.com/oauth/token",
            json={
                "grant_type":    "refresh_token",
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        data = resp.json()
        return data.get("access_token") or None
    except Exception:
        return None


# ── Jira-first pair builder ───────────────────────────────────────────────────

async def build_jira_first_pairs(
    repo_data: dict,
    jira_url: str,
    jira_email: str,
    jira_token: str,
    github_token: str | None,
    log,
    max_pr_changes: int | None,
    jira_refresh_token: str | None = None,
) -> list[dict]:
    """
    When a repo has no GitHub Issues, use Jira tickets as the spec source.
    Scans closed PR titles/bodies for Jira ticket IDs (including ranges like QP-23..34),
    fetches those tickets, and builds pairs in the standard format.
    """
    owner, repo = repo_data["owner"], repo_data["repo"]
    base = f"https://api.github.com/repos/{owner}/{repo}"
    loop = asyncio.get_event_loop()

    # 0. Validate Jira credentials — auto-refresh if token expired (401)
    ok, conn_msg = await loop.run_in_executor(
        None, lambda: test_jira_connection(jira_url, jira_email, jira_token)
    )
    if not ok and "401" in conn_msg and jira_refresh_token:
        log("Jira token expired — refreshing...")
        new_token = await loop.run_in_executor(
            None, lambda: _refresh_jira_token(jira_refresh_token)
        )
        if new_token:
            jira_token = new_token
            ok, conn_msg = await loop.run_in_executor(
                None, lambda: test_jira_connection(jira_url, jira_email, jira_token)
            )
            if ok:
                log("Jira token refreshed successfully")
    if not ok:
        log(f"⚠ Jira connection failed: {conn_msg}")
        return []
    log(f"Jira: {conn_msg}")

    # 1. Fetch all closed PRs
    log("Jira-first mode: fetching closed PRs to match against Jira tickets...")
    prs_raw = await loop.run_in_executor(
        None, lambda: _get_all_pages(f"{base}/pulls", github_token, {"state": "closed"}, max_pages=5)
    )
    log(f"Found {len(prs_raw)} closed PRs")

    # 2. Map Jira IDs → PR numbers (handles ranges like QP-23..34)
    jira_to_pr_numbers: dict[str, list[int]] = {}
    for pr in prs_raw:
        text = pr.get("title", "") + " " + (pr.get("body") or "")
        for jid in expand_jira_ids(text):
            jira_to_pr_numbers.setdefault(jid, []).append(pr["number"])

    if not jira_to_pr_numbers:
        log("No Jira IDs found in PR titles or descriptions")
        return []

    log(f"Matched {len(jira_to_pr_numbers)} Jira ticket(s): "
        f"{', '.join(list(jira_to_pr_numbers)[:8])}")

    # Fetch Jira tickets concurrently
    sem = asyncio.Semaphore(6)
    _auth_error: list[str] = []

    async def _fetch_ticket(jid: str) -> tuple[str, dict | None]:
        async with sem:
            try:
                result = await loop.run_in_executor(
                    None, lambda: fetch_jira_ticket(jid, jira_url, jira_email, jira_token)
                )
                return jid, result
            except RuntimeError as e:
                if not _auth_error:
                    _auth_error.append(str(e))
                return jid, None

    ticket_results = await asyncio.gather(*[_fetch_ticket(jid) for jid in jira_to_pr_numbers])
    ticket_map = {jid: t for jid, t in ticket_results if t}

    if _auth_error:
        log(f"⚠ Jira error: {_auth_error[0]}")
        log("Connect Jira with a valid token to enable Jira-first analysis.")
        return []
    elif len(ticket_map) == 0:
        log(f"⚠ Fetched 0/{len(jira_to_pr_numbers)} Jira tickets.")
        log(f"  Check your Jira URL: {jira_url[:80]}")
        log("  Make sure your API token has read access to the project (e.g. QP).")
        return []
    else:
        log(f"Fetched {len(ticket_map)}/{len(jira_to_pr_numbers)} Jira tickets successfully")

    # Fetch PR diffs for matched PRs only
    matched_pr_numbers = sorted({n for nums in jira_to_pr_numbers.values() for n in nums})
    log(f"Fetching diffs for {len(matched_pr_numbers)} matched PR(s)...")
    sem2 = asyncio.Semaphore(5)

    async def _fetch_pr(pr_number: int) -> dict | None:
        async with sem2:
            return await loop.run_in_executor(
                None, lambda: _fetch_pr_details(pr_number, github_token, base, None, max_pr_changes)
            )

    pr_details = await asyncio.gather(*[_fetch_pr(n) for n in matched_pr_numbers])
    pr_detail_map = {
        pr["number"]: pr for pr in pr_details
        if pr and not pr.get("oversized")
    }
    log(f"Got diffs for {len(pr_detail_map)} PRs")

    # 5. Build pairs: one pair per Jira ticket
    pairs = []
    for idx, (jid, ticket) in enumerate(ticket_map.items(), start=1):
        pr_numbers = jira_to_pr_numbers[jid]
        prs = [pr_detail_map[n] for n in pr_numbers if n in pr_detail_map]
        if not prs:
            continue

        for pr in prs:
            if pr.get("changes_requested", 0) >= 1:
                pr["rework_hours"] = _estimate_rework_hours(pr.get("commits", []))
            else:
                pr["rework_hours"] = 0.0

        # Build a synthetic "issue" from the Jira ticket so the analyzer sees
        # a familiar structure (same keys as GitHub issue dicts).
        body_parts = []
        if ticket.get("description"):
            body_parts.append(ticket["description"])
        if ticket.get("acceptance_criteria"):
            body_parts.append(f"Acceptance Criteria:\n{ticket['acceptance_criteria']}")
        if ticket.get("comments"):
            body_parts.append("Team discussion:\n" + "\n".join(f"- {c}" for c in ticket["comments"][:5]))

        pairs.append({
            "issue": {
                "number": idx,
                "title": ticket["summary"],
                "url": ticket["url"],
                "body": "\n\n".join(body_parts)[:2000],
                "labels": ticket.get("labels") or [],
                "comments": len(ticket.get("comments") or []),
                "closed_at": None,
                "project": "",
                "project_fields": {},
            },
            "prs": prs,
            "jira_tickets": [ticket],
        })

    log(f"Built {len(pairs)} Jira-first pair(s)")
    return pairs


# ── Linear-first pair builder ─────────────────────────────────────────────────

async def build_linear_first_pairs(
    repo_data: dict,
    linear_token: str,
    github_token: str | None,
    log,
    max_pr_changes: int | None,
) -> list[dict]:
    """
    When a repo has no GitHub Issues, use Linear tickets as the spec source.
    Scans closed PR titles/bodies for Linear issue IDs (e.g. ENG-123),
    fetches those tickets with comments + parent context, and builds pairs.
    """
    owner, repo = repo_data["owner"], repo_data["repo"]
    base = f"https://api.github.com/repos/{owner}/{repo}"
    loop = asyncio.get_event_loop()

    log("Linear-first mode: fetching closed PRs to match against Linear issues...")
    prs_raw = await loop.run_in_executor(
        None, lambda: _get_all_pages(f"{base}/pulls", github_token, {"state": "closed"}, max_pages=5)
    )
    log(f"Found {len(prs_raw)} closed PRs")

    # Map Linear IDs → PR numbers
    linear_to_pr_numbers: dict[str, list[int]] = {}
    for pr in prs_raw:
        text = pr.get("title", "") + " " + (pr.get("body") or "")
        for lid in extract_linear_ids(text):
            linear_to_pr_numbers.setdefault(lid, []).append(pr["number"])

    if not linear_to_pr_numbers:
        log("No Linear IDs found in PR titles or descriptions")
        return []

    log(f"Matched {len(linear_to_pr_numbers)} Linear issue(s): "
        f"{', '.join(list(linear_to_pr_numbers)[:8])}")

    # Fetch Linear issues concurrently (with comments + parent)
    sem = asyncio.Semaphore(6)

    async def _fetch_issue(lid: str) -> tuple[str, dict | None]:
        async with sem:
            return lid, await loop.run_in_executor(
                None, lambda: fetch_linear_issue(lid, linear_token)
            )

    issue_results = await asyncio.gather(*[_fetch_issue(lid) for lid in linear_to_pr_numbers])
    issue_map = {lid: t for lid, t in issue_results if t}
    log(f"Fetched {len(issue_map)}/{len(linear_to_pr_numbers)} Linear issues")

    # Fetch PR diffs for matched PRs only
    matched_pr_numbers = sorted({n for nums in linear_to_pr_numbers.values() for n in nums})
    log(f"Fetching diffs for {len(matched_pr_numbers)} matched PR(s)...")
    sem2 = asyncio.Semaphore(5)

    async def _fetch_pr(pr_number: int) -> dict | None:
        async with sem2:
            return await loop.run_in_executor(
                None, lambda: _fetch_pr_details(pr_number, github_token, base, None, max_pr_changes)
            )

    pr_details = await asyncio.gather(*[_fetch_pr(n) for n in matched_pr_numbers])
    pr_detail_map = {pr["number"]: pr for pr in pr_details if pr and not pr.get("oversized")}
    log(f"Got diffs for {len(pr_detail_map)} PRs")

    # Build pairs: one pair per Linear issue
    pairs = []
    for idx, (lid, issue) in enumerate(issue_map.items(), start=1):
        pr_numbers = linear_to_pr_numbers[lid]
        prs = [pr_detail_map[n] for n in pr_numbers if n in pr_detail_map]
        if not prs:
            continue

        for pr in prs:
            pr["rework_hours"] = (
                _estimate_rework_hours(pr.get("commits", []))
                if pr.get("changes_requested", 0) >= 1 else 0.0
            )

        # Build rich issue body: description + AC + parent context + team discussion
        body_parts = []
        if issue.get("description"):
            body_parts.append(issue["description"])
        if issue.get("acceptance_criteria"):
            body_parts.append(f"Acceptance Criteria:\n{issue['acceptance_criteria']}")
        if issue.get("parent_context"):
            body_parts.append(f"Parent feature:\n{issue['parent_context']}")
        if issue.get("comments"):
            body_parts.append("Team discussion:\n" + "\n".join(f"- {c}" for c in issue["comments"][:6]))

        pairs.append({
            "issue": {
                "number": idx,
                "title": issue["summary"],
                "url": issue["url"],
                "body": "\n\n".join(body_parts)[:2500],
                "labels": issue.get("labels") or [],
                "comments": len(issue.get("comments") or []),
                "closed_at": None,
                "project": "",
                "project_fields": {},
            },
            "prs": prs,
            "linear_tickets": [issue],
        })

    log(f"Built {len(pairs)} Linear-first pair(s)")
    return pairs


async def build_gitlab_linear_first_pairs(
    repo_data: dict,
    linear_token: str,
    gitlab_token: str | None,
    log,
    max_pr_changes: int | None,
) -> list[dict]:
    """
    Linear-first mode for GitLab: scan merged MR titles/bodies for Linear IDs,
    fetch those tickets, and build issue-MR pairs for drift analysis.
    """
    api_base, owner, repo, project_id = _parse_project_info(repo_data["repo_url"])
    loop = asyncio.get_event_loop()

    log("Linear-first mode: fetching merged MRs to match against Linear issues...")
    mrs_raw = await loop.run_in_executor(
        None,
        lambda: _gl_get_all_pages(
            f"{api_base}/projects/{project_id}/merge_requests",
            gitlab_token,
            {"state": "merged"},
            max_pages=5,
        ),
    )
    log(f"Found {len(mrs_raw)} merged MRs")

    # Map Linear IDs → MR iids
    linear_to_mr_iids: dict[str, list[int]] = {}
    for mr in mrs_raw:
        text = mr.get("title", "") + " " + (mr.get("description") or "")
        for lid in extract_linear_ids(text):
            linear_to_mr_iids.setdefault(lid, []).append(mr["iid"])

    if not linear_to_mr_iids:
        log("No Linear IDs found in MR titles or descriptions")
        return []

    log(f"Matched {len(linear_to_mr_iids)} Linear issue(s): "
        f"{', '.join(list(linear_to_mr_iids)[:8])}")

    # Fetch Linear issues concurrently
    sem = asyncio.Semaphore(6)

    async def _fetch_issue(lid: str) -> tuple[str, dict | None]:
        async with sem:
            return lid, await loop.run_in_executor(
                None, lambda: fetch_linear_issue(lid, linear_token)
            )

    issue_results = await asyncio.gather(*[_fetch_issue(lid) for lid in linear_to_mr_iids])
    issue_map = {lid: t for lid, t in issue_results if t}
    log(f"Fetched {len(issue_map)}/{len(linear_to_mr_iids)} Linear issues")

    # Fetch MR diffs for matched MRs
    matched_mr_iids = sorted({n for nums in linear_to_mr_iids.values() for n in nums})
    log(f"Fetching diffs for {len(matched_mr_iids)} matched MR(s)...")
    sem2 = asyncio.Semaphore(5)

    async def _fetch_mr(mr_iid: int) -> dict | None:
        async with sem2:
            return await loop.run_in_executor(
                None,
                lambda: _fetch_mr_details(mr_iid, gitlab_token, api_base, project_id, max_pr_changes),
            )

    mr_details = await asyncio.gather(*[_fetch_mr(iid) for iid in matched_mr_iids])
    mr_detail_map = {mr["number"]: mr for mr in mr_details if mr and not mr.get("oversized")}
    log(f"Got diffs for {len(mr_detail_map)} MRs")

    # Build pairs: one pair per Linear issue
    pairs = []
    for idx, (lid, issue) in enumerate(issue_map.items(), start=1):
        mr_iids = linear_to_mr_iids[lid]
        prs = [mr_detail_map[iid] for iid in mr_iids if iid in mr_detail_map]
        if not prs:
            continue

        for pr in prs:
            pr["rework_hours"] = (
                _gl_estimate_rework_hours(pr.get("commits", []))
                if pr.get("changes_requested", 0) >= 1 else 0.0
            )

        body_parts = []
        if issue.get("description"):
            body_parts.append(issue["description"])
        if issue.get("acceptance_criteria"):
            body_parts.append(f"Acceptance Criteria:\n{issue['acceptance_criteria']}")
        if issue.get("parent_context"):
            body_parts.append(f"Parent feature:\n{issue['parent_context']}")
        if issue.get("comments"):
            body_parts.append("Team discussion:\n" + "\n".join(f"- {c}" for c in issue["comments"][:6]))

        pairs.append({
            "issue": {
                "number": idx,
                "title": issue["summary"],
                "url": issue["url"],
                "body": "\n\n".join(body_parts)[:2500],
                "labels": issue.get("labels") or [],
                "comments": len(issue.get("comments") or []),
                "closed_at": None,
                "project": "",
                "project_fields": {},
            },
            "prs": prs,
            "linear_tickets": [issue],
        })

    log(f"Built {len(pairs)} Linear-first pair(s)")
    return pairs


# ── PR-description-first mode ─────────────────────────────────────────────────
# Fallback for repos that don't use GitHub Issues / GitLab Issues at all.
# Uses each merged PR's title + description as the spec, and its diff as the
# implementation.  Works well for teams that write requirements in PR bodies.

async def build_pr_first_pairs(
    repo_data:      dict,
    github_token:   str | None,
    log,
    max_pr_changes: int | None,
) -> list[dict]:
    """
    PR-description-first mode for GitHub.
    Fetches merged PRs from the analysis window and treats each PR description
    as the specification, with the diff as the implementation under review.
    """
    from datetime import datetime, timedelta, timezone

    owner  = repo_data["owner"]
    repo   = repo_data["repo"]
    base   = f"https://api.github.com/repos/{owner}/{repo}"
    period = repo_data.get("period_days", 30)
    loop   = asyncio.get_event_loop()

    log("No GitHub Issues found — switching to PR-description mode…")
    log("Using PR descriptions as specifications (tip: link PRs to issues with 'closes #N' for richer analysis)")

    prs_raw = await loop.run_in_executor(
        None,
        lambda: _get_all_pages(
            f"{base}/pulls", github_token, {"state": "closed"}, max_pages=5
        ),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=period)
    merged = [
        pr for pr in prs_raw
        if pr.get("merged_at")
        and datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00")) >= cutoff
    ]
    log(f"Found {len(merged)} merged PRs in the last {period} days")

    if not merged:
        return []

    # Filter to PRs with a meaningful description (the "spec")
    with_body = [
        pr for pr in merged
        if len((pr.get("body") or "").strip()) >= 40
        and not any(
            bot in (pr.get("user") or {}).get("login", "").lower()
            for bot in ("dependabot", "renovate", "greenkeeper", "snyk-bot")
        )
    ]
    if not with_body:
        log("⚠ Merged PRs found but none have descriptions long enough to serve as specs.")
        log("  Write PR descriptions explaining what the PR is supposed to do to enable analysis.")
        return []

    log(f"{len(with_body)} PRs have usable descriptions — fetching diffs…")

    sem = asyncio.Semaphore(5)

    async def _fetch(pr_raw: dict) -> dict | None:
        async with sem:
            return await loop.run_in_executor(
                None,
                lambda: _fetch_pr_details(
                    pr_raw["number"], github_token, base,
                    pr_raw.get("merged_at"), max_pr_changes,
                    issue_title=pr_raw.get("title", ""),
                    issue_body=(pr_raw.get("body") or "")[:400],
                ),
            )

    results = await asyncio.gather(*[_fetch(pr) for pr in with_body])

    pairs: list[dict] = []
    for pr in results:
        if not pr or pr.get("oversized"):
            continue
        pr_body = (pr.get("body") or "").strip()
        if not pr_body:
            continue
        pr["rework_hours"] = (
            _estimate_rework_hours(pr.get("commits", []))
            if pr.get("changes_requested", 0) >= 1 else 0.0
        )
        pairs.append({
            "issue": {
                "number":       pr["number"],
                "title":        pr["title"],
                "url":          pr["url"],
                "body":         pr_body[:2000],
                "labels":       [],
                "comments":     len(pr.get("reviews") or []),
                "closed_at":    pr.get("merged_at"),
                "author":       pr.get("author", ""),
                "author_url":   "",
                "project":      "",
                "project_fields": {},
                "_pr_description_mode": True,
            },
            "prs": [pr],
        })

    log(f"PR-description mode: built {len(pairs)} analyzable pair(s)")
    return pairs


async def build_gitlab_pr_first_pairs(
    repo_data:      dict,
    gitlab_token:   str | None,
    log,
    max_pr_changes: int | None,
) -> list[dict]:
    """
    PR-description-first mode for GitLab.
    Same concept as build_pr_first_pairs but uses GitLab MRs.
    """
    from datetime import datetime, timedelta, timezone

    api_base, owner, repo, project_id = _parse_project_info(repo_data["repo_url"])
    period = repo_data.get("period_days", 30)
    loop   = asyncio.get_event_loop()

    log("No GitLab Issues found — switching to MR-description mode…")

    mrs_raw = await loop.run_in_executor(
        None,
        lambda: _gl_get_all_pages(
            f"{api_base}/projects/{project_id}/merge_requests",
            gitlab_token, {"state": "merged"}, max_pages=5,
        ),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=period)
    merged = [
        mr for mr in mrs_raw
        if mr.get("merged_at")
        and datetime.fromisoformat(mr["merged_at"].replace("Z", "+00:00")) >= cutoff
    ]
    log(f"Found {len(merged)} merged MRs in the last {period} days")
    if not merged:
        return []

    with_body = [
        mr for mr in merged
        if len((mr.get("description") or "").strip()) >= 40
        and not any(
            bot in (mr.get("author") or {}).get("username", "").lower()
            for bot in ("dependabot", "renovate", "bot")
        )
    ]
    if not with_body:
        log("⚠ Merged MRs found but none have descriptions long enough to serve as specs.")
        return []

    log(f"{len(with_body)} MRs have usable descriptions — fetching diffs…")
    sem = asyncio.Semaphore(5)

    async def _fetch_gl(mr_raw: dict) -> dict | None:
        async with sem:
            return await loop.run_in_executor(
                None,
                lambda: _fetch_mr_details(
                    mr_raw["iid"], gitlab_token, api_base, project_id,
                    max_pr_changes,
                    issue_title=mr_raw.get("title", ""),
                    issue_body=(mr_raw.get("description") or "")[:400],
                ),
            )

    results = await asyncio.gather(*[_fetch_gl(mr) for mr in with_body])

    pairs: list[dict] = []
    for mr in results:
        if not mr or mr.get("oversized"):
            continue
        mr_body = (mr.get("body") or "").strip()
        if not mr_body:
            continue
        mr["rework_hours"] = (
            _gl_estimate_rework_hours(mr.get("commits", []))
            if mr.get("changes_requested", 0) >= 1 else 0.0
        )
        pairs.append({
            "issue": {
                "number":       mr["number"],
                "title":        mr["title"],
                "url":          mr["url"],
                "body":         mr_body[:2000],
                "labels":       [],
                "comments":     len(mr.get("reviews") or []),
                "closed_at":    mr.get("merged_at"),
                "author":       mr.get("author", ""),
                "author_url":   "",
                "project":      "",
                "project_fields": {},
                "_pr_description_mode": True,
            },
            "prs": [mr],
        })

    log(f"MR-description mode: built {len(pairs)} analyzable pair(s)")
    return pairs


# ── Analysis runner ───────────────────────────────────────────────────────────

async def run_analysis(
    job_id: str,
    repo_url: str,
    provider: str,
    model: str,
    platform: str = "github",
    github_token: str | None = None,
    gitlab_token: str | None = None,
    period_days: int = 30,
    api_key: str | None = None,
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_token: str | None = None,
    jira_refresh_token: str | None = None,
    linear_token: str | None = None,
    slack_token: str | None = None,
    slack_channels: list[str] | None = None,
    extra_context: str | None = None,
):
    job = jobs[job_id]

    def log(msg: str, step: int = None, tokens: int = None, estimated: bool = False):
        entry = {"msg": msg, "step": step}
        if tokens is not None:
            entry["tokens"] = tokens
            entry["estimated"] = estimated
            job["tokens"] = {"total": tokens, "estimated": estimated}
        job["progress"].append(entry)

    try:
        max_pr_changes = None if provider in ("claude", "claude-code", "openai") else 2000

        if platform == "gitlab":
            repo_data = await _gl_fetch_repo_data(repo_url, gitlab_token, period_days, log, max_pr_changes)
        else:
            # Reuse prefetch cache if available (avoids double-fetching)
            _cached = _prefetch_cache.get(f"url:{repo_url}")
            if _cached and _cached.get("status") == "done" and _cached.get("repo_data"):
                log("Reusing prefetched repo data...")
                repo_data = _cached["repo_data"]
                # Clear cache entry after use to avoid stale data
                _prefetch_cache.pop(f"url:{repo_url}", None)
            else:
                repo_data = await _gh_fetch_repo_data(repo_url, github_token, period_days, log, max_pr_changes)

        has_pairs = bool(repo_data["pairs"])

        # Spec-scan mode: if a spec doc was uploaded and there are no GitHub issues at all
        # (not even unlinked ones), skip normal analysis and compare spec vs. commits directly.
        has_any_issues = bool(repo_data["pairs"]) or bool(repo_data.get("unlinked_issues"))
        if extra_context and platform == "github" and not has_any_issues and not jira_url and not linear_token:
            log("No GitHub issues found — switching to spec-scan mode (spec doc vs. codebase)...", step=2)
            commits, repo_files = await asyncio.gather(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: fetch_commits_with_diffs(repo_data["owner"], repo_data["repo"], github_token, max_commits=60, log=log),
                ),
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: fetch_repo_files(repo_data["owner"], repo_data["repo"], github_token, max_files=40, log=log),
                ),
            )
            if not commits and not repo_files:
                log("⚠ No commits or files found. Nothing to analyse.")
            else:
                log(f"Found {len(commits)} commits + {len(repo_files)} source files. Running spec-scan analysis...", step=2)
                analysis = await analyze_spec(repo_data, commits, provider, model, api_key, log, extra_context, repo_files=repo_files)
                log("Generating report...", step=3)
                report_html = generate_report(repo_data, analysis)
                job["report"] = report_html
                job["repo_data"] = repo_data
                job["analysis"] = analysis
                job["status"] = "done"
                _save_job(job_id, job)
                return

        if not has_pairs and not jira_url and not linear_token:
            # No issue tracker — fall back to PR-description mode
            pairs = await (
                build_pr_first_pairs(repo_data, github_token, log, max_pr_changes)
                if platform == "github"
                else build_gitlab_pr_first_pairs(repo_data, gitlab_token, log, max_pr_changes)
            )
            repo_data["pairs"] = pairs
            repo_data["stats"]["pairs_built"] = len(pairs)
            has_pairs = bool(pairs)
            if not has_pairs:
                log("⚠ No analysable pairs found.")
                log("  Upload a spec document, connect Jira or Linear, or write PR descriptions explaining intended changes.")

        # Debug: log what Jira credentials were received
        if jira_url or jira_token:
            _ju = repr(jira_url)[:60]; _tl = len(jira_token or ""); _em = repr(jira_email)[:30]
            log(f"[debug] jira_url={_ju} token_len={_tl} email={_em}")

        if jira_url and jira_token:  # jira_email optional: empty → OAuth Bearer auth
            if has_pairs:
                log("Enriching with Jira ticket data...")
                repo_data["pairs"] = await enrich_pairs_with_jira(
                    repo_data["pairs"], jira_url, jira_email, jira_token, log
                )
            elif platform == "github":
                # Jira-first mode: GitHub only (uses GitHub PR API internally)
                log("No issues found — activating Jira-first mode...", step=1)
                pairs = await build_jira_first_pairs(
                    repo_data, jira_url, jira_email, jira_token,
                    github_token, log, max_pr_changes,
                    jira_refresh_token=jira_refresh_token,
                )
                repo_data["pairs"] = pairs
                repo_data["stats"]["pairs_built"] = len(pairs)
                # Jira auth failed or returned nothing → fall back to PR-description mode
                if not pairs:
                    log("Jira returned no pairs — falling back to PR-description mode…")
                    pairs = await build_pr_first_pairs(repo_data, github_token, log, max_pr_changes)
                    repo_data["pairs"] = pairs
                    repo_data["stats"]["pairs_built"] = len(pairs)

        elif linear_token:
            if has_pairs:
                log("Enriching with Linear issue data...")
                repo_data["pairs"] = await enrich_pairs_with_linear(
                    repo_data["pairs"], linear_token, log
                )
            elif platform == "gitlab":
                log("No issues found — activating Linear-first mode (GitLab)...", step=1)
                pairs = await build_gitlab_linear_first_pairs(
                    repo_data, linear_token, gitlab_token, log, max_pr_changes
                )
                repo_data["pairs"] = pairs
                repo_data["stats"]["pairs_built"] = len(pairs)
                if not pairs:
                    log("Linear returned no pairs — falling back to MR-description mode…")
                    pairs = await build_gitlab_pr_first_pairs(repo_data, gitlab_token, log, max_pr_changes)
                    repo_data["pairs"] = pairs
                    repo_data["stats"]["pairs_built"] = len(pairs)
            else:
                # Linear-first mode: GitHub
                log("No issues found — activating Linear-first mode...", step=1)
                pairs = await build_linear_first_pairs(
                    repo_data, linear_token, github_token, log, max_pr_changes
                )
                repo_data["pairs"] = pairs
                repo_data["stats"]["pairs_built"] = len(pairs)
                if not pairs:
                    log("Linear returned no pairs — falling back to PR-description mode…")
                    pairs = await build_pr_first_pairs(repo_data, github_token, log, max_pr_changes)
                    repo_data["pairs"] = pairs
                    repo_data["stats"]["pairs_built"] = len(pairs)

        if slack_token and repo_data["pairs"]:
            log("Searching Slack for PR and ticket discussions...")
            owner, repo = repo_data["owner"], repo_data["repo"]
            repo_data["pairs"] = await enrich_pairs_with_slack(
                repo_data["pairs"], owner, repo, slack_token, log,
                channels=slack_channels,
            )

        # ── Knowledge-graph pipeline ──────────────────────────────────────────
        # Runs after all integrations have enriched the pairs so the graph
        # includes Jira/Linear/Slack context.  Returns None on any failure so
        # the existing analysis path is never blocked.
        if _KG_AVAILABLE and repo_data.get("pairs"):
            await _run_kg_pipeline(
                repo_data,
                platform=platform,
                extra_context=extra_context,
                log=log,
            )

        log("Running drift analysis...", step=2)
        analysis = await analyze_drift(repo_data, provider, model, api_key, log, extra_context=extra_context)

        log("Generating report...", step=3)
        report_html = generate_report(repo_data, analysis)

        job["report"] = report_html
        job["repo_data"] = repo_data
        job["analysis"] = analysis
        job["status"] = "done"
        _save_job(job_id, job)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ── OAuth routes (auto-mode) ──────────────────────────────────────────────────

_VALID_SERVICES = {"github", "gitlab", "linear", "slack", "jira"}


@app.get("/connect/{service}")
async def oauth_connect(service: str, request: Request):
    """
    Step 1 of the OAuth popup flow.
    - If env vars for this service are configured: start OAuth directly.
    - Otherwise: delegate to the 1spur relay (which holds the secrets).
    """
    if service not in _VALID_SERVICES:
        return HTMLResponse("Unknown service", status_code=400)

    if _oauth_configured(service):
        cfg = _OAUTH_CONFIGS[service]
        state = _secrets.token_urlsafe(32)
        request.session[f"oauth_state_{service}"] = state

        redirect_uri = f"{_BASE_URL}/oauth-callback/{service}"
        params: dict[str, str] = {
            "client_id":    cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state":        state,
        }
        if "scope" in cfg:
            params["scope"] = cfg["scope"]
        if "user_scope" in cfg:               # Slack user-token scopes
            params["user_scope"] = cfg["user_scope"]
        if "extra_auth_params" in cfg:        # Jira: audience + prompt
            params.update(cfg["extra_auth_params"])

        qs = "&".join(f"{k}={_urlquote(str(v), safe='')}" for k, v in params.items())
        return RedirectResponse(f"{cfg['auth_url']}?{qs}")

    # Fallback: 1spur relay holds the secrets
    callback = _urlquote(f"{_BASE_URL}/callback/{service}", safe="")
    return RedirectResponse(f"{_RELAY_URL}/auth/{service}?callback={callback}")


@app.get("/oauth-callback/{service}")
async def oauth_provider_callback(
    service: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """
    Step 2: the OAuth provider redirects back here with an auth code.
    Exchange the code for an access token, then hand off to /callback/{service}.
    """
    if service not in _VALID_SERVICES:
        return HTMLResponse("Unknown service", status_code=400)

    if error:
        return RedirectResponse(f"/callback/{service}?error={_urlquote(error, safe='')}")

    # CSRF state check
    stored = request.session.pop(f"oauth_state_{service}", None)
    if not stored or stored != state:
        return RedirectResponse(f"/callback/{service}?error=invalid_state")

    cfg = _OAUTH_CONFIGS[service]
    redirect_uri = f"{_BASE_URL}/oauth-callback/{service}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # ── GitHub ──────────────────────────────────────────────────
            if service == "github":
                resp = await client.post(
                    cfg["token_url"],
                    data={
                        "client_id":     cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                        "code":          code,
                        "redirect_uri":  redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                token = resp.json().get("access_token", "")

            # ── GitLab ──────────────────────────────────────────────────
            elif service == "gitlab":
                resp = await client.post(
                    cfg["token_url"],
                    data={
                        "client_id":     cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                        "code":          code,
                        "grant_type":    "authorization_code",
                        "redirect_uri":  redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                token = resp.json().get("access_token", "")

            # ── Jira (Atlassian) ─────────────────────────────────────────
            elif service == "jira":
                resp = await client.post(
                    cfg["token_url"],
                    json={
                        "grant_type":    "authorization_code",
                        "client_id":     cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                        "code":          code,
                        "redirect_uri":  redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                jira_resp_data = resp.json()
                token = jira_resp_data.get("access_token", "")
                refresh_token = jira_resp_data.get("refresh_token", "")
                if not token:
                    err = jira_resp_data.get("error_description", "no_token")
                    return RedirectResponse(f"/callback/{service}?error={_urlquote(err, safe='')}")

                # Fetch Jira cloud ID — required for all Jira REST API calls
                resources_resp = await client.get(
                    "https://api.atlassian.com/oauth/token/accessible-resources",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
                resources = resources_resp.json()
                if not resources:
                    return RedirectResponse(f"/callback/{service}?error=no_jira_site")
                cloud_id  = resources[0]["id"]
                cloud_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
                return RedirectResponse(
                    f"/callback/{service}"
                    f"?token={_urlquote(token, safe='')}"
                    f"&jira_refresh_token={_urlquote(refresh_token, safe='')}"
                    f"&jira_cloud_url={_urlquote(cloud_url, safe='')}"
                )

            # ── Linear ──────────────────────────────────────────────────
            elif service == "linear":
                resp = await client.post(
                    cfg["token_url"],
                    data={
                        "client_id":     cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                        "code":          code,
                        "grant_type":    "authorization_code",
                        "redirect_uri":  redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                token = resp.json().get("access_token", "")

            # ── Slack ────────────────────────────────────────────────────
            elif service == "slack":
                # Slack recommends Basic auth for token exchange
                creds = _base64.b64encode(
                    f"{cfg['client_id']}:{cfg['client_secret']}".encode()
                ).decode()
                resp = await client.post(
                    cfg["token_url"],
                    data={"code": code, "redirect_uri": redirect_uri},
                    headers={
                        "Authorization": f"Basic {creds}",
                        "Content-Type":  "application/x-www-form-urlencoded",
                    },
                )
                data = resp.json()
                if not data.get("ok"):
                    err = data.get("error", "slack_error")
                    return RedirectResponse(f"/callback/{service}?error={_urlquote(err, safe='')}")
                # search:read is a user scope → use authed_user.access_token (xoxp-)
                token = (data.get("authed_user") or {}).get("access_token") or ""
                if not token:
                    # fall back to bot token if user token absent
                    token = data.get("access_token", "")

            else:
                token = ""

        if not token:
            return RedirectResponse(f"/callback/{service}?error=no_token")

        return RedirectResponse(f"/callback/{service}?token={_urlquote(token, safe='')}")

    except Exception as exc:
        return RedirectResponse(
            f"/callback/{service}?error={_urlquote(str(exc)[:200], safe='')}"
        )


@app.get("/callback/{service}")
async def oauth_finish(service: str, request: Request):
    """
    Final hop: render a tiny page that postMessages the token to the opener
    and self-closes.  Works for both the direct OAuth path and the relay path.
    """
    params              = dict(request.query_params)
    token               = params.get("token", "")
    jira_cloud_url      = params.get("jira_cloud_url", "")
    jira_refresh_token  = params.get("jira_refresh_token", "")
    error               = params.get("error", "")

    # Debug logging for GitLab
    if service == "gitlab":
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"GitLab OAuth callback - token present: {bool(token)}, error: {error}")
        if token:
            logger.info(f"GitLab token length: {len(token)}, starts with: {token[:10]}")

    payload: dict = {"service": service}
    if error:
        payload["error"] = error
    else:
        payload["token"] = token
        if jira_cloud_url:
            payload["jira_cloud_url"] = jira_cloud_url
        if jira_refresh_token:
            payload["jira_refresh_token"] = jira_refresh_token

    js_payload = json.dumps(payload)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Connecting…</title>
<style>
  body{{font-family:-apple-system,sans-serif;background:#09090b;color:#e4e4e7;
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    height:100vh;margin:0;gap:12px}}
  .spinner{{width:28px;height:28px;border:3px solid #27272a;border-top-color:#6366f1;
    border-radius:50%;animation:spin .7s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head><body>
<div class="spinner"></div>
<p style="font-size:14px;color:#a1a1aa">{"Error: " + error if error else "Connecting…"}</p>
<script>
(function(){{
  var payload = {js_payload};
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.postMessage(payload, window.location.origin);
      setTimeout(function(){{ window.close(); }}, 400);
      return;
    }}
  }} catch(e) {{}}
  // Fallback: no opener (e.g. redirect flow) — store and go home
  if (!payload.error) {{
    var k = {{ github:'sd_gh_token', gitlab:'sd_gl_token', linear:'sd_linear_token',
               slack:'sd_slack_token', jira:'sd_auto_jira_token' }}[payload.service];
    if (k && payload.token) localStorage.setItem(k, payload.token);
    if (payload.jira_cloud_url) localStorage.setItem('sd_auto_jira_url', payload.jira_cloud_url);
    if (payload.jira_refresh_token) localStorage.setItem('sd_auto_jira_refresh_token', payload.jira_refresh_token);
  }}
  window.location.href = '/';
}})();
</script>
</body></html>""")


def run():
    """Entry point for `1spur-scanner` CLI command."""
    uvicorn.run("scanner.main:app", host="0.0.0.0", port=7433)


if __name__ == "__main__":
    run()
