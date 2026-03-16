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


def _post(path: str, body: dict | None = None) -> dict:
    r = httpx.post(f"{BASE_URL}{path}", json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def _delete(path: str, body: dict | None = None) -> dict:
    r = httpx.delete(f"{BASE_URL}{path}", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


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
        clone_id: Clone identifier (subvolume name or container name)
    """
    return json.dumps(_get(f"/clones/{clone_id}"), ensure_ascii=False)


@mcp.tool()
def delete_clone(container_name: str) -> str:
    """Delete a clone and its container.
    
    Args:
        container_name: Docker container name of the clone
    """
    return json.dumps(_delete(f"/clones/{container_name}"), ensure_ascii=False)


@mcp.tool()
def refresh_clone(container_name: str, description: str | None = None) -> str:
    """Refresh a clone with the latest data from main.
    
    Args:
        container_name: Docker container name of the clone
        description: Optional new description
    """
    body = {"description": description} if description else None
    return json.dumps(_post(f"/clones/{container_name}/refresh", body), ensure_ascii=False)


@mcp.tool()
def get_clone_usage(clone_id: str) -> str:
    """Get disk usage for a specific clone.
    
    Args:
        clone_id: Clone identifier
    """
    return json.dumps(_get(f"/clones/{clone_id}/usage"), ensure_ascii=False)


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
    return json.dumps(_post(f"/snapshots/{snapshot_name}/clone", body), ensure_ascii=False)


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
