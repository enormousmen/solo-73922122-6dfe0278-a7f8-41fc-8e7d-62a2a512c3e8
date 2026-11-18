#!/usr/bin/env bash
# Demo: shared diagnostic SQL service.
# Prereq: service running (docker compose up, or local uvicorn) at $BASE.
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"
JSON='content-type: application/json'

pp() { python3 -m json.tool 2>/dev/null || cat; }
jid() { python3 -c "import json,sys;print(json.load(sys.stdin)['job_id'])"; }

# wait for a job to reach a terminal state (demo jobs are asynchronous)
wait_job() {  # $1 = job_id
  local id="$1" s
  for _ in $(seq 1 60); do
    s=$(curl -s "$BASE/jobs/$id" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
    case "$s" in succeeded|failed|cancelled) return 0;; esac
    sleep 0.5
  done
  echo "warning: job $id did not finish in time" >&2
}

echo "=== 1) submit a 3-statement job; statements run in order, autocommitted ==="
JOB=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{
  "statements": [
    "CREATE TEMP TABLE demo_t (a int)",
    "INSERT INTO demo_t VALUES (1),(2),(3)",
    "SELECT sum(a) AS total FROM demo_t"
  ]}' | jid)
echo "job_id=$JOB"
sleep 1
curl -s "$BASE/jobs/$JOB" | pp

echo
echo "=== 2) session state does NOT leak to the next job on the same connection ==="
JOB=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{
  "statements": [
    "SET application_name = '"'"'leaky'"'"'",
    "CREATE TEMP TABLE leak_t (x int)",
    "SELECT pg_advisory_lock(99)",
    "SELECT 1"
  ]}' | jid)
sleep 1
CONN=$(curl -s "$BASE/jobs/$JOB" | python3 -c "import json,sys;print(json.load(sys.stdin)['connection_id'])")
echo "dirty job ran on connection $CONN; submitting a check job (both conns may serve it;"
echo "run the demo twice or occupy the other conn to force the same one)"
JOB2=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{
  "statements": [
    "SHOW application_name",
    "SELECT count(*) AS temp_objects FROM pg_class WHERE relnamespace = pg_my_temp_schema()",
    "SELECT pg_try_advisory_lock(99) AS lock_was_free"
  ]}' | jid)
sleep 1
curl -s "$BASE/jobs/$JOB2" | pp

echo
echo "=== 3) cancel a running job; cancel is accepted immediately ==="
JOB=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{
  "statements": ["SELECT 1", "SELECT pg_sleep(60)", "SELECT 3"]}' | jid)
sleep 1
echo "-- cancel with a 3s delayed send (test hook delay_ms); the API answers at once:"
time curl -s -XPOST "$BASE/jobs/$JOB/cancel" -H "$JSON" -d '{"delay_ms": 3000}' | pp
echo "-- the running statement is interrupted once the cancel is sent (~3s),"
echo "-- statement 3 is skipped, statement 1 stays committed:"
sleep 5
curl -s "$BASE/jobs/$JOB" | pp

echo
echo "=== 4) slow cleanup (test hook) does not block the other connection ==="
JOB=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{
  "statements": ["SELECT 1"],
  "options": {"cleanup_delay_ms": 6000}}' | jid)
sleep 2
echo "-- pool while connection cleans up (state=cleaning):"
curl -s "$BASE/pool" | pp
JOB2=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{"statements": ["SELECT 2 AS still_fast"]}' | jid)
sleep 1
echo "-- a job on the OTHER connection finished meanwhile:"
curl -s "$BASE/jobs/$JOB2" | pp

echo
echo "=== 5) late cancel after natural finish is a no-op (409) ==="
JOB=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{"statements": ["SELECT 1"]}' | jid)
sleep 1
curl -s -o /dev/null -w "cancel after finish -> HTTP %{http_code}\n" \
  -XPOST "$BASE/jobs/$JOB/cancel" -H "$JSON" -d '{}'

echo
echo "=== 6) explicit transaction control is rejected (422) ==="
curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{"statements": ["BEGIN", "SELECT 1"]}' | pp

echo
echo "=== 7) logical replication: can UPDATE/DELETE be published from the publisher? ==="
echo "-- setup: one table with PK, one without; a plain publication, an"
echo "-- insert-only publication, and a publication with a column list"
SETUP=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{"statements": [
  "DROP PUBLICATION IF EXISTS demo_pub_ok",
  "DROP PUBLICATION IF EXISTS demo_pub_ins",
  "DROP PUBLICATION IF EXISTS demo_pub_cols",
  "DROP TABLE IF EXISTS public.demo_lr_ok",
  "DROP TABLE IF EXISTS public.demo_lr_nopk",
  "CREATE TABLE public.demo_lr_ok (id int PRIMARY KEY, v int)",
  "CREATE TABLE public.demo_lr_nopk (a int, b int)",
  "CREATE PUBLICATION demo_pub_ok FOR TABLE public.demo_lr_ok, public.demo_lr_nopk",
  "CREATE PUBLICATION demo_pub_ins FOR TABLE public.demo_lr_ok WITH (publish = '"'"'insert'"'"')",
  "CREATE PUBLICATION demo_pub_cols FOR TABLE public.demo_lr_ok (v)"
]}' | jid)
wait_job "$SETUP"
echo "-- table with PK (note: demo_pub_cols omits the PK column -> blocked):"
curl -s "$BASE/logical-replication/check/public/demo_lr_ok" | pp
echo "-- table without PK (replica identity DEFAULT, no primary key -> blocked):"
curl -s "$BASE/logical-replication/check/public/demo_lr_nopk" | pp
echo "-- cleanup:"
CLEAN=$(curl -s -XPOST "$BASE/jobs" -H "$JSON" -d '{"statements": [
  "DROP PUBLICATION IF EXISTS demo_pub_ok",
  "DROP PUBLICATION IF EXISTS demo_pub_ins",
  "DROP PUBLICATION IF EXISTS demo_pub_cols",
  "DROP TABLE IF EXISTS public.demo_lr_ok",
  "DROP TABLE IF EXISTS public.demo_lr_nopk"
]}' | jid)
wait_job "$CLEAN"
echo done
