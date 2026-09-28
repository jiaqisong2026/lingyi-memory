# -*- coding: utf-8 -*-
from __future__ import annotations
import logging, socket, time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)
try:
    import paramiko; _HAS_PARAMIKO = True
except ImportError:
    _HAS_PARAMIKO = False; paramiko = None


@dataclass
class SSHConfig:
    host: str = "localhost"
    port: int = 2222
    username: str = "kylin"
    password: str = ""
    key_filename: Optional[str] = None
    timeout: float = 10.0
    command_timeout: float = 30.0
    # R3-N3: configurable ceiling on per-call timeout override.
    # Operators can raise this without editing source.
    max_call_timeout_s: float = 600.0


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int
    latency_ms: float
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.returncode == 0


class SSHExecutor:
    OUTPUT_CAP = 1024 * 1024

    # R3-4: backward-compat class constant. New code should read
    # self.config.max_call_timeout_s (per-instance override).
    MAX_CALL_TIMEOUT = 600.0

    def __init__(self, config: SSHConfig):
        self.config = config
        self._client = None
        self._last_used = 0.0

    def connect(self) -> None:
        if self._client is not None:
            return
        if not _HAS_PARAMIKO:
            raise RuntimeError("paramiko not installed")
        cfg = self.config
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw: Dict = {
            "hostname": cfg.host,
            "port": cfg.port,
            "username": cfg.username,
            "timeout": cfg.timeout,
            "banner_timeout": cfg.timeout,
            "auth_timeout": cfg.timeout,
        }
        if cfg.key_filename:
            kw["key_filename"] = cfg.key_filename
        elif cfg.password:
            kw["password"] = cfg.password
        try:
            client.connect(**kw)
        except socket.timeout:
            raise RuntimeError("SSH timeout to: " + cfg.host + ":" + str(cfg.port))
        except paramiko.AuthenticationException:
            raise RuntimeError("SSH auth failed for: " + cfg.username)
        except Exception as e:
            raise RuntimeError("SSH error: " + str(e))
        self._client = client
        self._last_used = time.time()
        logger.info("SSH connected to %s:%d", cfg.host, cfg.port)

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception as e:
                logger.warning("close error: %s", e)
            self._client = None

    def is_alive(self) -> bool:
        if self._client is None:
            return False
        try:
            t = self._client.get_transport()
            return t is not None and t.is_active()
        except Exception:
            return False

    def run(self, command, timeout=None, cwd=None) -> CommandResult:
        # R3-4: clamp per-call timeout. Anything above MAX_CALL_TIMEOUT
        # is clamped; anything that is not a number falls back to the
        # configured default.
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                timeout = self.config.command_timeout
            if timeout < 0:
                timeout = self.config.command_timeout
            timeout = min(timeout, self.config.max_call_timeout_s)
        timeout = timeout or self.config.command_timeout
        start = time.time()
        if self._client is None:
            self.connect()
        # R3-1: shell-safe single-quote escaping. The previous code used
        # repr() which happens to escape ASCII single quotes but is not
        # a guarantee for arbitrary unicode / non-utf8 paths. _shell_quote
        # is a deliberate POSIX-shelling helper.
        prefix = "export LC_ALL=C.UTF-8 LANG=C.UTF-8; "
        if cwd:
            prefix += "cd " + _shell_quote(cwd) + " && "
        full_cmd = prefix + command
        try:
            _s, stdout, stderr = self._client.exec_command(
                full_cmd, timeout=timeout, get_pty=False,
            )
            so = stdout.read()
            se = stderr.read()
            rc = stdout.channel.recv_exit_status()
        except socket.timeout:
            err = "timeout after " + str(timeout) + "s"
            return CommandResult("", "", -1, (time.time() - start) * 1000, error=err)
        except Exception as e:
            err = "SSH error: " + str(e)
            return CommandResult("", "", -1, (time.time() - start) * 1000, error=err)
        lat = (time.time() - start) * 1000
        so_t = self._decode(so)
        se_t = self._decode(se)
        tr = len(so_t) > self.OUTPUT_CAP or len(se_t) > self.OUTPUT_CAP
        return CommandResult(
            so_t[: self.OUTPUT_CAP], se_t[: self.OUTPUT_CAP], rc, lat, tr,
        )

    @staticmethod
    def _decode(data):
        try:
            return data.decode("utf-8", "replace")
        except Exception:
            return data.decode("latin-1", "replace")


def _shell_quote(s: str) -> str:
    """POSIX shell single-quote escaping.

    Wraps the input in single quotes and escapes any inner single quote
    using the canonical `'"'"'` sequence. Refuses embedded newlines
    because a newline would split the command into multiple lines.
    """
    if not isinstance(s, str):
        s = str(s)
    if "\n" in s or "\r" in s:
        raise ValueError("path contains newline: " + repr(s))
    return "'" + s.replace("'", "'\\''") + "'"


def ssh_executor_from_vm_spec(path):
    import json
    from pathlib import Path
    s = json.loads(Path(path).read_text(encoding="utf-8"))
    p = s.get("network", {}).get("forward", [{}])[0].get("host_port", 2222)
    return SSHExecutor(SSHConfig(host="localhost", port=p, username="kylin"))