#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACLClouds 續期 —— **登入 session cookie** 路線（純 HTTP，零瀏覽器、零 Selenium）

為何另開一條路線（2026-10-09）
------------------------------
ACLClouds repo 嘅 API Key 路線（`acl_api_renew.py`）實測：卡片題答得啱、
`captcha_token` 攞到手，但帶住 token 打 renew 端點照樣 403 `captcha_required`
（4 個 payload 變體 × 3 輪 = 12 次全拒，最後撞 429）。嗰邊寫低嘅結論係：

    站方嗰道閘綁嘅係**登入 session 身份**，API Key 身份攞到嘅 token 唔算數。

本腳本就係用 **session cookie** 走返**同一條** captcha protocol（同一份
`acl_captcha.py`，唔係新寫 solver），去驗證嗰個結論：
  · token 認得 → 續期通，之後兩個帳號都改用呢條路線
  · 一樣 403   → 證實閘唔淨係睇身份，要另找原因（唔會白撞，只試一輪）

同 `scripts/aclclouds_renew.py`（Selenium）嘅分別：純 HTTP、無瀏覽器，
所以冇「撳完之後彈咗圖卡題但腳本睇唔到、只等剩餘增加然後逾時」呢個盲點。

環境變數
--------
    ACLCLOUDS_SESSION_COOKIE   __Host-aclclouds_session 的值（必填）
    ACLCLOUDS_SERVER_ID        預設 f743cf50
    ACLCLOUDS_BASE_URL         預設 https://aclclouds.com
    DRY_RUN=1                  只讀狀態、唔提交續期、唔解 captcha
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（或 TG_BOT_TOKEN / TG_CHAT_ID）
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acl_captcha import solve_captcha_api  # noqa: E402

BASE_URL = (os.environ.get("ACLCLOUDS_BASE_URL") or "https://aclclouds.com").rstrip("/")
SERVER_ID = (os.environ.get("ACLCLOUDS_SERVER_ID") or "f743cf50").strip()
COOKIE = (os.environ.get("ACLCLOUDS_SESSION_COOKIE") or "").strip()
COOKIE_NAME = "__Host-aclclouds_session"
DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() not in ("", "0", "false", "no")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def log(msg: str) -> None:
    print(msg, flush=True)


def tg(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        log("ℹ️ 未配置 TG，跳過通知")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
                          timeout=20)
        log(f"📨 TG {'已發送' if r.status_code == 200 else f'失敗 HTTP {r.status_code}'}")
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ TG 發送異常: {type(e).__name__}: {e}")


