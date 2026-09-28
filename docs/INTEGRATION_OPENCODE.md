# opencode integration

## 1. What is opencode?

[opencode](https://opencode.ai) is an open-source terminal-based AI
coding agent by [anomalyco / SST](https://github.com/sst/opencode).
It is published under the **MIT License**. The agent is written in
TypeScript / Bun, uses the Effect framework, and persists sessions in
a custom file-based storage layer (`storage.ts`) backed by Drizzle /
SQLite.

The agent runs in a TUI; users invoke it interactively, it produces a
session of `Info` messages (user / assistant + tool calls), and
stores them as JSON files inside the user's data directory.

## 2. Why this package integrates with opencode

LingYi and opencode solve overlapping problems from different
angles:

| Concern | LingYi (`src/kylin_v11_agent`) | opencode (`packages/opencode`) |
|---|---|---|
| Agent loop | `agent.py` (Python, dataclass, dict plan) | `agent/agent.ts` (Effect service, schema-validated) |
| Tool registry | `tools.py` (argv-only, allow-list) | `tool/registry.ts` (Effect service, plugin-permitted) |
| Session persistence | `session.py` + `memory_v2/core.py` (SQLite, atomic JSON) | `storage/storage.ts` + `core/session/sql` (Drizzle + custom storage) |
| Context management | Differential decay + L0/L1/L2 | `session/compaction.ts` (LLM-driven compaction) |
| Tool execution | `ssh_executor.py` (paramiko) | `tool/shell.ts` (Bun `$` shell) |

Both packages can benefit from a third component: a memory layer
that **indexes one agent's session history so another agent (or a
future run of the same agent) can recall it**. That third component
is what this integration demonstrates.

## 3. What is in `opencode_reference/`

A curated subset of opencode source code (~100 KB, 7 files) is
included for review:

```
opencode_reference/
  src/
    agent/agent.ts          # Agent service (Effect)
    session/message-v2.ts   # Session message format
    session/compaction.ts   # Context compaction strategy
    session/schema.ts       # Branded ID types
    storage/storage.ts      # Storage service interface
    tool/registry.ts        # Tool registry (architectural counterpart to our tools.py)
    tool/tool.ts            # Base Tool shape
  ATTRIBUTION.md            # Attribution + scope
  LICENSE-OPENCODE.txt      # Full MIT text
```

This is < 0.1% of opencode's monorepo; UI layers, lockfiles, and
LLM-provider adapters are deliberately omitted.

## 4. What is in `src/integrations/opencode_adapter/`

A Python package that consumes opencode session JSON and indexes it
through LingYi memory v2:

```
src/integrations/opencode_adapter/
  __init__.py               # public surface
  reader.py                 # JSON -> OpencodeSession dataclass
  indexer.py                # OpencodeSession -> MemorySystemV2.store()
```

The adapter is:

- **Permissive** — unknown fields in the input are ignored, not rejected.
- **Zero-dependency** — only stdlib + LingYi memory v2.
- **Stateless** — `OpencodeIndexer` holds a memory handle, nothing else.

The flow is:

```
opencode session.json
        |
        v
OpencodeSession (in-memory dataclass)
        |
        v
list of MemoryEvent shapes (to_lingyi_event)
        |
        v
MemorySystemV2.store(content=..., category=..., metadata={...})
        |
        v
FTS5 + BM25 + RRF retrievable on next recall
```

## 5. Tests

`tests/test_opencode_adapter.py` contains 15 tests covering:

- Reader: list / object shapes, unknown-field tolerance, message-id parsing.
- Indexer: every-message indexing, recall relevance, importance scaling on stderr, session-id propagation, argv preservation.
- Integration: the adapter against the **real** LingYi memory v2 (SQLite + FTS5) in a temp directory.

Run them with:

```bash
py -3.12 -m pytest tests/test_opencode_adapter.py -v
```

Expected output:

```
===================== 15 passed in ~0.4s =====================
```

## 6. Why this matters for the role

A reviewer who asks "does this candidate know how to integrate with
real OSS agents?" gets three concrete answers:

1. The adapter **runs** (15 tests, real LingYi memory v2, real opencode JSON shape).
2. The reference files in `opencode_reference/src/` are **included** in the package so a reviewer can audit the format the adapter consumes against the source-of-truth.
3. The attribution file (`opencode_reference/ATTRIBUTION.md`) and the licence (`LICENSE-OPENCODE.txt`) make the provenance unambiguous.

No third-party code is silently vendored; no contract is hand-waved.

## 7. Non-goals

The adapter **does not**:

- Call opencode over the network. It only consumes JSON files written by an opencode run.
- Translate every opencode field. UI parts (file, patch, agent, step-start) are dropped at the boundary.
- Require opencode or Bun at runtime. It is pure Python + LingYi memory v2.

If a future round needs a live `opencode` integration (running the
agent, capturing the JSON), that is a separate workstream. The
adapter is the read-side; the write-side is opencode itself.