"""Snaplicator MCP Server - exposes Snaplicator REST API as MCP tools."""
import os
import json
import httpx
from mcp.server.fastmcp import Context, FastMCP

BASE_URL = os.environ.get("SNAPLICATOR_URL", "http://localhost:8888")

# Mutating clone tools may only target clones on this connection's allowlist.
# Resolution order, per request:
#   1. the `?clones=...` query string on the MCP endpoint URL the client registered
#      (e.g. http://host:8765/mcp?clones=5435 ; comma-separated)
#   2. fallback env var SNAPLICATOR_MUTABLE_CLONES (server-wide default)
#   3. neither set -> unrestricted (legacy behavior)
# Each entry matches a clone by host port, subvolume name, or container name.
# NOTE: the query value is client-declared, so this is an accident guardrail,
# not a hard security boundary against an untrusted tailnet client.
_ENV_MUTABLE_CLONES = {
    s.strip() for s in os.environ.get("SNAPLICATOR_MUTABLE_CLONES", "").split(",") if s.strip()
}


def _allowed_clones(ctx: Context) -> set[str]:
    """Allowlist of clone identifiers this request may mutate (empty = unrestricted)."""
    raw = None
    try:
        request = ctx.request_context.request
        if request is not None:
            raw = request.query_params.get("clones")
    except Exception:
        raw = None
    if raw:
        return {s.strip() for s in raw.split(",") if s.strip()}
    return set(_ENV_MUTABLE_CLONES)


def _assert_clone_mutable(ctx: Context, clone: dict) -> None:
    """Reject a mutating operation when the clone is not on the allowlist."""
    allow = _allowed_clones(ctx)
    if not allow:
        return  # unrestricted
    keys = {clone.get("name"), clone.get("container_name"), str(clone.get("host_port"))}
    if keys.isdisjoint(allow):
        label = clone.get("display_name") or clone.get("container_name") or clone.get("name")
        raise ValueError(
            f"clone '{label}' (port {clone.get('host_port')}) is not in this MCP "
            f"connection's mutable allowlist ({', '.join(sorted(allow))}). "
            f"Refusing to mutate it."
        )

mcp = FastMCP(
    "snaplicator",
    instructions=(
        "Snaplicator manages PostgreSQL replicas, clones, and snapshots. "
        "Read-only tools (list_*, get_*, run_replication_check) are safe to call freely. "
        "Tools whose description starts with ⚠️ mutate real state: clone/snapshot create "
        "and delete, reset/refresh (which discard data), and publication/trigger changes that "
        "act on the upstream PRIMARY database. Confirm any " "⚠️" " operation with the user "
        "before calling, especially on a production deployment.\n\n"
        "Four of them deserve more than the usual confirmation, because what they "
        "break is not the thing you are pointing at:\n"
        "  set_anonymize_sql — unvalidated SQL that then runs against real production "
        "data on every future clone; shortening it silently stops masking.\n"
        "  set_replication_selection / choose_publication — narrowing a publication "
        "DROPs and recreates it on the live primary, which can break a replica "
        "belonging to someone else.\n"
        "  start_bootstrap — hours of initial copy, and force=true discards the "
        "replica's current contents.\n"
        "Every set_*/put-style tool takes the COMPLETE new value, not a delta. Read "
        "the matching get_* first and send back the full text or list."
    ),
)


