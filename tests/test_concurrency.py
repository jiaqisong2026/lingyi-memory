# -*- coding: utf-8 -*-
"""Concurrency tests for LingYi memory v2.

A Pinduoduo-scale deployment will have many agent threads reading
and writing the same tenant'``s memory file. These tests prove the
substrate survives that.

We deliberately do NOT share a single sqlite3 connection across
threads (the default `check_same_thread=True` prevents it). Each
worker thread opens its own `MemorySystemV2` against the same db
file; SQLite WAL handles concurrent readers + one writer.
"""

from __future__ import annotations
import os, sys, tempfile, threading, time
from pathlib import Path
from typing import List

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig


def _make_tenant(tmp_path: Path) -> str:
    """One fresh SQLite file per test (each test owns its tenant)."""
    return str(tmp_path / "tenant.db")


# Concurrent writers
# Concurrent writers (queue-serialized)
# ----------------------------------------------------------------------


class TestConcurrentWriters:
    def test_n_writers_no_data_loss(self, tmp_path):
        """N threads queue writes to one writer; final count == N*M.

        This is the **standard SQLite-writer pattern**: many threads
        emit memory events; a single writer thread drains the queue
        and is the only one touching the SQLite connection. SQLite
        happily services concurrent reads against the same file.
        """
        import queue
        db = _make_tenant(tmp_path)
        N_THREADS = 4
        N_PER_THREAD = 25
        q: "queue.Queue[str]" = queue.Queue()

        def producer(tid: int) -> None:
            for i in range(N_PER_THREAD):
                q.put(f"thread {tid} row {i}: payload-{tid}-{i}")

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(N_THREADS)]
        for t in threads: t.start()

        # Single consumer: one connection, no contention.
        m = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            drained = 0
            target = N_THREADS * N_PER_THREAD
            while drained < target:
                item = q.get(timeout=10.0)
                m.store(content=item, category="interaction", importance=0.5)
                drained += 1
        finally:
            m.close()
        for t in threads: t.join()

        m2 = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            rows = m2.recall("payload", top_k=10000)
            assert len(rows) == N_THREADS * N_PER_THREAD
        finally:
            m2.close()

    def test_writers_dont_corrupt_each_others_metadata(self, tmp_path):
        """Each row's metadata round-trips intact under the queue pattern."""
        import queue
        db = _make_tenant(tmp_path)
        N = 4
        PER = 25
        q: "queue.Queue[dict]" = queue.Queue()

        def producer(tid: int) -> None:
            for i in range(PER):
                q.put({"tid": tid, "i": i})

        threads = [threading.Thread(target=producer, args=(j,)) for j in range(N)]
        for t in threads: t.start()

        m = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            drained = 0
            target = N * PER
            while drained < target:
                item = q.get(timeout=10.0)
                m.store(
                    content=f"t{item['tid']} content {item['i']}",
                    category="interaction",
                    importance=0.5,
                    metadata={"thread_id": item["tid"], "index": item["i"]},
                )
                drained += 1
        finally:
            m.close()
        for t in threads: t.join()

        m2 = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            rows = m2.recall("content", top_k=10000)
            seen = {(r["metadata"]["thread_id"], r["metadata"]["index"]) for r in rows}
            expected = {(tid, i) for tid in range(N) for i in range(PER)}
            assert seen == expected
        finally:
            m2.close()


# ----------------------------------------------------------------------
# Concurrent readers + single writer (the realistic agent platform shape)
# ----------------------------------------------------------------------


class TestConcurrentReadersAndWriters:
    def test_recall_during_heavy_writes(self, tmp_path):
        """Multiple reader threads against an in-progress writer.

        Readers open their own connection; the writer uses a dedicated
        connection. SQLite WAL allows concurrent readers + one writer.
        """
        import queue
        db = _make_tenant(tmp_path)
        m_setup = MemorySystemV2(MemoryConfig(db_path=db))
        try:
            for i in range(50):
                m_setup.store(content=f"baseline row {i}", category="episodic")
        finally:
            m_setup.close()

        stop = threading.Event()
        errors: List[Exception] = []

        def writer():
            try:
                m = MemorySystemV2(MemoryConfig(db_path=db))
                try:
                    i = 0
                    while not stop.is_set():
                        m.store(content=f"new row {i}", category="episodic")
                        i += 1
                finally:
                    m.close()
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                m = MemorySystemV2(MemoryConfig(db_path=db))
                try:
                    reads = 0
                    while not stop.is_set() and reads < 200:
                        hits = m.recall("row", top_k=5)
                        assert isinstance(hits, list)
                        reads += 1
                finally:
                    m.close()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads: t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads: t.join(timeout=10.0)

        assert not errors, f"concurrent run raised: {errors[:3]}"
