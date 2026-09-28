# E2E test guide

The end-to-end tests in this package are deliberately opt-in. They reach
a real Kylin V11 host over SSH and depend on infrastructure that may or
may not be available where the package is evaluated.

## Default behaviour

```powershell
PS> py -3.12 -m pytest tests/test_ssh_executor_e2e.py -v
tests\test_ssh_executor_e2e.py::test_local_tcp_probe SKIPPED (KYLIN_SSH_E2E != 1)
======================== 1 skipped in 0.02s =========================
```

The test is module-skip by default. The skip line documents the first
guard that failed (`KYLIN_SSH_E2E != 1`), so the reason is visible in CI
output without having to read the test source.

## Three guards

1. **Environment opt-in.** `KYLIN_SSH_E2E=1` is required. This is the
   single switch an operator must set to enable E2E.
2. **`paramiko` import probe.** If paramiko is missing, the test is
   skipped with a different reason line (`paramiko not installed`).
3. **TCP probe to the configured host.** The default host is
   `localhost:2222` (matching `kylin_v11/scripts/launch_vm.py`). If the
   probe fails, the test is skipped with `host unreachable`.

Every guard produces a distinct skip reason, so a CI log can tell you
*why* the test was skipped without you having to read the source.

## Enabling E2E on a workstation

```powershell
# 1. Make sure a Kylin V11 VM is reachable on localhost:2222
ssh -p 2222 kylin@localhost uname -a

# 2. Install paramiko
py -3.12 -m pip install paramiko

# 3. Opt in and run
$env:KYLIN_SSH_E2E = "1"
py -3.12 -m pytest tests/test_ssh_executor_e2e.py -v
```

## Enabling E2E in CI

```yaml
- name: E2E against Kylin V11
  env:
    KYLIN_SSH_E2E: "1"
    KYLIN_SSH_HOST: ssh://kylin@10.0.0.42:22
  run: |
    pip install paramiko
    pytest tests/test_ssh_executor_e2e.py -v
```

If the CI runner has no Kylin host, simply leave `KYLIN_SSH_E2E`
unset; the test will skip cleanly and the rest of the suite still
passes.

## What the E2E actually exercises

The E2E file contains one smoke test:

- it opens an SSH connection to the configured host,
- runs a single read-only command (`uname -a` or equivalent),
- asserts `CommandResult.ok == True` and that `stdout` contains a
  recognisable kernel banner.

It does not exercise the full agent loop. The full loop is mocked
deterministically in `test_ssh_mock_v2.py` so it can run without
infra; the E2E file exists only to prove that the real SSH path still
works end-to-end when the opt-in flags are set.