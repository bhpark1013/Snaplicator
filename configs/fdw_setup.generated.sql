-- AUTO-GENERATED from configs/fdw.yaml. DO NOT EDIT BY HAND.
-- Regenerate via POST /replication/fdw/regenerate.
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

-- Foreign server: prod_fdw
DROP SERVER IF EXISTS "prod_fdw" CASCADE;
CREATE SERVER "prod_fdw" FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host :'primary_host', port :'primary_port', dbname :'primary_db', sslmode 'require', fetch_size '10000', use_remote_estimate 'true');
CREATE USER MAPPING FOR CURRENT_USER SERVER "prod_fdw" OPTIONS (user :'fdw_user', password :'fdw_password');

-- Table-level FDW: etl (5 table(s))
CREATE SCHEMA IF NOT EXISTS "etl";
DO $fdw_drop$ DECLARE k char; BEGIN SELECT c.relkind INTO k FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'etl' AND c.relname = 'curator_profile__curators_v1'; IF FOUND THEN IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE "etl"."curator_profile__curators_v1" CASCADE'; ELSIF k = 'r' THEN EXECUTE 'DROP TABLE "etl"."curator_profile__curators_v1" CASCADE'; END IF; END IF; END $fdw_drop$;
DO $fdw_drop$ DECLARE k char; BEGIN SELECT c.relkind INTO k FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'etl' AND c.relname = 'curator_profile__brand_curators_v1'; IF FOUND THEN IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE "etl"."curator_profile__brand_curators_v1" CASCADE'; ELSIF k = 'r' THEN EXECUTE 'DROP TABLE "etl"."curator_profile__brand_curators_v1" CASCADE'; END IF; END IF; END $fdw_drop$;
DO $fdw_drop$ DECLARE k char; BEGIN SELECT c.relkind INTO k FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'etl' AND c.relname = 'curator_profile__brand_curators__iv_raw_v1'; IF FOUND THEN IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE "etl"."curator_profile__brand_curators__iv_raw_v1" CASCADE'; ELSIF k = 'r' THEN EXECUTE 'DROP TABLE "etl"."curator_profile__brand_curators__iv_raw_v1" CASCADE'; END IF; END IF; END $fdw_drop$;
DO $fdw_drop$ DECLARE k char; BEGIN SELECT c.relkind INTO k FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'etl' AND c.relname = 'curator_profile__brand_curators__iv_scaled_v1'; IF FOUND THEN IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE "etl"."curator_profile__brand_curators__iv_scaled_v1" CASCADE'; ELSIF k = 'r' THEN EXECUTE 'DROP TABLE "etl"."curator_profile__brand_curators__iv_scaled_v1" CASCADE'; END IF; END IF; END $fdw_drop$;
DO $fdw_drop$ DECLARE k char; BEGIN SELECT c.relkind INTO k FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid WHERE n.nspname = 'etl' AND c.relname = 'curator_profile__brand_curators__iv_normalized_v1'; IF FOUND THEN IF k = 'f' THEN EXECUTE 'DROP FOREIGN TABLE "etl"."curator_profile__brand_curators__iv_normalized_v1" CASCADE'; ELSIF k = 'r' THEN EXECUTE 'DROP TABLE "etl"."curator_profile__brand_curators__iv_normalized_v1" CASCADE'; END IF; END IF; END $fdw_drop$;
IMPORT FOREIGN SCHEMA "etl" LIMIT TO ("curator_profile__curators_v1", "curator_profile__brand_curators_v1", "curator_profile__brand_curators__iv_raw_v1", "curator_profile__brand_curators__iv_scaled_v1", "curator_profile__brand_curators__iv_normalized_v1") FROM SERVER "prod_fdw" INTO "etl" OPTIONS (import_collate 'false', import_default 'false');

