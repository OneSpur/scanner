"""GitHub data fetcher — closed issues + linked PRs + diffs + reviews."""

import re
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable
import requests

from scanner.jira import extract_jira_ids


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_owner_repo(repo_url: str) -> tuple[str, str]:
    parts = repo_url.rstrip("/").split("github.com/")[-1].split("/")
    return parts[0], parts[1]


def _get(url: str, token: str | None, params: dict = None) -> dict | list:
    resp = requests.get(url, headers=_headers(token), params=params, timeout=20)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        remaining = resp.headers.get("X-RateLimit-Remaining", "0")
        raise RuntimeError(
            f"GitHub API rate limit exceeded ({remaining} requests left). "
            "Add a GitHub token — create one at github.com/settings/tokens (public_repo scope)."
        )
    if resp.status_code == 401:
        raise RuntimeError("GitHub token is invalid or expired. Please check your token and try again.")
    if resp.status_code == 404:
        if token:
            raise RuntimeError(
                "Repository not found. If this is a private repo, your token needs the full 'repo' scope "
                "(not just 'public_repo'). Create a new token at github.com/settings/tokens with 'repo' checked."
            )
        raise RuntimeError("Repository not found. Check the URL is correct, or add a GitHub token for private repos.")
    resp.raise_for_status()
    return resp.json()


def _get_all_pages(url: str, token: str | None, params: dict = None, max_pages: int = 5) -> list:
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


def _check_rate_limit(token: str | None) -> int:
    try:
        resp = requests.get("https://api.github.com/rate_limit", headers=_headers(token), timeout=8)
        return resp.json().get("rate", {}).get("remaining", 60)
    except Exception:
        return 60


def _iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def _is_bot(pr: dict) -> bool:
    login = (pr.get("user") or {}).get("login", "")
    return any(bot in login.lower() for bot in ["dependabot", "renovate", "greenkeeper", "snyk-bot"])


# ── PR linking ────────────────────────────────────────────────────────────────

def _find_linked_pr_numbers(issue_number: int, issue_body: str, token: str | None, base: str) -> list[int]:
    """Find PR numbers linked to an issue via timeline events + body regex."""
    linked = set()

    # 1. Timeline events: "cross-referenced" and "connected"
    try:
        events = _get_all_pages(f"{base}/issues/{issue_number}/timeline", token,
                                 {"per_page": 100}, max_pages=2)
        for ev in events:
            if ev.get("event") in ("cross-referenced", "connected"):
                src = ev.get("source") or {}
                issue_ref = src.get("issue") or {}
                pr_url = issue_ref.get("pull_request", {}).get("url", "")
                if pr_url:
                    try:
                        pr_num = int(pr_url.rstrip("/").split("/")[-1])
                        linked.add(pr_num)
                    except ValueError:
                        pass
    except Exception:
        pass

    # 2. Regex scan issue body for informal references like "closes #123"
    if issue_body:
        for m in re.finditer(r"(?:closes?|fixes?|resolves?|implements?|see)\s+#(\d+)", issue_body, re.I):
            linked.add(int(m.group(1)))

    return list(linked)


def _score_file_relevance(filename: str, patch: str, keywords: set[str]) -> int:
    text = (filename + " " + patch).lower()
    return sum(1 for kw in keywords if kw in text)


def _build_diff_text(files: list, issue_title: str, issue_body: str) -> str:
    """Sort files by relevance to the issue before truncating to 6000 chars."""
    _STOP = {"with", "from", "that", "this", "when", "have", "will", "also", "more",
             "into", "file", "code", "does", "after", "before", "should", "would"}
    keywords = {w for w in re.findall(r"[a-z]{4,}", (issue_title + " " + issue_body).lower()) if w not in _STOP}
    scored = []
    for f in files[:20]:
        fname = f.get("filename", "")
        patch = f.get("patch", "")
        if not patch:
            continue
        scored.append((_score_file_relevance(fname, patch, keywords), fname, patch))
    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(f"--- {fn}\n{p[:1500]}" for _, fn, p in scored)[:6000]


