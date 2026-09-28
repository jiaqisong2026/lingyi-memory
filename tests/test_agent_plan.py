# -*- coding: utf-8 -*-
"""Tests for KylinAgent.run() with a mocked ToolRegistry.

These tests close the round-1 review gap (R1-1): the agent loop itself
was untested. The tests use a small fake ToolRegistry that records
invocations and returns canned results, so they run with no subprocess
and no SSH.
"""

from __future__ import annotations
import os, sys, tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent.agent import (
    KylinAgent, AgentConfig, AgentReport,
)
from src.kylin_v11_agent.tools import (
    ToolSpec, ToolResult, ToolRegistry,
)


# ----------------------------------------------------------------------
# A fake registry that subclasses the real ToolRegistry (R1-N2 round-6).
# ----------------------------------------------------------------------

@dataclass
class _RecordedCall:
    name: str
    argv: List[str]


class FakeToolRegistry(ToolRegistry):
    '''R1-N2 round-6: real subclass instead of duck-typing.

    Subclasses override only invoke() so the parent class owns the spec
    registration + validation surface. A regression that adds a new public
    method on ToolRegistry still propagates to the fake automatically.
    '''

    def __init__(self) -> None:
        super().__init__(allow_write=True)
        self.calls: List[_RecordedCall] = []
        self._responses: Dict[str, ToolResult] = {}

    def set_response(self, name: str, result: ToolResult) -> None:
        self._responses[name] = result

    def invoke(self, name: str, argv=None) -> ToolResult:
        argv = list(argv or [])
        self.calls.append(_RecordedCall(name=name, argv=argv))
        if name in self._responses:
            r = self._responses[name]
            return ToolResult(
                name=r.name, argv=argv,
                returncode=r.returncode,
                stdout=r.stdout, stderr=r.stderr,
                latency_ms=r.latency_ms,
                truncated=r.truncated, error=r.error,
            )
        return ToolResult(
            name=name, argv=argv, returncode=0,
            stdout='ok', stderr='', latency_ms=0.1,
        )


# ----------------------------------------------------------------------
# A read-only ToolSpec that always succeeds in the fake.
# ----------------------------------------------------------------------



def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name, description="fake " + name,
        binary="python", allow_args=["-V", "-a", "-c", "-h"],
    )


@pytest.fixture
def temp_db(tmp_path):
    return str(tmp_path / "memory_v2.db")


