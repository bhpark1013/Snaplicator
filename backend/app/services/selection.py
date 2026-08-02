"""What to replicate, expressed as a set and applied as a publication.

The UI hands over the tables that should be replicated. Turning that into SQL
is not an ALTER: a FOR ALL TABLES publication cannot have a table taken out of
it, and neither can a schema-level one — PostgreSQL has no syntax for a hole.
The only way to exclude anything from such a publication is to replace it, so
that is what this does, choosing the narrowest form that expresses the wish:

    every table, future ones too      FOR ALL TABLES
    whole schemas, future ones too    FOR TABLES IN SCHEMA a, b
    a fixed set                       FOR TABLE s.t1, s.t2
    a mix                             FOR TABLES IN SCHEMA a, TABLE s.t1

The form matters beyond tidiness. FOR TABLES IN SCHEMA is what makes new
tables appear on their own, in the server, with no event trigger to install
and no superuser needed at the moment someone runs CREATE TABLE. Where a
schema is kept whole, that is the form to use; where it is not, the price of
excluding one table is that the rest no longer follow automatically.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import policy, publication
from .replication import (
    _run_publisher_sql,
    _run_subscriber_sql,
    install_auto_add_trigger,
    uninstall_auto_add_trigger,
)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_fqn(fqn: str) -> str:
    schema, _, table = fqn.partition(".")
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def all_tables(publisher_connstr: str) -> List[str]:
    out = _run_publisher_sql(
        publisher_connstr,
        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema');",
    )
    return sorted(l.strip() for l in out.splitlines() if l.strip())


def ensure_publication(publisher_connstr: str, publication_name: str) -> Dict:
    """Create the publication if the primary has none, covering everything.

    The install used to do this, which meant the shape of the publication was
    settled by a script before anyone had seen a table name. It belongs here
    instead — at the copy, which is the moment the choice stops being
    reversible — and FOR ALL TABLES is only the default the UI already shows
    when there is nothing to inherit. Choosing anything narrower goes through
    apply_selection first, and this then finds a publication and leaves it be.
    """
    exists = _run_publisher_sql(
        publisher_connstr,
        f"SELECT 1 FROM pg_publication WHERE pubname = {_quote_literal(publication_name)};",
    ).strip()
    if exists:
        return {"created": False}
    _run_publisher_sql(
        publisher_connstr,
        f"CREATE PUBLICATION {_quote_ident(publication_name)} FOR ALL TABLES;",
    )
    # Recorded as ours because nothing carried this name a moment ago. The
    # record is what later lets apply_selection narrow it without asking again.
    publication.save(publication_name, ours=True)
    return {"created": True}


def current_selection(publisher_connstr: str, publication_name: str) -> Dict:
    """The publication as a set of tables, plus which schemas follow future ones."""
    exists = _run_publisher_sql(
        publisher_connstr,
        f"SELECT puballtables FROM pg_publication WHERE pubname = {_quote_literal(publication_name)};",
    ).strip()
    tables = all_tables(publisher_connstr)
    if not exists:
        return {"exists": False, "all_tables": False, "auto_schemas": [], "tables": [], "available": tables}

    published = _run_publisher_sql(
        publisher_connstr,
        "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
        f"WHERE pubname = {_quote_literal(publication_name)};",
    )
    published_set = sorted({l.strip() for l in published.splitlines() if l.strip()})

    # Schema-level membership is the absence of a pg_publication_rel row for a
    # table that is nonetheless published — the same distinction the table list
    # already draws, asked here per schema.
    individual = _run_publisher_sql(
        publisher_connstr,
        "SELECT n.nspname || '.' || c.relname FROM pg_publication_rel pr "
        "JOIN pg_class c ON c.oid = pr.prrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_publication p ON p.oid = pr.prpubid "
        f"WHERE p.pubname = {_quote_literal(publication_name)};",
    )
    individual_set = {l.strip() for l in individual.splitlines() if l.strip()}

    all_tables_flag = exists == "t"
    auto: Set[str] = set()
    if all_tables_flag:
        auto = {t.split(".")[0] for t in tables}
    else:
        for fqn in published_set:
            if fqn not in individual_set:
                auto.add(fqn.split(".")[0])

    # A schema followed by the trigger has ordinary table-level membership, so
    # the catalog cannot tell it apart from one nobody asked to follow. What
    # was asked for is the thing to report.
    chosen = policy.load()
    if chosen["chosen"]:
        auto |= set(chosen["auto_schemas"])

    return {
        "exists": True,
        "all_tables": all_tables_flag,
        "auto_schemas": sorted(auto),
        "tables": published_set,
        "available": tables,
    }


def _plan(
    wanted: Set[str],
    auto_schemas: Set[str],
    available: List[str],
) -> Tuple[str, List[str], List[str]]:
    """Pick the narrowest publication form for the wish. Returns (form, schemas, tables)."""
    by_schema: Dict[str, Set[str]] = {}
    for fqn in available:
        by_schema.setdefault(fqn.split(".")[0], set()).add(fqn)

    # A schema can only follow its future tables if none of its present ones
    # are being left out — the exclusion is what makes the automatic form
    # unavailable, not a preference.
    whole: Set[str] = {
        s for s in auto_schemas
        if s in by_schema and by_schema[s] <= wanted
    }
    if whole and whole == set(by_schema) and wanted == set(available):
        return "all", [], []

    explicit = sorted(t for t in wanted if t.split(".")[0] not in whole)
    return "mixed", sorted(whole), explicit


def apply_selection(
    publisher_connstr: str,
    publication_name: str,
    tables: Iterable[str],
    auto_schemas: Iterable[str],
    subscriber: Optional[Dict] = None,
) -> Dict:
    """Make the publication say exactly this. Recreates it when it has to."""
    available = all_tables(publisher_connstr)
    available_set = set(available)
    wanted = {t for t in tables if t in available_set}
    unknown = sorted(set(tables) - available_set)

    form, whole_schemas, explicit = _plan(wanted, set(auto_schemas), available)

    if form == "all":
        body = "FOR ALL TABLES"
    else:
        parts: List[str] = []
        if whole_schemas:
            parts.append("FOR TABLES IN SCHEMA " + ", ".join(_quote_ident(s) for s in whole_schemas))
        if explicit:
            keyword = "TABLE" if parts else "FOR TABLE"
            parts.append(f"{keyword} " + ", ".join(_quote_fqn(t) for t in explicit))
        if not parts:
            # Replicating nothing is a legal wish and an empty publication is
            # how PostgreSQL spells it. Anything else would be inventing a
            # meaning for "none".
            body = ""
        else:
            body = ", ".join(parts) if len(parts) > 1 else parts[0]

    # A publication that already exists and was not created here belongs to
    # someone else until a person says otherwise. Narrowing it is a DROP, so
    # the cost of guessing wrong is not a misconfigured replica — it is another
    # team's replica losing its publication mid-stream.
    if not publication.may_rewrite(publication_name):
        existing = _run_publisher_sql(
            publisher_connstr,
            f"SELECT 1 FROM pg_publication WHERE pubname = {_quote_literal(publication_name)};",
        ).strip()
        if existing:
            raise PermissionError(
                f"the publication {publication_name} already exists on the primary and "
                "was not created here. Choose it explicitly to take it over, or create "
                "a new one to narrow instead."
            )
        # Ours from here: nothing had this name before this call.
        publication.save(publication_name, ours=True)

    pub = _quote_ident(publication_name)
    # DROP then CREATE, in one statement to the server, because there is no
    # ALTER that gets from FOR ALL TABLES to anything narrower. A subscription
    # pointing at it survives the gap — it errors while the publication is
    # missing and resumes on REFRESH — but the gap is why this is a single
    # round trip rather than two.
    sql = f"DROP PUBLICATION IF EXISTS {pub}; CREATE PUBLICATION {pub}"
    if body:
        sql += " " + body
    sql += ";"
    _run_publisher_sql(publisher_connstr, sql)

    # Schemas that were asked to follow new tables but could not be given
    # schema-level membership — because something in them is excluded — are
    # exactly the ones an event trigger has to cover. Where the publication
    # itself follows the schema, the trigger would only get in the way.
    wanted_auto = set(auto_schemas)
    trigger_schemas = sorted(wanted_auto - set(whole_schemas))
    excluded = sorted(available_set - wanted)
    policy.save(sorted(wanted_auto), excluded)
    try:
        if form == "all" or not trigger_schemas:
            uninstall_auto_add_trigger(publisher_connstr)
        else:
            install_auto_add_trigger(
                publisher_connstr, publication_name, trigger_schemas, excluded
            )
    except Exception:
        # The publication is already what was asked for; the trigger is the
        # part that keeps it that way tomorrow, and the loop reinstates it.
        pass

    refreshed = False
    if subscriber and subscriber.get("subscription"):
        try:
            _run_subscriber_sql(
                subscriber["container"],
                subscriber["user"],
                subscriber.get("password"),
                subscriber["db"],
                f"ALTER SUBSCRIPTION {_quote_ident(subscriber['subscription'])} REFRESH PUBLICATION;",
            )
            refreshed = True
        except Exception:
            # A subscription that is not there yet is the ordinary case now:
            # this runs before the first copy more often than after it.
            refreshed = False

    return {
        "form": "FOR ALL TABLES" if form == "all" else (body or "empty"),
        "auto_schemas": whole_schemas,
        "trigger_schemas": trigger_schemas,
        "tables": explicit,
        "count": len(wanted),
        "unknown": unknown,
        "subscription_refreshed": refreshed,
    }
