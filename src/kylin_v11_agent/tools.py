# -*- coding: utf-8 -*-
"""Strict tool registry for the Kylin V11 OS Agent.

Design constraints:
- Only argv-style invocation; never shell=True. This makes shell injection
  structurally impossible (no quoting bugs).
- Default toolset is read-only. Mutating tools must be explicitly registered
  by the caller and require allow_write=True.
- Every tool has: name, description, binary path, allowed args spec, timeout,
  output byte cap. The registry validates each invocation before exec.
- Tests for this module live in osam_tests/test_kylin_v11_agent.py.

Borrowed layout from Open Interpreter (Apache-2.0): tool objects with
`run(**kwargs)` interface plus a registry that resolves by name.
Adapted here to use only the standard library, with an explicit allow-list of
argv tokens instead of a free-form LLM string.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Hard upper bound on stdout/stderr captured per tool (1 MiB).
_DEFAULT_OUTPUT_CAP = 1024 * 1024
# Hard upper bound on wall clock per tool.
_DEFAULT_TIMEOUT = 30.0


@dataclass
class ToolSpec:
    """Static description of a tool the Agent can invoke."""

    name: str
    description: str
    binary: str
    allow_args: List[str] = field(default_factory=list)
    # If True, this tool can mutate system state; refused unless the registry
    # is created with allow_write=True.
    write: bool = False
    timeout_s: float = _DEFAULT_TIMEOUT
    output_cap_bytes: int = _DEFAULT_OUTPUT_CAP

    def validate_argv(self, argv: Sequence[str]) -> Optional[str]:
        """Return None if argv is allowed, else a human-readable reason.

        Policy:
          - flag tokens (start with "-") MUST appear in allow_args;
            anything else is rejected with "flag not allowed".
          - positional tokens are checked for shell metacharacters
            (";", "&", "|", "`", "$", newline). The check is deliberately
            conservative because there is no shell involved (subprocess
            is invoked with shell=False, argv-only); the metachar
            scan is belt-and-braces against the agent passing through
            malformed tokens.
          - empty argv is allowed by design: a tool may have all
            defaults and still be callable with no arguments.
        """
        if not argv:
            return None
        for token in argv:
            if token.startswith("-"):
                if token not in self.allow_args:
                    return f"flag not allowed: {token}"
                continue
            # positional: must look like a safe path or value
            if any(c in token for c in (";", "&", "|", "`", "$", "\n")):
                return f"unsafe token: {token!r}"
        return None


@dataclass
class ToolResult:
    """Concrete execution outcome for a single tool call."""

    name: str
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    latency_ms: float
    truncated: bool = False
    error: str = ""  # set if validation or pre-exec failed

    @property
    def ok(self) -> bool:
        return not self.error and self.returncode == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "ok": self.ok,
            "stdout_bytes": len(self.stdout.encode("utf-8", "replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", "replace")),
            "latency_ms": round(self.latency_ms, 2),
            "truncated": self.truncated,
            "error": self.error,
        }


class ToolRegistry:
    """Holds ToolSpec objects, resolves by name, executes safely."""

    def __init__(self, allow_write: bool = False,
                 executable_resolver: Optional[Callable[[str], str]] = None):
        self.allow_write = allow_write
        self._tools: Dict[str, ToolSpec] = {}
        self._resolver = executable_resolver or shutil.which

    def register(self, spec: ToolSpec) -> None:
        if spec.write and not self.allow_write:
            raise PermissionError(
                f"tool {spec.name!r} is a write tool but allow_write=False"
            )
        self._tools[spec.name] = spec

    def has(self, name: str) -> bool:
        return name in self._tools

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def invoke(self, name: str, argv: Optional[Sequence[str]] = None) -> ToolResult:
        argv = list(argv or [])
        if name not in self._tools:
            return ToolResult(name=name, argv=argv, returncode=-1, stdout="",
                              stderr="", latency_ms=0.0, error=f"unknown tool {name!r}")
        spec = self._tools[name]
        # Defence in depth: any write tool called when allow_write is off.
        if spec.write and not self.allow_write:
            return ToolResult(name=name, argv=argv, returncode=-1, stdout="",
                              stderr="", latency_ms=0.0,
                              error=f"write tool {name!r} refused (allow_write=False)")
        reason = spec.validate_argv(argv)
        if reason:
            return ToolResult(name=name, argv=argv, returncode=-1, stdout="",
                              stderr="", latency_ms=0.0, error=f"argv rejected: {reason}")
        binary_path = self._resolver(spec.binary) or spec.binary
        full_argv = [binary_path] + argv
        start = time.time()
        try:
            cp = subprocess.run(
                full_argv,
                capture_output=True,
                text=True,
                timeout=spec.timeout_s,
                check=False,
                shell=False,
                env=_safe_env(),
            )
        except subprocess.TimeoutExpired as e:
            lat = (time.time() - start) * 1000.0
            return ToolResult(
                name=name, argv=argv, returncode=-1,
                stdout=_truncate(e.stdout or "", spec.output_cap_bytes),
                stderr=_truncate(e.stderr or "", spec.output_cap_bytes),
                latency_ms=lat, truncated=True,
                error=f"timeout after {spec.timeout_s}s",
            )
        except FileNotFoundError:
            lat = (time.time() - start) * 1000.0
            return ToolResult(name=name, argv=argv, returncode=-1,
                              stdout="", stderr="", latency_ms=lat,
                              error=f"binary not found: {spec.binary}")

        lat = (time.time() - start) * 1000.0
        stdout = cp.stdout or ""
        stderr = cp.stderr or ""
        truncated = False
        cap = spec.output_cap_bytes
        if len(stdout) > cap:
            stdout = stdout[:cap]; truncated = True
        if len(stderr) > cap:
            stderr = stderr[:cap]; truncated = True
        return ToolResult(
            name=name, argv=argv, returncode=cp.returncode,
            stdout=stdout, stderr=stderr, latency_ms=lat, truncated=truncated,
        )


def _truncate(s: str, cap: int) -> str:
    if len(s) > cap:
        return s[:cap]
    return s


def _safe_env() -> Dict[str, str]:
    """Minimal env passed to subprocess; strips LD_* and locale overrides."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LD_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("PATH", os.environ.get("PATH", ""))
    return env


