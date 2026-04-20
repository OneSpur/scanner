"""
Core entity models for the 1Spur Scanner knowledge graph pipeline.

An Entity is any referenceable unit of meaning:
  - code:  PR, commit, file hunk, function, class, import
  - spec:  GitHub issue, Jira ticket, Linear issue, acceptance criterion
  - comm:  Slack message/thread, PR review comment
  - doc:   uploaded spec document, section

Entities get deterministic UUIDs from SHA256(source:type:natural_key),
making them stable across pipeline re-runs and dedup-friendly.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import Enum


# ── Enumerations ──────────────────────────────────────────────────────────────

class SourceIntegration(str, Enum):
    GITHUB  = "github"
    GITLAB  = "gitlab"
    JIRA    = "jira"
    LINEAR  = "linear"
    SLACK   = "slack"
    UPLOAD  = "upload"


class EntityType(str, Enum):
    # ── Code / repo ───────────────────────────────────────────────────────────
    PULL_REQUEST          = "pull_request"
    MERGE_REQUEST         = "merge_request"
    COMMIT                = "commit"
    FILE_HUNK             = "file_hunk"
    # ── Code atoms (extracted by atomizer) ───────────────────────────────────
    FUNCTION              = "function"
    CLASS                 = "class"
    IMPORT                = "import"
    # ── GitHub / GitLab issues ────────────────────────────────────────────────
    ISSUE                 = "issue"
    ISSUE_COMMENT         = "issue_comment"
    ACCEPTANCE_CRITERION  = "acceptance_criterion"
    # ── Jira ──────────────────────────────────────────────────────────────────
    JIRA_TICKET           = "jira_ticket"
    JIRA_COMMENT          = "jira_comment"
    # ── Linear ────────────────────────────────────────────────────────────────
    LINEAR_ISSUE          = "linear_issue"
    LINEAR_COMMENT        = "linear_comment"
    # ── Slack ─────────────────────────────────────────────────────────────────
    SLACK_THREAD          = "slack_thread"
    # ── Uploaded spec docs ────────────────────────────────────────────────────
    SPEC_DOCUMENT         = "spec_document"


# ── Helpers ───────────────────────────────────────────────────────────────────

# Fixed UUID namespace for entity ID generation (RFC 4122 DNS namespace)
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def make_entity_id(source: str, entity_type: str, natural_key: str) -> str:
    """Return a stable UUID string for the given (source, type, key) triple."""
    return str(uuid.uuid5(_NS, f"{source}:{entity_type}:{natural_key}"))


def content_hash(text: str) -> str:
    """Short SHA256 fingerprint (16 hex chars) for change detection."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── Core model ────────────────────────────────────────────────────────────────

@dataclass
class Entity:
    """
    A single node in the 1Spur Scanner knowledge graph.

    Fields
    ------
    id            Deterministic UUID string (make_entity_id)
    source        SourceIntegration value string
    entity_type   EntityType value string
    natural_key   Human-readable stable key, e.g. "octocat/hello-world#42"
    title         Short label (PR/issue title, function name, …)
    body          Main prose content (description, diff hunk, log body, …)
    metadata      Arbitrary extra fields (author, url, merged_at, …)
    content_hash  16-char SHA256 of (title+body) for dedup
    related_ids   List of other entity IDs this entity directly references
    """

    id:           str
    source:       str
    entity_type:  str
    natural_key:  str
    title:        str  = ""
    body:         str  = ""
    metadata:     dict = field(default_factory=dict)
    content_hash: str  = ""
    related_ids:  list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash(self.title + "\n" + self.body)

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def embedding_text(self) -> str:
        """Text fed to the embedding model — title + body, capped at 4096 chars."""
        parts = [p for p in (self.title, self.body) if p]
        return " ".join(parts)[:4096]

    def to_metadata(self) -> dict:
        """
        Flat dict stored as Qdrant payload.

        Qdrant payload values must be scalars or lists of scalars; nested dicts
        are not indexed.  We flatten `metadata` with a 'meta_' prefix and cap
        string lengths to keep payload sizes reasonable.
        """
        flat: dict = {
            "id":           self.id,
            "source":       self.source,
            "entity_type":  self.entity_type,
            "natural_key":  self.natural_key,
            "title":        self.title[:400],
            "body":         self.body[:2000],
            "content_hash": self.content_hash,
            "related_ids":  self.related_ids,
        }
        for k, v in (self.metadata or {}).items():
            if isinstance(v, (str, int, float, bool)):
                flat[f"meta_{k}"] = str(v)[:400] if isinstance(v, str) else v
        return flat


# ── Factory helpers ───────────────────────────────────────────────────────────

def make_entity(
    source: str,
    entity_type: str,
    natural_key: str,
    title: str = "",
    body: str = "",
    metadata: dict | None = None,
    related_ids: list | None = None,
) -> Entity:
    """Convenience constructor — computes the deterministic ID for you."""
    eid = make_entity_id(source, entity_type, natural_key)
    return Entity(
        id=eid,
        source=source,
        entity_type=entity_type,
        natural_key=natural_key,
        title=title,
        body=body,
        metadata=metadata or {},
        related_ids=related_ids or [],
    )
