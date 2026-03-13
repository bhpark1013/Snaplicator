"""Snapshot management commands."""
from __future__ import annotations
import typer
import json
from typing import Optional
from ..client import SnaplicatorClient

app = typer.Typer(help="Manage snapshots")

def _client(ctx: typer.Context) -> SnaplicatorClient:
    return ctx.obj

def _out(data):
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))

@app.command("list")
def list_snapshots(ctx: typer.Context):
    """List all snapshots."""
    _out(_client(ctx).get("/snapshots"))

@app.command()
def create(ctx: typer.Context, desc: str = typer.Option("", "--desc", "-d", help="Description")):
    """Create a snapshot of the main data."""
    body = {"description": desc} if desc else None
    _out(_client(ctx).post("/snapshots", body))

@app.command()
def delete(ctx: typer.Context, name: str = typer.Argument(..., help="Snapshot name")):
    """Delete a snapshot."""
    _out(_client(ctx).delete(f"/snapshots/{name}"))

@app.command()
def clone(ctx: typer.Context, name: str = typer.Argument(..., help="Snapshot name"), desc: Optional[str] = typer.Option(None, "--desc", "-d")):
    """Create a clone from a snapshot."""
    body = {"description": desc} if desc else None
    _out(_client(ctx).post(f"/snapshots/{name}/clone", body))
