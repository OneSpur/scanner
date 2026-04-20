"""
Stage 3 — EntityStore: Qdrant-backed vector store.

Three deployment modes (controlled by env vars or constructor args):
  :memory:   — ephemeral, in-process (default; fastest for single-run analysis)
  path=...   — persistent local SQLite + HNSW on disk (dev / re-run dedup)
  url=...    — remote Qdrant server (production)

Search strategy:
  When fastembed is installed (pulled in via qdrant-client[fastembed]):
    → high-level client.add() / client.query() API with dense BGE-small embeddings
    → optionally adds BM25 sparse vectors when qdrant-client >= 1.9 supports it
  Otherwise:
    → falls back to filter-only scroll (no semantic search, still functional)
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

# ── macOS safety: prevent ONNX runtime from forking child processes ───────────
# fastembed's ONNX backend spawns threads/processes for inference. On macOS,
# combining asyncio + fork causes SIGABRT. Lock it to single-threaded mode
# before any fastembed import happens.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("RAYON_RS_NUM_CPUS", "1")

if TYPE_CHECKING:
    from scanner.pipeline.entities import Entity

logger = logging.getLogger(__name__)

COLLECTION  = "scanner_entities"
DENSE_MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, fully local, ~130 MB on first run
# BM25 sparse model disabled: its multiprocessing initializer crashes Python on
# macOS when called from an asyncio thread-pool executor (fork-safety issue).
# Dense-only search is sufficient for 1Spur Scanner's entity retrieval use-case.


class EntityStore:
    """
    Thin wrapper around QdrantClient.

    Exposes three operations:
      upsert(entities)               — embed + insert, idempotent by entity.id
      search(query, filters, top_k)  — hybrid semantic + BM25 search
      count()                        — number of stored points
    """

    def __init__(
        self,
        mode:  str         = "memory",
        path:  str | None  = None,
        url:   str | None  = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            raise ImportError(
                "qdrant-client is required for the knowledge graph pipeline.\n"
                "Install it with:  pip install 'qdrant-client[fastembed]'"
            )

        # ── connect ───────────────────────────────────────────────────────────
        if url:
            api_key = os.getenv("QDRANT_API_KEY")
            self._client = QdrantClient(url=url, api_key=api_key)
            logger.info(f"EntityStore connected to remote Qdrant at {url}")
        elif path:
            self._client = QdrantClient(path=path)
            logger.info(f"EntityStore using persistent Qdrant at {path}")
        else:
            self._client = QdrantClient(":memory:")
            logger.info("EntityStore using in-memory Qdrant")

        # ── configure embedding models ────────────────────────────────────────
        self._has_fastembed = False
        self._has_sparse    = False   # BM25 disabled — crashes Python on macOS
        try:
            self._client.set_model(DENSE_MODEL)
            self._has_fastembed = True
            logger.info(f"Dense embedding model: {DENSE_MODEL}")
        except Exception as exc:
            logger.warning(f"fastembed not available ({exc}) — storing without embeddings")

    # ── upsert ────────────────────────────────────────────────────────────────

    def upsert(self, entities: list[Entity]) -> int:
        """
        Embed and upsert entities.
        Qdrant's upsert is idempotent by point ID, so re-running is safe.
        Returns the number of entities successfully stored.
        """
        if not entities:
            return 0

        if self._has_fastembed:
            return self._upsert_with_fastembed(entities)
        else:
            return self._upsert_payload_only(entities)

    def _upsert_with_fastembed(self, entities: list[Entity]) -> int:
        """Use the high-level add() API which handles embedding internally."""
        try:
            self._client.add(
                collection_name=COLLECTION,
                documents=[e.embedding_text for e in entities],
                metadata=[e.to_metadata() for e in entities],
                ids=[e.id for e in entities],
                batch_size=64,
            )
            logger.info(f"Upserted {len(entities)} entities (with embeddings)")
            return len(entities)
        except Exception as exc:
            logger.error(f"Qdrant upsert failed: {exc}")
            # Try one-by-one to at least store what we can
            count = 0
            for e in entities:
                try:
                    self._client.add(
                        collection_name=COLLECTION,
                        documents=[e.embedding_text],
                        metadata=[e.to_metadata()],
                        ids=[e.id],
                    )
                    count += 1
                except Exception:
                    pass
            return count

    def _upsert_payload_only(self, entities: list[Entity]) -> int:
        """
        Fallback: store entities with zero-vectors when fastembed is absent.
        Search degrades to filter-only scroll but the pipeline still runs.
        """
        try:
            from qdrant_client import QdrantClient, models
            # Ensure collection exists
            existing = {c.name for c in self._client.get_collections().collections}
            if COLLECTION not in existing:
                self._client.create_collection(
                    collection_name=COLLECTION,
                    vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
                )
            points = [
                models.PointStruct(
                    id=e.id,
                    vector=[0.0],
                    payload=e.to_metadata(),
                )
                for e in entities
            ]
            self._client.upsert(collection_name=COLLECTION, points=points)
            logger.info(f"Stored {len(entities)} entities (payload-only, no embeddings)")
            return len(entities)
        except Exception as exc:
            logger.error(f"Payload-only upsert failed: {exc}")
            return 0

    # ── search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query:               str,
        source_filter:       str | None = None,
        entity_type_filter:  str | None = None,
        top_k:               int        = 10,
    ) -> list[dict]:
        """
        Semantic search over stored entities.

        Returns a list of payload dicts (each is Entity.to_metadata()), sorted
        by relevance.  Falls back to filter-scroll if fastembed is unavailable.
        """
        if not query.strip():
            return []

        if self._has_fastembed:
            return self._semantic_search(query, source_filter, entity_type_filter, top_k)
        else:
            return self._filter_scroll(source_filter, entity_type_filter, top_k)

    def _build_filter(
        self,
        source_filter:      str | None,
        entity_type_filter: str | None,
    ):
        """Build a Qdrant Filter from optional field constraints."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        conditions = []
        if source_filter:
            conditions.append(
                FieldCondition(key="source", match=MatchValue(value=source_filter))
            )
        if entity_type_filter:
            conditions.append(
                FieldCondition(key="entity_type", match=MatchValue(value=entity_type_filter))
            )
        return Filter(must=conditions) if conditions else None

    def _semantic_search(
        self,
        query:               str,
        source_filter:       str | None,
        entity_type_filter:  str | None,
        top_k:               int,
    ) -> list[dict]:
        try:
            results = self._client.query(
                collection_name=COLLECTION,
                query_text=query,
                query_filter=self._build_filter(source_filter, entity_type_filter),
                limit=top_k,
            )
            return [r.metadata for r in results if r.metadata]
        except Exception as exc:
            logger.warning(f"Semantic search failed ({exc}), falling back to scroll")
            return self._filter_scroll(source_filter, entity_type_filter, top_k)

    def _filter_scroll(
        self,
        source_filter:      str | None,
        entity_type_filter: str | None,
        top_k:              int,
    ) -> list[dict]:
        try:
            points, _ = self._client.scroll(
                collection_name=COLLECTION,
                scroll_filter=self._build_filter(source_filter, entity_type_filter),
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
            return [p.payload for p in points if p.payload]
        except Exception as exc:
            logger.warning(f"Filter scroll failed: {exc}")
            return []

    # ── utilities ─────────────────────────────────────────────────────────────

    def count(self) -> int:
        """Number of points in the collection (0 if collection doesn't exist yet)."""
        try:
            return self._client.count(collection_name=COLLECTION).count
        except Exception:
            return 0

    def get_all(
        self,
        source_filter:      str | None = None,
        entity_type_filter: str | None = None,
    ) -> list[dict]:
        """
        Retrieve all matching payloads via paginated scroll.
        Use sparingly — mainly for the linker which needs the full entity graph.
        """
        results: list[dict] = []
        offset = None
        while True:
            try:
                points, next_offset = self._client.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=self._build_filter(source_filter, entity_type_filter),
                    limit=1000,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                break
            results.extend(p.payload for p in points if p.payload)
            if next_offset is None:
                break
            offset = next_offset
        return results
