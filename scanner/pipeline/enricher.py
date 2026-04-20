"""
Enricher: query the EntityStore and inject richer context into each pair.

After the Collector / Atomizer / Embedder / Linker have run, this module
bridges the knowledge graph back to the existing `repo_data["pairs"]` structure
that analyzer.py consumes.

For every pair it:
  1. Builds a targeted search query from the issue title + PR title
  2. Searches Qdrant for the top-K most relevant entities
  3. Formats them into a concise "qdrant_context" string block
  4. Attaches the block to `pair["qdrant_context"]`

The context block is later injected into the LLM prompt by analyzer.py's
_drift_prompt(), right alongside the existing jira_section / slack_section.

Search strategy
───────────────
We issue two targeted queries per pair:
  a) Acceptance criteria + spec entities  — "what was the requirement?"
  b) Code atoms (functions/classes)       — "what was actually changed?"
Then we deduplicate and rank by entity type priority before formatting.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scanner.pipeline.store import EntityStore
    from scanner.pipeline.linker import CommunityMap

logger = logging.getLogger(__name__)

# How many entities to retrieve per search leg
_SPEC_TOP_K = 5
_CODE_TOP_K = 5

# Priority order for display (lower index = shown first)
_TYPE_PRIORITY = [
    "acceptance_criterion",
    "jira_ticket",
    "linear_issue",
    "jira_comment",
    "linear_comment",
    "slack_thread",
    "function",
    "class",
    "file_hunk",
    "commit",
    "pull_request",
    "merge_request",
    "issue",
    "spec_document",
    "import",
]


def enrich_pairs(
    repo_data:     dict,
    store:         "EntityStore",
    community_map: "CommunityMap | None" = None,
) -> None:
    """
    Mutates `repo_data["pairs"]` in-place, adding a `qdrant_context` string to
    each pair dict.  No-ops gracefully if the store is empty or unavailable.

    Parameters
    ----------
    repo_data      The dict from github.py / gitlab.py (with pairs already enriched
                   by jira/linear/slack enrichers)
    store          Initialised EntityStore (must already have entities embedded)
    community_map  Optional: output of linker.link() for community-scoped search
    """
    total = store.count()
    if total == 0:
        logger.debug("Enricher: store is empty — skipping")
        return

    pairs = repo_data.get("pairs") or []
    if not pairs:
        return

    logger.info(f"Enricher: enriching {len(pairs)} pairs from {total} indexed entities")

    for pair in pairs:
        try:
            ctx = _build_context(pair, store, community_map)
        except Exception as exc:
            logger.warning(f"Enricher: failed to build context for pair: {exc}")
            ctx = ""
        if ctx:
            pair["qdrant_context"] = ctx


# ── Private helpers ───────────────────────────────────────────────────────────

def _build_context(
    pair:          dict,
    store:         "EntityStore",
    community_map: "CommunityMap | None",
) -> str:
    """
    Build the qdrant_context string for a single pair.
    Returns empty string if nothing relevant is found.
    """
    issue = pair.get("issue") or {}
    prs   = pair.get("prs") or []
    pr    = prs[0] if prs else {}

    issue_title = issue.get("title", "")
    pr_title    = pr.get("title", "")

    if not issue_title and not pr_title:
        return ""

    # ── Query 1: spec-side entities (ACs, Jira, Linear, Slack) ───────────────
    spec_query = " ".join(filter(None, [
        issue_title,
        issue.get("body", "")[:300],
    ]))
    spec_hits = _search_multi_type(
        store, spec_query,
        entity_types=["acceptance_criterion", "jira_ticket", "linear_issue",
                      "jira_comment", "slack_thread"],
        top_k=_SPEC_TOP_K,
    )

    # ── Query 2: code-side entities (functions, classes changed in this PR) ──
    code_query = " ".join(filter(None, [
        pr_title,
        pr.get("body", "")[:200],
    ]))
    code_hits = _search_multi_type(
        store, code_query,
        entity_types=["function", "class", "import", "file_hunk"],
        top_k=_CODE_TOP_K,
    )

    # ── Merge and deduplicate ─────────────────────────────────────────────────
    seen_ids: set[str]      = set()
    combined: list[dict]    = []

    for hit in spec_hits + code_hits:
        hid = hit.get("id", "")
        if hid and hid not in seen_ids:
            seen_ids.add(hid)
            combined.append(hit)

    if not combined:
        return ""

    # Sort by type priority
    def _rank(h: dict) -> int:
        et = h.get("entity_type", "")
        try:
            return _TYPE_PRIORITY.index(et)
        except ValueError:
            return len(_TYPE_PRIORITY)

    combined.sort(key=_rank)

    # ── Format ────────────────────────────────────────────────────────────────
    return _format_context(combined)


def _search_multi_type(
    store:        "EntityStore",
    query:        str,
    entity_types: list[str],
    top_k:        int,
) -> list[dict]:
    """
    Query the store for each entity type in the list and return merged results.
    We query per entity_type (rather than relying on a single unfiltered query)
    so the results aren't dominated by the most common type.
    """
    if not query.strip():
        return []

    seen:    set[str]   = set()
    results: list[dict] = []

    # One shot per entity type, limited to 3 each for variety
    per_type = max(2, top_k // len(entity_types))
    for et in entity_types:
        for hit in store.search(query, entity_type_filter=et, top_k=per_type):
            hid = hit.get("id", "")
            if hid and hid not in seen:
                seen.add(hid)
                results.append(hit)

    return results[:top_k]


def _format_context(hits: list[dict]) -> str:
    """
    Render a list of entity payloads as a compact context block for the LLM prompt.
    Keeps total length under ~1500 chars so it doesn't crowd out the diff.
    """
    lines: list[str] = []
    budget = 1400  # chars remaining

    for hit in hits:
        et    = hit.get("entity_type", "unknown")
        title = hit.get("title", "").strip()
        body  = hit.get("body", "").strip()

        if not title and not body:
            continue

        # Choose label and content based on type
        if et == "acceptance_criterion":
            label = "AC"
            content = body[:200]
        elif et in ("jira_ticket", "linear_issue"):
            label   = hit.get("meta_jira_id") or hit.get("meta_linear_id") or et.upper()
            content = body[:300]
        elif et in ("jira_comment", "linear_comment", "slack_thread"):
            label = et.replace("_", " ").upper()
            content = body[:250]
        elif et == "function":
            label   = "FN"
            content = f"{title}  {body[:150]}"
        elif et == "class":
            label   = "CLASS"
            content = f"{title}  {body[:150]}"
        elif et == "import":
            label   = "IMPORTS"
            content = body[:200]
        elif et in ("file_hunk",):
            label   = "CHANGED FILE"
            content = title
        else:
            label   = et.upper()
            content = (body or title)[:200]

        # Source badge
        source = hit.get("source", "")
        badge  = f"[{source.upper()}]" if source else ""

        line = f"{badge} {label}: {content}".strip()
        if len(line) > budget:
            line = line[:budget]
        budget -= len(line) + 1
        lines.append(line)
        if budget <= 0:
            break

    return "\n".join(lines)
