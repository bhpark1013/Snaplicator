# snaplicator-init (stages 1–2: plan & provision)

Surveys a Linux host, plans where the Snaplicator btrfs pool should live,
and — only with `--apply` — provisions it (issue #19). Without `--apply`
every invocation is **read-only**.

```sh
cd cli

# measure the payload from the publisher, then plan (read-only)
python3 -m snaplicator_init "postgres://user:pw@host/db"

# also print the execution steps without running them
python3 -m snaplicator_init "postgres://user:pw@host/db" --dry-run

# provision (root): plan + execute in one go
sudo python3 -m snaplicator_init "postgres://user:pw@host/db" --apply

# or split planning from execution (the plan JSON is the seam)
python3 -m snaplicator_init "postgres://..." --json > plan.json
sudo python3 -m snaplicator_init --plan plan.json --apply

# skip measurement (testing, or psql unavailable)
python3 -m snaplicator_init --payload-bytes 83751862272

# pin an exact pool size instead of the ×2 formula
sudo python3 -m snaplicator_init --pool-bytes 10737418240 --data-dir /data/pool --apply

# capture this machine's topology for offline replay / bug reports
python3 -m snaplicator_init --collect-fixture /tmp/fx
python3 -m snaplicator_init --from-fixture /tmp/fx --payload-bytes 1
```

Exit codes: `0` success · `1` no-fit (remediation printed) · `2`
collection/measurement failure · `3` execution refused by a safety gate
or a step failed.

## Candidate priorities

| # | shape of free space | action |
|---|---|---|
| 1 | existing btrfs mount | create a subvolume — nothing to format |
| 2 | free space inside a real local fs (ext4/xfs/btrfs) | loopback file + `mkfs.btrfs` |
| 3 | bare block device (no partitions, no fs signature, unmounted) | format — **never automatic**; requires explicit `--format-disk DEV` |

Requirement: `max(payload × 2, 10 GiB)` — grounded on prod telemetry
(78 GiB payload → ~280 GiB pool after months of snapshot/clone retention).
`--pool-bytes` overrides the formula outright.

## Execution model

Every step is *check-then-do*: a declarative check decides "already
satisfied" (skip), otherwise the mutation runs and the check is
re-verified. Re-running after a mid-way failure resumes where it stopped;
re-running after success is a no-op — when `--data-dir` points at an
already-mounted btrfs pool it is reused as-is, without re-applying the
free-space gate.

Loopback specifics: the image is `fallocate`d (not sparse), attached with
direct-io enabled (best-effort — kernel dependent), and persisted via a
tagged `/etc/fstab` line (`loop,nofail`). Known nuance: fstab cannot
express direct-io, so after a reboot the pool runs without it until
`--apply` is re-run (perf only, never correctness).

## Layout

- `snaplicator_init/plan.py` — pure decision logic (no subprocess/TTY/fs
  access); fully covered by fixture-driven tests
- `snaplicator_init/collect.py` — the only module that runs discovery
  commands (`findmnt --json`, `lsblk --json`), unprivileged
- `snaplicator_init/measure.py` — payload sizing via `psql` (pure SQL
  builders/parsers + one thin runner)
- `snaplicator_init/execute.py` — `build_steps` (pure: plan → step dicts,
  owns every safety gate) + `Runner` (the only code that mutates the
  machine; injectable for tests)
- `tests/fixtures/` — captured real-host outputs (the answer-key inputs)
  plus `golden-prod-plan.json` freezing the plan JSON contract

Dependencies: Python ≥ 3.10 stdlib only (+ `psql` when measuring;
`btrfs-progs`/`util-linux` at apply time).

```sh
python3 -m pytest        # from cli/; needs pytest only
```

Verified end-to-end on a live host: loopback provision (1 GiB image →
mkfs → mount → direct-io → fstab), convergent re-run, and
subvolume-in-existing-pool — each followed by full teardown. The
`--format-disk` path is unit-tested only; its live e2e needs a throwaway
VM with an attached blank disk.
