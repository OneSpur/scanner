"""
1Spur Scanner Knowledge Graph Pipeline
═══════════════════════════════════
A 4-stage ingestion pipeline that indexes all integration data into a
local Qdrant vector store, enabling richer semantic retrieval during
drift analysis.

Stages
──────
1. Collector  — extract Entity objects from repo_data (PRs, issues, Jira, Slack…)
2. Atomizer   — deep extraction: code atoms (fn/class) + acceptance criteria
3. Embedder   — deduplicate + batch-embed into Qdrant (fastembed BGE-small)
4. Linker     — NetworkX graph construction + Louvain community detection

After the four stages the Enricher queries Qdrant and injects `qdrant_context`
into each pair dict, which analyzer.py appends to its LLM prompts.

Usage (from main.py)
────────────────────
    store = await run_pipeline(repo_data, platform, extra_context, log)

The pipeline is completely optional: if qdrant-client is not installed it
returns None and 1Spur Scanner falls back to its original (non-graph) behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

# Must be set before any fastembed/ONNX import to prevent fork-safety crashes
# on macOS when running inside asyncio thread-pool executors.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")

logger = logging.getLogger(__name__)


async def run_pipeline(
    repo_data:     dict,
    platform:      str                    = "github",
    extra_context: str | None             = None,
    log:           Callable[[str], None] | None = None,
) -> "EntityStore | None":
    """
    Orchestrate all 4 pipeline stages asynchronously.

    Returns an initialised EntityStore on success, or None if the pipeline
    could not run (missing deps, empty data, …).  The store is later passed
    to the Enricher by run_analysis().

    Runs in a thread-pool executor so it doesn't block the FastAPI event loop
    during the CPU-bound embedding work.
    """
    try:
        loop = asyncio.get_event_loop()
        store = await loop.run_in_executor(
            None,
            lambda: _run_sync(repo_data, platform, extra_context, log),
        )
        return store
    except Exception as exc:
        _log_safe(log, f"⚠ Knowledge graph pipeline failed (non-fatal): {exc}")
        logger.exception("Pipeline error")
        return None


def _run_sync(
    repo_data:     dict,
    platform:      str,
    extra_context: str | None,
    log:           Callable[[str], None] | None,
) -> "EntityStore":
    """
    Synchronous pipeline runner (called inside a thread-pool executor).
    """
    from scanner.pipeline.store    import EntityStore
    from scanner.pipeline.collector import collect
    from scanner.pipeline.atomizer  import atomize
    from scanner.pipeline.embedder  import embed
    from scanner.pipeline.linker    import link
    from scanner.pipeline.enricher  import enrich_pairs

    # ── Initialise store ──────────────────────────────────────────────────────
    qdrant_url  = os.getenv("QDRANT_URL")
    qdrant_path = os.getenv("QDRANT_PATH")

    if qdrant_url:
        store = EntityStore(url=qdrant_url)
    elif qdrant_path:
        store = EntityStore(path=qdrant_path)
    else:
        store = EntityStore()  # :memory: default

    # ── Stage 1: Collect ─────────────────────────────────────────────────────
    _log_safe(log, "Knowledge graph: collecting entities…")
    base_entities = collect(repo_data, platform=platform, extra_context=extra_context)

    if not base_entities:
        _log_safe(log, "Knowledge graph: no entities found — skipping pipeline")
        return store

    # ── Stage 2: Atomize ──────────────────────────────────────────────────────
    _log_safe(log, f"Knowledge graph: atomizing {len(base_entities)} entities…")
    atom_entities = atomize(base_entities)
    all_entities  = base_entities + atom_entities

    _log_safe(log, f"Knowledge graph: {len(all_entities)} entities total "
              f"({len(base_entities)} base + {len(atom_entities)} atoms)")

    # ── Stage 3: Embed ────────────────────────────────────────────────────────
    stored = embed(all_entities, store, log=log)

    if stored == 0:
        _log_safe(log, "Knowledge graph: embedding produced no stored entities")
        return store

    # ── Stage 4: Link ─────────────────────────────────────────────────────────
    _log_safe(log, "Knowledge graph: building entity graph…")
    community_map = link(store, log=log)

    # ── Enrich pairs ─────────────────────────────────────────────────────────
    _log_safe(log, "Knowledge graph: enriching analysis pairs…")
    enrich_pairs(repo_data, store, community_map=community_map)

    enriched = sum(1 for p in (repo_data.get("pairs") or []) if p.get("qdrant_context"))
    _log_safe(log, f"Knowledge graph: {enriched}/{len(repo_data.get('pairs') or [])} "
              f"pairs enriched with graph context")

    return store


def _log_safe(log: Callable[[str], None] | None, msg: str) -> None:
    """Call log callback if provided, otherwise write to Python logger."""
    if log:
        log(msg)
    else:
        logger.info(msg)


# Re-export for convenience
from scanner.pipeline.store    import EntityStore   # noqa: F401, E402
from scanner.pipeline.entities import Entity        # noqa: F401, E402
