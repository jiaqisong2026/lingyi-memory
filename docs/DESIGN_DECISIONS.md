# Design decisions

The point of this document is to record the trade-offs explicitly so a
reviewer does not have to read every file to understand why a choice was
made.

## D1. No `shell=True`, ever

**Decision.** `ToolRegistry.run()` only ever uses `subprocess.run(argv,
...)`. There is no `shell=True`, no `Popen(..., shell=True)`, no
`os.system`.

**Why.** Shell injection is a structural, not procedural, problem. Once
`shell=True` is allowed anywhere, an attacker only needs one unescaped
quote to take over the process. argv-only makes the attack surface
empty.

**Cost.** Quoting and glob expansion has to be handled by the caller. We
mitigate this by giving every `ToolSpec` an explicit `allow_args` list —
if a tool needs `--exclude=*.tmp`, the spec records that exact flag and
the registry validates each argv token against it. This makes the
allow-list auditable in one place per tool.

**What we did not do.** We did not invent a domain-specific language for
the agent to speak; the agent emits argv lists, period.

## D2. WAL + busy_timeout for the memory store

**Decision.** `MemorySystemV2` opens its SQLite connection with
`isolation_level=None` (autocommit), `journal_mode=wal`, and
`busy_timeout=5000` (ms).

**Why.** WAL gives concurrent readers + one writer without locking the
whole file. busy_timeout lets a reader wait briefly for a writer
without throwing. The two together are enough for a single-host agent
that writes at human-interactive rates.

**What we did not do.** We did not introduce a separate vector store
dependency. The FTS5 + BM25 stack is enough for the workload this
package targets; adding a vector index is straightforward later (the
code already partitions by `category`, so a per-category index can be
added without rewriting the API).

## D3. Three-layer memory instead of one big context window

**Decision.** `L0 (perception) -> L1 (storage) -> L2 (cognition)`, with
strict boundaries.

**Why.** A single context window conflates "what happened" with "what is
true now" with "what the user might want next". The L0/L1/L2 split
forces every memory to declare its evidence chain before it can
influence retrieval, which is the only way to make unlearning
controllable later.

**Cost.** The first call has to do three index lookups. We offset this by
caching the L1 store list in memory and re-indexing only on write
(`self._bm25_dirty = True` after every `store()`).

## D4. RRF, not learned fusion

**Decision.** Hybrid retrieval uses Reciprocal Rank Fusion (Cormack et
al., k=60). There are no learned weights.

**Why.** A learned fusion model needs a labelled relevance dataset and
makes the system non-deterministic. RRF is parameter-free, well
studied, and beats either single scorer alone on the in-suite
benchmarks.

**Cost.** RRF assumes both scorers return rankings of comparable
quality. We keep this honest by always running both scorers and never
short-circuiting.

## D5. Triple-guarded opt-in E2E

**Decision.** `test_ssh_executor_e2e.py` is module-skip by default. To
actually run it, three conditions must hold:

1. `KYLIN_SSH_E2E == "1"` in the environment.
2. `import paramiko` succeeds.
3. A TCP probe to `localhost:2222` (or the configured host) succeeds.

If any check fails, the module records a skip with the specific reason
in the pytest summary line.

**Why.** A test suite that talks to the network by default is a CI
liability. Triple guard means even an accidental CI configuration
change cannot silently make the suite depend on infra that may not
exist. The skip line itself documents which guard failed.

## D6. Output cap and timeout on every tool call

**Decision.** Every `ToolSpec` carries `timeout_s` and
`output_cap_bytes`. Every SSH command inherits the same shape.

**Why.** A runaway `cat` or `find /` on a multi-million-file disk can
starve the agent. Output cap and timeout are the cheapest possible
defenses; we put them on the path of every call.

**What we did not do.** We did not implement partial-streaming of large
outputs. A future change can add an iterator API; today the data flow
fits comfortably under 1 MiB per call.

## D7. Path safety in `ssh_executor_from_vm_spec`

**Decision.** The factory only reads `network.forward[0].host_port` from
the JSON spec. It does **not** spawn a subprocess, open a socket, or
modify any file.

**Why.** Reading a JSON spec looks benign, but if the factory ever
spawns something based on the parsed data, the spec becomes an SSRF
vector. Keeping the factory pure means the spec can come from any
source without becoming a security boundary.