def _fetch_pr_details(pr_number: int, token: str | None, base: str, merged_at: str | None, max_changes: int | None = 2000, issue_title: str = "", issue_body: str = "") -> dict | None:
    """Fetch a single PR with its diff and reviews."""
    try:
        pr = _get(f"{base}/pulls/{pr_number}", token)
    except Exception:
        return None

    if _is_bot(pr):
        return None

    try:
        files = _get_all_pages(f"{base}/pulls/{pr_number}/files", token, max_pages=2)
    except Exception:
        files = []

    total_changes = sum(f.get("changes", 0) for f in files)
    if max_changes is not None and total_changes > max_changes:
        return {
            "number": pr_number,
            "title": pr.get("title", ""),
            "url": pr.get("html_url", ""),
            "author": (pr.get("user") or {}).get("login", ""),
            "state": pr.get("state", ""),
            "merged_at": pr.get("merged_at"),
            "body": pr.get("body") or "",
            "oversized": True,
            "total_changes": total_changes,
            "files": [],
            "diff_text": "",
            "reviews": [],
            "commits": [],
        }

    diff_text = _build_diff_text(files, issue_title, issue_body)

    try:
        reviews_raw = _get_all_pages(f"{base}/pulls/{pr_number}/reviews", token, max_pages=1)
        reviews = [
            {
                "state": r.get("state", ""),
                "body": (r.get("body") or "")[:500],
                "submitted_at": r.get("submitted_at", ""),
            }
            for r in reviews_raw
            if r.get("body") or r.get("state") == "CHANGES_REQUESTED"
        ]
        changes_requested = sum(1 for r in reviews_raw if r.get("state") == "CHANGES_REQUESTED")
    except Exception:
        reviews = []
        changes_requested = 0

    # Commits (for rework time estimation)
    try:
        commits_raw = _get_all_pages(f"{base}/pulls/{pr_number}/commits", token, max_pages=1)
        commits = [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0][:100],
                "timestamp": c["commit"]["committer"]["date"],
            }
            for c in commits_raw
        ]
    except Exception:
        commits = []

    pr_title = pr.get("title", "")
    pr_body = (pr.get("body") or "")[:800]
    jira_ids = extract_jira_ids(pr_title + " " + pr_body)

    return {
        "number": pr_number,
        "title": pr_title,
        "url": pr.get("html_url", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "state": pr.get("state", ""),
        "merged_at": pr.get("merged_at"),
        "merge_commit_sha": pr.get("merge_commit_sha", ""),
        "body": pr_body,
        "oversized": False,
        "total_changes": total_changes,
        "files": [f.get("filename", "") for f in files],
        "diff_text": diff_text,
        "reviews": reviews,
        "changes_requested": changes_requested,
        "commits": commits,
        "jira_ids": jira_ids,
    }


# ── GitHub Projects v2 ────────────────────────────────────────────────────────

_PROJECTS_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    projectsV2(first: 10) {
      nodes {
        id
        title
        number
        items(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            content {
              ... on Issue {
                number
              }
            }
            fieldValues(first: 20) {
              nodes {
                ... on ProjectV2ItemFieldSingleSelectValue {
                  name
                  field { ... on ProjectV2SingleSelectField { name } }
                }
                ... on ProjectV2ItemFieldIterationValue {
                  title
                  startDate
                  duration
                  field { ... on ProjectV2IterationField { name } }
                }
                ... on ProjectV2ItemFieldTextValue {
                  text
                  field { ... on ProjectV2Field { name } }
                }
                ... on ProjectV2ItemFieldNumberValue {
                  number
                  field { ... on ProjectV2Field { name } }
                }
                ... on ProjectV2ItemFieldDateValue {
                  date
                  field { ... on ProjectV2Field { name } }
                }
                ... on ProjectV2ItemFieldUserValue {
                  users(first: 3) { nodes { login } }
                  field { ... on ProjectV2Field { name } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _graphql(query: str, variables: dict, token: str) -> dict:
    resp = requests.post(
        "https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_project_context(owner: str, repo: str, token: str | None) -> dict[int, dict]:
    """
    Returns {issue_number: {project_fields}} for all issues found in any project.
    Returns empty dict silently if no token, no read:project scope, or no projects.
    """
    if not token:
        return {}

    try:
        data = _graphql(_PROJECTS_QUERY, {"owner": owner, "repo": repo}, token)
    except Exception:
        return {}

    # Graceful handling of missing read:project scope
    if "errors" in data:
        for err in data.get("errors", []):
            if "INSUFFICIENT_SCOPES" in err.get("type", "") or "read:project" in err.get("message", ""):
                return {}
        return {}

    projects = (data.get("data") or {}).get("repository", {}).get("projectsV2", {}).get("nodes", [])
    if not projects:
        return {}

    context: dict[int, dict] = {}

    for project in projects:
        project_title = project.get("title", "")
        for item in project.get("items", {}).get("nodes", []):
            content = item.get("content") or {}
            issue_number = content.get("number")
            if not issue_number:
                continue

            fields: dict[str, str] = {}
            for fv in item.get("fieldValues", {}).get("nodes", []):
                field_info = fv.get("field") or {}
                field_name = field_info.get("name", "")
                if not field_name:
                    continue

                if "name" in fv and "field" in fv:  # SingleSelectValue
                    fields[field_name] = fv["name"]
                elif "title" in fv and "startDate" in fv:  # IterationValue
                    fields[field_name] = fv["title"]
                    if fv.get("startDate"):
                        fields[f"{field_name} start"] = fv["startDate"]
                elif "text" in fv:
                    fields[field_name] = fv["text"]
                elif "number" in fv:
                    fields[field_name] = str(fv["number"])
                elif "date" in fv:
                    fields[field_name] = fv["date"]
                elif "users" in fv:
                    logins = [u["login"] for u in fv.get("users", {}).get("nodes", [])]
                    if logins:
                        fields[field_name] = ", ".join(logins)

            if fields or project_title:
                if issue_number not in context:
                    context[issue_number] = {"project": project_title, "fields": {}}
                context[issue_number]["fields"].update(fields)

    return context


# ── Rework time estimation ─────────────────────────────────────────────────────

def _estimate_rework_hours(commits: list[dict]) -> float:
    """
    git-hours algorithm: group commits into sessions (2hr gap = new session).
    Each session's first commit gets +2hr overhead credit.
    Returns estimated engineering hours.
    """
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

    total = FIRST_COMMIT_OVERHEAD  # first commit always gets overhead
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        if gap <= SESSION_GAP:
            total += gap
        else:
            total += FIRST_COMMIT_OVERHEAD  # new session

    return round(total.total_seconds() / 3600, 1)


# ── Main fetch ────────────────────────────────────────────────────────────────

async def fetch_repo_data(
    repo_url: str,
    token: str | None,
    period_days: int,
    log: Callable,
    max_pr_changes: int | None = 2000,
) -> dict:
    owner, repo = _parse_owner_repo(repo_url)
    base = f"https://api.github.com/repos/{owner}/{repo}"
    loop = asyncio.get_event_loop()
    since = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()

    # Rate limit check
    remaining = await loop.run_in_executor(None, lambda: _check_rate_limit(token))
    if remaining < 20:
        raise RuntimeError(
            f"GitHub API rate limit almost exhausted ({remaining} requests left). "
            "Add a GitHub token to get 5,000 requests/hour."
        )
    log(f"{'Authenticated' if token else 'Unauthenticated'} — {remaining} API requests remaining")

    max_pages = 5 if token else 1

    # Repo info
    log(f"Fetching repo info for {owner}/{repo}...")
    repo_info = await loop.run_in_executor(None, lambda: _get(base, token))

    # Closed issues (completed, within window)
    log("Fetching closed issues...")
    all_issues_raw = await loop.run_in_executor(
        None,
        lambda: _get_all_pages(f"{base}/issues", token, {
            "state": "closed", "since": since, "sort": "updated", "direction": "desc"
        }, max_pages=max_pages)
    )
    # Exclude PRs (GitHub returns PRs in issues endpoint)
    closed_issues = [
        i for i in all_issues_raw
        if "pull_request" not in i
        and i.get("state_reason") in ("completed", None)  # include completed + no-reason
    ]
    log(f"Found {len(closed_issues)} closed issues in the last {period_days} days")

    # Project context (optional — requires read:project scope)
    log("Fetching project context...")
    project_context = await loop.run_in_executor(None, lambda: _fetch_project_context(owner, repo, token))
    if project_context:
        log(f"Found project data for {len(project_context)} issues")

    # Build pairs: issue + linked PRs
    log("Linking issues to pull requests...")
    pairs = []
    unlinked_issues = []
    oversized_prs = []
    total_rework_days = 0.0

    sem = asyncio.Semaphore(8)  # max 8 concurrent GitHub requests

    async def _process_issue(issue: dict) -> dict | None:
        """Returns a pair dict, or None if unlinked/invalid."""
        async with sem:
            issue_number = issue["number"]
            issue_body = issue.get("body") or ""

            pr_numbers = await loop.run_in_executor(
                None,
                lambda n=issue_number, b=issue_body: _find_linked_pr_numbers(n, b, token, base)
            )

            if not pr_numbers:
                return {"unlinked": True, "number": issue_number, "title": issue["title"], "url": issue["html_url"]}

            pr_details = await asyncio.gather(*[
                loop.run_in_executor(
                    None,
                    lambda n=pr_num, m=issue.get("closed_at"), t=issue["title"], b=issue_body: _fetch_pr_details(n, token, base, m, max_pr_changes, t, b)
                )
                for pr_num in pr_numbers[:3]
            ])

            prs = []
            oversized = []
            for pr_detail in pr_details:
                if pr_detail is None:
                    continue
                if pr_detail.get("oversized"):
                    oversized.append(pr_detail)
                    continue
                if pr_detail.get("changes_requested", 0) >= 1:
                    pr_detail["rework_hours"] = _estimate_rework_hours(pr_detail.get("commits", []))
                else:
                    pr_detail["rework_hours"] = 0.0
                prs.append(pr_detail)

            if not prs:
                return {"unlinked": True, "number": issue_number, "title": issue["title"], "url": issue["html_url"],
                        "oversized": oversized}

            proj = project_context.get(issue_number, {})
            return {
                "pair": {
                    "issue": {
                        "number": issue_number,
                        "title": issue["title"],
                        "url": issue["html_url"],
                        "body": issue_body[:2000],
                        "labels": [l["name"] for l in issue.get("labels", [])],
                        "comments": issue.get("comments", 0),
                        "closed_at": issue.get("closed_at"),
                        "project": proj.get("project", ""),
                        "project_fields": proj.get("fields", {}),
                        "author": (issue.get("user") or {}).get("login", ""),
                        "author_url": "https://github.com/" + (issue.get("user") or {}).get("login", "") if (issue.get("user") or {}).get("login") else "",
                    },
                    "prs": prs,
                },
                "oversized": oversized,
                "rework_hours": sum(pr.get("rework_hours", 0) for pr in prs),
            }

    results = await asyncio.gather(*[_process_issue(issue) for issue in closed_issues[:40]])

    for result in results:
        if result is None:
            continue
        if result.get("unlinked"):
            unlinked_issues.append({"number": result["number"], "title": result["title"], "url": result["url"]})
            for op in result.get("oversized", []):
                oversized_prs.append({"number": op["number"], "title": op["title"], "url": op["url"], "total_changes": op["total_changes"]})
        else:
            pairs.append(result["pair"])
            total_rework_days += result["rework_hours"]
            for op in result.get("oversized", []):
                oversized_prs.append({"number": op["number"], "title": op["title"], "url": op["url"], "total_changes": op["total_changes"]})

    log(f"Built {len(pairs)} issue-PR pairs — {len(unlinked_issues)} unlinked, {len(oversized_prs)} oversized PRs skipped")

    # ── Rescue pass: scan merged PRs for issue references ────────────────────
    # Catches repos where PRs don't use closing keywords but do reference issues
    if unlinked_issues and len(pairs) < 20:
        try:
            unlinked_by_num = {u["number"]: u for u in unlinked_issues}
            closed_prs_raw = await loop.run_in_executor(
                None,
                lambda: _get_all_pages(f"{base}/pulls", token,
                                       {"state": "closed", "sort": "updated",
                                        "direction": "desc", "per_page": 50}, max_pages=2)
            )
            merged_prs = [p for p in closed_prs_raw if p.get("merged_at")]

            rescued = 0
            for pr_raw in merged_prs[:30]:
                pr_body  = (pr_raw.get("body") or "")
                pr_title = (pr_raw.get("title") or "")
                # Find any #N reference in body or title
                refs = {int(m) for m in re.findall(r"#(\d+)", pr_body + " " + pr_title)}
                matched_issue_nums = refs & set(unlinked_by_num.keys())
                if not matched_issue_nums:
                    continue
                pr_num = pr_raw["number"]
                pr_detail = await loop.run_in_executor(
                    None,
                    lambda n=pr_num, m=pr_raw.get("merged_at"): _fetch_pr_details(
                        n, token, base, m, max_pr_changes, "", ""
                    )
                )
                if not pr_detail or pr_detail.get("oversized"):
                    continue
                pr_detail["rework_hours"] = 0.0

                for issue_num in matched_issue_nums:
                    # Skip if already rescued
                    if issue_num not in unlinked_by_num:
                        continue
                    orig_issue = next((i for i in closed_issues if i["number"] == issue_num), None)
                    if not orig_issue:
                        continue
                    proj = project_context.get(issue_num, {})
                    issue_body = (orig_issue.get("body") or "")
                    pairs.append({
                        "issue": {
                            "number": issue_num,
                            "title": orig_issue["title"],
                            "url": orig_issue["html_url"],
                            "body": issue_body[:2000],
                            "labels": [l["name"] for l in orig_issue.get("labels", [])],
                            "comments": orig_issue.get("comments", 0),
                            "closed_at": orig_issue.get("closed_at"),
                            "project": proj.get("project", ""),
                            "project_fields": proj.get("fields", {}),
                            "author": (orig_issue.get("user") or {}).get("login", ""),
                            "author_url": "https://github.com/" + (orig_issue.get("user") or {}).get("login", "") if (orig_issue.get("user") or {}).get("login") else "",
                        },
                        "prs": [pr_detail],
                    })
                    del unlinked_by_num[issue_num]
                    rescued += 1

            if rescued:
                unlinked_issues = list(unlinked_by_num.values())
                log(f"Rescue pass: matched {rescued} additional issue-PR pairs via PR body scan")
        except Exception as e:
            log(f"Rescue pass failed (non-fatal): {e}")

    # Slim issue list for "when found" detection in analyzer
    all_issues_slim = [
        {
            "number": i["number"],
            "title": i["title"],
            "body": (i.get("body") or "")[:600],
            "created_at": i.get("created_at", ""),
            "html_url": i.get("html_url", ""),
        }
        for i in all_issues_raw
        if "pull_request" not in i
    ]

    return {
        "owner": owner,
        "repo": repo,
        "repo_url": repo_url,
        "description": repo_info.get("description") or "",
        "period_days": period_days,
        "pairs": pairs,
        "unlinked_issues": unlinked_issues,
        "oversized_prs": oversized_prs,
        "total_rework_hours": round(total_rework_days, 1),
        "all_issues": all_issues_slim,
        "stats": {
            "closed_issues": len(closed_issues),
            "pairs_built": len(pairs),
            "unlinked": len(unlinked_issues),
            "oversized": len(oversized_prs),
        },
    }


def fetch_commits_with_diffs(owner: str, repo: str, token: str | None, max_commits: int = 60, log: Callable = None) -> list[dict]:
    """Fetch recent commits with full diffs — used in spec-scan mode when no issue-PR pairs exist."""
    base = f"https://api.github.com/repos/{owner}/{repo}"
    if log:
        log(f"Fetching commits from {owner}/{repo}...")

    commits_raw = _get_all_pages(f"{base}/commits", token, max_pages=1)
    commits_raw = commits_raw[:max_commits]

    result = []
    for c in commits_raw:
        sha = c["sha"]
        try:
            data = _get(f"{base}/commits/{sha}", token)
            files = data.get("files", [])
            diff_parts = []
            for f in files[:15]:
                patch = f.get("patch", "")
                if patch:
                    diff_parts.append(f"--- {f['filename']} ---\n{patch[:1500]}")
            result.append({
                "sha": sha[:7],
                "message": c["commit"]["message"].split("\n")[0][:200],
                "author": c["commit"]["author"].get("name", ""),
                "date": c["commit"]["author"].get("date", ""),
                "files_changed": [f["filename"] for f in files],
                "diff": "\n".join(diff_parts)[:5000],
            })
        except Exception:
            continue

    return result


# Extensions worth reading for spec comparison
_SOURCE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".sol",
    ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".cs", ".rb",
    ".toml", ".yaml", ".yml", ".json", ".env.example",
}

# Paths to always skip
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "out", "__pycache__",
    ".venv", "venv", "vendor", "coverage", ".next", ".turbo",
    "target", "artifacts", "cache", ".cache", "migrations",
}

# Files to skip even if extension matches
_SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "go.sum",
}


def fetch_repo_files(
    owner: str,
    repo: str,
    token: str | None,
    max_files: int = 40,
    max_file_chars: int = 3000,
    log: Callable = None,
) -> list[dict]:
    """
    Fetch key source files from the repo's default branch.
    Used in spec-scan mode to give the LLM real code to compare against.
    Returns list of {path, content} dicts, prioritised by relevance.
    """
    base = f"https://api.github.com/repos/{owner}/{repo}"
    if log:
        log(f"Fetching repository file tree for {owner}/{repo}...")

    # Get the default branch SHA via the repo info
    try:
        repo_info = _get(base, token)
        default_branch = repo_info.get("default_branch", "main")
        tree_resp = _get(
            f"{base}/git/trees/{default_branch}",
            token,
            params={"recursive": "1"},
        )
    except Exception as e:
        if log:
            log(f"  Could not fetch file tree: {e}")
        return []

    tree = tree_resp.get("tree", [])
    # Filter to blob (file) entries only, skip large files (>200KB)
    blobs = [
        item for item in tree
        if item.get("type") == "blob"
        and item.get("size", 0) < 200_000
    ]

    # Score files by relevance — prefer source files in meaningful paths
    def _score(path: str) -> int:
        parts = path.split("/")
        # Skip any file inside a blacklisted directory
        if any(p in _SKIP_DIRS for p in parts[:-1]):
            return -1
        filename = parts[-1]
        if filename in _SKIP_FILES:
            return -1
        ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
        if ext not in _SOURCE_EXTS:
            return -1
        score = 0
        # Prefer files closer to root
        score -= len(parts) * 2
        # Boost important filenames
        name_lower = filename.lower()
        for kw in ("main", "index", "app", "server", "core", "engine", "contract", "router", "config", "schema", "model", "handler", "service", "api"):
            if kw in name_lower:
                score += 5
        # Boost smart contracts and core logic
        if ext in (".sol", ".rs", ".go"):
            score += 4
        if ext in (".py", ".ts"):
            score += 2
        return score

    scored = [(item, _score(item["path"])) for item in blobs]
    scored = [(item, s) for item, s in scored if s >= 0]
    scored.sort(key=lambda x: -x[1])
    selected = [item for item, _ in scored[:max_files]]

    if log:
        log(f"  Reading {len(selected)} source files...")

    results = []
    for item in selected:
        path = item["path"]
        try:
            raw = requests.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{path}",
                headers=_headers(token),
                timeout=10,
            )
            if raw.status_code != 200:
                continue
            content = raw.text[:max_file_chars]
            if len(content.strip()) < 50:
                continue
            results.append({"path": path, "content": content})
        except Exception:
            continue

    if log:
        log(f"  Loaded {len(results)} files, {sum(len(f['content']) for f in results):,} chars total")

    return results
