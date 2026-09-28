# -*- coding: utf-8 -*-
'''Mock unit tests for SSHExecutor — no real network, all paramiko calls MagicMocked.

Covers the public surface of src.kylin_v11_agent.ssh_executor:
- SSHConfig dataclass shape
- CommandResult.ok property
- SSHExecutor.run: success, cwd injection, LC_ALL prefix, timeout propagation, error wrapping, truncation
- SSHExecutor.is_alive: callable, transport states, exception fallback
- SSHExecutor.connect: paramiko missing -> RuntimeError
- ssh_executor_from_vm_spec: default port, custom port, empty forward list fallback
'''

from __future__ import annotations
import os
import sys
import json
import socket
import tempfile
import pytest
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if '银河os' not in p]

from src.kylin_v11_agent import ssh_executor as ssh_mod
from src.kylin_v11_agent.ssh_executor import (
    SSHConfig,
    CommandResult,
    SSHExecutor,
    ssh_executor_from_vm_spec,
)


# ----- helpers -----

def _make_mock_client(stdout_bytes=b'', stderr_bytes=b'', returncode=0, raise_on_exec=None):
    client = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = stdout_bytes
    stdout.channel.recv_exit_status.return_value = returncode
    stderr = MagicMock()
    stderr.read.return_value = stderr_bytes
    if raise_on_exec is not None:
        client.exec_command.side_effect = raise_on_exec
    else:
        client.exec_command.return_value = (MagicMock(), stdout, stderr)
    return client

def _executor_with_client(client):
    ex = SSHExecutor(SSHConfig())
    ex._client = client
    return ex


# ----- TestCommandResultOk -----

class TestCommandResultOk:
    def test_ok_when_rc_zero_and_no_error(self):
        r = CommandResult('out', '', 0, 10.0)
        assert r.ok is True
    def test_not_ok_when_nonzero_rc(self):
        r = CommandResult('out', 'err', 1, 10.0)
        assert r.ok is False
    def test_not_ok_when_error_set(self):
        r = CommandResult('', '', -1, 5.0, error='boom')
        assert r.ok is False
    def test_not_ok_when_error_and_nonzero(self):
        r = CommandResult('a', 'b', 2, 1.0, truncated=True, error='x')
        assert r.ok is False


# ----- TestRunSuccess -----

class TestRunSuccess:
    def test_normal_command_returns_ok_result(self):
        client = _make_mock_client(stdout_bytes=b'hello', returncode=0)
        ex = _executor_with_client(client)
        r = ex.run('echo hello')
        assert r.stdout == 'hello'
        assert r.stderr == ''
        assert r.returncode == 0
        assert r.ok is True
        assert r.error == ''
        assert r.latency_ms >= 0.0
        # LC_ALL prefix + get_pty=False
        client.exec_command.assert_called_once()
        args, kwargs = client.exec_command.call_args
        assert 'LC_ALL=C.UTF-8' in args[0]
        assert 'echo hello' in args[0]
        assert kwargs.get('get_pty') is False

class TestRunWithCwd:
    def test_cwd_is_injected_into_command(self):
        client = _make_mock_client(stdout_bytes=b'ok', returncode=0)
        ex = _executor_with_client(client)
        r = ex.run('ls', cwd='/tmp')
        assert r.ok is True
        args, _ = client.exec_command.call_args
        cmd = args[0]
        assert 'cd ' in cmd
        assert "'/tmp'" in cmd
        assert 'ls' in cmd

    def test_no_cwd_no_cd_prefix(self):
        client = _make_mock_client(stdout_bytes=b'ok', returncode=0)
        ex = _executor_with_client(client)
        ex.run('ls')
        args, _ = client.exec_command.call_args
        cmd = args[0]
        assert 'cd ' not in cmd
        assert 'ls' in cmd


class TestRunWithLcAllPrefix:
    def test_lc_all_lang_prefix_present(self):
        client = _make_mock_client(stdout_bytes=b'out')
        ex = _executor_with_client(client)
        ex.run('whoami')
        args, _ = client.exec_command.call_args
        cmd = args[0]
        assert 'LC_ALL=C.UTF-8' in cmd
        assert 'LANG=C.UTF-8' in cmd
        assert cmd.startswith('export LC_ALL=C.UTF-8')

    def test_default_timeout_is_command_timeout(self):
        client = _make_mock_client(stdout_bytes=b'out')
        ex = _executor_with_client(client)
        ex.run('whoami')
        _, kwargs = client.exec_command.call_args
        assert kwargs.get('timeout') == 30.0

    def test_explicit_timeout_overrides_default(self):
        client = _make_mock_client(stdout_bytes=b'out')
        ex = _executor_with_client(client)
        ex.run('whoami', timeout=5.0)
        _, kwargs = client.exec_command.call_args
        assert kwargs.get('timeout') == 5.0


