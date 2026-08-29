#!/usr/bin/env python3
"""Lifecycle automation for Monkey Network and configurable Pterodactyl-like panels.

Secrets are read from environment variables only. The script never bypasses CAPTCHA.
Monkey endpoint discovered from the panel:
  GET  /api/client/servers/{id}/lifecycle
  POST /api/client/servers/{id}/lifecycle/confirm (only when can_confirm=true)
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone


def request_json(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=(json.dumps(body).encode() if body is not None else None))
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "replace")
        return r.status, (json.loads(raw) if raw else {})


def monkey():
    base = os.getenv("MONKEY_API_BASE", "https://dash.monkey-network.xyz/api/client").rstrip("/")
    key = os.getenv("MONKEY_API_KEY")
    sid = os.getenv("MONKEY_SERVER_IDENTIFIER", "2533c753")
    if not key:
        return {"platform":"monkey-network", "ok":False, "error":"MONKEY_API_KEY missing"}
    h = {"Authorization":"Bearer " + key,
         "Accept":"Application/vnd.pterodactyl.v1+json",
         "Content-Type":"application/json"}
    out = {"platform":"monkey-network", "server_identifier":sid}
    try:
        code, life = request_json(f"{base}/servers/{sid}/lifecycle", headers=h)
        out.update(ok=(code == 200), lifecycle=life)
        if life.get("can_confirm") is True:
            # Only invoke the platform's own confirmation when its API explicitly says it is allowed.
            try:
                c, result = request_json(f"{base}/servers/{sid}/lifecycle/confirm",
                                         method="POST", headers=h, body={})
                out["confirm"] = {"http":c, "result":result}
            except urllib.error.HTTPError as e:
                out["confirm"] = {"http":e.code, "error":e.read().decode("utf-8","replace")[:500]}
        else:
            out["action"] = "等待平台进入 can_confirm=true 窗口"
    except urllib.error.HTTPError as e:
        out.update(ok=False, error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    except Exception as e:
        out.update(ok=False, error=str(e))
    return out


def health(name, url):
    try:
        code, body = request_json(url)
        return {"platform":name, "ok":code == 200, "http":code, "response":body}
    except Exception as e:
        return {"platform":name, "ok":False, "error":str(e)}


def main():
    results = [monkey()]
    acl = os.getenv("ACLCLOUDS_HEALTH_URL", "").strip()
    if acl:
        results.append(health("aclclouds", acl))
    else:
        results.append({"platform": "aclclouds", "ok": True, "action": "health check skipped: no URL configured"})
    report = {"checked_at":datetime.now(timezone.utc).isoformat(), "results":results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A failed check exits non-zero for GitHub Actions alerting.
    # Monkey lifecycle is the required check; optional health checks do not block renewal.
    if not results[0].get("ok"): sys.exit(2)

if __name__ == "__main__": main()
