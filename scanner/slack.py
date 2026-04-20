"""Slack data fetcher — enriches pairs with PR/ticket thread discussions."""

import asyncio
from typing import Callable
import requests

_SLACK_API = "https://slack.com/api"


# ── Slack REST client ─────────────────────────────────────────────────────────

def _slack_get(method: str, token: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{_SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown")
        if error == "invalid_auth":
            raise RuntimeError(
                "Slack: invalid token. Use a User OAuth token (xoxp-...) "
                "with search:read and channels:history scopes."
            )
        if error in ("missing_scope", "not_allowed_token_type"):
            raise RuntimeError(
                f"Slack token missing scope. Need search:read + channels:history "
                f"(user token, not bot token). Error: {error}"
            )
        if error == "ratelimited":
            return {}
        raise RuntimeError(f"Slack API error: {error}")
    return data


def _search_messages(query: str, token: str, count: int = 5) -> list[dict]:
    """Full-text search. Requires search:read (user token only)."""
    try:
        data = _slack_get("search.messages", token, {"query": query, "count": count, "sort": "timestamp"})
        return (data.get("messages") or {}).get("matches", [])
    except RuntimeError:
        raise
    except Exception:
        return []


def _fetch_thread(channel: str, ts: str, token: str) -> list[dict]:
    """Fetch all messages in a thread."""
    try:
        data = _slack_get("conversations.replies", token, {
            "channel": channel, "ts": ts, "limit": 30
        })
        return data.get("messages", [])
    except Exception:
        return []


# ── Thread formatter ──────────────────────────────────────────────────────────

def _format_thread(messages: list[dict], label: str = "",
                   usernames: dict | None = None) -> str:
    """
    Convert raw Slack messages to readable text for the AI.
    Skips system messages, bot noise, very short messages.
    Prefixes each line with the display name when available.
    """
    lines = []
    for msg in messages:
        # Skip system subtypes
        if msg.get("subtype") in ("bot_message", "channel_join", "channel_leave",
                                   "channel_topic", "file_share"):
            continue
        text = (msg.get("text") or "").strip()
        # Strip Slack user/channel references (<@U123> → @user)
        text = _clean_slack_text(text)
        if len(text) < 15:
            continue
        # Resolve display name: prefer msg.username (set for some msgs),
        # then look up user ID in the usernames dict passed from search results
        name = (msg.get("username") or "").strip()
        if not name and usernames:
            name = usernames.get(msg.get("user", ""), "")
        prefix = f"{name}: " if name else ""
        lines.append(f"{prefix}{text[:400]}")

    if not lines:
        return ""
    header = f"[{label}]\n" if label else ""
    return header + "\n---\n".join(lines[:15])


def _clean_slack_text(text: str) -> str:
    """Simplify Slack mrkdwn: strip user/channel refs, unfurl URLs."""
    import re
    text = re.sub(r"<@[A-Z0-9]+>", "@user", text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)  # <url|label> → label
    text = re.sub(r"<([^>]+)>", r"\1", text)             # <url> → url
    return text.strip()


# ── Search helpers ────────────────────────────────────────────────────────────

def _get_thread_for_query(query: str, token: str, label: str = "") -> tuple[str, list[str], list[dict]]:
    """
    Search Slack for query.
    Returns (formatted thread text, list of permalinks, list of {name, url} author dicts).
    """
    matches = _search_messages(query, token, count=3)
    all_messages = []
    permalinks = []
    # Build user_id → display_name from search match results
    usernames: dict[str, str] = {}
    # Collect {name, url} for the authors of matched messages
    message_authors: list[dict] = []
    seen_authors: set[str] = set()

    for match in matches[:2]:
        channel = (match.get("channel") or {}).get("id", "")
        ts = match.get("ts", "")
        permalink = match.get("permalink", "")
        user_id = match.get("user", "")
        display_name = (match.get("username") or "").strip()

        if user_id and display_name:
            usernames[user_id] = display_name
        if display_name and permalink and display_name not in seen_authors:
            message_authors.append({"name": display_name, "url": permalink})
            seen_authors.add(display_name)

        if channel and ts:
            thread = _fetch_thread(channel, ts, token)
            if thread:
                all_messages.extend(thread)
                if permalink:
                    permalinks.append(permalink)

    if not all_messages:
        return "", [], []
    return _format_thread(all_messages, label=label, usernames=usernames), permalinks, message_authors


def get_thread_for_pr(owner: str, repo: str, pr_number: int, token: str) -> tuple[str, list[str], list[dict]]:
    """Search Slack for discussions referencing a specific GitHub PR."""
    query = f"github.com/{owner}/{repo}/pull/{pr_number}"
    return _get_thread_for_query(query, token, label=f"PR #{pr_number} discussion")


def get_thread_for_ticket(ticket_id: str, token: str) -> tuple[str, list[str], list[dict]]:
    """Search Slack for discussions referencing a Jira/Linear ticket."""
    return _get_thread_for_query(ticket_id, token, label=f"{ticket_id} discussion")


def get_thread_for_channel(channel: str, query: str, token: str) -> tuple[str, list[str], list[dict]]:
    """Search a specific Slack channel for messages matching a query."""
    channel_name = channel.lstrip("#")
    full_query = f"in:#{channel_name} {query}"
    return _get_thread_for_query(full_query, token, label=f"#{channel_name}")


# ── Pair enrichment ───────────────────────────────────────────────────────────

async def enrich_pairs_with_slack(
    pairs: list[dict],
    owner: str,
    repo: str,
    token: str,
    log: Callable,
    channels: list[str] | None = None,
) -> list[dict]:
    """
    For each pair, search Slack for:
    1. Threads discussing the linked PR(s) — post-ship feedback
    2. Threads discussing the Jira/Linear ticket — pre-build spec decisions

    Attaches combined thread text under pair['slack_discussion'].
    """
    loop = asyncio.get_event_loop()
    # Slack rate limit: ~1 req/s for search, be conservative
    sem = asyncio.Semaphore(2)
    enriched = 0

    async def _enrich(idx: int, pair: dict) -> None:
        nonlocal enriched
        async with sem:
            issue = pair.get("issue", {})
            prs = pair.get("prs", [])
            threads = []
            all_permalinks: list[str] = []
            all_authors: list[dict] = []
            seen_author_names: set[str] = set()

            def _collect(text, links, authors):
                if text:
                    threads.append(text)
                    all_permalinks.extend(links)
                    for a in authors:
                        if a["name"] not in seen_author_names:
                            all_authors.append(a)
                            seen_author_names.add(a["name"])

            # Search by PR URL (post-ship feedback)
            for pr in prs[:2]:
                pr_num = pr.get("number")
                if pr_num:
                    text, links, authors = await loop.run_in_executor(
                        None, lambda n=pr_num: get_thread_for_pr(owner, repo, n, token)
                    )
                    _collect(text, links, authors)

            # Search by ticket ID (Jira or Linear)
            ticket_id = issue.get("jira_id", "")
            if not ticket_id:
                for jt in (pair.get("jira_tickets") or [])[:1]:
                    ticket_id = jt.get("id", "")
            if not ticket_id:
                for lt in (pair.get("linear_tickets") or [])[:1]:
                    ticket_id = lt.get("id", "")

            if ticket_id:
                text, links, authors = await loop.run_in_executor(
                    None, lambda t=ticket_id: get_thread_for_ticket(t, token)
                )
                _collect(text, links, authors)

            # Search specified channels by issue/PR title keywords
            if channels:
                issue_title = issue.get("title", "")
                if issue_title:
                    for ch in channels:
                        text, links, authors = await loop.run_in_executor(
                            None, lambda c=ch, q=issue_title: get_thread_for_channel(c, q, token)
                        )
                        _collect(text, links, authors)

            if threads:
                pairs[idx]["slack_discussion"] = "\n\n".join(threads)
                pairs[idx]["slack_permalinks"] = list(dict.fromkeys(all_permalinks))
                pairs[idx]["slack_authors"] = all_authors  # [{name, url}, ...]
                enriched += 1

    await asyncio.gather(*[_enrich(i, pair) for i, pair in enumerate(pairs)])
    log(f"Found Slack discussions for {enriched}/{len(pairs)} pairs")
    return pairs