def _get(path: str) -> dict:
    r = httpx.get(f"{BASE_URL}{path}", timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None, timeout: int = 60) -> dict:
    r = httpx.post(f"{BASE_URL}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _put(path: str, body: dict | None = None, timeout: int = 60) -> dict:
    r = httpx.put(f"{BASE_URL}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _delete(path: str, body: dict | None = None) -> dict:
    # httpx.delete() does not accept a json body; use request() directly
    r = httpx.request("DELETE", f"{BASE_URL}{path}", json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def _extract_port(identifier: str) -> int | None:
    """Extract a port from a connection-URL-ish identifier, else None.

    Accepts:
      - postgresql://user:pass@host:5435/db (also postgres://, with/without
        credentials, db name, query params)
      - psql DSN style: "host=... port=5435 ..."
    """
    ident = identifier.strip()
    if "://" in ident:
        from urllib.parse import urlsplit
        try:
            return urlsplit(ident).port
        except ValueError:
            return None
    if "port=" in ident:  # key=value DSN
        for token in ident.split():
            if token.startswith("port="):
                value = token.split("=", 1)[1]
                return int(value) if value.isdigit() else None
        return None
    return None


def _resolve_clone(identifier: str | int) -> dict:
    """Resolve a clone by subvolume name, container name, host port,
    or connection URL/DSN (postgresql://user:pass@host:port/db).

    The backend API only matches subvolume/container names, so port lookup
    is resolved here against the clone list. Returns the clone record.
    Raises ValueError if not found or ambiguous.
    """
    clones = _get("/clones")
    ident = str(identifier).strip()
    url_port = None if ident.isdigit() else _extract_port(ident)
    # Port lookup (purely numeric identifier, or port taken from a URL/DSN)
    if ident.isdigit() or url_port is not None:
        port = int(ident) if ident.isdigit() else url_port
        matches = [c for c in clones if c.get("host_port") == port]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = [c.get("container_name") or c.get("name") for c in matches]
            raise ValueError(f"Port {port} matches multiple clones: {names}")
        raise ValueError(f"No clone found listening on port {port}")
    # Name lookup (same rule as the backend: subvolume name or container name)
    for c in clones:
        if ident in (c.get("name"), c.get("container_name")):
            return c
    raise ValueError(f"No clone found matching '{ident}' (tried subvolume name, container name)")


# ── Health ──

@mcp.tool()
def health() -> str:
    """Check if the Snaplicator server is running."""
    return json.dumps(_get("/health"))


# ── Clones ──

@mcp.tool()
def list_clones() -> str:
    """List all database clones with their container status, ports, and metadata."""
    return json.dumps(_get("/clones"), ensure_ascii=False)


@mcp.tool()
def create_clone(description: str, port: int | None = None, username: str | None = None, password: str | None = None) -> str:
    """Create a new database clone from the main replica.

    ⚠️ Mutates state: launches a new container and consumes disk.

    Args:
        description: What this clone is for (e.g. "feature-xyz testing")
        port: Optional host port. Auto-assigned if not specified.
        username: Optional extra DB login to create in the clone. Must be given
            together with password. If omitted, connect with the default
            account (snaplicator on prod).
        password: Password for the extra DB login (required when username is set).
    """
    body = {"description": description}
    if port is not None:
        body["port"] = port
    if username:
        body["username"] = username
        body["password"] = password
    return json.dumps(_post("/clones", body), ensure_ascii=False)


@mcp.tool()
def get_clone_detail(clone_id: str) -> str:
    """Get detailed info about a specific clone.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
    """
    clone = _resolve_clone(clone_id)
    return json.dumps(_get(f"/clones/{clone['name']}"), ensure_ascii=False)


@mcp.tool()
def delete_clone(ctx: Context, clone_id: str) -> str:
    """Delete a clone and its container.

    ⚠️ Destructive & irreversible: removes the container AND its btrfs subvolume (all clone data). Confirm with the user before running.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
    """
    clone = _resolve_clone(clone_id)
    _assert_clone_mutable(ctx, clone)
    target = clone.get("container_name") or clone["name"]
    return json.dumps(_delete(f"/clones/{target}"), ensure_ascii=False)


@mcp.tool()
def refresh_clone(ctx: Context, clone_id: str, description: str | None = None) -> str:
    """Refresh a clone with the latest data from main.

    ⚠️ Destructive: replaces the clone's current data with the latest from main. Confirm with the user before running.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        description: Optional new description
    """
    clone = _resolve_clone(clone_id)
    _assert_clone_mutable(ctx, clone)
    target = clone.get("container_name") or clone["name"]
    body = {"description": description} if description else None
    return json.dumps(_post(f"/clones/{target}/refresh", body, timeout=180), ensure_ascii=False)


@mcp.tool()
def reset_clone_to_snapshot(ctx: Context, clone_id: str, snapshot_name: str) -> str:
    """Reset (switch) an existing clone to a snapshot's state, keeping its port.

    ⚠️ Destructive: discards the clone's current data and recreates its container on the snapshot. Confirm with the user before running.

    The clone's container is recreated on top of the snapshot data. Works with
    both main snapshots and clone snapshots (any snapshot under the data root).
    The clone keeps its existing name/description.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        snapshot_name: Snapshot directory name to reset the clone to
    """
    clone = _resolve_clone(clone_id)
    _assert_clone_mutable(ctx, clone)
    body: dict = {"snapshot_name": snapshot_name}
    return json.dumps(_post(f"/clones/{clone['name']}/reset", body, timeout=180), ensure_ascii=False)


@mcp.tool()
def create_clone_snapshot(
    ctx: Context,
    clone_id: str,
    description: str | None = None,
    previous_snapshot: str | None = None,
    insert_before: str | None = None,
    retention_days: int = 14,
) -> str:
    """Create a snapshot of a specific clone's current state (not the main replica).

    ⚠️ Mutates state: creates a new snapshot subvolume.

    Useful before risky operations on a clone; restore later with reset_clone_to_snapshot.

    Lineage (previous_snapshot / insert_before) is display-only ordering for the
    snapshot graph — it does not touch the snapshot data. To splice the new
    snapshot between two existing ones A -> B, pass previous_snapshot="A" and
    insert_before="B" (B is then re-linked to follow the new snapshot).

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        description: What this snapshot captures (e.g. "before migration test")
        previous_snapshot: Name of the snapshot this one should follow (the "before" link). Omit to start a new chain.
        insert_before: Name of an existing snapshot to insert in front of (the "after" link); that snapshot is re-pointed to follow the new one.
        retention_days: Days to keep before it is eligible for cleanup; 0 = keep forever. Default 14.
    """
    clone = _resolve_clone(clone_id)
    _assert_clone_mutable(ctx, clone)
    body: dict = {"retention_days": retention_days}
    if description:
        body["description"] = description
    if previous_snapshot:
        body["previous_snapshot"] = previous_snapshot
    if insert_before:
        body["insert_before"] = insert_before
    return json.dumps(_post(f"/clones/{clone['name']}/snapshots", body), ensure_ascii=False)


@mcp.tool()
def list_clone_snapshots(clone_id: str) -> str:
    """List snapshots taken from a specific clone.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
    """
    clone = _resolve_clone(clone_id)
    return json.dumps(_get(f"/clones/{clone['name']}/snapshots"), ensure_ascii=False)


@mcp.tool()
def get_clone_usage(clone_id: str) -> str:
    """Get disk usage for a specific clone.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
    """
    clone = _resolve_clone(clone_id)
    return json.dumps(_get(f"/clones/{clone['name']}/usage"), ensure_ascii=False)


@mcp.tool()
def get_filesystem_usage() -> str:
    """Get overall filesystem usage summary for the data directory."""
    return json.dumps(_get("/clones/usage/fs"), ensure_ascii=False)


@mcp.tool()
def get_clone_create_progress() -> str:
    """Where the clone currently being built has got to.

    create_clone / clone_from_snapshot do not return until the clone is serving,
    so this is how progress is read: call it from a separate turn while the
    create is still open. `active` distinguishes a run in flight from the last
    finished one. Stages: checkpoint → snapshot → permissions → container →
    ready → subscriptions → sequences → anonymize → user.
    """
    return json.dumps(_get("/clones/create-progress"), ensure_ascii=False)


@mcp.tool()
def set_clone_description(ctx: Context, clone_id: str, name: str | None = None,
                          description: str | None = None) -> str:
    """Rename a clone or change its description.

    ⚠️ Mutates state, but metadata only: no database content is touched and the
    change is reversible. Subject to the mutable-clone allowlist like any other
    clone write.

    Args:
        clone_id: Clone identifier (subvolume name, container name, port, or connection URL)
        name: New display name, or None to leave it
        description: New description, or None to leave it
    """
    clone = _resolve_clone(clone_id)
    _assert_clone_mutable(ctx, clone)
    body = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if not body:
        raise ValueError("Provide at least one of `name` / `description`")
    return json.dumps(_post(f"/clones/{clone['name']}/description", body), ensure_ascii=False)


# ── Anonymization ──
#
# The script runs on every clone made from the live main replica. Clones made
# from a snapshot skip it — the backend assumes a clone_snapshot is already
# anonymized — so reading this does not by itself tell you whether a given
# clone was scrubbed. get_clone_detail says which snapshot it came from.

@mcp.tool()
def get_anonymize_sql() -> str:
    """The anonymization script that runs on clones made from the main replica.

    `configured` false means there is no script: clones are handed out as plain
    copies of production. That is a legitimate setup and a bad accident, which
    is why it is reported rather than assumed.
    """
    return json.dumps(_get("/clones/anonymize-sql"), ensure_ascii=False)


@mcp.tool()
def set_anonymize_sql(sql: str) -> str:
    """Replace the anonymization script.

    ⚠️ Dangerous, and in two directions. The text is saved verbatim with NO
    validation and NO dry run, then executed against real production data on
    every clone built from the main replica after this. Bad SQL fails the whole
    clone build; a shortened script silently stops masking whatever it dropped.
    A full overwrite, not a patch — read get_anonymize_sql first and send back
    the complete text. Always confirm the diff with the user.

    Args:
        sql: The complete new script. An empty string is rejected by the server.
    """
    return json.dumps(_put("/clones/anonymize-sql", {"sql": sql}), ensure_ascii=False)


# ── Notifications ──

@mcp.tool()
def get_notification_config() -> str:
    """Webhook notification settings (URL is returned redacted)."""
    return json.dumps(_get("/notifications"), ensure_ascii=False)


@mcp.tool()
def set_notification_config(webhook_url: str | None = None, enabled: bool | None = None) -> str:
    """Change webhook notification settings.

    ⚠️ Mutates state: sets where Snaplicator sends alerts. Passing enabled=false
    silences failure notifications for everyone using this install.

    Args:
        webhook_url: New webhook URL, or None to leave it
        enabled: Turn notifications on/off, or None to leave it
    """
    body: dict = {}
    if webhook_url is not None:
        body["webhook_url"] = webhook_url
    if enabled is not None:
        body["enabled"] = enabled
    if not body:
        raise ValueError("Provide at least one of `webhook_url` / `enabled`")
    return json.dumps(_put("/notifications", body), ensure_ascii=False)


@mcp.tool()
def send_test_notification() -> str:
    """Send a test message to the configured webhook.

    ⚠️ Sends a real message to a real channel. Harmless but visible to people.
    """
    return json.dumps(_post("/notifications/test"), ensure_ascii=False)


# ── Snapshots ──

@mcp.tool()
def list_snapshots() -> str:
    """List all snapshots of the main replica."""
    return json.dumps(_get("/snapshots"), ensure_ascii=False)


@mcp.tool()
def create_snapshot(
    description: str,
    previous_snapshot: str | None = None,
    insert_before: str | None = None,
    retention_days: int = 14,
) -> str:
    """Create a snapshot of the current main replica state.

    ⚠️ Mutates state: creates a new btrfs snapshot of main.

    Lineage (previous_snapshot / insert_before) is display-only ordering for the
    snapshot graph — it does not touch the snapshot data. To splice the new
    snapshot between two existing ones A -> B, pass previous_snapshot="A" and
    insert_before="B" (B is then re-linked to follow the new snapshot).

    Args:
        description: What this snapshot captures (e.g. "before migration")
        previous_snapshot: Name of the snapshot this one should follow (the "before" link). Omit to start a new chain.
        insert_before: Name of an existing snapshot to insert in front of (the "after" link); that snapshot is re-pointed to follow the new one.
        retention_days: Days to keep before it is eligible for cleanup; 0 = keep forever. Default 14.
    """
    body: dict = {"description": description, "retention_days": retention_days}
    if previous_snapshot:
        body["previous_snapshot"] = previous_snapshot
    if insert_before:
        body["insert_before"] = insert_before
    return json.dumps(_post("/snapshots", body), ensure_ascii=False)


@mcp.tool()
def delete_snapshot(snapshot_name: str) -> str:
    """Delete a snapshot.

    ⚠️ Destructive & irreversible: deletes the snapshot subvolume. Confirm with the user before running.

    Args:
        snapshot_name: Snapshot directory name
    """
    return json.dumps(_delete(f"/snapshots/{snapshot_name}"), ensure_ascii=False)


@mcp.tool()
def set_snapshot_retention(snapshot_name: str, retention_days: int = 14) -> str:
    """Change how long a snapshot is kept before it is eligible for cleanup.

    ⚠️ Mutates state: rewrites this snapshot's retention metadata (no PG data is
    touched). expires_at is recomputed from the snapshot's original creation time.

    Args:
        snapshot_name: Snapshot directory name
        retention_days: Days to keep from creation; 0 = keep forever.
    """
    return json.dumps(
        _post(f"/snapshots/{snapshot_name}/retention", {"retention_days": retention_days}),
        ensure_ascii=False,
    )


@mcp.tool()
def set_snapshot_previous(snapshot_name: str, previous_snapshot: str | None = None) -> str:
    """Re-link a single snapshot's predecessor in the lineage graph.

    ⚠️ Mutates state: rewrites this snapshot's `previous_snapshot` pointer. Lineage
    is display-only ordering for the snapshot graph — NO snapshot data is touched.

    Pass previous_snapshot=None to detach the snapshot into a root. This changes only
    THIS snapshot's link; it does NOT reconnect its followers (use move_snapshot for a
    drag-style move that heals the gap).

    Args:
        snapshot_name: Snapshot directory name to re-link
        previous_snapshot: Name of the snapshot it should follow, or None to make it a root
    """
    return json.dumps(
        _post(f"/snapshots/{snapshot_name}/lineage", {"previous_snapshot": previous_snapshot}),
        ensure_ascii=False,
    )


@mcp.tool()
def move_snapshot(snapshot_name: str, after: str | None = None, before: str | None = None) -> str:
    """Move a snapshot to a new position in the lineage graph (like dragging it).

    ⚠️ Mutates state: rewrites previous_snapshot links. Lineage is display-only
    ordering for the snapshot graph — NO snapshot data is touched.

    Provide exactly one of `after` / `before`, or neither to detach into a root:
      - after=X  → place the snapshot immediately after X (as X's follower / a branch off X)
      - before=Y → insert the snapshot immediately before Y (Y is re-linked to follow it)
      - neither  → detach into a new root

    The snapshot's existing followers are reconnected to its old predecessor so the
    chain stays intact (the same heal+splice the UI does on drag). The final graph is
    validated acyclic server-side; an invalid move is rejected.

    Args:
        snapshot_name: Snapshot to move
        after: Snapshot to place this one immediately after
        before: Snapshot to insert this one immediately before
    """
    if after and before:
        raise ValueError("Provide only one of `after` / `before` (or neither for a root)")
    if snapshot_name in (after, before):
        raise ValueError("`after` / `before` cannot be the snapshot itself")

    snaps = _get("/snapshots")
    prev = {s["name"]: ((s.get("metadata") or {}).get("previous_snapshot") or None) for s in snaps}
    if snapshot_name not in prev:
        raise ValueError(f"Snapshot not found: {snapshot_name}")
    for target in (after, before):
        if target and target not in prev:
            raise ValueError(f"Target snapshot not found: {target}")

    old_prev = prev.get(snapshot_name)
    updates: dict[str, str | None] = {}
    # heal: followers of the moved node reconnect to its old predecessor
    for name, p in prev.items():
        if p == snapshot_name:
            updates[name] = old_prev
    # splice into the new position
    if after:
        updates[snapshot_name] = after
    elif before:
        updates[snapshot_name] = prev.get(before)
        updates[before] = snapshot_name
    else:
        updates[snapshot_name] = None

    payload = {"updates": [{"snapshot": k, "previous_snapshot": v} for k, v in updates.items()]}
    return json.dumps(_post("/snapshots/lineage/batch", payload), ensure_ascii=False)


@mcp.tool()
def clone_from_snapshot(snapshot_name: str, description: str | None = None) -> str:
    """Create a new clone from a specific snapshot.

    ⚠️ Mutates state: launches a new container and consumes disk.

    Args:
        snapshot_name: Snapshot to clone from
        description: Optional description for the new clone
    """
    body = {"description": description} if description else None
    return json.dumps(_post(f"/snapshots/{snapshot_name}/clone", body, timeout=180), ensure_ascii=False)


# ── Replication ──

@mcp.tool()
def get_replication_lag() -> str:
    """Get current replication lag between publisher and subscriber in seconds."""
    return json.dumps(_get("/replication/lag"))


@mcp.tool()
def get_replication_status() -> str:
    """Get subscription worker status (running, LSN positions, last sync time)."""
    return json.dumps(_get("/replication/subscription-status"))


@mcp.tool()
def list_replication_tables() -> str:
    """List all tables with their publication and subscriber status."""
    return json.dumps(_get("/replication/tables"), ensure_ascii=False)


@mcp.tool()
def add_tables_to_replication(tables: list[str], refresh: bool = False) -> str:
    """Add tables to the publication for replication.

    ⚠️ Acts on the publisher (the upstream PRIMARY database). On a production deployment this ALTERs the live primary's publication — confirm with the user first.

    Args:
        tables: List of table names to add
        refresh: Whether to refresh the subscription after adding
    """
    return json.dumps(_post("/replication/tables", {"tables": tables, "refresh": refresh}), ensure_ascii=False)


@mcp.tool()
def remove_tables_from_replication(tables: list[str], refresh: bool = False) -> str:
    """Remove tables from the publication.

    ⚠️ Acts on the publisher (the upstream PRIMARY database) and changes what is replicated. On a production deployment this ALTERs the live primary — confirm with the user first.

    Args:
        tables: List of table names to remove
        refresh: Whether to refresh the subscription after removing
    """
    return json.dumps(_delete("/replication/tables", {"tables": tables, "refresh": refresh}), ensure_ascii=False)


@mcp.tool()
def refresh_subscription() -> str:
    """Refresh the subscription to pick up publication changes.

    ⚠️ Mutates replication: refreshes the subscriber's subscription.
    """
    return json.dumps(_post("/replication/refresh"))


@mcp.tool()
def get_replication_logs(tail: int = 500) -> str:
    """Get replication-related log lines from the replica container.

    Args:
        tail: Number of recent log lines to fetch (max 5000)
    """
    return json.dumps(_get(f"/replication/logs?tail={tail}"), ensure_ascii=False)


@mcp.tool()
def run_replication_check() -> str:
    """Run replication consistency check on both publisher and subscriber."""
    return json.dumps(_get("/replication/check"), ensure_ascii=False)


@mcp.tool()
def get_replication_info() -> str:
    """Get publisher and subscriber connection info."""
    return json.dumps(_get("/replication/info"), ensure_ascii=False)


@mcp.tool()
def get_trigger_status() -> str:
    """Check if the auto-add event trigger is installed on the publisher."""
    return json.dumps(_get("/replication/trigger-status"))


@mcp.tool()
def install_trigger() -> str:
    """Install or update the auto-add event trigger on the publisher.

    ⚠️ Acts on the publisher (the upstream PRIMARY database): runs DDL to install/replace an event trigger. On a production deployment this changes the live primary — confirm with the user first.
    """
    return json.dumps(_post("/replication/trigger-install"))


@mcp.tool()
def get_copy_progress() -> str:
    """Get initial data copy progress (for new subscriptions)."""
    return json.dumps(_get("/replication/copy-progress"))


# ── Replication check SQL ──

@mcp.tool()
def get_replication_check_sql() -> str:
    """The SQL run_replication_check executes on both ends.

    `configured` false means the query is still the shipped example, which
    references tables that do not exist here — so the check always errors and
    always reads as broken replication rather than as an unanswered question.
    """
    return json.dumps(_get("/replication/check-sql"), ensure_ascii=False)


@mcp.tool()
def set_replication_check_sql(sql: str) -> str:
    """Replace the replication-check SQL.

    ⚠️ Mutates state, but the server refuses anything not provably read-only
    (400) and additionally runs it in a READ ONLY transaction. A full overwrite,
    not a patch.

    Args:
        sql: The complete new query. Read-only statements only.
    """
    return json.dumps(_put("/replication/check-sql", {"sql": sql}), ensure_ascii=False)


# ── Publication and table selection ──

@mcp.tool()
def list_publications() -> str:
    """Publications on the primary, which one this replica speaks for, and
    whether this install may rewrite it (`ours`)."""
    return json.dumps(_get("/replication/publications"), ensure_ascii=False)


@mcp.tool()
def choose_publication(name: str, mode: str) -> str:
    """Say which publication this replica speaks for.

    ⚠️ Acts on the publisher (the upstream PRIMARY database). `create` runs
    CREATE PUBLICATION on the live primary. `adopt` claims the right to rewrite
    a publication that may predate this install and may be feeding a replica
    that has nothing to do with us — narrowing one drops and recreates it, which
    would break that other replica. Confirm with the user first.

    Args:
        name: Publication name
        mode: create (new, empty, ours to narrow) | reuse (read as it stands,
              never rewritten) | adopt (existing, taken over — rewritable)
    """
    if mode not in ("create", "reuse", "adopt"):
        raise ValueError("mode must be one of: create, reuse, adopt")
    return json.dumps(_put("/replication/publication", {"name": name, "mode": mode}), ensure_ascii=False)


@mcp.tool()
def get_replication_selection() -> str:
    """The publication as a set of tables, plus which schemas future tables
    join on their own."""
    return json.dumps(_get("/replication/selection"), ensure_ascii=False)


@mcp.tool()
def set_replication_selection(tables: list[str], auto_schemas: list[str] | None = None) -> str:
    """Make the publication contain exactly these tables.

    ⚠️ Acts on the publisher (the upstream PRIMARY database) and is destructive
    to replication. A FOR ALL TABLES publication cannot have a table removed, so
    the first exclusion DROPs and recreates the publication — any other
    subscriber reading it is affected. Tables dropped from the selection stop
    replicating and go stale on the replica. Returns 409 if this install does
    not own the publication. Send the COMPLETE desired set, not a delta.

    Args:
        tables: Full list of schema.table to replicate
        auto_schemas: Schemas whose future tables should join by themselves
    """
    body = {"tables": tables, "auto_schemas": auto_schemas or []}
    return json.dumps(_put("/replication/selection", body, timeout=180), ensure_ascii=False)


# ── Bootstrap (initial schema clone + subscription) ──

@mcp.tool()
def get_bootstrap_status(tail: int = 40) -> str:
    """Whether the replica has been brought up, is being brought up, or neither.

    Args:
        tail: Recent log lines to include (max 2000)
    """
    return json.dumps(_get(f"/replication/bootstrap?tail={tail}"), ensure_ascii=False)


@mcp.tool()
def start_bootstrap(force: bool = False) -> str:
    """Clone the schema from the primary and create the subscription.

    ⚠️ The heaviest operation here. Reads the primary's whole schema and starts
    an initial data copy measured in minutes to hours; force=true restarts one
    that already exists, discarding the replica's current contents. Returns
    immediately — the run continues on its own; poll get_bootstrap_status.
    409 means it is already running or already subscribed.

    Args:
        force: Re-run even if the replica is already bootstrapped
    """
    return json.dumps(_post(f"/replication/bootstrap?force={str(force).lower()}"), ensure_ascii=False)


@mcp.tool()
def cancel_bootstrap() -> str:
    """Stop a running bootstrap.

    ⚠️ Leaves whatever it managed to create — a half-built replica, not a clean
    slate. 409 if nothing is running.
    """
    return json.dumps(_delete("/replication/bootstrap"), ensure_ascii=False)


# ── Fidelity and capacity: is this replica actually a copy? ──

@mcp.tool()
def get_capacity() -> str:
    """Will the current selection fit in the pool?

    Two separate answers: `fits` is a fact about this disk today and is what the
    copy refuses over; `comfortable` is a forecast about the snapshots and
    clones that come later, and is only ever advisory.
    """
    return json.dumps(_get("/replication/capacity"), ensure_ascii=False)


@mcp.tool()
def get_schema_drift() -> str:
    """Where the replica's shape no longer matches what the primary publishes.

    Read-only by design: by the time a difference is detectable the DDL that
    would close it is gone, and inventing one is how a diagnostic becomes an
    outage.
    """
    return json.dumps(_get("/replication/schema-drift"), ensure_ascii=False)


@mcp.tool()
def get_schema_errors(limit: int = 200) -> str:
    """Objects the initial schema clone could not create.

    `recorded: false` means the clone reported nothing or predates this record —
    absence of evidence, not a clean bill of health.

    Args:
        limit: Maximum errors to return (1-2000)
    """
    return json.dumps(_get(f"/replication/schema-errors?limit={limit}"), ensure_ascii=False)


@mcp.tool()
def get_extension_parity() -> str:
    """The primary's extensions against what this replica can offer.

    Two different failures with two different fixes: `not installed` (the files
    are there, CREATE EXTENSION was never run) versus `not available` (the
    binary is not in the image, so no SQL can fix it — only a different
    POSTGRES_IMAGE can).
    """
    return json.dumps(_get("/replication/extensions"), ensure_ascii=False)


@mcp.tool()
def get_sync_log(limit: int = 100) -> str:
    """Recent auto-sync activity: new tables, column and constraint adds,
    schema moves, FDW drift re-imports, errors.

    Args:
        limit: Maximum events to return
    """
    return json.dumps(_get(f"/replication/sync-log?limit={limit}"), ensure_ascii=False)


# ── FDW: tables read live from the primary instead of replicated ──

@mcp.tool()
def get_fdw_state() -> str:
    """The FDW yaml config plus the foreign tables actually present on the replica."""
    return json.dumps(_get("/replication/fdw"), ensure_ascii=False)


@mcp.tool()
def set_fdw_credentials(user: str, password: str, host: str | None = None,
                        port: int | None = None, dbname: str | None = None) -> str:
    """Set the login foreign tables are read as, and build the FDW server with it.

    ⚠️ Stores a credential for the upstream PRIMARY database and connects to it
    to verify. Leave host/port/dbname empty for wherever replication already
    connects — a bastion or a pooler is the reason to differ.

    Args:
        user: Primary database username
        password: Its password
        host: Override host, or None for the replication host
        port: Override port, or None
        dbname: Override database, or None
    """
    body: dict = {"user": user, "password": password}
    if host is not None:
        body["host"] = host
    if port is not None:
        body["port"] = port
    if dbname is not None:
        body["dbname"] = dbname
    return json.dumps(_put("/replication/fdw/credentials", body), ensure_ascii=False)


@mcp.tool()
def clear_fdw_credentials() -> str:
    """Forget the FDW login.

    ⚠️ Every foreign table stops being readable until a credential is set again.
    """
    return json.dumps(_delete("/replication/fdw/credentials"), ensure_ascii=False)


@mcp.tool()
def add_fdw_tables(tables: list[dict]) -> str:
    """Expose primary tables on the replica as foreign tables.

    ⚠️ Runs IMPORT FOREIGN SCHEMA against the PRIMARY and rewrites the FDW
    config. Reads of these tables hit the live primary, so they add load to
    production. Rejected (400) if a requested table is already replicated
    through the publication — a table cannot be both.

    Args:
        tables: [{"schema": "public", "name": "orders"}, ...]
    """
    return json.dumps(_post("/replication/fdw/tables", {"tables": tables}, timeout=180), ensure_ascii=False)


@mcp.tool()
def remove_fdw_tables(tables: list[dict]) -> str:
    """Remove foreign tables. Both the yaml entries and the tables go.

    ⚠️ Mutates state: anything querying these tables on the replica breaks.

    Args:
        tables: [{"schema": "public", "name": "orders"}, ...]
    """
    return json.dumps(_delete("/replication/fdw/tables", {"tables": tables}), ensure_ascii=False)


@mcp.tool()
def add_fdw_schemas(schemas: list[str]) -> str:
    """Expose whole primary schemas as foreign tables.

    ⚠️ Same as add_fdw_tables but for every table in the schema, current and
    imported at once. Runs IMPORT FOREIGN SCHEMA against the PRIMARY.

    Args:
        schemas: Schema names
    """
    return json.dumps(_post("/replication/fdw/schemas", {"schemas": schemas}, timeout=180), ensure_ascii=False)


@mcp.tool()
def remove_fdw_schemas(schemas: list[str]) -> str:
    """Remove whole schemas from FDW.

    ⚠️ Mutates state: every foreign table in these schemas disappears.

    Args:
        schemas: Schema names
    """
    return json.dumps(_delete("/replication/fdw/schemas", {"schemas": schemas}), ensure_ascii=False)


@mcp.tool()
def regenerate_fdw() -> str:
    """Re-render the generated FDW SQL from the yaml and re-apply it to the replica.

    ⚠️ Rebuilds every foreign table from the config. Use after a manual yaml
    edit or to recover from drift; 400 if the config does not validate against
    the current publication.
    """
    return json.dumps(_post("/replication/fdw/regenerate", timeout=300), ensure_ascii=False)


if __name__ == "__main__":
    # MCP_TRANSPORT=streamable-http serves HTTP at http://MCP_HOST:MCP_PORT/mcp
    # (bind MCP_HOST to the Tailscale IP so the endpoint is tailnet-only).
    # Default stdio keeps `claude mcp add -- ssh ...` setups working.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        from mcp.server.transport_security import TransportSecuritySettings

        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8765"))
        mcp.settings.host = host
        mcp.settings.port = port
        # SDK's DNS-rebinding protection only allows localhost Host headers by
        # default; clients reach us via the Tailscale IP, so allow that too.
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=[f"{host}:{port}", host, f"localhost:{port}", f"127.0.0.1:{port}"],
        )
    mcp.run(transport=transport)
