# -*- coding: utf-8 -*-
"""Tests for the new SSHConfig.max_call_timeout_s knob (R3-N3)."""

from __future__ import annotations
import os, sys
from typing import Any, Dict, List

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent.ssh_executor import SSHExecutor, SSHConfig


def _fake_client():
    class _Stdout:
        def read(self):
            return b""

        class channel:
            @staticmethod
            def recv_exit_status():
                return 0

    class _T:
        @staticmethod
        def is_active():
            return True

    class _Client:
        def exec_command(self, cmd, timeout=None, get_pty=False):
            self.last_timeout = timeout
            self.last_cmd = cmd
            return (None, _Stdout(), _Stdout())

        def get_transport(self):
            return _T()

    return _Client()


def test_default_ceiling_is_600():
    cfg = SSHConfig()
    assert cfg.max_call_timeout_s == 600.0


def test_operator_can_raise_ceiling():
    cfg = SSHConfig(max_call_timeout_s=3600.0)
    ex = SSHExecutor(cfg)
    client = _fake_client()
    ex._client = client
    ex.run("whoami", timeout=10 ** 6)
    assert client.last_timeout == 3600.0


def test_operator_can_lower_ceiling():
    cfg = SSHConfig(max_call_timeout_s=10.0)
    ex = SSHExecutor(cfg)
    client = _fake_client()
    ex._client = client
    ex.run("whoami", timeout=30.0)
    # 30.0 is clamped to the operator-configured 10.0 ceiling.
    assert client.last_timeout == 10.0


def test_negative_timeout_falls_back_to_default():
    cfg = SSHConfig(command_timeout=12.5, max_call_timeout_s=60.0)
    ex = SSHExecutor(cfg)
    client = _fake_client()
    ex._client = client
    ex.run("whoami", timeout=-1)
    assert client.last_timeout == 12.5