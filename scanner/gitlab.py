"""GitLab data fetcher — closed issues + linked MRs + diffs + notes."""

import re
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse, quote
import requests

from scanner.jira import extract_jira_ids


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers(token: str | None) -> dict:
    h: dict = {}
    if token:
        # OAuth tokens use Bearer auth, Personal Access Tokens use PRIVATE-TOKEN
        # Try Bearer first (for OAuth), fall back to PRIVATE-TOKEN if it looks like a PAT
        if token.startswith("glpat-"):
            h["PRIVATE-TOKEN"] = token
        else:
            h["Authorization"] = f"Bearer {token}"
    return h


def _parse_project_info(repo_url: str) -> tuple[str, str, str, str]:
    """
    Parse a GitLab repo URL into components.

    Returns (api_base, owner, repo, url_encoded_namespace).
    Works for gitlab.com and self-hosted instances, including nested namespaces.

    Examples:
        https://gitlab.com/owner/repo
            → ('https://gitlab.com/api/v4', 'owner', 'repo', 'owner%2Frepo')
        https://gitlab.company.com/group/subgroup/project
            → ('https://gitlab.company.com/api/v4', 'group', 'project', 'group%2Fsubgroup%2Fproject')
    """
    p = urlparse(repo_url.rstrip("/"))
    api_base = f"{p.scheme}://{p.netloc}/api/v4"
    namespace = p.path.strip("/")
    encoded = quote(namespace, safe="")
    parts = namespace.split("/")
    owner = parts[0] if parts else namespace
    repo = parts[-1] if len(parts) > 1 else namespace
    return api_base, owner, repo, encoded


def _get(url: str, token: str | None, params: dict | None = None) -> dict | list:
    headers = _headers(token)
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    
    # Debug logging for auth issues
    if resp.status_code in (401, 403):
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"GitLab API error {resp.status_code} for {url}")
        logger.error(f"Headers used: {list(headers.keys())}")
        logger.error(f"Response: {resp.text[:200]}")
    
    if resp.status_code == 401:
        if not token:
            raise RuntimeError(
                "GitLab authentication required. This project requires a GitLab token. "
                "Create a Personal Access Token at Settings → Access Tokens with 'read_api' scope."
            )
        raise RuntimeError(
            "GitLab token is invalid or expired. "
            "Create a new token at Settings → Access Tokens with 'read_api' scope."
        )
    if resp.status_code == 403:
        raise RuntimeError(
            "GitLab access denied. Your token may lack 'read_api' scope, "
            "or you may not have access to this project."
        )
    if resp.status_code == 404:
        msg = (
            "GitLab project not found. If this is a private project, your token needs 'read_api' scope. "
            "Create a token at gitlab.com/-/user_settings/personal_access_tokens."
            if token else
            "GitLab project not found. Check the URL is correct, or add a GitLab token for private projects."
        )
        raise RuntimeError(msg)
    if resp.status_code == 429:
        raise RuntimeError(
            "GitLab API rate limit exceeded. Please wait a moment and try again, "
            "or add a GitLab token to increase your rate limit."
        )
    resp.raise_for_status()
    return resp.json()


def _get_all_pages(url: str, token: str | None, params: dict | None = None, max_pages: int = 5) -> list:
    params = {**(params or {}), "per_page": 100}
    results = []
    for page in range(1, max_pages + 1):
        params["page"] = page
        data = _get(url, token, params)
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
    return results


def _iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def _is_bot(mr: dict) -> bool:
    username = (mr.get("author") or {}).get("username", "")
    return any(bot in username.lower() for bot in ["dependabot", "renovate", "greenkeeper", "snyk-bot"])


# ── MR linking ────────────────────────────────────────────────────────────────

