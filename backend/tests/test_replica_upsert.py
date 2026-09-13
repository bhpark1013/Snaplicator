"""End-to-end coverage for subscriber-side replica UPSERT handling.

The source still emits ordinary logical-replication INSERT/UPDATE/DELETE
messages.  These tests prove that only a conflicting replicated INSERT is
turned into an update on the subscriber, without weakening any other unique
constraint or changing locally issued SQL.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from app.services.replica_upsert import (
	install_replica_upsert,
	remove_replica_upsert,
)
from conftest import (
	E2E_SUB,
	E2E_SUBSCRIPTION,
	PG_DB,
	PG_PASSWORD,
	PG_USER,
	PUBLICATION,
	psql_conn,
	wait_until,
)


def _install(table: str, constraint: str) -> bool:
	return install_replica_upsert(
		E2E_SUB,
		PG_USER,
		PG_PASSWORD,
		PG_DB,
		"public",
		table,
		constraint,
	)


def _remove(table: str) -> None:
	remove_replica_upsert(
		E2E_SUB,
		PG_USER,
		PG_PASSWORD,
		PG_DB,
		"public",
		table,
	)


def _create_replica_upsert_table(pg_pair, table: str, *, extra_unique: bool = False) -> None:
	"""Create through DDL replication, install, then connect its DML stream."""
	pub, sub = pg_pair["pub"], pg_pair["sub"]
	extra = ", external_key text UNIQUE" if extra_unique else ""
	psql_conn(
		pub,
		f"CREATE TABLE {table} ("
		"id bigint PRIMARY KEY, "
		"business_key text NOT NULL, "
		"payload text, nullable_value text"
		f"{extra}, "
		f"CONSTRAINT {table}_business_key_key UNIQUE (business_key)"
		");",
	)
	wait_until(
		lambda: psql_conn(sub, f"SELECT to_regclass('public.{table}') IS NOT NULL;") == "t",
		desc=f"{table} schema replicated",
	)
	_install(table, f"{table}_business_key_key")
	psql_conn(sub, f"ALTER SUBSCRIPTION {E2E_SUBSCRIPTION} REFRESH PUBLICATION;")
	wait_until(
		lambda: psql_conn(
			sub,
			"SELECT count(*) FROM pg_subscription_rel sr "
			"JOIN pg_class c ON c.oid = sr.srrelid "
			"JOIN pg_namespace n ON n.oid = c.relnamespace "
			f"WHERE n.nspname = 'public' AND c.relname = '{table}' "
			"AND sr.srsubstate IN ('r', 's');",
		) == "1",
		timeout=60,
		desc=f"{table} subscription ready",
	)


def _subscriber_logs() -> str:
	proc = subprocess.run(
		["docker", "logs", E2E_SUB],
		text=True,
		capture_output=True,
		check=True,
	)
	return f"{proc.stdout}\n{proc.stderr}"


def _wait_for_log(fragment: str, *, since: int, timeout: float = 30.0) -> None:
	deadline = time.time() + timeout
	while time.time() < deadline:
		if fragment in _subscriber_logs()[since:]:
			return
		time.sleep(0.5)
	raise AssertionError(f"timed out waiting for subscriber log fragment: {fragment}")


class TestReplicatedUpserts:
	def test_conflicting_insert_then_repeated_upsert_update_and_delete(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		table = "replica_upsert_flow"
		_create_replica_upsert_table(pg_pair, table)
		assert _install(table, f"{table}_business_key_key") is False

		# Replica-only history has the same business key but a different PK.
		psql_conn(
			sub,
			f"INSERT INTO {table} VALUES "
			"(900, 'look-510058', 'replica-old', 'replica-only');",
		)

		# This is an INSERT on the source.  On the replica it must replace every
		# writable value, including the PK and an explicit NULL.
		psql_conn(
			pub,
			f"INSERT INTO {table} (id, business_key, payload, nullable_value) "
			"VALUES (1, 'look-510058', 'source-v1', NULL) "
			"ON CONFLICT (business_key) DO UPDATE SET "
			"id = EXCLUDED.id, payload = EXCLUDED.payload, "
			"nullable_value = EXCLUDED.nullable_value;",
		)
		wait_until(
			lambda: psql_conn(
				sub,
				f"SELECT id, business_key, payload, nullable_value IS NULL "
				f"FROM {table};",
			) == "1|look-510058|source-v1|t",
			desc="conflicting replicated INSERT converted to a complete update",
		)

		# A later source UPSERT is an UPDATE, and succeeds because the first
		# merge also copied the source PK.
		psql_conn(
			pub,
			f"INSERT INTO {table} (id, business_key, payload, nullable_value) "
			"VALUES (1, 'look-510058', 'source-v2', 'now-set') "
			"ON CONFLICT (business_key) DO UPDATE SET "
			"payload = EXCLUDED.payload, nullable_value = EXCLUDED.nullable_value;",
		)
		wait_until(
			lambda: psql_conn(
				sub,
				f"SELECT id, payload, nullable_value FROM {table};",
			) == "1|source-v2|now-set",
			desc="repeated source UPSERT update replicated",
		)

		psql_conn(pub, f"DELETE FROM {table} WHERE business_key = 'look-510058';")
		wait_until(
			lambda: psql_conn(sub, f"SELECT count(*) FROM {table};") == "0",
			desc="delete after repaired insert",
		)

		# The trigger introspects columns at execution time.  Recreate drift
		# after an in-stream ADD COLUMN so a new conflicting INSERT proves the
		# added column is copied too, without reinstalling the trigger.
		psql_conn(
			pub,
			f"ALTER TABLE {table} ADD COLUMN added_later text; "
			f"ALTER TABLE {table} ADD COLUMN discarded text; "
			f"ALTER TABLE {table} DROP COLUMN discarded; "
			f"ALTER TABLE {table} ADD COLUMN derived text "
			"GENERATED ALWAYS AS (payload || '-derived') STORED;",
		)
		wait_until(
			lambda: psql_conn(
				sub,
				"SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
				"FROM information_schema.columns "
				f"WHERE table_schema = 'public' AND table_name = '{table}' "
				"AND column_name IN ('added_later', 'discarded', 'derived');",
			) == "added_later,derived",
			desc="added, generated, and dropped columns applied to replica",
		)
		psql_conn(
			sub,
			f"INSERT INTO {table} VALUES "
			"(901, 'look-510058', 'replica-again', 'stale', 'stale-column');",
		)
		psql_conn(
			pub,
			f"INSERT INTO {table} "
			"(id, business_key, payload, nullable_value, added_later) "
			"VALUES (2, 'look-510058', 'source-v3', NULL, 'new-column') "
			"ON CONFLICT (business_key) DO UPDATE SET "
			"payload = EXCLUDED.payload, nullable_value = EXCLUDED.nullable_value, "
			"added_later = EXCLUDED.added_later;",
		)
		wait_until(
			lambda: psql_conn(
				sub,
				f"SELECT id, payload, nullable_value IS NULL, added_later, derived "
				f"FROM {table};",
			) == "2|source-v3|t|new-column|source-v3-derived",
			desc="conflicting insert copies column added after installation",
		)

		psql_conn(pub, f"DELETE FROM {table} WHERE business_key = 'look-510058';")
		wait_until(
			lambda: psql_conn(sub, f"SELECT count(*) FROM {table};") == "0",
			desc="delete after repaired insert",
		)

	def test_local_origin_upsert_keeps_postgres_semantics(self, pg_pair):
		sub = pg_pair["sub"]
		table = "replica_upsert_local_origin"
		_create_replica_upsert_table(pg_pair, table)

		psql_conn(sub, f"INSERT INTO {table} VALUES (700, 'local', 'v1', NULL);")
		psql_conn(
			sub,
			f"INSERT INTO {table} VALUES (701, 'local', 'v2', 'native') "
			"ON CONFLICT (business_key) DO UPDATE SET "
			"payload = EXCLUDED.payload, nullable_value = EXCLUDED.nullable_value;",
		)
		assert psql_conn(
			sub,
			f"SELECT id, payload, nullable_value FROM {table} WHERE business_key = 'local';",
		) == "700|v2|native"


class TestFailureBoundaries:
	def test_unrelated_unique_collision_rolls_back_whole_source_transaction(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		table = "replica_upsert_atomic"
		_create_replica_upsert_table(pg_pair, table, extra_unique=True)
		psql_conn(
			sub,
			f"INSERT INTO {table} "
			"(id, business_key, payload, nullable_value, external_key) VALUES "
			"(900, 'merge-me', 'before', NULL, 'replica-safe'), "
			"(901, 'replica-blocker', 'blocker', NULL, 'taken');",
		)
		log_offset = len(_subscriber_logs())

		# Both source rows are one transaction.  The first matches the configured
		# key; the second conflicts only on another unique constraint.
		psql_conn(
			pub,
			f"BEGIN; "
			f"INSERT INTO {table} VALUES "
			"(1, 'merge-me', 'after', NULL, 'source-safe'); "
			f"INSERT INTO {table} VALUES "
			"(2, 'source-new', 'second', NULL, 'taken'); "
			"COMMIT;",
		)
		try:
			_wait_for_log(f"{table}_external_key_key", since=log_offset)

			# The trigger must not swallow the unrelated collision, and PostgreSQL
			# must roll back the earlier trigger update from the same apply txn.
			assert psql_conn(
				sub,
				f"SELECT id, payload, external_key FROM {table} "
				"WHERE business_key = 'merge-me';",
			) == "900|before|replica-safe"
			assert psql_conn(
				sub,
				f"SELECT count(*) FROM {table} WHERE business_key = 'source-new';",
			) == "0"
		finally:
			# Heal deliberate drift even when an assertion fails, so the shared
			# session fixture never leaves its apply worker crash-looping.
			psql_conn(sub, f"DELETE FROM {table} WHERE business_key = 'replica-blocker';")
		wait_until(
			lambda: psql_conn(
				sub,
				f"SELECT count(*) FROM {table} "
				"WHERE (id = 1 AND payload = 'after') "
				"OR (id = 2 AND business_key = 'source-new');",
			) == "2",
			timeout=60,
			desc="failed source transaction retried atomically",
		)

	def test_install_rejects_unsafe_constraint_shapes(self, pg_pair):
		sub = pg_pair["sub"]
		psql_conn(
			sub,
			"CREATE TABLE replica_upsert_nullable ("
			"id bigint PRIMARY KEY, business_key text UNIQUE); "
			"CREATE TABLE replica_upsert_deferrable ("
			"id bigint PRIMARY KEY, business_key text NOT NULL, "
			"CONSTRAINT replica_upsert_deferrable_key "
			"UNIQUE (business_key) DEFERRABLE INITIALLY IMMEDIATE);",
		)

		with pytest.raises((ValueError, RuntimeError)):
			_install("replica_upsert_nullable", "replica_upsert_nullable_business_key_key")
		with pytest.raises((ValueError, RuntimeError)):
			_install("replica_upsert_deferrable", "replica_upsert_deferrable_key")

	def test_install_rejects_incoming_foreign_key_and_partitioned_table(self, pg_pair):
		sub = pg_pair["sub"]
		psql_conn(
			sub,
			"CREATE TABLE replica_upsert_fk_target ("
			"id bigint PRIMARY KEY, business_key text NOT NULL, "
			"CONSTRAINT replica_upsert_fk_target_key UNIQUE (business_key)); "
			"CREATE TABLE replica_upsert_fk_child ("
			"id bigint PRIMARY KEY, target_key text REFERENCES "
			"replica_upsert_fk_target (business_key)); "
			"CREATE TABLE replica_upsert_partitioned ("
			"id bigint NOT NULL, business_key text NOT NULL, "
			"CONSTRAINT replica_upsert_partitioned_key UNIQUE (business_key, id)"
			") PARTITION BY RANGE (id);",
		)

		with pytest.raises((ValueError, RuntimeError)):
			_install("replica_upsert_fk_target", "replica_upsert_fk_target_key")
		with pytest.raises((ValueError, RuntimeError)):
			_install("replica_upsert_partitioned", "replica_upsert_partitioned_key")


class TestRecoveryAndRemoval:
	def test_initial_copy_merges_an_existing_replica_row(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		table = "replica_upsert_initial_copy"
		psql_conn(
			pub,
			f"CREATE TABLE {table} ("
			"id bigint PRIMARY KEY, business_key text NOT NULL, payload text, "
			f"CONSTRAINT {table}_business_key_key UNIQUE (business_key));",
		)
		wait_until(
			lambda: psql_conn(sub, f"SELECT to_regclass('public.{table}') IS NOT NULL;") == "t",
			desc="initial-copy table schema replicated",
		)
		psql_conn(pub, f"INSERT INTO {table} VALUES (1, 'same-key', 'source-copy');")
		_install(table, f"{table}_business_key_key")
		psql_conn(sub, f"INSERT INTO {table} VALUES (900, 'same-key', 'replica-old');")

		# REFRESH starts the table-sync COPY. It must use the same REPLICA
		# trigger semantics as the steady-state apply worker.
		psql_conn(sub, f"ALTER SUBSCRIPTION {E2E_SUBSCRIPTION} REFRESH PUBLICATION;")
		try:
			wait_until(
				lambda: psql_conn(sub, f"SELECT id, payload FROM {table};") == "1|source-copy",
				timeout=60,
				desc="initial COPY merged replica-only row",
			)
		finally:
			# A failed table-sync worker retries independently. Remove deliberate
			# drift so even a failed assertion cannot leave that worker looping.
			if psql_conn(sub, f"SELECT count(*) FROM {table} WHERE id = 900;") == "1":
				psql_conn(sub, f"DELETE FROM {table} WHERE id = 900;")
				wait_until(
					lambda: psql_conn(sub, f"SELECT count(*) FROM {table} WHERE id = 1;") == "1",
					timeout=60,
					desc="initial COPY cleanup resumed table sync",
				)

	def test_install_recovers_an_already_stalled_insert(self, pg_pair):
		pub, sub = pg_pair["pub"], pg_pair["sub"]
		table = "replica_upsert_recovery"
		psql_conn(
			pub,
			f"CREATE TABLE {table} ("
			"id bigint PRIMARY KEY, business_key text NOT NULL, payload text, "
			f"CONSTRAINT {table}_business_key_key UNIQUE (business_key));",
		)
		wait_until(
			lambda: psql_conn(sub, f"SELECT to_regclass('public.{table}') IS NOT NULL;") == "t",
			desc="recovery table schema replicated",
		)
		psql_conn(sub, f"ALTER SUBSCRIPTION {E2E_SUBSCRIPTION} REFRESH PUBLICATION;")
		wait_until(
			lambda: psql_conn(
				sub,
				"SELECT count(*) FROM pg_subscription_rel sr "
				"JOIN pg_class c ON c.oid = sr.srrelid "
				f"WHERE c.relname = '{table}' AND sr.srsubstate IN ('r', 's');",
			) == "1",
			timeout=60,
			desc="recovery table subscription ready",
		)
		psql_conn(sub, f"INSERT INTO {table} VALUES (900, 'same-key', 'replica-old');")
		log_offset = len(_subscriber_logs())
		psql_conn(pub, f"INSERT INTO {table} VALUES (1, 'same-key', 'source');")
		try:
			_wait_for_log(f"{table}_business_key_key", since=log_offset)
			assert psql_conn(sub, f"SELECT id, payload FROM {table};") == "900|replica-old"

			_install(table, f"{table}_business_key_key")
			wait_until(
				lambda: psql_conn(sub, f"SELECT id, payload FROM {table};") == "1|source",
				timeout=60,
				desc="stalled INSERT retried after trigger installation",
			)
		finally:
			# If installation or the assertion failed, removing the local conflict
			# still lets PostgreSQL retry the source INSERT and resumes the fixture.
			if psql_conn(
				sub,
				f"SELECT count(*) FROM {table} WHERE id = 900 AND business_key = 'same-key';",
			) == "1":
				psql_conn(sub, f"DELETE FROM {table} WHERE id = 900;")
				wait_until(
					lambda: psql_conn(sub, f"SELECT count(*) FROM {table} WHERE id = 1;") == "1",
					timeout=60,
					desc="recovery test cleanup resumed subscription",
				)

	def test_remove_restores_replica_role_unique_violation(self, pg_pair):
		sub = pg_pair["sub"]
		table = "replica_upsert_remove"
		_create_replica_upsert_table(pg_pair, table)
		psql_conn(sub, f"INSERT INTO {table} VALUES (900, 'same-key', 'old', NULL);")
		psql_conn(
			sub,
			f"SET session_replication_role = replica; "
			f"INSERT INTO {table} VALUES (1, 'same-key', 'merged', NULL);",
		)
		assert psql_conn(sub, f"SELECT id, payload FROM {table};") == "1|merged"

		_remove(table)
		with pytest.raises(RuntimeError, match=f"{table}_business_key_key"):
			psql_conn(
				sub,
				f"SET session_replication_role = replica; "
				f"INSERT INTO {table} VALUES (2, 'same-key', 'must-fail', NULL);",
			)
		assert psql_conn(sub, f"SELECT id, payload FROM {table};") == "1|merged"
