"""Index opencode sessions into LingYi memory v2.

The indexer is a thin translation layer:

  OpencodeSession -> list of MemoryEvent -> KylinAgentMemory.store()

It is deliberately stateless: an `OpencodeIndexer` instance is just a
memory handle + a few knobs (category override, importance scaling).
This makes it cheap to re-run on a session that has been partially
ingested.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .reader import OpencodeSession, OpencodeMessage, read_session_json

logger = logging.getLogger(__name__)


@dataclass
class OpencodeIndexer:
    """Stateful wrapper around a LingYi memory handle.

    `memory` is any object that exposes the v2 API:
      - `store(content, category, importance, **kwargs) -> Optional[str]`
      - `recall(query, top_k) -> List[Dict[str, Any]]`
    """

    memory: Any
    default_category: str = "interaction"
    importance_scale: float = 0.5

    def _importance_for(self, msg: OpencodeMessage) -> float:
        """Importance rises with content length and falls on stderr.

        Cap at 0.95 so the v2 differential-decay rate stays positive.
        """
        base = self.importance_scale
        if len(msg.text) > 200:
            base += 0.1
        if msg.tool_call:
            base += 0.1
        if msg.tool_stderr:
            base -= 0.2
        return max(0.05, min(0.95, base))

    def index_message(self, session_id: str, msg: OpencodeMessage) -> Optional[str]:
        """Store a single message into memory. Returns the memory id."""
        evt = msg.to_lingyi_event(session_id)
        # Pack extra fields into metadata so the v2 store API stays happy.
        metadata = {
            "tool": evt["tool"],
            "argv": evt["argv"],
            "stdout": evt["stdout"][:256],
            "stderr": evt["stderr"][:256],
            "returncode": evt["returncode"],
        }
        return self.memory.store(
            content=evt["content"],
            category=self.default_category,
            importance=self._importance_for(msg),
            session_id=session_id,
            metadata=metadata,
        )

    def index_session(self, session: OpencodeSession) -> int:
        """Index every message in a session. Returns the count stored."""
        stored = 0
        for msg in session.messages:
            mid = self.index_message(session.session_id, msg)
            if mid:
                stored += 1
        logger.info(
            "opencode_indexer: stored %d / %d messages from session %s",
            stored, len(session.messages), session.session_id,
        )
        return stored

    def index_file(self, path: str) -> int:
        """Read a session JSON file and index it in one call."""
        return self.index_session(read_session_json(path))

    def recall(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Recall memories relevant to a query."""
        return self.memory.recall(query, top_k=top_k)


def index_session(memory: Any, path: str, **kwargs: Any) -> int:
    """Functional entry point: read + index in one call."""
    return OpencodeIndexer(memory, **kwargs).index_file(path)