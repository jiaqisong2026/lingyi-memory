# -*- coding: utf-8 -*-
"""Command-line interface for MemorySystemV2.

Usage examples:
    python -m src.memory_v2.cli summary --db ./data/mem.db
    python -m src.memory_v2.cli top --db ./data/mem.db --limit 5
    python -m src.memory_v2.cli gaps --db ./data/mem.db
    python -m src.memory_v2.cli recall --db ./data/mem.db "how to tune nginx"
    python -m src.memory_v2.cli prune --db ./data/mem.db --min-strength 0.3
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from . import MemoryConfig, MemorySystemV2
from .dashboard import MemoryDashboard


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_v2",
        description="Inspect and operate on a MemorySystemV2 database.",
    )
    p.add_argument(
        "--db", required=True,
        help="Path to the sqlite database (e.g. ./data/mem.db).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("summary", help="One-shot health snapshot.")

    top = sub.add_parser("top", help="Top N memories by importance.")
    top.add_argument("--limit", type=int, default=10)

    gaps = sub.add_parser("gaps", help="Recent unfilled knowledge gaps.")
    gaps.add_argument("--limit", type=int, default=10)

    confl = sub.add_parser("conflicts", help="Knowledge-graph conflicts.")
    confl.add_argument("--limit", type=int, default=20)

    recall = sub.add_parser("recall", help="Run a recall query.")
    recall.add_argument("query")
    recall.add_argument("--top-k", type=int, default=5)
    recall.add_argument("--json", action="store_true",
                        help="Print raw results as JSON.")

    prune = sub.add_parser("prune", help="Age-out weak KG edges.")
    prune.add_argument("--min-strength", type=float, default=0.3)
    prune.add_argument("--max-age-days", type=float, default=90.0)

    decay = sub.add_parser("decay", help="Memory decay statistics.")
    decay.add_argument("--json", action="store_true")

    return p


def _open(db_path: str):
    cfg = MemoryConfig(db_path=db_path)
    return MemorySystemV2(cfg), MemoryDashboard(MemorySystemV2(cfg))


def cmd_summary(args, mem, dash):
    snap = dash.summary()
    print(json.dumps(snap, ensure_ascii=False, indent=2))


def cmd_top(args, mem, dash):
    for i, m in enumerate(dash.get_top_memories(args.limit), 1):
        print(
            f"{i:>3}. [{m['category']:<11}] imp={m['importance']:.2f} "
            f"hits={m['access_count']:<3}  {m['content']}"
        )


def cmd_gaps(args, mem, dash):
    for i, g in enumerate(dash.get_recent_gaps(args.limit), 1):
        print(
            f"{i:>3}. obs={g['observation_count']:<3}  {g['query']}"
        )


def cmd_conflicts(args, mem, dash):
    items = dash.get_conflicts(args.limit)
    if not items:
        print("No conflicts detected.")
        return
    for i, c in enumerate(items, 1):
        ent = c.get("entity", "?")
        strat = c.get("resolution_strategy", "?")
        mems = c.get("conflicting_memories", [])
        print(f"{i:>3}. entity={ent}  strategy={strat}  n={len(mems)}")


def cmd_recall(args, mem, dash):
    results = mem.recall(args.query, top_k=args.top_k)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("No matches.")
        return
    for i, r in enumerate(results, 1):
        score = r.get("score", 0.0)
        cat = r.get("category", "?")
        print(
            f"{i:>3}. score={score:.4f}  [{cat}]  {r.get('content', '')}"
        )


def cmd_prune(args, mem, dash):
    n = mem.conn  # for typing; use via KG
    from .knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph(mem.conn)
    removed = kg.prune_weak_edges(
        min_strength=args.min_strength,
        max_age_seconds=args.max_age_days * 86400,
    )
    print(f"Pruned {removed} weak/stale edges.")


def cmd_decay(args, mem, dash):
    stats = dash.get_decay_stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        for k, v in stats.items():
            print(f"{k:<20} {v}")


def main(argv: List[str] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    mem, dash = _open(args.db)
    try:
        handler = {
            "summary":   cmd_summary,
            "top":       cmd_top,
            "gaps":      cmd_gaps,
            "conflicts": cmd_conflicts,
            "recall":    cmd_recall,
            "prune":     cmd_prune,
            "decay":     cmd_decay,
        }[args.cmd]
        handler(args, mem, dash)
    finally:
        mem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())