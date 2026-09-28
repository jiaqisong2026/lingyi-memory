# -*- coding: utf-8 -*-
'''Opt-in real SSH E2E tests for SSHExecutor.

Guards (any one failing => module-level skip, NOT fail):
1. env KYLIN_SSH_E2E != '1'  -> skip (default off)
2. paramiko not installed    -> skip
3. TCP connect to host:port within 2s fails -> skip (no live SSH)

Default: ALL tests SKIPPED. Enable by setting KYLIN_SSH_E2E=1 plus live SSH.
'''

from __future__ import annotations

import os
import sys
import socket
import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if '银河os' not in p]

from src.kylin_v11_agent.ssh_executor import (
    SSHConfig,
    CommandResult,
    SSHExecutor,
    _HAS_PARAMIKO,
)

# ----- module-level guards -----

SSH_HOST = os.environ.get('KYLIN_SSH_HOST', '127.0.0.1')
SSH_PORT = int(os.environ.get('KYLIN_SSH_PORT', '2222'))


def _tcp_alive(host, port, timeout=2.0):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


if os.environ.get('KYLIN_SSH_E2E') != '1':
    pytest.skip('KYLIN_SSH_E2E != 1, opt-in only', allow_module_level=True)

if not _HAS_PARAMIKO:
    pytest.skip('paramiko not installed', allow_module_level=True)

if not _tcp_alive(SSH_HOST, SSH_PORT):
    pytest.skip('no live SSH at %s:%d' % (SSH_HOST, SSH_PORT), allow_module_level=True)


# ----- fixtures -----

@pytest.fixture
def ssh_config():
    return SSHConfig(
        host=SSH_HOST,
        port=SSH_PORT,
        username=os.environ.get('KYLIN_SSH_USER', 'kylin'),
        password=os.environ.get('KYLIN_SSH_PASSWORD', ''),
        key_filename=os.environ.get('KYLIN_SSH_KEY'),
        timeout=10.0,
        command_timeout=30.0,
    )


@pytest.fixture
def executor(ssh_config):
    ex = SSHExecutor(ssh_config)
    try:
        ex.connect()
    except Exception as e:
        pytest.skip('SSH connect failed: %s' % e)
    yield ex
    ex.close()


# ----- tests -----


def test_e2e_env_disabled_by_default():
    # Reaffirms layer 1: env opt-in. This test only runs if all module-level guards pass.
    assert os.environ.get('KYLIN_SSH_E2E') == '1'


def test_e2e_tcp_probe_fails():
    # Reaffirms layer 3 helper: reserved port must refuse quickly.
    assert not _tcp_alive('127.0.0.1', 1, timeout=0.5)


def test_e2e_paramiko_connect_and_run_echo(executor):
    result = executor.run('echo hello')
    assert result.ok, 'echo hello failed: %s' % result.error
    assert 'hello' in result.stdout
    assert result.returncode == 0
    assert isinstance(result, CommandResult)


def test_e2e_run_pwd(executor):
    result = executor.run('pwd')
    assert result.ok, 'pwd failed: %s' % result.error
    assert result.stdout.strip() != ''


def test_e2e_run_with_cwd(executor):
    result = executor.run('pwd', cwd='/tmp')
    assert result.ok, 'pwd with cwd failed: %s' % result.error
    assert result.returncode == 0
