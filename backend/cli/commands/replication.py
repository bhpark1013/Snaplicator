"""Replication management commands."""
from __future__ import annotations
import typer
import json
from typing import Optional, List
from ..client import SnaplicatorClient

app = typer.Typer(help="Monitor and manage replication")

def _client(ctx: typer.Context) -> SnaplicatorClient:
    return ctx.obj

def _out(data):
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))

@app.command()
def lag(ctx: typer.Context):
    """Show replication lag."""
    _out(_client(ctx).get("/replication/lag"))

@app.command()
def status(ctx: typer.Context):
    """Show subscription status."""
    _out(_client(ctx).get("/replication/subscription-status"))

@app.command()
def tables(ctx: typer.Context):
    """List replication tables."""
    _out(_client(ctx).get("/replication/tables"))

@app.command("add-tables")
def add_tables(ctx: typer.Context, table_names: List[str] = typer.Argument(..., help="Table names to add"), refresh_sub: bool = typer.Option(False, "--refresh", "-r", help="Refresh subscription after")):
    """Add tables to publication."""
    body = {"tables": table_names, "refresh": refresh_sub}
    _out(_client(ctx).post("/replication/tables", body))

@app.command("remove-tables")
def remove_tables(ctx: typer.Context, table_names: List[str] = typer.Argument(..., help="Table names to remove"), refresh_sub: bool = typer.Option(False, "--refresh", "-r", help="Refresh subscription after")):
    """Remove tables from publication."""
    body = {"tables": table_names, "refresh": refresh_sub}
    _out(_client(ctx).delete("/replication/tables", body))

@app.command("refresh")
def refresh_sub(ctx: typer.Context):
    """Refresh subscription."""
    _out(_client(ctx).post("/replication/refresh"))

@app.command()
def logs(ctx: typer.Context, tail: int = typer.Option(500, "--tail", "-n", help="Number of log lines")):
    """Show replication logs."""
    _out(_client(ctx).get(f"/replication/logs?tail={tail}"))

@app.command()
def check(ctx: typer.Context):
    """Run replication check."""
    _out(_client(ctx).get("/replication/check"))

@app.command()
def info(ctx: typer.Context):
    """Show connection info."""
    _out(_client(ctx).get("/replication/info"))

@app.command("trigger-status")
def trigger_status(ctx: typer.Context):
    """Check auto-add trigger status."""
    _out(_client(ctx).get("/replication/trigger-status"))

@app.command("trigger-install")
def trigger_install(ctx: typer.Context):
    """Install auto-add trigger on publisher."""
    _out(_client(ctx).post("/replication/trigger-install"))

@app.command("copy-progress")
def copy_progress(ctx: typer.Context):
    """Show initial copy progress."""
    _out(_client(ctx).get("/replication/copy-progress"))
