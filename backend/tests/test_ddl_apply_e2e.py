"""Phase 2 — end-to-end DDL replication through a REAL logical replication
stream (two containers, publication + subscription).

Contract under test, to be implemented in app.services.replication:

    install_ddl_apply(subscriber_container, user, password, db,
                      initial_watermark: int = 0) -> dict
    verify_ddl_apply_installed(subscriber_container, user, password, db) -> bool
    add_log_table_to_publication(publisher_connstr, publication_name) -> dict
    get_ddl_apply_status(subscriber_container, user, password, db) -> dict
        # {"watermark": int, "failures": int, "deferred_pending": int}

install_ddl_apply must, idempotently, create on the subscriber:
  * public._snaplicator_ddl_log       (same shape as publisher's — receives
                                       replicated rows)
  * public._snaplicator_ddl_watermark (single row, last_applied_id; seeded
                                       with initial_watermark)
  * public._snaplicator_ddl_failures  (log_id, ddl_text, error, failed_at)
  * public._snaplicator_ddl_deferred  (log_id, ddl_text, search_path, ...)
  * an ENABLE ALWAYS AFTER INSERT trigger on the log table that:
      - skips rows with id <= watermark (initial-COPY / clone protection)
      - defers ddl_text matching CONCURRENTLY (cannot run in the apply txn)
      - otherwise SETs the captured search_path and EXECUTEs ddl_text
      - on error: records to failures + RAISE WARNING, NEVER re-raises
        (re-raising would crash-loop the apply worker — the exact incident
        this system exists to prevent); watermark advances either way

Ordering guarantee under test: the log row commits in the same transaction
as the DDL on the publisher, so the apply worker executes the DDL at its
exact position between the surrounding DML — no LSN gates, no polling.
"""
from __future__ import annotations

from app.services.replication import (
	CAPTURE_LOG_PUBLICATION,
	enable_ddl_apply,
	get_ddl_apply_status,
	run_deferred_ddl,
	verify_ddl_apply_installed,
)

from conftest import (
	E2E_SUB,
	E2E_SUBSCRIPTION,
	LOG_TABLE,
	PG_DB,
	PG_PASSWORD,
	PG_USER,
	PUBLICATION,
	psql_conn,
	wait_until,
)


def _status():
	return get_ddl_apply_status(E2E_SUB, PG_USER, PG_PASSWORD, PG_DB)


class TestInfra:
	def test_apply_installed_and_log_published(self, pg_pair):
		assert verify_ddl_apply_installed(E2E_SUB, PG_USER, PG_PASSWORD, PG_DB) is True
		# The log rides the data publication — one publication, and the
		# subscription was never told a second name.
		out = psql_conn(
			pg_pair["pub"],
			f"SELECT count(*) FROM pg_publication_tables "
			f"WHERE pubname = '{PUBLICATION}' AND tablename = '{LOG_TABLE}';",
		)
		assert out == "1", "the data publication carries the log table"
		out = psql_conn(
			pg_pair["pub"],
			f"SELECT count(*) FROM pg_publication "
			f"WHERE pubname = '{CAPTURE_LOG_PUBLICATION}';",
		)
		assert out == "0", "and nothing creates a second publication for it"

	def test_pre_watermark_ddl_not_replayed(self, pg_pair):
		"""Rows captured before the watermark arrive via initial COPY (they
		ARE in the subscriber's log copy) but must never be executed."""
		sub = pg_pair["sub"]
		wait_until(
			lambda: psql_conn(
				sub,
				f"SELECT count(*) FROM {LOG_TABLE} "
				f"WHERE ddl_text LIKE '%pre_watermark_seq%';",
			) == "1",
			desc="pre-watermark log row copied",
		)
		assert psql_conn(
			sub, "SELECT count(*) FROM pg_class WHERE relname = 'pre_watermark_seq';"
		) == "0"
		assert _status()["failures"] == 0

	def test_enable_ddl_apply_idempotent(self, pg_pair):
		"""The loop calls the enable sequence every 30s once the flag is on —
		re-running it against a live stream must be a read-only no-op."""
		res = enable_ddl_apply(
			pg_pair["pub"], PUBLICATION,
			E2E_SUB, PG_USER, PG_PASSWORD, PG_DB, E2E_SUBSCRIPTION,
		)
		assert res["added"] is False, "the log table is already in the publication"
		assert res["refreshed"] is False, "so nothing has to be refreshed"