def _find_linked_mr_iids(
    issue_iid: int,
    issue_body: str,
    token: str | None,
    api_base: str,
    project_id: str,
) -> list[int]:
    """
    Find MR iids linked to a GitLab issue.

    Two strategies:
    1. GitLab's dedicated endpoint: GET /issues/:iid/closed_by (most reliable)
    2. Regex fallback: scan issue body for "closes !N" / "fixes !N" references
    """
    linked: set[int] = set()

    # 1. Official "closed by" endpoint
    try:
        mrs = _get(f"{api_base}/projects/{project_id}/issues/{issue_iid}/closed_by", token)
        for mr in (mrs or []):
            if mr.get("iid"):
                linked.add(int(mr["iid"]))
    except Exception:
        pass

    # 2. Regex scan — GitLab uses !N syntax for MRs in body text
    if issue_body:
        for m in re.finditer(r"(?:closes?|fixes?|resolves?|implements?)\s+!(\d+)", issue_body, re.I):
            linked.add(int(m.group(1)))

    return list(linked)


# ── Diff helpers ──────────────────────────────────────────────────────────────

def _score_file_relevance(filename: str, patch: str, keywords: set[str]) -> int:
    text = (filename + " " + patch).lower()
    return sum(1 for kw in keywords if kw in text)


def _build_diff_text(changes: list, issue_title: str, issue_body: str) -> str:
    """Sort changed files by relevance to the issue, truncate to 6000 chars."""
    _STOP = {"with", "from", "that", "this", "when", "have", "will", "also", "more",
             "into", "file", "code", "does", "after", "before", "should", "would"}
    keywords = {w for w in re.findall(r"[a-z]{4,}", (issue_title + " " + issue_body).lower()) if w not in _STOP}

    scored = []
    for f in changes[:20]:
        fname = f.get("new_path") or f.get("old_path", "")
        patch = f.get("diff", "")
        if not patch:
            continue
        scored.append((_score_file_relevance(fname, patch, keywords), fname, patch))

    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(f"--- {fn}\n{p[:1500]}" for _, fn, p in scored)[:6000]


# ── MR detail fetcher ─────────────────────────────────────────────────────────

def _fetch_mr_details(
    mr_iid: int,
    token: str | None,
    api_base: str,
    project_id: str,
    max_changes: int | None = 2000,
    issue_title: str = "",
    issue_body: str = "",
) -> dict | None:
    """Fetch a single MR with diff, review notes, and commits."""
    try:
        mr = _get(f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}", token)
    except Exception:
        return None

    if _is_bot(mr):
        return None

    # Diff via /changes endpoint
    try:
        changes_resp = _get(
            f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/changes",
            token,
        )
        changes = changes_resp.get("changes", []) if isinstance(changes_resp, dict) else []
    except Exception:
        changes = []

    # Use diff line count as a size proxy (GitHub uses additions+deletions)
    total_changes = sum(len(c.get("diff", "").splitlines()) for c in changes)
    if max_changes is not None and total_changes > max_changes:
        return {
            "number": mr_iid,
            "title": mr.get("title", ""),
            "url": mr.get("web_url", ""),
            "author": (mr.get("author") or {}).get("username", ""),
            "state": mr.get("state", ""),
            "merged_at": mr.get("merged_at"),
            "body": (mr.get("description") or ""),
            "oversized": True,
            "total_changes": total_changes,
            "files": [],
            "diff_text": "",
            "reviews": [],
            "commits": [],
        }

    diff_text = _build_diff_text(changes, issue_title, issue_body)

    # Notes (review comments) — filter out system-generated notes
    reviews: list[dict] = []
    changes_requested = 0
    try:
        notes_raw = _get_all_pages(
            f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/notes",
            token,
            {"sort": "asc", "order_by": "created_at"},
            max_pages=1,
        )
        human_notes = [n for n in notes_raw if not n.get("system", False) and n.get("body")]
        reviews = [
            {
                "state": "COMMENT",
                "body": (n.get("body") or "")[:500],
                "submitted_at": n.get("created_at", ""),
            }
            for n in human_notes[:10]
        ]
        # Use any human review activity as signal (proxy for "changes_requested")
        changes_requested = len(human_notes)
    except Exception:
        pass

    # Commits (for rework hours estimation)
    commits: list[dict] = []
    try:
        commits_raw = _get_all_pages(
            f"{api_base}/projects/{project_id}/merge_requests/{mr_iid}/commits",
            token,
            max_pages=1,
        )
        commits = [
            {
                "sha": (c.get("id") or "")[:7],
                "message": (c.get("message") or "").split("\n")[0][:100],
                "timestamp": c.get("authored_date") or c.get("created_at", ""),
            }
            for c in commits_raw
        ]
    except Exception:
        pass

    mr_title = mr.get("title", "")
    mr_body = (mr.get("description") or "")[:800]
    jira_ids = extract_jira_ids(mr_title + " " + mr_body)

    return {
        "number": mr_iid,
        "title": mr_title,
        "url": mr.get("web_url", ""),
        "author": (mr.get("author") or {}).get("username", ""),
        "state": mr.get("state", ""),
        "merged_at": mr.get("merged_at"),
        "merge_commit_sha": mr.get("merge_commit_sha") or "",
        "body": mr_body,
        "oversized": False,
        "total_changes": total_changes,
        "files": [c.get("new_path") or c.get("old_path", "") for c in changes],
        "diff_text": diff_text,
        "reviews": reviews,
        "changes_requested": changes_requested,
        "commits": commits,
        "jira_ids": jira_ids,
    }


