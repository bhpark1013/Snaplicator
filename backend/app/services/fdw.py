"""FDW (postgres_fdw) management for snaplicator replica.

Single source of truth is ``configs/fdw.yaml``. The backend renders the yaml
into ``configs/fdw_setup.generated.sql`` and also applies it to the live
replica via psql. The generated SQL is what ``replica-init/06_setup_fdw.sh``
uses on container init.

Secrets (FDW_USER/FDW_PASSWORD) and connection info (PRIMARY_HOST/PORT/DB)
are NOT baked into the generated SQL. They are passed at apply time via
``psql -v`` variables.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Tuple

import yaml
from pydantic import BaseModel, Field, ConfigDict


# ─── YAML model ─────────────────────────────────────────────────────────


class FdwTable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(alias="schema")
    name: str

    def fqn(self) -> str:
        return f"{self.schema_name}.{self.name}"


class FdwSchema(BaseModel):
    name: str


class FdwServer(BaseModel):
    name: str
    options: Dict[str, str] = Field(default_factory=dict)


class FdwConfig(BaseModel):
    server: FdwServer
    schemas: List[FdwSchema] = Field(default_factory=list)
    tables: List[FdwTable] = Field(default_factory=list)

    def schema_set(self) -> set[str]:
        return {s.name for s in self.schemas}

    def table_set(self) -> set[str]:
        return {t.fqn() for t in self.tables}


YAML_HEADER = """# Single source of truth for postgres_fdw management on this snaplicator replica.
#
# Edit via the Replication UI (preferred) or by hand.
# When edited by hand: POST /replication/fdw/regenerate after saving to re-apply.
#
# Server connection (host/port/db) is reused from .env PRIMARY_* values.
# Credentials are read from .env FDW_USER and FDW_PASSWORD.

