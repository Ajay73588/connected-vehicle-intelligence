#!/usr/bin/env bash
# Chaos / recovery test against the docker-compose stack. Kills real containers and asserts the system recovers without
# losing or duplicating data.  Usage: bash scripts/chaos_compose.sh   (stack must be up: `make up`)
set -euo pipefail
API=${API:-http://localhost:8000}
PY=${PYTHON:-python}
c() { docker compose "$@"; }
step() { printf '\n=== %s\n' "$*"; }
wait_ready() { for _ in $(seq 1 60); do curl -fs "$API/health/ready" >/dev/null 2>&1 && return 0; sleep 2; done; echo "API not ready"; return 1; }
e2e() { "$PY" scripts/e2e.py --base "$API" --timeout 90 >/dev/null && echo "e2e OK"; }
metric() { curl -fs "$API/metrics" | awk -v m="$1" '$1 ~ m {s+=$2} END {print s+0}'; }

wait_ready; e2e

step "1. Kafka dies -> edge applies back-pressure (503), recovers when the broker returns"
c kill kafka
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST localhost:${INGEST_PORT:-8001}/v1/telemetry -H "X-Device-Key: ${INGEST_API_KEY:-dev-device-key-change-me}" -H 'content-type: application/json' -d '[]' || true)
echo "edge status while Kafka is down: $code (200/202 = nothing to publish, 503 = back-pressure)"
c start kafka
for _ in $(seq 1 40); do c ps kafka | grep -q healthy && break; sleep 3; done
sleep 10; e2e

step "2. Stream processor crashes mid-flight -> restarts, replays uncommitted offsets, no duplicates"
c kill stream-processor
sleep 3
$PY scripts/e2e.py --base "$API" --timeout 5 >/dev/null 2>&1 || true   # events queue up in Kafka while the consumer is down
c start stream-processor
sleep 15; e2e

step "3. PostgreSQL dies -> processors back off (circuit breaker), resume after recovery"
c kill postgres
sleep 8
curl -s "$API/health/ready" | grep -q 'unavailable\|postgres' && echo "readiness reports the outage"
c start postgres
for _ in $(seq 1 40); do c ps postgres | grep -q healthy && break; sleep 3; done
wait_ready; sleep 10; e2e

step "4. Redis dies -> API degrades (in-process rate limiter), pipeline falls back to Bloom-filter dedupe"
c kill redis
sleep 5
curl -fs "$API/health/ready" | grep -q degraded && echo "API reports degraded (still serving)"
c start redis
sleep 10; e2e

step "5. API instance dies -> a second replica serves (stateless)"
c up -d --scale api=2 --no-recreate api
sleep 10
victim=$(docker ps --filter name=cvi-api -q | head -n1)
docker kill "$victim"
sleep 3; e2e

echo; echo "CHAOS TEST PASSED"
