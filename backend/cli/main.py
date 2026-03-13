"""Snaplicator CLI - psql-style remote API client."""
from __future__ import annotations
import os
import sys
import typer
from typing import Optional
from .client import SnaplicatorClient
from .commands import clones, snapshots, replication

app = typer.Typer(
    name="snaplicator",
    help="Snaplicator CLI - manage PostgreSQL replicas, clones, and snapshots.\n\nSet SNAPLICATOR_URL env var or use --host to specify the server.",
    no_args_is_help=True,
)
app.add_typer(clones.app, name="clones", help="Manage database clones")
app.add_typer(snapshots.app, name="snap", help="Manage snapshots")
app.add_typer(replication.app, name="repl", help="Monitor and manage replication")


@app.callback()
def main(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(
        None, "--host", "-H",
        envvar="SNAPLICATOR_URL",
        help="Snaplicator API URL (e.g. http://localhost:8888). Falls back to SNAPLICATOR_URL env var.",
    ),
):
    """Initialize the client with the target host."""
    url = host or os.environ.get("SNAPLICATOR_URL")
    if not url:
        typer.echo("Error: No host specified. Use --host or set SNAPLICATOR_URL.", err=True)
        raise typer.Exit(1)
    ctx.ensure_object(dict)
    ctx.obj = SnaplicatorClient(url)


@app.command()
def health(ctx: typer.Context):
    """Check server health."""
    import json
    result = ctx.obj.get("/health")
    typer.echo(json.dumps(result, indent=2))


def cli():
    app()


if __name__ == "__main__":
    cli()
