"""Snaplicator MCP Server - exposes Snaplicator REST API as MCP tools."""
import os
import json
import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("SNAPLICATOR_URL", "http://localhost:8888")

mcp = FastMCP(
    "snaplicator",
    instructions="Snaplicator manages PostgreSQL replicas, clones, and snapshots. Use these tools to manage database clones for development/testing.",
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
def create_clone(description: str, port: int | None = None) -> str:
    """Create a new database clone from the main replica.

    Args:
        description: What this clone is for (e.g. "feature-xyz testing")
        port: Optional host port. Auto-assigned if not specified.
    """
    body = {"description": description}
    if port is not None:
        body["port"] = port
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
def delete_clone(clone_id: str) -> str:
    """Delete a clone and its container.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
    """
    clone = _resolve_clone(clone_id)
    target = clone.get("container_name") or clone["name"]
    return json.dumps(_delete(f"/clones/{target}"), ensure_ascii=False)


@mcp.tool()
def refresh_clone(clone_id: str, description: str | None = None) -> str:
    """Refresh a clone with the latest data from main.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        description: Optional new description
    """
    clone = _resolve_clone(clone_id)
    target = clone.get("container_name") or clone["name"]
    body = {"description": description} if description else None
    return json.dumps(_post(f"/clones/{target}/refresh", body, timeout=180), ensure_ascii=False)


@mcp.tool()
def reset_clone_to_snapshot(clone_id: str, snapshot_name: str, description: str | None = None) -> str:
    """Reset (switch) an existing clone to a snapshot's state, keeping its port.

    The clone's container is recreated on top of the snapshot data. Works with
    both main snapshots and clone snapshots (any snapshot under the data root).

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        snapshot_name: Snapshot directory name to reset the clone to
        description: Optional new description for the clone
    """
    clone = _resolve_clone(clone_id)
    body: dict = {"snapshot_name": snapshot_name}
    if description:
        body["description"] = description
    return json.dumps(_post(f"/clones/{clone['name']}/reset", body, timeout=180), ensure_ascii=False)


@mcp.tool()
def create_clone_snapshot(clone_id: str, description: str | None = None) -> str:
    """Create a snapshot of a specific clone's current state (not the main replica).

    Useful before risky operations on a clone; restore later with reset_clone_to_snapshot.

    Args:
        clone_id: Clone identifier - subvolume name, container name, host port (e.g. "5435"), or connection URL/DSN (e.g. "postgresql://user:pass@host:5435/db")
        description: What this snapshot captures (e.g. "before migration test")
    """
    clone = _resolve_clone(clone_id)
    body = {"description": description} if description else None
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
def create_snapshot(description: str) -> str:
    """Create a snapshot of the current main replica state.

    Args:
        description: What this snapshot captures (e.g. "before migration")
    """
    return json.dumps(_post("/snapshots", {"description": description}), ensure_ascii=False)


@mcp.tool()
def delete_snapshot(snapshot_name: str) -> str:
    """Delete a snapshot.

    Args:
        snapshot_name: Snapshot directory name
    """
    return json.dumps(_delete(f"/snapshots/{snapshot_name}"), ensure_ascii=False)


@mcp.tool()
def clone_from_snapshot(snapshot_name: str, description: str | None = None) -> str:
    """Create a new clone from a specific snapshot.

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

    Args:
        tables: List of table names to add
        refresh: Whether to refresh the subscription after adding
    """
    return json.dumps(_post("/replication/tables", {"tables": tables, "refresh": refresh}), ensure_ascii=False)


@mcp.tool()
def remove_tables_from_replication(tables: list[str], refresh: bool = False) -> str:
    """Remove tables from the publication.

    Args:
        tables: List of table names to remove
        refresh: Whether to refresh the subscription after removing
    """
    return json.dumps(_delete("/replication/tables", {"tables": tables, "refresh": refresh}), ensure_ascii=False)


@mcp.tool()
def refresh_subscription() -> str:
    """Refresh the subscription to pick up publication changes."""
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
    """Install or update the auto-add event trigger on the publisher."""
    return json.dumps(_post("/replication/trigger-install"))


@mcp.tool()
def get_copy_progress() -> str:
    """Get initial data copy progress (for new subscriptions)."""
    return json.dumps(_get("/replication/copy-progress"))


if __name__ == "__main__":
    mcp.run(transport="stdio")
