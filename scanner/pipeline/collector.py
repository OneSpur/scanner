"""
Stage 1 — Collector: convert repo_data into a flat list of Entity objects.

repo_data is the dict built by scanner/github.py or scanner/gitlab.py
(and subsequently enriched by jira.py, linear.py, slack.py).

Entity extraction map
─────────────────────
repo_data["pairs"]
  pair["issue"]              → ISSUE
  pair["prs"][*]             → PULL_REQUEST / MERGE_REQUEST
  pair["prs"][*]["commits"]  → COMMIT (one per commit)
  pair["prs"][*]["files"]    → FILE_HUNK (one per changed file)
  pair["prs"][*]["reviews"]  → appended to PR body (not separate entities)
  pair.get("jira_tickets")   → JIRA_TICKET + JIRA_COMMENT (per ticket)
  pair.get("linear_tickets") → LINEAR_ISSUE
  pair.get("slack_discussion")→ SLACK_THREAD

repo_data["all_issues"]      → ISSUE (unlinked — for cross-referencing)
extra_context                → SPEC_DOCUMENT (uploaded file text)
"""

from __future__ import annotations

import logging
from typing import Sequence

from scanner.pipeline.entities import Entity, EntityType, SourceIntegration, make_entity

logger = logging.getLogger(__name__)


def collect(
    repo_data:     dict,
    platform:      str        = "github",
    extra_context: str | None = None,
) -> list[Entity]:
    """
    Main entry-point: walk repo_data and return all extracted entities.

    Parameters
    ----------
    repo_data     The dict returned by github.fetch_repo_data / gitlab.fetch_repo_data
                  (possibly enriched by jira/linear/slack enrichers)
    platform      "github" or "gitlab" — controls entity type label
    extra_context Text content of any uploaded spec documents (optional)
    """
    entities: list[Entity] = []

    owner = repo_data.get("owner", "")
    repo  = repo_data.get("repo", "")
    ns    = f"{owner}/{repo}"         # natural key namespace prefix

    pr_type = (
        EntityType.MERGE_REQUEST.value if platform == "gitlab"
        else EntityType.PULL_REQUEST.value
    )
    src = (
        SourceIntegration.GITLAB.value if platform == "gitlab"
        else SourceIntegration.GITHUB.value
    )

    # ── Linked pairs ──────────────────────────────────────────────────────────
    for pair in repo_data.get("pairs") or []:
        issue_entities, pr_ids = _collect_pair(pair, ns, src, pr_type)
        entities.extend(issue_entities)

        # ── Jira tickets attached to this pair ────────────────────────────────
        for jt in pair.get("jira_tickets") or []:
            entities.extend(_collect_jira_ticket(jt, pr_ids))

        # ── Linear issues attached to this pair ───────────────────────────────
        for lt in pair.get("linear_tickets") or []:
            entities.extend(_collect_linear_issue(lt, pr_ids))

        # ── Slack thread attached to this pair ────────────────────────────────
        slack_disc = (pair.get("slack_discussion") or "").strip()
        if slack_disc:
            pr_title = pair["prs"][0]["title"] if pair.get("prs") else ""
            entities.append(
                make_entity(
                    source=SourceIntegration.SLACK.value,
                    entity_type=EntityType.SLACK_THREAD.value,
                    natural_key=f"slack:{ns}:{pair['issue']['number']}",
                    title=f"Slack discussion: {pr_title}",
                    body=slack_disc[:3000],
                    metadata={"issue_number": str(pair["issue"]["number"])},
                    related_ids=pr_ids,
                )
            )

    # ── Unlinked / background issues ─────────────────────────────────────────
    for iss in repo_data.get("all_issues") or []:
        num = iss.get("number")
        if num is None:
            continue
        entities.append(
            make_entity(
                source=src,
                entity_type=EntityType.ISSUE.value,
                natural_key=f"{ns}#{num}",
                title=iss.get("title", ""),
                body=(iss.get("body") or "")[:2000],
                metadata={
                    "url":        iss.get("html_url", ""),
                    "created_at": iss.get("created_at", ""),
                    "linked":     "false",
                },
            )
        )

    # ── Uploaded spec documents ───────────────────────────────────────────────
    if extra_context and extra_context.strip():
        entities.append(
            make_entity(
                source=SourceIntegration.UPLOAD.value,
                entity_type=EntityType.SPEC_DOCUMENT.value,
                natural_key=f"upload:{ns}:spec_docs",
                title="Uploaded spec documents",
                body=extra_context[:6000],
                metadata={"repo": ns},
            )
        )

    logger.info(f"Collector extracted {len(entities)} entities from {ns}")
    return entities


