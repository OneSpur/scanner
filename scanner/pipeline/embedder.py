"""
Stage 3 — Embedder: batch-embed all entities and persist them in Qdrant.

Responsibilities
────────────────
• Accept the merged entity list (Collector output + Atomizer additions)
• Deduplicate by entity ID before sending to the store
• Call EntityStore.upsert() in configurable batches
• Log progress through the caller's log function

Deduplication
─────────────
The Collector + Atomizer can produce the same entity ID more than once if
the same natural key appears in multiple pairs (e.g. a Jira ticket linked to
two different PRs).  We dedup by ID, keeping the first occurrence (entities
are deterministic, so any occurrence is equivalent).

Batching
────────
fastembed downloads the BGE-small model (~130 MB) on first use and caches it
in ~/.cache/huggingface.  Subsequent runs are instant.  We default to batches
of 128 entities so the embedding loop shows incremental progress.
"""

from __future__ import annotations

import logging
from typing import Callable

from scanner.pipeline.entities import Entity
from scanner.pipeline.store import EntityStore

logger = logging.getLogger(__name__)

_DEFAULT_BATCH = 128


def embed(
    entities:   list[Entity],
    store:      EntityStore,
    log:        Callable[[str], None] | None = None,
    batch_size: int                          = _DEFAULT_BATCH,
) -> int:
    """
    Deduplicate entities by ID, then upsert them into the EntityStore in batches.

    Parameters
    ----------
    entities    Combined output of Collector + Atomizer
    store       An initialised EntityStore
    log         Optional progress callback (same signature as main.py's `log`)
    batch_size  Number of entities per Qdrant upsert call

    Returns
    -------
    Total number of entities successfully stored.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            logger.info(msg)

    if not entities:
        _log("Embedder: no entities to store")
        return 0

    # ── Deduplication ─────────────────────────────────────────────────────────
    seen:   set[str]    = set()
    unique: list[Entity] = []
    for e in entities:
        if e.id not in seen:
            seen.add(e.id)
            unique.append(e)

    dupes = len(entities) - len(unique)
    if dupes:
        logger.debug(f"Embedder: dropped {dupes} duplicate entities")

    total = len(unique)
    _log(f"Embedding {total} entities into knowledge graph…")

    # ── Batch upsert ──────────────────────────────────────────────────────────
    stored = 0
    for i in range(0, total, batch_size):
        batch  = unique[i : i + batch_size]
        n      = store.upsert(batch)
        stored += n
        pct    = int((i + len(batch)) / total * 100)
        logger.debug(f"Embedder: {i + len(batch)}/{total} ({pct}%)")

    _log(f"Knowledge graph: {stored}/{total} entities indexed")
    return stored
