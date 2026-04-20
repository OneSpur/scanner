"""
Drift analyzer — 5-step pipeline from algorithm-top-to-bottom-v1.md
Step 0: pairs already built by github.py
Step 1: grade intent quality (deterministic, no LLM)
Step 2: extract intent (LLM call #1)
Step 3: extract implementation (LLM call #2)
Step 4: compare requirements vs implementation (LLM call #3)
Step 5: filter and rank findings
"""

import re
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable

# ── Skills loader ─────────────────────────────────────────────────────────────

_SKILLS_DIR = Path(__file__).parent / "skills"


def _load_skill(filename: str, fallback: str) -> str:
    """Load a prompt from skills/ — fall back to hardcoded string if missing."""
    try:
        return (_SKILLS_DIR / filename).read_text().strip()
    except FileNotFoundError:
        return fallback


def _load_config() -> dict:
    """Load config.yaml from skills/ — return empty dict if missing."""
    try:
        import yaml
        return yaml.safe_load((_SKILLS_DIR / "config.yaml").read_text()) or {}
    except Exception:
        return {}


_cfg = _load_config()


# ── Noise filter ──────────────────────────────────────────────────────────────

_noise_cfg = _cfg.get("noise", {})

_noise_title_patterns = _noise_cfg.get("title_patterns") or [
    r"^ci\s*:", r"^chore\s*:", r"^build\s*:", r"^bump\s",
    r"^update\s+(dep|package|version)", r"renovate", r"dependabot",
    r"add\s+(explicit\s+)?timeout", r"fix\s+typo", r"spelling",
    r"^lint\s", r"^format\s", r"add\s+missing\s+(import|type)",
    r"remove\s+unused", r"cleanup", r"clean[\s_-]up",
]
_NOISE_TITLE = re.compile(r"^\s*(" + "|".join(_noise_title_patterns) + r")", re.I)

_NOISE_LABELS = set(_noise_cfg.get("labels") or [
    "dependencies", "ci", "chore", "documentation", "maintenance", "infrastructure"
])


def _is_noise(issue: dict) -> bool:
    """Return True for pure infra/CI/deps issues — no product spec to drift from."""
    title = issue.get("title", "")
    labels = {l.lower() for l in (issue.get("labels") or [])}
    if _NOISE_TITLE.match(title):
        return True
    # All labels are infra — no product context
    if labels and labels.issubset(_NOISE_LABELS):
        return True
    return False


# ── Step 1: Grade intent quality ──────────────────────────────────────────────

_grade_cfg     = _cfg.get("intent_grading", {})
_score_cfg     = _grade_cfg.get("scoring", {})
_RICH_THRESHOLD   = _grade_cfg.get("rich_threshold", 4)
_MEDIUM_THRESHOLD = _grade_cfg.get("medium_threshold", 2)
_MIN_BODY_LEN  = _score_cfg.get("min_body_length", 100)
_REQ_KEYWORDS  = _score_cfg.get("requirement_keywords") or ["should", "must", "shall", "needs? to", "required", "expect"]
_REQ_LABELS    = _score_cfg.get("relevant_labels") or ["feature", "bug", "enhancement", "spec"]
_REQ_KW_PATTERN = re.compile(r"\b(" + "|".join(_REQ_KEYWORDS) + r")\b", re.I)


def _grade_intent(issue: dict) -> str:
    """
    Score the issue body quality to decide whether to spend LLM calls on it.
    Thresholds and scoring weights are loaded from skills/config.yaml.
    """
    body = issue.get("body") or ""
    score = 0

    if len(body) > _MIN_BODY_LEN:
        score += _score_cfg.get("min_body_length_score", 1)
    if re.search(r"[-*]\s|\[[ x]\]", body):  # bullet list or checkbox
        score += _score_cfg.get("has_structure_score", 2)
    if _REQ_KW_PATTERN.search(body):
        score += _score_cfg.get("has_requirement_keywords_score", 1)
    if any(l in (issue.get("labels") or []) for l in _REQ_LABELS):
        score += _score_cfg.get("has_relevant_label_score", 1)
    if issue.get("comments", 0) > 0:
        score += _score_cfg.get("has_comments_score", 1)

    if score >= _RICH_THRESHOLD:
        return "rich"
    if score >= _MEDIUM_THRESHOLD:
        return "medium"
    return "thin"


# ── LLM callers ───────────────────────────────────────────────────────────────

def _find_claude_binary() -> str:
    """Locate the claude CLI binary — checks PATH then common install locations."""
    import shutil, glob
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/bin/claude",
        "/usr/local/Homebrew/Caskroom/claude-code/*/claude",
        str(Path.home() / ".local/bin/claude"),
        str(Path.home() / "Library/Application Support/Claude/claude"),
    ]
    for pattern in candidates:
        matches = glob.glob(pattern)
        if matches:
            return sorted(matches)[-1]  # take latest version if glob matched
    raise FileNotFoundError(
        "Claude Code CLI not found. Install it from claude.ai/download or ensure 'claude' is in your PATH."
    )

_CLAUDE_BIN: str | None = None

def _get_claude_bin() -> str:
    global _CLAUDE_BIN
    if _CLAUDE_BIN is None:
        _CLAUDE_BIN = _find_claude_binary()
    return _CLAUDE_BIN


