"""Phase 1 — publisher-side DDL capture tests (TDD: written before the SUT).

Contract under test, to be implemented in app.services.replication:

    install_capture_triggers(publisher_connstr: str, publication_name: str) -> dict
    verify_capture_installed(publisher_connstr: str) -> bool

install_capture_triggers must, idempotently:
  * create table _snaplicator_ddl_log
      (id bigserial PK, captured_at timestamptz, lsn pg_lsn, txid bigint,
       command_tag text, object_identity text, schema_name text,
       ddl_text text, search_path text)
  * install event triggers
      _snaplicator_capture_ddl   ON ddl_command_end   (CREATE/ALTER, wide)
      _snaplicator_capture_drop  ON sql_drop          (DROP)
    and replace the legacy _snaplicator_auto_pub_add trigger
  * capture-side guards — everything else is captured wide:
      1. recursion  — DDL touching _snaplicator_* objects is never logged
      2. noise/DCL  — GRANT/REVOKE/SECURITY LABEL/COMMENT never logged
      3. dedupe     — at most one log row per (txid, ddl_text)
  * ddl_text = current_query(); search_path = current search_path
  * legacy behaviour kept: CREATE TABLE is auto-added to the publication,
    EXCEPT when the publication is FOR ALL TABLES (must not error — dev bug)
  * the log table itself is NOT auto-added to the publication
    (publication membership is a Phase 2 deployment step)
"""
from __future__ import annotations

import pytest

from app.services.replication import (  # SUT — does not exist yet (red)
	install_capture_triggers,
	verify_capture_installed,
)

from conftest import (
	ALLTABLES_DB,
	ALLTABLES_PUBLICATION,
	LOG_TABLE,
	PG_DB,
	PUBLICATION,
	connstr_for,
	log_count,
	log_rows,
	psql,
	psql_multi,
)


# ── basic capture: CREATE / ALTER ──────────────────────────────────


