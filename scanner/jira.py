"""Jira data fetcher — enriches issue-PR pairs with Jira ticket specs."""

import re
import asyncio
from typing import Callable
from urllib.parse import quote as _urlquote
import requests


# ── Jira ticket ID patterns ───────────────────────────────────────────────────

_JIRA_ID_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9}-\d+)\b')
_JIRA_RANGE_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9})-(\d+)\.\.(\d+)\b')


def extract_jira_ids(text: str) -> list[str]:
    """Extract Jira ticket IDs like PROJ-123 from a string."""
    return list(dict.fromkeys(_JIRA_ID_RE.findall(text or "")))


def expand_jira_ids(text: str) -> list[str]:
    """
    Extract Jira IDs, expanding ranges like QP-23..34 → QP-23, QP-24, ..., QP-34.
    Also handles slash-separated refs like QP-9/QP-10.
    """
    ids = []
    text_no_ranges = text or ""
    for m in _JIRA_RANGE_RE.finditer(text or ""):
        prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        if end > start and (end - start) < 50:  # sanity cap
            ids.extend(f"{prefix}-{n}" for n in range(start, end + 1))
        text_no_ranges = text_no_ranges.replace(m.group(0), "")
    ids.extend(_JIRA_ID_RE.findall(text_no_ranges))
    return list(dict.fromkeys(ids))


# ── ADF → plain text ──────────────────────────────────────────────────────────

