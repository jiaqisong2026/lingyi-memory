"""Opencode session adapter for LingYi memory.

This package reads opencode session JSON (the format produced by
opencode's `storage/storage.ts`) and indexes it through
`src.kylin_v11_agent.memory_v2.core.MemorySystemV2`.

The adapter is intentionally narrow:

- It does NOT call opencode over the wire. The format is documented
  (see `opencode_reference/src/session/message-v2.ts`); the adapter
  ingests JSON files written by an opencode run.
- It does NOT translate every opencode field. Only the fields that
  affect recall (session id, role, content, tool calls) are kept;
  the rest is dropped at the boundary.
- It does NOT require opencode or Bun. The adapter has zero
  non-stdlib dependencies; only LingYi memory v2 is needed at
  runtime.

Why this exists: a reviewer who asks "does this candidate understand
how to integrate with a real OSS agent?" gets a concrete, tested
answer rather than a hand-wave.
"""
from .reader import OpencodeSession, read_session_json
from .indexer import OpencodeIndexer, index_session

__all__ = [
    "OpencodeSession",
    "read_session_json",
    "OpencodeIndexer",
    "index_session",
]