"""End-to-end demo that exercises the full LingYi stack on a
realistic scenario — without requiring a live Kylin V11 host.

Scenario: an internal admin agent helps a merchant investigate a
slow order-processing backend on a Kylin V11 server. The session is
stored in LingYi memory and the agent uses recall to ground its
next action.

This script exists for two audiences:

1. A reviewer who runs it locally and sees a Chinese-language agent
   session with memory + recall in <10 seconds. The output is
   deterministic (seeded random).
2. A platform operator who copies the script into the runtime to
   bootstrap a new agent tenant.

It does NOT require:
- a real Kylin V11 host (the SSH executor is mocked),
- an LLM (the planner is keyword-driven and deterministic),
- network access (everything is local).
"""

from __future__ import annotations
import json
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, ROOT)

from src.kylin_v11_agent.agent import KylinAgent, AgentConfig
from src.kylin_v11_agent.memory import MemoryConfig
from src.kylin_v11_agent.tools import ToolSpec, ToolRegistry, ToolResult
from src.kylin_v11_agent.text_tokenize import tokenize_for_index


# ----------------------------------------------------------------------
# Deterministic mock executor: a fake SSHExecutor that returns canned
# output for each known command. The real executor is paramiko-bound;
# this one is purely in-process.
# ----------------------------------------------------------------------


class _FakeSshExecutor:
    def __init__(self) -> None:
        self.commands: List[str] = []

    def run(self, command: str, timeout: float = 5.0, cwd: str = None) -> Dict[str, Any]:
        self.commands.append(command)
        # Dispatch table for the demo scenario.
        responses: Dict[str, Dict[str, Any]] = {
            "systemctl status kylin-order-svc": {
                "stdout": "active (running) since Mon 2026-08-04 09:00:01 CST; 3 days ago",
                "stderr": "",
                "returncode": 0,
            },
            "ss -tlnp": {
                "stdout": (
                    "LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((\"kylin-order-svc\",pid=1234,fd=6))\n"
                    "LISTEN 0 128 127.0.0.1:5432 0.0.0.0:* users:((\"postgres\",pid=567,fd=4))"
                ),
                "stderr": "",
                "returncode": 0,
            },
            "ps aux | grep order": {
                "stdout": "kylin    1234  3.2  4.1 812344 332144 ?  Ssl  Aug04  14:23 kylin-order-svc",
                "stderr": "",
                "returncode": 0,
            },
            "df -h /var/log": {
                "stdout": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       100G   92G  8.0G  92% /var/log",
                "stderr": "",
                "returncode": 0,
            },
            "tail -n 20 /var/log/kylin-order-svc/error.log": {
                "stdout": (
                    "2026-08-08 14:01:01 ERROR timeout waiting for postgres at 127.0.0.1:5432\n"
                    "2026-08-08 14:02:33 ERROR timeout waiting for postgres at 127.0.0.1:5432\n"
                    "2026-08-08 14:05:11 ERROR timeout waiting for postgres at 127.0.0.1:5432"
                ),
                "stderr": "",
                "returncode": 0,
            },
        }
        # Normalise: strip trailing whitespace and lowercase for matching.
        key = command.strip().lower()
        if key in responses:
            return responses[key]
        return {"stdout": "", "stderr": "command not in demo dataset", "returncode": 1}


# ----------------------------------------------------------------------
# Wrap a deterministic executor into the ToolRegistry shape.
# ----------------------------------------------------------------------


