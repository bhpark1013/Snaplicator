select slot_name,
       plugin,
       slot_type,
       active,
       active_pid,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as retained_wal
from pg_replication_slots
where slot_name = 'snaplicator_subscription';
