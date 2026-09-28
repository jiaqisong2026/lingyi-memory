# -*- coding: utf-8 -*-
"""Knowledge graph for Layer 2 cognition.

Lightweight entity-relation graph built from semantic memories.
Supports conflict detection (TOKI arithmetic) and pattern discovery.

Nodes: entities (file paths, commands, tools, config items)
Edges: relations (modified, depends_on, causes, replaces)
Attributes: strength (based on co-occurrence), evidence_ids (supporting memories)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """Entity-relation knowledge graph derived from semantic memories."""

    RELATION_TYPES = {
        "modified", "depends_on", "causes", "replaces",
        "contains", "located_in", "runs_on", "listens_on",
        "installed_on", "configured_by", "failed_with",
    }

    def __init__(self, conn: Any):
        self.conn = conn
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_graph (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                strength REAL DEFAULT 1.0,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                evidence_ids TEXT DEFAULT '[]',
                PRIMARY KEY (source, target, relation)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_source ON knowledge_graph(source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_target ON knowledge_graph(target)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_relation ON knowledge_graph(relation)")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Entity extraction from memory content
    # ------------------------------------------------------------------

    @staticmethod
    def extract_entities(content: str) -> List[str]:
        """Extract entity-like tokens from memory content.

        Heuristics:
        - Paths: /etc/..., /var/..., /opt/...
        - Commands: single-word lowercase tokens that look like binaries
        - Config keys: key=value patterns
        """
        import re
        entities: Set[str] = set()

        # File paths
        for m in re.finditer(r'(/[a-zA-Z0-9_./-]+)', content):
            path = m.group(1)
            if len(path) > 3 and len(path) < 200:
                entities.add(path)

        # IP addresses and ports
        for m in re.finditer(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', content):
            entities.add(m.group(1))

        # Tool/command names (short lowercase tokens)
        for m in re.finditer(r'\b([a-z][a-z0-9_-]{1,20})\b', content.lower()):
            token = m.group(1)
            if token not in ("the", "and", "for", "not", "was", "has", "had",
                             "but", "with", "this", "that", "from", "been",
                             "were", "are", "its", "can", "will", "would"):
                entities.add(token)

        # CJK runs (consecutive Chinese characters) — length 2 to 12.
        # Common technical terms like nginx, docker, etc. are usually
        # written in English in this codebase, so CJK entities are
        # typically Chinese nouns (服务, 端口, 进程, etc.).
        for m in re.finditer(r'([\u4e00-\u9fff]{2,3})', content):
            entities.add(m.group(1))

        # Windows-style paths (C:\..., D:/...)
        for m in re.finditer(r"([A-Za-z]:[\\/][^\s\x22\x27]+)", content):
            entities.add(m.group(1))

        # URLs (http, https, ftp)
        for m in re.finditer(r'((?:https?|ftp)://[^\s]+)', content):
            entities.add(m.group(1))

        return sorted(entities)

    # ------------------------------------------------------------------
    # Edge construction
    # ------------------------------------------------------------------

    def add_edges_from_memory(
        self,
        memory_id: str,
        content: str,
        tool_name: str = "",
        argv: List[str] = [],
    ) -> int:
        """Build edges from a memory's content and tool context.

        Returns the number of edges added/updated.
        """
        entities = self.extract_entities(content)
        if len(entities) < 2:
            return 0

        c = self.conn.cursor()
        now = time.time()
        edges_added = 0

        # Default relation based on tool context.
        # Read-only inspection tools -> "checked"; mutating tools -> "modified".
        # Domain tools (disk/network_state/memory/process_count) keep
        # their specific relations for backward compatibility.
        content_lower = content.lower()
        read_only_tools = {"df", "du", "ls", "uname", "cat", "stat",
                           "ip", "ifconfig", "ss", "netstat", "ps",
                           "top", "free", "uptime", "date", "whoami"}
        if tool_name == "disk":
            # Legacy alias kept for backward compatibility with v1 KG data.
            default_rel = "contains"
        elif tool_name in {"network_state", "ip", "ifconfig",
                           "ss", "netstat"}:
            default_rel = "listens_on"
        elif tool_name in {"memory", "process_count", "ps", "top",
                           "free"}:
            default_rel = "runs_on"
        elif tool_name in read_only_tools or any(
                v in content_lower for v in ("checked", "queried",
                                              "listed", "showed")):
            default_rel = "checked"
        else:
            default_rel = "modified"

        # Create edges between co-occurring entities
        for i, e1 in enumerate(entities):
            for e2 in entities[i + 1: min(i + 4, len(entities))]:
                # Avoid self-loops and very short entities
                if e1 == e2 or len(e1) < 2 or len(e2) < 2:
                    continue

                relation = default_rel
                edge_key = (e1, e2, relation)

                existing = c.execute(
                    "SELECT strength, last_seen FROM knowledge_graph "
                    "WHERE source=? AND target=? AND relation=?",
                    edge_key,
                ).fetchone()

                if existing:
                    new_strength = min(existing[0] + 0.1, 5.0)
                    c.execute(
                        "UPDATE knowledge_graph SET strength=?, last_seen=?, "
                        "evidence_ids=? WHERE source=? AND target=? AND relation=?",
                        (new_strength, now, json.dumps([memory_id]), *edge_key),
                    )
                else:
                    c.execute(
                        "INSERT INTO knowledge_graph "
                        "(source, target, relation, strength, first_seen, last_seen, evidence_ids) "
                        "VALUES (?, ?, ?, 1.0, ?, ?, ?)",
                        (*edge_key, now, now, json.dumps([memory_id])),
                    )
                edges_added += 1

        self.conn.commit()
        return edges_added

    # ------------------------------------------------------------------
    # Conflict detection (TOKI arithmetic)
    # ------------------------------------------------------------------

    def detect_conflicts(self) -> List[Dict[str, Any]]:
        """Detect conflicting facts about the same entity.

        A conflict exists when two memories assert different values
        for the same entity-property pair.
        """
        c = self.conn.cursor()
        # Find entities that appear in multiple memories with different contexts
        rows = c.execute("""
            SELECT DISTINCT sm.id, sm.content, sm.importance, sm.last_accessed,
                   sm.access_count
            FROM semantic_memories sm
            WHERE sm.category = 'semantic'
              AND sm.importance >= 0.5
            ORDER BY sm.importance DESC, sm.last_accessed DESC
        """).fetchall()

        conflicts: List[Dict[str, Any]] = []
        entity_values: Dict[str, List[Tuple[str, float, float]]] = {}

        for row in rows:
            mem_id, content, importance, last_acc, access_count = row
            entities = self.extract_entities(content)
            for entity in entities:
                entity_values.setdefault(entity, []).append(
                    (mem_id, importance, last_acc)
                )

        # Find entities with multiple conflicting memories
        for entity, memories in entity_values.items():
            if len(memories) >= 3:
                # Check if there's variance in importance (suggests conflict)
                importances = [m[1] for m in memories]
                if max(importances) - min(importances) > 0.3:
                    conflicts.append({
                        "entity": entity,
                        "conflicting_memories": [
                            {"id": m[0], "importance": m[1], "last_accessed": m[2]}
                            for m in memories
                        ],
                        "resolution_strategy": "LWW",  # Last Write Wins by default
                    })

        return conflicts

    def resolve_conflict(
        self,
        entity: str,
        strategy: str = "LWW",
        winner_id: Optional[str] = None,
    ) -> bool:
        """Resolve a conflict using TOKI arithmetic strategies.

        Strategies:
        - LWW: Last Write Wins (most recent)
        - EWM: Earliest Write Matters (first verified)
        - PRP: Popularity Resolves Parity (most accessed)
        """
        c = self.conn.cursor()
        if strategy == "LWW" and winner_id:
            c.execute(
                "UPDATE semantic_memories SET importance = 0.95 WHERE id = ?",
                (winner_id,),
            )
        elif strategy == "EWM":
            row = c.execute(
                "SELECT id FROM semantic_memories "
                "WHERE category='semantic' AND importance >= 0.5 "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE semantic_memories SET importance = 0.95 WHERE id = ?",
                    (row[0],),
                )
        elif strategy == "PRP":
            row = c.execute(
                "SELECT id FROM semantic_memories "
                "WHERE category='semantic' AND importance >= 0.5 "
                "ORDER BY access_count DESC LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE semantic_memories SET importance = 0.95 WHERE id = ?",
                    (row[0],),
                )

        self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_neighbors(self, entity: str, direction: str = "both") -> List[Dict[str, Any]]:
        """Get all edges connected to an entity."""
        c = self.conn.cursor()
        if direction in ("in", "both"):
            rows = c.execute(
                "SELECT source, target, relation, strength, last_seen "
                "FROM knowledge_graph WHERE target = ?",
                (entity,),
            ).fetchall()
        else:
            rows = []
        if direction in ("out", "both"):
            rows.extend(c.execute(
                "SELECT source, target, relation, strength, last_seen "
                "FROM knowledge_graph WHERE source = ?",
                (entity,),
            ).fetchall())
        return [
            {"source": r[0], "target": r[1], "relation": r[2],
             "strength": r[3], "last_seen": r[4]}
            for r in rows
        ]

    def get_graph_stats(self) -> Dict[str, Any]:
        c = self.conn.cursor()
        nodes = c.execute("SELECT COUNT(DISTINCT source) FROM knowledge_graph").fetchone()[0]
        edges = c.execute("SELECT COUNT(*) FROM knowledge_graph").fetchone()[0]
        relations = c.execute(
            "SELECT relation, COUNT(*) FROM knowledge_graph GROUP BY relation"
        ).fetchall()
        return {
            "nodes": nodes,
            "edges": edges,
            "relations": {r[0]: r[1] for r in relations},
        }

    # Edge pruning: age-out weak/stale edges.

    def prune_weak_edges(
        self,
        min_strength: float = 0.3,
        max_age_seconds: float = 90 * 86400,
    ) -> int:
        # Remove edges whose strength has decayed below the
        # threshold AND that have not been seen within the age
        # window. Returns the number of edges removed.
        c = self.conn.cursor()
        cutoff = time.time() - max_age_seconds
        before = c.execute("SELECT COUNT(*) FROM knowledge_graph").fetchone()[0]
        c.execute(
            "DELETE FROM knowledge_graph "
            "WHERE strength < ? AND last_seen < ?",
            (min_strength, cutoff),
        )
        self.conn.commit()
        after = c.execute("SELECT COUNT(*) FROM knowledge_graph").fetchone()[0]
        removed = before - after
        if removed:
            logger.info("Pruned %d weak/stale KG edges", removed)
        return removed