# ── Rework time estimation ────────────────────────────────────────────────────

def _estimate_rework_hours(commits: list[dict]) -> float:
    """git-hours algorithm: group commits into sessions (2hr gap = new session)."""
    if not commits:
        return 0.0

    timestamps = []
    for c in commits:
        try:
            timestamps.append(_iso(c["timestamp"]))
        except Exception:
            pass

    if not timestamps:
        return 0.0

    timestamps.sort()
    SESSION_GAP = timedelta(hours=2)
    FIRST_COMMIT_OVERHEAD = timedelta(hours=2)

    total = FIRST_COMMIT_OVERHEAD
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if gap <= SESSION_GAP:
            total += gap
        else:
            total += FIRST_COMMIT_OVERHEAD

    return round(total.total_seconds() / 3600, 1)


# ── Main fetch ────────────────────────────────────────────────────────────────

async def fetch_repo_data(
    repo_url: str,
    token: str | None,
    period_days: int,
    log: Callable,
    max_pr_changes: int | None = 2000,
) -> dict:
    """
    Fetch GitLab project data: closed issues + linked MRs + diffs.
    Returns the same structure as github.fetch_repo_data so the rest of the
    pipeline (analyzer, report, Jira/Linear/Slack enrichment) works unchanged.
    """
    api_base, owner, repo, project_id = _parse_project_info(repo_url)
    p = urlparse(repo_url)
    host = p.netloc

    loop = asyncio.get_event_loop()
    since = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()

    log(f"Connecting to GitLab ({host})...", step=1)
    if token:
        # Debug: check token format
        is_pat = token.startswith("glpat-")
        token_preview = token[:10] if len(token) >= 10 else token
        log(f"Using GitLab token (type: {'PAT' if is_pat else 'OAuth'}, preview: {token_preview}...)")
    else:
        log("⚠ No GitLab token provided — most operations will fail. Add a token for private repos.")

    # Project info
    log(f"Fetching project info for {owner}/{repo}...")
    project_info = await loop.run_in_executor(
        None, lambda: _get(f"{api_base}/projects/{project_id}", token)
    )

    # Closed issues within the time window
    log("Fetching closed issues...")
    max_pages = 5 if token else 1
    all_issues_raw = await loop.run_in_executor(
        None,
        lambda: _get_all_pages(
            f"{api_base}/projects/{project_id}/issues",
            token,
            {
                "state": "closed",
                "updated_after": since,
                "sort": "desc",
                "order_by": "updated_at",
            },
            max_pages=max_pages,
        ),
    )
    log(f"Found {len(all_issues_raw)} closed issues in the last {period_days} days")

    # Build issue → MR pairs concurrently
    log("Linking issues to merge requests...")

    pairs: list[dict] = []
    unlinked_issues: list[dict] = []
    oversized_prs: list[dict] = []
    total_rework_hours = 0.0

    sem = asyncio.Semaphore(6)  # max concurrent GitLab requests

    async def _process_issue(issue: dict) -> dict | None:
        async with sem:
            issue_iid: int = issue["iid"]
            issue_body: str = issue.get("description") or ""

            mr_iids = await loop.run_in_executor(
                None,
                lambda iid=issue_iid, b=issue_body: _find_linked_mr_iids(
                    iid, b, token, api_base, project_id
                ),
            )

            if not mr_iids:
                return {
                    "unlinked": True,
                    "number": issue_iid,
                    "title": issue["title"],
                    "url": issue.get("web_url", ""),
                }

            mr_details_list = await asyncio.gather(*[
                loop.run_in_executor(
                    None,
                    lambda iid=mr_iid, t=issue["title"], b=issue_body: _fetch_mr_details(
                        iid, token, api_base, project_id, max_pr_changes, t, b
                    ),
                )
                for mr_iid in mr_iids[:3]
            ])

            prs: list[dict] = []
            oversized: list[dict] = []
            for mr_detail in mr_details_list:
                if mr_detail is None:
                    continue
                if mr_detail.get("oversized"):
                    oversized.append(mr_detail)
                    continue
                if mr_detail.get("changes_requested", 0) >= 1:
                    mr_detail["rework_hours"] = _estimate_rework_hours(mr_detail.get("commits", []))
                else:
                    mr_detail["rework_hours"] = 0.0
                prs.append(mr_detail)

            if not prs:
                return {
                    "unlinked": True,
                    "number": issue_iid,
                    "title": issue["title"],
                    "url": issue.get("web_url", ""),
                    "oversized": oversized,
                }

            # GitLab labels are strings directly (not objects)
            raw_labels = issue.get("labels", [])
            labels = [l if isinstance(l, str) else (l.get("name", "") if isinstance(l, dict) else str(l)) for l in raw_labels]

            # Use milestone as the "project" field (closest GitLab equivalent)
            milestone_title = (issue.get("milestone") or {}).get("title", "")

            return {
                "pair": {
                    "issue": {
                        "number": issue_iid,
                        "title": issue["title"],
                        "url": issue.get("web_url", ""),
                        "body": issue_body[:2000],
                        "labels": labels,
                        "comments": issue.get("user_notes_count", 0),
                        "closed_at": issue.get("closed_at"),
                        "project": milestone_title,
                        "project_fields": {},
                    },
                    "prs": prs,
                },
                "oversized": oversized,
                "rework_hours": sum(pr.get("rework_hours", 0) for pr in prs),
            }

    results = await asyncio.gather(*[_process_issue(issue) for issue in all_issues_raw[:40]])

    for result in results:
        if result is None:
            continue
        if result.get("unlinked"):
            unlinked_issues.append({
                "number": result["number"],
                "title": result["title"],
                "url": result["url"],
            })
            for op in result.get("oversized", []):
                oversized_prs.append({
                    "number": op["number"],
                    "title": op["title"],
                    "url": op["url"],
                    "total_changes": op["total_changes"],
                })
        else:
            pairs.append(result["pair"])
            total_rework_hours += result["rework_hours"]
            for op in result.get("oversized", []):
                oversized_prs.append({
                    "number": op["number"],
                    "title": op["title"],
                    "url": op["url"],
                    "total_changes": op["total_changes"],
                })

    log(
        f"Built {len(pairs)} issue-MR pairs — "
        f"{len(unlinked_issues)} unlinked, {len(oversized_prs)} oversized MRs skipped"
    )

    all_issues_slim = [
        {
            "number": i["iid"],
            "title": i["title"],
            "body": (i.get("description") or "")[:600],
            "created_at": i.get("created_at", ""),
            "html_url": i.get("web_url", ""),
        }
        for i in all_issues_raw
    ]

    return {
        "owner": owner,
        "repo": repo,
        "repo_url": repo_url,
        "description": project_info.get("description") or "",
        "period_days": period_days,
        "pairs": pairs,
        "unlinked_issues": unlinked_issues,
        "oversized_prs": oversized_prs,
        "total_rework_hours": round(total_rework_hours, 1),
        "all_issues": all_issues_slim,
        "stats": {
            "closed_issues": len(all_issues_raw),
            "pairs_built": len(pairs),
            "unlinked": len(unlinked_issues),
            "oversized": len(oversized_prs),
        },
    }
