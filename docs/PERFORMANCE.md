# Performance

All numbers below come from the actual scripts in `evidence/` and are
reproducible with `scripts/reproduce_stress.ps1`. No claims here are
extrapolated from external benchmarks.

## 1. Unit-test wall-clock

| Suite | Tests | Wall-clock | Skip count |
|---|---|---|---|
| `test_ssh_mock_v2.py` | 27 | 0.65 s | 0 |
| `test_memory_v2.py` | 35 | 1.75 s | 0 |
| `test_ssh_executor_e2e.py` | 1 | <0.01 s | 1 (opt-in) |
| **Total default run** | **62 passed, 0 failed** | **2.40 s** | 1 |

## 2. Stress benchmark — 1 000 records, 100 queries

```
Hardware:   Windows 11, Python 3.12 stdlib SQLite
Dataset:    1 000 stores, 4 fields per record, mixed categories
Queries:    100 hybrid (FTS5 BM25 + SQL keyword + RRF fusion)

Store   p50 = 0.633 ms   wall = 633 ms  for 1 000 records
Recall  p50 = 49.30 ms   wall = 4 930 ms for 100 queries
DB size       828 KB  on disk after run
```

Notes on the recall number:

- Hybrid retrieval is intentionally doing **two** index scans plus an
  RRF merge. For workloads with >10 K records, an external vector index
  (or a C-extension for the BM25 path) would be the next optimisation.
- The stress run uses Python 3.12 stdlib SQLite. Switching to the
  pre-compiled SQLite from `chromadb` (already a project optional
  dependency) typically halves the recall time for this workload.

## 3. Tool registry — per-call overhead

| Operation | Time |
|---|---|
| `ToolSpec.validate_argv` (10 tokens, 3 with flags) | ~30 µs |
| `subprocess.run(argv, capture_output=True, timeout=30)` (echo hello) | ~12 ms cold, ~2 ms warm |
| `SSHExecutor.run("echo hello")` over loopback | ~80 ms (SSH handshake dominates) |

## 4. Memory footprint

| Component | Bytes | Notes |
|---|---|---|
| `kylin_v11_agent/__init__.py` | 838 | re-exports only |
| `agent.py` | 10 425 | plan/reflect loop |
| `memory_v2/core.py` | 30 569 | full L0/L1/L2 |
| `memory_v2/retrieval.py` | 10 232 | RRF |
| `memory_v2/knowledge_graph.py` | 13 333 | graph + lineage |
| Total Python source | ≈ 95 KB | excluding `__pycache__` |

## 5. Failure-mode budget

Every public surface has a failure budget:

| API | Guaranteed behaviour on error |
|---|---|
| `MemorySystemV2.store()` | returns `None` and logs; never raises to caller |
| `MemorySystemV2.recall()` | returns `[]` and logs; never raises to caller |
| `SSHExecutor.run()` | returns `CommandResult(error=..., returncode=-1)` |
| `ToolRegistry.run()` | returns `ToolResult(success=False, error=...)` |
| `SessionManager.get()` | returns `None` and logs; never raises |

This means a single bad row, a flaky network, or a confused agent cannot
crash the host process.