async def _call_claude_code(system: str, user: str, _retries: int = 4) -> str:
    """Use Claude Code CLI with stored subscription credentials (no API key needed)."""
    import asyncio, random
    claude_bin = _get_claude_bin()
    for attempt in range(_retries):
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p",
            "--output-format", "text",
            "--no-session-persistence",
            "--model", "claude-haiku-4-5",
            "--system-prompt", system,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=user.encode()), timeout=180
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError("Claude Code error: subprocess timed out after 180s")
        if proc.returncode == 0:
            return stdout.decode()
        out = stdout.decode()[:400]
        err = stderr.decode()[:400]
        combined = (out or err).lower()
        if "rate limit" in combined or "429" in combined or "overloaded" in combined:
            wait = min(15 * (2 ** attempt), 60) + random.uniform(0, 5)
            print(f"    [claude-code] rate limit (attempt {attempt+1}), waiting {wait:.0f}s...", flush=True)
            await asyncio.sleep(wait)
            continue
        # Daily usage limit or other hard error — don't retry
        raise RuntimeError(f"Claude Code error: {(out or err)[:300]}")
    raise RuntimeError("Claude Code error: rate limit — all retries exhausted")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~1.3 tokens per word."""
    return max(1, int(len(text.split()) * 1.3))


async def _call_llm(provider: str, model: str, api_key: str | None,
                    system: str, user: str,
                    _tokens: dict | None = None) -> str:
    if provider == "claude-code":
        result = await _call_claude_code(system, user)
        if _tokens is not None:
            _tokens["input"]  += _estimate_tokens(system + user)
            _tokens["output"] += _estimate_tokens(result)
            _tokens["estimated"] = True
        return result

    if provider == "ollama":
        import httpx
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={"model": model, "prompt": user, "stream": False,
                      "system": system, "options": {"temperature": 0}},
            )
            resp.raise_for_status()
            data = resp.json()
            if _tokens is not None:
                _tokens["input"]  += data.get("prompt_eval_count") or _estimate_tokens(system + user)
                _tokens["output"] += data.get("eval_count") or _estimate_tokens(data["response"])
                if not data.get("prompt_eval_count"):
                    _tokens["estimated"] = True
            return data["response"]

    elif provider == "claude":
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model=model, max_tokens=1024, temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if _tokens is not None:
            _tokens["input"]  += msg.usage.input_tokens
            _tokens["output"] += msg.usage.output_tokens
        return msg.content[0].text

    elif provider == "openai":
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": 0,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                      "max_tokens": 1024},
            )
            resp.raise_for_status()
            data = resp.json()
            if _tokens is not None:
                usage = data.get("usage", {})
                _tokens["input"]  += usage.get("prompt_tokens", 0) or _estimate_tokens(system + user)
                _tokens["output"] += usage.get("completion_tokens", 0) or _estimate_tokens(data["choices"][0]["message"]["content"])
                if not usage.get("prompt_tokens"):
                    _tokens["estimated"] = True
            return data["choices"][0]["message"]["content"]

    raise ValueError(f"Unknown provider: {provider}")


def _parse_json(raw: str) -> dict | list:
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"[\[{].*[\]}]", raw, re.S)
        if match:
            candidate = re.sub(r",\s*([\}\]])", r"\1", match.group())
            try:
                return json.loads(candidate)
            except Exception:
                pass
    return {}


# ── Single-call analysis prompt ───────────────────────────────────────────────

DRIFT_SYSTEM = _load_skill("drift_detection.md", """\
You are a senior engineer doing a spec-vs-code review. Given a GitHub issue (the spec) and a PR diff (the implementation), find real divergences between what was promised and what was shipped.

Return JSON only. No markdown. Be conservative — fewer precise findings beat many vague ones.""")

def _drift_prompt(issue: dict, prs: list, grade: str, pair: dict | None = None, extra_context: str | None = None) -> str:
    body = (issue.get("body") or "")[:1500]
    labels = ", ".join(issue.get("labels") or [])
    # In PR-description mode the "spec" is the PR's own description, not a separate issue
    is_pr_desc_mode = issue.get("_pr_description_mode", False)

    proj_lines = []
    proj_name = issue.get("project", "")
    proj_fields = issue.get("project_fields") or {}
    if proj_name:
        proj_lines.append(f"PROJECT: {proj_name}")
    for k, v in proj_fields.items():
        proj_lines.append(f"{k.upper()}: {v}")
    project_section = ("\nPROJECT CONTEXT:\n" + "\n".join(proj_lines) + "\n") if proj_lines else ""

    # Jira spec context
    jira_section = ""
    for jt in ((pair or {}).get("jira_tickets") or [])[:2]:
        parts = [f"JIRA {jt['id']}: {jt['summary']}"]
        if jt.get("issue_type"):
            parts.append(f"Type: {jt['issue_type']}")
        if jt.get("description"):
            parts.append(f"Description:\n{jt['description'][:800]}")
        if jt.get("acceptance_criteria"):
            parts.append(f"ACCEPTANCE CRITERIA (hard requirements):\n{jt['acceptance_criteria'][:600]}")
        if jt.get("comments"):
            discussion = "\n".join(f"- {c}" for c in jt["comments"][:6])
            parts.append(f"Team discussion (may contain scope decisions):\n{discussion}")
        jira_section += "\nJIRA SPEC:\n" + "\n".join(parts) + "\n"

    # Linear spec context
    linear_section = ""
    for lt in ((pair or {}).get("linear_tickets") or [])[:2]:
        parts = [f"LINEAR {lt['id']}: {lt['summary']}"]
        if lt.get("priority"):
            parts.append(f"Priority: {lt['priority']}")
        if lt.get("description"):
            parts.append(f"Description:\n{lt['description'][:800]}")
        if lt.get("acceptance_criteria"):
            parts.append(f"ACCEPTANCE CRITERIA (hard requirements):\n{lt['acceptance_criteria'][:600]}")
        if lt.get("parent_context"):
            parts.append(f"Parent feature context:\n{lt['parent_context'][:400]}")
        if lt.get("comments"):
            discussion = "\n".join(f"- {c}" for c in lt["comments"][:6])
            parts.append(f"Team discussion (may contain scope decisions):\n{discussion}")
        linear_section += "\nLINEAR SPEC:\n" + "\n".join(parts) + "\n"

    pr_sections = []
    for pr in prs:
        files_str = "\n".join(pr.get("files", [])[:15])
        diff = pr.get("diff_text", "")[:4000]
        reviews_str = "\n".join(
            f"- [{r['state']}] {r['body'][:200]}"
            for r in pr.get("reviews", [])[:5]
            if r.get("body")
        )
        pr_sections.append(
            f"PR #{pr['number']}: {pr['title']}\n"
            f"DESCRIPTION: {pr.get('body','')[:300]}\n"
            f"FILES CHANGED:\n{files_str}\n"
            f"DIFF:\n{diff}\n"
            + (f"REVIEW COMMENTS:\n{reviews_str}\n" if reviews_str else "")
        )

    # When a spec doc is uploaded, it's the primary source of requirements — allow more
    n_req = 8 if extra_context else (2 if grade == "medium" else 4)

    extra_section = f"\nADDITIONAL SPEC DOCUMENTS (uploaded by user — treat as authoritative background context). Each file is delimited by === filename === headers:\n{extra_context[:12000]}\n" if extra_context else ""

    # Slack discussion context
    slack_disc = ((pair or {}).get("slack_discussion") or "").strip()
    slack_section = f"\nSLACK TEAM DISCUSSION:\n{slack_disc[:1500]}\n" if slack_disc else ""

    # Knowledge-graph context (injected by pipeline enricher)
    qdrant_ctx = ((pair or {}).get("qdrant_context") or "").strip()
    qdrant_section = (
        f"\nKNOWLEDGE GRAPH CONTEXT (semantically related entities — "
        f"acceptance criteria, Jira/Linear notes, changed functions):\n{qdrant_ctx}\n"
    ) if qdrant_ctx else ""

    issue_author = issue.get("author", "")
    author_line = f"ISSUE AUTHOR: @{issue_author}\n" if issue_author else ""

    spec_label = "PR DESCRIPTION (used as spec — no linked issue found)" if is_pr_desc_mode else "SPEC"
    item_label  = "PR" if is_pr_desc_mode else "ISSUE"

    return f"""{item_label} #{issue['number']}: {issue['title']}
