# Main replica access

The main replica accepts TCP connections only as `snaplicator_readonly`.
Snaplicator management uses the existing `snaplicator` account through the
container's Unix socket. Writable clones retain their original access rules.

Apply `scripts/harden-main-replica.py` on the Docker host. Its HBA file must
live outside the replica data directory. Set `REPLICA_HBA_FILE` to that host
path in the deployment environment so subsequent main-container creation
uses the same rules. The manager must also be able to read that path; a path
under its shared `/data/snaplicator` mount works.

The script writes connection credentials to `main-readonly.json` in its state
directory with mode `0600`. Do not commit or paste that file into logs. Set a
distinct `application_name` on each client's connection string. A shared
account identifies a connection by application and IP, not a person's identity.

Logging records connections, disconnections and modifying SQL, with timestamp,
role, database, application, client address, session ID and transaction ID.
These logs can contain data values, so keep them restricted to operators.
Logical apply errors retain the source transaction and LSN in their context.

```sh
docker logs --since 10m snaplicator_replica
```

The hardening script keeps the stopped original container for rollback and
historical logs. Never start it while the replacement is running: both mount
the same data directory.

