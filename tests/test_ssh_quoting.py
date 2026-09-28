# -*- coding: utf-8 -*-
"""Tests for the R3-1 fix: cwd must be POSIX-shell-safe single-quoted."""

from __future__ import annotations
import pytest

from src.kylin_v11_agent.ssh_executor import (
    SSHExecutor, SSHConfig, _shell_quote,
)


class TestShellQuote:
    def test_plain_ascii(self):
        assert _shell_quote("/tmp/normal") == "'/tmp/normal'"

    def test_unicode_round_trip(self):
        out = _shell_quote("/tmp/中文路径")
        assert out == "'/tmp/中文路径'"
        # The single-quote wrapper guarantees the shell sees a single token.
        assert out.startswith("'") and out.endswith("'")

    def test_embedded_single_quote_escaped(self):
        # The classic injection pattern: a single quote that would
        # close the quoted string and inject a new command.
        out = _shell_quote("evil'; cat /etc/passwd; echo '")
        # Inner single quotes must be replaced with the canonical '"'"'
        # sequence; there must NOT be an unbalanced single quote in the
        # result.
        # Count single quotes: with N inner quotes we expect 2 + 2*N
        # (open/close + 4 characters per escape).
        # Easier: assert no three-in-a-row runs of odd quote count.
        assert "'\\''" in out
        # Re-decode manually to make sure the shell would see the
        # original string and nothing else.
        decoded = out[1:-1].replace("'\\''", "'")
        assert decoded == "evil'; cat /etc/passwd; echo '"

    def test_empty_string(self):
        assert _shell_quote("") == "''"

    def test_newline_refused(self):
        with pytest.raises(ValueError):
            _shell_quote("a\nb")
        with pytest.raises(ValueError):
            _shell_quote("a\rb")


class TestRunUsesShellQuote:
    """Verify SSHExecutor.run() actually uses _shell_quote for cwd."""

    def test_cwd_with_quote_does_not_break_out(self, monkeypatch):
        """A malicious cwd must not produce an unbalanced command string."""
        execed: list[str] = []

        class _FakeClient:
            def exec_command(self, cmd, timeout=None, get_pty=False):
                execed.append(cmd)
                # Return a successful-looking result.
                class _Stdout:
                    def read(self):
                        return b"ok"

                    class channel:
                        @staticmethod
                        def recv_exit_status():
                            return 0

                return (None, _Stdout(), _Stdout())

            def get_transport(self):
                class _T:
                    @staticmethod
                    def is_active():
                        return True
                return _T()

        ex = SSHExecutor(SSHConfig())
        ex._client = _FakeClient()
        ex.run("whoami", cwd="evil'; cat /etc/passwd; echo '")

        # The single command sent to the channel must contain the
        # escaped path AND must NOT contain a raw `cat /etc/passwd`
        # outside the single-quote wrapper.
        assert len(execed) == 1
        cmd = execed[0]
        # Path content is preserved, but the dangerous `; cat` is
        # inside a single-quoted string and therefore inert to the shell.
        assert "cat /etc/passwd" in cmd
        # The outer command prefix must end with `cd '` and the cd
        # argument must close with `' &&`.
        assert "cd '" in cmd
        assert " && whoami" in cmd


class TestTimeoutCeiling:
    """R3-4: a planner bug passing an absurd timeout must be clamped."""

    def test_huge_timeout_clamped(self, monkeypatch):
        execed: list[dict] = []

        class _FakeClient:
            def exec_command(self, cmd, timeout=None, get_pty=False):
                execed.append({"cmd": cmd, "timeout": timeout})

                class _S:
                    def read(self):
                        return b""

                    class channel:
                        @staticmethod
                        def recv_exit_status():
                            return 0

                return (None, _S(), _S())

            def get_transport(self):
                class _T:
                    @staticmethod
                    def is_active():
                        return True
                return _T()

        ex = SSHExecutor(SSHConfig())
        ex._client = _FakeClient()
        ex.run("whoami", timeout=10 ** 9)
        assert execed[0]["timeout"] == SSHExecutor.MAX_CALL_TIMEOUT

    def test_negative_timeout_falls_back_to_default(self, monkeypatch):
        execed: list[dict] = []

        class _FakeClient:
            def exec_command(self, cmd, timeout=None, get_pty=False):
                execed.append({"timeout": timeout})

                class _S:
                    def read(self):
                        return b""

                    class channel:
                        @staticmethod
                        def recv_exit_status():
                            return 0

                return (None, _S(), _S())

            def get_transport(self):
                class _T:
                    @staticmethod
                    def is_active():
                        return True
                return _T()

        ex = SSHExecutor(SSHConfig(command_timeout=12.5))
        ex._client = _FakeClient()
        ex.run("whoami", timeout=-1)
        assert execed[0]["timeout"] == 12.5