LABELS: {labels}
{author_line}{spec_label}:
{body}
{project_section}{jira_section}{linear_section}{slack_section}{qdrant_section}{extra_section}
{"".join(pr_sections)}

TASK — do this in your head, return only the final JSON:
1. Extract up to {n_req} concrete, user-facing business requirements from the spec (skip style/refactor/internal).{"  The spec is the PR description itself — treat it as the intended behaviour for this change." if is_pr_desc_mode else ""}
2. If SLACK TEAM DISCUSSION is present, also extract any additional requirements or spec changes discussed there that are NOT already in the spec — treat them as late-breaking requirements.
3. For each requirement (from spec or Slack), read the diff and decide:

"drift" — code CONTRADICTS the requirement (does something different).
  → spec_said: what was expected (plain English, 1 sentence)
  → code_does: what the code actually does instead (specific, reference the diff)
  → code_evidence: REQUIRED — "filename.ext:START-END" copied exactly from diff header + line numbers of the key changed lines (e.g. "wagtail/forms.py:45-52"). Single line ok too: "wagtail/forms.py:45". No real lines = not drift.
  → severity: "high" | "medium" | "low"
  → code_fix REQUIRED: 1-sentence suggestion of what the code should do to satisfy the spec (start with a verb, e.g. "Update X to…")
  → doc_fix REQUIRED: 1-sentence suggestion of how to update the spec/ticket to accept the current code instead (start with a verb, e.g. "Update the spec to…")

"gap" — requirement is COMPLETELY absent from the diff (not touched at all, and clearly in scope).
  → spec_said: what was expected
  → code_does: "Not implemented"
  → code_evidence: ""
  → severity: "high" | "medium" | "low"
  → note: "Forgotten" | "Deferred" | "Addressed elsewhere"
  → code_fix REQUIRED: 1-sentence suggestion of what needs to be implemented (start with a verb, e.g. "Add X to…")
  → doc_fix: "" (always empty for gaps)

"aligned" — requirement is satisfied. Do NOT include.

RULES:
- drift MUST have a real file:line from the diff. No evidence = aligned or gap.
- Do NOT flag things obvious from the PR title — find non-obvious divergences.
- If a Slack discussion contains a clear decision that contradicts or extends the spec (e.g. a rename, a new constraint, a dropped feature), flag it as drift or gap even if the original spec doesn't mention it.
- Return empty results if everything is aligned.
- source_author: the @handle or display name of whoever stated this requirement. Use @handle for GitHub (from ISSUE AUTHOR line), or the name prefix in Jira/Linear comments (e.g. "Jane Smith" from "Jane Smith: the button should..."), or "team" for Slack.
- source_platform: "github_issue" if from the issue body, "jira" if from a Jira comment, "linear" if from a Linear comment, "slack" if from Slack, "doc" if from an uploaded spec document.

