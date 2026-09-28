# Changelog

All notable changes to this package are recorded here. Versions are
monotonic within a single review round.

## Round 6 — Round-2 backlog sweep + opencode deepening (current)

### Added
- **`tests/data/sample_opencode_session.json`** — real-shape opencode
  session file (6 messages, two user + four assistant) shipped
  with the test suite.
- **`scripts/ingest_opencode_session.py`** — CLI entry point that
  ingests any opencode session.json into a LingYi memory DB.
  Accepts `--session`, `--db`, `--category`, `--importance`.
- **`docs/ARCHITECTURE.md` section 6** — ASCII flow diagram for the
  opencode → LingYi memory ingest path (R2-N2).
- **`tests/test_opencode_adapter.py::TestRealSessionFile`** — two
  regression tests covering parse of the committed fixture and
  ingest + recall against real LingYi memory v2.

### Changed
- **`src/kylin_v11_agent/agent.py::_plan`** now normalises
  punctuation + case via regex before matching keywords, so
  descriptions like `ShowPythonInterpreter` or
  `show, python-version` hit the right tool (R3-N2).
- **`src/kylin_v11_agent/agent.py::AgentConfig.task_timeout_s`**
  carries an inline comment documenting the trade-off: the
  budget is checked BEFORE each new tool call; in-flight tools
  are not interrupted (R1-N3).
- **`tests/test_agent_plan.py::FakeToolRegistry`** is now a real
  `ToolRegistry` subclass instead of a duck-typed stand-in
  (R1-N2).

### Test counts
| Round | Tests passed | Failed | Skipped |
|---|---|---|---|
| Round 1 | 62 | 0 | 1 |
| Round 2 | 81 | 0 | 1 |
| Round 3 | 101 | 0 | 1 |
| Round 4 | 125 | 0 | 1 |
| Round 5 | 157 | 0 | 1 |
| **Round 6 (current)** | **163** | **0** | **1** |

### Final ship decision

SHIP. Every LOW and INFO from the Round-2 verdict is now closed
(with code + test or with a build-pipeline rule). The opencode
integration is no longer a synthetic fixture-only story: a real
session JSON is shipped, a CLI is shipped, and recall against
the real LingYi v2 memory works end-to-end. 163 tests pass in
~15 s; package size still ~190 KB zipped.


## Round 5 — operability + demo

### Added
- **`KylinAgent.from_existing_memory(memory)`** — public class method
  that builds a `KylinAgent` reusing a caller-supplied memory handle
  (typically a `MemorySystemV2`). Replaces the brittle
  `KylinAgent.__new__(KylinAgent)` pattern that the demo used to
  depend on (R5 / OPS-1).
- **`scripts/demo_end_to_end.py`** — runnable end-to-end demo of a
  Chinese-language agent session ("merchant-admin" investigating a
  stuck `kylin-order-svc` on a Kylin V11 host). Exercises the full
  stack: keyword routing, v2 memory, recall, CJK tokenization.
  Output is deterministic (`random.seed(42)`) and lives at
  `evidence/demo_end_to_end.txt`.
- **`tests/test_failure_injection.py`** — 30 resilience tests:
  corrupted file, missing parent dir, special characters, unicode /
  emoji, 1 MB single row, idempotent close, double-store, etc.
- **`docs/RUNBOOK.md` §6.1** — sample `litestream.yml` config block
  for S3 continuous backup of the SQLite + WAL pair, plus a cold-restore
  procedure (R5 / OPS-2).
- **`production/runtime.py`** — code comment warning that a single
  shared `MemorySystemV2` connection across threads requires a write
  queue; the proven pattern is in `tests/test_concurrency.py`
  (R5 / OPS-4).

### Changed
- `scripts/demo_end_to_end.py` — uses `KylinAgent.from_existing_memory`
  instead of `KylinAgent.__new__`. Public API surface only; behaviour
  identical.
- `README.md` — headline test count refreshed to **157**; Quick start
  gained a "run the demo" step; layout block lists the Round 5 test
  files; §5 evidence table now includes the demo and the realistic
  benchmark.

### Test counts
| Round | Tests passed | Failed | Skipped |
|---|---|---|---|
| Round 1 | 62 | 0 | 1 |
| Round 2 | 81 | 0 | 1 |
| Round 3 | 101 | 0 | 1 |
| Round 4 | 125 | 0 | 1 |
| **Round 5 (current)** | **157** | **0** | **1** |

### Final ship decision
SHIP. Three small R5 review items (OPS-1 public constructor, OPS-2
sample Litestream config, OPS-4 shared-connection comment) all
addressed. Package is now demonstrable, resilient, and operable; 157
tests pass in ~13 s; reviewer can run the demo, the benchmark, the
concurrency proof, and the failure-injection suite without any
network access.

## Round 4 — Pinduoduo-fit polish + production artifacts

### Added
- **`production/`** — long-running runtime + HTTP health server
  (endpoints `/healthz`, `/readyz`, `/metrics` in Prometheus text
  format, `/v1/stats` JSON) + `Dockerfile` (non-root, fixed
  UID/GID 10001, healthcheck) + `runtime.py` entrypoint with
  SIGTERM/SIGINT graceful shutdown.
- **`tests/test_health_server.py`** — 10 tests covering endpoints,
  lifecycle, and HealthState counters.
- **`src/kylin_v11_agent/text_tokenize.py`** — CJK bigram + ASCII
  tokenizer with pluggable external hook (jieba example included).
- **`tests/test_chinese_tokenize.py`** — 11 tests for the tokenizer.
- **`tests/test_concurrency.py`** — 3 tests proving per-thread
  connection + queue-serialised writer + multi-reader patterns.
