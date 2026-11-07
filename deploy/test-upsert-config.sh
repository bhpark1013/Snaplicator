#!/usr/bin/env bash
set -eu
SRC=${1:-"$(cd "$(dirname "$0")" && pwd)/install.sh"}

die() { printf '%s\n' "$*" >&2; return 1; }
eval "$(awk '/^env_json_mapping\(\) \{/,/^\}/' "$SRC")"
eval "$(awk '/^configure_replica_upsert_tables\(\) \{/,/^\}/' "$SRC")"

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT
env_file=$tmp_dir/.env

REPLICA_UPSERT_TABLES_SET=""
unset REPLICA_UPSERT_TABLES
configure_replica_upsert_tables "$env_file"
[ "$REPLICA_UPSERT_TABLES" = '{}' ]

printf '%s\n' 'REPLICA_UPSERT_TABLES='\''{"public.cache":"cache_key_unique"}'\''' > "$env_file"
unset REPLICA_UPSERT_TABLES
configure_replica_upsert_tables "$env_file"
[ "$REPLICA_UPSERT_TABLES" = '{"public.cache":"cache_key_unique"}' ]

REPLICA_UPSERT_TABLES_SET=x
REPLICA_UPSERT_TABLES='{}'
configure_replica_upsert_tables "$env_file"
[ "$REPLICA_UPSERT_TABLES" = '{}' ]

quoted="REPLICA_UPSERT_TABLES='$REPLICA_UPSERT_TABLES'"
eval "$quoted"
[ "$REPLICA_UPSERT_TABLES" = '{}' ]

printf '4 passed, 0 failed\n'
