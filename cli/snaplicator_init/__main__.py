"""CLI entry point.

    python3 -m snaplicator_init [CONNSTR] [options]

Default invocation is plan-only (read-only). `--dry-run` additionally
prints the execution steps; `--apply` runs them (root). Exit codes:
    0  plan produced (and, with --apply, provisioning succeeded)
    1  no-fit (plan produced, remediation printed)
    2  collection or measurement failure
    3  execution refused by a safety gate, or a step failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .collect import CollectError, collect, read_fixture, write_fixture
from .execute import ExecuteError, Runner, build_steps, render_steps
from .measure import MeasureError, measure
from .plan import make_plan
from .report import render


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="snaplicator-init",
        description="Survey this machine, plan where the btrfs pool should "
                    "live, and (with --apply) provision it.",
    )
    p.add_argument("connstr", nargs="?", help="publisher libpq connstring/URI for payload measurement")
    p.add_argument("--payload-bytes", type=int, help="skip measurement, use this payload size")
    p.add_argument("--pool-bytes", type=int, help="override the required pool size (skips the ×2 formula)")
    p.add_argument("--tables", help="comma-separated schema-qualified tables to replicate")
    p.add_argument("--schemas", help="comma-separated schemas to replicate")
    p.add_argument("--data-dir", help="pin the pool location to the filesystem containing PATH")
    p.add_argument("--force", action="store_true", help="with --data-dir: skip the free-space check")
    p.add_argument("--json", action="store_true", help="emit the plan as JSON instead of a report")
    p.add_argument("--from-fixture", metavar="DIR", help="replay a captured fixture instead of live discovery")
    p.add_argument("--collect-fixture", metavar="DIR", help="dump live discovery output to DIR and exit")
    p.add_argument("--plan", metavar="FILE", help="load a saved plan JSON instead of planning (for --apply)")
    p.add_argument("--apply", action="store_true", help="execute the plan (requires root)")
    p.add_argument("--dry-run", action="store_true", help="print the execution steps without running them")
    p.add_argument("--yes", action="store_true", help="accept the recommended candidate when several fit")
    p.add_argument("--format-disk", metavar="DEV", help="format this bare disk (destructive, never automatic)")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def _build_plan(args):
    """Returns (plan, payload_source) or an int exit code."""
    if args.plan:
        try:
            with open(args.plan, "r", encoding="utf-8") as f:
                return json.load(f), f"loaded from {args.plan}"
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot load plan {args.plan}: {e}", file=sys.stderr)
            return 2

    try:
        if args.from_fixture:
            findmnt_raw, lsblk_raw = read_fixture(args.from_fixture)
        else:
            findmnt_raw, lsblk_raw = collect()
    except CollectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    top_tables = None
    if args.payload_bytes is not None:
        payload = args.payload_bytes
        payload_source = "--payload-bytes"
    elif args.connstr:
        tables = args.tables.split(",") if args.tables else None
        schemas = args.schemas.split(",") if args.schemas else None
        try:
            measured = measure(args.connstr, tables=tables, schemas=schemas)
        except MeasureError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        payload = measured["payload_bytes"]
        top_tables = measured["top_tables"]
        scope = "tables" if tables else ("schemas" if schemas else "whole database")
        payload_source = f"measured from publisher, scope: {scope}"
    elif args.pool_bytes is not None:
        payload = 0
        payload_source = "--pool-bytes (no payload measured)"
    else:
        print(
            "error: provide a publisher CONNSTR to measure the payload, "
            "or --payload-bytes / --pool-bytes to skip measurement",
            file=sys.stderr,
        )
        return 2

    plan = make_plan(
        findmnt_raw,
        lsblk_raw,
        payload_bytes=payload,
        required_override=args.pool_bytes,
        data_dir=args.data_dir,
        force=args.force,
        top_tables=top_tables,
    )
    return plan, payload_source


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        if args.collect_fixture:
            findmnt_path, lsblk_path = write_fixture(args.collect_fixture)
            print(f"fixture written: {findmnt_path}, {lsblk_path}")
            return 0
    except CollectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    built_plan = _build_plan(args)
    if isinstance(built_plan, int):
        return built_plan
    plan, payload_source = built_plan

    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    elif args.pool_bytes is not None and not args.plan:
        print(render(plan, payload_source, required_source="--pool-bytes override"))
    else:
        print(render(plan, payload_source))

    if not (args.apply or args.dry_run):
        return 0 if plan["status"] == "ok" else 1

    try:
        built = build_steps(
            plan,
            data_dir=args.data_dir,
            format_disk=args.format_disk,
            assume_yes=args.yes,
        )
    except ExecuteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    if args.dry_run:
        print()
        print(render_steps(built))
        print("(dry run — nothing was changed)")
        return 0

    if os.geteuid() != 0:
        print("error: --apply mutates the machine; re-run with sudo",
              file=sys.stderr)
        return 3

    try:
        Runner().run(built)
    except ExecuteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    print(f"pool ready: {built['pool_dir']} ({built['action']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
