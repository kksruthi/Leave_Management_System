#!/usr/bin/env bash
# Convenience: start a throwaway Postgres 16 for local development.
# Requires Docker. If you already run Postgres, just set DATABASE_URL instead.
set -euo pipefail

NAME=leave-engine-db

case "${1:-up}" in
  up)
    docker run -d --name "$NAME" \
      -e POSTGRES_USER=leave \
      -e POSTGRES_PASSWORD=leave \
      -e POSTGRES_DB=leave_engine \
      -p 5432:5432 postgres:16
    echo "Waiting for Postgres..."
    until docker exec "$NAME" pg_isready -U leave -d leave_engine >/dev/null 2>&1; do sleep 1; done
    echo "Ready: postgresql+psycopg2://leave:leave@localhost:5432/leave_engine"
    ;;
  down)
    docker rm -f "$NAME"
    ;;
  *)
    echo "usage: $0 [up|down]" >&2
    exit 1
    ;;
esac
