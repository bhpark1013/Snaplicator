-- Replication verification query (EXAMPLE / TEMPLATE)
--
-- This query is run on BOTH the publisher and the subscriber; the two
-- result sets are compared to confirm replication is healthy.
--
-- The real query is environment-specific (it depends on which tables you
-- replicate), so it is NOT tracked in git. Copy this file to
-- `configs/replication_check.sql` (or set CHECK_SQL_PATH) and edit it, or
-- edit it from the web UI at /replication.
--
-- Requirements:
--   * MUST be read-only (SELECT only). Writes/DDL are rejected on save and
--     execution is additionally wrapped in a READ ONLY transaction.
--   * Keep it cheap — it runs on every replication-check poll.
--
-- Examples:

-- 1) Row count of a core table that should converge on both sides:
select count(*) as customers from public.customers;

-- 2) Freshness check (latest row timestamp):
-- select max(created_time) as latest from public.some_event_table;

-- 3) Lightweight checksum across a small dimension table:
-- select count(*) as n, sum(hashtext(t::text)) as digest from public.some_lookup t;
