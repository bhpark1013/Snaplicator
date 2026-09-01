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


def render(plan: Dict[str, Any], payload_source: str,
           required_source: str = "recommended: room for snapshots and clones, payload × 2") -> str:
    lines = []
    lines.append("snaplicator init plan (read-only — nothing was changed)")
    lines.append(
        f"  payload:  {human_bytes(plan['payload_bytes'])}  ({payload_source})"
    )
    if plan.get("minimum_bytes"):
        lines.append(
            f"  needed:   {human_bytes(plan['minimum_bytes'])}  "
            "(the data itself — below this nothing can be installed)"
        )
    lines.append(
        f"  roomy:    {human_bytes(plan['required_bytes'])}  ({required_source})"
    )
    lines.append("")
    lines.append("candidates (ranked):")
    any_tight = False
    for c in plan["candidates"]:
        if c["fits"] and c.get("comfortable", True):
            mark = "✓"
        elif c["fits"]:
            mark = "△"
            any_tight = True
        else:
            mark = "✗"
        fs = c["fstype"] or "no filesystem"
        lines.append(
            f"  {mark} [{c['priority']}] {c['target']:<24} {fs:<8} "
            f"{human_bytes(c['avail_bytes']):>10} free   → {_ACTION_LABEL[c['action']]}"
        )
    if not plan["candidates"]:
        lines.append("  (none discovered)")
    if any_tight:
        lines.append(
            "  △ = holds the data but under the recommended ×2 headroom —"
            " selectable, not recommended"
        )
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
        for w in plan.get("warnings") or []:
            lines.append(f"  ! {w}")
    else:
        lines.append("no home for the pool on this machine:")
        for r in plan["remediation"]:
            lines.append(f"  • {r}")
    return "\n".join(lines)
