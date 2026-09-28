# -*- coding: utf-8 -*-
"""Memory system v2 — three-layer orthogonal architecture.

Layer 0 (Perception): MemoryEvent extraction from tool calls.
Layer 1 (Memory): Four-quadrant storage (episodic/semantic/procedural/preference)
                 with RRF hybrid retrieval and differential decay.
Layer 2 (Cognition): Knowledge graph + active learning.

Backward-compatible API:
    from src.memory_v2 import MemorySystemV2
    mem = MemorySystemV2(db_path="...")
    mem.store(content="...", category="interaction", ...)  # same as before
    hits = mem.recall(query="...", top_k=5)               # same as before
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure we can be imported both as a package (src.memory_v2) and standalone
_pkg_root = str(Path(__file__).resolve().parent.parent)
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

from .core import (
    MemoryEvent,
    MemoryCategory,
    MemoryLevel,
    MemorySystemV2,
    MemoryConfig,
)
from .retrieval import RRFRetriever
from .decay import DifferentialDecay
from .knowledge_graph import KnowledgeGraph
from .active_learning import ActiveLearner

__all__ = [
    "MemoryEvent",
    "MemoryCategory",
    "MemoryLevel",
    "MemorySystemV2",
    "MemoryConfig",
    "RRFRetriever",
    "DifferentialDecay",
    "KnowledgeGraph",
    "ActiveLearner",
]
from .dashboard import MemoryDashboard

__all__ = [
    'MemoryEvent', 'MemoryCategory', 'MemoryLevel', 'MemorySystemV2',
    'MemoryConfig', 'RRFRetriever', 'DifferentialDecay',
    'KnowledgeGraph', 'ActiveLearner', 'MemoryDashboard',
]
