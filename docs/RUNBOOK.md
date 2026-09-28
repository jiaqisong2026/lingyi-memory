# LingYi Operations Runbook

> For SRE / ops engineers who run LingYi in production. This is
> the **what to do when** guide.

## 1. Daily operations

### 1.1 Health check

```
curl -s http://<host>:8080/healthz   # -> {"status": "alive"} (200) or 503
curl -s http://<host>:8080/readyz    # -> {"status": "ready"} (200) or 503
```

If `/healthz` returns 503, the process is shutting down. If
`/readyz` returns 503, the memory handle is not open. Both should
page on-call.

### 1.2 Metrics scrape

```
curl -s http://<host>:8080/metrics   # Prometheus text format
```

Available counters:

| Metric | Meaning |
|---|---|
| `lingyi_stores_total` | Total memory rows stored since process start. |
| `lingyi_recalls_total` | Total recall calls since process start. |
| `lingyi_store_errors_total` | Total store failures. Should be 0. |
| `lingyi_recall_errors_total` | Total recall failures. Should be 0. |
| `lingyi_last_store_latency_ms` | Last store latency in milliseconds. |
| `lingyi_last_recall_latency_ms` | Last recall latency in milliseconds. |
| `lingyi_http_requests_total` | Total HTTP requests received (health + memory). |

### 1.3 Logs

Structured JSON logs to stdout. Standard pattern fields:

- `ts` — ISO timestamp
- `level` — DEBUG/INFO/WARN/ERROR
- `name` — logger name (e.g. `lingyi.runtime`)
- `message` — human-readable

Recommended log retention: 14 days local, ship to centralised log
store (ELK / Loki / Splunk) for 90 days.

## 2. Deployment

### 2.1 Container

```
docker build -f production/Dockerfile -t lingyi:$(git rev-parse --short HEAD) .
docker run --rm \
  -p 8080:8080 \
  -v /var/lib/lingyi:/var/lib/lingyi \
  -e LINGYI_DB_PATH=/var/lib/lingyi/data/memory.db \
  -e LINGYI_LOG_LEVEL=INFO \
  lingyi:$(git rev-parse --short HEAD)
```

### 2.2 Kubernetes

A minimal deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: lingyi
spec:
  replicas: 3
  selector:
    matchLabels: { app: lingyi }
  template:
    metadata:
      labels: { app: lingyi }
    spec:
      containers:
        - name: lingyi
          image: registry.example.com/lingyi:v3
          ports:
            - { containerPort: 8080 }
          env:
            - { name: LINGYI_DB_PATH, value: /var/lib/lingyi/data/memory.db }
            - { name: LINGYI_LOG_LEVEL, value: INFO }
          volumeMounts:
            - { name: data, mountPath: /var/lib/lingyi }
          readinessProbe:
            httpGet: { path: /readyz, port: 8080 }
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /healthz, port: 8080 }
            periodSeconds: 10
            failureThreshold: 3
          resources:
            requests: { cpu: 200m, memory: 256Mi }
            limits: { cpu: 1, memory: 1Gi }
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: lingyi-data }
```

### 2.3 Per-tenant isolation

Each tenant gets a unique `db_path`. In Kubernetes, mount a
`PersistentVolumeClaim` per tenant and set `LINGYI_DB_PATH` to the
tenant's mounted path. This is the cheapest, most defensible
isolation model.

For >1000 tenants, switch to a per-tenant schema in a single MySQL /
Postgres database instead of per-tenant SQLite. The `MemorySystemV2`
API does not change.

## 3. Incident response

### 3.1 Symptom: `/readyz` returning 503

Likely cause: memory handle is not open. Action:

```bash
# 1. Confirm the symptom.
curl -s -i http://<host>:8080/readyz

# 2. Check the process is alive.
kubectl get pod -l app=lingyi
# or:
ps -ef | grep lingyi

# 3. Check the DB file exists and is readable.
ls -la $LINGYI_DB_PATH
# or:
kubectl exec -it <pod> -- ls -la /var/lib/lingyi/data/memory.db

# 4. Check the most recent log lines for the open() error.
kubectl logs <pod> --tail=50
```

### 3.2 Symptom: slow recalls

Likely cause: database file is large (>1 GB) or contention is high.
Actions:

```bash
# 1. Check the db file size.
ls -la $LINGYI_DB_PATH

# 2. If large, run cleanup_expired() to remove episodic rows whose
#    TTL has passed.
python -c "
import sys; sys.path.insert(0, '/opt/lingyi')
from src.kylin_v11_agent.memory_v2.core import MemorySystemV2, MemoryConfig
m = MemorySystemV2(MemoryConfig(db_path='$LINGYI_DB_PATH'))
removed = m.cleanup_expired()
print('removed', removed, 'expired rows')
m.close()
"

# 3. If still slow, consider sharding by category (see SYSTEM_DESIGN_PDD.md).
```

### 3.3 Symptom: `database is locked` errors

Likely cause: multiple writers contending on the same SQLite file.
Default `busy_timeout_ms` is 30 s; if the production pattern uses
shared connections across threads, the runtime must serialise
writes (see `tests/test_concurrency.py` for the queue-serialised
writer pattern).

Action:

```bash
# 1. Confirm the error rate from the metrics.
curl -s http://<host>:8080/metrics | grep errors

