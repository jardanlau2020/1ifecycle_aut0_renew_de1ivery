#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACLClouds 續期流程 —— 瀏覽器流量診斷（**只觀察，唔提交答案**）

為何要呢支
----------
2026-10-10 實證：
  · API Key 路線：解卡成功、token 到手，但 renew 端點 403 captcha_required
  · session cookie 路線：**一樣 403**（真 session 身份都唔認）
  ⇒ 「閘綁登入 session 身份」呢個假設**被否證**。

剩低嘅未知：站方前端**究竟點樣**把 captcha 交去續期端點（欄位？context？
抑或根本係另一條 endpoint / 多步流程）。與其再猜，不如**錄佢真實請求**。

本腳本做嘅事（全部係前端自己會做嘅動作，冇額外寫入）：
  注入 session cookie → 開 /server/<id> → 開 performance log
  → 撳 Renouveler → 收 20 秒 → 印出所有 api/auth 請求（method/url/status/postData）
  → 截圖 + 印頁面文字

輸出直接用嚟設計正確嘅續期請求；唔會解 captcha、唔會提交任何答案。

環境變數：ACLCLOUDS_SESSION_COOKIE / ACLCLOUDS_SERVER_ID / ACLCLOUDS_BASE_URL
"""
from __future__ import annotations

import json
import os
import time

BASE_URL = (os.environ.get("ACLCLOUDS_BASE_URL") or "https://aclclouds.com").rstrip("/")
SERVER_ID = (os.environ.get("ACLCLOUDS_SERVER_ID") or "f743cf50").strip()
COOKIE = (os.environ.get("ACLCLOUDS_SESSION_COOKIE") or "").strip()
COOKIE_NAME = "__Host-aclclouds_session"
SHOT_DIR = os.environ.get("ACLCLOUDS_SHOT_DIR", ".") or "."
SERVER_URL = f"{BASE_URL}/server/{SERVER_ID}"

INTERESTING = ("/api/", "/auth/", "renew", "captcha", "upgrade")


def log(m: str) -> None:
    print(m, flush=True)


def build_driver():
    from selenium import webdriver
    from selenium.webdriver.chromium.options import ChromiumOptions

    opts = ChromiumOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    return webdriver.Chrome(options=opts)


def drain(driver, since_ts: float):
    """讀 performance log，回傳 (請求清單, 新增 console 訊息)。"""
    reqs, msgs = [], []
    try:
        entries = driver.get_log("performance")
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 攞唔到 performance log: {e}")
        entries = []
    for e in entries:
        try:
            m = json.loads(e["message"])["message"]
        except Exception:  # noqa: BLE001
            continue
        if m.get("method") == "Network.requestWillBeSent":
            p = m["params"]
            r = p.get("request", {})
            url = r.get("url", "")
            if any(k in url for k in INTERESTING):
                reqs.append({"t": round(p.get("timestamp", 0), 2), "method": r.get("method"),
                             "url": url, "postData": r.get("postData"),
                             "headers": {k: v for k, v in (r.get("headers") or {}).items()
                                         if k.lower() in ("x-xsrf-token", "authorization",
                                                          "content-type", "x-requested-with")}})
        elif m.get("method") == "Network.responseReceived":
            p = m["params"]
            url = (p.get("response") or {}).get("url", "")
            if any(k in url for k in INTERESTING):
                for r in reqs:
                    if r["url"] == url and "status" not in r:
                        r["status"] = (p.get("response") or {}).get("status")
    try:
        for e in driver.get_log("browser"):
            msgs.append(f"[{e.get('level')}] {e.get('message','')[:200]}")
    except Exception:  # noqa: BLE001
        pass
    return reqs, msgs


def main() -> int:
    log("=" * 62)
    log(f"🔬 ACLClouds 流量診斷 @ {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    log(f"🌐 {SERVER_URL}")
    if not COOKIE:
        log("❌ 冇 ACLCLOUDS_SESSION_COOKIE")
        return 1

    driver = build_driver()
    try:
        driver.get(BASE_URL)
        time.sleep(3)
        driver.execute_cdp_cmd("Network.setCookie", {
            "name": COOKIE_NAME, "value": COOKIE, "domain": "aclclouds.com", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax"})
        driver.get(SERVER_URL)
        time.sleep(6)
        body_before = driver.find_element("tag name", "body").text
        log(f"📄 撳之前頁面文字[:300]: {body_before[:300]!r}")
        try:
            os.makedirs(SHOT_DIR, exist_ok=True)
            driver.save_screenshot(os.path.join(SHOT_DIR, "diag_before.png"))
            log("   📸 diag_before.png")
        except Exception:  # noqa: BLE001
            pass

        # 開錄
        drain(driver, 0)

        from selenium.webdriver.common.by import By
        btn = None
        for xp in ("//*[self::button or self::a or @role='button']"
                   "[contains(translate(normalize-space(.),"
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'renouvel')]",
                   "//*[self::button or self::a or @role='button']"
                   "[contains(translate(normalize-space(.),"
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'renew')]"):
            els = driver.find_elements(By.XPATH, xp)
            if els:
                btn = els[0]
                break
        if btn is None:
            log("⚠️ 搵唔到 Renouveler 掣（可能未到窗口／未登入）")
        else:
            log(f"🖱️ 撳掣: {btn.tag_name} / {btn.text[:40]!r}")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except Exception:  # noqa: BLE001
                driver.execute_script("arguments[0].click();", btn)

        time.sleep(20)

        reqs, msgs = drain(driver, 0)
        log("\n───── 錄到嘅 api/auth 請求 ─────")
        for r in reqs:
            pd = r.get("postData")
            log(f"  {r['method']:5s} {r.get('status','?')} {r['url'][:110]}")
            if pd:
                log(f"        postData: {str(pd)[:300]}")
            hdr = r.get("headers") or {}
            if hdr:
                log(f"        headers: { {k: (v[:24] + '…' if len(v) > 24 else v) for k, v in hdr.items()} }")
        if not reqs:
            log("  （冇錄到）")

        log("\n───── console ─────")
        for m in msgs[-15:]:
            log(f"  {m}")

        body_after = driver.find_element("tag name", "body").text
        log(f"\n📄 撳之後頁面文字[:600]: {body_after[:600]!r}")
        try:
            driver.save_screenshot(os.path.join(SHOT_DIR, "diag_after.png"))
            log("   📸 diag_after.png")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