class TestBasicCapture:
	def test_create_table(self, clean_log):
		psql("CREATE TABLE cap_t1 (id int PRIMARY KEY, name text);")
		rows = log_rows()
		assert len(rows) == 1
		r = rows[0]
		assert r["command_tag"] == "CREATE TABLE"
		assert r["object_identity"] == "public.cap_t1"
		assert r["schema_name"] == "public"
		assert "CREATE TABLE cap_t1" in r["ddl_text"]
		assert r["lsn"] != ""
		assert r["txid"] != ""

	def test_alter_table_add_column(self, clean_log):
		psql("CREATE TABLE cap_t2 (id int);")
		psql("ALTER TABLE cap_t2 ADD COLUMN extra text;")
		rows = log_rows("command_tag = 'ALTER TABLE'")
		assert len(rows) == 1
		assert "ADD COLUMN extra" in rows[0]["ddl_text"]

	def test_alter_table_drop_not_null(self, clean_log):
		"""The 2026-06-29 incident case: NOT NULL relaxed on the publisher."""
		psql("CREATE TABLE cap_incident (id int, order_item_id int NOT NULL);")
		psql("ALTER TABLE cap_incident ALTER COLUMN order_item_id DROP NOT NULL;")
		assert log_count("ddl_text LIKE '%DROP NOT NULL%'") == 1

	def test_alter_table_set_not_null(self, clean_log):
		psql("CREATE TABLE cap_t3 (id int, c int);")
		psql("ALTER TABLE cap_t3 ALTER COLUMN c SET NOT NULL;")
		assert log_count("ddl_text LIKE '%SET NOT NULL%'") == 1

	def test_alter_table_type_change(self, clean_log):
		psql("CREATE TABLE cap_t4 (c varchar(10));")
		psql("ALTER TABLE cap_t4 ALTER COLUMN c TYPE varchar(100);")
		assert log_count("ddl_text LIKE '%TYPE varchar(100)%'") == 1

	def test_alter_table_add_check(self, clean_log):
		psql("CREATE TABLE cap_t5 (amount int);")
		psql("ALTER TABLE cap_t5 ADD CONSTRAINT amt_pos CHECK (amount > 0);")
		assert log_count("ddl_text LIKE '%CHECK (amount > 0)%'") == 1

	def test_alter_table_drop_constraint(self, clean_log):
		psql("CREATE TABLE cap_t6 (amount int CONSTRAINT amt6 CHECK (amount > 0));")
		psql("ALTER TABLE cap_t6 DROP CONSTRAINT amt6;")
		assert log_count("ddl_text LIKE '%DROP CONSTRAINT amt6%'") == 1

	def test_rename_column(self, clean_log):
		psql("CREATE TABLE cap_t7 (old_name int);")
		psql("ALTER TABLE cap_t7 RENAME COLUMN old_name TO new_name;")
		assert log_count("ddl_text LIKE '%RENAME COLUMN old_name TO new_name%'") == 1

	def test_rename_table(self, clean_log):
		psql("CREATE TABLE cap_t8 (id int);")
		psql("ALTER TABLE cap_t8 RENAME TO cap_t8_renamed;")
		assert log_count("ddl_text LIKE '%RENAME TO cap_t8_renamed%'") == 1

	def test_create_and_drop_index(self, clean_log):
		psql("CREATE TABLE cap_t9 (id int);")
		psql("CREATE INDEX cap_t9_idx ON cap_t9 (id);")
		assert log_count("command_tag = 'CREATE INDEX'") == 1
		psql("DROP INDEX cap_t9_idx;")
		assert log_count("command_tag = 'DROP INDEX'") == 1

	def test_non_table_objects_captured_wide(self, clean_log):
		"""Wide capture: sequence / view / function / type all logged."""
		psql("CREATE SEQUENCE cap_seq;")
		psql("CREATE VIEW cap_v AS SELECT 1 AS one;")
		psql(
			"CREATE FUNCTION cap_fn() RETURNS int LANGUAGE sql "
			"AS $body$ SELECT 42 $body$;"
		)
		psql("CREATE TYPE cap_mood AS ENUM ('ok', 'meh');")
		for tag in ("CREATE SEQUENCE", "CREATE VIEW", "CREATE FUNCTION", "CREATE TYPE"):
			assert log_count(f"command_tag = '{tag}'") == 1, tag

	def test_non_public_schema_captured(self, clean_log):
		"""The 'deprecated'-schema lesson: no schema filter at capture."""
		psql("CREATE SCHEMA cap_legacy;")
		psql("CREATE TABLE cap_legacy.old_t (id int);")
		assert log_count("command_tag = 'CREATE SCHEMA'") == 1
		rows = log_rows("command_tag = 'CREATE TABLE'")
		assert len(rows) == 1
		assert rows[0]["object_identity"] == "cap_legacy.old_t"
		assert rows[0]["schema_name"] == "cap_legacy"


# ── DROP capture (sql_drop twin trigger) ───────────────────────────


class TestDropCapture:
	def test_drop_table(self, clean_log):
		psql("CREATE TABLE drop_t1 (id int);")
		psql("DROP TABLE drop_t1;")
		rows = log_rows("command_tag = 'DROP TABLE'")
		assert len(rows) == 1
		assert rows[0]["object_identity"] == "public.drop_t1"
		assert "DROP TABLE drop_t1" in rows[0]["ddl_text"]

	def test_drop_table_cascade_single_row(self, clean_log):
		"""CASCADE drops dependents too — still one row (replay uses CASCADE)."""
		psql("CREATE TABLE drop_t2 (id int);")
		psql("CREATE VIEW drop_v2 AS SELECT * FROM drop_t2;")
		psql("DROP TABLE drop_t2 CASCADE;")
		assert log_count("ddl_text LIKE '%DROP TABLE drop_t2 CASCADE%'") == 1

	def test_drop_multiple_tables_single_row(self, clean_log):
		psql("CREATE TABLE drop_m1 (id int);")
		psql("CREATE TABLE drop_m2 (id int);")
		psql("DROP TABLE drop_m1, drop_m2;")
		assert log_count("ddl_text LIKE '%DROP TABLE drop_m1, drop_m2%'") == 1

	def test_drop_schema(self, clean_log):
		psql("CREATE SCHEMA drop_s;")
		psql("DROP SCHEMA drop_s;")
		assert log_count("command_tag = 'DROP SCHEMA'") == 1


# ── dedupe guard ───────────────────────────────────────────────────