def _adf_to_text(node, depth: int = 0) -> str:
    """Recursively convert Atlassian Document Format JSON to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(child, depth) for child in node)

    node_type = node.get("type", "")
    content = node.get("content", [])
    text = node.get("text", "")

    if node_type == "text":
        return text
    if node_type in ("paragraph", "blockquote", "heading"):
        return _adf_to_text(content, depth) + "\n"
    if node_type == "bulletList":
        return "\n".join("- " + _adf_to_text(item.get("content", []), depth + 1).strip()
                         for item in content) + "\n"
    if node_type == "orderedList":
        return "\n".join(f"{i}. " + _adf_to_text(item.get("content", []), depth + 1).strip()
                         for i, item in enumerate(content, 1)) + "\n"
    if node_type == "listItem":
        return _adf_to_text(content, depth)
    if node_type == "codeBlock":
        code = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        return f"```\n{code}\n```\n"
    if node_type == "hardBreak":
        return "\n"
    if node_type in ("doc", "tableRow", "tableCell", "tableHeader", "table",
                     "panel", "expand", "nestedExpand", "rule"):
        return _adf_to_text(content, depth)
    return _adf_to_text(content, depth) if content else text


# ── Jira REST API ─────────────────────────────────────────────────────────────

def _jira_get(url: str, email: str, token: str) -> dict:
    # Auto-mode (OAuth): email is empty → use Bearer token
    # Manual mode: email provided → use Basic auth (email + API token)
    if email:
        resp = requests.get(url, auth=(email, token),
                            headers={"Accept": "application/json"}, timeout=20)
    else:
        resp = requests.get(url,
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/json"}, timeout=20)
    if resp.status_code == 401:
        raise RuntimeError("Jira: invalid credentials.")
    if resp.status_code == 403:
        raise RuntimeError("Jira: token lacks permission to read this project.")
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()


def _parse_jira_description(fields: dict) -> str:
    desc = fields.get("description")
    if not desc:
        return ""
    if isinstance(desc, str):
        return desc[:2000]
    if isinstance(desc, dict):
        return _adf_to_text(desc).strip()[:2000]
    return ""


def _find_acceptance_criteria(fields: dict) -> str:
    for key in ("customfield_10016", "customfield_10020", "customfield_10014",
                "acceptance_criteria", "acceptanceCriteria"):
        val = fields.get(key)
        if val:
            if isinstance(val, str):
                return val[:1000]
            if isinstance(val, dict):
                return _adf_to_text(val).strip()[:1000]
    return ""


def test_jira_connection(jira_url: str, email: str, token: str) -> tuple[bool, str]:
    """
    Quick connectivity + auth check using an endpoint covered by read:jira-work scope.
    Returns (ok, error_message). error_message is "" when ok=True.
    """
    base = jira_url.rstrip("/")
    # Use /rest/api/3/project which requires read:jira-work (same scope as ticket fetching).
    # /rest/api/3/myself requires read:me which is a different scope.
    url = f"{base}/rest/api/3/project?maxResults=1"
    try:
        if email:
            resp = requests.get(url, auth=(email, token),
                                headers={"Accept": "application/json"}, timeout=10)
        else:
            resp = requests.get(url,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/json"}, timeout=10)
        if resp.status_code == 200:
            projects = resp.json()
            count = len(projects) if isinstance(projects, list) else (projects.get("total", 0))
            return True, f"Connected ({count} project(s) accessible)"
        if resp.status_code == 401:
            body_preview = resp.text[:300]
            return False, f"401 Unauthorized. URL={url} response={body_preview!r}"
        if resp.status_code == 403:
            return False, f"Jira token lacks permission (403). URL={url}"
        if resp.status_code == 404:
            return False, (
                f"Jira URL not found (404): {base}\n"
                "  For Jira Cloud use: https://yourcompany.atlassian.net\n"
                "  For Atlassian OAuth use: https://api.atlassian.com/ex/jira/<cloud-id>"
            )
        return False, f"Jira HTTP {resp.status_code} for {url}: {resp.text[:200]!r}"
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to Jira at {base} — check the URL."
    except requests.exceptions.Timeout:
        return False, f"Jira connection timed out at {base}."
    except Exception as exc:
        return False, f"Jira connection error: {exc}"


def fetch_jira_ticket(ticket_id: str, jira_url: str, email: str, token: str) -> dict | None:
    """Fetch a single Jira ticket and return a normalized dict."""
    base = jira_url.rstrip("/")
    url = (f"{base}/rest/api/3/issue/{ticket_id}"
           "?fields=summary,description,issuetype,status,priority,labels,assignee,comment")
    try:
        data = _jira_get(url, email, token)
    except RuntimeError:
        raise  # propagate auth/permission errors to caller
    except Exception:
        return None

    if not data or "fields" not in data:
        return None

    fields = data["fields"]
    description = _parse_jira_description(fields)
    acceptance_criteria = _find_acceptance_criteria(fields)

    comments = []
    for c in (fields.get("comment") or {}).get("comments", [])[:8]:
        body = c.get("body", "")
        if isinstance(body, dict):
            body = _adf_to_text(body).strip()
        if body:
            author = ((c.get("author") or {}).get("displayName") or "")
            comments.append(f"{author}: {body[:300]}" if author else body[:300])

    return {
        "id": ticket_id,
        "url": f"{base}/browse/{ticket_id}",
        "summary": fields.get("summary", ""),
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "labels": fields.get("labels") or [],
        "comments": comments,
    }


def search_jira_issues(query: str, jira_url: str, email: str, token: str,
                       max_results: int = 3) -> list[dict]:
    """
    Search Jira using JQL full-text search for tickets related to a query string.
    Used to find related tickets when no explicit Jira ID is referenced in the PR.
    Returns a list of normalized ticket dicts (same format as fetch_jira_ticket).
    """
    base = jira_url.rstrip("/")
    # Escape quotes in the query for JQL
    safe_query = query.replace('"', '\\"')[:200]
    jql = f'text ~ "{safe_query}" ORDER BY updated DESC'
    url = f"{base}/rest/api/3/issue/picker"
    # Use the simpler issue picker for title matching
    search_url = (f"{base}/rest/api/3/search"
                  f"?jql={_urlquote(jql)}&maxResults={max_results}"
                  f"&fields=summary,description,issuetype,status,priority,labels,comment")
    try:
        data = _jira_get(search_url, email, token)
    except RuntimeError:
        raise
    except Exception:
        return []

    results = []
    for item in (data.get("issues") or [])[:max_results]:
        fields = item.get("fields") or {}
        description = _parse_jira_description(fields)
        acceptance_criteria = _find_acceptance_criteria(fields)

        comments = []
        for c in (fields.get("comment") or {}).get("comments", [])[:5]:
            body = c.get("body", "")
            if isinstance(body, dict):
                body = _adf_to_text(body).strip()
            if body:
                author = ((c.get("author") or {}).get("displayName") or "")
                comments.append(f"{author}: {body[:300]}" if author else body[:300])

        ticket_id = item.get("key", "")
        results.append({
            "id": ticket_id,
            "url": f"{base}/browse/{ticket_id}",
            "summary": fields.get("summary", ""),
            "description": description,
            "acceptance_criteria": acceptance_criteria,
            "issue_type": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "priority": (fields.get("priority") or {}).get("name", ""),
            "labels": fields.get("labels") or [],
            "comments": comments,
        })
    return results


# ── Pair enrichment (GitHub-issues-first mode) ────────────────────────────────

async def enrich_pairs_with_jira(
    pairs: list[dict],
    jira_url: str,
    email: str,
    token: str,
    log: Callable,
) -> list[dict]:
    """
    1. Scan PR titles/bodies for explicit Jira IDs → fetch those tickets.
    2. For pairs with no Jira ticket found by ID, search Jira by issue title (JQL).
    Attaches results under pair['jira_tickets'].
    """
    loop = asyncio.get_event_loop()
    auth_error: list[str] = []

    # ── Pass 1: explicit ticket IDs in PR bodies ──────────────────────────────
    id_to_pairs: dict[str, list[int]] = {}
    for pair_idx, pair in enumerate(pairs):
        for pr in pair.get("prs", []):
            text = pr.get("title", "") + " " + (pr.get("body") or "")
            for jid in extract_jira_ids(text):
                id_to_pairs.setdefault(jid, []).append(pair_idx)

    sem = asyncio.Semaphore(6)

    if id_to_pairs:
        unique_ids = list(id_to_pairs.keys())
        log(f"Found {len(unique_ids)} Jira ticket ID(s): {', '.join(unique_ids[:10])}")

        async def _fetch(ticket_id: str) -> tuple[str, dict | None]:
            async with sem:
                try:
                    result = await loop.run_in_executor(
                        None, lambda: fetch_jira_ticket(ticket_id, jira_url, email, token)
                    )
                    return ticket_id, result
                except RuntimeError as e:
                    if not auth_error:
                        auth_error.append(str(e))
                    return ticket_id, None

        results = await asyncio.gather(*[_fetch(jid) for jid in unique_ids])
        ticket_map = {jid: ticket for jid, ticket in results if ticket}

        if auth_error:
            log(f"⚠ Jira credential error: {auth_error[0]}")
        elif len(ticket_map) == 0 and unique_ids:
            log(f"⚠ Could not fetch any Jira tickets. Check your Jira URL and token permissions.")
        else:
            log(f"Fetched {len(ticket_map)}/{len(unique_ids)} Jira tickets via ID")

        for jid, ticket in ticket_map.items():
            for pair_idx in id_to_pairs.get(jid, []):
                pairs[pair_idx].setdefault("jira_tickets", []).append(ticket)
    else:
        log("No explicit Jira ticket IDs found in PR descriptions")

    if auth_error:
        return pairs  # no point trying title search if auth is broken

    # ── Pass 2: title-based JQL search for pairs still missing Jira context ───
    pairs_needing_search = [
        (idx, pair) for idx, pair in enumerate(pairs)
        if not pair.get("jira_tickets")
    ]
    if pairs_needing_search:
        log(f"Searching Jira by issue title for {len(pairs_needing_search)} pair(s)...")
        enriched_via_search = 0

        async def _search(pair_idx: int, title: str) -> tuple[int, list[dict]]:
            async with sem:
                try:
                    tickets = await loop.run_in_executor(
                        None, lambda: search_jira_issues(title, jira_url, email, token, max_results=2)
                    )
                    return pair_idx, tickets
                except RuntimeError:
                    return pair_idx, []
                except Exception:
                    return pair_idx, []

        search_tasks = [
            _search(idx, pair.get("issue", {}).get("title", ""))
            for idx, pair in pairs_needing_search
            if pair.get("issue", {}).get("title", "")
        ]
        if search_tasks:
            search_results = await asyncio.gather(*search_tasks)
            for pair_idx, tickets in search_results:
                if tickets:
                    pairs[pair_idx].setdefault("jira_tickets", []).extend(tickets)
                    enriched_via_search += 1
            if enriched_via_search:
                log(f"Found related Jira tickets via title search for {enriched_via_search} pair(s)")

    return pairs
