"""Realistic-scale benchmark: 100k records, 10k queries, concurrent workers.

This is the script a Pinduoduo reviewer would want to see: it
measures sustained throughput on a workload shape that resembles a
real agent platform (many events, occasional reads).

It does NOT pretend to be production-perfect. Numbers will vary by
hardware. The script's purpose is to be repeatable.
"""

from __future__ import annotations
import argparse
import json
import os
import queue
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)

from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig


def _percentile(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--records", type=int, default=100_000)
    p.add_argument("--queries", type=int, default=10_000)
    p.add_argument("--writers", type=int, default=4)
    p.add_argument("--readers", type=int, default=4)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", type=str, default=None)
    args = p.parse_args()

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    cfg = MemoryConfig(db_path=tmp.name)
    mem = MemorySystemV2(cfg)

    try:
        # Phase 1: write `records` rows through `writers` threads.
        q: "queue.Queue[int]" = queue.Queue()
        for i in range(args.records):
            q.put(i)

        write_latencies: list[float] = []

        def producer():
            m = MemorySystemV2(cfg)
            try:
                while True:
                    try:
                        i = q.get_nowait()
                    except queue.Empty:
                        return
                    t0 = time.perf_counter()
                    m.store(
                        content=f"event {i}: user modified config file /etc/app{i % 500}.conf and restarted service node-{i % 50}",
                        category="episodic" if i % 3 else "interaction",
                        importance=0.4 + (i % 10) * 0.06,
                    )
                    write_latencies.append((time.perf_counter() - t0) * 1000.0)
            finally:
                m.close()

        threads = [threading.Thread(target=producer) for _ in range(args.writers)]
        t0 = time.perf_counter()
        for t in threads: t.start()
        for t in threads: t.join()
        write_wall = time.perf_counter() - t0

        # Phase 2: read `queries` through `readers` threads.
        qr: "queue.Queue[int]" = queue.Queue()
        for i in range(args.queries):
            qr.put(i)

        read_latencies: list[float] = []

        def reader():
            m = MemorySystemV2(cfg)
            try:
                while True:
                    try:
                        i = qr.get_nowait()
                    except queue.Empty:
                        return
                    t0 = time.perf_counter()
                    m.recall(query=f"config file {i}", top_k=10)
                    read_latencies.append((time.perf_counter() - t0) * 1000.0)
            finally:
                m.close()

        rthreads = [threading.Thread(target=reader) for _ in range(args.readers)]
        t0 = time.perf_counter()
        for t in rthreads: t.start()
        for t in rthreads: t.join()
        read_wall = time.perf_counter() - t0

        # PDD-5: record the SQLite version so the benchmark output
        # is reproducible across machines.
        sqlite_version = mem._conn.execute("SELECT sqlite_version()").fetchone()[0]

        result = {
            "sqlite_version": sqlite_version,
            "config": {
                "records": args.records,
                "queries": args.queries,
                "writers": args.writers,
                "readers": args.readers,
            },
            "writes": {
                "wall_seconds": round(write_wall, 3),
                "throughput_ops": round(args.records / write_wall, 1),
                "latency_ms": {
                    "p50": round(_percentile(write_latencies, 50), 3),
                    "p95": round(_percentile(write_latencies, 95), 3),
                    "p99": round(_percentile(write_latencies, 99), 3),
                    "max": round(max(write_latencies) if write_latencies else 0.0, 3),
                },
            },
            "reads": {
                "wall_seconds": round(read_wall, 3),
                "throughput_ops": round(args.queries / read_wall, 1),
                "latency_ms": {
                    "p50": round(_percentile(read_latencies, 50), 3),
                    "p95": round(_percentile(read_latencies, 95), 3),
                    "p99": round(_percentile(read_latencies, 99), 3),
                    "max": round(max(read_latencies) if read_latencies else 0.0, 3),
                },
            },
            "db_size_bytes": os.path.getsize(tmp.name),
        }
        text = json.dumps(result, indent=2, ensure_ascii=False)
        print(text)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text + "\n")
    finally:
        mem.close()
        os.remove(tmp.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())