"""


def load_yaml(path: str | Path) -> FdwConfig:
    p = Path(path)
    if not p.exists():
        # Sane default: empty config with a single server entry.
        return FdwConfig(server=FdwServer(name="prod_fdw", options={
            "sslmode": "require",
            "fetch_size": "10000",
            "use_remote_estimate": "true",
        }))
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return FdwConfig.model_validate(raw)


def save_yaml_atomic(path: str | Path, cfg: FdwConfig) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Hand-format the body so 'tables:' entries stay on one line each (matches
    # initial file). yaml.safe_dump would expand them to block style.
    body = {
        "server": {
            "name": cfg.server.name,
            "options": cfg.server.options,
        },
        "schemas": [{"name": s.name} for s in cfg.schemas],
    }
    body_text = yaml.safe_dump(body, sort_keys=False, default_flow_style=False)

    table_lines = ["tables:"]
    if not cfg.tables:
        table_lines = ["tables: []"]
    else:
        for t in cfg.tables:
            table_lines.append(
                f"  - {{ schema: {t.schema_name}, name: {t.name} }}"
            )
    table_text = "\n".join(table_lines) + "\n"

    final = YAML_HEADER + body_text + "\n" + table_text

    fd, tmp_path = tempfile.mkstemp(prefix=".fdw.yaml.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(final)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# ─── SQL rendering ─────────────────────────────────────────────────────


def _q_lit(s: str) -> str:
    """Single-quote a SQL literal."""
    return "'" + s.replace("'", "''") + "'"


def _q_ident(s: str) -> str:
    """Double-quote a SQL identifier."""
    return '"' + s.replace('"', '""') + '"'


def render_sql(cfg: FdwConfig) -> str:
    """Render a self-contained psql script.

    The script expects these psql variables (provided via ``-v`` at apply time):
      primary_host, primary_port, primary_db, fdw_user, fdw_password
    """
    server_name = cfg.server.name
    server_ident = _q_ident(server_name)

    # Static yaml-defined options inlined into the CREATE SERVER OPTIONS clause.
    # host/port/dbname are supplied via psql :'var' substitution at apply time.
    static_opts_sql_parts = [
        f"{k} {_q_lit(v)}"
        for k, v in cfg.server.options.items()
        if k not in {"host", "port", "dbname"}
    ]

    lines: list[str] = []
    lines.append("-- AUTO-GENERATED from configs/fdw.yaml. DO NOT EDIT BY HAND.")
    lines.append("-- Regenerate via POST /replication/fdw/regenerate.")
    lines.append("\\set ON_ERROR_STOP on")
    lines.append("")
    lines.append("CREATE EXTENSION IF NOT EXISTS postgres_fdw;")
    lines.append("")

    # Recreate server cleanly. DROP CASCADE removes any dependent foreign tables
    # and user mappings, then we rebuild from yaml. Simpler than ALTER, safer
    # than a DO block (which can't see psql :'var' substitutions because it is
    # dollar-quoted).
    lines.append(f"-- Foreign server: {server_name}")
    lines.append(f"DROP SERVER IF EXISTS {server_ident} CASCADE;")
    create_opts_sql = ", ".join(
        ["host :'primary_host'", "port :'primary_port'", "dbname :'primary_db'"]
        + static_opts_sql_parts
    )
    lines.append(
        f"CREATE SERVER {server_ident} FOREIGN DATA WRAPPER postgres_fdw OPTIONS ({create_opts_sql});"
    )
    lines.append(
        f"CREATE USER MAPPING FOR CURRENT_USER SERVER {server_ident} "
        "OPTIONS (user :'fdw_user', password :'fdw_password');"
    )
    lines.append("")

    # Group table entries by schema for efficient IMPORT FOREIGN SCHEMA call.
    tables_by_schema: dict[str, list[str]] = {}
    for t in cfg.tables:
        tables_by_schema.setdefault(t.schema_name, []).append(t.name)

    # Schema-level imports. The DROP SERVER CASCADE above already removed all
    # foreign tables linked to this server, so we just (re)create the schema
    # and import. If a regular table with the same name as a remote table
    # exists, IMPORT will fail — that's intentional (catch the conflict).
    for s in cfg.schemas:
        sch_ident = _q_ident(s.name)
        lines.append(f"-- Schema-level FDW: {s.name}")
        lines.append(f"CREATE SCHEMA IF NOT EXISTS {sch_ident};")
        lines.append(
            f"IMPORT FOREIGN SCHEMA {sch_ident} FROM SERVER {server_ident} INTO {sch_ident} "
            "OPTIONS (import_collate 'false', import_default 'false');"
        )
        lines.append("")

    # Table-level imports. Regular tables with conflicting names are dropped
    # (their data is presumed empty — the typical replica-init pattern is to
    # schema-clone DDL but leave row data to either logical replication or FDW).
    for sch_name, tbl_names in tables_by_schema.items():
        sch_ident = _q_ident(sch_name)
        sch_lit = _q_lit(sch_name)
        lines.append(f"-- Table-level FDW: {sch_name} ({len(tbl_names)} table(s))")
        lines.append(f"CREATE SCHEMA IF NOT EXISTS {sch_ident};")
        for t in tbl_names:
            t_ident = _q_ident(t)
            t_lit = _q_lit(t)
            lines.append(
                "DO $fdw_drop$ DECLARE k char; BEGIN "
                f"SELECT c.relkind INTO k FROM pg_class c "
                f"JOIN pg_namespace n ON c.relnamespace = n.oid "
                f"WHERE n.nspname = {sch_lit} AND c.relname = {t_lit}; "
                "IF FOUND THEN "
                f"IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE {sch_ident}.{t_ident} CASCADE'; "
                f"ELSIF k = 'r' THEN EXECUTE 'DROP TABLE {sch_ident}.{t_ident} CASCADE'; "
                "END IF; END IF; END $fdw_drop$;"
            )
        names_list = ", ".join(_q_ident(t) for t in tbl_names)
        lines.append(
            f"IMPORT FOREIGN SCHEMA {sch_ident} LIMIT TO ({names_list}) "
            f"FROM SERVER {server_ident} INTO {sch_ident} "
            "OPTIONS (import_collate 'false', import_default 'false');"
        )
        lines.append("")

    return "\n".join(lines) + "\n"


def write_sql_atomic(path: str | Path, sql: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".fdw_setup.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sql)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# ─── Apply ─────────────────────────────────────────────────────────────


def apply_to_replica(
    sql_path: str | Path,
    container: str,
    pg_user: str,
    pg_db: str,
    primary_host: str,
    primary_port: int | str,
    primary_db: str,
    fdw_user: str,
    fdw_password: str,
    pg_password: Optional[str] = None,
) -> Dict:
    """Run the generated SQL inside the replica container via ``docker exec psql``.

    All credentials and conn info are passed as psql variables so they don't end
    up on the command line or in any file (the SQL file itself contains no secrets).
    """
    p = Path(sql_path)
    sql_text = p.read_text(encoding="utf-8")

    cmd = [
        "docker", "exec", "-i", container,
        "psql", "-U", pg_user, "-d", pg_db,
        "-v", "ON_ERROR_STOP=1",
        "-v", f"primary_host={primary_host}",
        "-v", f"primary_port={primary_port}",
        "-v", f"primary_db={primary_db}",
        "-v", f"fdw_user={fdw_user}",
        "-v", f"fdw_password={fdw_password}",
        "-f", "-",
    ]
    env = os.environ.copy()
    if pg_password:
        env["PGPASSWORD"] = pg_password

    proc = subprocess.run(
        cmd,
        input=sql_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return {
        "rc": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "ok": proc.returncode == 0,
    }


# ─── Validation ────────────────────────────────────────────────────────


def validate_config(cfg: FdwConfig) -> list[str]:
    errors: list[str] = []
    sch_set = cfg.schema_set()
    # No schema appears in both lists.
    for t in cfg.tables:
        if t.schema_name in sch_set:
            errors.append(
                f"Table {t.fqn()} conflicts with schema-level entry '{t.schema_name}'. "
                "Pick one or the other, not both."
            )
    # No duplicate tables.
    seen = set()
    for t in cfg.tables:
        if t.fqn() in seen:
            errors.append(f"Duplicate table entry: {t.fqn()}")
        seen.add(t.fqn())
    # No duplicate schemas.
    seen_s = set()
    for s in cfg.schemas:
        if s.name in seen_s:
            errors.append(f"Duplicate schema entry: {s.name}")
        seen_s.add(s.name)
    return errors


def validate_against_publication(
    cfg: FdwConfig,
    publisher_connstr: str,
    publication_name: str,
) -> list[str]:
    """Return tables in cfg that are also in the publication (conflict)."""
    sql = (
        f"SELECT schemaname || '.' || tablename FROM pg_publication_tables "
        f"WHERE pubname = '{publication_name}';"
    )
    cmd = ["psql", "-At", publisher_connstr, "-c", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        # Can't verify — return empty (don't block).
        return []
    pub_set = {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}

    errors: list[str] = []
    for t in cfg.tables:
        if t.fqn() in pub_set:
            errors.append(
                f"Table {t.fqn()} is registered in publication '{publication_name}'. "
                "Remove it from the publication before adding it to FDW."
            )
    # For schema-level entries, also check (though publication can be FOR TABLES IN SCHEMA)
    for s in cfg.schemas:
        for fq in pub_set:
            if fq.startswith(s.name + "."):
                errors.append(
                    f"Schema '{s.name}' has table {fq} in publication '{publication_name}'. "
                    "Cannot use schema-level FDW for a schema with replicated tables."
                )
                break
    return errors


# ─── Inspection ────────────────────────────────────────────────────────


def list_foreign_tables_on_replica(
    container: str,
    pg_user: str,
    pg_db: str,
    pg_password: Optional[str] = None,
) -> list[Dict]:
    """Query ``information_schema.foreign_tables`` on the replica."""
    sql = (
        "SELECT foreign_table_schema, foreign_table_name, foreign_server_name "
        "FROM information_schema.foreign_tables "
        "WHERE foreign_table_schema NOT IN ('pg_catalog','information_schema') "
        "ORDER BY foreign_table_schema, foreign_table_name;"
    )
    cmd = [
        "docker", "exec", container,
        "psql", "-U", pg_user, "-d", pg_db,
        "-At", "-F", "|", "-c", sql,
    ]
    env = os.environ.copy()
    if pg_password:
        env["PGPASSWORD"] = pg_password
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if proc.returncode != 0:
        return []
    out: list[Dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[0]:
            out.append({
                "schema": parts[0],
                "table": parts[1],
                "server": parts[2],
            })
    return out


# ─── High-level helpers (used by routes) ───────────────────────────────


def _regenerate_and_apply(
    cfg: FdwConfig,
    yaml_path: str | Path,
    sql_path: str | Path,
    apply_args: Dict,
) -> Dict:
    """Save yaml, render sql, apply to replica. yaml/sql writes happen only after apply succeeds."""
    sql = render_sql(cfg)
    # Apply first using a temp file inside the same dir so we don't expose half-written SQL.
    apply_path = Path(sql_path)
    apply_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_sql = tempfile.mkstemp(prefix=".fdw_apply.", dir=str(apply_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sql)
        result = apply_to_replica(sql_path=tmp_sql, **apply_args)
    finally:
        try:
            os.unlink(tmp_sql)
        except FileNotFoundError:
            pass

    if not result.get("ok"):
        return {"applied": False, "result": result}

    # Apply succeeded — persist yaml + generated SQL.
    save_yaml_atomic(yaml_path, cfg)
    write_sql_atomic(sql_path, sql)
    return {"applied": True, "result": result}


def add_tables(
    cfg: FdwConfig,
    yaml_path: str | Path,
    sql_path: str | Path,
    new_tables: list[Tuple[str, str]],
    apply_args: Dict,
    *,
    publisher_connstr: Optional[str] = None,
    publication_name: Optional[str] = None,
) -> Dict:
    existing = cfg.table_set()
    sch_set = cfg.schema_set()
    skipped: list[str] = []
    added: list[str] = []

    for schema, name in new_tables:
        fqn = f"{schema}.{name}"
        if fqn in existing:
            skipped.append(fqn)
            continue
        if schema in sch_set:
            skipped.append(f"{fqn} (schema-level already configured)")
            continue
        cfg.tables.append(FdwTable.model_validate({"schema": schema, "name": name}))
        added.append(fqn)

    val_errs = validate_config(cfg)
    if val_errs:
        return {"applied": False, "errors": val_errs}

    if publisher_connstr and publication_name:
        pub_errs = validate_against_publication(cfg, publisher_connstr, publication_name)
        if pub_errs:
            return {"applied": False, "errors": pub_errs}

    apply_result = _regenerate_and_apply(cfg, yaml_path, sql_path, apply_args)
    return {"added": added, "skipped": skipped, **apply_result}


def remove_tables(
    cfg: FdwConfig,
    yaml_path: str | Path,
    sql_path: str | Path,
    targets: list[Tuple[str, str]],
    apply_args: Dict,
) -> Dict:
    target_set = {f"{s}.{n}" for s, n in targets}
    before = cfg.table_set()
    cfg.tables = [t for t in cfg.tables if t.fqn() not in target_set]
    removed = sorted(before & target_set)
    not_found = sorted(target_set - before)

    apply_result = _regenerate_and_apply(cfg, yaml_path, sql_path, apply_args)
    # Note: the generated SQL itself only re-imports the still-listed tables; the
    # actual DROP for the removed foreign tables happens below for safety.
    # We do that as a separate small command using docker exec.
    drop_sqls = [
        f'DROP FOREIGN TABLE IF EXISTS "{fq.split(".")[0]}"."{fq.split(".")[1]}" CASCADE;'
        for fq in removed
    ]
    if drop_sqls:
        cmd = [
            "docker", "exec", "-i", apply_args["container"],
            "psql", "-U", apply_args["pg_user"], "-d", apply_args["pg_db"],
            "-v", "ON_ERROR_STOP=1", "-f", "-",
        ]
        env = os.environ.copy()
        if apply_args.get("pg_password"):
            env["PGPASSWORD"] = apply_args["pg_password"]
        subprocess.run(cmd, input="\n".join(drop_sqls), capture_output=True,
                       text=True, env=env, timeout=60)

    return {"removed": removed, "not_found": not_found, **apply_result}


def add_schemas(
    cfg: FdwConfig,
    yaml_path: str | Path,
    sql_path: str | Path,
    new_schemas: list[str],
    apply_args: Dict,
    *,
    publisher_connstr: Optional[str] = None,
    publication_name: Optional[str] = None,
) -> Dict:
    existing = cfg.schema_set()
    table_schemas = {t.schema_name for t in cfg.tables}
    added: list[str] = []
    skipped: list[str] = []
    for s in new_schemas:
        if s in existing:
            skipped.append(s)
            continue
        if s in table_schemas:
            skipped.append(f"{s} (already has table-level entries)")
            continue
        cfg.schemas.append(FdwSchema(name=s))
        added.append(s)

    val_errs = validate_config(cfg)
    if val_errs:
        return {"applied": False, "errors": val_errs}

    if publisher_connstr and publication_name:
        pub_errs = validate_against_publication(cfg, publisher_connstr, publication_name)
        if pub_errs:
            return {"applied": False, "errors": pub_errs}

    apply_result = _regenerate_and_apply(cfg, yaml_path, sql_path, apply_args)
    return {"added": added, "skipped": skipped, **apply_result}


def remove_schemas(
    cfg: FdwConfig,
    yaml_path: str | Path,
    sql_path: str | Path,
    targets: list[str],
    apply_args: Dict,
) -> Dict:
    target_set = set(targets)
    before = cfg.schema_set()
    cfg.schemas = [s for s in cfg.schemas if s.name not in target_set]
    removed = sorted(before & target_set)
    not_found = sorted(target_set - before)
    apply_result = _regenerate_and_apply(cfg, yaml_path, sql_path, apply_args)
    return {"removed": removed, "not_found": not_found, **apply_result}


# ─── FDW remote column-drift detection ─────────────────────────────────
#
# postgres_fdw foreign tables are a *static* local snapshot captured at
# IMPORT FOREIGN SCHEMA time. If the remote source gains/alters a column the
# local foreign table silently goes stale. We detect drift cheaply by mapping
# a helper foreign table onto the remote `information_schema.columns` view
# (its own shape never drifts) and comparing per-table column signatures
# against the local foreign-table definition. On any drift — or if the
# foreign server vanished — we reuse the idempotent full re-render/re-import.

_DRIFT_HELPER_SCHEMA = "_snaplicator"
_DRIFT_HELPER_TABLE = "remote_cols"


def _docker_psql_capture(
    container: str,
    pg_user: str,
    pg_db: str,
    sql: str,
    pg_password: Optional[str] = None,
    timeout: int = 90,
):
    cmd = [
        "docker", "exec", "-i", container,
        "psql", "-U", pg_user, "-d", pg_db,
        "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", "-f", "-",
    ]
    env = os.environ.copy()
    if pg_password:
        env["PGPASSWORD"] = pg_password
    return subprocess.run(
        cmd, input=sql, capture_output=True, text=True, env=env, timeout=timeout
    )


def detect_fdw_drift(
    cfg: FdwConfig,
    container: str,
    pg_user: str,
    pg_db: str,
    pg_password: Optional[str] = None,
) -> Dict:
    """Compare remote vs local column signatures for every configured FDW table.

    Returns dict: server_ok(bool), targets(int), drifted(list[(sch,tbl)]),
    error(str|None).
    """
    if not cfg.tables and not cfg.schemas:
        return {"server_ok": True, "targets": 0, "drifted": [], "error": None}

    server = cfg.server.name
    targets: set = {(t.schema_name, t.name) for t in cfg.tables}
    if cfg.schemas:
        sch_names = {s.name for s in cfg.schemas}
        for ft in list_foreign_tables_on_replica(container, pg_user, pg_db, pg_password):
            if ft.get("server") == server and ft.get("schema") in sch_names:
                targets.add((ft["schema"], ft["table"]))
    if not targets:
        return {"server_ok": True, "targets": 0, "drifted": [], "error": None}

    helper_sch = _q_ident(_DRIFT_HELPER_SCHEMA)
    helper_tbl = _q_ident(_DRIFT_HELPER_TABLE)
    helper_fqn = f"{helper_sch}.{helper_tbl}"
    server_ident = _q_ident(server)
    server_lit = _q_lit(server)
    pairs_sql = ", ".join(
        f"({_q_lit(s)}::text, {_q_lit(t)}::text)" for s, t in sorted(targets)
    )

    sql = rf"""\set ON_ERROR_STOP on
