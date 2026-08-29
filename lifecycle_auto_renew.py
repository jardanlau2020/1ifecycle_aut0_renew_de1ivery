#!/usr/bin/env python3
"""Lifecycle automation for Monkey Network, ACLClouds, Weirdhost + Telegram alerts.

Secrets are read from environment variables only. The script never bypasses CAPTCHA.
Monkey endpoint discovered from the panel:
  GET  /api/client/servers/{id}/lifecycle
  POST /api/client/servers/{id}/lifecycle/confirm (only when can_confirm=true)

ACLCLOUDS / WEIRDHOST use session-cookie based auto-renewal. The renew/start
endpoints are standard Pterodactyl patterns; if the panel returns a redirect or
captcha the script stops and reports the issue rather than guessing.
"""
import json, os, sys, urllib.request, urllib.error, urllib.parse, time
from datetime import datetime, timezone


# ── Helpers ─────────────────────────────────────────────────────────────────
def _env(key, default=""):
    """Return env var, treating empty string as unset."""
    v = os.getenv(key)
    return v if v else default


# ── Telegram ────────────────────────────────────────────────────────────────
TG_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TG_CHAT  = _env("TELEGRAM_CHAT_ID")
TG_OK = bool(TG_TOKEN and TG_CHAT)

def tg_send(text):
    """Send a message to Telegram. Returns True on success."""
    if not TG_OK:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TG_CHAT, "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


def tg_report(lines):
    """Send a report block to Telegram. Always succeeds silently if TG not configured."""
    text = "\n".join(lines)
    if TG_OK:
        tg_send(text)
    return text


