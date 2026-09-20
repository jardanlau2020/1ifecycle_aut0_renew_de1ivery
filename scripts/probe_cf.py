#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CF 攔截頁乾淨探針（2026-09-20）。

目的：分離「我哋嘅自動化太粗暴」同「出口 IP 信譽太低」兩個假設。
- 主腳本（weirdhost_renew.py）會不停撳、每 24 秒 refresh、又做 CSS 改動，
  無法判斷 CF 到底係因為 IP 定因為我哋自己搞亂咗驗證流程。
- 呢個探針完全乾淨：真瀏覽器、正常 viewport/UA/時區、**只撳一次**，
  之後耐心等 70 秒，睇 cf_clearance 會唔會落地。

兩個引擎對照（PROBE_ENGINES 控制）：
  playwright  —— 普通 Playwright（同 SeleniumBase 一樣走 CDP）
  patchright  —— undetected Playwright（唔會漏 Runtime.enable 指紋）

輸出：ARM 結果 JSON + probe_<engine>.png 截圖（artifact）。
"""
import json
import os
import sys
import time

URL = os.environ.get("PROBE_URL", "https://hub.weirdhost.xyz/")
WAIT = int(os.environ.get("PROBE_WAIT", "70"))
ENGINES = [e.strip() for e in
           os.environ.get("PROBE_ENGINES", "playwright,patchright").split(",") if e.strip()]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PROXY = (os.environ.get("PROBE_PROXY") or "").strip()
CF_BLOCK_MARKERS = ("잠시만", "Just a moment", "보안 확인", "Un instant", "security verification")

sys.stdout.reconfigure(line_buffering=True)


def human_click(page, x, y):
    """盡量似真人嘅點擊序列（2026-09-20 加）。

    背景：fridaydev run 35502154511 改用 patchright 之後，widget 即刻回
    `Le test a échoué. Rechargez la page et réessayez.` —— CF 主動講「測試失敗」。
    而 `page.mouse.click()` 係 down→up 零延遲、無中間移動，係典型機器人特徵。
    真人係：移幾步過去 → 停一停 → 按住 → 百幾毫秒 → 鬆手。
    """
    page.mouse.move(max(0, x - 60), max(0, y - 25), steps=4)
    time.sleep(0.25)
    page.mouse.move(x - 8, y - 3, steps=6)
    time.sleep(0.35)
    page.mouse.move(x, y, steps=3)
    time.sleep(0.45)          # 停低先撳
    page.mouse.down()
    time.sleep(0.12)          # 按住 120ms，唔好 0ms
    page.mouse.up()
    time.sleep(0.2)


def cdp_widget_box(page):
    """用 CDP pierce 搵 CF widget iframe 嘅 box（closed shadow DOM 都睇得穿）。"""
    try:
        sess = page.context.new_cdp_session(page)
        doc = sess.send("DOM.getDocument", {"depth": -1, "pierce": True})
    except Exception as e:
        print(f"  [CDP] 開唔到 session: {repr(e)[:90]}", flush=True)
        return None

    found = []

    def _walk(n):
        name = (n.get("nodeName") or "").lower()
        if name in ("iframe", "input", "div"):
            a = n.get("attributes") or []
            am = {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}
            blob = " ".join(str(am.get(k, "")) for k in ("src", "class", "id", "name")).lower()
            if "turnstile" in blob or "challenge" in blob:
                found.append((n.get("nodeId"), name, blob[:60]))
        for key in ("children", "shadowRoots", "contentDocument"):
            v = n.get(key)
            kids = v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
            for c in kids:
                if isinstance(c, dict):
                    _walk(c)

    _walk(doc.get("root", {}))
    for node_id, name, blob in found:
        try:
            box = sess.send("DOM.getBoxModel", {"nodeId": node_id})
        except Exception:
            continue
        q = box["model"]["content"]
        xs, ys = q[0::2], q[1::2]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w > 100 and h > 30:
            print(f"  [CDP] 搵到 {name} box=({min(xs):.0f},{min(ys):.0f},{w:.0f}x{h:.0f}) "
                  f"| {blob}", flush=True)
            return {"x": min(xs), "y": min(ys), "w": w, "h": h,
                    "click": (min(xs) + 30, min(ys) + h / 2.0)}
    print("  [CDP] pierce 冇搵到 widget box", flush=True)
    return None


def run_engine(engine):
    print(f"\n{'=' * 72}\n[ARM] {engine}\n{'=' * 72}", flush=True)
    res = {"engine": engine, "clicked": False, "passed": False, "cf_clearance": False,
           "titles": [], "notes": []}
    if engine == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        kwargs = {"headless": False,
                  "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if PROXY:
            kwargs["proxy"] = {"server": PROXY}
            print(f"  [INFO] {engine} 走代理：{PROXY}", flush=True)
        browser = p.chromium.launch(**kwargs)
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul",
                                  viewport={"width": 1280, "height": 760})
        page = ctx.new_page()
        try:
            page.goto("https://www.cloudflare.com/cdn-cgi/trace", timeout=45000)
            info = dict(l.split("=", 1) for l in page.inner_text("body").splitlines()
                        if "=" in l)
            print(f"  [INFO] 實際出口 IP={info.get('ip')} loc={info.get('loc')}", flush=True)
        except Exception as e:
            print(f"  [WARN] 出口測試失敗: {repr(e)[:70]}", flush=True)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        t0 = time.time()
        clicked_at = None
        while time.time() - t0 < WAIT:
            time.sleep(2)
            elapsed = time.time() - t0
            try:
                title = page.title()
            except Exception:
                title = "?"
            names = {c["name"] for c in ctx.cookies()}
            blocked = any(m in title for m in CF_BLOCK_MARKERS)
            res["titles"].append(f"{elapsed:.0f}s:{title[:22]}")
            print(f"  t={elapsed:4.0f}s title={title[:40]!r} cookies={sorted(names)[:6]} "
                  f"blocked={blocked}", flush=True)
            if "cf_clearance" in names:
                res["cf_clearance"] = True
            if "cf_clearance" in names:
                # 2026-09-20 實測：patchright 一撳就有 cf_clearance，但攔截頁仍然唔走。
                # 試用「取到 clearance 就重新載入」——CF 見 cookie 有效應該直接放行。
                res["clicked"] = res["clicked"]  # noqa
                tries = res.get("reload_tries", 0)
                if tries < 3:
                    res["reload_tries"] = tries + 1
                    print(f"  🔄 cf_clearance 已入 cookie jar → 第 {tries + 1} 次重新載入",
                          flush=True)
                    try:
                        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(4)
                        t2 = page.title()
                        blocked2 = any(m in t2 for m in CF_BLOCK_MARKERS)
                        print(f"  🔄 重載後 title={t2[:40]!r} blocked={blocked2}", flush=True)
                        if not blocked2:
                            res["passed"] = True
                            res["clicked_at_s"] = res.get("clicked_at_s")
                            print(f"  ✅✅ {engine}：重新載入後真正入到站 ✅✅", flush=True)
                            break
                    except Exception as e:
                        print(f"  [WARN] 重載失敗: {repr(e)[:90]}", flush=True)
                # 重載都唔得 → 唔好死等，早啲收工
                break
            if "cf_clearance" in names and not blocked:
                res["passed"] = True
                print(f"  ✅ {engine} 過咗 CF（cf_clearance 落地 + 唔再係攔截頁）", flush=True)
                break
            # 撳一次（未撳過 + 已經撳完 8 秒以上就唔好再騷擾）
            if not res["clicked"] and elapsed > 2 and blocked:
                box = None
                try:
                    for fr in page.frames:
                        if "challenges.cloudflare.com" in (fr.url or ""):
                            el = fr.frame_element()
                            bb = el.bounding_box()
                            if bb and bb["width"] > 100:
                                box = {"x": bb["x"], "y": bb["y"], "w": bb["width"],
                                       "h": bb["height"],
                                       "click": (bb["x"] + 30, bb["y"] + bb["height"] / 2)}
                                break
                except Exception as e:
                    res["notes"].append(f"frame scan: {repr(e)[:60]}")
                if box is None:
                    box = cdp_widget_box(page)
                if box:
                    cx, cy = box["click"]
                    print(f"  → 撳 widget ({cx:.0f},{cy:.0f}) 一次，之後唔再騷擾", flush=True)
                    try:
                        human_click(page, cx, cy)   # 2026-09-20：改用似真人嘅點擊序列
                        res["clicked"] = True
                        res["click_style"] = "human"
                        clicked_at = elapsed
                    except Exception as e:
                        res["notes"].append(f"click: {repr(e)[:70]}")
                else:
                    res["notes"].append(f"t={elapsed:.0f}s 搵唔到 widget")
        try:
            page.screenshot(path=f"probe_{engine}.png", full_page=False)
        except Exception as e:
            res["notes"].append(f"screenshot: {repr(e)[:60]}")
        try:
            res["final_title"] = page.title()
            res["final_url"] = page.url
        except Exception:
            pass
        if clicked_at is not None:
            res["clicked_at_s"] = round(clicked_at, 1)
        try:
            browser.close()
        except Exception:
            pass
    return res


def main():
    out = []
    for eng in ENGINES:
        try:
            out.append(run_engine(eng))
        except Exception as e:
            print(f"[ERROR] {eng} 爆咗: {type(e).__name__}: {e}", flush=True)
            out.append({"engine": eng, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    print("\n" + "=" * 72)
    print("PROBE RESULT")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("=" * 72)
    with open("probe_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if not any(r.get("passed") for r in out):
        sys.exit(2)   # 兩個引擎都過唔到 → 明確失敗（唔係假綠）


if __name__ == "__main__":
    main()
