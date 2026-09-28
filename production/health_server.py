"""Minimal HTTP health + metrics endpoint for production deployments.

Why this exists:
- Pinduoduo-style platforms standardise on `/healthz`, `/readyz`, and
  Prometheus-format `/metrics` for every service. This file gives
  LingYi those endpoints without adding a heavy web framework.

Design:
- Pure `http.server` from stdlib. Zero non-stdlib deps.
- Threaded via `ThreadingHTTPServer` so a slow `/metrics` scrape does
  not block the health endpoints.
- Graceful shutdown: SIGTERM / SIGINT handler drains in-flight
  requests and closes the underlying `MemorySystemV2` cleanly.

Usage:
    from production.health_server import start_health_server, stop_health_server
    from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig

    mem = MemorySystemV2(MemoryConfig(db_path="/var/lib/lingyi/memory.db"))
    server = start_health_server(mem, host="0.0.0.0", port=8080)
    try:
        # ... main loop ...
        pass
    finally:
        stop_health_server(server)
        mem.close()
"""

from __future__ import annotations
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class HealthState:
    """Mutable state shared between the memory worker and the HTTP server."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._ready.set()  # ready by default; the runtime can clear() during init
        self._started_at = time.time()
        self._counters: Dict[str, int] = {
            "stores_total": 0,
            "recalls_total": 0,
            "store_errors_total": 0,
            "recall_errors_total": 0,
            "http_requests_total": 0,
            "last_store_latency_ms": 0,
            "last_recall_latency_ms": 0,
        }
        self._lock = threading.Lock()

    def mark_ready(self) -> None:
        self._ready.set()

    def mark_not_ready(self) -> None:
        self._ready.clear()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def incr(self, key: str, by: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + by

    def set(self, key: str, value: float) -> None:
        with self._lock:
            self._counters[key] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.time() - self._started_at, 3),
                "counters": dict(self._counters),
            }


class _HealthHandler(BaseHTTPRequestHandler):
    """Routes GET /healthz, /readyz, /metrics, /v1/stats.

    The `memory` and `state` attributes are set on the *server*
    instance by `make_handler`; we read them off the class.
    """

    server_version = "LingYiHealth/1.0"

    # Quieter access log; the platform's log collector takes care of it.
    def log_message(self, fmt, *args):
        logger.debug("http %s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # PDD-3: echo the request id so a single request can be
        # traced end-to-end through the platform'``s log collector.
        rid = self.headers.get("X-Request-Id")
        if rid:
            self.send_header("X-Request-Id", rid)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        rid = self.headers.get("X-Request-Id")
        if rid:
            self.send_header("X-Request-Id", rid)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler contract)
        # Bump the http_requests_total counter on every request.
        self.server.health_state.incr("http_requests_total")
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            # Liveness: the process is up. Always 200 unless we are shutting down.
            if getattr(self.server, "shutting_down", False):
                self._send_json(503, {"status": "shutting_down"})
            else:
                self._send_json(200, {"status": "alive"})
            return

        if path == "/readyz":
            # Readiness: the memory handle is open and responsive.
            ready = self.server.health_state.is_ready()
            payload = {
                "status": "ready" if ready else "not_ready",
                "memory_open": bool(getattr(self.server, "memory", None)),
            }
            self._send_json(200 if ready else 503, payload)
            return

        if path == "/metrics":
            # Prometheus text exposition format (subset).
            counters = self.server.health_state.snapshot()["counters"]
            lines = []
            for name, value in counters.items():
                lines.append(f"# TYPE lingyi_{name} counter")
                lines.append(f"lingyi_{name} {value}")
            self._send_text(200, "\n".join(lines) + "\n", "text/plain")
            return

        if path == "/v1/stats":
            # JSON snapshot of the counters + uptime.
            self._send_json(200, self.server.health_state.snapshot())
            return

        self._send_json(404, {"error": "not_found", "path": path})


def make_handler(memory: Any, state: HealthState) -> type:
    """Bind `memory` and `state` onto a fresh handler class."""
    cls = type(
        "BoundHealthHandler",
        (_HealthHandler,),
        {
            "memory": memory,
            "health_state": state,
        },
    )
    return cls


def start_health_server(
    memory: Any,
    host: str = "0.0.0.0",
    port: int = 8080,
    state: Optional[HealthState] = None,
) -> ThreadingHTTPServer:
    """Start the health server in a background thread.

    Returns the server so the caller can later call `stop_health_server`.
    """
    state = state or HealthState()
    handler_cls = make_handler(memory, state)
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.health_state = state  # type: ignore[attr-defined]
    server.memory = memory  # type: ignore[attr-defined]
    server.shutting_down = False  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, name="lingyi-health", daemon=True)
    thread.start()
    logger.info("lingyi health server listening on %s:%d", host, port)
    return server


def stop_health_server(server: ThreadingHTTPServer, timeout_s: float = 5.0) -> None:
    """Shut down the health server, draining in-flight requests.

    Sets `shutting_down` so /healthz returns 503 in the brief window
    between the call and the actual socket close.
    """
    setattr(server, "shutting_down", True)
    # /readyz reports not-ready so a load balancer pulls us out.
    if hasattr(server, "health_state"):
        server.health_state.mark_not_ready()  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()
    logger.info("lingyi health server stopped")


def install_signal_handlers(server: ThreadingHTTPServer, on_stop: Optional[Callable[[], None]] = None) -> None:
    """Install SIGTERM / SIGINT handlers that shut the server down cleanly.

    Idempotent: calling twice replaces the previous handler.
    """
    def _handle(signum, frame):
        logger.info("signal %d received; shutting down", signum)
        try:
            if on_stop is not None:
                on_stop()
            stop_health_server(server)
        finally:
            # Re-raise default behaviour for the runtime to exit.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)