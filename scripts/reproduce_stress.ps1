# Reproduce the stress benchmark captured in evidence\stress_benchmark.txt.
# Usage: pwsh -File scripts\reproduce_stress.ps1

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

Write-Host "==> Reproducing stress benchmark (1 000 stores / 100 recalls)" -ForegroundColor Cyan
py -3.12 -c @"
import sys, time, tempfile, os
sys.path.insert(0, '.')
from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
m = MemorySystemV2(MemoryConfig(db_path=tmp.name))

N = 1000
t0 = time.perf_counter()
for i in range(N):
    m.store(
        content=f"event {i}: user modified config file /etc/app{i%50}.conf and restarted service",
        category="episodic",
        importance=0.5 + (i % 10) * 0.05,
    )
store_time = time.perf_counter() - t0

Q = 100
t0 = time.perf_counter()
for i in range(Q):
    m.recall(query=f"config file {i}", top_k=5)
recall_time = time.perf_counter() - t0

print(f"stress: {N} stores in {store_time*1000:.1f} ms (p50 {store_time*1000/N:.3f} ms/store)")
print(f"stress: {Q} recalls in {recall_time*1000:.1f} ms (p50 {recall_time*1000/Q:.3f} ms/recall)")
print(f"db size: {os.path.getsize(tmp.name)/1024:.1f} KB")
m.close()
os.remove(tmp.name)
"@ | Tee-Object -FilePath "$root\evidence\stress_benchmark_local.txt"

Write-Host "==> Done. Local run saved to evidence\stress_benchmark_local.txt" -ForegroundColor Green