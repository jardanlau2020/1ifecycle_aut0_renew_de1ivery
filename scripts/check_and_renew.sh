#!/usr/bin/env bash
# ============================================================
# MonkeyBytes (monkey-network.xyz) 自动续约脚本
# Auto-renew for MonkeyBytes free Discord bot hosting
# 原理：Pterodactyl Client API（API Key 认证，无需过 CAPTCHA）
#   1. GET  .../lifecycle      -> 检查 can_confirm / days_remaining
#   2. can_confirm=true        -> POST .../lifecycle/confirm（+15 日）
# 用法：MONKEY_API_KEY=xxx [SERVER_ID=2533c753] bash check_and_renew.sh
# ============================================================
set -euo pipefail

API_KEY="${MONKEY_API_KEY:?Error: MONKEY_API_KEY env var is not set}"
SERVER_ID="${SERVER_ID:-2533c753}"
BASE="https://dash.monkey-network.xyz/api/client/servers/${SERVER_ID}"

echo "== MonkeyBytes auto-renew check (server ${SERVER_ID}) =="

LIFECYCLE=$(curl -sS -f -H "Authorization: Bearer ${API_KEY}" -H "Accept: application/json" "${BASE}/lifecycle")
echo "Lifecycle: ${LIFECYCLE}"

CAN_CONFIRM=$(echo "${LIFECYCLE}" | jq -r '.can_confirm')
DAYS_REMAINING=$(echo "${LIFECYCLE}" | jq -r '.days_remaining')

if [ "${CAN_CONFIRM}" = "true" ]; then
  echo "can_confirm=true (${DAYS_REMAINING} days remaining) -> confirming server now..."
  RESP=$(curl -sS -X POST \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -d '{}' \
    "${BASE}/lifecycle/confirm")
  echo "Confirm response: ${RESP}"
  if echo "${RESP}" | jq -e 'has("error")' >/dev/null 2>&1; then
    echo "!! Confirm failed: ${RESP}"
    exit 1
  fi
  echo "== Server confirmed! New state: =="
  curl -sS -f -H "Authorization: Bearer ${API_KEY}" -H "Accept: application/json" "${BASE}/lifecycle"
  echo ""
else
  echo "Not confirmable yet (${DAYS_REMAINING} days remaining, need <= 7). No action needed."
fi