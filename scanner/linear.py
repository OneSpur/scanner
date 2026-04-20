"""Linear data fetcher — enriches issue-PR pairs with Linear ticket specs."""

import re
import asyncio
from typing import Callable
import requests


# ── Linear issue ID patterns ──────────────────────────────────────────────────

_LINEAR_ID_RE = re.compile(r'\b([A-Z][A-Z0-9]*-\d+)\b')
_LINEAR_URL_RE = re.compile(r'linear\.app/[^/\s]+/issue/([A-Z][A-Z0-9]*-\d+)')

_GRAPHQL_URL = "https://api.linear.app/graphql"


def extract_linear_ids(text: str) -> list[str]:
    """
    Extract Linear issue IDs (e.g. ENG-123) from text.
    Prefers full URL matches to avoid false positives from bare IDs.
    """
    ids = []
    url_ids = set()
    for m in _LINEAR_URL_RE.finditer(text or ""):
        ids.append(m.group(1))
        url_ids.add(m.group(1))
    for m in _LINEAR_ID_RE.finditer(text or ""):
        if m.group(1) not in url_ids:
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


# ── Acceptance criteria parser ────────────────────────────────────────────────

_AC_HEADER_RE = re.compile(
    r'(?:^|\n)(#{1,3})\s*(acceptance criteria|acceptance|definition of done|'
    r'done when|done criteria|ac)\s*\n',
    re.I,
)


def _parse_acceptance_criteria(description: str) -> tuple[str, str]:
    """
    Extract acceptance criteria section from markdown description.
    Returns (description_without_ac, ac_text).
    """
    if not description:
        return "", ""

    m = _AC_HEADER_RE.search(description)
    if not m:
        return description, ""

    heading_level = len(m.group(1))
    content_start = m.end()

    # Find next heading at same or higher level
    next_heading = re.search(
        r'\n#{1,' + str(heading_level) + r'}\s',
        description[content_start:]
    )
    if next_heading:
        ac_text = description[content_start:content_start + next_heading.start()].strip()
        desc_without = (description[:m.start()] + description[content_start + next_heading.start():]).strip()
    else:
        ac_text = description[content_start:].strip()
        desc_without = description[:m.start()].strip()

    return desc_without, ac_text


# ── Linear GraphQL client ─────────────────────────────────────────────────────

def _linear_graphql(query: str, variables: dict, api_key: str) -> dict:
    resp = requests.post(
        _GRAPHQL_URL,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=20,
    )
    if resp.status_code == 401:
        raise RuntimeError("Linear: invalid API key.")
    if resp.status_code == 403:
        raise RuntimeError("Linear: API key lacks permission.")
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL error: {data['errors'][0].get('message', 'unknown')}")
    return data


_FETCH_ISSUE_QUERY = """
query FetchIssue($identifier: String!) {
  issue(id: $identifier) {
    id
    identifier
    title
    description
    priority
    priorityLabel
    url
    state { name type }
    labels { nodes { name } }
    completedAt
    comments(first: 10) {
      nodes {
        body
        createdAt
        user { name }
      }
    }
    parent {
      identifier
      title
      description
    }
  }
}
"""


def fetch_linear_issue(identifier: str, api_key: str) -> dict | None:
    """
    Fetch a single Linear issue by identifier (e.g. ENG-123).
    Includes comments (team discussion) and parent issue context.
    Returns normalized dict or None if not found.
    """
    try:
        data = _linear_graphql(_FETCH_ISSUE_QUERY, {"identifier": identifier}, api_key)
    except Exception:
        return None

    issue = (data.get("data") or {}).get("issue")
    if not issue:
        return None
    raw_description = issue.get("description") or ""
    description, ac_text = _parse_acceptance_criteria(raw_description)

    # Parent issue context (sub-issues need broader feature spec)
    parent = issue.get("parent")
    parent_context = ""
    if parent:
        parent_desc = (parent.get("description") or "")[:500]
        parent_context = f"{parent['identifier']}: {parent['title']}\n{parent_desc}".strip()

    # Comments: often contain scope decisions ("we decided to defer X")
    comments = []
    for c in (issue.get("comments") or {}).get("nodes", []):
        body = (c.get("body") or "").strip()
        if body:
            author = (c.get("user") or {}).get("name", "")
            entry = f"{author}: {body[:400]}" if author else body[:400]
            comments.append(entry)

    labels = [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])]

    return {
        "id": identifier,
        "url": issue.get("url", f"https://linear.app/issue/{identifier}"),
        "summary": issue.get("title", ""),
        "description": description,
        "acceptance_criteria": ac_text,
        "parent_context": parent_context,
        "issue_type": (issue.get("state") or {}).get("name", ""),
        "status": (issue.get("state") or {}).get("type", ""),
        "priority": issue.get("priorityLabel") or "",
        "labels": labels,
        "comments": comments,
    }


# ── Pair enrichment (GitHub-issues-first mode) ────────────────────────────────

async def enrich_pairs_with_linear(
    pairs: list[dict],
    api_key: str,
    log: Callable,
) -> list[dict]:
    """
    Scan PR titles/bodies for Linear IDs, fetch those issues,
    attach them to pairs under 'linear_tickets'.
    """
    loop = asyncio.get_event_loop()

    id_to_pairs: dict[str, list[int]] = {}
    for pair_idx, pair in enumerate(pairs):
        for pr in pair.get("prs", []):
            text = pr.get("title", "") + " " + (pr.get("body") or "")
            for lid in extract_linear_ids(text):
                id_to_pairs.setdefault(lid, []).append(pair_idx)

    if not id_to_pairs:
        log("No Linear issue IDs found in PR descriptions — skipping Linear enrichment")
        return pairs

    unique_ids = list(id_to_pairs.keys())
    log(f"Found {len(unique_ids)} Linear issue ID(s): {', '.join(unique_ids[:10])}")

    sem = asyncio.Semaphore(6)

    async def _fetch(issue_id: str) -> tuple[str, dict | None]:
        async with sem:
            result = await loop.run_in_executor(
                None, lambda: fetch_linear_issue(issue_id, api_key)
            )
            return issue_id, result

    results = await asyncio.gather(*[_fetch(lid) for lid in unique_ids])
    issue_map = {lid: issue for lid, issue in results if issue}
    log(f"Fetched {len(issue_map)}/{len(unique_ids)} Linear issues")

    for lid, issue in issue_map.items():
        for pair_idx in id_to_pairs.get(lid, []):
            pairs[pair_idx].setdefault("linear_tickets", []).append(issue)

    return pairs