Return:
{{"results": [{{"spec_said": "...", "code_does": "...", "code_evidence": "file.py:LINE or empty", "code_snippet": "2-4 actual lines from the diff showing the divergence, or empty string", "status": "drift|gap", "severity": "high|medium|low", "note": "", "source_quote": "exact quote from issue proving this requirement (max 100 chars)", "source_author": "handle or display name", "source_platform": "github_issue|jira|linear|slack|doc", "context_source": "filename if requirement came from an ADDITIONAL SPEC DOCUMENT, else empty string", "code_fix": "1-sentence code fix suggestion", "doc_fix": "1-sentence doc/spec fix suggestion or empty for gaps"}}]}}"""


# ── Evidence URL builder ──────────────────────────────────────────────────────

def _build_evidence_url(owner: str, repo: str, sha: str, evidence_text: str) -> str:
    """Convert 'path/file.py:45' or 'path/file.py:45-52' → GitHub blob URL."""
    if not evidence_text or ":" not in evidence_text:
        return ""
    parts = evidence_text.rsplit(":", 1)
    if len(parts) != 2:
        return ""
    filename, line_part = parts[0].strip(), parts[1].strip()
    filename = re.sub(r"^[ab]/", "", filename).lstrip("+-/ \t")
    if not filename:
        return ""
    # Accept both "45" and "45-52" formats
    line_match = re.match(r"^(\d+)(?:-(\d+))?$", line_part)
    if not line_match:
        return ""
    start_line = line_match.group(1)
    end_line = line_match.group(2)
    ref = sha if sha else "HEAD"
    url = f"https://github.com/{owner}/{repo}/blob/{ref}/{filename}#L{start_line}"
    if end_line:
        url += f"-L{end_line}"
    return url


# ── When-found detector ───────────────────────────────────────────────────────

def _find_when_discovered(
    issue_number: int,
    pr_merged_at: str | None,
    all_issues: list,
) -> dict:
    """
    Scan all_issues for a follow-up that references the original issue number,
    created AFTER the PR was merged.
    Returns a dict describing when (if ever) the drift was surfaced.
    """
    if not pr_merged_at:
        return {"status": "unknown"}

    try:
        merged_dt = datetime.fromisoformat(pr_merged_at.replace("Z", "+00:00"))
    except Exception:
        return {"status": "unknown"}

    pattern = re.compile(rf"#{issue_number}\b")

    for iss in all_issues:
        created_raw = iss.get("created_at", "")
        if not created_raw:
            continue
        try:
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        if created_dt <= merged_dt:
            continue  # only look at issues opened after merge

        body = iss.get("body") or ""
        title = iss.get("title", "")
        if pattern.search(body) or pattern.search(title):
            hours_later = round((created_dt - merged_dt).total_seconds() / 3600)
            return {
                "status": "found",
                "hours_later": hours_later,
                "follow_up_number": iss["number"],
                "follow_up_title": title,
                "follow_up_url": iss.get("html_url", ""),
            }

    hours_since = round((datetime.now(timezone.utc) - merged_dt).total_seconds() / 3600)
    return {"status": "not_found", "hours_since_merge": hours_since}


# ── Step 5: Filter and rank ───────────────────────────────────────────────────

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
STATUS_ORDER = {"drift": 0, "gap": 1}


def _rank_findings(findings: list) -> list:
    # Drop unclear and aligned
    findings = [f for f in findings if f.get("status") in ("drift", "gap")]
    # Drift findings MUST have a file:line evidence — otherwise they're unverifiable
    findings = [
        f for f in findings
        if f.get("status") != "drift" or (
            f.get("code_evidence") and
            f["code_evidence"] not in ("", "no_evidence", "N/A", "none", "None")
        )
    ]
    # Drop implied low-severity
    findings = [
        f for f in findings
        if not (f.get("confidence") == "implied" and f.get("severity") == "low")
    ]
    findings.sort(key=lambda f: (
        SEVERITY_ORDER.get(f.get("severity")),
        STATUS_ORDER.get(f.get("status"), 2),
        0 if f.get("confidence") == "explicit" else 1,
    ))
    return findings


# ── Per-pair analysis ─────────────────────────────────────────────────────────

async def _analyze_pair(
    pair: dict,
    provider: str,
    model: str,
    api_key: str | None,
    log: Callable,
    owner: str = "",
    repo: str = "",
    all_issues: list | None = None,
    extra_context: str | None = None,
    _tokens: dict | None = None,
) -> list[dict]:
    """Run 1 LLM call for one issue-PR pair. Returns list of findings."""
    issue = pair["issue"]
    prs = pair["prs"]

    # Noise filter
    if _is_noise(issue):
        log(f"  #{issue['number']} '{issue['title'][:40]}' — skipped (infra/ci noise)")
        return []

    grade = _grade_intent(issue)
    if grade == "thin":
        log(f"  #{issue['number']} '{issue['title'][:40]}' — skipped (thin intent)")
        return []

    log(f"  #{issue['number']} '{issue['title'][:40]}' — grade: {grade}")

    primary_pr = prs[0]
    pr_merged_at = primary_pr.get("merged_at") or issue.get("closed_at", "")
    merge_sha = primary_pr.get("merge_commit_sha", "")
    try:
        raw = await _call_llm(provider, model, api_key,
                              DRIFT_SYSTEM, _drift_prompt(issue, prs, grade, pair, extra_context=extra_context),
                              _tokens=_tokens)
        data = _parse_json(raw)
        results = data.get("results", [])
    except Exception as e:
        log(f"    Analysis failed: {e.__class__.__name__}: {str(e)[:300]}")
        return []

    findings = []
    for result in results:
        if result.get("status") not in ("drift", "gap"):
            continue

        evidence_text = result.get("code_evidence", "")
        evidence_url = _build_evidence_url(owner, repo, merge_sha, evidence_text) if evidence_text else ""
        when_found = _find_when_discovered(issue["number"], pr_merged_at, all_issues or [])

        # ── Source attribution ────────────────────────────────────────────────
        source_author   = result.get("source_author", "") or ""
        source_platform = result.get("source_platform", "github_issue") or "github_issue"

        # Build a link for the attribution chip
        if source_platform == "github_issue":
            # Strip leading @ if LLM included it, then build profile URL
            handle = source_author.lstrip("@") or issue.get("author", "")
            source_author_url = f"https://github.com/{handle}" if handle else issue.get("author_url", "")
            source_author = f"@{handle}" if handle else source_author
        elif source_platform == "jira":
            # Link to the Jira ticket, not the user profile
            jira_tickets = pair.get("jira_tickets") or []
            source_author_url = jira_tickets[0]["url"] if jira_tickets else ""
        elif source_platform == "linear":
            linear_tickets = pair.get("linear_tickets") or []
            source_author_url = linear_tickets[0]["url"] if linear_tickets else ""
        elif source_platform == "slack":
            slack_authors = pair.get("slack_authors", [])
            # Try to match the LLM-identified author name to a known Slack author
            matched = next(
                (a for a in slack_authors if source_author and a["name"].lower() in source_author.lower()),
                None
            )
            if matched:
                source_author_url = matched["url"]
            elif slack_authors:
                # Fall back to first author's permalink
                source_author_url = slack_authors[0]["url"]
            else:
                slack_links = pair.get("slack_permalinks", [])
                source_author_url = slack_links[0] if slack_links else ""
        else:
            source_author_url = ""

        findings.append({
            "status": result["status"],
            "severity": result.get("severity"),
            "confidence": "explicit" if result.get("source_quote") else "implied",
            "issue_number": issue["number"],
            "issue_title": issue["title"],
            "issue_url": issue["url"],
            "pr_number": primary_pr["number"],
            "pr_title": primary_pr["title"],
            "pr_url": primary_pr["url"],
            "pr_author": primary_pr.get("author", ""),
            "requirement": result.get("spec_said", ""),
            "requirement_source": result.get("source_quote", ""),
            "context_source": result.get("context_source", ""),
            "source_author": source_author,
            "source_author_url": source_author_url,
            "source_platform": source_platform,
            "spec_said": result.get("spec_said", ""),
            "code_does": result.get("code_does", ""),
            "code_snippet": result.get("code_snippet", ""),
            "code_evidence": evidence_text,
            "code_evidence_url": evidence_url,
            "code_fix": result.get("code_fix", ""),
            "doc_fix": result.get("doc_fix", ""),
            "note": result.get("note", ""),
            "rework_hours": sum(pr.get("rework_hours", 0) for pr in prs),
            "caught_in_review": primary_pr.get("changes_requested", 0) > 0,
            "pr_merged_at": pr_merged_at,
            "when_found": when_found,
        })

    ranked = _rank_findings(findings)

    # False-link guard: if 100% of findings are GAP and the PR body never mentions
    # the issue number, this is likely an incidental cross-reference, not a real link.
    if ranked and all(f.get("status") == "gap" for f in ranked):
        issue_ref = f"#{issue['number']}"
        pr_mentions = any(issue_ref in (pr.get("body") or "") for pr in prs)
        if not pr_mentions and not _titles_share_topic(issue["title"], primary_pr["title"]):
            log(f"    Skipping — likely false issue-PR link (all gaps, no topic overlap)")
            return []

    log(f"    {len(ranked)} findings (drift/gap) after filtering")
    return ranked


_TOPIC_STOPWORDS = set(_cfg.get("false_link_stopwords") or [
    "docs", "test", "tests", "fix", "fixes", "fixed", "add", "adds", "added",
    "update", "updates", "change", "changes", "improve", "feature", "with",
    "from", "when", "that", "this", "have", "will", "also", "more", "first",
    "version", "support", "using", "allow", "make", "page", "admin", "error",
    "issue", "into", "file", "code", "item", "list", "block", "form",
])


def _titles_share_topic(issue_title: str, pr_title: str) -> bool:
    """Return True if issue and PR share at least one non-generic keyword."""
    issue_words = {w for w in re.findall(r"\b[a-z]{4,}\b", issue_title.lower()) if w not in _TOPIC_STOPWORDS}
    pr_words    = {w for w in re.findall(r"\b[a-z]{4,}\b", pr_title.lower())    if w not in _TOPIC_STOPWORDS}
    return bool(issue_words & pr_words)


SUMMARY_SYSTEM = _load_skill("summary.md", """\
You are a technical writer generating a 3-5 sentence executive summary of spec drift findings for a tech lead or engineering manager.

Be specific and concrete — name actual issues, PRs, requirements, and what diverged. Avoid generic filler. Write in plain English, no markdown, no bullet points, no em dashes.

