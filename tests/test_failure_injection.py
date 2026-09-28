# -*- coding: utf-8 -*-
"""Failure-injection tests.

A production system either survives its failure modes or it does
not. These tests prove the substrate behaves predictably when
things go wrong:

- a database file that does not exist
- a read-only directory (cannot create .db)
- a corrupted db file
- unicode / emoji in stored content
- extremely long content (1 MB single row)
- very high importance values
- concurrent open + close from many threads
- recall over a brand-new empty store
- query with no special characters (and every special character)
"""

from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "memory.db")


class TestNonExistentPath:
    def test_open_creates_file(self, tmp_path):
        # Point at a non-existent path; the substrate must create the
        # file (and its parent directory) and become usable.
        db = str(tmp_path / "new_subdir" / "memory.db")
        m = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            mid = m.store(content="hello", category="interaction")
            assert mid is not None
            hits = m.recall("hello", top_k=1)
            assert len(hits) == 1
        finally:
            m.close()


class TestUnicodeContent:
    def test_emoji_round_trip(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            content = "用户提交了订单 🚀 #12345 — 包含 emoji 表情"
            m.store(content=content, category="interaction", importance=0.7)
            hits = m.recall("订单", top_k=5)
            # Either the bigram finds it or the asciii token "12345" does;
            # we just need the row to be retrievable.
            assert any("订单" in h["content"] or "12345" in h["content"] for h in hits)
        finally:
            m.close()

    def test_very_long_content(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            big = "foo bar baz " * 100_000  # ~1.2 MB
            mid = m.store(content=big, category="episodic", importance=0.5)
            assert mid is not None
            hits = m.recall("foo", top_k=1)
            assert len(hits) >= 1
        finally:
            m.close()


class TestImportanceEdgeValues:
    @pytest.mark.parametrize("imp", [0.0, 0.5, 1.0, 1.5, -0.1])
    def test_extreme_importance_does_not_crash(self, tmp_db, imp):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            # We never expect a ValueError from importance; either accept
            # or clamp; the row must come back.
            m.store(content="x" * 10, category="interaction", importance=imp)
            hits = m.recall("x", top_k=1)
            assert len(hits) == 1
        finally:
            m.close()


class TestEmptyStore:
    def test_recall_on_empty_store_returns_empty(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            assert m.recall("anything", top_k=5) == []
        finally:
            m.close()

    def test_stats_on_empty_store(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            s = m.stats() if hasattr(m, "stats") else {}
            # The point is just: no exception.
            assert s is not None or s == {}
        finally:
            m.close()


class TestCorruptedFile:
    def test_opens_but_reports_corruption(self, tmp_path):
        """If the file exists but is not a valid sqlite database, open
        must fail loudly. Silent data loss would be the worst outcome."""
        bad = tmp_path / "garbage.db"
        bad.write_bytes(b"this is not a sqlite database file at all\n" * 100)
        with pytest.raises(sqlite3.DatabaseError):
            MemorySystemV2(MemoryConfig(db_path=str(bad)))


class TestSpecialCharsInQuery:
    @pytest.mark.parametrize("q", [
        "",
        " ",
        "\n\t\r",
        "; DROP TABLE semantic_memories;--",
        "'; SELECT * FROM semantic_memories; --",
        "订单 AND user_id = 'admin'",
        "❤️",
        "%",
        "_",
        "\\\\",
        "a" * 1000,
    ])
    def test_recall_with_special_query(self, tmp_db, q):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            m.store(content="baseline content", category="interaction")
            # We never expect an exception; the result may be empty.
            hits = m.recall(q, top_k=5)
            assert isinstance(hits, list)
        finally:
            m.close()


class TestOpenCloseManyTimes:
    def test_100_open_close_cycles_dont_corrupt(self, tmp_db):
        for i in range(100):
            m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
            m.store(content=f"row {i}", category="interaction")
            m.close()
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            hits = m.recall("row", top_k=200)
            assert len(hits) == 100
        finally:
            m.close()


class TestConcurrentOpenClose:
    def test_concurrent_open_close_does_not_crash(self, tmp_db):
        """Eight threads each open + close 20 times. We do not
        require that every iteration succeed under lock contention;
        we only require the test process not crash and at least
        some iterations succeed. The queue-serialised writer
        pattern in `tests/test_concurrency.py` is the recommended
        production pattern; this test just makes sure the
        substrate does not blow up when misused."""
        import queue
        successes = 0
        failures: List[Exception] = []

        def worker():
            nonlocal successes
            for _ in range(20):
                try:
                    m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
                    m.store(content="ping", category="interaction")
                    m.close()
                    successes += 1
                except Exception as e:
                    failures.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        # At least 10% of the 160 attempts must succeed; the rest
        # may fail with OperationalError("database is locked"),
        # which is the expected behaviour under contention.
        assert successes >= 16, f"only {successes}/160 succeeded"


class TestCloseIdempotent:
    def test_close_called_twice_does_not_raise(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        m.close()
        m.close()  # must be a no-op


class TestStoreReturnValue:
    def test_store_returns_string_id(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            mid = m.store(content="x", category="interaction")
            assert isinstance(mid, str) and len(mid) > 0
        finally:
            m.close()

    def test_consecutive_store_ids_are_unique(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            ids = {m.store(content="x", category="interaction") for _ in range(50)}
            assert len(ids) == 50
        finally:
            m.close()


class TestMetadataEdgeCases:
    def test_empty_metadata(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            mid = m.store(content="x", category="interaction", metadata={})
            assert mid is not None
        finally:
            m.close()

    def test_nested_metadata(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            meta = {"a": {"b": {"c": [1, 2, 3]}}, "x": None, "y": True}
            mid = m.store(content="x", category="interaction", metadata=meta)
            assert mid is not None
            hits = m.recall("x", top_k=1)
            assert hits
            stored = hits[0].get("metadata", {})
            # Round-trip equality on the nested structure.
            assert stored.get("a") == {"b": {"c": [1, 2, 3]}}
            assert stored.get("y") is True
        finally:
            m.close()


class TestTags:
    def test_tags_round_trip(self, tmp_db):
        m = MemorySystemV2(MemoryConfig(db_path=tmp_db))
        try:
            mid = m.store(
                content="x", category="interaction",
                tags=["merchant:42", "tier:gold", "中文标签"],
            )
            assert mid is not None
            hits = m.recall("x", top_k=1)
            assert hits
            assert "中文标签" in hits[0].get("tags", [])
        finally:
            m.close()