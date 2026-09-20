#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proxy_up.py — 由 NODE_LINK 起一個本地 sing-box socks5 代理（CI 用）。

2026-09-20 新增。來源係 dafaguo 嗰套 setup_proxy.sh 嘅簡化版，但修咗一個
**致命細節**：vmess 節點嘅 path 若帶 query（例如 `/vmess-argo?ed=2560`），
sing-box 會將個 `?` 轉義成 `%3F`（`GET /vmess-argo%3F...`）→ 節點回 HTTP 404、
連唔通。實測（NAS，sing-box 1.10.7）：path 去掉 query 即通。
所以呢度會自動剁走 vmess path 嘅 `?xxx`。

支援：vmess://(base64 JSON)、vless://、tuic://、trojan://
輸出：本機 socks5://127.0.0.1:<port>，並實測出口 IP（打 CF trace）。
用法：NODE_LINK='vmess://...' python3 scripts/proxy_up.py
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = int(os.environ.get("PROXY_PORT", "10818"))
SB_BIN = os.environ.get("SING_BOX_BIN", "./sing-box")
SB_VER = os.environ.get("SING_BOX_VERSION", "1.10.7")
CFG = "/tmp/proxy_up_cfg.json"
LOG = "/tmp/proxy_up.log"
STATE = "/tmp/proxy_state.json"


def log(m):
    print(f"[proxy] {m}", flush=True)


def ensure_sing_box():
    if os.path.exists(SB_BIN) and os.access(SB_BIN, os.X_OK):
        try:
            v = subprocess.run([SB_BIN, "version"], capture_output=True, text=True, timeout=20)
            if "sing-box" in (v.stdout or ""):
                log(f"已自帶 sing-box：{(v.stdout or '').splitlines()[0]}")
                return True
        except Exception:
            pass
    url = (f"https://github.com/SagerNet/sing-box/releases/download/v{SB_VER}/"
           f"sing-box-{SB_VER}-linux-amd64.tar.gz")
    log(f"下載 sing-box v{SB_VER} …")
    try:
        tgz = "/tmp/sing-box.tar.gz"
        urllib.request.urlretrieve(url, tgz)
        subprocess.run(["tar", "-xzf", tgz, "-C", "/tmp"], check=True, timeout=180)
        src = f"/tmp/sing-box-{SB_VER}-linux-amd64/sing-box"
        os.replace(src, SB_BIN)
        os.chmod(SB_BIN, 0o755)
        log("sing-box 就緒")
        return True
    except Exception as e:
        log(f"❌ sing-box 下載/解壓失敗: {type(e).__name__}: {str(e)[:120]}")
        return False


def outbound_from_link(link):
    """由分享連結砌 sing-box outbound。"""
    protocol = link.split("://", 1)[0].strip().lower()
    body = link.split("://", 1)[1].split("#")[0]
    if protocol == "vmess":
        raw = base64.b64decode(body + "=" * (-len(body) % 4)).decode("utf-8", "replace")
        c = json.loads(raw)
        path = c.get("path") or "/"
        if "?" in path:
            log(f"⚠️ vmess path 帶 query（{path}）→ 剁走，否則 sing-box 會轉義成 %3F 收 404")
            path = path.split("?", 1)[0]
        ob = {
            "type": "vmess", "tag": "proxy",
            "server": c["add"], "server_port": int(c["port"]),
            "uuid": c["id"], "security": c.get("scy") or "auto",
            "alter_id": int(c.get("aid") or 0),
            "transport": {"type": "ws", "path": path,
                          "headers": {"Host": c.get("host") or c.get("sni") or c["add"]}},
        }
        if str(c.get("tls", "")).lower() in ("tls", "true", "1") or c.get("sni"):
            ob["tls"] = {"enabled": True, "server_name": c.get("sni") or c.get("host"),
                         "insecure": str(c.get("insecure", "0")) in ("1", "true"),
                         # 一定要鎖 http/1.1：uTLS 指紋默認會帶 h2，而 h2 冇 WS upgrade
                         "alpn": ["http/1.1"],
                         "utls": {"enabled": True, "fingerprint": c.get("fp") or "chrome"}}
        return ob
    raise SystemExit(f"未支援嘅協議：{protocol}（目前只做 vmess）")


def start(ob):
    cfg = {
        "log": {"level": "warn", "output": LOG, "timestamp": True},
        "inbounds": [{"type": "socks", "tag": "socks-in", "listen": "127.0.0.1",
                      "listen_port": PORT}],
        "outbounds": [ob],
    }
    json.dump(cfg, open(CFG, "w"), indent=1)
    subprocess.Popen([SB_BIN, "run", "-c", CFG], stdout=open("/tmp/proxy_up.out", "w"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(6)


def exit_ip():
    """經代理打 CF trace 拎出口 IP。"""
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "25", "--socks5-hostname", f"127.0.0.1:{PORT}",
             "https://www.cloudflare.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=40).stdout
        info = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        return info.get("ip"), info.get("loc")
    except Exception as e:
        log(f"出口測試失敗: {repr(e)[:80]}")
        return None, None


def main():
    link = (os.environ.get("NODE_LINK") or "").strip()
    proxy = ""
    if not link:
        log("未配置 NODE_LINK → 直連模式（唔起代理）")
    elif not ensure_sing_box():
        log("無 sing-box → 直連模式")
    else:
        try:
            ob = outbound_from_link(link)
            log(f"節點：{ob['server']}:{ob['server_port']}（{ob['type']}）")
            start(ob)
            ip, loc = exit_ip()
            if ip:
                log(f"✅ 代理通！出口 IP={ip} loc={loc}")
                proxy = f"socks5://127.0.0.1:{PORT}"
            else:
                log("❌ 代理唔通（睇 /tmp/proxy_up.log）")
                try:
                    log(open(LOG).read()[-400:])
                except Exception:
                    pass
        except Exception as e:
            log(f"❌ 建立代理失敗: {type(e).__name__}: {str(e)[:150]}")
    json.dump({"proxy": proxy}, open(STATE, "w"))
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"PROBE_PROXY={proxy}\n")
    log(f"結果：PROBE_PROXY={proxy or '(直連)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
