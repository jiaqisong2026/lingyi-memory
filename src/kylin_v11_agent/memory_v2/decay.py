# -*- coding: utf-8 -*-
"""Differential decay model for semantic memories.

Instead of fixed TTL, each memory has a per-item decay rate modulated by:
  λ = λ_base × f_frequency × f_recency × f_semantic_density

This implements the FadeMem (arXiv:2601.18642) principle:
"memories that are used more often decay slower."
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional


class DifferentialDecay:
    """Computes and applies differential decay rates to semantic memories."""

    def __init__(self, base_lambda: float = 0.01):
        self.base_lambda = base_lambda

    def compute_decay_rate(
        self,
        access_count: int = 0,
        last_accessed: float = 0.0,
        created_at: float = 0.0,
        importance: float = 0.5,
        tag_count: int = 0,
    ) -> float:
        """Compute the effective decay rate for a memory item.

        Higher access count → slower decay (reinforcement).
        More recent access → slower decay (freshness).
        Higher importance → slower decay (salience).
        More tags → slower decay (semantic density).
        """
        now = time.time()
        age_days = max((now - created_at) / 86400.0, 0.01)

        # f_frequency: log-scaled access reinforcement
        # 0 accesses → factor=1.0 (normal decay)
        # 10+ accesses → factor≈0.3 (70% slower)
        # 100+ accesses → factor≈0.1 (90% slower)
        f_frequency = 1.0 / (1.0 + math.log1p(access_count) * 0.5)

        # f_recency: exponential decay based on last access
        # accessed today → factor≈1.0
        # accessed 30 days ago → factor≈2.0
        # accessed 365 days ago → factor≈5.0
        recency_days = max((now - last_accessed) / 86400.0, 0.0)
        f_recency = 1.0 + recency_days * 0.1

        # f_semantic_density: more tags = more stable
        f_density = 1.0 / (1.0 + tag_count * 0.2)

        # f_importance: higher importance = slower decay
        f_importance = 1.0 - importance * 0.5

        effective_lambda = (
            self.base_lambda
            * f_frequency
            * f_recency
            * f_density
            * f_importance
        )

        return max(effective_lambda, 0.0001)  # floor at 0.0001

    def should_decay(self, item: Dict[str, Any]) -> bool:
        """Check if a memory item should be pruned based on its decay."""
        decay_rate = self.compute_decay_rate(
            access_count=item.get("access_count", 0),
            last_accessed=item.get("last_accessed", 0.0),
            created_at=item.get("created_at", 0.0),
            importance=item.get("importance", 0.5),
            tag_count=len(item.get("tags", [])),
        )
        now = time.time()
        age_days = (now - item.get("created_at", now)) / 86400.0
        # A memory is "decayed" when its accumulated decay exceeds 1.0
        accumulated_decay = decay_rate * age_days
        return accumulated_decay > 1.0

    def prune_expired(self, conn: Any) -> int:
        """Remove memories whose accumulated decay exceeds threshold.

        Returns the number of pruned items.
        """
        c = conn.cursor()
        # Get all semantic memories with their metadata
        rows = c.execute("""
            SELECT id, access_count, last_accessed, created_at,
                   importance, tags
            FROM semantic_memories
        """).fetchall()

        to_prune = []
        for row in rows:
            item = {
                "id": row[0],
                "access_count": row[1] or 0,
                "last_accessed": row[2] or 0.0,
                "created_at": row[3] or 0.0,
                "importance": row[4] or 0.5,
                "tags": [],
            }
            if row[5]:
                import json
                try:
                    item["tags"] = json.loads(row[5])
                except Exception:
                    pass

            if self.should_decay(item):
                to_prune.append(row[0])

        if to_prune:
            placeholders = ",".join(["?"] * len(to_prune))
            c.execute(f"DELETE FROM semantic_memories WHERE id IN ({placeholders})", to_prune)
            conn.commit()

        return len(to_prune)

    def get_decay_stats(self, conn: Any) -> Dict[str, Any]:
        """Return decay statistics across all memories."""
        c = conn.cursor()
        rows = c.execute("""
            SELECT id, access_count, last_accessed, created_at,
                   importance, tags
            FROM semantic_memories
        """).fetchall()

        stats = {"total": len(rows), "high_decay": 0, "low_decay": 0}
        for row in rows:
            item = {
                "id": row[0],
                "access_count": row[1] or 0,
                "last_accessed": row[2] or 0.0,
                "created_at": row[3] or 0.0,
                "importance": row[4] or 0.5,
                "tags": [],
            }
            if row[5]:
                import json
                try:
                    item["tags"] = json.loads(row[5])
                except Exception:
                    pass

            rate = self.compute_decay_rate(**item)
            if rate > 0.05:
                stats["high_decay"] += 1
            else:
                stats["low_decay"] += 1

        return stats
