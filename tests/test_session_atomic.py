# -*- coding: utf-8 -*-
"""Tests for SessionManager._save atomic write (R3-5)."""

from __future__ import annotations
import json
from pathlib import Path

import pytest

from src.kylin_v11_agent.session import (
    AgentSession, SessionManager, TaskRecord,
)


@pytest.fixture
def mgr(tmp_path):
    return SessionManager(sessions_dir=str(tmp_path))


def test_save_writes_valid_json(mgr, tmp_path):
    s = mgr.create("test")
    s.record_task(TaskRecord(
        task_id="t1", description="x", tool="uname", argv=["-a"],
        returncode=0, success=True, latency_ms=1.0,
    ))
    mgr.update(s)

    path = tmp_path / (s.session_id + ".json")
    assert path.exists()
    # R1-4: no indent=2 anymore.
    text = path.read_text(encoding="utf-8")
    assert "\n  " not in text
    d = json.loads(text)
    assert d["session_id"] == s.session_id
    assert d["task_count"] == 1


def test_save_is_atomic_no_tmp_leftover(mgr, tmp_path):
    s = mgr.create("test")
    mgr.update(s)

    # No .tmp files should remain in the dir after a successful save.
    leftover = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover == []

    # Reload should give back the same session.
    s2 = mgr.get(s.session_id)
    assert s2 is not None
    assert s2.session_id == s.session_id