#!/usr/bin/env bash
# Smoke test: live + retain + recall against a running astrocyte-rest (e.g. after `make runbook`).
set -euo pipefail

: "${ASTROCYTES_HTTP_PUBLISH_PORT:=8080}"
BASE_URL="${ASTROCYTES_SMOKE_BASE_URL:-http://127.0.0.1:${ASTROCYTES_HTTP_PUBLISH_PORT}}"
PRINCIPAL="${ASTROCYTES_SMOKE_PRINCIPAL:-agent:smoke}"
BANK_ID="${ASTROCYTES_SMOKE_BANK_ID:-smoke-bank}"

echo "GET ${BASE_URL}/live"
curl -fsS --max-time 10 "${BASE_URL}/live"

echo
echo "POST ${BASE_URL}/v1/retain"
curl -fsS --max-time 60 -X POST "${BASE_URL}/v1/retain" \
  -H "Content-Type: application/json" \
  -H "X-Astrocyte-Principal: ${PRINCIPAL}" \
  -d "{\"content\":\"smoke retain $(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"bank_id\":\"${BANK_ID}\"}"

echo
echo "POST ${BASE_URL}/v1/recall"
curl -fsS --max-time 60 -X POST "${BASE_URL}/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Astrocyte-Principal: ${PRINCIPAL}" \
  -d "{\"query\":\"smoke\",\"bank_id\":\"${BANK_ID}\",\"max_results\":5}"

echo
echo "smoke-rest: ok"
