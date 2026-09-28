# -*- coding: utf-8 -*-
"""RRF (Reciprocal Rank Fusion) hybrid retrieval.

Three parallel search channels, fused by RRF scoring:
  1. FTS5 full-text search (best for scale)
  2. BM25 in-memory scoring (CJK-aware, synonym expansion)
  3. SQL importance/recency fallback (no-query scenario)

Reference: Cormack et al., "Reciprocal Rank Fusion outperforms
           Condorcet and individual Rank Learning Methods" (SIGIR 2009).
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RRFRetriever:
    """RRF hybrid retriever for MemorySystemV2."""

    def __init__(self, conn: Any, fts5_available: bool, rrf_k: int = 60,
                 bm25_scorer: Any = None):
        self.conn = conn
        self._fts5_available = fts5_available
        self.rrf_k = rrf_k
        self._bm25_scorer = bm25_scorer

    def search(
        self,
        query: str,
        top_k: int = 5,
        level: Any = None,
        category: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute RRF fusion across three channels."""
        if not query or not query.strip():
            return self._importance_fallback(top_k, category, user_id)

        # Channel 1: FTS5
        fts5_scores = self._channel_fts5(query, top_k * 10)
        # Channel 2: BM25
        bm25_scores = self._channel_bm25(query, top_k * 10)
        # Channel 3: SQL keyword match (lightweight)
        sql_scores = self._channel_sql_keyword(query, top_k * 10)

        # Fuse via RRF
        fused = self._rrf_fuse([fts5_scores, bm25_scores, sql_scores], top_k)

        if not fused:
            return self._importance_fallback(top_k, category, user_id)

        # Fetch full records for top candidates
        candidate_ids = [doc_id for doc_id, _ in fused[:top_k * 2]]
        return self._fetch_records(candidate_ids, fused, top_k, category, user_id)

    # ------------------------------------------------------------------
    # Channel 1: FTS5
    # ------------------------------------------------------------------

    def _channel_fts5(self, query: str, limit: int = 50) -> Dict[str, float]:
        if not self._fts5_available:
            return {}
        try:
            c = self.conn.cursor()
            rows = c.execute(
                "SELECT id, rank FROM semantic_fts "
                "WHERE semantic_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return {r[0]: -r[1] for r in rows}  # FTS5 rank is negative
        except Exception as e:
            logger.debug("FTS5 channel failed: %s", e)
            return {}

    # ------------------------------------------------------------------
    # Channel 2: BM25 (inline, no external dependency)
    # ------------------------------------------------------------------

    def _channel_bm25(self, query: str, limit: int = 50) -> Dict[str, float]:
        """Inline BM25 scorer — avoids rebuilding the full index on every query."""
        from .bm25_compat import tokenize_with_expansion
        try:
            c = self.conn.cursor()
            rows = c.execute(
                "SELECT id, content FROM semantic_memories"
            ).fetchall()
            if not rows:
                return {}

            # Build doc list
            docs: Dict[str, List[str]] = {}
            doc_lens: Dict[str, int] = {}
            doc_freqs: Dict[str, int] = {}
            total_len = 0

            for doc_id, content in rows:
                tokens = tokenize_with_expansion(content)
                docs[doc_id] = tokens
                doc_lens[doc_id] = len(tokens)
                total_len += len(tokens)
                for t in set(tokens):
                    doc_freqs[t] = doc_freqs.get(t, 0) + 1

            num_docs = len(docs)
            avg_dl = total_len / num_docs if num_docs > 0 else 1.0
            k1, b = 1.5, 0.75

            query_tokens = tokenize_with_expansion(query)
            scores: Dict[str, float] = {}

            for doc_id, doc_tokens in docs.items():
                score = 0.0
                dl = len(doc_tokens)
                tf_counter: Dict[str, int] = {}
                for t in doc_tokens:
                    tf_counter[t] = tf_counter.get(t, 0) + 1

                for qt in query_tokens:
                    tf = tf_counter.get(qt, 0)
                    df = doc_freqs.get(qt, 0)
                    if df == 0:
                        continue
                    idf = math.log((num_docs - df + 0.5) / (df + 0.5) + 1.0)
                    tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1)))
                    score += idf * tf_norm

                if score > 0:
                    scores[doc_id] = score

            return dict(sorted(scores.items(), key=lambda x: -x[1])[:limit])
        except Exception as e:
            logger.debug("BM25 channel failed: %s", e)
            return {}

    # ------------------------------------------------------------------
    # Channel 3: SQL keyword match
    # ------------------------------------------------------------------

    def _channel_sql_keyword(self, query: str, limit: int = 50) -> Dict[str, float]:
        """Lightweight SQL keyword matching for fast recall."""
        keywords = query.lower().split()
        if not keywords:
            return {}
        try:
            c = self.conn.cursor()
            conditions = []
            params = []
            for kw in keywords:
                conditions.append("LOWER(content) LIKE ?")
                params.append(f"%{kw}%")
            sql = f"SELECT id, content FROM semantic_memories WHERE {' OR '.join(conditions)} LIMIT ?"
            rows = c.execute(sql, params + [limit]).fetchall()
            scores: Dict[str, float] = {}
            for row_id, content in rows:
                score = sum(1 for kw in keywords if kw in content.lower())
                scores[row_id] = score
            return scores
        except Exception as e:
            logger.debug("SQL keyword channel failed: %s", e)
            return {}

    # ------------------------------------------------------------------
    # RRF Fusion
    # ------------------------------------------------------------------

    def _rrf_fuse(
        self,
        channels: List[Dict[str, float]],
        top_k: int,
    ) -> List[Tuple[str, float]]:
        """Fuse multiple ranked lists using Reciprocal Rank Fusion.

        RRF_score(doc) = Σ(1 / (k + rank_i(doc)))
        """
        rrf_scores: Dict[str, float] = {}
        for channel_scores in channels:
            for rank, (doc_id, _) in enumerate(channel_scores.items(), start=1):
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank)

        ranked = sorted(rrf_scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]

    # ------------------------------------------------------------------
    # Fallback: importance + recency
    # ------------------------------------------------------------------

    def _importance_fallback(
        self, top_k: int,
        category: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fallback when no query matches anything."""
        c = self.conn.cursor()
        sql = "SELECT id, content, category, importance, access_count, created_at, last_accessed, tags, metadata, session_id, user_id FROM semantic_memories WHERE 1=1"
        params = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY importance DESC, last_accessed DESC LIMIT ?"
        params.append(top_k)
        rows = c.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Record fetching
    # ------------------------------------------------------------------

    def _fetch_records(
        self, candidate_ids: List[str],
        fused_scores: List[Tuple[str, float]],
        top_k: int,
        category: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not candidate_ids:
            return []
        placeholders = ",".join(["?"] * len(candidate_ids))
        sql = f"SELECT id, content, category, importance, access_count, created_at, last_accessed, tags, metadata, session_id, user_id FROM semantic_memories WHERE id IN ({placeholders})"
        params = list(candidate_ids)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)

        rows = self.conn.cursor().execute(sql, params).fetchall()
        score_map = {doc_id: score for doc_id, score in fused_scores}

        scored_rows = []
        now = time.time()
        for row in rows:
            rid = row[0]
            rrf_score = score_map.get(rid, 0.0)
            importance = row[3] if len(row) > 3 else 0.5
            last_acc = row[6] if len(row) > 6 else now
            recency = 1.0 / (1.0 + (now - last_acc) / 86400)
            combined = rrf_score * 1.0 + importance * 3.0 + recency * 0.5
            scored_rows.append((combined, row))

        scored_rows.sort(key=lambda x: x[0], reverse=True)
        return [self._row_to_dict(r[1]) for r in scored_rows[:top_k]]

    @staticmethod
    def _row_to_dict(row: tuple) -> Dict[str, Any]:
        return {
            "id": row[0],
            "content": row[1],
            "category": row[2],
            "importance": row[3],
            "access_count": row[4],
            "created_at": row[5],
            "last_accessed": row[6],
            "tags": json.loads(row[7]) if row[7] else [],
            "metadata": json.loads(row[8]) if row[8] else {},
            "session_id": row[9] if len(row) > 9 else "",
            "user_id": row[10] if len(row) > 10 else "default",
        }
