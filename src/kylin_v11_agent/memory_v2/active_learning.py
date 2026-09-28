# -*- coding: utf-8 -*-
"""Active learning loop for Layer 2 cognition.

Cycle: Observe → Hypothesize → Verify → Consolidate

When the agent encounters a recall gap (no relevant memories found),
the active learner generates a "knowledge gap report" and tracks
whether subsequent tasks help fill that gap.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ActiveLearner:
    """Monitors recall gaps and drives knowledge acquisition."""

    def __init__(self, conn: Any):
        self.conn = conn
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_gaps (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'semantic',
                first_observed REAL NOT NULL,
                last_observed REAL NOT NULL,
                observation_count INTEGER DEFAULT 0,
                filled BOOLEAN DEFAULT 0,
                filled_at REAL DEFAULT NULL,
                evidence_ids TEXT DEFAULT '[]'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_filled ON knowledge_gaps(filled)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_cat ON knowledge_gaps(category)")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Observation: detect recall gaps
    # ------------------------------------------------------------------

    def observe_gap(
        self,
        query: str,
        category: str = "semantic",
        top_k: int = 5,
        hits: int = 0,
    ) -> Optional[str]:
        """Record a recall gap when no relevant memories are found.

        Returns the gap ID if a new gap was created or updated.
        """
        if hits > 0:
            return None  # No gap — memories were found

        c = self.conn.cursor()
        now = time.time()

        # Check if this query pattern already has a gap record
        existing = c.execute(
            "SELECT id FROM knowledge_gaps WHERE query = ? AND filled = 0",
            (query,),
        ).fetchone()

        if existing:
            gap_id = existing[0]
            c.execute(
                "UPDATE knowledge_gaps SET observation_count = observation_count + 1, "
                "last_observed = ? WHERE id = ?",
                (now, gap_id),
            )
        else:
            gap_id = f"gap_{uuid.uuid4().hex[:12]}"
            c.execute(
                "INSERT INTO knowledge_gaps "
                "(id, query, category, first_observed, last_observed, observation_count) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (gap_id, query, category, now, now),
            )

        self.conn.commit()
        logger.info("Knowledge gap observed: %s (count=%d)", query[:50],
                     c.execute("SELECT observation_count FROM knowledge_gaps WHERE id=?", (gap_id,)).fetchone()[0])
        return gap_id

    # ------------------------------------------------------------------
    # Hypothesis: generate gap reports
    # ------------------------------------------------------------------

    def get_gap_report(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Generate a report of unfilled knowledge gaps.

        Prioritizes gaps with high observation counts (repeatedly encountered).
        """
        c = self.conn.cursor()
        rows = c.execute(
            "SELECT id, query, category, observation_count, first_observed, last_observed "
            "FROM knowledge_gaps WHERE filled = 0 "
            "ORDER BY observation_count DESC, last_observed DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "gap_id": r[0],
                "query": r[1],
                "category": r[2],
                "observation_count": r[3],
                "first_observed": r[4],
                "last_observed": r[5],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Verification: mark gaps as filled
    # ------------------------------------------------------------------

    def fill_gap(
        self,
        gap_id: str,
        evidence_ids: Optional[List[str]] = None,
    ) -> bool:
        """Mark a knowledge gap as filled after verification."""
        c = self.conn.cursor()
        now = time.time()
        c.execute(
            "UPDATE knowledge_gaps SET filled = 1, filled_at = ?, "
            "evidence_ids = ? WHERE id = ?",
            (now, json.dumps(evidence_ids or []), gap_id),
        )
        self.conn.commit()
        logger.info("Knowledge gap filled: %s", gap_id)
        return True

    # ------------------------------------------------------------------
    # Consolidation: periodic review
    # ------------------------------------------------------------------

    def consolidate(self) -> Dict[str, Any]:
        """Run a consolidation cycle on knowledge gaps.

        Returns stats about gap resolution.
        """
        c = self.conn.cursor()

        total_gaps = c.execute(
            "SELECT COUNT(*) FROM knowledge_gaps"
        ).fetchone()[0]

        filled = c.execute(
            "SELECT COUNT(*) FROM knowledge_gaps WHERE filled = 1"
        ).fetchone()[0]

        persistent = c.execute(
            "SELECT COUNT(*) FROM knowledge_gaps WHERE filled = 0 AND observation_count >= 3"
        ).fetchone()[0]

        return {
            "total_gaps": total_gaps,
            "filled": filled,
            "unfilled": total_gaps - filled,
            "persistent": persistent,  # gaps seen 3+ times
        }

    # ------------------------------------------------------------------
    # Integration with MemorySystemV2
    # ------------------------------------------------------------------

    def check_recall_gap(
        self,
        query: str,
        category: str = "semantic",
        top_k: int = 5,
        hits: int = 0,
    ) -> Optional[str]:
        """Convenience method: observe gap if recall returned nothing."""
        return self.observe_gap(query, category, top_k, hits)
