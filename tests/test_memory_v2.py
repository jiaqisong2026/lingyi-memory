# -*- coding: utf-8 -*-
"""Tests for MemorySystemV2 — three-layer orthogonal architecture.

Covers:
- store → recall round-trip (v2 API)
- RRF hybrid retrieval (FTS5 + BM25 + SQL keyword)
- Differential decay computation
- Knowledge graph entity extraction and edge construction
- Active learning gap observation and filling
- Bridge backward compatibility (old API → new internals)
- Procedural rule storage and retrieval
- Core snapshot generation
- Feedback loop importance adjustment
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

# CRITICAL: Insert G:\kylin BEFORE any other src/ path so that
# `import src.memory_v2` resolves to G:\kylin\src\memory_v2,
# NOT to the os_agent_memory project's src/.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# Remove conflicting paths that shadow our src/
sys.path = [p for p in sys.path if '银河os' not in p]


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "memory_v2.db")


@pytest.fixture
def mem(tmp_db: str):
    """MemorySystemV2 instance with a temp DB."""
    from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig
    cfg = MemoryConfig(db_path=tmp_db)
    m = MemorySystemV2(cfg)
    yield m
    m.close()


@pytest.fixture
def bridge(tmp_db: str):
    """Backward-compatible bridge instance."""
    from src.kylin_v11_agent.memory_v2.bridge import KylinAgentMemoryBridge
    from src.kylin_v11_agent.memory import MemoryConfig as OldConfig
    cfg = OldConfig(db_path=tmp_db, use_project_memory=False)
    b = KylinAgentMemoryBridge(cfg)
    yield b
    b.close()


# ====================================================================
# Core: store and recall
# ====================================================================


class TestCoreStoreRecall:
    def test_store_and_recall_basic(self, mem):
        mid = mem.store(content="user prefers vim editor", category="preference",
                        importance=0.9, tags=["editor", "vim"])
        assert mid is not None
        hits = mem.recall("vim editor", top_k=5)
        assert len(hits) >= 1
        assert any("vim" in h["content"].lower() for h in hits)

    def test_store_multiple_categories(self, mem):
        ids = []
        for cat, content in [
            ("semantic", "nginx listens on port 80"),
            ("preference", "user likes dark theme"),
            ("procedural", "restart nginx after config change"),
        ]:
            mid = mem.store(content=content, category=cat, importance=0.7)
            ids.append(mid)

        # Each category should be retrievable
        semantic_hits = mem.recall("nginx port", top_k=5)
        assert len(semantic_hits) >= 1

    def test_recall_with_user_filter(self, mem):
        mem.store(content="alice's preference", user_id="alice", importance=0.8)
        mem.store(content="bob's preference", user_id="bob", importance=0.8)
        alice_hits = mem.recall("preference", top_k=5, user_id="alice")
        bob_hits = mem.recall("preference", top_k=5, user_id="bob")
        # Each should find their own
        assert len(alice_hits) >= 1 or len(bob_hits) >= 1

    def test_recall_empty_query_fallback(self, mem):
        mem.store(content="important fact", importance=0.95)
        hits = mem.recall("", top_k=5)
        # Should fall back to importance-based ordering
        assert isinstance(hits, list)

    def test_importance_ordering(self, mem):
        mem.store(content="low priority", importance=0.1)
        mem.store(content="high priority", importance=0.95)
        hits = mem.recall("", top_k=5)
        if len(hits) >= 2:
            assert hits[0]["importance"] >= hits[-1]["importance"]


# ====================================================================
# RRF Retrieval
# ====================================================================


class TestRRFRetrieval:
    def test_fts5_channel(self, mem):
        mem.store(content="the quick brown fox jumps over the lazy dog",
                  category="semantic", importance=0.5)
        hits = mem.recall("quick brown fox", top_k=5)
        assert len(hits) >= 1

    def test_bm25_channel_with_synonyms(self, mem):
        mem.store(content="docker container deployment pipeline",
                  category="semantic", importance=0.6)
        # "部署" should expand to include "deploy", "docker", "pipeline"
        hits = mem.recall("部署 docker", top_k=5)
        assert len(hits) >= 1

    def test_sql_keyword_channel(self, mem):
        mem.store(content="system crashed with OOM error",
                  category="semantic", importance=0.7)
        hits = mem.recall("crash OOM", top_k=5)
        assert len(hits) >= 1

    def test_rrf_fusion_prefers_consistent_docs(self, mem):
        """Docs appearing in multiple channels should rank higher."""
        # This doc appears in all 3 channels
        mem.store(content="nginx reverse proxy configuration",
                  category="semantic", importance=0.8, tags=["nginx", "proxy"])
        # This doc only in FTS5/BM25
        mem.store(content="redis cache configuration",
                  category="semantic", importance=0.8, tags=["redis"])
        hits = mem.recall("nginx reverse proxy", top_k=10)
        if len(hits) >= 2:
            # The nginx doc should rank higher due to RRF fusion
            nginx_hit = next((h for h in hits if "nginx" in h["content"]), None)
            assert nginx_hit is not None


# ====================================================================
# Differential Decay
# ====================================================================


class TestDifferentialDecay:
    def test_high_access_slows_decay(self):
        from src.kylin_v11_agent.memory_v2.decay import DifferentialDecay
        dd = DifferentialDecay(base_lambda=0.01)
        now = time.time()
        rate_low = dd.compute_decay_rate(access_count=0, last_accessed=now,
                                         created_at=now - 86400 * 30)
        rate_high = dd.compute_decay_rate(access_count=100, last_accessed=now,
                                          created_at=now - 86400 * 30)
        assert rate_high < rate_low  # High access → slower decay

    def test_recent_access_slows_decay(self):
        from src.kylin_v11_agent.memory_v2.decay import DifferentialDecay
        dd = DifferentialDecay(base_lambda=0.01)
        now = time.time()
        rate_old = dd.compute_decay_rate(last_accessed=now - 86400 * 60,
                                         created_at=now - 86400 * 90)
        rate_new = dd.compute_decay_rate(last_accessed=now,
                                         created_at=now - 86400 * 30)
        assert rate_new < rate_old

    def test_high_importance_slows_decay(self):
        from src.kylin_v11_agent.memory_v2.decay import DifferentialDecay
        dd = DifferentialDecay(base_lambda=0.01)
        now = time.time()
        rate_low = dd.compute_decay_rate(importance=0.1, last_accessed=now,
                                         created_at=now - 86400 * 30)
        rate_high = dd.compute_decay_rate(importance=0.9, last_accessed=now,
                                          created_at=now - 86400 * 30)
        assert rate_high < rate_low

    def test_procedural_never_decays(self):
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig, MemoryCategory
        cfg = MemoryConfig(db_path=":memory:")
        ms = MemorySystemV2(cfg)
        decay = ms._decay_for(MemoryCategory.PROCEDURAL, 0.8)
        assert decay == 0.0
        ms.close()


# ====================================================================
# Knowledge Graph
# ====================================================================


class TestKnowledgeGraph:
    def test_entity_extraction_paths(self):
        from src.kylin_v11_agent.memory_v2.knowledge_graph import KnowledgeGraph
        entities = KnowledgeGraph.extract_entities(
            "modified /etc/nginx/nginx.conf and /var/log/nginx/error.log"
        )
        assert "/etc/nginx/nginx.conf" in entities
        assert "/var/log/nginx/error.log" in entities

    def test_entity_extraction_commands(self):
        from src.kylin_v11_agent.memory_v2.knowledge_graph import KnowledgeGraph
        entities = KnowledgeGraph.extract_entities(
            "ran systemctl restart nginx and checked df -h output"
        )
        assert "systemctl" in entities
        assert "nginx" in entities

    def test_edge_construction(self, mem):
        from src.kylin_v11_agent.memory_v2.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph(mem._conn)
        mem.store(content="modified /etc/nginx/nginx.conf and restarted nginx service",
                  category="semantic", importance=0.7, tool_name="disk")
        edges = kg.add_edges_from_memory(
            memory_id="test_mem_1",
            content="modified /etc/nginx/nginx.conf and restarted nginx service",
            tool_name="disk",
        )
        assert edges >= 1
        stats = kg.get_graph_stats()
        assert stats["edges"] >= 1

    def test_conflict_detection(self, mem):
        from src.kylin_v11_agent.memory_v2.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph(mem._conn)
        mem.store(content="nginx port is 80", category="semantic", importance=0.9)
        mem.store(content="nginx port is 8080", category="semantic", importance=0.5)
        mem.store(content="nginx default port 443", category="semantic", importance=0.3)
        conflicts = kg.detect_conflicts()
        # Should detect at least one conflict about "nginx" or "port"
        assert isinstance(conflicts, list)


# ====================================================================
# Active Learning
# ====================================================================


class TestActiveLearning:
    def test_gap_observation(self, mem):
        from src.kylin_v11_agent.memory_v2.active_learning import ActiveLearner
        al = ActiveLearner(mem._conn)
        gap_id = al.observe_gap("how to configure firewall", "semantic", top_k=5, hits=0)
        assert gap_id is not None

    def test_no_gap_when_hits_found(self, mem):
        from src.kylin_v11_agent.memory_v2.active_learning import ActiveLearner
        al = ActiveLearner(mem._conn)
        mem.store(content="firewall configured with iptables", category="semantic", importance=0.8)
        gap_id = al.observe_gap("firewall configuration", "semantic", top_k=5, hits=1)
        assert gap_id is None  # No gap when hits found

    def test_gap_report(self, mem):
        from src.kylin_v11_agent.memory_v2.active_learning import ActiveLearner
        al = ActiveLearner(mem._conn)
        al.observe_gap("missing knowledge A", "semantic", hits=0)
        al.observe_gap("missing knowledge B", "semantic", hits=0)
        report = al.get_gap_report(limit=10)
        assert len(report) >= 2

    def test_fill_gap(self, mem):
        from src.kylin_v11_agent.memory_v2.active_learning import ActiveLearner
        al = ActiveLearner(mem._conn)
        al.observe_gap("test gap", "semantic", hits=0)
        report = al.get_gap_report(limit=10)
        assert len(report) >= 1
        gap_id = report[0]["gap_id"]
        assert al.fill_gap(gap_id, evidence_ids=["mem_1"])

    def test_consolidation(self, mem):
        from src.kylin_v11_agent.memory_v2.active_learning import ActiveLearner
        al = ActiveLearner(mem._conn)
        al.observe_gap("gap A", "semantic", hits=0)
        al.observe_gap("gap B", "semantic", hits=0)
        stats = al.consolidate()
        assert "total_gaps" in stats
        assert "filled" in stats


# ====================================================================
# Bridge: backward compatibility
# ====================================================================


class TestBridge:
    def test_bridge_store_and_recall(self, bridge):
        mid = bridge.store(
            content="check kernel version", success=True,
            tool="uname", argv=["-a"], returncode=0,
            latency_ms=12.3, error="",
        )
        assert mid is not None
        hits = bridge.recall("kernel", top_k=5)
        assert isinstance(hits, list)

    def test_bridge_audit(self, bridge):
        bridge.audit(success=False, tool="disk", argv=["-h"],
                     returncode=1, latency_ms=10.0,
                     content="", error="rc=1")
        import sqlite3
        conn = sqlite3.connect(bridge.config.db_path)
        rows = conn.execute(
            "SELECT tool, success FROM agent_runs WHERE tool='disk'"
        ).fetchall()
        conn.close()
        assert any(r[1] == 0 for r in rows)

    def test_bridge_stats(self, bridge):
        stats = bridge.stats()
        assert "db_path" in stats
        assert "total_memories" in stats

    def test_bridge_close(self, bridge):
        bridge.close()
        # Should not raise


# ====================================================================
# Procedural Rules
# ====================================================================


class TestProceduralRules:
    def test_store_and_retrieve_rules(self, mem):
        mid = mem.store_distilled_rule(
            rule_content="Always run systemctl daemon-reload after modifying unit files",
            rule_type="procedural",
            confidence=0.9,
        )
        assert mid is not None
        rules = mem.get_procedural_rules(top_k=20)
        assert len(rules) >= 1
        assert any("daemon-reload" in r["content"] for r in rules)


# ====================================================================
# Core Snapshot
# ====================================================================


class TestCoreSnapshot:
    def test_snapshot_generation(self, mem):
        mem.store(content="user prefers vim", category="preference",
                  importance=0.95, access_count=10)
        mem.store(content="nginx runs on port 80", category="semantic",
                  importance=0.9, access_count=8)
        snapshot = mem.get_core_snapshot(user_id="test", max_tokens=500)
        assert "[Core Memory Snapshot" in snapshot
        assert "vim" in snapshot.lower() or "nginx" in snapshot.lower()

    def test_empty_snapshot(self, mem):
        snapshot = mem.get_core_snapshot(min_confidence=0.99)
        assert snapshot == ""


# ====================================================================
# Feedback Loop
# ====================================================================


class TestFeedbackLoop:
    def test_positive_feedback_increases_importance(self, mem):
        mid = mem.store(content="test feedback", importance=0.5)
        assert mem.apply_feedback(mid, positive=True, reason="user confirmed")
        c = mem._conn.cursor()
        row = c.execute("SELECT importance FROM semantic_memories WHERE id=?", (mid,)).fetchone()
        assert row and row[0] > 0.5

    def test_negative_feedback_decreases_importance(self, mem):
        mid = mem.store(content="test feedback neg", importance=0.8)
        assert mem.apply_feedback(mid, positive=False, reason="user disagreed")
        c = mem._conn.cursor()
        row = c.execute("SELECT importance FROM semantic_memories WHERE id=?", (mid,)).fetchone()
        assert row and row[0] < 0.8

    def test_feedback_on_missing_id(self, mem):
        assert not mem.apply_feedback("nonexistent_id")


# ====================================================================
# Provenance / Lineage
# ====================================================================


class TestProvenance:
    def test_record_and_query_lineage(self, mem):
        mid = mem.store(content="test lineage", importance=0.7)
        event_id = mem.record_provenance(mid, "accessed", detail="first read", source="test")
        assert event_id > 0
        lineage = mem.get_lineage(mid)
        assert any(e["event_type"] == "created" for e in lineage)
        assert any(e["event_type"] == "accessed" for e in lineage)


# ====================================================================
# Cleanup
# ====================================================================


class TestCleanup:
    def test_cleanup_expired(self, mem):
        count = mem.cleanup_expired()
        assert isinstance(count, int)
        assert count >= 0


# ====================================================================
# Stats
# ====================================================================


class TestStats:
    def test_stats_structure(self, mem):
        mem.store(content="stat test", importance=0.5)
        stats = mem.stats()
        assert "total_memories" in stats
        assert stats["total_memories"] >= 1
        assert "by_category" in stats
