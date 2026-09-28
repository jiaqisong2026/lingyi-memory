"""Long-running runtime that wires memory + health server + signals.

Run as the container entrypoint:

    LINGYI_DB_PATH=/var/lib/lingyi/memory.db \
    LINGYI_HEALTH_PORT=8080 \
    python -m production.runtime

The runtime keeps the process alive until SIGTERM / SIGINT, then
drains the health server and closes the memory handle cleanly.
"""

from __future__ import annotations
import logging
import os
import signal
import sys
import time
from typing import Optional

from production.health_server import (
    HealthState, start_health_server, stop_health_server, install_signal_handlers,
)
from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig

logger = logging.getLogger("lingyi.runtime")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LINGYI_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    db_path = os.environ.get("LINGYI_DB_PATH", "/var/lib/lingyi/data/memory.db")
    host = os.environ.get("LINGYI_HEALTH_HOST", "0.0.0.0")
    port = int(os.environ.get("LINGYI_HEALTH_PORT", "8080"))
    enable_wal = os.environ.get("LINGYI_ENABLE_WAL", "1") not in ("0", "false", "False")

    logger.info("opening memory at %s (enable_wal=%s)", db_path, enable_wal)
    mem = MemorySystemV2(MemoryConfig(db_path=db_path, enable_wal=enable_wal))
    # R5 / OPS-4: ``mem`` is owned by this process and is NOT thread-safe
    # to share across threads. SQLite + Python's default connection has a
    # per-thread binding; using a single connection from multiple threads
    # will produce 'database is locked' errors under load. The supported
    # patterns are: (a) per-thread connections, or (b) a write queue that
    # serialises every mutation through one connection. See
    # tests/test_concurrency.py for the proven queue-serialised writer
    # pattern and docs/SYSTEM_DESIGN_PDD.md for the production guidance.
    state = HealthState()
    state.mark_ready()

    server = start_health_server(mem, host=host, port=port, state=state)
    install_signal_handlers(
        server,
        on_stop=lambda: mem.close(),
    )

    # Idle loop. Real production code would have a job queue / RPC
    # loop here; we keep it empty so the runtime is a stable target.
    try:
        while True:
            time.sleep(60.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt; shutting down")
    finally:
        stop_health_server(server)
        mem.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())