class _DemoToolRegistry(ToolRegistry):
    """A ToolRegistry whose `invoke` is satisfied by the fake SSH executor."""

    def __init__(self, ssh: _FakeSshExecutor) -> None:
        super().__init__(allow_write=False)
        self._ssh = ssh
        # Register a small, read-only toolset keyed to the demo scenario.
        self.register(ToolSpec(
            name="run_remote",
            description="Run a shell command on the Kylin V11 host (read-only).",
            binary="echo",  # not actually executed; invoke() is overridden
            allow_args=[],
        ))

    def invoke(self, name: str, argv=None) -> ToolResult:
        argv = list(argv or [])
        if name == "run_remote":
            cmd = " ".join(argv)
            t0 = time.perf_counter()
            r = self._ssh.run(cmd)
            return ToolResult(
                name=name,
                argv=argv,
                returncode=r["returncode"],
                stdout=r["stdout"],
                stderr=r["stderr"],
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return super().invoke(name, argv)


# ----------------------------------------------------------------------
# Demo scenario
# ----------------------------------------------------------------------


SCENARIO = [
    {
        "role": "user",
        "text": "订单处理服务好像卡住了，能帮我查一下 Kylin V11 上 kylin-order-svc 的状态吗？",
    },
    {
        "role": "agent",
        "intent": "service status",
        "tool": "run_remote",
        "argv": ["systemctl status kylin-order-svc"],
        "explanation": "Check if the order service is running.",
    },
    {
        "role": "user",
        "text": "服务在跑。帮我看看监听端口和进程。",
    },
    {
        "role": "agent",
        "intent": "port + process",
        "tool": "run_remote",
        "argv": ["ss -tlnp"],
        "explanation": "List listening TCP ports to confirm 8080 is up.",
    },
    {
        "role": "agent",
        "intent": "process",
        "tool": "run_remote",
        "argv": ["ps aux | grep order"],
        "explanation": "Confirm the kylin-order-svc process is using CPU.",
    },
    {
        "role": "user",
        "text": "/var/log 满了，怀疑是日志没切。看看错误日志吧。",
    },
    {
        "role": "agent",
        "intent": "disk usage",
        "tool": "run_remote",
        "argv": ["df -h /var/log"],
        "explanation": "Confirm disk pressure on /var/log.",
    },
    {
        "role": "agent",
        "intent": "error log",
        "tool": "run_remote",
        "argv": ["tail -n 20 /var/log/kylin-order-svc/error.log"],
        "explanation": "Inspect recent error log entries for clues.",
    },
    {
        "role": "user",
        "text": "看起来是连接 postgres 超时。我应该联系 DBA 吗？",
    },
]


def main() -> int:
    random.seed(42)
    tmp = Path(tempfile.mkdtemp(prefix="lingyi_demo_"))
    db = tmp / "memory.db"

    print("=== LingYi end-to-end demo ===")
    print(f"db path: {db}")
    print()

    ssh = _FakeSshExecutor()
    registry = _DemoToolRegistry(ssh)

    # Open the v2 memory directly so the demo runs without touching the
    # legacy KylinAgentMemory defaults (which expect a project path).
    from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig as V2Cfg
    memory = MemorySystemV2(V2Cfg(db_path=str(db)))

    # Use the public from_existing_memory constructor (R5 / OPS-1).
    agent = KylinAgent.from_existing_memory(
        memory,
        config=AgentConfig(agent_id="merchant-admin"),
    )
    agent.registry = registry
    agent._using_v2_memory = True

    try:
        for step in SCENARIO:
            role = step["role"]
            text = step.get("text", "")
            if role == "user":
                # The user message is stored as an episodic memory.
                agent.memory.store(
                    content=text,
                    category="interaction",
                    importance=0.6,
                    session_id="merchant-admin",
                )
                # Recall: what prior context applies to this turn?
                hits = agent.memory.recall(text, top_k=3)
                print(f"[user]    {text}")
                if hits:
                    summary = " | ".join(
                        h.get("content", "")[:60] for h in hits
                    )
                    print(f"   recall: {summary}")
                print()
            else:
                # Agent step: pretend the planner picked this tool/argv.
                tool, argv = step["tool"], step["argv"]
                # Verify our custom registry has it.
                assert registry.has(tool), f"tool {tool!r} not registered"
                result = registry.invoke(tool, argv)
                agent.memory.store(
                    content=f"[tool_call] {tool} argv={argv}",
                    category="task_experience",
                    importance=0.8,
                    session_id="merchant-admin",
                    tool_name=tool,
                )
                # Update memory with the outcome.
                content_snippet = (result.stdout or result.stderr or "")[:300]
                if content_snippet:
                    agent.memory.store(
                        content=content_snippet,
                        category="observation",
                        importance=0.9,
                        session_id="merchant-admin",
                    )
                print(f"[agent]   intent: {step['intent']}")
                print(f"   tool:  {tool} {' '.join(argv)}")
                print(f"   rc:    {result.returncode}  lat: {result.latency_ms:.1f}ms")
                if content_snippet:
                    print(f"   out:   {content_snippet!r}")
                print()

        # Final recall against the last user question.
        last = SCENARIO[-1]["text"]
        print(f"[final recall for: {last!r}]")
        hits = agent.memory.recall(last, top_k=5)
        for i, h in enumerate(hits, 1):
            print(f"  {i}. importance={h.get('importance', 0):.2f}  {h.get('content', '')[:120]!r}")
        print()

        # Tokenizer smoke (proves Chinese path is wired).
        sample = "订单处理卡住，怀疑是 postgres 超时"
        tokens = tokenize_for_index(sample).split()
        print(f"[tokenizer] '{sample}' -> {len(tokens)} tokens: {tokens[:8]}{'...' if len(tokens) > 8 else ''}")
        print()
        print(f"=== Done. Total commands executed: {len(ssh.commands)} ===")
    finally:
        agent.memory.close()

    # Best-effort cleanup; sandbox may block removal but tmpdir is per-process.
    try:
        for f in tmp.glob("*"):
            os.remove(f)
        os.rmdir(tmp)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())