# ── Private helpers ───────────────────────────────────────────────────────────

def _collect_pair(
    pair:    dict,
    ns:      str,
    src:     str,
    pr_type: str,
) -> tuple[list[Entity], list[str]]:
    """
    Extract entities from a single (issue, PRs) pair.
    Returns (list_of_entities, list_of_pr_entity_ids).
    """
    entities: list[Entity] = []
    pr_ids:   list[str]    = []

    issue = pair.get("issue") or {}
    inum  = issue.get("number")

    # ── Issue entity ──────────────────────────────────────────────────────────
    if inum:
        issue_eid = _issue_entity_id(src, ns, inum)
        labels    = issue.get("labels") or []
        entities.append(
            make_entity(
                source=src,
                entity_type=EntityType.ISSUE.value,
                natural_key=f"{ns}#{inum}",
                title=issue.get("title", ""),
                body=(issue.get("body") or "")[:2000],
                metadata={
                    "url":        issue.get("url", ""),
                    "labels":     ",".join(labels),
                    "closed_at":  issue.get("closed_at") or "",
                    "author":     issue.get("author", ""),
                    "linked":     "true",
                },
            )
        )
    else:
        issue_eid = None

    # ── PR entities ───────────────────────────────────────────────────────────
    for pr in pair.get("prs") or []:
        pnum = pr.get("number")
        if pnum is None:
            continue

        pr_eid = _pr_entity_id(src, ns, pnum, pr_type)
        pr_ids.append(pr_eid)

        # Build review text to include in PR body
        reviews_text = "\n".join(
            f"[{r['state']}] {r.get('body', '')[:300]}"
            for r in (pr.get("reviews") or [])[:5]
            if r.get("body") or r.get("state") == "CHANGES_REQUESTED"
        )
        pr_body = "\n\n".join(filter(None, [
            pr.get("body", "")[:600],
            f"REVIEWS:\n{reviews_text}" if reviews_text else "",
        ]))

        pr_related = [issue_eid] if issue_eid else []

        entities.append(
            make_entity(
                source=src,
                entity_type=pr_type,
                natural_key=f"{ns}!{pnum}",
                title=pr.get("title", ""),
                body=pr_body,
                metadata={
                    "url":               pr.get("url", ""),
                    "author":            pr.get("author", ""),
                    "merged_at":         pr.get("merged_at") or "",
                    "total_changes":     pr.get("total_changes", 0),
                    "changes_requested": pr.get("changes_requested", 0),
                    "rework_hours":      pr.get("rework_hours", 0.0),
                },
                related_ids=pr_related,
            )
        )

        # ── Commits ───────────────────────────────────────────────────────────
        for commit in pr.get("commits") or []:
            sha = commit.get("sha", "")
            if not sha:
                continue
            commit_eid = _commit_entity_id(src, ns, sha)
            entities.append(
                make_entity(
                    source=src,
                    entity_type=EntityType.COMMIT.value,
                    natural_key=f"{ns}@{sha}",
                    title=commit.get("message", "")[:200],
                    body=commit.get("message", ""),
                    metadata={
                        "sha":       sha,
                        "timestamp": commit.get("timestamp", ""),
                    },
                    related_ids=[pr_eid],
                )
            )

        # ── File hunks (one entity per changed file) ──────────────────────────
        for file_entry in pr.get("files") or []:
            # files is a list of strings like "path/to/file.py" or
            # could be a dict if the full file object is stored
            if isinstance(file_entry, str):
                fpath = file_entry
                patch = ""
            elif isinstance(file_entry, dict):
                fpath = file_entry.get("filename") or file_entry.get("path", "")
                patch = (file_entry.get("patch") or "")[:1500]
            else:
                continue

            if not fpath:
                continue

            entities.append(
                make_entity(
                    source=src,
                    entity_type=EntityType.FILE_HUNK.value,
                    natural_key=f"{ns}!{pnum}:{fpath}",
                    title=fpath,
                    body=patch,
                    metadata={
                        "pr_number": pnum,
                        "filepath":  fpath,
                    },
                    related_ids=[pr_eid],
                )
            )

    return entities, pr_ids