# 2. If errors are non-zero, add a write-queue between the agent
#    runtime and the substrate (see docs/SYSTEM_DESIGN_PDD.md §3).
```

### 3.4 Symptom: disk full

Likely cause: episodic memories accumulated without TTL cleanup.
Action:

```bash
# 1. Free disk first.
df -h $LINGYI_DATA_DIR

# 2. Run cleanup_expired().
python -c "from src.kylin_v11_agent.memory_v2.core import *; ..."
# (same as 3.2 step 2)

# 3. Add a cron to run cleanup_expired() nightly.
# 0 3 * * * python -c "..."
```

### 3.5 Symptom: process keeps crashing

Likely cause: corrupted database file. Action:

```bash
# 1. Inspect the last crash log.
kubectl logs <pod> --previous

# 2. From a backup, restore the most recent good copy.
# 3. Open the failed file in isolation:
python -c "
import sqlite3
try:
    conn = sqlite3.connect('$LINGYI_DB_PATH')
    rows = conn.execute('PRAGMA integrity_check').fetchall()
    print('integrity:', rows)
except Exception as e:
    print('FAIL:', e)
"

# 4. If integrity_check reports anything but 'ok', the file is
#    corrupt. The substrate should refuse to open it (see
#    tests/test_failure_injection.py::TestCorruptedFile).
```

## 4. Capacity planning

| Tier | Tenants | Daily events / tenant | Total events / day | DB growth / day | Recommended hosts |
|---|---|---|---|---|---|
| Small | < 100 | 10 000 | 1 M | ~ 1 GB | 2 (HA) |
| Medium | 100 - 1 000 | 50 000 | 50 M | ~ 50 GB | 5 + sharded by category |
| Large | 1 000 - 10 000 | 100 000 | 1 B | ~ 1 TB | 20 + cross-AZ replication |

The numbers assume a 256-byte average row + WAL + 30-day retention
on episodic tier. Tune to your own workload.

## 5. Upgrade procedure

1. Build the new image: `docker build -f production/Dockerfile -t lingyi:vN+1 .`
2. Push to the registry: `docker push registry.example.com/lingyi:vN+1`
3. Roll out via `kubectl set image deployment/lingyi lingyi=registry.example.com/lingyi:vN+1`
4. Monitor `/readyz` and `lingyi_store_errors_total` for 15 minutes.
5. If the rollout stalls, `kubectl rollout undo deployment/lingyi` reverts.

Schema migrations:

- The substrate does not require an external migration step. The
  v2 schema uses `CREATE TABLE IF NOT EXISTS` so the new code
  picks up new tables on first open. The v1 bridge is left
  untouched for backward compatibility with existing tenants.
- For tenants on very old versions, see `docs/MIGRATION.md` (not
  included in this submission; recorded in the project tracker).

## 6. Backup and recovery

A SQLite + WAL pair is backed up like any other file:

```bash
# Cold backup: while the service is down.
cp $LINGYI_DATA_DIR/memory.db $BACKUP_DIR/memory.db.$(date +%Y%m%d)

# Hot backup: while the service is up, use sqlite3'``s .backup command
# in a side connection so WAL is flushed.
python -c "
import sqlite3
src = sqlite3.connect('$LINGYI_DB_PATH')
dst = sqlite3.connect('$BACKUP_DIR/memory.db.$(date +%Y%m%d)')
src.backup(dst)
dst.close()
src.close()
"
```

For continuous backup, run [Litestream](https://litestream.io)
against the SQLite file and ship to S3. Litestream reads the WAL
directly; it has no impact on the running service.

### 6.1 Sample Litestream configuration

Drop this at `/etc/litestream.yml` on the host (or mount it into the
container). It mirrors the SQLite + WAL pair to S3 every 10 s and
keeps a 24 h of generations on local disk for fast restores.

```yaml
# /etc/litestream.yml -- sample, copy and adapt to your bucket.
dbs:
  - path: /var/lib/lingyi/data/memory.db
    replicas:
      - url: s3://lingyi-backups/memory.db
        # S3 credentials live in the runtime env, not in this file.
        access-key-id:      ${LITESTREAM_ACCESS_KEY_ID}
        secret-access-key: ${LITESTREAM_SECRET_ACCESS_KEY}
        # Keep local WAL for 24 h so a restore does not need to pull
        # from S3 for the very latest writes.
        retention: 24h
        # Snapshot cadence is per database; 6 h is a reasonable default
        # for a write-heavy agent memory.
        snapshot-interval: 6h

# Run as: litestream replicate -config /etc/litestream.yml
# On Kubernetes, ship litestream as a sidecar in the same pod as the
# LingYi container, sharing the data volume.
```

Restore procedure (cold start on a fresh host):

```bash
litestream restore -config /etc/litestream.yml \
  /var/lib/lingyi/data/memory.db
systemctl start lingyi  # or kubectl apply the Deployment
```

## 7. Security

- The default port 8080 binds to 0.0.0.0 in the runtime. In
  production, restrict to the cluster network via NetworkPolicy.
- The HTTP endpoints return memory metadata (counters, uptime)
  but never the memory content. There is no `/v1/dump` endpoint
  and there will not be one.
- `SSHConfig.key_filename` and `password` are passed directly to
  paramiko. The runtime layer (not the substrate) is responsible
  for sourcing them from a secret store.
- See `docs/DESIGN_DECISIONS.md` §D7 for the path-safety argument
  behind `ssh_executor_from_vm_spec`.