Focus on: what was supposed to be built vs what was actually shipped, what rework cost the team, what risks or gaps remain. Make it feel real and actionable.""")


async def _generate_exec_summary(
    top_findings: list,
    rework_days: float,
    pairs_analyzed: int,
    unlinked_count: int,
    provider: str,
    model: str,
    api_key: str | None,
) -> str:
    if not top_findings and rework_days <= 0:
        return ""

    findings_text = ""
    for f in top_findings[:6]:
        findings_text += (
            f"[{f['status'].upper()} / {f.get('severity','?')}] "
            f"Issue #{f['issue_number']} \"{f['issue_title']}\"\n"
            f"  Requirement: {f['requirement'][:200]}\n"
            f"  Source quote: {f['requirement_source'][:150]}\n"
            f"  Evidence: {(f.get('evidence') or f.get('note',''))[:200]}\n\n"
        )

    user_msg = f"""Scanned {pairs_analyzed} issue-PR pairs. Rework: {rework_days} engineer-hours. Unlinked issues: {unlinked_count}.

Top findings:
{findings_text}
Write a 3-5 sentence executive summary for a tech lead. Be specific — name actual issues/requirements/divergences. No bullet points."""

    result = await _call_llm(provider, model, api_key, SUMMARY_SYSTEM, user_msg)
    # Strip any accidental markdown
    return result.strip().replace("**", "").replace("##", "").replace("# ", "")


# ── Batch analysis (all pairs → one LLM call) ─────────────────────────────────

BATCH_SYSTEM = _load_skill("batch_analysis.md", """\
You are a senior engineer doing a spec-vs-code review across multiple GitHub issues and PRs.
For each pair provided, find real divergences between what was promised and what was shipped.
Return JSON only. No markdown. Be conservative — fewer precise findings beat many vague ones.""")

def _batch_prompt(eligible_pairs: list, extra_context: str | None = None) -> str:
    parts = []
    for i, (pair, grade) in enumerate(eligible_pairs):
        issue = pair["issue"]
        prs = pair["prs"]
        body = (issue.get("body") or "")[:1000]
        labels = ", ".join(issue.get("labels") or [])
        n_req = 6 if extra_context else (2 if grade == "medium" else 3)

        pr_sections = []
        for pr in prs:
            files_str = "\n".join(pr.get("files", [])[:10])
            diff = pr.get("diff_text", "")[:4000]
            reviews_str = "\n".join(
                f"- [{r['state']}] {r['body'][:150]}"
                for r in pr.get("reviews", [])[:3]
                if r.get("body")
            )
            pr_sections.append(
                f"  PR #{pr['number']}: {pr['title']}\n"
                f"  FILES: {files_str}\n"
                f"  DIFF:\n{diff}\n"
                + (f"  REVIEWS:\n{reviews_str}\n" if reviews_str else "")
            )

        slack_disc = pair.get("slack_discussion", "")
        slack_section = f"\nSLACK TEAM DISCUSSION:\n{slack_disc[:1500]}\n" if slack_disc else ""

        parts.append(
            f"--- PAIR {i+1} ---\n"
            f"ISSUE #{issue['number']}: {issue['title']}\n"
            f"LABELS: {labels}\n"
            f"SPEC: {body}\n"
            + slack_section
            + "\n".join(pr_sections)
        )

    extra_section = f"\nADDITIONAL SPEC DOCUMENTS (uploaded by user — treat as authoritative background context for ALL pairs):\n{extra_context[:12000]}\n" if extra_context else ""
    prompt = extra_section + "\n\n".join(parts)
    prompt += """

For EACH pair above:
1. Extract up to 3 user-facing business requirements from the spec (and from ADDITIONAL SPEC DOCUMENTS if provided).
2. If SLACK TEAM DISCUSSION is present, also extract any additional requirements or spec changes discussed there that are NOT already in the spec — treat them as late-breaking requirements.
3. Compare all requirements (from spec or Slack) against the diff.

For each divergence found:
- "drift": code CONTRADICTS the requirement (does something different)
  → code_evidence REQUIRED: "filename.ext:START-END" — copy the filename EXACTLY from the "--- " diff header, use the line numbers of the relevant changed lines (e.g. "wagtail/forms.py:45-52"). Single line: "wagtail/forms.py:45". No real lines = skip it.
  → code_fix REQUIRED: 1-sentence suggestion of what the code should change to satisfy the spec (start with a verb)
  → doc_fix REQUIRED: 1-sentence suggestion of how to update the spec/ticket to accept the current code instead (start with a verb, e.g. "Update the spec to…")
- "gap": requirement is completely absent from the diff and clearly in scope
  → code_evidence: ""
  → code_fix REQUIRED: 1-sentence suggestion of what needs to be implemented (start with a verb)
  → doc_fix: ""
- skip "aligned" requirements entirely

RULES:
- drift MUST have a real file:line copied exactly from the diff header + line number. No hallucinated lines.
- If a Slack discussion contains a clear decision that contradicts or extends the spec (e.g. a rename, a new constraint, a dropped feature), flag it as drift or gap even if the original spec doesn't mention it.
- Return empty results array for a pair if everything is aligned.
- Fewer precise findings beat many vague ones.

Return:
{"pairs": [{"issue_number": 123, "results": [{"spec_said": "...", "code_does": "...", "code_evidence": "file.py:LINE or empty", "code_snippet": "2-4 actual lines from the diff, or empty", "status": "drift|gap", "severity": "high|medium|low", "note": "", "source_quote": "exact quote from spec (max 100 chars)", "context_source": "filename if requirement came from an ADDITIONAL SPEC DOCUMENT, else empty string", "code_fix": "1-sentence code fix suggestion", "doc_fix": "1-sentence doc/spec fix suggestion or empty for gaps"}]}, ...]}"""
    return prompt


# ── Step 5b: Deterministic evidence check ─────────────────────────────────────

def _drop_unverifiable_evidence(findings: list, pairs: list) -> list:
    """Drop DRIFT findings whose evidence file is not in the PR's changed files."""
    # Build lookup: issue_number -> set of changed filenames (basenames + full paths)
    changed_files: dict[int, set] = {}
    for pair in pairs:
        inum = pair["issue"]["number"]
        files = set()
        for pr in pair.get("prs", []):
            for f in pr.get("files", []):
                # f is "path/to/file.py" — include both full and basename
                files.add(f)
                files.add(f.split("/")[-1])
        changed_files[inum] = files

    result = []
    for f in findings:
        if f.get("status") != "drift":
            result.append(f)
            continue
        evidence = f.get("code_evidence", "")
        if not evidence:
            result.append(f)
            continue
        # Extract filename from "path/file.py:LINE"
        filename = evidence.split(":")[0].strip().lstrip("ab/+-\t ")
        basename = filename.split("/")[-1]
        known = changed_files.get(f["issue_number"], set())
        if not known or filename in known or basename in known:
            result.append(f)
        # else: drop — evidence file not in the PR diff
    return result