def _collect_jira_ticket(ticket: dict, pr_ids: list[str]) -> list[Entity]:
    """Extract a JIRA_TICKET entity + JIRA_COMMENTs."""
    entities: list[Entity] = []
    jid = ticket.get("id", "")
    if not jid:
        return entities

    body_parts = []
    if ticket.get("description"):
        body_parts.append(ticket["description"][:1000])
    if ticket.get("acceptance_criteria"):
        body_parts.append(f"ACCEPTANCE CRITERIA:\n{ticket['acceptance_criteria'][:600]}")

    ticket_eid = make_entity(
        source=SourceIntegration.JIRA.value,
        entity_type=EntityType.JIRA_TICKET.value,
        natural_key=f"jira:{jid}",
        title=f"{jid}: {ticket.get('summary', '')}",
        body="\n\n".join(body_parts),
        metadata={
            "jira_id":    jid,
            "issue_type": ticket.get("issue_type", ""),
            "status":     ticket.get("status", ""),
            "priority":   ticket.get("priority", ""),
        },
        related_ids=pr_ids,
    )
    entities.append(ticket_eid)

    # Comments
    for i, comment in enumerate((ticket.get("comments") or [])[:10]):
        if not comment or not comment.strip():
            continue
        entities.append(
            make_entity(
                source=SourceIntegration.JIRA.value,
                entity_type=EntityType.JIRA_COMMENT.value,
                natural_key=f"jira:{jid}:comment:{i}",
                title=f"Comment on {jid}",
                body=comment[:500],
                related_ids=[ticket_eid.id],
            )
        )

    return entities


def _collect_linear_issue(issue: dict, pr_ids: list[str]) -> list[Entity]:
    """Extract a LINEAR_ISSUE entity."""
    lid = issue.get("id", "")
    if not lid:
        return []

    body_parts = []
    if issue.get("description"):
        body_parts.append(issue["description"][:1000])
    if issue.get("acceptance_criteria"):
        body_parts.append(f"ACCEPTANCE CRITERIA:\n{issue['acceptance_criteria'][:600]}")
    if issue.get("parent_context"):
        body_parts.append(f"PARENT CONTEXT:\n{issue['parent_context'][:400]}")

    return [
        make_entity(
            source=SourceIntegration.LINEAR.value,
            entity_type=EntityType.LINEAR_ISSUE.value,
            natural_key=f"linear:{lid}",
            title=f"{lid}: {issue.get('summary', '')}",
            body="\n\n".join(body_parts),
            metadata={
                "linear_id": lid,
                "priority":  issue.get("priority", ""),
                "status":    issue.get("status", ""),
            },
            related_ids=pr_ids,
        )
    ]


# ── ID helpers ────────────────────────────────────────────────────────────────

from scanner.pipeline.entities import make_entity_id  # noqa: E402


def _issue_entity_id(src: str, ns: str, number: int) -> str:
    return make_entity_id(src, EntityType.ISSUE.value, f"{ns}#{number}")


def _pr_entity_id(src: str, ns: str, number: int, pr_type: str) -> str:
    return make_entity_id(src, pr_type, f"{ns}!{number}")


def _commit_entity_id(src: str, ns: str, sha: str) -> str:
    return make_entity_id(src, EntityType.COMMIT.value, f"{ns}@{sha}")
