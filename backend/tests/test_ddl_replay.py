"""End-to-end replay check for captured DDL.

Runs a migration-like scenario on the publisher, then re-executes the
captured (ddl_text, search_path) rows in id order against a FRESH database
and asserts the resulting schema is identical (catalog-level comparison).

This proves the captured artifacts are sufficient to reproduce the schema.
The subscriber-side transport (log table in the publication + ENABLE ALWAYS
apply trigger) is Phase 2 and is not under test here.
"""
from __future__ import annotations

import base64

from conftest import psql, psql_multi, log_count

REPLAY_DB = "ddl_replay_target"

# One psql session, one -c per statement: SET search_path persists across
# statements while each DDL still runs in its own transaction (so CREATE
# INDEX CONCURRENTLY works) — the same shape as a real migration session.
SCENARIO = [
	"CREATE SCHEMA mig;",
	"SET search_path TO mig;",  # not DDL — must not be captured
	"CREATE TABLE parent (id int PRIMARY KEY);",
	"CREATE TABLE child (id int PRIMARY KEY, pid int);",
	"ALTER TABLE child ADD CONSTRAINT child_fk FOREIGN KEY (pid) REFERENCES parent(id);",
	"CREATE SEQUENCE mig_seq;",
	"ALTER SEQUENCE mig_seq INCREMENT BY 5;",
	"CREATE TABLE events (id int, created date) PARTITION BY RANGE (created);",
	"CREATE TABLE events_a PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');",
	"CREATE TABLE events_b (id int, created date);",
	"ALTER TABLE events ATTACH PARTITION events_b FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');",
	"CREATE EXTENSION pg_trgm;",
	"CREATE MATERIALIZED VIEW mv AS SELECT count(*) AS n FROM parent;",
	"CREATE FUNCTION touch_fn() RETURNS trigger LANGUAGE plpgsql AS $t$ BEGIN RETURN NEW; END $t$;",
	"CREATE TRIGGER parent_touch BEFORE INSERT ON parent FOR EACH ROW EXECUTE FUNCTION touch_fn();",
	"CREATE INDEX CONCURRENTLY child_pid_idx ON child (id);",
	"CREATE SCHEMA mig2;",
	"ALTER TABLE parent SET SCHEMA mig2;",
]
CAPTURED_EXPECTED = 17  # every statement except the SET

# Catalog-level schema snapshot, restricted to the scenario-owned schemas.
COMPARE_QUERIES = [
	(
		"columns",
		"SELECT table_schema, table_name, column_name, data_type, is_nullable, "
		"coalesce(column_default,'') FROM information_schema.columns "
		"WHERE table_schema IN ('mig','mig2') ORDER BY 1, 2, ordinal_position;",
	),
	(
		"constraints",
		"SELECT n.nspname, c.relname, con.conname, pg_get_constraintdef(con.oid) "
		"FROM pg_constraint con "
		"JOIN pg_class c ON c.oid = con.conrelid "
		"JOIN pg_namespace n ON n.oid = c.relnamespace "
		"WHERE n.nspname IN ('mig','mig2') ORDER BY 1, 2, 3;",
	),
	(
		"indexes",
		"SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
		"WHERE schemaname IN ('mig','mig2') ORDER BY 1, 2, 3;",
	),
	(
		"sequences",
		"SELECT schemaname, sequencename, data_type::text, start_value::text, "
		"increment_by::text FROM pg_sequences "
		"WHERE schemaname IN ('mig','mig2') ORDER BY 1, 2;",
	),
	(
		"matviews",
		"SELECT schemaname, matviewname, definition FROM pg_matviews "
		"WHERE schemaname IN ('mig','mig2') ORDER BY 1, 2;",
	),
	(
		"triggers",
		"SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
		"JOIN pg_class c ON c.oid = t.tgrelid "
		"JOIN pg_namespace n ON n.oid = c.relnamespace "
		"WHERE NOT t.tgisinternal AND n.nspname IN ('mig','mig2') ORDER BY 1;",
	),
	(
		"partitions",
		"SELECT n.nspname, c.relname, coalesce(pg_get_expr(c.relpartbound, c.oid),'') "
		"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
		"WHERE n.nspname IN ('mig','mig2') AND (c.relispartition OR c.relkind = 'p') "
		"ORDER BY 1, 2;",
	),
	(
		"extensions",
		"SELECT extname FROM pg_extension ORDER BY 1;",
	),
]


def _fetch_log_entries() -> list[tuple[str, str]]:
	"""(search_path, ddl_text) in id order — base64 so any text survives."""
	out = psql(
		"SELECT id::text || ':' "
		"|| replace(encode(convert_to(coalesce(search_path,''),'UTF8'),'base64'), chr(10), '') || ':' "
		"|| replace(encode(convert_to(ddl_text,'UTF8'),'base64'), chr(10), '') "
		"FROM public._snaplicator_ddl_log ORDER BY id;"
	)
	entries = []
	for line in out.splitlines():
		_id, sp_b64, ddl_b64 = line.split(":")
		sp = base64.b64decode(sp_b64).decode() if sp_b64 else ""
		ddl = base64.b64decode(ddl_b64).decode()
		entries.append((sp, ddl))
	return entries


class TestMigrationReplay:
	def test_scenario_replays_to_identical_schema(self, clean_log):
		# 1) run the migration scenario on the publisher
		psql_multi(SCENARIO)

		# 2) capture completeness: exactly one row per DDL statement
		assert log_count() == CAPTURED_EXPECTED

		# 3) replay into a fresh database, id order, honouring search_path;
		#    SET and DDL as separate -c so non-transactional DDL (CIC) works
		psql(f"DROP DATABASE IF EXISTS {REPLAY_DB};")
		psql(f"CREATE DATABASE {REPLAY_DB};")
		for sp, ddl in _fetch_log_entries():
			cmds = []
			if sp:
				cmds.append(f"SET search_path = {sp};")
			cmds.append(ddl)
			psql_multi(cmds, db=REPLAY_DB)

		# 4) the replayed schema must be identical to the publisher's
		for name, query in COMPARE_QUERIES:
			pub_out = psql(query)
			rep_out = psql(query, db=REPLAY_DB)
			assert pub_out == rep_out, (
				f"schema mismatch in {name}:\n"
				f"--- publisher ---\n{pub_out}\n"
				f"--- replayed ----\n{rep_out}"
			)
