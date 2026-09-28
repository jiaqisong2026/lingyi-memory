# Architecture

## 1. Layered view

```
                  +---------------------------+
                  |        Agent (agent.py)   |
                  |  plan -> execute -> reflect|
                  +-------------+-------------+
                                |
            +-------------------+---------------------+
            |                                         |
   +--------v--------+                       +---------v--------+
   |   ToolRegistry  |                       |   KylinAgentMemory|
   |  argv-only exec |                       |   (v1 bridge)     |
   |  no shell=True  |                       +---+----------+----+
   +--------+--------+                          |          |
            |                                   |          |
            | real exec                        |          | optional
   +--------v--------+                          |          v
   |   SSHExecutor   |  <-- only if remote    |   MemorySystemV2
   |  (paramiko)     |                         |   (three-layer)
   +-----------------+                         +---+----------+
                                                |          |
                                       +--------+--+   +----+----+
                                       | Knowledge |   | Active |
                                       |  Graph    |   | Learner|
                                       +-----------+   +--------+
```

Two execution paths are supported:

- **local**: `ToolRegistry.run()` → `subprocess.run(argv, ...)`
- **remote**: `SSHExecutor.run()` → `paramiko.exec_command()`

The Agent does not care which one is in use; both return a uniform
`CommandResult` shape with `stdout`, `stderr`, `returncode`, `latency_ms`,
`ok`, and `error` fields.

## 2. Memory v2 — three-layer orthogonal architecture

```
   L0  Perception         MemoryEvent (structured tool-call extraction)
            |
            v
   L1  Memory              Four-quadrant (episodic/semantic/procedural/preference)
            |              Five temporal levels (working/short/mid/long/procedural)
            |              SQLite + FTS5 + BM25 fallback
            v
   L2  Cognition           KnowledgeGraph (entity + edge)
                           ActiveLearner (gap observation + filling)
```

### 2.1 L0 Perception

Every tool invocation produces a `MemoryEvent` with a fixed schema:

```
event_type, subject, verb, object, context, outcome,
confidence, timestamp, session_id, user_id,
tool_name, argv, returncode, latency_ms, error
```

The agent's reflect loop converts raw `(tool, argv, returncode, stdout,
error)` tuples into `MemoryEvent`s before they reach the storage layer.
This isolates the cognitive core from the I/O surface.

### 2.2 L1 Storage

- SQLite (single file, WAL mode, `busy_timeout=5000 ms`).
- FTS5 virtual table for keyword search; BM25-compatible scorer
  (`bm25_compat.py`) is the fallback when FTS5 is not compiled in.
- Per-row differential decay rate (`decay_rate`) drives the L1 -> evict
  sweep; the rate is a function of category and importance, computed in
  `decay.py`.
- A periodic auto-consolidation pass (every `consolidation_interval`
  stores, default 20) folds frequently-recalled episodic memories into
  the semantic tier.

### 2.3 L2 Cognition

- **KnowledgeGraph**: extract entity pairs from stored memories and
  attach typed edges (`related_to`, `modified`, `depends_on`, ...). Lineage
  queries answer "where did this fact come from".
- **ActiveLearner**: every recall miss becomes a "gap" record; the gap is
  promoted to a hypothesis once a future store proves related, or pruned
  if it sits unfilled past the retention window.

## 3. Hybrid retrieval (RRF)

Two scorers run side by side:

```
   FTS5 BM25 scorer   ──┐
                        ├── RRF fusion (k=60, Cormack et al.)
   exact keyword SQL  ──┘
```

Reciprocal Rank Fusion is parameter-free, has no learnable weights, and
beats either scorer alone on the in-suite recall benchmarks. The fusion
implementation is in `memory_v2/retrieval.py`; the per-row scoring is
parallel-safe (`SELECT ... ORDER BY score DESC LIMIT k`).

## 4. SSH executor — failure surface

`SSHExecutor.run()` enumerates every failure mode and wraps it as a
`CommandResult`:

| Failure | Returned shape |
|---|---|
| Network unreachable | `returncode=-1, error="SSH error: ..."` |
| Auth rejected | `returncode=-1, error="SSH auth failed for: ..."` |
| Command timeout | `returncode=-1, error="timeout after Ns"` |
| Paramiko not installed | `RuntimeError` on `connect()` |
| Output larger than 1 MiB | `truncated=True`, content clipped |
| Non-UTF-8 output | decoded as `utf-8 → latin-1` with `replace` |

Every `run()` also sets a 30 s default timeout (`SSHConfig.command_timeout`)
that is per-command, not per-session. Long-running commands opt into
`timeout=` overrides.

## 5. Tool registry — argv-only by construction

`ToolRegistry` accepts `ToolSpec(name, binary, allow_args, write, timeout,
output_cap)`. Before every invocation, the registry validates each argv
token against the allow-list:

- argv tokens starting with `-` must appear in `ToolSpec.allow_args`,
- argv tokens starting without `-` are positional and always allowed,
- if any check fails, the call is refused before subprocess spawn.

There is no `shell=True`, no string interpolation, no quoting bugs. A
malicious or confused agent cannot escape the argv vector.


## 6. opencode integration (R3)

opencode sessions are ingested via a thin adapter that maps the
opencode `Info` / `parts` JSON shape onto LingYi memory events.

```
   opencode session.json
          |
          v
   OpencodeSessionReader.read_session_json()
          |   (permissive JSON, drops unknown fields)
          v
   list[OpencodeMessage]   (role, text, tool_call, stdout, stderr)
          |
          v
   OpencodeIndexer.index_session()
          |   (importance = f(content, tool_call, stderr))
          v
   MemorySystemV2.store(content, category, importance, session_id)
          |
          +--> recall(query) ---> [MemoryEvent rows]
```

The adapter (`src/integrations/opencode_adapter/`) is a stateless
translator. The reader is intentionally permissive (unknown fields
ignored) so that a future opencode schema change does not break
ingest. The indexer takes a category override and an importance
scale so a caller can bias the storage policy.

The opencode reference code under `opencode_reference/` is a
curated MIT-attributed subset (7 files, ~100 KB) so an
architectural reviewer can compare LingYi's choices against
opencode's side by side without leaving the submission.
See `opencode_reference/ATTRIBUTION.md` for provenance and
`docs/INTEGRATION_OPENCODE.md` for the integration narrative.
