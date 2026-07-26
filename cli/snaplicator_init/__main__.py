"""CLI entry point.

    python3 -m snaplicator_init [CONNSTR] [options]

Stage 1 is plan-only: every invocation is a dry run. Exit codes:
    0  plan produced (a home exists)
    1  no-fit (plan produced, remediation printed)
    2  collection or measurement failure
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .collect import CollectError, collect, read_fixture, write_fixture
from .measure import MeasureError, measure
from .plan import make_plan
from .report import render


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="snaplicator-init",
        description="Survey this machine and plan where the btrfs pool should live (read-only).",
    )
    p.add_argument("connstr", nargs="?", help="publisher libpq connstring/URI for payload measurement")
    p.add_argument("--payload-bytes", type=int, help="skip measurement, use this payload size")
    p.add_argument("--tables", help="comma-separated schema-qualified tables to replicate")
    p.add_argument("--schemas", help="comma-separated schemas to replicate")
    p.add_argument("--data-dir", help="pin the pool location to the filesystem containing PATH")
    p.add_argument("--force", action="store_true", help="with --data-dir: skip the free-space check")
    p.add_argument("--json", action="store_true", help="emit the plan as JSON instead of a report")
    p.add_argument("--from-fixture", metavar="DIR", help="replay a captured fixture instead of live discovery")
    p.add_argument("--collect-fixture", metavar="DIR", help="dump live discovery output to DIR and exit")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    try:
        if args.collect_fixture:
            findmnt_path, lsblk_path = write_fixture(args.collect_fixture)
            print(f"fixture written: {findmnt_path}, {lsblk_path}")
            return 0

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
    else:
        print(
            "error: provide a publisher CONNSTR to measure the payload, "
            "or --payload-bytes to skip measurement",
            file=sys.stderr,
        )
        return 2

    plan = make_plan(
        findmnt_raw,
        lsblk_raw,
        payload_bytes=payload,
        data_dir=args.data_dir,
        force=args.force,
        top_tables=top_tables,
    )

    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        print(render(plan, payload_source))
    return 0 if plan["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
