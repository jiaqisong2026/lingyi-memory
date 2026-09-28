# -*- coding: utf-8 -*-
"""Integration bridge: old KylinAgentMemory API → MemorySystemV2.

This module provides backward-compatible wrappers so existing code
using `from src.kylin_v11_agent.memory import KylinAgentMemory`
continues to work, but internally delegates to MemorySystemV2.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class KylinAgentMemoryBridge:
    """Backward-compatible wrapper around MemorySystemV2.

    Maps old method signatures to new internals:
      store(content, success, tool, argv, returncode, latency_ms, error)
        → MemorySystemV2.store(content=..., category="task_experience", ...)
      recall(query, top_k)
        → MemorySystemV2.recall(query, top_k)
      audit(success, tool, argv, returncode, latency_ms, content, error)
        → MemorySystemV2.audit(...)
    """

    def __init__(self, config=None):
        from .core import MemoryConfig, MemorySystemV2
        self.config = config or type('Cfg', (), {'db_path': '', 'use_project_memory': True})()
        if not self.config.db_path:
            self.config.db_path = self._default_db_path()
        try:
            Path(self.config.db_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        self._v2 = MemorySystemV2(
            config=MemoryConfig(db_path=self.config.db_path),
        )
        # Legacy bridge: keep a reference to old MemoryManager if available
        self._legacy_manager = None
        self._sqlite_store = None
        if getattr(self.config, 'use_project_memory', True):
            self._legacy_manager = self._try_load_legacy()

    # ------------------------------------------------------------------
    # Public API — backward compatible
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        success: bool,
        tool: str,
        argv: List[str],
        returncode: int,
        latency_ms: float,
        error: str = "",
    ) -> Optional[str]:
        importance = 0.85 if success else 0.6
        body = self._compose(content, success, tool, argv, returncode, latency_ms, error)
        self.audit(success, tool, argv, returncode, latency_ms, body[:512], error)
        if self._legacy_manager is not None:
            try:
                mid = self._legacy_manager.store(
                    body, category="task_experience", importance=importance
                )
                return mid
            except Exception as e:
                logger.warning("Legacy store failed, falling back to v2: %s", e)
        return self._v2.store(
            content=body,
            category="task_experience",
            importance=importance,
        )

    def recall(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if self._legacy_manager is not None:
            try:
                rows = self._legacy_manager.retrieve(query, top_k=top_k)
                out = []
                for row in rows:
                    if isinstance(row, dict):
                        out.append(row)
                    else:
                        out.append({
                            "id": getattr(row, "id", ""),
                            "content": getattr(row, "content", str(row)),
                            "importance": getattr(row, "importance", 0.0),
                            "level": getattr(row, "level", ""),
                        })
                if out:
                    return out
            except Exception as e:
                logger.debug("Legacy recall failed: %s", e)
        return self._v2.recall(query, top_k=top_k)

    def close(self) -> None:
        try:
            self._v2.close()
        except Exception:
            pass
        self._legacy_manager = None

    def stats(self) -> Dict[str, Any]:
        v2_stats = self._v2.stats()
        return {
            **v2_stats,
            "use_project_memory": self._legacy_manager is not None,
            "markdown_mirror": getattr(self.config, 'markdown_mirror', False),
        }

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
        self._v2.audit(success, tool, argv, returncode, latency_ms, content, error)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compose(
        content: str, success: bool, tool: str, argv: List[str],
        returncode: int, latency_ms: float, error: str,
    ) -> str:
        flag = "OK" if success else "FAIL"
        argv_str = " ".join(argv) if argv else "<no-args>"
        head = f"[{flag}] tool={tool} argv={argv_str} rc={returncode} ms={latency_ms:.1f}"
        if error:
            head += f" err={error}"
        return head + (" | " + content if content else "")

    def _try_load_legacy(self) -> Any:
        try:
            src_root = Path(__file__).resolve().parents[2]
            import sys
            if str(src_root) not in sys.path:
                sys.path.insert(0, str(src_root))
            from src.memory.memory_manager import MemoryManager
            return MemoryManager(db_path=self.config.db_path)
        except Exception as e:
            logger.debug("Legacy MemoryManager unavailable: %s", e)
            return None

    def _default_db_path(self) -> str:
        d = Path(__file__).resolve().parents[2]
        if d.drive == "G:":
            return str(d / "data" / "kylin_v11_agent_memory.db")
        return str(Path("G:/kylin/银河os agent完善/projects/os_agent_memory/data/kylin_v11_agent_memory.db"))
