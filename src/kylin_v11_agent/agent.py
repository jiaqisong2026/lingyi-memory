# -*- coding: utf-8 -*-
"""Top-level Kylin V11 OS Agent.

Pipeline (per task):
  1. Retrieve: pull prior task_experience memories relevant to this task.
  2. Plan:     produce a list of (tool, argv) calls using keyword routing.
  3. Execute:  run each call via ToolRegistry (argv, timeout, output cap).
  4. Reflect:  store the outcome (success or failure) back into memory.
  5. Report:   summarise the task for downstream callers / CLI.

Borrowed structure from Open Interpreter (Apache-2.0): plan -> execute ->
update history. Adapted to be deterministic and Kylin V11 specific.
"""

from __future__ import annotations

import json
import logging
import platform
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tools import ToolRegistry, ToolResult, ToolSpec, default_read_only_tools
from .memory import KylinAgentMemory, MemoryConfig

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    agent_id: str = "kylin_v11_agent"
    allow_write: bool = False
    extra_tools: List[ToolSpec] = field(default_factory=list)
    memory_config: Optional[MemoryConfig] = None
    # Per-call wall-clock budget for the WHOLE task (not per tool).
    # R1-N3 round-6: this budget is checked BEFORE each new tool call.
    # A single tool with timeout_s > task_timeout_s will NOT be killed
    # mid-flight; the agent loop stops starting NEW tools once the
    # budget is exhausted. Per-tool timeout enforcement is the
    # tool's own responsibility (see SSHConfig.max_call_timeout_s
    # for SSHExecutor; ToolSpec.timeout_s for the argv-only path).
    task_timeout_s: float = 60.0
    # If True, KylinAgent routes to the v2 memory bridge instead of the legacy KylinAgentMemory.
    use_v2_memory: bool = False


@dataclass
class AgentReport:
    task_id: str
    description: str
    success: bool
    steps: List[Dict[str, Any]]
    total_latency_ms: float
    memory_hits: int
    memory_stores: int
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "success": self.success,
            "steps": self.steps,
            "total_latency_ms": round(self.total_latency_ms, 2),
            "memory_hits": self.memory_hits,
            "memory_stores": self.memory_stores,
            "notes": list(self.notes),
        }


# ----------------------- keyword-based planner -----------------------

# Cross-platform keywords: python_version and platform_info are always
# available; uname/disk/etc only on POSIX / Kylin V11. The planner matches
# the first available tool in this order.

_INTENT_KEYWORDS: Dict[str, List[str]] = {
    # Always-available Python-based introspection.
    "python_version": ["python version", "python -v", "interpreter"],
    "platform_info": ["platform", "host info", "system info"],
    # Kylin V11 / POSIX only (router falls back if missing).
    "uname": ["kernel", "version", "arch", "x86_64", "aarch64"],
    "os_release": ["distro", "release", "kylin"],
    "uptime": ["uptime", "running since"],
    "memory": ["memory", "ram", "swap"],
    "disk": ["disk", "filesystem", "fs", "space"],
    "process_count": ["cpu", "cores", "processor"],
    "network_state": ["network", "socket", "port", "listening"],
    "memory_db": ["memory db", "agent memory"],
    "agent_binary": ["agent binary", "agent bin"],
    # Windows-specific.
    "winver": ["windows version", "win ver"],
    "systeminfo": ["windows system", "systeminfo"],
}


def _platform_eval_argv() -> List[str]:
    """Build a safe argv for `python -c "print(platform.platform())"`."""
    expr = "import platform,sys;print(platform.platform())"
    return ["-c", expr]


def _python_version_argv() -> List[str]:
    return ["-V"]


