#!/bin/bash
# Runs once, the first time the Postgres container starts with an empty data volume.
# Creates the databases and the two logins:
#   harborline_app  - used by the simulator (owns everything)
#   etl_reader      - YOUR login: read-only on wms / ax2012 / iot / tracker, with REPLICATION for CDC
#   uat_reviewer    - Iris Novak's login for dashboard reviews: read-only on wms / ax2012 / iot (not yours to use)
# The app re-applies all of this (and the table grants) every time it starts.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-EOSQL
  CREATE ROLE harborline_app LOGIN PASSWORD '${HL_APP_PASSWORD}';
  CREATE ROLE etl_reader LOGIN REPLICATION PASSWORD '${HL_ETL_PASSWORD}';
  CREATE ROLE uat_reviewer LOGIN PASSWORD '${HL_REVIEWER_PASSWORD:-uat-review-2026}';
  CREATE DATABASE wms          OWNER harborline_app;
  CREATE DATABASE ax2012       OWNER harborline_app;
  CREATE DATABASE iot          OWNER harborline_app;
  CREATE DATABASE tracker      OWNER harborline_app;
  CREATE DATABASE sim_internal OWNER harborline_app;
  REVOKE CONNECT ON DATABASE sim_internal FROM PUBLIC;
EOSQL

for db in wms ax2012 iot tracker; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<-EOSQL
    GRANT CONNECT ON DATABASE $db TO etl_reader;
EOSQL
done

for db in wms ax2012 iot; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "GRANT CONNECT ON DATABASE $db TO uat_reviewer;"
done