class TestDedupe:
	def test_serial_column_single_row(self, clean_log):
		"""CREATE TABLE with serial spawns sequence+index subcommands —
		pg_event_trigger_ddl_commands() returns several rows, log gets one."""
		psql("CREATE TABLE dd_serial (id serial PRIMARY KEY);")
		assert log_count() == 1

	def test_alter_drop_column_single_row(self, clean_log):
		"""ALTER TABLE DROP COLUMN fires BOTH ddl_command_end and sql_drop
		('table column' dropped object) — must collapse to one row."""
		psql("CREATE TABLE dd_dropcol (id int, c int);")
		psql("ALTER TABLE dd_dropcol DROP COLUMN c;")
		assert log_count("ddl_text LIKE '%DROP COLUMN c%'") == 1

	def test_multi_statement_string_single_row(self, clean_log):
		"""One PQexec with two DDLs: current_query() is identical for both
		firings — one row (whole string), replay executes both statements."""
		psql("CREATE TABLE dd_ms1 (id int); CREATE TABLE dd_ms2 (id int);")
		rows = log_rows()
		assert len(rows) == 1
		assert "dd_ms1" in rows[0]["ddl_text"]
		assert "dd_ms2" in rows[0]["ddl_text"]

	def test_same_ddl_different_transactions_two_rows(self, clean_log):
		"""Dedupe is per-transaction, not global: the same statement executed
		again later must be logged again."""
		psql("CREATE TABLE dd_re (id int);")
		psql("ALTER TABLE dd_re ADD COLUMN c int;")
		psql("ALTER TABLE dd_re DROP COLUMN c;")
		psql("ALTER TABLE dd_re ADD COLUMN c int;")
		assert log_count("ddl_text LIKE '%ADD COLUMN c int%'") == 2


# ── capture guards: recursion, DCL/noise, secrets ──────────────────


class TestGuards:
	def test_snaplicator_objects_never_logged(self, clean_log):
		"""Recursion hard-exclude: our own plumbing must not enter the log."""
		psql(f"CREATE INDEX _snaplicator_tmp_idx ON {LOG_TABLE} (command_tag);")
		psql(f"DROP INDEX _snaplicator_tmp_idx;")
		assert log_count() == 0

	def test_grant_revoke_not_captured(self, clean_log):
		psql("CREATE TABLE guard_g (id int);")
		psql("CREATE ROLE cap_role NOLOGIN;")
		try:
			psql("GRANT SELECT ON guard_g TO cap_role;")
			psql("REVOKE SELECT ON guard_g FROM cap_role;")
			assert log_count("command_tag IN ('GRANT', 'REVOKE')") == 0
		finally:
			psql("DROP OWNED BY cap_role; DROP ROLE cap_role;")

	def test_role_password_never_in_log(self, clean_log):
		"""ALTER ROLE is cluster-global (event triggers skip it) — but assert
		the outcome directly: no password string may ever land in the log."""
		psql("CREATE ROLE cap_pwrole NOLOGIN;")
		try:
			psql("ALTER ROLE cap_pwrole PASSWORD 'supersecret123';")
			assert log_count("ddl_text LIKE '%supersecret123%'") == 0
		finally:
			psql("DROP ROLE cap_pwrole;")

	def test_publication_ddl_not_captured(self, clean_log):
		"""Publication DDL is publisher-only infrastructure — replaying it on
		a subscriber (which has no publication) would fail on every auto
		pub-add. Never logged, from either trigger."""
		psql("CREATE TABLE guard_pub_t (id int);")
		psql(f"TRUNCATE {LOG_TABLE};")
		psql(f"ALTER PUBLICATION {PUBLICATION} DROP TABLE guard_pub_t;")
		psql(f"ALTER PUBLICATION {PUBLICATION} ADD TABLE guard_pub_t;")
		assert log_count() == 0

	def test_comment_not_captured(self, clean_log):
		psql("CREATE TABLE guard_c (id int);")
		psql("COMMENT ON TABLE guard_c IS 'noise';")
		assert log_count("command_tag = 'COMMENT'") == 0

	def test_truncate_not_captured(self, clean_log):
		"""TRUNCATE rides native logical replication (pubtruncate=t) —
		capturing it would double-truncate on replay."""
		psql("CREATE TABLE guard_tr (id int);")
		psql("INSERT INTO guard_tr VALUES (1);")
		psql("TRUNCATE guard_tr;")
		assert log_count("ddl_text ILIKE '%TRUNCATE%'") == 0


