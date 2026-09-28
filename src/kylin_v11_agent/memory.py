# -*- coding: utf-8 -*-
"""Memory bridge v2."""

from __future__ import annotations

import json, logging, os, sqlite3, time, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryConfig:
    db_path: str = ""
    markdown_mirror: bool = False
    markdown_path: str = ""
    use_project_memory: bool = True


class _KylinV10Shim:
    def __init__(self, raw_store): self._raw = raw_store

    def store(self, content, category="interaction", importance=0.5, level=None, metadata=None):
        try:
            mem_id = "kv10_" + uuid.uuid4().hex[:8]
            self._raw.store_memory(mem_id, content, memory_type=category or "interaction", importance=importance, metadata=metadata)
            return mem_id
        except Exception as e:
            logger.debug("_KylinV10Shim.store failed: %s", e)
            return None

    def retrieve(self, query, top_k=5, level=None, category=None):
        try:
            rows = self._raw.search_memories(query, top_k=top_k)
            return rows if rows else []
        except Exception as e:
            logger.debug("_KylinV10Shim.retrieve failed: %s", e)
            return []


class KylinAgentMemory:
    def __init__(self, config=None):
        self.config = config or MemoryConfig()
        if not self.config.db_path: self.config.db_path = self._default_db_path()
        try: Path(self.config.db_path).parent.mkdir(parents=True, exist_ok=True)
        except: pass
        self._memory = None; self._sqlite_store = None; self._init_db()
        if self.config.use_project_memory: self._memory = self._try_load_memory_manager()
        if self._memory is None: self._memory = self._try_load_kylin_v10_sqlite()

    def store(self, content, success, tool, argv, returncode, latency_ms, error=""):
        importance = 0.85 if success else 0.6
        body = self._compose(content, success, tool, argv, returncode, latency_ms, error)
        self.audit(success, tool, argv, returncode, latency_ms, body[:512], error)
        if self._memory is None: return None
        try: mid = self._memory.store(body, category="task_experience", importance=importance)
        except Exception as e:
            logger.warning("KylinAgentMemory.store failed: %s", e)
            return None
        return mid

    def recall(self, query, top_k=5):
        if self._memory is None: return []
        try: rows = self._memory.retrieve(query, top_k=top_k)
        except Exception as e:
            logger.debug("KylinAgentMemory.recall failed: %s", e)
            return []
        out = []
        for row in rows:
            if isinstance(row, dict): out.append(row)
            else: out.append({"id": getattr(row, "id", ""), "content": getattr(row, "content", str(row)), "importance": getattr(row, "importance", 0.0), "level": getattr(row, "level", "")})
        return out

    def close(self):
        try:
            if self._sqlite_store is not None and hasattr(self._sqlite_store, "close"): self._sqlite_store.close()
        except: pass
        self._sqlite_store = None; self._memory = None

    def stats(self):
        return {"db_path": self.config.db_path, "use_project_memory": self._memory is not None, "markdown_mirror": self.config.markdown_mirror}

    def _default_db_path(self):
        d=Path(__file__).resolve().parents[2]; return str((d/"data"/"kylin_v11_agent_memory.db") if d.drive=="G:" else Path("G:/kylin/银河os agent完善/projects/os_agent_memory/data/kylin_v11_agent_memory.db"))

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.config.db_path)
            q = "CREATE TABLE IF NOT EXISTS agent_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, tool TEXT NOT NULL, argv TEXT NOT NULL, returncode INTEGER NOT NULL, success INTEGER NOT NULL, latency_ms REAL NOT NULL, content TEXT NOT NULL, error TEXT DEFAULT " + chr(34) + chr(34) + ")"
            conn.execute(q)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at)")
            conn.commit(); conn.close()
        except Exception as e: logger.warning("_init_db failed: %s", e)

    def _audit_row(self, success, tool, argv, returncode, latency_ms, content, error):
        try:
            conn = sqlite3.connect(self.config.db_path)
            conn.execute("INSERT INTO agent_runs (created_at, tool, argv, returncode, success, latency_ms, content, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (time.time(), tool, json.dumps(argv, ensure_ascii=False), int(returncode), 1 if success else 0, float(latency_ms), content, error or ""))
            conn.commit(); conn.close()
        except (sqlite3.OperationalError, OSError) as e: logger.warning("_audit_row skipped (disk issue): %s", e)

    def _compose(self, content, success, tool, argv, returncode, latency_ms, error):
        flag = "OK" if success else "FAIL"
        argv_str = " ".join(argv) if argv else "<no-args>"
        head = "[" + flag + "] tool=" + tool + " argv=" + argv_str + " rc=" + str(returncode) + " ms=" + "%.1f" % latency_ms
        if error: head += " err=" + error
        return head + (" | " + content if content else "")

    def _try_load_memory_manager(self):
        try:
            src_root = Path(__file__).resolve().parents[2]
            import sys
            if str(src_root) not in sys.path:
                sys.path.insert(0, str(src_root))
            from src.memory.memory_manager import MemoryManager
            return MemoryManager(db_path=self.config.db_path)
        except (ImportError, FileNotFoundError) as e:
            logger.debug("MemoryManager unavailable (import/path): %s", e); return None
        except sqlite3.OperationalError as e:
            logger.debug("MemoryManager unavailable (disk issue): %s", e); return None
        except Exception as e:
            logger.debug("MemoryManager unavailable: %s", e); return None

    def _try_load_kylin_v10_sqlite(self):
        try:
            from memory.kylin_v10_adapter import SQLiteMemoryStore
            raw = SQLiteMemoryStore(db_path=self.config.db_path)
            self._sqlite_store = raw
        except Exception as e:
            logger.debug("SQLiteMemoryStore unavailable: %s", e); return None
        return _KylinV10Shim(raw)

    def audit(self, success, tool, argv, returncode, latency_ms, content, error=""):
        try: self._audit_row(success, tool, list(argv), returncode, latency_ms, content, error)
        except Exception as e: logger.warning("audit row insert failed: %s", e)