# ── Step 5c: Verify findings (judge pass) ─────────────────────────────────────

JUDGE_SYSTEM = _load_skill("judge.md", """\
You verify whether a spec drift finding is a clear false positive.
Return JSON only. No markdown. Default to valid=true when uncertain.""")

def _judge_prompt(finding: dict, issue_body: str, diff_text: str) -> str:
    return f"""ISSUE #{finding['issue_number']}: {finding['issue_title']}
ISSUE BODY (excerpt):
{issue_body[:800]}

PR DIFF (excerpt):
{diff_text[:2000]}

FINDING TO VERIFY:
- Type: {finding['status'].upper()}
- spec_said: {finding.get('spec_said', '')}
- code_does: {finding.get('code_does', '')}
- code_evidence: {finding.get('code_evidence', '')}

Set valid=false ONLY IF the finding is clearly wrong in one of these ways:
1. spec_said describes something not mentioned anywhere in the issue
2. The PR and issue are clearly unrelated (different feature/area entirely)
3. For DRIFT: the code actually does satisfy the spec — the finding has it backwards
4. code_evidence references a file that is not in the diff at all

If in doubt, return valid=true. A possibly-real finding is better than a missed one.

Return: {{"valid": true|false, "reason": "one sentence"}}"""


async def _verify_findings(
    findings: list,
    pairs: list,
    provider: str,
    model: str,
    api_key: str | None,
    log: Callable,
) -> list:
    """Run a lightweight judge LLM call on each finding. Drop findings the judge rejects."""
    # Build lookup: issue_number -> (issue_body, diff_text)
    pair_lookup: dict = {}
    for pair in pairs:
        inum = pair["issue"]["number"]
        body = pair["issue"].get("body") or ""
        diff = "".join(pr.get("diff_text", "")[:1000] for pr in pair.get("prs", []))
        pair_lookup[inum] = (body, diff)

    verified = []
    judge_concurrency = 2 if provider == "claude-code" else 4
    sem = asyncio.Semaphore(judge_concurrency)

    async def _judge_one(f):
        async with sem:
            inum = f["issue_number"]
            issue_body, diff_text = pair_lookup.get(inum, ("", ""))
            try:
                raw = await _call_llm(provider, model, api_key,
                                      JUDGE_SYSTEM, _judge_prompt(f, issue_body, diff_text))
                result = _parse_json(raw)
                valid = result.get("valid", True)
                reason = result.get("reason", "")
                if not valid:
                    log(f"    Judge rejected #{inum} finding: {reason[:80]}")
                return f if valid else None
            except Exception:
                return f  # on error, keep the finding

    results = await asyncio.gather(*[_judge_one(f) for f in findings])
    verified = [f for f in results if f is not None]
    dropped = len(findings) - len(verified)
    if dropped:
        log(f"  Judge dropped {dropped} false positive(s)")
    return verified


# ── Batch analysis runner ─────────────────────────────────────────────────────

async def _analyze_batch(
    batch: list,  # list of (pair, grade)
    provider: str,
    model: str,
    api_key: str | None,
    log: Callable,
    owner: str,
    repo: str,
    all_issues: list,
    extra_context: str | None = None,
    _tokens: dict | None = None,
) -> list[dict]:
    """Run one LLM call for a batch of pairs. Returns flat list of findings."""
    if not batch:
        return []

    try:
        raw = await _call_llm(provider, model, api_key, BATCH_SYSTEM, _batch_prompt(batch, extra_context=extra_context), _tokens=_tokens)
        data = _parse_json(raw)
        pair_results = data.get("pairs", [])
    except Exception as e:
        log(f"    Batch failed: {e.__class__.__name__}: {str(e)[:200]}")
        return None  # None = failed ([] = succeeded but found nothing)

    # Build lookup: issue_number -> (pair, grade)
    pair_lookup = {pair["issue"]["number"]: (pair, grade) for pair, grade in batch}

    findings = []
    for pr_result in pair_results:
        issue_num = pr_result.get("issue_number")
        results = pr_result.get("results", [])
        if not issue_num or issue_num not in pair_lookup:
            continue

        pair, grade = pair_lookup[issue_num]
        issue = pair["issue"]
        prs = pair["prs"]
        primary_pr = prs[0]
        pr_merged_at = primary_pr.get("merged_at") or issue.get("closed_at", "")
        merge_sha = primary_pr.get("merge_commit_sha", "")
        pair_findings = []
        for result in results:
            if result.get("status") not in ("drift", "gap"):
                continue
            evidence_text = result.get("code_evidence", "")
            evidence_url = _build_evidence_url(owner, repo, merge_sha, evidence_text) if evidence_text else ""
            when_found = _find_when_discovered(issue_num, pr_merged_at, all_issues)

            pair_findings.append({
                "status": result["status"],
                "severity": result.get("severity"),
                "confidence": "explicit" if result.get("source_quote") else "implied",
                "issue_number": issue_num,
                "issue_title": issue["title"],
                "issue_url": issue["url"],
                "pr_number": primary_pr["number"],
                "pr_title": primary_pr["title"],
                "pr_url": primary_pr["url"],
                "pr_author": primary_pr.get("author", ""),
                "requirement": result.get("spec_said", ""),
                "requirement_source": result.get("source_quote", ""),
                "context_source": result.get("context_source", ""),
                "spec_said": result.get("spec_said", ""),
                "code_does": result.get("code_does", ""),
                "code_snippet": result.get("code_snippet", ""),
                "code_evidence": evidence_text,
                "code_evidence_url": evidence_url,
                "code_fix": result.get("code_fix", ""),
                "doc_fix": result.get("doc_fix", ""),
                "note": result.get("note", ""),
                "rework_hours": sum(pr.get("rework_hours", 0) for pr in prs),
                "caught_in_review": primary_pr.get("changes_requested", 0) > 0,
                "pr_merged_at": pr_merged_at,
                "when_found": when_found,
            })

        ranked = _rank_findings(pair_findings)

        # False-link guard (same as per-pair)
        if ranked and all(f.get("status") == "gap" for f in ranked):
            issue_ref = f"#{issue_num}"
            pr_mentions = any(issue_ref in (pr.get("body") or "") for pr in prs)
            if not pr_mentions and not _titles_share_topic(issue["title"], primary_pr["title"]):
                log(f"    Skipping #{issue_num} — likely false issue-PR link")
                continue

        n = len(ranked)
        log(f"  #{issue_num} '{issue['title'][:40]}' — {n} finding{'s' if n != 1 else ''}")
        findings.extend(ranked)

    return findings


# ── Spec-scan mode ────────────────────────────────────────────────────────────