- **`scripts/bench_realistic.py`** — 100k-record / 10k-query
  benchmark with multi-thread writers + readers; outputs JSON with
  p50/p95/p99/max latencies + throughput.
- **`docs/SYSTEM_DESIGN_PDD.md`** — system-design narrative:
  tenant isolation, per-row cost, recall latency, concurrency
  model, horizontal scaling, cost model, failure modes.
- **`MemoryConfig.busy_timeout_ms`** default raised from 5 000 ms
  to 30 000 ms for production-realistic concurrency.

### Test counts
| Round | Tests passed | Failed | Skipped |
|---|---|---|---|
| Round 1 | 62 | 0 | 1 |
| Round 2 | 81 | 0 | 1 |
| Round 3 | 101 | 0 | 1 |
| Round 4 | **125** | **0** | **1** |

### Final ship decision
SHIP. All 5 R4 findings (PDD-1..PDD-5) addressed in this commit.
Package now demonstrates: production deployment, observability,
concurrency story, Chinese support, real benchmark, system design.

## Round 3 — opencode integration + carry-over fixes

### Added
- **`src/integrations/opencode_adapter/`** — Python adapter that reads
  opencode session JSON and indexes it through LingYi memory v2.
  Includes `reader.py` (permissive JSON -> dataclass), `indexer.py`
  (stateless wrapper around any v2-compatible memory handle), and 15
  passing tests including a real-integration test against the live
  `MemorySystemV2`.
- **`opencode_reference/`** — curated MIT-attributed subset of opencode
  source (7 files, ~100 KB) for architectural review. Includes the
  agent service, session message format, compaction strategy, storage
  interface, and tool registry. Full attribution in
  `opencode_reference/ATTRIBUTION.md`; full MIT text in
  `opencode_reference/LICENSE-OPENCODE.txt`.
- **`docs/INTEGRATION_OPENCODE.md`** — integration narrative: what
  opencode is, what is included, how the adapter works, non-goals.
- **`tests/test_config_max_timeout.py`** — 4 tests for the new
  `SSHConfig.max_call_timeout_s` knob (R3-N3).
- **`SSHConfig.max_call_timeout_s`** — operator-configurable ceiling
  on per-call timeout override (R3-N3).
- **`agent.py::_plan`** raises `ValueError` on unknown override keys
  (R1-N1).

### Test counts
| Round | Tests passed | Failed | Skipped |
|---|---|---|---|
| Round 1 | 62 | 0 | 1 |
| Round 2 | 81 | 0 | 1 |
| Round 3 | **101** | **0** | **1** |

### Final ship decision
SHIP. LingYi core, opencode integration, opencode reference, three
rounds of review artefacts. One MED finding (R2-OC1 — README
discoverability) addressed in this same commit by adding a
second-deliverable paragraph to README §1.

## Round 2 — post-review fixes

### Fixed
- **R3-1 (HIGH)**: `SSHExecutor.run()` now uses a deliberate POSIX
  single-quote helper (`_shell_quote`) for the `cwd` argument instead
  of `repr()`. A new test in `tests/test_ssh_quoting.py` proves a
  malicious `cwd="evil'; cat /etc/passwd; echo '"` no longer breaks
  out of the quoted path. Newline paths are refused with `ValueError`.
- **R3-4 (MED)**: `SSHExecutor.run(timeout=...)` now clamps the
  per-call timeout to `MAX_CALL_TIMEOUT = 600 s`. A planner bug that
  passes `timeout=10**9` no longer pins the agent forever. New tests
  cover the clamp and the negative-timeout fallback.
- **R1-1 (HIGH)**: Added `tests/test_agent_plan.py` (7 tests) that
  exercises `KylinAgent.run()` end-to-end through a fake `ToolRegistry`,
  proving keyword routing, argv override, fallback behaviour, and
  `AgentReport` shape.
- **R1-4 (LOW)**: `SessionManager._save` no longer emits `indent=2`,
  reducing session-file size by ~5x for busy sessions.
- **R1-6 (INFO)**: Added `tests/test_cli_help.py` (2 tests) that
  smoke-tests the CLI `main()` entry point.
- **R3-5 (LOW)**: `SessionManager._save` now writes through a
  `tempfile.mkstemp` + `os.replace` sequence so a crash mid-write
  leaves the previous file untouched. New `tests/test_session_atomic.py`
  asserts no `.tmp` file is left behind.

### Changed
- **R2-1, R2-2, R2-4, R2-6**: `README.md` rewritten — opens with a
  one-sentence "what this does" + "why this is not just another CRUD
  project", embeds the test summary block inline, and clarifies the
  Python 3.10 vs 3.12 distinction.
- **R3-2 (HIGH)**: `ToolSpec.validate_argv` docstring expanded to
  document the explicit policy for flag tokens, positional tokens, and
  empty argv.

### Added
- `LICENSE` (Apache-2.0 short form).
- `scripts/clean.ps1` (R2-5) to remove `__pycache__` and stale `.tmp`
  files from the package directory.
- `CHANGELOG.md` (R2-3, this file).

### Test counts
| Round | Tests passed | Failed | Skipped |
|---|---|---|---|
| Round 1 initial | 62 | 0 | 1 |
| Round 2 | **81** | **0** | **1** |

## Round 1 — initial submission

- 19 source files (~95 KB) in `src/kylin_v11_agent/`.
- 3 test files (62 unit tests passing, 1 opt-in E2E module-skip).
- README, ARCHITECTURE, PERFORMANCE, E2E_GUIDE.
- 2 reproduce scripts (`run_tests.ps1`, `reproduce_stress.ps1`).
- Real evidence files (no fabricated numbers).