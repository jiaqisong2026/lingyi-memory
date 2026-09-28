# -*- coding: utf-8 -*-
"""Core data models for MemorySystemV2.

Three-layer orthogonal architecture:
  L0 Perception: MemoryEvent — structured extraction from tool calls
  L1 Memory:     Four-quadrant storage (episodic/semantic/procedural/preference)
  L2 Cognition:  Knowledge graph + active learning (in knowledge_graph.py / active_learning.py)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ====================================================================
# Enums
# ====================================================================


class MemoryCategory(str, Enum):
    """Four-quadrant memory categories (cognitive science)."""
    EPISODIC = "episodic"       # Episodic: what happened when/where
    SEMANTIC = "semantic"       # Semantic: facts and knowledge
    PROCEDURAL = "procedural"   # Procedural: rules and how-to
    PREFERENCE = "preference"   # Preference: user tastes and drift


class MemoryLevel(str, Enum):
    """Temporal depth within each category."""
    WORKING = "working"         # Current session buffer, 5min TTL
    SHORT = "short_term"        # Current session, 1h TTL
    MID = "mid_term"            # Recent memories, 30d TTL
    LONG = "long_term"          # Permanent knowledge
    PROCEDURAL = "procedural"   # Distilled rules, permanent + high-priority


# ====================================================================
# L0: MemoryEvent — structured perception
# ====================================================================


@dataclass
class MemoryEvent:
    """Minimal semantic unit of a memory event.

    Extracted from (tool, argv, returncode, stdout) tuples by the
    perception layer before storage in L1 tables.
    """
    event_type: str             # action | observation | preference | error | knowledge
    subject: str                # "user" | "agent" | "system"
    verb: str                   # "modified" | "queried" | "crashed" | "installed"
    object: str                 # "/etc/nginx/nginx.conf" | "disk_usage"
    context: Dict[str, Any] = field(default_factory=dict)
    outcome: str = "unknown"    # success | failure | partial
    confidence: float = 0.5     # 0-1, based on tool return code + output stability
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    user_id: str = "default"
    tool_name: str = ""
    argv: List[str] = field(default_factory=list)
    returncode: int = -1
    latency_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "subject": self.subject,
            "verb": self.verb,
            "object": self.object,
            "context": dict(self.context),
            "outcome": self.outcome,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tool_name": self.tool_name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryEvent":
        return cls(
            event_type=d.get("event_type", "observation"),
            subject=d.get("subject", "agent"),
            verb=d.get("verb", "query"),
            object=d.get("object", ""),
            context=d.get("context", {}),
            outcome=d.get("outcome", "unknown"),
            confidence=float(d.get("confidence", 0.5)),
            timestamp=float(d.get("timestamp", time.time())),
            session_id=d.get("session_id", ""),
            user_id=d.get("user_id", "default"),
            tool_name=d.get("tool_name", ""),
            argv=d.get("argv", []),
            returncode=int(d.get("returncode", -1)),
            latency_ms=float(d.get("latency_ms", 0.0)),
            error=d.get("error", ""),
        )

    def to_content(self) -> str:
        """Serialize to a flat string for backward-compatible FTS5/BM25 indexing."""
        parts = [
            f"[{self.event_type}] {self.subject} {self.verb} {self.object}",
        ]
        if self.context:
            parts.append(f"context={json.dumps(self.context, ensure_ascii=False)}")
        if self.error:
            parts.append(f"error={self.error}")
        return " | ".join(parts)


# ====================================================================
# L1: MemoryItem — stored memory entry
# ====================================================================


@dataclass
class MemoryItem:
    """A single memory entry in the L1 storage layer."""
    id: str
    content: str
    level: MemoryLevel
    category: MemoryCategory
    importance: float           # 0-1
    access_count: int = 0
    created_at: float = 0.0
    last_accessed: float = 0.0
    ttl: int = -1               # seconds; -1 = permanent
    metadata: Dict[str, Any] = field(default_factory=dict)
    decay_rate: float = 0.01    # differential decay rate
    tags: List[str] = field(default_factory=list)
    session_id: str = ""
    user_id: str = "default"


# ====================================================================
# Configuration
# ====================================================================


@dataclass
class MemoryConfig:
    """Configuration for MemorySystemV2."""
    db_path: str = ""
    use_project_memory: bool = True
    markdown_mirror: bool = False
    markdown_path: str = ""
    enable_wal: bool = True  # Set False for in-memory / shared cache test scenarios
    busy_timeout_ms: int = 30000  # Wait this long for a writer lock before raising (30s handles 100+ concurrent writers on commodity NVMe)
    # RRF fusion parameter (Cormack et al. recommend k=60)
    rrf_k: int = 60
    # Differential decay base rate
    decay_base_lambda: float = 0.01
    # Core snapshot budget
    core_snapshot_max_tokens: int = 800
    core_snapshot_min_confidence: float = 0.7
    core_snapshot_min_access: int = 2
    # Consolidation interval (every N stores)
    consolidation_interval: int = 20


# ====================================================================
# L1: MemorySystemV2 — four-quadrant storage with RRF retrieval
# ====================================================================


class MemorySystemV2:
    """Three-layer orthogonal memory system.

    Layer 0 (Perception): MemoryEvent extraction from tool calls.
    Layer 1 (Memory): Four-quadrant storage with RRF hybrid retrieval.
    Layer 2 (Cognition): Knowledge graph + active learning.

    Backward-compatible API:
        mem.store(content="...", category="interaction", ...)
        hits = mem.recall(query="...", top_k=5)
    """

    def __init__(self, config: Optional[MemoryConfig] = None):
        self.config = config or MemoryConfig()
        if not self.config.db_path:
            self.config.db_path = self._default_db_path()
        try:
            Path(self.config.db_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._conn = sqlite3.connect(self.config.db_path)
        self._configure_sqlite()
        self._init_db()
        self._bm25_dirty = True
        self._store_count = 0
        # Backward compat: try loading old MemoryManager
        self._legacy_manager = None
        if self.config.use_project_memory:
            self._legacy_manager = self._try_load_legacy()
        # L2 components (initialized lazily)
        self._knowledge_graph: Optional["KnowledgeGraph"] = None
        self._active_learner: Optional["ActiveLearner"] = None
        self._consolidation_engine = None

    # ------------------------------------------------------------------
    # DB initialization
    # ------------------------------------------------------------------


    @property
    def conn(self) -> Any:
        """Public accessor for the underlying sqlite3 connection.

        Use this when constructing helper components (knowledge graph,
        active learner, custom plugins) that need to share the same DB
        handle. Returning ``self._conn`` directly is intentional — the
        property exists so callers don't reach into a private attr.
        """
        return self._conn

    def _configure_sqlite(self) -> None:
        """Apply production-grade PRAGMAs to the live sqlite3 connection.

        Activates WAL journaling (concurrent readers / single writer),
        sets busy_timeout so a short reader never errors on lock contention,
        and adjusts cache/temp settings. Skipped for ``":memory::`` because
        in-memory databases cannot use WAL.

        The resulting ``journal_mode`` is exposed via ``self._journal_mode``
        for tests and operators.
        """
        self._journal_mode = "unknown"
        if self.config.db_path in (":memory:", "", None):
            self._journal_mode = "memory"
            return
        if not self.config.enable_wal:
            self._journal_mode = "rollback"
            c = self._conn.cursor()
            try:
                c.execute(f"PRAGMA busy_timeout={int(self.config.busy_timeout_ms)}")
                c.execute("PRAGMA foreign_keys=ON")
            finally:
                c.close()
            return
        c = self._conn.cursor()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute(f"PRAGMA busy_timeout={int(self.config.busy_timeout_ms)}")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
            row = c.execute("PRAGMA journal_mode").fetchone()
            self._journal_mode = row[0] if row else "unknown"
        finally:
            c.close()

    def _init_db(self) -> None:

        conn = self._conn
        c = conn.cursor()

        # --- episodic_memories (session-level, auto-expire) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_session ON episodic_memories(session_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_expires ON episodic_memories(expires_at)")

        # --- semantic_memories (facts, FTS5 + BM25) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS semantic_memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'semantic',
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL,
                decay_rate REAL DEFAULT 0.01,
                tags TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                session_id TEXT DEFAULT '',
                user_id TEXT DEFAULT 'default'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sem_cat ON semantic_memories(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sem_user ON semantic_memories(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sem_importance ON semantic_memories(importance DESC)")
        try:
            c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts USING fts5(
                    id, content, category, tags,
                    content_rowid=rowid
                )
            """)
            self._fts5_available = True
        except Exception:
            self._fts5_available = False

        # --- procedural_memories (rules, permanent) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS procedural_memories (
                id TEXT PRIMARY KEY,
                rule_content TEXT NOT NULL,
                precondition TEXT,
                trigger_conditions TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.8,
                verified_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                last_verified REAL NOT NULL
            )
        """)

        # --- preference_memories (user preferences, drift detection) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS preference_memories (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                change_history TEXT DEFAULT '[]',
                created_at REAL NOT NULL,
                last_changed REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pref_user_dim ON preference_memories(user_id, dimension)")

        # --- provenance (lineage, shared with legacy) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS provenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT DEFAULT '',
                source TEXT DEFAULT '',
                timestamp REAL NOT NULL,
                FOREIGN KEY (memory_id) REFERENCES semantic_memories(id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_prov_mem ON provenance(memory_id)")

        # --- agent_runs (audit table, shared with legacy) ---
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                tool TEXT NOT NULL,
                argv TEXT NOT NULL,
                returncode INTEGER NOT NULL,
                success INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                content TEXT NOT NULL,
                error TEXT DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ar_created ON agent_runs(created_at)")

        conn.commit()

    # ------------------------------------------------------------------
    # Legacy bridge (backward compatibility)
    # ------------------------------------------------------------------

    def _try_load_legacy(self) -> Any:
        """Try loading the old MemoryManager for backward compat."""
        try:
            from pathlib import Path
            import sys
            src_root = Path(__file__).resolve().parents[2]
            if str(src_root) not in sys.path:
                sys.path.insert(0, str(src_root))
            from src.memory.memory_manager import MemoryManager
            return MemoryManager(db_path=self.config.db_path)
        except Exception as e:
            logger.debug("Legacy MemoryManager unavailable: %s", e)
            return None

    def _default_db_path(self) -> str:
        from pathlib import Path
        d = Path(__file__).resolve().parents[2] / "data" / "kylin_v11_agent_memory_v2.db"
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return str(d)

    # ------------------------------------------------------------------
    # Public API — backward compatible
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        category: str = "interaction",
        importance: float = 0.5,
        level: Optional[MemoryLevel] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: str = "",
        user_id: str = "default",
        tags: Optional[List[str]] = None,
        tool_name: str = "",
        access_count: int = 0,
    ) -> Optional[str]:
        """Store a memory entry. Backward-compatible signature."""
        mid = str(uuid.uuid4())
        now = time.time()
        cat = self._resolve_category(category)
        lvl = self._resolve_level(cat, importance, level)
        ttl = self._ttl_for(lvl)
        decay = self._decay_for(cat, importance)
        tags_list = tags or []
        meta = metadata or {}

        conn = self._conn
        c = conn.cursor()

        # Store in semantic_memories (primary)
        if tool_name:
            meta.setdefault("tool_name", tool_name)
        c.execute("""
            INSERT INTO semantic_memories
            (id, content, category, importance, access_count, created_at,
             last_accessed, decay_rate, tags, metadata, session_id, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid, content, cat.value, importance, int(access_count),
            now, now, decay, json.dumps(tags_list, ensure_ascii=False),
            json.dumps(meta, ensure_ascii=False), session_id, user_id,
        ))

        # Sync FTS5
        if self._fts5_available:
            try:
                c.execute(
                    "INSERT INTO semantic_fts (id, content, category, tags) VALUES (?, ?, ?, ?)",
                    (mid, content, cat.value, json.dumps(tags_list, ensure_ascii=False)),
                )
            except Exception:
                pass

        # Record provenance
        self._record_provenance(c, mid, "created",
                                f"Stored as {lvl.value}, importance={importance:.2f}",
                                source=session_id or "manual")

        conn.commit()
        self._bm25_dirty = True

        # Auto-consolidation trigger
        self._store_count += 1
        if (self._consolidation_engine and
                self._store_count % self.config.consolidation_interval == 0):
            try:
                self._consolidation_engine.run_consolidation()
            except Exception as e:
                logger.warning("Auto-consolidation failed: %s", e)

        return mid

    def recall(self, query: str, top_k: int = 5,
               level: Optional[MemoryLevel] = None,
               category: Optional[str] = None,
               user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recall memories via RRF hybrid retrieval. Backward-compatible signature."""
        results = self._rrf_search(query, top_k=top_k, level=level,
                                   category=category, user_id=user_id)
        # Update access counts
        if results:
            now = time.time()
            c = self._conn.cursor()
            for item in results:
                mid = item.get("id")
                if mid:
                    c.execute(
                        "UPDATE semantic_memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        (now, mid),
                    )
            self._conn.commit()
        return results

    def audit(
        self,
        success: bool,
        tool: str,
        argv: List[str],
        returncode: int,
        latency_ms: float,
        content: str,
        error: str = "",
    ) -> None:
        """Write an audit row to agent_runs table."""
        c = self._conn.cursor()
        c.execute("""
            INSERT INTO agent_runs
            (created_at, tool, argv, returncode, success, latency_ms, content, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), tool, json.dumps(argv, ensure_ascii=False),
            int(returncode), 1 if success else 0,
            float(latency_ms), content[:512], error,
        ))
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

    def stats(self) -> Dict[str, Any]:
        c = self._conn.cursor()
        total = c.execute("SELECT COUNT(*) FROM semantic_memories").fetchone()[0]
        by_level = c.execute(
            "SELECT category, COUNT(*) FROM semantic_memories GROUP BY category"
        ).fetchall()
        return {
            "db_path": self.config.db_path,
            "total_memories": total,
            "by_category": {r[0]: r[1] for r in by_level},
            "use_project_memory": self._legacy_manager is not None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_category(self, category: str) -> MemoryCategory:
        mapping = {
            "interaction": MemoryCategory.SEMANTIC,
            "task_experience": MemoryCategory.SEMANTIC,
            "preference": MemoryCategory.PREFERENCE,
            "procedural": MemoryCategory.PROCEDURAL,
            "knowledge": MemoryCategory.SEMANTIC,
        }
        return mapping.get(category, MemoryCategory.SEMANTIC)

    def _resolve_level(
        self, cat: MemoryCategory, importance: float,
        level: Optional[MemoryLevel],
    ) -> MemoryLevel:
        if cat == MemoryCategory.PROCEDURAL:
            return MemoryLevel.PROCEDURAL
        if level is not None:
            return level
        if importance >= 0.7:
            return MemoryLevel.LONG
        elif importance >= 0.4:
            return MemoryLevel.MID
        elif importance >= 0.2:
            return MemoryLevel.SHORT
        return MemoryLevel.WORKING

    def _ttl_for(self, level: MemoryLevel) -> int:
        return {
            MemoryLevel.WORKING: 300,
            MemoryLevel.SHORT: 3600,
            MemoryLevel.MID: 2592000,
            MemoryLevel.LONG: -1,
            MemoryLevel.PROCEDURAL: -1,
        }[level]

    def _decay_for(self, cat: MemoryCategory, importance: float) -> float:
        """Base decay rate modulated by category and importance."""
        base = self.config.decay_base_lambda
        if cat == MemoryCategory.PROCEDURAL:
            return 0.0  # procedural never decays
        if cat == MemoryCategory.PREFERENCE:
            return base * 0.5  # preferences decay slower
        return base * (1.0 - importance * 0.7)  # higher importance → slower decay

    def _record_provenance(self, cursor: Any, memory_id: str,
                           event_type: str, detail: str = "",
                           source: str = "") -> int:
        cursor.execute("""
            INSERT INTO provenance (memory_id, event_type, detail, source, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (memory_id, event_type, detail, source, time.time()))
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # RRF Hybrid Retrieval
    # ------------------------------------------------------------------

    def _rrf_search(
        self, query: str, top_k: int = 5,
        level: Optional[MemoryLevel] = None,
        category: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """RRF (Reciprocal Rank Fusion) hybrid retrieval across three channels.

        Channel 1: FTS5 full-text search
        Channel 2: BM25 in-memory scoring
        Channel 3: SQL importance/recency fallback

        RRF formula: score(doc) = Σ(1 / (k + rank_i(doc)))
        """
        from .retrieval import RRFRetriever
        retriever = RRFRetriever(
            conn=self._conn,
            fts5_available=self._fts5_available,
            rrf_k=self.config.rrf_k,
            bm25_scorer=None,  # lazy-loaded
        )
        return retriever.search(
            query=query, top_k=top_k,
            level=level, category=category, user_id=user_id,
        )

    # ------------------------------------------------------------------
    # Core Snapshot (L1 core memory freeze)
    # ------------------------------------------------------------------

    def get_core_snapshot(
        self, user_id: str = "default",
        max_tokens: Optional[int] = None,
        min_confidence: Optional[float] = None,
        min_access: Optional[int] = None,
    ) -> str:
        """Generate L1 core memory snapshot for system prompt injection."""
        mt = max_tokens or self.config.core_snapshot_max_tokens
        mc = min_confidence or self.config.core_snapshot_min_confidence
        ma = min_access or self.config.core_snapshot_min_access

        c = self._conn.cursor()
        rows = c.execute("""
            SELECT content, category, importance, access_count, id
            FROM semantic_memories
            WHERE importance >= ? AND access_count >= ?
            ORDER BY importance DESC, access_count DESC
            LIMIT 50
        """, (mc, ma)).fetchall()

        if not rows:
            return ""

        by_category: Dict[str, list] = {}
        for row in rows:
            by_category.setdefault(row[1], []).append(row)

        lines = [f"[Core Memory Snapshot - user:{user_id}]"]
        char_budget = int(mt / 1.5)
        used = 0

        category_labels = {
            "semantic": "Knowledge",
            "preference": "Preferences",
            "procedural": "Procedures",
            "episodic": "Recent Events",
        }

        for cat, items in by_category.items():
            label = category_labels.get(cat, cat.title())
            header = f"## {label}"
            if used + len(header) > char_budget:
                break
            lines.append(header)
            used += len(header)

            for content, _, importance, access_count, item_id in items:
                line = f"- {content} (conf={importance:.2f}, used {access_count}x)"
                if used + len(line) > char_budget:
                    break
                lines.append(line)
                used += len(line)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Feedback loop
    # ------------------------------------------------------------------

    def apply_feedback(self, memory_id: str, positive: bool = True,
                       reason: str = "") -> bool:
        """Adjust importance based on explicit feedback."""
        c = self._conn.cursor()
        row = c.execute(
            'SELECT importance, metadata FROM semantic_memories WHERE id=?',
            (memory_id,),
        ).fetchone()
        if not row:
            return False

        meta = json.loads(row[1]) if row[1] else {}
        history = meta.setdefault('feedback_history', [])
        old = float(row[0])
        delta = 0.08 if positive else -0.12
        new = max(0.0, min(1.0, old + delta))
        history.append({
            't': time.time(), 'positive': bool(positive),
            'reason': reason, 'old': old, 'new': new,
        })
        c.execute(
            'UPDATE semantic_memories SET importance=?, metadata=? WHERE id=?',
            (new, json.dumps(meta, ensure_ascii=False), memory_id),
        )
        self._conn.commit()
        self._bm25_dirty = True
        return True

    # ------------------------------------------------------------------
    # Consolidation engine hook
    # ------------------------------------------------------------------

    def set_consolidation_engine(self, engine: Any) -> None:
        self._consolidation_engine = engine

    def run_consolidation(self) -> Dict[str, Any]:
        if not getattr(self, '_consolidation_engine', None):
            return {"status": "no_engine"}
        return self._consolidation_engine.run_consolidation()

    # ------------------------------------------------------------------
    # Procedural rules
    # ------------------------------------------------------------------

    def store_distilled_rule(
        self, rule_content: str, rule_type: str = "procedural",
        source_reflection: str = "", confidence: float = 0.8,
    ) -> Optional[str]:
        """Store a distilled procedural rule (L5)."""
        mid = str(uuid.uuid4())
        now = time.time()
        c = self._conn.cursor()
        c.execute("""
            INSERT INTO procedural_memories
            (id, rule_content, precondition, trigger_conditions,
             confidence, verified_count, created_at, last_verified)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """, (mid, rule_content, "", "[]", confidence, now, now))
        self._conn.commit()
        return mid

    def get_procedural_rules(self, top_k: int = 20) -> List[Dict[str, Any]]:
        c = self._conn.cursor()
        rows = c.execute("""
            SELECT id, rule_content, confidence, verified_count, trigger_conditions
            FROM procedural_memories
            ORDER BY confidence DESC, verified_count DESC
            LIMIT ?
        """, (top_k,)).fetchall()
        return [
            {
                "id": r[0], "content": r[1], "confidence": r[2],
                "verified_count": r[3],
                "trigger_conditions": json.loads(r[4]) if r[4] else [],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Provenance / lineage
    # ------------------------------------------------------------------

    def record_provenance(self, memory_id: str, event_type: str,
                          detail: str = "", source: str = "") -> int:
        c = self._conn.cursor()
        return self._record_provenance(c, memory_id, event_type, detail, source)

    def get_lineage(self, memory_id: str) -> List[Dict[str, Any]]:
        c = self._conn.cursor()
        rows = c.execute(
            "SELECT event_type, detail, source, timestamp FROM provenance "
            "WHERE memory_id = ? ORDER BY timestamp",
            (memory_id,),
        ).fetchall()
        return [
            {"event_type": r[0], "detail": r[1], "source": r[2], "timestamp": r[3]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Remove expired episodic memories."""
        now = time.time()
        c = self._conn.cursor()
        c.execute("DELETE FROM episodic_memories WHERE expires_at < ?", (now,))
        count = c.rowcount
        self._conn.commit()
        if count > 0:
            logger.info("Cleaned up %d expired episodic memories", count)
        return count
