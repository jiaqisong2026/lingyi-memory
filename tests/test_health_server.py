# -*- coding: utf-8 -*-
"""Tests for the production health server."""

from __future__ import annotations
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from production.health_server import (
    HealthState, start_health_server, stop_health_server,
)
from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, dict]:
    req = Request(url)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, {"body": body, "headers": dict(resp.headers)}


@pytest.fixture
def server():
    db = str(Path(tempfile.gettempdir()) / ("lingyi_test_" + os.urandom(3).hex() + ".db"))
    if os.path.exists(db):
        os.remove(db)
    mem = MemorySystemV2(MemoryConfig(db_path=db))
    port = _free_port()
    srv = start_health_server(mem, host="127.0.0.1", port=port)
    try:
        yield srv, mem, port
    finally:
        stop_health_server(srv)
        mem.close()
        if os.path.exists(db):
            os.remove(db)


class TestEndpoints:
    def test_healthz_alive(self, server):
        srv, _, port = server
        status, data = _http_get(f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert json.loads(data["body"])["status"] == "alive"

    def test_readyz_when_memory_open(self, server):
        srv, _, port = server
        status, data = _http_get(f"http://127.0.0.1:{port}/readyz")
        assert status == 200
        body = json.loads(data["body"])
        assert body["status"] == "ready"
        assert body["memory_open"] is True

    def test_readyz_503_when_not_ready(self, server):
        srv, _, port = server
        srv.health_state.mark_not_ready()
        try:
            try:
                _http_get(f"http://127.0.0.1:{port}/readyz")
            except HTTPError as e:
                assert e.code == 503
        finally:
            srv.health_state.mark_ready()

    def test_metrics_text_format(self, server):
        srv, _, port = server
        srv.health_state.incr("stores_total", 7)
        status, data = _http_get(f"http://127.0.0.1:{port}/metrics")
        assert status == 200
        assert "text/plain" in data["headers"]["Content-Type"]
        body = data["body"]
        assert "lingyi_stores_total 7" in body
        assert "# TYPE lingyi_stores_total counter" in body

    def test_stats_json(self, server):
        srv, _, port = server
        srv.health_state.incr("stores_total", 3)
        status, data = _http_get(f"http://127.0.0.1:{port}/v1/stats")
        assert status == 200
        body = json.loads(data["body"])
        assert "uptime_seconds" in body
        assert body["counters"]["stores_total"] == 3

    def test_404_for_unknown_path(self, server):
        srv, _, port = server
        try:
            _http_get(f"http://127.0.0.1:{port}/does-not-exist")
        except HTTPError as e:
            assert e.code == 404


class TestLifecycle:
    def test_stop_healthz_returns_503_during_shutdown(self, server):
        srv, _, port = server
        # Manually set shutting_down so /healthz returns 503 immediately.
        setattr(srv, "shutting_down", True)
        try:
            try:
                _http_get(f"http://127.0.0.1:{port}/healthz")
            except HTTPError as e:
                assert e.code == 503
        finally:
            setattr(srv, "shutting_down", False)

    def test_stop_is_idempotent(self):
        """Calling stop_health_server on an already-stopped server
        must not raise."""
        db = str(Path(tempfile.gettempdir()) / ("lingyi_test_" + os.urandom(3).hex() + ".db"))
        mem = MemorySystemV2(MemoryConfig(db_path=db))
        port = _free_port()
        srv = start_health_server(mem, host="127.0.0.1", port=port)
        stop_health_server(srv)
        # Second stop must be a no-op.
        stop_health_server(srv)
        mem.close()
        if os.path.exists(db):
            os.remove(db)


class TestHealthState:
    def test_counters_incr_and_set(self):
        s = HealthState()
        s.incr("a")
        s.incr("a", 4)
        s.set("b", 7.0)
        snap = s.snapshot()
        assert snap["counters"]["a"] == 5
        assert snap["counters"]["b"] == 7
        assert snap["uptime_seconds"] >= 0

    def test_ready_toggle(self):
        s = HealthState()
        assert s.is_ready() is True
        s.mark_not_ready()
        assert s.is_ready() is False
        s.mark_ready()
        assert s.is_ready() is True