class TestInStreamApply:
	def test_incident_drop_not_null_then_null_insert(self, pg_pair):
		"""The 2026-06-29 incident, end to end: relax NOT NULL on the
		publisher, then insert a NULL — without DDL replication this
		crash-loops the subscription; with it, the DDL applies in-stream
		before the DML and the row lands."""
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(pub, "ALTER TABLE seed ALTER COLUMN req DROP NOT NULL;")
		psql_conn(pub, "INSERT INTO seed (id, val, req) VALUES (1, 'incident', NULL);")
		wait_until(
			lambda: psql_conn(
				sub, "SELECT count(*) FROM seed WHERE id = 1 AND req IS NULL;"
			) == "1",
			desc="NULL row replicated after in-stream DROP NOT NULL",
		)
		assert psql_conn(
			sub,
			"SELECT attnotnull FROM pg_attribute "
			"WHERE attrelid = 'public.seed'::regclass AND attname = 'req';",
		) == "f"

	def test_add_column_then_data(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(pub, "ALTER TABLE seed ADD COLUMN extra text;")
		psql_conn(pub, "INSERT INTO seed (id, val, req, extra) VALUES (2, 'b', 1, 'x');")
		wait_until(
			lambda: psql_conn(sub, "SELECT extra FROM seed WHERE id = 2;") == "x",
			desc="row with new column replicated",
		)

	def test_create_table_flows_through(self, pg_pair):
		"""CREATE TABLE arrives in-stream; auto pub-add puts it in the
		publication; after REFRESH its data flows too."""
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(pub, "CREATE TABLE flow_t (id int PRIMARY KEY, v text);")
		wait_until(
			lambda: psql_conn(sub, "SELECT count(*) FROM pg_class WHERE relname = 'flow_t';") == "1",
			desc="flow_t created on subscriber",
		)
		psql_conn(sub, f"ALTER SUBSCRIPTION {E2E_SUBSCRIPTION} REFRESH PUBLICATION;")
		psql_conn(pub, "INSERT INTO flow_t VALUES (1, 'hello');")
		wait_until(
			lambda: psql_conn(sub, "SELECT v FROM flow_t WHERE id = 1;") == "hello",
			desc="flow_t data replicated after refresh",
		)

	def test_multi_statement_with_search_path(self, pg_pair):
		"""One query string: schema + SET search_path + table. Captured as a
		single row and replayed whole — table must land in the new schema."""
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(
			pub,
			"CREATE SCHEMA e2e_s; SET search_path TO e2e_s; "
			"CREATE TABLE sp_tbl (id int);",
		)
		wait_until(
			lambda: psql_conn(sub, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'e2e_s' AND c.relname = 'sp_tbl';") == "1",
			desc="e2e_s.sp_tbl created on subscriber",
		)

	def test_drop_table_replicates(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(pub, "CREATE TABLE drop_flow_t (id int);")
		wait_until(
			lambda: psql_conn(sub, "SELECT count(*) FROM pg_class WHERE relname = 'drop_flow_t';") == "1",
			desc="drop_flow_t created on subscriber",
		)
		psql_conn(pub, "DROP TABLE drop_flow_t;")
		wait_until(
			lambda: psql_conn(sub, "SELECT count(*) FROM pg_class WHERE relname = 'drop_flow_t';") == "0",
			desc="drop_flow_t dropped on subscriber",
		)


class TestFailureIsolation:
	def test_failure_recorded_and_stream_survives(self, pg_pair):
		"""A DDL that cannot apply (subscriber-local drift) is recorded
		loudly but must NOT stall the stream — the apply worker keeps
		applying subsequent changes."""
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(sub, "CREATE TABLE clash_t (id int);")  # local drift
		psql_conn(pub, "CREATE TABLE clash_t (id int, extra text);")
		wait_until(
			lambda: psql_conn(
				sub,
				"SELECT count(*) FROM public._snaplicator_ddl_failures "
				"WHERE ddl_text LIKE '%clash_t%';",
			) == "1",
			desc="failure recorded",
		)
		# failure context includes search_path for manual replay
		assert psql_conn(
			sub,
			"SELECT count(*) FROM public._snaplicator_ddl_failures "
			"WHERE ddl_text LIKE '%clash_t%' AND search_path IS NOT NULL;",
		) == "1"
		# stream alive after the failure
		psql_conn(pub, "INSERT INTO seed (id, val, req) VALUES (3, 'alive', 2);")
		wait_until(
			lambda: psql_conn(sub, "SELECT count(*) FROM seed WHERE id = 3;") == "1",
			desc="stream still applying after failure",
		)
		assert _status()["failures"] >= 1

	def test_concurrently_is_deferred_not_executed(self, pg_pair):
		"""CREATE INDEX CONCURRENTLY cannot run inside the apply worker's
		transaction — it must land in the deferred queue (executed later,
		out-of-band, by the sync loop) without failing the stream."""
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		psql_conn(pub, "CREATE INDEX CONCURRENTLY seed_val_idx ON seed (val);")
		wait_until(
			lambda: psql_conn(
				sub,
				"SELECT count(*) FROM public._snaplicator_ddl_deferred "
				"WHERE ddl_text LIKE '%seed_val_idx%';",
			) == "1",
			desc="CONCURRENTLY ddl deferred",
		)
		assert psql_conn(
			sub, "SELECT count(*) FROM pg_class WHERE relname = 'seed_val_idx';"
		) == "0"
		assert _status()["deferred_pending"] == 1
		assert psql_conn(
			sub,
			"SELECT count(*) FROM public._snaplicator_ddl_failures "
			"WHERE ddl_text LIKE '%seed_val_idx%';",
		) == "0"

	def test_deferred_executor_one_shot(self, pg_pair):
		"""The loop's out-of-band executor: builds the deferred index exactly
		once; a second run finds nothing to do."""
		sub = pg_pair["sub"]
		res = run_deferred_ddl(E2E_SUB, PG_USER, PG_PASSWORD, PG_DB)
		assert res["errors"] == []
		assert len(res["executed"]) == 1
		assert psql_conn(
			sub, "SELECT count(*) FROM pg_class WHERE relname = 'seed_val_idx';"
		) == "1"
		assert _status()["deferred_pending"] == 0
		res2 = run_deferred_ddl(E2E_SUB, PG_USER, PG_PASSWORD, PG_DB)
		assert res2["executed"] == [] and res2["errors"] == []