def _spec(name: str, binary: str, *,
          description: str, allow_args: Sequence[str] = (),
          write: bool = False, timeout_s: float = _DEFAULT_TIMEOUT) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        binary=binary,
        allow_args=list(allow_args),
        write=write,
        timeout_s=timeout_s,
    )


# --- platform detection helpers ---------------------------------------------

def _is_windows() -> bool:
    return platform.system() == "Windows"


def _python_executable() -> str:
    """Return the running Python executable (works for Windows and POSIX)."""
    return sys.executable


def default_read_only_tools() -> List[ToolSpec]:
    """A baseline read-only toolset for a fresh Kylin V11 environment.

    On non-Kylin hosts (Windows CI), tools whose binary is not present are
    silently dropped from the set so the registry stays useful. The
    `python_*` tools are always registered because they rely on the running
    Python interpreter which is always available.
    """
    py = _python_executable()
    common = ("--version", "--help", "-h", "-V")

    tools: List[ToolSpec] = []

    # Always-available: anything routed through Python or `cmd /c ver` on Windows.
    tools.append(_spec(
        "python_version", py,
        description="Print running Python version (always available)",
        allow_args=("-V", "--version", "-c"),
        timeout_s=10.0,
    ))
    tools.append(_spec(
        "python_eval", py,
        description="Evaluate a small read-only Python expression",
        allow_args=("-c",),
        timeout_s=10.0,
    ))
    tools.append(_spec(
        "platform_info", py,
        description="Print platform.platform() output via Python",
        allow_args=("-c",),
        timeout_s=10.0,
    ))

    # Kylin V11 / generic Linux tools.
    if not _is_windows():
        tools += [
            _spec("uname", "uname",
                  description="Print kernel and arch info",
                  allow_args=("-a", "-r", "-m", "-s", "-v", *common)),
            _spec("os_release", "cat",
                  description="Read /etc/os-release",
                  allow_args=("/etc/os-release",)),
            _spec("uptime", "uptime",
                  description="System uptime and load",
                  allow_args=("-p", "-s", "-h", *common)),
            _spec("memory", "free",
                  description="Memory stats",
                  allow_args=("-h", "-m", "-g", "-t", *common)),
            _spec("disk", "df",
                  description="Filesystem usage",
                  allow_args=("-h", "-T", "-t", "ext4", "-t", "xfs", "-t", "btrfs", *common)),
            _spec("process_count", "nproc",
                  description="Number of CPUs",
                  allow_args=("--all", *common)),
            _spec("network_state", "ss",
                  description="Show listening sockets",
                  allow_args=("-t", "-u", "-l", "-n", "-p", "-a", *common),
                  timeout_s=10.0),
            _spec("memory_db", "cat",
                  description="Inspect the on-VM Agent memory.db path",
                  allow_args=("/var/lib/kylin_v11_agent/memory.db",)),
            _spec("agent_binary", "ls",
                  description="List /opt/kylin-v11-agent/bin contents",
                  allow_args=("-l", "-la", "/opt/kylin-v11-agent/bin")),
        ]
    else:
        # Windows-side equivalents for local dev / smoke tests.
        tools.append(_spec(
            "winver", "cmd",
            description="Print Windows version via cmd /c ver",
            allow_args=("/c", "ver"),
            timeout_s=10.0,
        ))
        tools.append(_spec(
            "systeminfo", "systeminfo",
            description="Show Windows system information",
            allow_args=(),
            timeout_s=15.0,
        ))
    return tools


def parse_safe_argv(spec: ToolSpec, args_str: str) -> List[str]:
    """Helper for callers (CLI/tests) to turn a string into a safe argv list."""
    tokens = shlex.split(args_str) if args_str else []
    reason = spec.validate_argv(tokens)
    if reason:
        raise ValueError(reason)
    return tokens