class KylinAgent:
    """OS Agent targeting the Kylin V11 environment."""

    def __init__(self, config: Optional[AgentConfig] = None):
        self.config = config or AgentConfig()
        self.registry = ToolRegistry(allow_write=self.config.allow_write)
        for spec in default_read_only_tools():
            self.registry.register(spec)
        for spec in self.config.extra_tools:
            try:
                self.registry.register(spec)
            except PermissionError as e:
                logger.warning("extra tool rejected: %s", e)

        mem_cfg = self.config.memory_config or MemoryConfig()
        self._using_v2_memory = False
        if self.config.use_v2_memory:
            try:
                from .memory_v2 import MemoryConfig as _V2Cfg
                from .memory_v2.bridge import KylinAgentMemoryBridge
                v2_cfg = _V2Cfg(db_path=mem_cfg.db_path or chr(34)+chr(34))
                self.memory = KylinAgentMemoryBridge(v2_cfg)
                self._using_v2_memory = True
                logger.info("KylinAgent using v2 memory bridge")
            except Exception as e:
                logger.warning("v2 memory unavailable (%s); falling back to legacy", e)
        if not self._using_v2_memory:
            self.memory = KylinAgentMemory(mem_cfg)
        self.stats: Dict[str, int] = {
            "total": 0, "success": 0, "failed": 0,
            "memory_hits": 0, "memory_stores": 0,
        }

    # ------------- public -------------

    def run(self, description: str,
            *, argv_override: Optional[Dict[str, List[str]]] = None) -> AgentReport:
        task_id = "kv11_" + uuid.uuid4().hex[:8]
        start = time.time()
        notes: List[str] = []

        hits = self._recall(description)
        if hits:
            self.stats["memory_hits"] += 1
            notes.append(f"memory hit: {len(hits)}")

        plan = self._plan(description, argv_override=argv_override or {})
        steps_report: List[Dict[str, Any]] = []
        any_success = False
        all_success = True

        for tool, argv in plan:
            if time.time() - start > self.config.task_timeout_s:
                notes.append(f"task timeout after {self.config.task_timeout_s}s")
                break
            result = self.registry.invoke(tool, argv)
            content = (result.stdout or result.stderr or "").strip()
            error = result.error or ("" if result.ok else f"rc={result.returncode}")
            self.memory.audit(
                success=result.ok,
                tool=tool, argv=argv,
                returncode=result.returncode,
                latency_ms=result.latency_ms,
                content=content[:512], error=error,
            )
            mid = self.memory.store(
                content=description, success=result.ok,
                tool=tool, argv=argv,
                returncode=result.returncode,
                latency_ms=result.latency_ms,
                error=error,
            )
            if mid:
                self.stats["memory_stores"] += 1
            if result.ok:
                any_success = True
            else:
                all_success = False
            steps_report.append({"tool": tool, "argv": argv, **result.to_dict()})

        total_latency_ms = (time.time() - start) * 1000.0
        success = all_success and bool(plan)
        self.stats["total"] += 1
        if success:
            self.stats["success"] += 1
        else:
            self.stats["failed"] += 1
        if not plan:
            notes.append("no plan matched description keywords")
            success = False

        return AgentReport(
            task_id=task_id,
            description=description,
            success=success,
            steps=steps_report,
            total_latency_ms=total_latency_ms,
            memory_hits=len(hits) if hits else 0,
            memory_stores=self.stats["memory_stores"] if (any_success or not all_success) else 0,
            notes=notes,
        )

    def get_report(self) -> Dict[str, Any]:
        return {
            "agent_id": self.config.agent_id,
            "tools": self.registry.names(),
            "memory": self.memory.stats(),
            "stats": dict(self.stats),
            "using_v2_memory": self._using_v2_memory,
        }

    def close(self) -> None:
        try:
            self.memory.close()
        except Exception:
            pass

    # ------------- internals -------------

    def _recall(self, description: str) -> List[Dict[str, Any]]:
        return self.memory.recall(description, top_k=3)

    def _plan(self, description: str, *, argv_override: Dict[str, List[str]]) -> List[tuple[str, List[str]]]:
        # R1-N1: every key in argv_override must reference a tool the
        # registry knows about. A planner bug supplying the override on
        # the wrong tool name is now a loud ValueError instead of a
        # silent no-op.
        unknown = [k for k in argv_override if not self.registry.has(k)]
        if unknown:
            raise ValueError(
                "argv_override references unknown tool(s): " + repr(unknown)
            )
        # R3-N2 round-6: collapse non-letter chars to spaces and lower case
        # so a description like 'PythonVersion' or 'show Python.Version'
        # still matches the keyword 'python version'.
        import re as _re
        text = _re.sub(r'[^a-z0-9\u4e00-\u9fff]+', ' ', description.lower())
        chosen: List[tuple[str, List[str]]] = []
        for tool, words in _INTENT_KEYWORDS.items():
            if not self.registry.has(tool):
                continue
            if any(w in text for w in words):
                argv = list(argv_override.get(tool, []))
                # Sensible default argv if caller did not override.
                if not argv:
                    if tool == "os_release":
                        argv = ["/etc/os-release"]
                    elif tool == "memory_db":
                        argv = ["/var/lib/kylin_v11_agent/memory.db"]
                    elif tool == "agent_binary":
                        argv = ["-la", "/opt/kylin-v11-agent/bin"]
                    elif tool == "uname":
                        argv = ["-a"]
                    elif tool == "process_count":
                        argv = ["--all"]
                    elif tool == "uptime":
                        argv = ["-p"]
                    elif tool == "memory":
                        argv = ["-h"]
                    elif tool == "disk":
                        argv = ["-h"]
                    elif tool == "network_state":
                        argv = ["-t", "-l", "-n"]
                    elif tool == "python_version":
                        argv = _python_version_argv()
                    elif tool == "platform_info":
                        argv = _platform_eval_argv()
                    elif tool == "python_eval":
                        argv = ["-c", "print('ok')"]
                    elif tool == "winver":
                        argv = ["/c", "ver"]
                chosen.append((tool, argv))
        if not chosen and self.registry.has("python_version"):
            chosen.append(("python_version", list(argv_override.get("python_version", ["-V"]))))
        if not chosen and self.registry.has("uname"):
            chosen.append(("uname", list(argv_override.get("uname", ["-a"]))))
        return chosen

    # ------------- alternate constructors -------------
    @classmethod
    def from_existing_memory(cls, memory: Any, config: Optional[AgentConfig] = None) -> 'KylinAgent':
        '''Build a KylinAgent that reuses a caller-supplied memory handle.

        Why this exists: the demo (and integration scripts) want to drive
        a pre-constructed `MemorySystemV2` (e.g. opened with a specific
        `db_path`) without round-tripping through `AgentConfig.memory_config`.
        Using `KylinAgent.__new__` works but is brittle; this is the public
        supported way (R5 / OPS-1).
        '''
        cfg = config or AgentConfig()
        agent = cls.__new__(cls)
        agent.config = cfg
        agent.registry = ToolRegistry(allow_write=cfg.allow_write)
        for spec in default_read_only_tools():
            agent.registry.register(spec)
        for spec in cfg.extra_tools:
            try:
                agent.registry.register(spec)
            except PermissionError as e:
                logger.warning('extra tool rejected: %s', e)
        agent.memory = memory
        agent._using_v2_memory = getattr(memory, 'is_v2', False)
        agent.stats = {'total': 0, 'success': 0, 'failed': 0, 'memory_hits': 0, 'memory_stores': 0}
        return agent