SPEC_SCAN_SYSTEM = """\
You are a senior engineer auditing a codebase against a product specification document.

Given a section of the specification and evidence from the codebase (recent commits with diffs), identify:
1. Requirements that are NOT implemented at all → status: "gap"
2. Requirements that are implemented differently than specified → status: "drift"

Skip requirements that are fully and correctly implemented — do not report them.

Return a JSON array. Each element:
{
  "status": "gap" | "drift",
  "severity": "high" | "medium" | "low",
  "spec_said": "the exact requirement from the spec (verbatim or close paraphrase)",
  "code_does": "what the codebase actually does, or 'Not implemented'",
  "code_snippet": "1-4 line diff snippet as evidence, or empty string",
  "code_evidence": "filename (and optionally :line) from the diff, or empty string",
  "code_fix": "one sentence starting with a verb — what needs to change"
}

Be conservative. Only report genuine gaps and divergences. Return [] if everything in this spec section is correctly implemented."""


def _chunk_spec(spec_text: str, max_chunk: int = 5000) -> list[str]:
    """Split spec text into logical sections, respecting headings."""
    sections = re.split(r'\n(?=(?:#+ |\d+\.\s+[A-Z]|[A-Z][A-Z ]{4,}\n))', spec_text)
    chunks = []
    current = ""
    for section in sections:
        if len(current) + len(section) <= max_chunk:
            current += "\n" + section
        else:
            if current.strip():
                chunks.append(current.strip())
            if len(section) > max_chunk:
                paras = section.split("\n\n")
                para_chunk = ""
                for para in paras:
                    if len(para_chunk) + len(para) <= max_chunk:
                        para_chunk += "\n\n" + para
                    else:
                        if para_chunk.strip():
                            chunks.append(para_chunk.strip())
                        para_chunk = para
                if para_chunk.strip():
                    chunks.append(para_chunk.strip())
                current = ""
            else:
                current = section
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c.strip()) > 100]


def _format_commits_for_spec_scan(commits: list[dict]) -> str:
    """Format commits as a readable evidence block for the LLM."""
    lines = []
    for c in commits:
        lines.append(f"COMMIT {c['sha']}: {c['message']}")
        if c.get("files_changed"):
            lines.append(f"Files: {', '.join(c['files_changed'][:10])}")
        if c.get("diff"):
            lines.append(c["diff"])
        lines.append("")
    return "\n".join(lines)


def _format_repo_files(repo_files: list[dict], budget: int = 20000) -> str:
    """Format fetched source files into a readable block, respecting a char budget."""
    if not repo_files:
        return ""
    lines = ["=== CURRENT SOURCE FILES ==="]
    used = 0
    for f in repo_files:
        header = f"\n--- {f['path']} ---\n"
        content = f["content"]
        if used + len(header) + len(content) > budget:
            remaining = budget - used - len(header) - 40
            if remaining < 200:
                break
            content = content[:remaining] + "\n... (truncated)"
        lines.append(header + content)
        used += len(header) + len(content)
        if used >= budget:
            break
    return "\n".join(lines)


async def analyze_spec(
    repo_data: dict,
    commits: list[dict],
    provider: str,
    model: str,
    api_key: str | None,
    log: Callable,
    extra_context: str,
    repo_files: list[dict] | None = None,
) -> dict:
    """Spec-scan mode: compare uploaded spec doc against codebase commits + files, section by section."""
    owner = repo_data.get("owner", "")
    repo_name = repo_data.get("repo", "")

    chunks = _chunk_spec(extra_context)
    log(f"Spec split into {len(chunks)} sections for analysis...")

    commits_text = _format_commits_for_spec_scan(commits)
    files_text = _format_repo_files(repo_files or [])

    if files_text:
        log(f"Evidence: {len(commits)} commits + {len(repo_files)} source files")
    else:
        log(f"Evidence: {len(commits)} commits, {len(commits_text):,} chars of diffs")

    # Build combined evidence block: files first (current state), then commits (history)
    evidence_parts = []
    if files_text:
        evidence_parts.append(files_text)
    if commits_text:
        evidence_parts.append(f"=== RECENT COMMIT HISTORY ===\n{commits_text[:8000]}")
    evidence_block = "\n\n".join(evidence_parts)

    _tokens: dict = {"input": 0, "output": 0, "estimated": False}
    all_findings = []

    for i, chunk in enumerate(chunks):
        chunk_label = chunk[:60].replace("\n", " ").strip()
        log(f"  Spec section {i+1}/{len(chunks)}: '{chunk_label}...'")

        user_prompt = (
            f"SPEC SECTION:\n{chunk}\n\n"
            f"CODEBASE EVIDENCE ({owner}/{repo_name}):\n{evidence_block}\n\n"
            f"Analyse this spec section against the codebase evidence. Return JSON array of findings."
        )

        try:
            raw = await _call_llm(provider, model, api_key, SPEC_SCAN_SYSTEM, user_prompt, _tokens=_tokens)
            parsed = _parse_json(raw)
            if not isinstance(parsed, list):
                continue

            for f in parsed:
                if not isinstance(f, dict):
                    continue
                if f.get("status") not in ("gap", "drift"):
                    continue
                all_findings.append({
                    "status": f.get("status", "gap"),
                    "severity": f.get("severity", "medium"),
                    "confidence": "implied",
                    "issue_number": 0,
                    "issue_title": f"[Spec] {chunk_label[:60]}",
                    "issue_url": "",
                    "pr_number": 0,
                    "pr_title": "",
                    "pr_url": "",
                    "pr_author": "",
                    "requirement": f.get("spec_said", ""),
                    "requirement_source": f.get("spec_said", ""),
                    "context_source": "spec document",
                    "source_author": "",
                    "source_author_url": "",
                    "source_platform": "doc",
                    "spec_said": f.get("spec_said", ""),
                    "code_does": f.get("code_does", ""),
                    "code_snippet": f.get("code_snippet", ""),
                    "code_evidence": f.get("code_evidence", ""),
                    "code_evidence_url": "",
                    "code_fix": f.get("code_fix", ""),
                    "doc_fix": "",
                    "note": "",
                    "rework_hours": 0.0,
                    "caught_in_review": False,
                    "pr_merged_at": "",
                    "when_found": {"status": "unknown"},
                })

            total = _tokens["input"] + _tokens["output"]
            log(None, tokens=total, estimated=_tokens["estimated"])

        except Exception as e:
            log(f"  Section {i+1} failed: {str(e)[:200]}")
            continue

    sev_order = {"high": 0, "medium": 1, "low": 2}
    all_findings.sort(key=lambda f: sev_order.get(f.get("severity", "low"), 2))

    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for f in all_findings:
        s = f.get("severity", "low")
        severity_counts[s] = severity_counts.get(s, 0) + 1

    log(f"Spec-scan complete — {len(all_findings)} findings across {len(chunks)} spec sections")

    return {
        "findings": all_findings,
        "total_rework_hours": 0.0,
        "exec_summary": "",
        "tokens": _tokens,
        "summary": {
            "total_findings": len(all_findings),
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "drift": sum(1 for f in all_findings if f["status"] == "drift"),
            "gap": sum(1 for f in all_findings if f["status"] == "gap"),
            "pairs_analyzed": len(chunks),
            "issues_unlinked": 0,
            "issues_closed": 0,
            "oversized_prs": 0,
        },
    }


