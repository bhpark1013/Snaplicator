# snaplicator-init (stage 1: plan only)

Surveys a Linux host and plans where the Snaplicator btrfs pool should
live (issue #19). **Read-only**: it measures, discovers, ranks and reports —
it never formats, mounts, or writes outside `--collect-fixture`.

```sh
cd cli

# measure the payload from the publisher, then plan
python3 -m snaplicator_init "postgres://user:pw@host/db"

# skip measurement (testing, or psql unavailable)
python3 -m snaplicator_init --payload-bytes 83751862272

# machine-readable plan (the stage-2 executor contract)
python3 -m snaplicator_init --payload-bytes 83751862272 --json

# capture this machine's topology for offline replay / bug reports
python3 -m snaplicator_init --collect-fixture /tmp/fx
python3 -m snaplicator_init --from-fixture /tmp/fx --payload-bytes 1
```

Exit codes: `0` a home exists · `1` no-fit (remediation printed) · `2`
collection/measurement failure.

## Candidate priorities

| # | shape of free space | action |
|---|---|---|
| 1 | existing btrfs mount | create a subvolume — nothing to format |
| 2 | free space inside a real local fs (ext4/xfs/btrfs) | loopback file + `mkfs.btrfs` |
| 3 | bare block device (no partitions, no fs signature, unmounted) | format — **never auto-selected**; requires an explicit flag in stage 2 |

Requirement: `max(payload × 2, 10 GiB)` — grounded on prod telemetry
(78 GiB payload → ~280 GiB pool after months of snapshot/clone retention).

## Layout

- `snaplicator_init/plan.py` — pure decision logic (no subprocess/TTY/fs
  access); fully covered by fixture-driven tests
- `snaplicator_init/collect.py` — the only module that runs discovery
  commands (`findmnt --json`, `lsblk --json`), unprivileged
- `snaplicator_init/measure.py` — payload sizing via `psql` (pure SQL
  builders/parsers + one thin runner)
- `tests/fixtures/` — captured real-host outputs (the answer-key inputs)
  plus `golden-prod-plan.json` freezing the plan JSON contract

Dependencies: Python ≥ 3.10 stdlib only (+ `psql` when measuring).

```sh
python3 -m pytest        # from cli/; needs pytest only
```
