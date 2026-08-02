"""What should happen to tables that do not exist yet.

Kept separately from the publication because the publication cannot express
it. PostgreSQL has two ways for a table to join one on its own — FOR ALL
TABLES and FOR TABLES IN SCHEMA — and neither tolerates an exception: there is
no syntax for "this schema, minus these two, and keep taking new ones". A
publication that has to leave anything out therefore becomes a fixed list, and
the only thing that can still add to it is an event trigger.

So the wish ("follow this schema") and the mechanism (schema-level membership,
or a trigger, or nothing) come apart, and the wish has to be written down
somewhere. Here, next to the manager's other state, so that a restarted
manager reinstates what was asked for instead of guessing from the shape of
the publication it finds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _path() -> Path:
    d = Path.home() / ".snaplicator"
    d.mkdir(parents=True, exist_ok=True)
    return d / "selection_policy.json"


def load() -> Dict:
    """The recorded wish. Absent means never chosen."""
    try:
        data = json.loads(_path().read_text())
        if isinstance(data, dict):
            return {
                "auto_schemas": list(data.get("auto_schemas") or []),
                "off_schemas": list(data.get("off_schemas") or []),
                "excluded": list(data.get("excluded") or []),
                "chosen": True,
                # Written before following became the default, when the stored
                # list *was* the scope. Reading it under the new meaning would
                # turn following back on for every schema left out of it — an
                # upgrade that starts publishing tables somebody excluded. The
                # old meaning is kept until the next save says otherwise.
                "legacy": "off_schemas" not in data,
            }
    except Exception:
        pass
    return {
        "auto_schemas": [], "off_schemas": [], "excluded": [],
        "chosen": False, "legacy": False,
    }


def save(
    auto_schemas: List[str],
    excluded: List[str],
    off_schemas: Optional[List[str]] = None,
) -> None:
    """Record the wish as its exceptions.

    `off_schemas` is the load-bearing half. Following is what a replicated
    schema does unless someone says otherwise, and a default cannot be stored
    as a list of the schemas it applies to: a schema created tomorrow is in no
    such list, so every list is out of date the moment a schema appears. Only
    the departures from the default are finite and knowable, so those are what
    is written down. `auto_schemas` is kept because the screen still shows the
    answer per schema, and reading it back is cheaper than recomputing it.
    """
    _path().write_text(json.dumps({
        "auto_schemas": sorted(set(auto_schemas)),
        "off_schemas": sorted(set(off_schemas or [])),
        "excluded": sorted(set(excluded)),
    }))


def capture_scope(
    publication_name: str,
) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[List[str]]]:
    """(follow_schemas, excluded, unfollow_schemas) for the auto-add half.

    `follow_schemas` stays None whether or not anyone has chosen, because the
    scope is always derived from what the publication already covers. That is
    the default: a schema you replicate keeps taking its new tables. What a
    person can change is which schemas step out of it, and that is
    `unfollow_schemas`.

    A publication this install may not rewrite follows nothing, whatever the
    policy says. Auto-add is an ALTER PUBLICATION — the same rewrite the
    selection screen refuses on a publication that is not ours, arriving by a
    different door, and the one that made "this install will never rewrite
    it" untrue in practice.
    """
    from . import publication as publication_svc

    chosen = load()
    follow: Optional[List[str]] = None
    excluded = chosen["excluded"] if chosen["chosen"] else None
    unfollow = chosen["off_schemas"] if chosen["chosen"] else None
    if chosen.get("legacy"):
        # The stored list is the scope, as it was when it was written.
        follow, unfollow = chosen["auto_schemas"], None
    if not publication_svc.may_rewrite(publication_name):
        follow = []
    return follow, excluded, unfollow