# ── HTTP helpers ────────────────────────────────────────────────────────────
def request_json(url, method="GET", headers=None, body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        return r.status, (json.loads(raw) if raw else {})


# ── Monkey Network ──────────────────────────────────────────────────────────
def monkey():
    base = _env("MONKEY_API_BASE", "https://dash.monkey-network.xyz/api/client").rstrip("/")
    key = _env("MONKEY_API_KEY")
    sid = _env("MONKEY_SERVER_IDENTIFIER", "2533c753")
    if not key:
        return {"platform": "monkey-network", "ok": False, "error": "MONKEY_API_KEY missing"}
    h = {"Authorization": "Bearer " + key,
         "Accept": "Application/vnd.pterodactyl.v1+json",
         "Content-Type": "application/json"}
    out = {"platform": "monkey-network", "server_identifier": sid}
    try:
        code, life = request_json(f"{base}/servers/{sid}/lifecycle", headers=h)
        out.update(ok=(code == 200), lifecycle=life)
        if life.get("can_confirm") is True:
            try:
                c, result = request_json(f"{base}/servers/{sid}/lifecycle/confirm",
                                         method="POST", headers=h, body={})
                out["confirm"] = {"http": c, "result": result}
            except urllib.error.HTTPError as e:
                out["confirm"] = {"http": e.code,
                                  "error": e.read().decode("utf-8", "replace")[:500]}
        else:
            out["action"] = "等待平台进入 can_confirm=true 窗口"
    except urllib.error.HTTPError as e:
        out.update(ok=False, error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    except Exception as e:
        out.update(ok=False, error=str(e))
    return out


# ── ACLClouds / Weirdhost (session-cookie auto-renewal) ─────────────────────
def _panel_session(platform):
    """Build a Cookie header from the platform session secret + known cookie name."""
    cookie = _env(f"{platform.upper()}_SESSION_COOKIE")
    cname = _env(f"{platform.upper()}_COOKIE_NAME", {
        "aclclouds": "__Host-aclclouds_session",
        "weirdhost": "laravel_session",
    }.get(platform, "laravel_session"))
    if not cookie:
        return None, None
    return f"{cname}={cookie}", cname


def _panel_headers(platform, extra=None):
    cookie, _ = _panel_session(platform)
    h = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "lifecycle-auto-renew/1.0",
    }
    if cookie:
        h["Cookie"] = cookie
    if extra:
        h.update(extra)
    return h


def _panel_base(platform):
    env_name = f"{platform.upper()}_BASE_URL"
    default = {
        "aclclouds": "https://aclclouds.com",
        "weirdhost": "https://hub.weirdhost.xyz",
    }.get(platform, "")
    return _env(env_name, default).rstrip("/")


def _panel_server_id(platform):
    return _env(f"{platform.upper()}_SERVER_ID")


def _try_renew(platform):
    """Try to POST a renew request to the panel. Returns dict with ok + detail."""
    base = _panel_base(platform)
    sid = _panel_server_id(platform)
    if not base:
        return {"ok": False, "error": f"{platform}: BASE_URL 未设置"}
    if not sid:
        return {"ok": False, "error": f"{platform}: SERVER_ID 未设置"}

    headers = _panel_headers(platform)
    endpoints = [
        f"/api/client/servers/{sid}/renew",
        f"/api/client/servers/{sid}/lifecycle/renew",
        f"/api/client/servers/{sid}/lifecycle",
    ]
    for ep in endpoints:
        try:
            code, body = request_json(f"{base}{ep}", method="POST", headers=headers,
                                      body={}, timeout=20)
            if code in (200, 201, 204):
                return {"ok": True, "endpoint": ep, "http": code, "response": body}
            if code in (401, 403):
                # Check if it's a Cloudflare challenge page
                body_str = str(body)
                if "Just a moment" in body_str or "cf-mitigated" in body_str:
                    return {"ok": False,
                            "error": "Cloudflare 挑战拦截，需浏览器操作",
                            "endpoint": ep, "http": code}
                return {"ok": False, "error": f"Session cookie 无效或已过期 (HTTP {code})",
                        "endpoint": ep}
            if code in (422, 400):
                return {"ok": False, "error": f"请求被拒绝 (HTTP {code}): {str(body)[:200]}",
                        "endpoint": ep}
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return {"ok": False, "error": "面板重定向，需浏览器操作",
                        "endpoint": ep, "http": e.code}
            # Check for Cloudflare challenge in error body
            try:
                err_body = e.read().decode("utf-8", "replace")
                if "Just a moment" in err_body or "cf-mitigated" in err_body:
                    return {"ok": False,
                            "error": "Cloudflare 挑战拦截，需浏览器操作",
                            "endpoint": ep, "http": e.code}
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {e.code}",
                    "endpoint": ep}
        except Exception as e:
            return {"ok": False, "error": str(e), "endpoint": ep}
    return {"ok": False, "error": "所有 renew 端点均失败"}


def _try_start(platform):
    """Try to POST a start request to the panel."""
    base = _panel_base(platform)
    sid = _panel_server_id(platform)
    if not base or not sid:
        return {"ok": False, "error": "BASE_URL 或 SERVER_ID 未设置"}
    headers = _panel_headers(platform)
    endpoints = [
        f"/api/client/servers/{sid}/start",
        f"/api/client/servers/{sid}/lifecycle/start",
        f"/api/client/servers/{sid}/control/start",
    ]
    for ep in endpoints:
        try:
            code, body = request_json(f"{base}{ep}", method="POST", headers=headers,
                                      body={}, timeout=20)
            if code in (200, 201, 204):
                return {"ok": True, "endpoint": ep, "http": code, "response": body}
            if code in (401, 403):
                body_str = str(body)
                if "Just a moment" in body_str or "cf-mitigated" in body_str:
                    return {"ok": False,
                            "error": "Cloudflare 挑战拦截，需浏览器操作",
                            "endpoint": ep, "http": code}
                return {"ok": False, "error": f"Session cookie 无效 (HTTP {code})", "endpoint": ep}
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return {"ok": False, "error": "面板重定向，需浏览器操作", "endpoint": ep}
            try:
                err_body = e.read().decode("utf-8", "replace")
                if "Just a moment" in err_body or "cf-mitigated" in err_body:
                    return {"ok": False,
                            "error": "Cloudflare 挑战拦截，需浏览器操作",
                            "endpoint": ep, "http": e.code}
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {e.code}", "endpoint": ep}
        except Exception as e:
            return {"ok": False, "error": str(e), "endpoint": ep}
    return {"ok": False, "error": "所有 start 端点均失败"}


