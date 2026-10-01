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
#
# 2026-10-01 修（transient 误判）：
#   · 原逻辑：面板 5xx（Cloudflare 522/502/503）或连不上时，账号接口取不到数据，
#     COUNT 为空字符串，`[ "" = "0" ]` 为假 → 直接 exit 1 标红。
#     实际那只是上游临时故障，不是脚本坏，天天标红纯噪音。
#   · 现改为：curl 统一走 curl_retry（对 000/5xx 退避重试 3 次：5s/15s/30s）；
#     重试后仍失败 → 打 TRANSIENT 标记并 exit 0（日更 + 7 日窗口，漏一次不影响续期）。
#   · 「0 台服务器 → exit 0」的判定，只在账号接口真返回 200 时才执行，
#     避免把上游故障误判成空账号。
#
# 用法：MONKEY_API_KEY=xxx SERVER_ID=xxxxxxxx bash check_and_renew.sh
# ============================================================
set -uo pipefail

API_KEY="${MONKEY_API_KEY:?Error: MONKEY_API_KEY env var is not set}"
SERVER_ID="${SERVER_ID:?Error: SERVER_ID env var is not set (面板服务器 8 位短 ID)}"
CLIENT_API="https://dash.monkey-network.xyz/api/client"
BASE="${CLIENT_API}/servers/${SERVER_ID}"
LIFECYCLE_TMP="$(mktemp)"

hdr=(-H "Authorization: Bearer ${API_KEY}" -H "Accept: application/json")

# curl 带退避重试：仅对 000（连接失败）与 5xx（上游故障）重试
# 用法: curl_retry <outfile> <url> [curl args...]  ->  stdout 输出最终 HTTP 码
curl_retry() {
  local out="$1"; shift
  local url="$1"; shift
  local waits=(5 15 30)
  local code i
  for i in 0 1 2; do
    code="$(curl -sS -o "$out" -w '%{http_code}' "$@" "$url" 2>/dev/null)" || code=000
    case "$code" in
      000|5*) : ;;
      *) printf '%s\n' "$code"; return 0 ;;
    esac
    if [ "$i" -lt 2 ]; then
      printf '   (HTTP %s, retry in %ss)\n' "$code" "${waits[$i]}" >&2
      sleep "${waits[$i]}"
    fi
  done
  printf '%s\n' "$code"
  return 0
}

list_servers() {
  echo "-- 账号下现役服务器 --"
  local body code
  code="$(curl_retry /tmp/acct.json "${CLIENT_API}" "${hdr[@]}")"
  body="$(cat /tmp/acct.json 2>/dev/null || true)"
  echo "   GET /api/client -> HTTP ${code}, ${#body} bytes"
  echo "   raw: ${body:0:1200}"
  echo "${body}" | jq -r '.data[]?.attributes | "   id=\(.identifier)  name=\(.name)  suspended=\(.is_suspended // "?")"' 2>/dev/null \
    || echo "   （jq 解析失败，见上面 raw）"
}

echo "== MonkeyBytes auto-renew check (server ${SERVER_ID}) =="

HTTP_CODE="$(curl_retry "${LIFECYCLE_TMP}" "${BASE}/lifecycle" "${hdr[@]}")"
if [ "${HTTP_CODE}" != "200" ]; then
  echo "!! lifecycle 接口回 HTTP ${HTTP_CODE}"
  cat "${LIFECYCLE_TMP}" 2>/dev/null; echo

  # 上游临时故障：不是脚本问题，日更 + 7 日窗口足够，exit 0 不标红
  case "${HTTP_CODE}" in
    000|5*)
      echo "⚠️ TRANSIENT: 面板上游不可用（HTTP ${HTTP_CODE}），本次跳过，等下一次排程。"
      echo "   （522/502/503 = Cloudflare 源站故障，非脚本错误）"
      exit 0 ;;
  esac

  [ "${HTTP_CODE}" = "404" ] && echo "!! 404 通常 = 该 server ID 已不存在（被删/重建），要换新 ID"
  list_servers

  # 只有账号接口真返回 200 才判断「0 台」，否则别把故障误判成空账号
  ACC_CODE="$(curl_retry /tmp/acct.json "${CLIENT_API}" "${hdr[@]}")"
  if [ "${ACC_CODE}" != "200" ]; then
    echo "⚠️ TRANSIENT: 账号接口 HTTP ${ACC_CODE}，无法判定，本次跳过（exit 0）"
    exit 0
  fi
  COUNT="$(jq -r '.meta.pagination.total // (.data | length) // empty' /tmp/acct.json 2>/dev/null)"
  if [ "${COUNT}" = "0" ]; then
    echo "ℹ️ 帳號下 0 台伺服器 → 冇嘢可以續，收工（exit 0）"
    echo "   要恢復自動續期：去 dash.monkey-network.xyz 開返機，再更新 secret"
    echo "   MONKEY_SERVER_IDENTIFIER = 新機嘅 8 位 identifier。"
    exit 0
  fi
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