# ── Main entry ────────────────────────────────────────────────────────────────

async def analyze_drift(
    repo_data: dict,
    provider: str,
    model: str,
    api_key: str | None,
    log: Callable,
    extra_context: str | None = None,
) -> dict:
    pairs = repo_data.get("pairs", [])
    stats = repo_data.get("stats", {})
    owner = repo_data.get("owner", "")
    repo = repo_data.get("repo", "")
    all_issues = repo_data.get("all_issues", [])

    # Attach extra_context to repo_data so prompt builders can access it
    if extra_context:
        repo_data["extra_context"] = extra_context
        log(f"Using {len(extra_context):,} chars of additional spec context from uploaded files.")

    log(f"Analysing {len(pairs)} issue-PR pairs...")

    # Pre-flight: verify LLM is reachable before doing any work
    try:
        await _call_llm(provider, model, api_key, "Reply OK.", "ping")
    except Exception as e:
        err = str(e)
        # Only hard-fail on auth errors (wrong credentials), not usage limits
        if any(x in err.lower() for x in ["401", "authentication", "expired", "login", "invalid"]):
            hint = "Run `claude` in your terminal and log in again." if provider == "claude-code" else "Check your API key."
            raise RuntimeError(f"AI model authentication failed: {err[:250]} — {hint}")
        # Usage limits / overloaded: log and continue, batches will handle retries
        log(f"Warning: pre-flight check failed ({err[:150]}), continuing anyway...")

    eligible = []
    for pair in pairs:
        issue = pair["issue"]
        if _is_noise(issue):
            log(f"  #{issue['number']} '{issue['title'][:40]}' — skipped (noise)")
            continue
        grade = _grade_intent(issue)
        if grade == "thin":
            log(f"  #{issue['number']} '{issue['title'][:40]}' — skipped (thin)")
            continue
        log(f"  #{issue['number']} '{issue['title'][:40]}' — grade: {grade}")
        eligible.append((pair, grade))

    all_findings = []
    analyzed_count = len(eligible)
    _tokens: dict = {"input": 0, "output": 0, "estimated": False}

    if provider == "claude-code":
        # Batch mode: 5 pairs per LLM call — reduces subprocess spawns from N to N/5
        BATCH_SIZE = 2
        batches = [eligible[i:i+BATCH_SIZE] for i in range(0, len(eligible), BATCH_SIZE)]
        log(f"Running {len(batches)} batch call(s) ({BATCH_SIZE} pairs each)...")
        sem = asyncio.Semaphore(3)

        async def _run_batch(batch):
            async with sem:
                result = await _analyze_batch(batch, provider, model, api_key, log,
                                            owner=owner, repo=repo, all_issues=all_issues,
                                            extra_context=repo_data.get("extra_context"),
                                            _tokens=_tokens)
                total = _tokens["input"] + _tokens["output"]
                log(None, tokens=total, estimated=_tokens["estimated"])
                return result

        batch_results = await asyncio.gather(*[_run_batch(b) for b in batches])
        failed_count   = sum(1 for r in batch_results if r is None)
        succeeded_count = len(batches) - failed_count

        if failed_count > 0 and succeeded_count == 0:
            # Every batch failed — almost certainly a rate-limit or auth issue.
            # Raising here stops a misleading "No drift detected" report.
            raise RuntimeError(
                f"Analysis failed: all {failed_count} LLM batch call(s) returned errors. "
                "If you are using Claude Code, you may have hit your usage limit — "
                "check the reset time shown in the batch errors above, then re-run."
            )
        if failed_count > 0:
            log(f"  Warning: {failed_count}/{len(batches)} batch(es) failed — results may be incomplete")

        for findings in batch_results:
            if findings is not None:
                all_findings.extend(findings)
    else:
        # Per-pair mode for API providers (fast, no subprocess overhead)
        concurrency = 6
        sem = asyncio.Semaphore(concurrency)

        async def _run_pair(pair, grade):
            async with sem:
                try:
                    findings = await _analyze_pair(
                        pair, provider, model, api_key, log,
                        owner=owner, repo=repo, all_issues=all_issues,
                        extra_context=repo_data.get("extra_context"),
                        _tokens=_tokens,
                    )
                    total = _tokens["input"] + _tokens["output"]
                    log(None, tokens=total, estimated=_tokens["estimated"])
                    return findings or []
                except Exception as e:
                    log(f"  #{pair['issue']['number']} failed: {str(e)[:200]}")
                    return []

        results = await asyncio.gather(*[_run_pair(p, g) for p, g in eligible])
        for findings in results:
            all_findings.extend(findings)

    all_findings = _rank_findings(all_findings)

    # Step 5b: Drop drift findings whose evidence file is not in the PR diff
    before = len(all_findings)
    all_findings = _drop_unverifiable_evidence(all_findings, pairs)
    dropped = before - len(all_findings)
    if dropped:
        log(f"  Dropped {dropped} finding(s) with unverifiable evidence")

    # Step 5c: LLM judge pass — only for API providers (fast enough to be worth it)
    if all_findings and provider != "claude-code":
        log(f"Verifying {len(all_findings)} findings...")
        all_findings = await _verify_findings(all_findings, pairs, provider, model, api_key, log)

    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for f in all_findings:
        s = f.get("severity") or "low"
        severity_counts[s] = severity_counts.get(s, 0) + 1

    total_rework_days = repo_data.get("total_rework_hours", 0.0)

    log(f"Analysis complete — {len(all_findings)} findings across {analyzed_count} pairs")

    # Step 6: Generate executive summary from top findings
    exec_summary = ""

    return {
        "findings": all_findings,
        "total_rework_hours": total_rework_days,
        "exec_summary": exec_summary,
        "tokens": _tokens,
        "summary": {
            "total_findings": len(all_findings),
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "drift": sum(1 for f in all_findings if f["status"] == "drift"),
            "gap": sum(1 for f in all_findings if f["status"] == "gap"),
            "pairs_analyzed": analyzed_count,
            "issues_unlinked": len(repo_data.get("unlinked_issues", [])),
            "issues_closed": stats.get("closed_issues", 0),
            "oversized_prs": len(repo_data.get("oversized_prs", [])),
        },
    }