def aclclouds():
    """ACLClouds: renew + start using session cookie.

    Panel: https://aclclouds.com (Laravel SPA + Sanctum)
    Cookie name: __Host-aclclouds_session
    Server URL: /server/{id}

    Known limitations:
    - Session cookie authenticates web SPA but NOT the Laravel API (401 Unauthenticated)
    - Sanctum CSRF endpoint blocked by Cloudflare (403)
    - API endpoints for renew/start are SPA routes (return HTML shell, not JSON)
    - Panel is NOT Pterodactyl-based; API paths differ from standard /api/client/servers/{id}
    - Manual browser renew required until proper API token is available
    """
    cookie, cname = _panel_session("aclclouds")
    if not cookie:
        return {"platform": "aclclouds", "ok": False, "error": "ACLCLOUDS_SESSION_COOKIE 未设置"}
    result = {"platform": "aclclouds", "cookie_name": cname, "panel_url": "https://aclclouds.com"}
    renew = _try_renew("aclclouds")
    result["renew"] = renew
    if renew.get("ok"):
        time.sleep(2)
        start = _try_start("aclclouds")
        result["start"] = start
        result["ok"] = start.get("ok", False)
    else:
        result["ok"] = False
        # Add helpful context based on common error patterns
        err = renew.get("error", "")
        if "401" in err or "Unauthenticated" in err:
            result["hint"] = "Session cookie 无法认证 API（Laravel Sanctum SPA 模式），需浏览器手动操作或提供 API token"
        elif "404" in err:
            result["hint"] = "API 路径不存在，面板非 Pterodactyl 格式，续期端点与标准 /api/client/servers/{id}/renew 不同"
    return result


def weirdhost():
    """Weirdhost: confirm/renew using session cookie."""
    cookie, cname = _panel_session("weirdhost")
    if not cookie:
        return {"platform": "weirdhost", "ok": False, "error": "WEIRDHOST_SESSION_COOKIE 未设置"}
    result = {"platform": "weirdhost", "cookie_name": cname}
    renew = _try_renew("weirdhost")
    result["renew"] = renew
    if renew.get("ok"):
        time.sleep(2)
        start = _try_start("weirdhost")
        result["start"] = start
        result["ok"] = start.get("ok", False)
    else:
        result["ok"] = False
    return result


# ── Health check ────────────────────────────────────────────────────────────
def health(name, url):
    try:
        code, body = request_json(url)
        return {"platform": name, "ok": code == 200, "http": code, "response": body}
    except Exception as e:
        return {"platform": name, "ok": False, "error": str(e)}


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    results = [monkey(), aclclouds(), weirdhost()]
    acl = _env("ACLCLOUDS_HEALTH_URL", "http://141.11.237.77:30551/health")
    results.append(health("aclclouds", acl))

    now = datetime.now(timezone.utc)
    report = {"checked_at": now.isoformat(), "results": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # ── Telegram notification ──
    lines = [f"🔔 <b>生命周期检查</b> · {now.strftime('%m-%d %H:%M UTC')}"]
    all_ok = True
    for r in results:
        pname = r.get("platform", "?")
        if r.get("ok"):
            lines.append(f"  ✅ {pname}")
        else:
            all_ok = False
            err = r.get("error", r.get("confirm", {}).get("error", "未知错误"))
            lines.append(f"  ❌ {pname}: {err}")

    if all_ok:
        lines.append("\n✅ 全部平台正常")
    else:
        lines.append("\n⚠️ 存在异常，请检查")

    tg_report(lines)

    if not all_ok:
        sys.exit(2)


if __name__ == "__main__":
    main()