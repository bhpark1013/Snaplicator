"""Human-readable rendering of a plan dict. Pure string building."""

from __future__ import annotations

from typing import Any, Dict

GiB = 1024 ** 3


def human_bytes(n: int) -> str:
    if n >= GiB:
        return f"{n / GiB:.1f} GiB"
    mib = 1024 ** 2
    if n >= mib:
        return f"{n / mib:.0f} MiB"
    return f"{n} B"


_ACTION_LABEL = {
    "subvolume": "create a btrfs subvolume (nothing to format)",
    "loopback-file": "create a loopback file + mkfs.btrfs",
    "format-disk": "format the whole device as btrfs (explicit flag only)",
}


def render(plan: Dict[str, Any], payload_source: str) -> str:
    lines = []
    lines.append("snaplicator init plan (read-only — nothing was changed)")
    lines.append(
        f"  payload:  {human_bytes(plan['payload_bytes'])}  ({payload_source})"
    )
    lines.append(
        f"  required: {human_bytes(plan['required_bytes'])}  (payload × 2, floor 10 GiB)"
    )
    lines.append("")
    lines.append("candidates (ranked):")
    for c in plan["candidates"]:
        mark = "✓" if c["fits"] else "✗"
        fs = c["fstype"] or "no filesystem"
        lines.append(
            f"  {mark} [{c['priority']}] {c['target']:<24} {fs:<8} "
            f"{human_bytes(c['avail_bytes']):>10} free   → {_ACTION_LABEL[c['action']]}"
        )
    if not plan["candidates"]:
        lines.append("  (none discovered)")
    lines.append("")

    chosen = plan["chosen"]
    if chosen is not None:
        lines.append(f"chosen: {chosen['target']} — {_ACTION_LABEL[chosen['action']]}")
        if chosen["action"] == "loopback-file":
            lines.append(
                "  note: loopback carries a modest I/O overhead — fine for test/dev"
                " workloads; heavy production churn prefers a dedicated device"
                " (--format-disk, stage 2)."
            )
        if plan["needs_confirmation"]:
            lines.append(
                "  several candidates fit — this is the recommendation; pin one"
                " with --data-dir PATH"
            )
    else:
        lines.append("no home for the pool on this machine:")
        for r in plan["remediation"]:
            lines.append(f"  • {r}")
    return "\n".join(lines)
