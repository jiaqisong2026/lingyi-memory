#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ingest an opencode session JSON file into a LingYi memory database.

Usage:
    python scripts/ingest_opencode_session.py --session FILE --db PATH

After ingest, the memory is left on disk; use any reader (CLI dashboard,
the demo script, or the API) to recall from it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.integrations.opencode_adapter.indexer import OpencodeIndexer
from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig


def main() -> int:
    logging.basicConfig(level=os.environ.get('LINGYI_LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--session', required=True, help='path to an opencode session.json file')
    ap.add_argument('--db', required=True, help='path to the LingYi SQLite database to write into')
    ap.add_argument('--category', default='interaction', help='memory category override (default: interaction)')
    ap.add_argument('--importance', type=float, default=0.5, help='importance scale 0..1 (default 0.5)')
    args = ap.parse_args()

    session_path = Path(args.session)
    if not session_path.exists():
        print('session file not found:', session_path, file=sys.stderr)
        return 2

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    memory = MemorySystemV2(MemoryConfig(db_path=str(db_path)))
    try:
        idx = OpencodeIndexer(memory, default_category=args.category, importance_scale=args.importance)
        stored = idx.index_file(session_path)
        print(f'ingested {stored} message(s) from {session_path.name} into {db_path}')
        return 0
    finally:
        memory.close()


if __name__ == '__main__':
    sys.exit(main())
