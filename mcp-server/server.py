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
        "before calling, especially on a production deployment."
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
