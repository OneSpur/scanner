"""
Stage 4 — Linker: build a cross-integration entity graph and detect communities.

Algorithm
─────────
1. Load all entity payloads from the EntityStore
2. Build a NetworkX graph where:
     nodes  = entity IDs
     edges  = entity.related_ids relationships (directed)
            + cross-type reference edges inferred from natural keys
              (e.g. Slack thread body that mentions a PR number)
3. Run community detection on the undirected projection:
     - Primary:   NetworkX built-in Louvain (networkx >= 3.0)
     - Fallback:  connected components (always available)
4. Return a CommunityMap (entity_id → community_id)

The CommunityMap is used by the Enricher to scope Qdrant searches:
"find me related entities that live in the same community as this PR".

Cross-reference inference
─────────────────────────
We scan entity body text for patterns that suggest cross-integration links:
  - "PR #42" or "!42"  in Slack / Jira text  → link to that PR entity
  - "PROJ-123"         in PR / issue text     → link to that Jira entity
  - Shared URL substrings (github.com/owner/repo) between entities

This mirrors what Graphify does: entity linking via shared mentions and URLs.

NetworkX + Louvain note
───────────────────────
`nx.community.louvain_communities()` is available since networkx 2.7 and is
non-deterministic by default.  We pass `seed=42` for reproducibility.
The algorithm runs fast on graphs up to ~10k nodes (well within typical usage).
"""

from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# ── Patterns for cross-reference inference ───────────────────────────────────

_PR_REF_PAT    = re.compile(r"(?:PR\s*#|!)\s*(\d+)", re.IGNORECASE)
_ISSUE_REF_PAT = re.compile(r"#\s*(\d+)")
_JIRA_REF_PAT  = re.compile(r"\b([A-Z]{2,10}-\d+)\b")


# ── Public types ──────────────────────────────────────────────────────────────

CommunityMap = dict[str, int]   # entity_id → community_id


def link(
    store,
    log: Callable[[str], None] | None = None,
) -> CommunityMap:
    """
    Build the entity graph and run community detection.

    Parameters
    ----------
    store   An initialised EntityStore (must already contain embedded entities)
    log     Optional progress callback

    Returns
    -------
    CommunityMap: dict mapping entity_id → community_id (integer label)
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            logger.info(msg)

    try:
        import networkx as nx
    except ImportError:
        _log("networkx not installed — skipping community detection")
        return {}

    # ── Load all entities ─────────────────────────────────────────────────────
    payloads = store.get_all()
    if not payloads:
        _log("Linker: no entities in store — skipping")
        return {}

    _log(f"Linker: building graph from {len(payloads)} entities…")

    # ── Build lookup indexes ──────────────────────────────────────────────────
    # entity_id → payload
    by_id:         dict[str, dict] = {p["id"]: p for p in payloads if p.get("id")}
    # natural_key → entity_id
    by_natural_key: dict[str, str] = {
        p["natural_key"]: p["id"]
        for p in payloads
        if p.get("natural_key") and p.get("id")
    }
    # Jira key → entity_id (fast lookup for cross-ref)
    jira_by_id: dict[str, str] = {
        p.get("meta_jira_id", ""): p["id"]
        for p in payloads
        if p.get("meta_jira_id") and p.get("id")
    }

    # ── Construct graph ───────────────────────────────────────────────────────
    G = nx.Graph()

    for eid, payload in by_id.items():
        G.add_node(
            eid,
            entity_type=payload.get("entity_type", ""),
            source=payload.get("source", ""),
            title=payload.get("title", ""),
        )

    edge_count = 0

    for eid, payload in by_id.items():
        # ── Edges from explicit related_ids ───────────────────────────────────
        for rid in (payload.get("related_ids") or []):
            if rid in by_id:
                G.add_edge(eid, rid, relation="related_to")
                edge_count += 1

        # ── Cross-reference inference from body text ───────────────────────────
        body         = payload.get("body", "")
        natural_key  = payload.get("natural_key", "")
        repo_ns      = _infer_repo_ns(natural_key)

        if body and repo_ns:
            # PR references in Slack / Jira → link to PR entity
            for m in _PR_REF_PAT.finditer(body):
                pr_num = m.group(1)
                pr_key = f"{repo_ns}!{pr_num}"
                pr_eid = by_natural_key.get(pr_key)
                if pr_eid and pr_eid != eid:
                    G.add_edge(eid, pr_eid, relation="mentions_pr")
                    edge_count += 1

            # Issue references "#42"
            for m in _ISSUE_REF_PAT.finditer(body):
                inum = m.group(1)
                for candidate_key in (f"{repo_ns}#{inum}",):
                    candidate_eid = by_natural_key.get(candidate_key)
                    if candidate_eid and candidate_eid != eid:
                        G.add_edge(eid, candidate_eid, relation="mentions_issue")
                        edge_count += 1

            # Jira references "PROJ-123"
            for m in _JIRA_REF_PAT.finditer(body):
                jira_id = m.group(1)
                jira_eid = jira_by_id.get(jira_id)
                if jira_eid and jira_eid != eid:
                    G.add_edge(eid, jira_eid, relation="mentions_jira")
                    edge_count += 1

    _log(f"Linker: graph has {G.number_of_nodes()} nodes, {edge_count} edges")

    # ── Community detection ───────────────────────────────────────────────────
    community_map: CommunityMap = {}

    try:
        communities = nx.community.louvain_communities(G, seed=42)
        for community_id, node_set in enumerate(communities):
            for nid in node_set:
                community_map[nid] = community_id
        _log(f"Linker: detected {len(communities)} communities (Louvain)")
    except AttributeError:
        # networkx < 2.7 — fall back to connected components
        for component_id, component in enumerate(nx.connected_components(G)):
            for nid in component:
                community_map[nid] = component_id
        _log(f"Linker: used connected-components ({len(set(community_map.values()))} groups)")
    except Exception as exc:
        logger.warning(f"Community detection failed ({exc}) — each entity gets its own community")
        for i, nid in enumerate(G.nodes):
            community_map[nid] = i

    return community_map


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_repo_ns(natural_key: str) -> str:
    """
    Extract 'owner/repo' prefix from a natural key like 'owner/repo#42'.
    Returns empty string if the key doesn't follow this pattern.
    """
    # Matches "owner/repo" before any trailing #, !, @, ::, or :
    m = re.match(r"^([\w.\-]+/[\w.\-]+)(?:[#!@:]|$)", natural_key)
    return m.group(1) if m else ""
