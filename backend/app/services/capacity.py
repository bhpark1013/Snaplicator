"""Whether what was chosen will fit where it has to go.

The installer asks the same question, but it has to ask it about the whole
database: nothing has been selected yet, and it runs before there is a screen
to select on. Answering it there and refusing on the answer turns a forecast
about the largest possible choice into a gate on every smaller one.

Here the choice exists. The publication names the tables, so the payload is
the sum of what those tables occupy, and the pool is a real directory with a
real amount of free space under it. That makes two different statements
possible, and only one of them is worth refusing over:

  fits         the data can land at all. A fact about this disk today.
  comfortable  room is left over for the snapshots and clones that follow.
               A forecast about months from now, already watched by the
               runtime usage alert.

Nothing here reserves anything. The pool is a btrfs subvolume sharing its
filesystem's free space, with no quota, so being tight is a thing to say and
not a thing to prevent.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from .replication import _run_publisher_sql

# Kept in step with cli/snaplicator_init/plan.py, which asks the same
# question before there is anything to ask it about.
MINIMUM_MULTIPLIER = 1.1
ROOMY_MULTIPLIER = 2


def pool_dir() -> str:
    """Where the pool is, read when asked rather than when imported.

    The settings object refuses to exist without a configured environment, so
    binding it at import time would make this module unimportable anywhere
    that is not a running manager — including from a test.
    """
    from ..core.config import settings

    return settings.root_data_dir


def pool_free_bytes() -> Optional[int]:
    """Free space where the replica and its clones live, or None if unknown."""
    try:
        st = os.statvfs(pool_dir())
        return int(st.f_bavail) * int(st.f_frsize)
    except (OSError, ValueError, TypeError):
        return None


def published_payload_bytes(publisher_connstr: str, publication_name: str) -> int:
    """What the publication's tables occupy on the primary, indexes included.

    pg_total_relation_size counts indexes and TOAST, which is what actually
    crosses: the subscriber builds the same indexes over the same rows.
    """
    out = _run_publisher_sql(
        publisher_connstr,
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
        "FROM pg_publication_tables pt "
        "JOIN pg_namespace n ON n.nspname = pt.schemaname "
        "JOIN pg_class c ON c.relname = pt.tablename AND c.relnamespace = n.oid "
        f"WHERE pt.pubname = '{publication_name}';",
    ).strip()
    try:
        return int(out.splitlines()[0])
    except (IndexError, ValueError):
        return 0


def check(publisher_connstr: str, publication_name: str) -> Dict:
    """The two statements, plus the numbers behind them.

    `fits` is None when either side is unknown — an unreadable pool or an
    unreachable primary is not evidence that the copy will fail, and a check
    that cannot see is not entitled to refuse.
    """
    free = pool_free_bytes()
    try:
        payload = published_payload_bytes(publisher_connstr, publication_name)
    except Exception:
        payload = 0

    minimum = int(MINIMUM_MULTIPLIER * payload)
    roomy = ROOMY_MULTIPLIER * payload

    fits: Optional[bool] = None
    comfortable: Optional[bool] = None
    if free is not None and payload > 0:
        fits = free >= minimum
        comfortable = free >= roomy

    try:
        pool = pool_dir()
    except Exception:
        pool = ""

    return {
        "pool": pool,
        "payload_bytes": payload,
        "free_bytes": free,
        "minimum_bytes": minimum,
        "roomy_bytes": roomy,
        "fits": fits,
        "comfortable": comfortable,
    }


def _gib(n: int) -> str:
    return f"{n / (1024 ** 3):.1f} GiB"


def refusal(result: Dict) -> Optional[str]:
    """Why the copy must not start, or None to let it.

    Only the fact refuses. Everything else — tight, unknown, unmeasured — is
    for the screen to say and the person to weigh.
    """
    if result.get("fits") is False:
        return (
            f"the selection is {_gib(result['payload_bytes'])} and "
            f"{result['pool']} has {_gib(result['free_bytes'])} free — the copy "
            "cannot finish. Replicate fewer tables, or give the pool more room."
        )
    return None
