-- Replication Check: Compare row counts between Publisher and Subscriber
-- Run on both sides to verify data sync

SELECT
    schemaname AS schema,
    relname AS table_name,
    n_live_tup::text AS estimated_rows
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY relname;
