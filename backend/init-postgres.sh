#!/bin/bash
# Create the sinas_data database for user data (Database Connections, DDL, CDC).
# This runs once on first Postgres initialization.
#
# Skipped when BUILTIN_DATABASE_ENABLED is false (docker-compose.yml passes
# it into the postgres service), matching the backend, which then doesn't
# create sinas_data or its Database Connection either. Same false spellings
# as the backend's boolean setting. No `exit`: the postgres entrypoint
# sources a script that isn't executable, and an exit would end it.
set -e

case "$(echo "${BUILTIN_DATABASE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
    false|0|no|off|f|n)
        echo "BUILTIN_DATABASE_ENABLED=false: not creating the sinas_data database"
        ;;
    *)
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE sinas_data'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'sinas_data')\gexec
EOSQL
        ;;
esac
