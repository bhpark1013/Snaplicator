#!/bin/bash
set -euo pipefail

# DB 준비 대기만 담당
echo "Waiting for PostgreSQL to be ready..."
until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  sleep 1
done
echo "PostgreSQL is ready"
