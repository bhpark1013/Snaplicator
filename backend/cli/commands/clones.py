"""Clone management commands."""
from __future__ import annotations
import typer
import json
from typing import Optional
from ..client import SnaplicatorClient

app = typer.Typer(help="Manage database clones")

def _client(ctx: typer.Context) -> SnaplicatorClient:
    return ctx.obj

def _out(data):
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))

@app.command("list")
def list_clones(ctx: typer.Context):
    """List all clones."""
    _out(_client(ctx).get("/clones"))

@app.command()
def create(ctx: typer.Context, desc: str = typer.Option("", "--desc", "-d", help="Description"), port: Optional[int] = typer.Option(None, "--port", "-p", help="Host port")):
    """Create a new clone from main."""
    body = {}
    if desc:
        body["description"] = desc
    if port is not None:
        body["port"] = port
    _out(_client(ctx).post("/clones", body or None))

@app.command()
def detail(ctx: typer.Context, clone_id: str = typer.Argument(..., help="Clone ID or container name")):
    """Get clone detail."""
    _out(_client(ctx).get(f"/clones/{clone_id}"))

@app.command()
def delete(ctx: typer.Context, name: str = typer.Argument(..., help="Container name")):
    """Delete a clone."""
    _out(_client(ctx).delete(f"/clones/{name}"))

@app.command()
def refresh(ctx: typer.Context, name: str = typer.Argument(..., help="Container name"), desc: Optional[str] = typer.Option(None, "--desc", "-d")):
    """Refresh a clone with latest data."""
    body = {"description": desc} if desc else None
    _out(_client(ctx).post(f"/clones/{name}/refresh", body))

@app.command()
def usage(ctx: typer.Context, clone_id: str = typer.Argument(..., help="Clone ID")):
    """Get disk usage for a clone."""
    _out(_client(ctx).get(f"/clones/{clone_id}/usage"))

@app.command()
def snapshots(ctx: typer.Context, clone_id: str = typer.Argument(..., help="Clone ID")):
    """List snapshots for a clone."""
    _out(_client(ctx).get(f"/clones/{clone_id}/snapshots"))

@app.command("create-snapshot")
def create_snapshot(ctx: typer.Context, clone_id: str = typer.Argument(..., help="Clone ID"), desc: Optional[str] = typer.Option(None, "--desc", "-d")):
    """Create a snapshot of a clone."""
    body = {"description": desc} if desc else None
    _out(_client(ctx).post(f"/clones/{clone_id}/snapshots", body))

@app.command()
def reset(ctx: typer.Context, clone_id: str = typer.Argument(..., help="Clone ID"), snapshot: str = typer.Option(..., "--snapshot", "-s", help="Snapshot name to reset to"), desc: Optional[str] = typer.Option(None, "--desc", "-d")):
    """Reset a clone to a snapshot."""
    body = {"snapshot_name": snapshot}
    if desc:
        body["description"] = desc
    _out(_client(ctx).post(f"/clones/{clone_id}/reset", body))

@app.command("fs-usage")
def fs_usage(ctx: typer.Context):
    """Get filesystem usage summary."""
    _out(_client(ctx).get("/clones/usage/fs"))
