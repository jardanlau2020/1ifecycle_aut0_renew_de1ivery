#!/usr/bin/env bash
# ============================================================
# MonkeyBytes (monkey-network.xyz) 自动续约脚本
# Auto-renew for MonkeyBytes free Discord bot hosting
# 原理：Pterodactyl Client API（API Key 认证，无需过 CAPTCHA）
#   1. GET  .../lifecycle      -> 检查 can_confirm / days_remaining
#   2. can_confirm=true        -> POST .../lifecycle/confirm（+15 日）
#
# 2026-09-23 修：
#   · SERVER_ID 不再写死。旧值 2533c753（曼谷机）已被平台删除，09-18~09-22 连续 5 单
#     全部 404 死掉；现由 workflow 用 secrets.MONKEY_SERVER_IDENTIFIER 注入。
#   · 目标 404 时自动列出账号下现役服务器（identifier / 名字 / 是否 suspended），
#     方便一眼换 ID，不用再盲猜。
# 用法：MONKEY_API_KEY=xxx SERVER_ID=xxxxxxxx bash check_and_renew.sh
# ============================================================
set -uo pipefail

API_KEY="${MONKEY_API_KEY:?Error: MONKEY_API_KEY env var is not set}"
SERVER_ID="${SERVER_ID:?Error: SERVER_ID env var is not set (面板服务器 8 位短 ID)}"
CLIENT_API="https://dash.monkey-network.xyz/api/client"
BASE="${CLIENT_API}/servers/${SERVER_ID}"
LIFECYCLE_TMP="$(mktemp)"

hdr=(-H "Authorization: Bearer ${API_KEY}" -H "Accept: application/json")

list_servers() {
  echo "-- 账号下现役服务器 --"
  local body
  if ! body="$(curl -sS "${hdr[@]}" "${CLIENT_API}")"; then
    echo "   （拉 /api/client 也失败，API key 可能已失效/被撤销）"
    return 0
  fi
  if echo "${body}" | jq -e '.data' >/dev/null 2>&1; then
    echo "${body}" | jq -r '.data[]?.attributes | "   \(.identifier)   \(.name)   suspended=\(.is_suspended // "?")"'
  else
    echo "   （返回非服务器列表）${body:0:400}"
  fi
}

echo "== MonkeyBytes auto-renew check (server ${SERVER_ID}) =="

HTTP_CODE="$(curl -sS -o "${LIFECYCLE_TMP}" -w '%{http_code}' "${hdr[@]}" "${BASE}/lifecycle" || echo 000)"
if [ "${HTTP_CODE}" != "200" ]; then
  echo "!! lifecycle 接口回 HTTP ${HTTP_CODE}"
  cat "${LIFECYCLE_TMP}" 2>/dev/null; echo
  [ "${HTTP_CODE}" = "404" ] && echo "!! 404 通常 = 该 server ID 已不存在（被删/重建），要换新 ID"
  list_servers
  exit 1
fi

LIFECYCLE="$(cat "${LIFECYCLE_TMP}")"
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
  curl -sS "${hdr[@]}" "${BASE}/lifecycle"
  echo ""
else
  echo "Not confirmable yet (${DAYS_REMAINING} days remaining, need <= 7). No action needed."
fi