# ── transactional behaviour ────────────────────────────────────────


class TestTransactions:
	def test_two_ddl_same_transaction(self, clean_log):
		"""Distinct statements inside one txn: two rows, same txid, ordered."""
		psql("CREATE TABLE tx_t (id int);")
		psql(f"TRUNCATE {LOG_TABLE};")
		psql_multi(
			[
				"BEGIN;",
				"ALTER TABLE tx_t ADD COLUMN a int;",
				"ALTER TABLE tx_t ADD COLUMN b int;",
				"COMMIT;",
			]
		)
		rows = log_rows()
		assert len(rows) == 2
		assert rows[0]["txid"] == rows[1]["txid"]
		assert "ADD COLUMN a" in rows[0]["ddl_text"]
		assert "ADD COLUMN b" in rows[1]["ddl_text"]

	def test_rolled_back_ddl_not_captured(self, clean_log):
		"""Log rides the DDL's own transaction: aborted DDL leaves no row —
		nothing to replay for schema changes that never happened."""
		psql_multi(
			[
				"BEGIN;",
				"CREATE TABLE tx_rb (id int);",
				"ROLLBACK;",
			]
		)
		assert log_count() == 0

	def test_search_path_captured(self, clean_log):
		psql("CREATE SCHEMA sp_s;")
		psql(f"TRUNCATE {LOG_TABLE};")
		psql_multi(
			[
				"SET search_path TO sp_s;",
				"CREATE TABLE sp_t (id int);",
			]
		)
		rows = log_rows("command_tag = 'CREATE TABLE'")
		assert len(rows) == 1
		assert rows[0]["schema_name"] == "sp_s"
		assert "sp_s" in rows[0]["search_path"]

	def test_do_block_captured_as_outer_statement(self, clean_log):
		"""current_query() returns the outer DO block — which is itself
		replayable SQL."""
		psql("DO $do$ BEGIN EXECUTE 'CREATE TABLE do_t (id int)'; END $do$;")
		rows = log_rows("command_tag = 'CREATE TABLE'")
		assert len(rows) == 1
		assert rows[0]["ddl_text"].startswith("DO ")


# ── publication auto-add (legacy behaviour, both shapes) ───────────


class TestPublicationAutoAdd:
	def test_create_table_auto_added_to_publication(self, clean_log):
		psql("CREATE TABLE pub_auto_t (id int);")
		out = psql(
			f"SELECT count(*) FROM pg_publication_tables "
			f"WHERE pubname = '{PUBLICATION}' AND tablename = 'pub_auto_t';"
		)
		assert out == "1"

	def test_log_table_not_auto_added(self, capture_installed):
		"""Publication membership of the log table is a Phase 2 deploy step."""
		out = psql(
			f"SELECT count(*) FROM pg_publication_tables "
			f"WHERE pubname = '{PUBLICATION}' AND tablename = '{LOG_TABLE}';"
		)
		assert out == "0"

	def test_for_all_tables_create_table_no_error(self, capture_installed_alltables):
		"""Dev-shape regression: ALTER PUBLICATION ADD TABLE on a FOR ALL
		TABLES publication errors — the trigger must skip it, and the CREATE
		must succeed and still be captured."""
		psql(f"TRUNCATE {LOG_TABLE};", db=ALLTABLES_DB)
		psql("CREATE TABLE alltab_t (id int);", db=ALLTABLES_DB)  # must not raise
		assert log_count("command_tag = 'CREATE TABLE'", db=ALLTABLES_DB) == 1
		out = psql(
			f"SELECT count(*) FROM pg_publication_tables "
			f"WHERE pubname = '{ALLTABLES_PUBLICATION}' AND tablename = 'alltab_t';",
			db=ALLTABLES_DB,
		)
		assert out == "1"  # FOR ALL TABLES includes it automatically


# ── migration-DDL coverage (previously untested kinds) ────────────