class TestRunTimeout:
    def test_socket_timeout_returns_error_with_rc_minus_one(self):
        client = MagicMock()
        client.exec_command.side_effect = socket.timeout()
        ex = _executor_with_client(client)
        r = ex.run('sleep 60', timeout=30)
        assert r.returncode == -1
        assert r.stdout == ''
        assert r.stderr == ''
        assert r.ok is False
        assert 'timeout after' in r.error
        assert '30' in r.error


class TestRunSocketError:
    def test_generic_exception_wrapped_in_ssh_error(self):
        client = MagicMock()
        client.exec_command.side_effect = Exception('connection reset')
        ex = _executor_with_client(client)
        r = ex.run('ls')
        assert r.returncode == -1
        assert r.error == 'SSH error: connection reset'
        assert r.ok is False



class TestTruncatedOutput:
    def test_stdout_over_1mb_is_truncated(self):
        big = b'X' * (1024 * 1024 + 100)
        client = _make_mock_client(stdout_bytes=big, returncode=0)
        ex = _executor_with_client(client)
        r = ex.run('cat huge')
        assert r.truncated is True
        assert len(r.stdout) == 1024 * 1024
        assert r.returncode == 0

    def test_stderr_over_1mb_truncates_both_streams(self):
        # SYMMETRIC: if either side > 1MB, BOTH are sliced to [:1MB]
        big_err = b'E' * (1024 * 1024 + 200)
        small_out = b'OK'
        client = _make_mock_client(
            stdout_bytes=small_out, stderr_bytes=big_err, returncode=0
        )
        ex = _executor_with_client(client)
        r = ex.run('ls')
        assert r.truncated is True
        assert r.stdout == 'OK'
        assert len(r.stderr) == 1024 * 1024


class TestDecodeReplaceMode:
    def test_invalid_utf8_does_not_raise(self):
        bad = b'\xff\xfe\xfd hello'
        out = SSHExecutor._decode(bad)
        assert out
        assert 'hello' in out

    def test_empty_bytes_decode_to_empty_string(self):
        assert SSHExecutor._decode(b'') == ''

    def test_ascii_round_trip(self):
        msg = 'alpha beta gamma'
        assert SSHExecutor._decode(msg.encode('utf-8')) == msg

class TestIsAliveMethod:
    def test_is_alive_is_callable(self):
        import inspect
        ex = SSHExecutor(SSHConfig())
        assert callable(ex.is_alive)
        assert inspect.isroutine(ex.is_alive)

    def test_no_client_returns_false(self):
        ex = SSHExecutor(SSHConfig())
        ex._client = None
        assert ex.is_alive() is False

    def test_active_transport_returns_true(self):
        client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = True
        client.get_transport.return_value = transport
        ex = SSHExecutor(SSHConfig())
        ex._client = client
        assert ex.is_alive() is True

    def test_inactive_transport_returns_false(self):
        client = MagicMock()
        transport = MagicMock()
        transport.is_active.return_value = False
        client.get_transport.return_value = transport
        ex = SSHExecutor(SSHConfig())
        ex._client = client
        assert ex.is_alive() is False

    def test_exception_in_get_transport_returns_false(self):
        client = MagicMock()
        client.get_transport.side_effect = Exception('boom')
        ex = SSHExecutor(SSHConfig())
        ex._client = client
        assert ex.is_alive() is False

    def test_transport_is_none_returns_false(self):
        client = MagicMock()
        client.get_transport.return_value = None
        ex = SSHExecutor(SSHConfig())
        ex._client = client
        assert ex.is_alive() is False

class TestConnectParamikoMissing:
    def test_runtime_error_when_paramiko_missing(self):
        ex = SSHExecutor(SSHConfig())
        ex._client = None
        with patch.object(ssh_mod, '_HAS_PARAMIKO', False):
            with pytest.raises(RuntimeError) as excinfo:
                ex.connect()
        assert 'paramiko not installed' in str(excinfo.value)


class TestSshExecutorFromVmSpec:
    def test_custom_port_from_forward(self, tmp_path):
        spec = {'network': {'forward': [{'host_port': 2223, 'guest_port': 22}]}}
        spec_file = tmp_path / 'vm.json'
        spec_file.write_text(json.dumps(spec), encoding='utf-8')
        ex = ssh_executor_from_vm_spec(str(spec_file))
        assert isinstance(ex, SSHExecutor)
        assert ex.config.port == 2223
        assert ex.config.host == 'localhost'

    def test_default_port_when_no_network_key(self, tmp_path):
        spec = {'other': 'value'}
        spec_file = tmp_path / 'vm.json'
        spec_file.write_text(json.dumps(spec), encoding='utf-8')
        ex = ssh_executor_from_vm_spec(str(spec_file))
        assert ex.config.port == 2222

    def test_default_port_when_forward_entry_has_no_host_port(self, tmp_path):
        spec = {'network': {'forward': [{}]}}
        spec_file = tmp_path / 'vm.json'
        spec_file.write_text(json.dumps(spec), encoding='utf-8')
        ex = ssh_executor_from_vm_spec(str(spec_file))
        assert ex.config.port == 2222