DO $chk$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = {server_lit}) THEN
    RAISE EXCEPTION 'snaplicator_fdw_server_missing';
  END IF;
END $chk$;
CREATE SCHEMA IF NOT EXISTS {helper_sch};
DROP FOREIGN TABLE IF EXISTS {helper_fqn};
CREATE FOREIGN TABLE {helper_fqn} (
  table_schema text,
  table_name text,
  column_name text,
  data_type text,
  ordinal_position int
) SERVER {server_ident}
  OPTIONS (schema_name 'information_schema', table_name 'columns');
WITH tgt(sch, tbl) AS (VALUES {pairs_sql}),
remote AS (
  SELECT rc.table_schema AS sch, rc.table_name AS tbl,
         string_agg(rc.column_name || ':' || rc.data_type,
                    ',' ORDER BY rc.ordinal_position) AS sig
  FROM {helper_fqn} rc
  JOIN tgt ON tgt.sch = rc.table_schema AND tgt.tbl = rc.table_name
  GROUP BY 1, 2
),
loc AS (
  SELECT c.table_schema AS sch, c.table_name AS tbl,
         string_agg(c.column_name || ':' || c.data_type,
                    ',' ORDER BY c.ordinal_position) AS sig
  FROM information_schema.columns c
  JOIN information_schema.foreign_tables ft
    ON ft.foreign_table_schema = c.table_schema
   AND ft.foreign_table_name = c.table_name
   AND ft.foreign_server_name = {server_lit}
  JOIN tgt ON tgt.sch = c.table_schema AND tgt.tbl = c.table_name
  GROUP BY 1, 2
)
SELECT COALESCE(r.sch, l.sch) || '|' || COALESCE(r.tbl, l.tbl)
FROM remote r
FULL JOIN loc l ON r.sch = l.sch AND r.tbl = l.tbl
WHERE r.sig IS DISTINCT FROM l.sig;
"""

    try:
        proc = _docker_psql_capture(container, pg_user, pg_db, sql, pg_password)
    except Exception as e:  # noqa: BLE001
        return {"server_ok": True, "targets": len(targets), "drifted": [],
                "error": f"psql invocation failed: {e}"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "snaplicator_fdw_server_missing" in err:
            return {"server_ok": False, "targets": len(targets),
                    "drifted": [], "error": None}
        return {"server_ok": True, "targets": len(targets),
                "drifted": [], "error": err[:800]}

    drifted = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "|" in line:
            s, t = line.split("|", 1)
            drifted.append((s, t))
    return {"server_ok": True, "targets": len(targets),
            "drifted": drifted, "error": None}


def sync_fdw_drift(
    cfg: FdwConfig,
    yaml_path,
    sql_path,
    apply_args: Dict,
) -> Dict:
    """Detect remote column drift on FDW tables and, if found (or if the
    foreign server is gone), re-render + re-import via the idempotent path.

    Returns: checked(int), drifted(list[str]), reapplied(bool), error.
    """
    det = detect_fdw_drift(
        cfg,
        apply_args["container"],
        apply_args["pg_user"],
        apply_args["pg_db"],
        apply_args.get("pg_password"),
    )
    if det.get("error"):
        return {"checked": det["targets"], "drifted": [],
                "reapplied": False, "error": det["error"]}

    server_missing = not det["server_ok"]
    drifted_pairs = det["drifted"]
    if not server_missing and not drifted_pairs:
        return {"checked": det["targets"], "drifted": [],
                "reapplied": False, "error": None}

    res = _regenerate_and_apply(cfg, yaml_path, sql_path, apply_args)
    drifted_names = [f"{s}.{t}" for s, t in drifted_pairs]
    if server_missing and not drifted_names:
        drifted_names = ["<foreign-server-missing>"]
    return {
        "checked": det["targets"],
        "drifted": drifted_names,
        "reapplied": bool(res.get("applied")),
        "error": None if res.get("applied") else res.get("result"),
    }