@pytest.fixture
def agent_with_fake_registry(temp_db):
    """Build a KylinAgent whose ToolRegistry has been swapped for the fake.

    We avoid spinning up a real MemorySystemV2 by injecting a stub memory
    that records every call without touching disk.
    """
    from src.kylin_v11_agent.memory import MemoryConfig
    a = KylinAgent(AgentConfig(
        agent_id="test",
        allow_write=False,
        memory_config=MemoryConfig(db_path=temp_db, use_project_memory=False),
    ))
    fake = FakeToolRegistry()
    for name in ("python_version", "platform_info", "uname", "os_release",
                 "memory", "disk", "process_count", "network_state",
                 "python_eval"):
        fake.register(_spec(name))
    a.registry = fake  # type: ignore[assignment]
    # Replace memory too: record calls and return harmless defaults.
    class _Mem:
        def __init__(self):
            self.stores = 0
            self.audits = 0
        def recall(self, q, top_k=5):
            return []
        def audit(self, **kwargs):
            self.audits += 1
        def store(self, **kwargs):
            self.stores += 1
            return "mem_" + str(self.stores)
        def stats(self):
            return {"stores": self.stores, "audits": self.audits}
        def close(self):
            pass
    a.memory = _Mem()  # type: ignore[assignment]
    yield a, fake
    try:
        a.close()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestPlannerKeywordRouting:
    def test_python_version_keyword(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        r = agent.run("show python version")
        assert r.success is True
        names = [c.name for c in fake.calls]
        assert "python_version" in names

    def test_disk_keyword(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        agent.run("check filesystem space")
        names = [c.name for c in fake.calls]
        assert "disk" in names

    def test_no_keyword_falls_back_to_python_version(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        r = agent.run("asdkjfhasdkjfh")
        # Should still run something via fallback; report is not a crash.
        assert r is not None
        assert r.task_id.startswith("kv11_")

    def test_argv_override_applied(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        agent.run("disk space", argv_override={"disk": ["-h", "--total"]})
        disk_calls = [c for c in fake.calls if c.name == "disk"]
        assert disk_calls and disk_calls[0].argv == ["-h", "--total"]


class TestRunReporting:
    def test_report_shape(self, agent_with_fake_registry):
        agent, _ = agent_with_fake_registry
        r = agent.run("show python version")
        d = r.to_dict()
        assert set(d.keys()) == {
            "task_id", "description", "success", "steps",
            "total_latency_ms", "memory_hits", "memory_stores", "notes",
        }
        assert d["description"] == "show python version"
        assert isinstance(d["steps"], list)
        assert d["total_latency_ms"] >= 0.0

    def test_memory_call_audit_counted(self, agent_with_fake_registry):
        agent, _ = agent_with_fake_registry
        agent.run("show python version")
        assert agent.memory.audits >= 1
        assert agent.memory.stores >= 1

    def test_get_report_smoke(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        agent.run("disk space")
        rep = agent.get_report()
        assert rep["agent_id"] == "test"
        assert "python_version" in rep["tools"]
        assert "stats" in rep
        assert rep["stats"]["total"] >= 1

class TestArgvOverrideValidation:
    """R1-N1: an unknown override key must raise, not silently ignore."""

    def test_unknown_override_key_raises(self, agent_with_fake_registry):
        agent, _ = agent_with_fake_registry
        with pytest.raises(ValueError) as exc:
            agent.run("disk space", argv_override={"nonexistent_tool": ["-x"]})
        assert "nonexistent_tool" in str(exc.value)

class TestFromExistingMemory:
    """R5 / OPS-1: KylinAgent.from_existing_memory public constructor."""

    def test_reuses_supplied_memory_handle(self):
        from src.kylin_v11_agent.agent import KylinAgent, AgentConfig
        from src.kylin_v11_agent.memory import MemoryConfig
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig as V2Cfg
        mem = MemorySystemV2(V2Cfg(db_path=":memory:"))
        try:
            agent = KylinAgent.from_existing_memory(mem, config=AgentConfig(agent_id="test"))
            assert agent.memory is mem
            assert agent.registry.has("python_version")
        finally:
            mem.close()

    def test_picks_up_extra_tools_from_config(self):
        from src.kylin_v11_agent.agent import KylinAgent, AgentConfig
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig as V2Cfg
        from src.kylin_v11_agent.tools import ToolSpec
        mem = MemorySystemV2(V2Cfg(db_path=":memory:"))
        try:
            cfg = AgentConfig(
                agent_id="test",
                extra_tools=[ToolSpec(name="noop", description="n", binary="echo", allow_args=[])],
            )
            agent = KylinAgent.from_existing_memory(mem, config=cfg)
            assert agent.registry.has("noop")
            assert agent.stats == {"total": 0, "success": 0, "failed": 0, "memory_hits": 0, "memory_stores": 0}
        finally:
            mem.close()
class TestKeywordNormalization:
    """R3-N2 round-6: keyword router must normalise punctuation/case."""

    def test_camelcase_description_still_matches(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        # Use "ShowPythonInterpreter" which after case/punct normalisation maps
        # to "show python interpreter" and hits python_version only.
        agent.run("ShowPythonInterpreter")
        names = [c.name for c in fake.calls]
        assert "python_version" in names

    def test_punctuation_separated_words_match(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        agent.run("show, python-version")
        names = [c.name for c in fake.calls]
        assert "python_version" in names

    def test_chinese_keeps_intact(self, agent_with_fake_registry):
        agent, fake = agent_with_fake_registry
        # Chinese text must NOT be replaced by spaces; the regex excludes CJK range.
        agent.run("中文测试 python_version")
        names = [c.name for c in fake.calls]
        assert "python_version" in names
class TestTaskTimeoutBudget:
    """R1-N3 round-6: task_timeout_s stops the LOOP, not in-flight calls."""

    def test_long_running_tool_is_not_interrupted_mid_flight(self, tmp_path):
        # A tool that takes 0.3 s; agent task_timeout_s = 0.05 s.
        # The first call must complete fully; subsequent calls are skipped.
        from src.kylin_v11_agent.agent import KylinAgent, AgentConfig
        from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig as V2Cfg
        from src.kylin_v11_agent.tools import ToolRegistry, ToolSpec

        class _SlowRegistry(ToolRegistry):
            def __init__(self):
                super().__init__(allow_write=True)
