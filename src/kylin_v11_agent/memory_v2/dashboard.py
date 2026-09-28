# -*- coding: utf-8 -*-
"""Read-only inspection API for MemorySystemV2.

Useful for debugging, monitoring, and surfacing state to a higher-level
planner or human operator. All methods are read-only — they never mutate
the underlying database.

Typical usage:
    from src.memory_v2 import MemorySystemV2
    from src.memory_v2.dashboard import MemoryDashboard

    mem = MemorySystemV2()
    d = MemoryDashboard(mem)
    print(d.summary())             # one-shot health snapshot
    print(d.get_top_memories(5))   # top-5 by importance
    print(d.get_recent_gaps(5))    # top-5 unfilled knowledge gaps
    print(d.get_kg_summary())      # node/edge/relation counts
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .core import MemorySystemV2
from .knowledge_graph import KnowledgeGraph
from .active_learning import ActiveLearner

logger = logging.getLogger(__name__)


class MemoryDashboard:
    """Thin read-only facade over MemorySystemV2 + L2 components."""

    def __init__(self, memory: MemorySystemV2):
        self.memory = memory
        self.kg = KnowledgeGraph(memory.conn)
        self.al = ActiveLearner(memory.conn)

    # ---- memories ----------------------------------------------------

    def get_top_memories(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return top memories by importance, then recency."""
        c = self.memory.conn.cursor()
        rows = c.execute(
            "SELECT id, content, category, importance, access_count, last_accessed "
            "FROM semantic_memories "
            "ORDER BY importance DESC, last_accessed DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0], "content": r[1], "category": r[2],
                "importance": r[3], "access_count": r[4],
                "last_accessed": r[5],
            }
            for r in rows
        ]

    def get_decay_stats(self) -> Dict[str, Any]:
        """Distribution of decay_rate, importance, and last_accessed."""
        c = self.memory.conn.cursor()
        total = c.execute("SELECT COUNT(*) FROM semantic_memories").fetchone()[0]
        if total == 0:
            return {"total": 0}
        avg_imp = c.execute(
            "SELECT AVG(importance) FROM semantic_memories"
        ).fetchone()[0] or 0.0
        avg_decay = c.execute(
            "SELECT AVG(decay_rate) FROM semantic_memories"
        ).fetchone()[0] or 0.0
        # Count how many memories have not been touched in 30+ days.
        cutoff = time.time() - 30 * 86400
        stale = c.execute(
            "SELECT COUNT(*) FROM semantic_memories WHERE last_accessed < ?",
            (cutoff,),
        ).fetchone()[0]
        return {
            "total": total,
            "avg_importance": round(avg_imp, 4),
            "avg_decay_rate": round(avg_decay, 4),
            "stale_30d": stale,
        }

    # ---- knowledge graph ---------------------------------------------

    def get_kg_summary(self) -> Dict[str, Any]:
        """Wrap KnowledgeGraph.get_graph_stats() with extra pruning hint."""
        stats = self.kg.get_graph_stats()
        stats["weak_edge_candidates"] = self._count_weak_edges()
        return stats

    def _count_weak_edges(self, threshold: float = 0.3) -> int:
        c = self.memory.conn.cursor()
        return c.execute(
            "SELECT COUNT(*) FROM knowledge_graph WHERE strength < ?",
            (threshold,),
        ).fetchone()[0]

    # ---- knowledge gaps ----------------------------------------------

    def get_recent_gaps(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return top unfilled knowledge gaps (delegates to ActiveLearner)."""
        return self.al.get_gap_report(limit=limit)

    def get_gap_summary(self) -> Dict[str, Any]:
        """Aggregate stats from the active learner."""
        return self.al.consolidate()

    def get_conflicts(self, limit: int = 20) -> List[Dict[str, Any]]:
        # Surface knowledge-graph conflicts (TOKI-style) so the
        # operator can see when the agent holds contradictory facts.
        return self.kg.detect_conflicts()[:limit]

    # ---- recall diagnostics ------------------------------------------

    def get_recall_stats(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        """Run a recall pass and report ranking diagnostics.

        Useful for tuning the RRF k parameter and detecting cases where
        one channel dominates the others.
        """
        results = self.memory.recall(query, top_k=top_k)
        if not results:
            return {"query": query, "hits": 0, "channels": {}, "top_score": 0.0}
        # Group by category to see what came back.
        by_cat: Dict[str, int] = {}
        for r in results:
            cat = r.get("category", "unknown")
            by_cat[cat] = by_cat.get(cat, 0) + 1
        top_score = max((r.get("score", 0.0) for r in results), default=0.0)
        return {
            "query": query,
            "hits": len(results),
            "by_category": by_cat,
            "top_score": round(float(top_score), 4),
        }

    # ---- one-shot summary --------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """All-in-one health snapshot for logging / dashboards."""
        return {
            "memory": self.get_decay_stats(),
            "kg": self.get_kg_summary(),
            "gaps": self.get_gap_summary(),
            "pending_in_agent": len(getattr(self.memory, "pending_gaps", []) or []),
        }