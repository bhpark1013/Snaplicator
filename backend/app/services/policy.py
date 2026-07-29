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
from typing import Dict, List


def _path() -> Path:
    d = Path.home() / ".snaplicator"
    d.mkdir(parents=True, exist_ok=True)
    return d / "selection_policy.json"


def load() -> Dict:
    """{'auto_schemas': [...], 'excluded': [...]}. Absent means never chosen."""
    try:
        data = json.loads(_path().read_text())
        if isinstance(data, dict):
            return {
                "auto_schemas": list(data.get("auto_schemas") or []),
                "excluded": list(data.get("excluded") or []),
                "chosen": True,
            }
    except Exception:
        pass
    return {"auto_schemas": [], "excluded": [], "chosen": False}


def save(auto_schemas: List[str], excluded: List[str]) -> None:
    _path().write_text(json.dumps({
        "auto_schemas": sorted(set(auto_schemas)),
        "excluded": sorted(set(excluded)),
    }))
