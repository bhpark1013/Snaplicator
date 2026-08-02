"""Which publication this replica is allowed to speak for.

A publication is not ours to assume. A primary may already carry several —
production here carries five — and one of them is likely feeding a replica
somebody else depends on. Narrowing a publication means dropping and
recreating it (see selection.py: PostgreSQL has no syntax for taking a table
out of FOR ALL TABLES), so mistaking someone else's publication for our own
does not misconfigure this replica. It breaks theirs.

The name in the environment is therefore a proposal, not a fact. What makes it
usable is a decision recorded here — reuse the one that exists, or create a new
one under a name of our own — and until that decision exists, nothing is
narrowed. `ours` is the whole point of the record: it is the difference between
a publication this install may rewrite and one it may only read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .replication import _run_publisher_sql

# PostgreSQL would accept far more once quoted, but a publication this install
# creates is one it also has to name in generated SQL and show in a URL. The
# narrow set is a deliberate refusal to carry quoting bugs for a name nobody
# needs.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _path() -> Path:
    d = Path.home() / ".snaplicator"
    d.mkdir(parents=True, exist_ok=True)
    return d / "publication_choice.json"


def load() -> Dict:
    """{'name': str|None, 'ours': bool, 'chosen': bool}. Absent means unasked."""
    try:
        data = json.loads(_path().read_text())
        if isinstance(data, dict) and data.get("name"):
            return {
                "name": str(data["name"]),
                "ours": bool(data.get("ours")),
                "chosen": True,
            }
    except Exception:
        pass
    return {"name": None, "ours": False, "chosen": False}


def save(name: str, ours: bool) -> Dict:
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            "publication name must start with a letter or underscore and contain "
            "only letters, digits and underscores"
        )
    _path().write_text(json.dumps({"name": name, "ours": bool(ours)}))
    return load()


def active(default: Optional[str]) -> Optional[str]:
    """The publication in force: what was chosen, else what the install proposed."""
    chosen = load()
    return chosen["name"] if chosen["chosen"] else (default or None)


def may_rewrite(name: str) -> bool:
    """Whether this install may drop and recreate `name`.

    True only for a publication it was told it owns. A name that merely came
    from the environment does not qualify: the installer writes that file
    without ever looking at the primary, so it is a guess about someone else's
    server until a person says otherwise.
    """
    chosen = load()
    if not chosen["chosen"] or chosen["name"] != name:
        return False
    return bool(chosen["ours"])


def list_existing(publisher_connstr: str, include_internal: bool = False) -> List[Dict]:
    """Publications on the primary, with how much each covers.

    Snaplicator's own DDL-log publication is left out. This list exists to ask
    which publication belongs to someone else; offering the one this install
    created for its own plumbing asks the reader to arbitrate between us and
    us, and picking it would point the replica at a single log table.
    """
    from .replication import CAPTURE_LOG_PUBLICATION

    out = _run_publisher_sql(
        publisher_connstr,
        "SELECT p.pubname, p.puballtables, "
        "  (SELECT count(*) FROM pg_publication_tables t WHERE t.pubname = p.pubname) "
        "FROM pg_publication p ORDER BY p.pubname;",
    )
    chosen = load()
    rows: List[Dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        name = parts[0]
        if name == CAPTURE_LOG_PUBLICATION and not include_internal:
            continue
        rows.append({
            "name": name,
            "all_tables": parts[1] == "t",
            "table_count": int(parts[2]) if parts[2].isdigit() else 0,
            "ours": chosen["chosen"] and chosen["name"] == name and chosen["ours"],
            "active": chosen["name"] == name if chosen["chosen"] else False,
        })
    return rows


def create(publisher_connstr: str, name: str) -> Dict:
    """Make a new, empty publication and record it as ours.

    Empty rather than FOR ALL TABLES: the table screen is where coverage is
    decided, and starting from everything would mean the first thing a new
    publication does is offer to replicate 333 GB.
    """
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            "publication name must start with a letter or underscore and contain "
            "only letters, digits and underscores"
        )
    exists = _run_publisher_sql(
        publisher_connstr,
        f"SELECT 1 FROM pg_publication WHERE pubname = '{name}';",
    ).strip()
    if exists:
        raise ValueError(f"a publication named {name} already exists on the primary")
    _run_publisher_sql(publisher_connstr, f'CREATE PUBLICATION "{name}";')
    return save(name, ours=True)