class TestMigrationDdl:
	def test_add_foreign_key(self, clean_log):
		psql("CREATE TABLE gap_parent (id int PRIMARY KEY);")
		psql("CREATE TABLE gap_child (id int PRIMARY KEY, pid int);")
		psql(
			"ALTER TABLE gap_child ADD CONSTRAINT gap_child_fk "
			"FOREIGN KEY (pid) REFERENCES gap_parent(id);"
		)
		assert log_count("ddl_text LIKE '%FOREIGN KEY%'") == 1

	def test_alter_table_set_schema(self, clean_log):
		psql("CREATE SCHEMA gap_target;")
		psql("CREATE TABLE gap_mover (id int);")
		psql("ALTER TABLE gap_mover SET SCHEMA gap_target;")
		rows = log_rows("ddl_text LIKE '%SET SCHEMA gap_target%'")
		assert len(rows) == 1
		assert rows[0]["command_tag"] == "ALTER TABLE"

	def test_alter_sequence(self, clean_log):
		psql("CREATE SEQUENCE gap_seq;")
		psql("ALTER SEQUENCE gap_seq INCREMENT BY 5;")
		assert log_count("command_tag = 'ALTER SEQUENCE'") == 1

	def test_partitioned_table_lifecycle(self, clean_log):
		psql(
			"CREATE TABLE gap_events (id int, created date) "
			"PARTITION BY RANGE (created);"
		)
		psql(
			"CREATE TABLE gap_events_a PARTITION OF gap_events "
			"FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');"
		)
		psql("CREATE TABLE gap_events_b (id int, created date);")
		psql(
			"ALTER TABLE gap_events ATTACH PARTITION gap_events_b "
			"FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');"
		)
		assert log_count("command_tag = 'CREATE TABLE'") == 3
		assert log_count("ddl_text LIKE '%ATTACH PARTITION%'") == 1

	def test_create_extension(self, clean_log):
		"""The extension script's inner DDL fires the trigger per command
		(first: CREATE TYPE citext), so command_tag records an inner tag —
		what matters is that the statement is captured exactly once and
		ddl_text is the replayable CREATE EXTENSION itself."""
		psql("CREATE EXTENSION citext;")
		try:
			assert log_count("ddl_text LIKE 'CREATE EXTENSION citext%'") == 1
			assert log_count() == 1  # dedupe: dozens of member objects, one row
		finally:
			psql("DROP EXTENSION citext;")

	def test_create_materialized_view(self, clean_log):
		psql("CREATE MATERIALIZED VIEW gap_mv AS SELECT 1 AS one;")
		assert log_count("command_tag = 'CREATE MATERIALIZED VIEW'") == 1

	def test_create_row_trigger(self, clean_log):
		psql("CREATE TABLE gap_trg_t (id int);")
		psql(
			"CREATE FUNCTION gap_trg_fn() RETURNS trigger LANGUAGE plpgsql "
			"AS $t$ BEGIN RETURN NEW; END $t$;"
		)
		psql(
			"CREATE TRIGGER gap_trg BEFORE INSERT ON gap_trg_t "
			"FOR EACH ROW EXECUTE FUNCTION gap_trg_fn();"
		)
		assert log_count("command_tag = 'CREATE FUNCTION'") == 1
		assert log_count("command_tag = 'CREATE TRIGGER'") == 1

	def test_create_index_concurrently(self, clean_log):
		"""Captured with tag CREATE INDEX. In-stream replay must defer it
		(cannot run inside a transaction) — capture itself is ordinary."""
		psql("CREATE TABLE gap_cic_t (id int);")
		psql("CREATE INDEX CONCURRENTLY gap_cic_idx ON gap_cic_t (id);")
		rows = log_rows("command_tag = 'CREATE INDEX'")
		assert len(rows) == 1
		assert "CONCURRENTLY" in rows[0]["ddl_text"]


# ── installer ──────────────────────────────────────────────────────


class TestInstall:
	def test_verify_reports_installed(self, capture_installed):
		assert verify_capture_installed(connstr_for(PG_DB)) is True

	def test_install_is_idempotent(self, capture_installed, clean_log):
		psql("CREATE TABLE idem_t (id int);")
		install_capture_triggers(connstr_for(PG_DB), PUBLICATION)  # again
		# log rows survived the reinstall
		assert log_count("command_tag = 'CREATE TABLE'") == 1
		# no duplicate triggers
		out = psql(
			"SELECT count(*) FROM pg_event_trigger "
			"WHERE evtname LIKE '_snaplicator_capture%';"
		)
		assert out == "2"  # ddl_command_end + sql_drop
		# capture still works after reinstall
		psql("CREATE TABLE idem_t2 (id int);")
		assert log_count("command_tag = 'CREATE TABLE'") == 2