def make_session() -> requests.Session:
    """只帶 session cookie（唔帶 API Key）——呢個就係本路線嘅自變量。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": BASE_URL + "/"})
    s.cookies.set(COOKIE_NAME, COOKIE, domain="aclclouds.com", path="/", secure=True)
    return s


def api_get(s: requests.Session, path: str):
    return s.get(BASE_URL + path, timeout=30)


def api_post(s: requests.Session, path: str, payload=None):
    return s.post(BASE_URL + path, json=payload if payload is not None else {}, timeout=30)


def find_server(s: requests.Session) -> dict | None:
    """GET /api/client（面板嘅伺服器清單）。"""
    r = api_get(s, "/api/client")
    log(f"🔎 GET /api/client -> HTTP {r.status_code}")
    if r.status_code != 200:
        log(f"   body[:200]={r.text[:200]}")
        return None
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        log("   響應唔係 JSON（可能被 CF 擋／cookie 失效）")
        return None
    rows = data.get("data") if isinstance(data, dict) else data
    rows = rows or []
    log(f"   共 {len(rows)} 台服務")
    for row in rows:
        attrs = (row or {}).get("attributes") or row or {}
        sid = attrs.get("id") or row.get("id")
        name = attrs.get("name")
        exp = attrs.get("expires_at")
        log(f"   ▪️ {name} ({sid}) 到期 {exp} | can_renew={attrs.get('can_renew')}")
        if SERVER_ID and str(sid) == SERVER_ID:
            return {"id": sid, "name": name, "expires_at": exp,
                    "can_renew": attrs.get("can_renew")}
    if rows:                                    # 對唔上就取第一台（單服帳號）
        row = rows[0]
        attrs = row.get("attributes") or row
        return {"id": attrs.get("id") or row.get("id"), "name": attrs.get("name"),
                "expires_at": attrs.get("expires_at"), "can_renew": attrs.get("can_renew")}
    return None


def main() -> int:
    log("=" * 60)
    log(f"🚀 ACLClouds session-cookie 續期 @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    log(f"🌐 {BASE_URL} | server_id={SERVER_ID} | DRY_RUN={DRY_RUN}")
    if not COOKIE:
        log("❌ 冇 ACLCLOUDS_SESSION_COOKIE，唔使玩")
        return 1

    s = make_session()

    # 0. 驗證 cookie 真係認得（唔靠估）
    srv = find_server(s)
    if not srv:
        log("❌ cookie 睇唔到伺服器清單 → 可能已失效")
        tg("🚨 ACLClouds（session 路線）· Cookie 失效或面板讀唔到，請人手處理")
        return 1

    sid, before = srv["id"], srv["expires_at"]
    log(f"📦 目標：{srv['name']} ({sid}) | 到期 {before} | can_renew={srv['can_renew']}")

    if DRY_RUN:
        log("🧪 DRY_RUN=1 → 只讀，唔提交、唔解 captcha")
        return 0

    # 1. 先裸打一次 renew，確認係咪要 captcha
    path = f"/api/client/servers/{sid}/upgrade/renew"
    r = api_post(s, path)
    log(f"① POST {path} -> HTTP {r.status_code} | {r.text[:200]}")
    if r.status_code < 300:
        log("✅ 唔使 captcha 就續到")
    else:
        body = r.text or ""
        if "captcha" not in body.lower():
            log("⚠️ 唔係 captcha 問題，停手（避免瞎撞）")
            tg(f"🚨 ACLClouds · 續期被拒（HTTP {r.status_code}，非 captcha）｜{body[:160]}")
            return 1

        # 2. 用**同一個 session**解站方卡片題 → 帶 token 重發
        log("🧩 走站方 renewal_gate captcha protocol（同一 session 身份）…")
        tok = solve_captcha_api(s, BASE_URL, "renewal_gate", max_rounds=6)
        if not tok:
            log("❌ 解唔到 captcha")
            tg("🚨 ACLClouds · 續期 captcha 解唔到，請人手處理")
            return 1

        for label, payload in (("captcha_token+captcha_answer",
                                {"captcha_token": tok, "captcha_answer": "human"}),
                               ("純 captcha_token", {"captcha_token": tok})):
            r2 = api_post(s, path, payload)
            log(f"② 帶 token 重發（{label}）-> HTTP {r2.status_code} | {r2.text[:200]}")
            if r2.status_code < 300:
                r = r2
                break
        else:
            log("❌ session 身份帶 token 一樣被拒 → 閘唔淨係睇身份，收手（唔再空轉）")
            tg("🚨 ACLClouds · session 身份帶 captcha token 仍被拒（HTTP 403）"
               "｜閘唔淨係睇身份，要另找原因；請人手撳")
            return 1

    # 3. 核對：剩餘時間有冇真係變長
    time.sleep(3)
    srv2 = find_server(s)
    after = (srv2 or {}).get("expires_at")
    log(f"📆 續期前 {before} → 續期後 {after}")
    if after and before and after != before:
        log("✅ 到期日已變 → 續期成功")
        tg(f"✅ ACLClouds · 續期成功\n{before} → {after}")
        return 0
    log("⚠️ 到期日冇變，未確認成功")
    tg(f"⚠️ ACLClouds · 續期未確認（{before} → {after}）｜請上panel睇一眼")
    return 1


if __name__ == "__main__":
    sys.exit(main())
