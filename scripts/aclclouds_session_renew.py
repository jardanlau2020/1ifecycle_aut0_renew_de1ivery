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
import subprocess
import sys
import tempfile
import time
import urllib.parse

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

# ── Cap（tiagozip/cap 自架 PoW）—— 2026-10-10 實證：續期嗰道閘就係佢 ──────────
# 證據：瀏覽器流量診斷（run 38013798276）撳 Renouveler 之後，前端彈嘅係
# 「Confirmation anti-robot … Vérifier que vous êtes humain」+ **Cap** 標記嘅
# widget。即係之前一直餵嘅 `/auth/captcha` 卡片 token（renewal_gate context）
# 根本唔係呢道閘要嘅嘢 —— 所以點都 403 captcha_required。
# 解算器唔係新寫：同 login 探針用同一份 vendored cap_core/solve.mjs。
CAP_SITEKEY = "235a82a3e3"
CAP_API = f"https://cap.aclclouds.com/{CAP_SITEKEY}"
HERE = os.path.dirname(os.path.abspath(__file__))


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


def _xsrf(s: requests.Session):
    """由 cookie jar 攞 XSRF-TOKEN（URL 解碼後）——session 身份嘅 POST 冇佢會 419。"""
    try:
        for c in s.cookies:
            if c.name == "XSRF-TOKEN":
                return urllib.parse.unquote(c.value)
    except Exception:  # noqa: BLE001
        pass
    return None


def bootstrap_csrf(s: requests.Session) -> bool:
    """GET 一個頁面叫站方發 XSRF-TOKEN cookie。

    2026-10-10 實證：session cookie 路線第一次裸打 renew 回 **HTTP 419**
    （Laravel CSRF token mismatch），唔係 403 captcha_required —— 因為 session
    身份嘅 state-changing POST 要 CSRF，而 API Key 路線（Sanctum）唔使。
    """
    if _xsrf(s):
        return True
    for path in ("/", "/login"):
        try:
            s.get(BASE_URL + path, timeout=30)
        except Exception as e:  # noqa: BLE001
            log(f"   [csrf] GET {path} 異常: {e}")
            continue
        if _xsrf(s):
            log(f"   [csrf] 已由站方取得 XSRF-TOKEN（GET {path}）")
            return True
    return False


def api_get(s: requests.Session, path: str):
    return s.get(BASE_URL + path, timeout=30)


def api_post(s: requests.Session, path: str, payload=None):
    """POST 一律帶 X-XSRF-TOKEN（同 acl_captcha.py 同一套做法）。"""
    h = {}
    tok = _xsrf(s)
    if tok:
        h["X-XSRF-TOKEN"] = tok
        h["X-Requested-With"] = "XMLHttpRequest"
        h.setdefault("Referer", BASE_URL + "/")
    return s.post(BASE_URL + path, json=payload if payload is not None else {},
                  headers=h or None, timeout=30)


def solve_cap(s: requests.Session):
    """Cap challenge → node PoW（cap_core/solve.mjs）→ redeem → 一次性 token。

    同 probe_acl_login.py 同一條路（登入閘已實證通），唔係新寫 solver。
    """
    r = s.post(f"{CAP_API}/challenge", timeout=30)
    r.raise_for_status()
    chal = r.json()
    n_ch = len(chal.get("challenges") or [])
    if not n_ch:
        log(f"   [cap] challenge 無 challenges: {str(chal)[:160]}")
        return None
    log(f"   [cap] challenge 攞到 {n_ch} 條 PoW 題")
    with tempfile.TemporaryDirectory() as td:
        inp, out = os.path.join(td, "c.json"), os.path.join(td, "s.json")
        with open(inp, "w") as f:
            json.dump(chal, f)
        p = subprocess.run(["node", os.path.join(HERE, "..", "cap_core", "solve.mjs"),
                            inp, out],
                           capture_output=True, text=True, timeout=300, cwd=HERE)
        if p.returncode != 0:
            log(f"   [cap] solve.mjs rc={p.returncode}: {(p.stderr or p.stdout)[-300:]}")
            return None
        with open(out) as f:
            sol = json.load(f)
    r2 = s.post(f"{CAP_API}/redeem",
                json={"token": sol["token"], "solutions": sol["solutions"]},
                headers={"Content-Type": "application/json"}, timeout=30)
    try:
        j2 = r2.json()
    except ValueError:
        log(f"   [cap] redeem 非 JSON HTTP {r2.status_code}: {r2.text[:160]}")
        return None
    if not j2.get("success") or not j2.get("token"):
        log(f"   [cap] redeem fail HTTP {r2.status_code}: {str(j2)[:160]}")
        return None
    log(f"   [cap] ✅ PoW 解完（{n_ch} 條），一次性 token 到手（len={len(str(j2['token']))}）")
    return str(j2["token"])


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
        # 面板用 `identifier`（短 ID），唔係 `id`（實測 2026-10-10：用 id 會攞到 None）
        sid = attrs.get("identifier") or attrs.get("uuid") or attrs.get("id") or row.get("id")
        name = attrs.get("name")
        exp = attrs.get("expires_at")
        log(f"   ▪️ {name} ({sid}) 到期 {exp} | can_renew={attrs.get('can_renew')}")
        if SERVER_ID and str(sid) == SERVER_ID:
            return {"id": sid, "name": name, "expires_at": exp,
                    "can_renew": attrs.get("can_renew")}
    if rows:                                    # 對唔上就取第一台（單服帳號）
        row = rows[0]
        attrs = row.get("attributes") or row
        return {"id": attrs.get("identifier") or attrs.get("uuid") or attrs.get("id") or row.get("id"),
                "name": attrs.get("name"),
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
    bootstrap_csrf(s)

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

        # 2. 解**站方真正嗰道閘**：Cap（前端撳完 Renouveler 彈嘅就係佢）
        log("🧩 解站方 Cap（cap.aclclouds.com）…")
        cap_tok = solve_cap(s)

        tried = []
        if cap_tok:
            tried.append(("Cap token（captcha_token）", {"captcha_token": cap_tok}))
            tried.append(("Cap token（token）", {"token": cap_tok}))
        # 兜底：舊卡片題 token（已證實多數唔認，但唔花額外成本）
        log("🧩 同時試埋自家卡片題 token（renewal_gate）作兜底…")
        card_tok = solve_captcha_api(s, BASE_URL, "renewal_gate", max_rounds=6)
        if card_tok:
            tried.append(("卡片 token（captcha_token）", {"captcha_token": card_tok}))

        if not tried:
            log("❌ 兩種 token 都攞唔到")
            tg("🚨 ACLClouds · 續期驗證 token 攞唔到，請人手處理")
            return 1

        for label, payload in tried:
            r2 = api_post(s, path, payload)
            log(f"② 帶 token 重發（{label}）-> HTTP {r2.status_code} | {r2.text[:200]}")
            if r2.status_code < 300:
                r = r2
                break
        else:
            log("❌ 兩種 token 都被拒 → 收手（唔再空轉，避免撞 429）")
            tg("🚨 ACLClouds · Cap 同卡片 token 都被拒（403 captcha_required）"
               "｜請人手撳（面板撳 Renouveler → 勾 Cap → 完成）")
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
