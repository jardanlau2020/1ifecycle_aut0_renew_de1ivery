#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import asyncio
import aiohttp
import base64
import random
import re
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import unquote

from seleniumbase import SB

try:
    from nacl import encoding, public
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

sys.stdout.reconfigure(line_buffering=True)

BASE_URL = "https://hub.weirdhost.xyz/server/"
API_BASE_URL = "https://hub.weirdhost.xyz/api/client"
DOMAIN = "hub.weirdhost.xyz"
MAX_COOKIE_COUNT = 5

RENEWAL_BUTTON_SELECTORS = [
    "//button//span[contains(text(), '연장하기')]/parent::button",
    "//button[contains(text(), '연장하기')]",
    "//button//span[contains(text(), '시간추가')]/parent::button",
    "//button[contains(text(), '시간추가')]",
    "//button//span[contains(text(), '시간 추가')]/parent::button",
    "//button[contains(text(), '시간 추가')]",
]


# ============================================================
#  工具函数
# ============================================================

def mask_sensitive(text, show_chars=3):
    if not text:
        return "***"
    text = str(text)
    if len(text) <= show_chars * 2:
        return "*" * len(text)
    return text[:show_chars] + "*" * (len(text) - show_chars * 2) + text[-show_chars:]


def mask_email(email):
    if not email or "@" not in email:
        return mask_sensitive(email)
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def mask_remark(remark):
    if not remark:
        return "***"
    if "@" in remark:
        return mask_email(remark)
    return mask_sensitive(remark)


def mask_server_id(server_id):
    if not server_id:
        return "***"
    if len(server_id) <= 4:
        return "*" * len(server_id)
    return server_id[:2] + "*" * (len(server_id) - 4) + server_id[-2:]


def random_delay(min_sec=0.5, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))


def calculate_remaining_time(expiry_str):
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
            try:
                expiry_dt = datetime.strptime(expiry_str.strip(), fmt)
                diff = expiry_dt - datetime.now()
                if diff.total_seconds() < 0:
                    return "已过期"
                days = diff.days
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                parts = []
                if days > 0:
                    parts.append(f"{days}天")
                if hours > 0:
                    parts.append(f"{hours}小时")
                if minutes > 0 and days == 0:
                    parts.append(f"{minutes}分钟")
                return " ".join(parts) if parts else "不到1分钟"
            except ValueError:
                continue
        return "无法解析"
    except:
        return "计算失败"


def parse_expiry_to_datetime(expiry_str):
    if not expiry_str or expiry_str == "Unknown":
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(expiry_str.strip(), fmt)
        except ValueError:
            continue
    return None


def get_remaining_days(expiry_str):
    expiry_dt = parse_expiry_to_datetime(expiry_str)
    if not expiry_dt:
        return None
    diff = expiry_dt - datetime.now()
    return diff.total_seconds() / 86400


def format_remaining_days(rd):
    if rd is None:
        return "?"
    return f"{rd:.1f}"


def parse_weirdhost_cookie(cookie_str):
    if not cookie_str:
        return (None, None)
    cookie_str = cookie_str.strip()
    if "=" in cookie_str:
        parts = cookie_str.split("=", 1)
        if len(parts) == 2:
            return (parts[0].strip(), unquote(parts[1].strip()))
    return (None, None)


def build_server_url(server_id):
    if not server_id:
        return None
    server_id = server_id.strip()
    return server_id if server_id.startswith("http") else f"{BASE_URL}{server_id}"


# ============================================================
#  账号自动检测
# ============================================================

def parse_account_config(raw_value):
    if not raw_value:
        return None
    raw_value = raw_value.strip()

    remark = ""
    cookie_str = ""

    if "-----" in raw_value:
        parts = raw_value.split("-----", 1)
        remark = parts[0].strip()
        cookie_str = parts[1].strip() if len(parts) > 1 else ""
    else:
        cookie_str = raw_value

    if not cookie_str or "=" not in cookie_str:
        return None

    cookie_name, cookie_value = parse_weirdhost_cookie(cookie_str)
    if not cookie_name or not cookie_name.startswith("remember_web"):
        return None

    return {
        "remark": remark,
        "cookie_str": cookie_str,
        "cookie_name": cookie_name,
        "cookie_value": cookie_value,
    }


def detect_accounts():
    accounts = []
    for i in range(1, MAX_COOKIE_COUNT + 1):
        env_name = f"WEIRDHOST_COOKIE_{i}"
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue

        config = parse_account_config(raw)
        if not config:
            print(f"[WARN] {env_name} 格式错误，跳过")
            print(f"       正确格式: 备注-----remember_web_xxx=yyy")
            continue

        remark = config["remark"] or f"账号{i}"
        print(f"[INFO] 检测到 {env_name}: {mask_remark(remark)}")

        accounts.append({
            "index": i,
            "cookie_env": env_name,
            "remark": remark,
            "cookie_str": config["cookie_str"],
            "cookie_name": config["cookie_name"],
            "cookie_value": config["cookie_value"],
        })

    return accounts


# ============================================================
#  Telegram 通知
# ============================================================

async def tg_notify(message):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            )
        except Exception as e:
            print(f"[ERROR] TG 发送失败: {e}")


async def tg_notify_photo(photo_path, caption=""):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id or not os.path.exists(photo_path):
        return
    async with aiohttp.ClientSession() as session:
        try:
            with open(photo_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("photo", f, filename=os.path.basename(photo_path))
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
                await session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data)
        except Exception as e:
            print(f"[ERROR] TG 图片发送失败: {e}")


def sync_tg_notify(message):
    asyncio.run(tg_notify(message))


def sync_tg_notify_photo(photo_path, caption=""):
    asyncio.run(tg_notify_photo(photo_path, caption))


# ============================================================
#  GitHub Secret
# ============================================================

def encrypt_secret(public_key, secret_value):
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


async def update_github_secret(secret_name, secret_value):
    repo_token = os.environ.get("REPO_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo_token or not repository or not NACL_AVAILABLE:
        return False
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {repo_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with aiohttp.ClientSession() as session:
        try:
            pk_url = f"https://api.github.com/repos/{repository}/actions/secrets/public-key"
            async with session.get(pk_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                pk_data = await resp.json()
            encrypted_value = encrypt_secret(pk_data["key"], secret_value)
            secret_url = f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}"
            async with session.put(secret_url, headers=headers, json={
                "encrypted_value": encrypted_value, "key_id": pk_data["key_id"]
            }) as resp:
                return resp.status in (201, 204)
        except:
            return False


# ============================================================
#  基于 Selenium 的浏览器内 API 调用
# ============================================================

def api_fetch_json(sb, url, xsrf_token=None):
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{DOMAIN}/",
    }
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = xsrf_token

    script = """
        var done = arguments[arguments.length - 1];
        fetch(arguments[0], {
            headers: arguments[1]
        })
        .then(resp => {
            if (resp.status === 401) return {_error: 'unauthorized'};
            return resp.json();
        })
        .then(data => done(data))
        .catch(err => done({_error: err.toString()}));
    """
    result = sb.driver.execute_async_script(script, url, headers)
    if isinstance(result, dict) and "_error" in result:
        print(f"[ERROR]   fetch 失败: {result['_error']}")
        return None
    return result


def get_xsrf_token_from_cookies(sb):
    try:
        cookies = sb.get_cookies()
        for c in cookies:
            if c.get("name") == "XSRF-TOKEN":
                return unquote(c.get("value", ""))
    except:
        pass
    return None


# ============================================================
#  Turnstile 处理（登录阶段）
# ============================================================

def js_eval(sb, script, default=None, label=""):
    """執行 JS（統一包成 IIFE，兼容 driver / CDP 兩種模式）。

    2026-09-20 定案：UC reconnect 之後 SeleniumBase 會轉入 CDP 模式，
    `sb.execute_script()` 直通 CDP `Runtime.evaluate()`，**頂層 `return`
    會 SyntaxError**（普通 driver.execute_script 會幫你包 function，所以
    平時冇事）。本檔多個 JS 片段用頂層 return，例外又被 `except: return None`
    食咗 → 靜默當「搵唔到 Turnstile」，前線只見到「无法获取 Turnstile 坐标」。
    """
    s = (script or "").strip()
    if not s:
        return default
    if not s.startswith("("):
        s = "(function(){\n" + s + "\n})()"
    try:
        return sb.execute_script(s)
    except Exception as e:
        if label:
            print(f"[WARN]   JS[{label}] 執行失敗: {repr(e)[:150]}")
        return default


def cf_page_diag(sb, tag=""):
    """診斷當前頁面形態（下一次 run 直接睇 log 定案，唔使靠估）。

    JS 行唔通（CDP session 死）時會退回 page_source regex，照樣攞到 title/url。
    """
    info = js_eval(sb, """
        var out = {
            url: location.href, title: document.title,
            has_widget: !!document.querySelector('.cf-turnstile'),
            has_input: !!document.querySelector('input[name="cf-turnstile-response"]'),
            has_stage: !!document.querySelector('#challenge-stage, #challenge-form, #cf-chl-widget-container'),
            iframes: []
        };
        var f = document.querySelectorAll('iframe');
        for (var i = 0; i < f.length && i < 10; i++) {
            var r = f[i].getBoundingClientRect();
            out.iframes.push({src: (f[i].src || '').slice(0, 80), x: Math.round(r.x),
                              y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)});
        }
        out.text = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 300);
        return out;
    """, default=None, label="cf_page_diag")
    if info:
        print(f"[DIAG] {tag} " + json.dumps(info, ensure_ascii=False)[:1000])
        return info
    # JS 行唔通 → 用 page_source 補救，唔好白白冇證據
    src = ""
    try:
        src = sb.get_page_source() or ""
    except Exception as e:
        print(f"[DIAG] {tag} 連 page_source 都攞唔到: {repr(e)[:100]}")
    m = re.search(r"<title[^>]*>(.*?)</title>", src, re.S | re.I)
    title = (m.group(1).strip()[:120] if m else "(冇 title)")
    try:
        url = sb.get_current_url()
    except Exception:
        url = "(未知)"
    ifr = re.findall(r'<iframe[^>]+src="([^"]{0,90})"', src)
    print(f"[DIAG] {tag} JS 行唔通 → url={url}｜title={title!r}｜iframe 數={len(ifr)} | "
          f"{ifr[:4]}｜source 長度={len(src)}")
    return None


def ts_exists(sb):
    return bool(js_eval(sb, """
        return !!(
            document.querySelector('input[name="cf-turnstile-response"]') ||
            document.querySelector('.cf-turnstile') ||
            document.querySelector('iframe[src*="challenges.cloudflare.com"]')
        );
    """, default=False, label="ts_exists"))


def ts_solved(sb):
    return bool(js_eval(sb, """
        var i = document.querySelector('input[name="cf-turnstile-response"]');
        return !!(i && i.value && i.value.length > 20);
    """, default=False, label="ts_solved"))


def expand_turnstile(sb):
    try:
        sb.execute_script("""
            (function() {
                var ti = document.querySelector('input[name="cf-turnstile-response"]');
                if (!ti) return;
                var el = ti;
                for (var i = 0; i < 20; i++) {
                    el = el.parentElement;
                    if (!el) break;
                    var s = window.getComputedStyle(el);
                    if (s.overflow === 'hidden') el.style.overflow = 'visible';
                    el.style.minWidth = 'max-content';
                }
                document.querySelectorAll('.cf-turnstile').forEach(function(c) {
                    c.style.overflow = 'visible';
                    c.style.width = '300px';
                    c.style.height = '65px';
                });
                document.querySelectorAll('iframe').forEach(function(f) {
                    if (f.src && f.src.includes('challenges.cloudflare.com')) {
                        f.style.width = '300px';
                        f.style.height = '65px';
                        f.style.visibility = 'visible';
                        f.style.opacity = '1';
                    }
                });
            })();
        """)
    except:
        pass


def focus_turnstile_area(sb):
    js_eval(sb, """
        const selectors = [
            '.cf-turnstile',
            'iframe[src*="challenges.cloudflare"]',
            'input[name="cf-turnstile-response"]',
            'label.cb-lb',
            '.cb-lb',
            'input[type="checkbox"]'
        ];
        for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                return true;
            }
        }
        window.scrollTo(0, Math.max(0, document.body.scrollHeight * 0.45));
        return false;
    """, label="focus_turnstile_area")
    time.sleep(0.5)


def handle_turnstile(sb, timeout=120):
    if not ts_exists(sb):
        return True
    print("[INFO]   检测到 Turnstile，尝试自动解决...")
    try:
        sb.uc_gui_handle_captcha()
        if ts_solved(sb) or not ts_exists(sb):
            print("[INFO]   自动解决成功 ✅")
            return True
    except:
        pass

    print("[INFO]   自动解决未完成，进入手动处理 ...")
    start = time.time()
    last_action = 0
    while time.time() - start < timeout:
        if ts_solved(sb):
            print("[INFO]   Turnstile 令牌已生成 ✅")
            return True
        if not ts_exists(sb):
            print("[INFO]   Turnstile 元素消失，可能已通过")
            return True

        expand_turnstile(sb)
        focus_turnstile_area(sb)

        now = time.time()
        if now - last_action > 4:
            clicked = False
            try:
                iframes = sb.driver.find_elements("css selector", "iframe")
                for iframe in iframes:
                    try:
                        sb.driver.switch_to.frame(iframe)
                        for sel in ["input[type='checkbox']", "label.cb-lb", ".cb-lb input"]:
                            try:
                                elem = sb.driver.find_element("css selector", sel)
                                if elem.is_displayed():
                                    elem.click()
                                    clicked = True
                                    print(f"[INFO]   在 iframe 中点击了 {sel}")
                                    break
                            except:
                                pass
                        if clicked:
                            break
                    except:
                        pass
                    finally:
                        sb.driver.switch_to.default_content()
            except Exception as e:
                print(f"[WARN]   iframe 点击异常: {e}")
            finally:
                try:
                    sb.driver.switch_to.default_content()
                except:
                    pass

            if not clicked:
                try:
                    sb.uc_gui_click_captcha()
                    print("[INFO]   已调用 uc_gui_click_captcha（备用）")
                except:
                    pass
            last_action = now

        time.sleep(2)

    print("[ERROR]  Turnstile 手动处理超时 ❌")
    return False


# ============================================================
#  Turnstile 处理（续期阶段）
# ============================================================

def check_turnstile_exists_popup(sb):
    try:
        return sb.execute_script(
            "return document.querySelector('input[name=\"cf-turnstile-response\"]') !== null;"
        )
    except:
        return False

def check_turnstile_solved_popup(sb):
    try:
        return sb.execute_script("""
            var input = document.querySelector('input[name="cf-turnstile-response"]');
            return input && input.value && input.value.length > 20;
        """)
    except:
        return False

EXPAND_POPUP_JS = """
(function() {
    var turnstileInput = document.querySelector('input[name="cf-turnstile-response"]');
    if (!turnstileInput) return 'no turnstile input';
    var el = turnstileInput;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var style = window.getComputedStyle(el);
        if (style.overflow === 'hidden' || style.overflowX === 'hidden' || style.overflowY === 'hidden') {
            el.style.overflow = 'visible';
        }
        el.style.minWidth = 'max-content';
    }
    var turnstileContainers = document.querySelectorAll('[class*="sc-fKFyDc"], [class*="nwOmR"]');
    turnstileContainers.forEach(function(container) {
        container.style.overflow = 'visible';
        container.style.width = '300px';
        container.style.minWidth = '300px';
        container.style.height = '65px';
    });
    var iframes = document.querySelectorAll('iframe');
    iframes.forEach(function(iframe) {
        if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) {
            iframe.style.width = '300px';
            iframe.style.height = '65px';
            iframe.style.minWidth = '300px';
            iframe.style.visibility = 'visible';
            iframe.style.opacity = '1';
        }
    });
    return 'done';
})();
"""

def mark_viewport_point(sb, x, y):
    """喺 viewport 座標 (x,y) 畫一個紅色十字，用嚟喺截圖度核對「我哋以為嘅位置」。

    2026-09-20 加：run 35500018550 真鼠標出咗（screen 165,453），但 cf_fail.png
    睇到 widget 其實喺圖片座標 (~196,304) 而 CDP 報 (115,280) —— 兩者差 ~80px。
    紅十字就係「CDP 以為嘅點」，同截圖對照即知座標系錯喺邊。
    """
    return js_eval(sb, """
        (function(){
          var old = document.getElementById('__probe_mark');
          if (old) old.remove();
          var d = document.createElement('div');
          d.id = '__probe_mark';
          d.style.cssText = 'position:fixed;left:' + (x - 9) + 'px;top:' + (y - 9) +
            'px;width:18px;height:18px;border:2px solid #ff0000;border-radius:50%;' +
            'background:rgba(255,0,0,.45);z-index:2147483647;pointer-events:none;';
          (document.body || document.documentElement).appendChild(d);
          var r = d.getBoundingClientRect();
          return {mark_left: r.left, mark_top: r.top, scrollX: window.scrollX || 0,
                  scrollY: window.scrollY || 0, dpr: window.devicePixelRatio || 1,
                  iw: window.innerWidth, ih: window.innerHeight};
        })();
    """, default=None, label="mark_viewport_point")


def mouse_loc():
    """讀返 X server 上真實嘅鼠標位置（xdotool getmouselocation），核對有冇被 clamp。"""
    try:
        out = subprocess.run(["xdotool", "getmouselocation"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        d = dict(p.split(":", 1) for p in out.split() if ":" in p)
        return int(d.get("x", -1)), int(d.get("y", -1))
    except Exception:
        return -1, -1


def detect_offset_from_image(path):
    """由截圖自動搵「藍色 Turnstile widget 框」嘅實際位置，同 CDP 報嘅座標對照。

    用純 PIL（GHA 有裝 Pillow）掃描：CF 託管挑戰嘅 widget 係白色底 + 淺灰邊框，
    但最穩陣嘅特徵係「checkbox 方格」（深色邊、白色內部）同右邊 Cloudflare logo
    （橙色）。呢度搵橙色像素團（CF logo）嘅質心 —— 佢一定喺 widget 右半邊，
    據此推算 iframe 左邊界同垂直中心，用嚟校正點擊座標。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    px = im.load()
    orange_xs, orange_ys = [], []
    # CF logo 橙 = 約 (246,130,31)；容差放寬
    for yy in range(0, h, 2):
        for xx in range(0, w, 2):
            r, g, b = px[xx, yy]
            if r > 200 and 90 < g < 165 and b < 90:
                orange_xs.append(xx)
                orange_ys.append(yy)
    if len(orange_xs) < 8:
        return None
    logo_cx = sum(orange_xs) / len(orange_xs)
    logo_cy = sum(orange_ys) / len(orange_ys)
    print(f"[DIAG]   截圖 {w}x{h}：CF logo 橙色像素 {len(orange_xs)} 個，質心=({logo_cx:.0f},{logo_cy:.0f})")
    # Turnstile 標準 widget 300x65：logo 喺右邊約 x=+250 處（距左邊界），垂直居中
    return {"left": round(logo_cx - 250), "center_y": round(logo_cy),
            "checkbox": (round(logo_cx - 250 + 30), round(logo_cy))}


def cdp_shadow_click(sb):
    """用 CDP 穿透 **closed shadow DOM** 搵 Turnstile widget / checkbox，再用真鼠標事件點佢。

    2026-09-20 實測（run 35496893092）：hub.weirdhost.xyz 撞 CF 攔截頁
    （title「잠시만 기다리십시오…」/ 보안 확인 수행 중），頁面 iframes=[]、
    .cf-turnstile 唔見，但 light DOM 有 input[name=cf-turnstile-response]
    ——即 CF 新版攔截頁將 widget 收咗喺 closed shadow root 入面，普通
    document.querySelector 永遠搵唔到（所以之前一直「无法获取 Turnstile 坐标」）。
    CDP `DOM.getDocument(pierce=True)` 睇得穿 closed shadow root。
    呢個只係「用真鼠標點個 widget」，唔係解 captcha。
    """
    try:
        doc = sb.driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})
    except Exception as e:
        print(f"[WARN]   [CDP] DOM.getDocument 失敗: {repr(e)[:110]}")
        return False

    found = []

    def _attrs(n):
        a = n.get("attributes") or []
        return {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}

    def _walk(n):
        try:
            name = (n.get("nodeName") or "").lower()
            am = _attrs(n)
            blob = " ".join(str(am.get(k, "")) for k in
                            ("src", "class", "id", "name", "type", "aria-label")).lower()
            if name in ("iframe", "input", "div", "span", "label", "button", "cf-turnstile",
                        "shadow-root", "challenge") and (
                    "turnstile" in blob or "challenge" in blob or "checkbox" in blob
                    or name in ("iframe", "cf-turnstile")):
                found.append((n.get("nodeId"), name, am, blob[:70]))
        except Exception:
            pass
        for key in ("children", "shadowRoots", "contentDocument"):
            for c in (n.get(key) or []):
                _walk(c)

    _walk(doc.get("root", {}))
    if found:
        print(f"[INFO]   [CDP] pierce 掃到 {len(found)} 個候選: "
              f"{[(f[1], f[3]) for f in found[:4]]}")
    else:
        print("[WARN]   [CDP] pierce 都掃唔到任何候選節點")
    for node_id, name, am, blob in found[:8]:
        try:
            box = sb.driver.execute_cdp_cmd("DOM.getBoxModel", {"nodeId": node_id})
            quad = box["model"]["content"]
            xs, ys = quad[0::2], quad[1::2]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w < 4 or h < 4:
                continue
            cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
            if name in ("iframe", "cf-turnstile") and w > 100:
                cx, cy = min(xs) + 30, min(ys) + h / 2.0   # Turnstile checkbox 喺左邊
            print(f"[INFO]   [CDP] 點 {name} box=({int(min(xs))},{int(min(ys))},{int(w)}x{int(h)})"
                  f" → ({int(cx)},{int(cy)})")
            # 2026-09-20：改用 OS 級真鼠標（xdotool）點，唔再用 CDP 合成事件。
            # 證據：run 35497523552 —— 合成 mousePressed 確實觸發到 widget，
            # 但 CF instrumentation 認得出係非真人輸入，挑戰即刻換個 widget id
            # 重發（26 輪死循環：hfb6b → vn9bo → bq2mw → m7tzl…）。
            # xdotool 出嘅係 X server 層事件，來源同真人滑鼠一樣。
            # 座標核對（2026-09-20）：畫紅十字喺 CDP 報嘅 viewport 點 → 截圖 → 由圖反推
            # widget 真實位置。run 35500018550 顯示點擊前後 widget id 都變，但 cf_fail.png
            # 見 widget 喺圖片 (~196,304)，而 CDP 報 (115,280)：即可能一直撳錯位。
            try:
                mk = mark_viewport_point(sb, cx, cy)
                if mk:
                    print(f"[DIAG]   紅十字 viewport ({int(cx)},{int(cy)}) → rect "
                          f"({mk.get('mark_left')},{mk.get('mark_top')}) | scroll="
                          f"({mk.get('scrollX')},{mk.get('scrollY')}) dpr={mk.get('dpr')} "
                          f"viewport={mk.get('iw')}x{mk.get('ih')}")
            except Exception as e:
                print(f"[WARN]   畫標記失敗: {repr(e)[:80]}")
            clicked = gui_click_viewport(sb, cx, cy)
            mx, my = mouse_loc()
            print(f"[DIAG]   xdotool 讀返鼠標位置=({mx},{my})")
            try:
                sb.save_screenshot("cf_click_mark.png")
                det = detect_offset_from_image("cf_click_mark.png")
                cy_mid = int((min(ys) + max(ys)) / 2)
                if det:
                    dx = det["left"] - int(min(xs))
                    dy = det["center_y"] - cy_mid
                    print(f"[DIAG]   圖像反推 widget left={det['left']} center_y={det['center_y']} "
                          f"vs CDP left={int(min(xs))} center_y={cy_mid} → 偏差 "
                          f"dx={dx} dy={dy}（建議撳 ({det['checkbox'][0]},{det['checkbox'][1]})）")
                    # 偏差大 = CDP 座標唔可信 → 即刻用圖像反推嘅點補撳（自我校正）
                    if abs(dx) > 15 or abs(dy) > 10:
                        bx, by = det["checkbox"]
                        print(f"[INFO]   CDP 座標偏差過大 → 改用圖像座標補撳 ({bx},{by})")
                        gui_click_viewport(sb, bx, by)
                        time.sleep(2)
                        try:
                            sb.save_screenshot("cf_click_mark2.png")
                            det2 = detect_offset_from_image("cf_click_mark2.png")
                            if det2:
                                print(f"[DIAG]   補撳後 widget left={det2['left']} "
                                      f"center_y={det2['center_y']}")
                        except Exception:
                            pass
                        return True
                else:
                    print("[WARN]   圖像反推失敗（搵唔到 CF logo 橙色像素）")
            except Exception as e:
                print(f"[WARN]   截圖/圖像分析失敗: {repr(e)[:90]}")
            if clicked:
                print("[INFO]   [CDP] 已用真鼠標點 widget ✅")
                return True
            print("[WARN]   [CDP] 真鼠標點唔到，退回 CDP 合成事件")
            sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                                      {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"})
            time.sleep(0.15)
            for ev in ("mousePressed", "mouseReleased"):
                sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                                          {"type": ev, "x": cx, "y": cy, "button": "left",
                                           "clickCount": 1})
                time.sleep(0.08)
            return True
        except Exception:
            continue
    return False


def get_turnstile_checkbox_coords(sb):
    """攞 Turnstile checkbox 嘅頁面座標（相對 viewport）。

    2026-09-20 改：①改經 js_eval（CDP 模式下頂層 return 會爆，之前靜默返 None）；
    ②擴闊搜尋（任何 iframe / .cf-turnstile 容器 / input 父鏈 / shadow DOM），
    並且將「搵唔到」嘅原因寫入 log，唔再靜默。
    """
    coords = js_eval(sb, """
        function visible(el) {
            var r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }
        function pick(el) {
            var r = el.getBoundingClientRect();
            return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width),
                    height: Math.round(r.height),
                    click_x: Math.round(r.x + 30), click_y: Math.round(r.y + r.height / 2)};
        }
        var cf_sel = 'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]';
        var hit = document.querySelector(cf_sel);
        if (hit && visible(hit)) return pick(hit);
        var widget = document.querySelector('.cf-turnstile, [id*="turnstile"], [class*="turnstile"]');
        if (widget && visible(widget)) return pick(widget);
        var input = document.querySelector('input[name="cf-turnstile-response"]');
        if (input) {
            var el = input;
            for (var i = 0; i < 8 && el; i++) {
                if (visible(el) && el.getBoundingClientRect().width > 100) return pick(el);
                el = el.parentElement;
            }
        }
        var all = document.querySelectorAll('iframe');
        for (var j = 0; j < all.length; j++) {
            if (visible(all[j])) return pick(all[j]);
        }
        return {error: 'no_turnstile_element', iframes: all.length,
                body_len: document.body ? document.body.innerText.length : 0};
    """, default=None, label="get_turnstile_checkbox_coords")
    if not coords:
        print("[WARN] 无法获取 Turnstile 坐标（JS 執行失敗）")
        return None
    if coords.get("error"):
        print(f"[WARN] 无法获取 Turnstile 坐标（{coords['error']}，頁面 iframe 數={coords.get('iframes')}）")
        return None
    return coords

def activate_browser_window():
    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "chrome"],
            capture_output=True, text=True, timeout=3
        )
        window_ids = result.stdout.strip().split('\n')
        if window_ids and window_ids[0]:
            subprocess.run(
                ["xdotool", "windowactivate", window_ids[0]],
                timeout=2, stderr=subprocess.DEVNULL
            )
            time.sleep(0.2)
            return True
    except:
        pass
    return False

def xdotool_click(x, y):
    x, y = int(x), int(y)
    try:
        activate_browser_window()
    except Exception:
        pass
    try:
        r1 = subprocess.run(["xdotool", "mousemove", "--clearmodifiers", str(x), str(y)],
                            capture_output=True, text=True, timeout=3)
        time.sleep(0.25)   # 讓 CF 睇到指針先移動到 widget 上，再落 click
        r2 = subprocess.run(["xdotool", "click", "--clearmodifiers", "1"],
                            capture_output=True, text=True, timeout=3)
        if r1.returncode == 0 and r2.returncode == 0:
            return True
        print(f"[WARN]   xdotool 失敗: move rc={r1.returncode} {r1.stderr.strip()[:50]} | "
              f"click rc={r2.returncode} {r2.stderr.strip()[:50]}")
    except Exception as e:
        print(f"[WARN]   xdotool 異常: {repr(e)[:90]}")
    try:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")
        return True
    except Exception:
        return False


def gui_click_viewport(sb, cx, cy):
    """將 viewport 座標換成 X11 絕對座標，再用 xdotool 出**真鼠標**點擊。

    2026-09-20 新增。CDP `Input.dispatchMouseEvent` 出嘅係合成事件，CF 嘅
    挑戰頁 instrumentation 會標記佢（實測：撳完即刻換 widget id 重發，26 輪都
    過唔到）。X server 層嘅事件來源同真人無異，所以做一次座標換算。
    window.screenX/screenY + Chrome 工具列高度 = viewport 左上角嘅螢幕座標。
    JS 行唔通（CDP session 死）時退回 xdotool 攞窗口幾何。
    """
    info = js_eval(sb, """
        return {sx: window.screenX || 0, sy: window.screenY || 0,
                oh: window.outerHeight || 0, ih: window.innerHeight || 0,
                ox: window.outerWidth || 0, iw: window.innerWidth || 0};
    """, default=None, label="gui_click_window_info")
    if not info:
        try:
            info = sb.execute_script("""
                return {sx: window.screenX || 0, sy: window.screenY || 0,
                        oh: window.outerHeight || 0, ih: window.innerHeight || 0,
                        ox: window.outerWidth || 0, iw: window.innerWidth || 0};
            """)
        except Exception:
            info = None
    sx = sy = 0.0
    bar, side = 88.0, 0.0
    if info:
        try:
            sx = float(info.get("sx") or 0)
            sy = float(info.get("sy") or 0)
            oh, ih = float(info.get("oh") or 0), float(info.get("ih") or 0)
            ox, iw = float(info.get("ox") or 0), float(info.get("iw") or 0)
            if oh > ih > 0:
                bar = oh - ih
            if ox > iw > 0:
                side = (ox - iw) / 2.0
        except Exception:
            pass
    else:
        # JS 全靜默：用 xdotool 攞窗口位置，假設窗口貼 X 原點（xvfb 冇 WM）
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "chrome"],
                               capture_output=True, text=True, timeout=3)
            wid = (r.stdout.strip().split("\n") or [""])[0]
            if wid:
                g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                   capture_output=True, text=True, timeout=3).stdout
                gd = dict(l.split("=", 1) for l in g.strip().splitlines() if "=" in l)
                sx, sy = float(gd.get("X", 0)), float(gd.get("Y", 0))
        except Exception:
            pass
    abs_x, abs_y = sx + side + cx, sy + bar + cy
    print(f"[INFO]   真鼠標 → screen=({int(abs_x)},{int(abs_y)}) "
          f"[viewport ({int(cx)},{int(cy)}) + window ({int(sx)},{int(sy)}) + bar {int(bar)}]")
    return xdotool_click(abs_x, abs_y)


def click_turnstile_checkbox(sb):
    coords = get_turnstile_checkbox_coords(sb)
    if not coords:
        print("[WARN] 无法获取 Turnstile 坐标")
        return False
    try:
        window_info = sb.execute_script("""
            return {screenX:window.screenX||0, screenY:window.screenY||0,
                    outerHeight:window.outerHeight, innerHeight:window.innerHeight};
        """)
        chrome_bar_height = window_info["outerHeight"] - window_info["innerHeight"]
        abs_x = coords["click_x"] + window_info["screenX"]
        abs_y = coords["click_y"] + window_info["screenY"] + chrome_bar_height
        return xdotool_click(abs_x, abs_y)
    except Exception as e:
        print(f"[ERROR] 坐标计算失败: {e}")
        return False

def check_result_popup(sb):
    try:
        return sb.execute_script("""
            var buttons = document.querySelectorAll('button');
            var hasNextBtn = false;
            for (var i = 0; i < buttons.length; i++) {
                if (buttons[i].innerText.includes('NEXT') || buttons[i].innerText.includes('Next')) {
                    hasNextBtn = true; break;
                }
            }
            var bodyText = document.body.innerText || '';
            var hasSuccessTitle = bodyText.includes('Success');
            var hasSuccessContent = bodyText.includes('성공') || bodyText.includes('갱신') || bodyText.includes('연장');
            var hasCooldown = bodyText.includes('아직') || bodyText.includes('Error');
            if (hasNextBtn || hasSuccessTitle) {
                if (hasCooldown && bodyText.includes('아직')) return 'cooldown';
                if (hasSuccessTitle && hasSuccessContent) return 'success';
                if (hasNextBtn) {
                    if (hasCooldown) return 'cooldown';
                    if (hasSuccessContent) return 'success';
                }
            }
            return null;
        """)
    except:
        return None

def check_popup_still_open(sb):
    try:
        return sb.execute_script("""
            var t = document.querySelector('input[name="cf-turnstile-response"]');
            if (!t) return false;
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = buttons[i].innerText || '';
                if ((text.includes('시간추가') || text.includes('시간 추가') || text.includes('연장하기'))
                    && !text.includes('DELETE')) {
                    var rect = buttons[i].getBoundingClientRect();
                    if (rect.x > 200 && rect.width > 0) return true;
                }
            }
            return false;
        """)
    except:
        return False

def click_next_button(sb):
    try:
        for sel in [
            "//button[contains(text(), 'NEXT')]",
            "//button[contains(text(), 'Next')]",
            "//button//span[contains(text(), 'NEXT')]",
        ]:
            if sb.is_element_visible(sel):
                sb.click(sel)
                print("[INFO] 已点击 NEXT 按钮")
                return True
    except:
        pass
    return False

def handle_renewal_popup(sb, screenshot_prefix="", timeout=90):
    screenshot_name = f"{screenshot_prefix}_popup.png" if screenshot_prefix else "popup_fixed.png"

    print("[INFO]   [阶段1] 等待弹窗和 Turnstile...")

    turnstile_ready = False
    for _ in range(20):
        result = check_result_popup(sb)
        if result == "cooldown":
            print("[INFO]   检测到冷却期弹窗")
            sb.save_screenshot(screenshot_name)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if result == "success":
            print("[INFO]   检测到成功弹窗")
            sb.save_screenshot(screenshot_name)
            return {"status": "success", "screenshot": screenshot_name}
        if check_turnstile_exists_popup(sb):
            turnstile_ready = True
            print("[INFO]   检测到 Turnstile")
            break
        time.sleep(1)

    if not turnstile_ready:
        print("[WARN]   未检测到 Turnstile")
        sb.save_screenshot(screenshot_name)
        return {"status": "error", "message": "未检测到 Turnstile", "screenshot": screenshot_name}

    print("[INFO]   [阶段2] 修复弹窗样式...")
    for _ in range(3):
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.5)
    sb.save_screenshot(screenshot_name)

    print("[INFO]   [阶段3] 点击 Turnstile...")
    for attempt in range(6):
        if check_turnstile_solved_popup(sb):
            print("[INFO]   Turnstile 已通过!")
            break
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.3)
        click_turnstile_checkbox(sb)
        for _ in range(8):
            time.sleep(0.5)
            if check_turnstile_solved_popup(sb):
                print("[INFO]   Turnstile 已通过!")
                break
        if check_turnstile_solved_popup(sb):
            break
        sb.save_screenshot(
            f"{screenshot_prefix}_turnstile_{attempt}.png" if screenshot_prefix
            else f"turnstile_attempt_{attempt}.png"
        )

    print("[INFO]   等待提交结果...")
    result_start = time.time()
    last_screenshot_time = 0

    while time.time() - result_start < 45:
        result = check_result_popup(sb)
        if result == "success":
            print("[INFO]   续期成功!")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "success", "screenshot": screenshot_name}
        if result == "cooldown":
            print("[INFO]   冷却期内")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if not check_popup_still_open(sb):
            time.sleep(2)
            result = check_result_popup(sb)
            if result:
                sb.save_screenshot(screenshot_name)
                if result == "success":
                    click_next_button(sb)
                    return {"status": "success", "screenshot": screenshot_name}
                elif result == "cooldown":
                    click_next_button(sb)
                    return {"status": "cooldown", "screenshot": screenshot_name}
        if time.time() - last_screenshot_time > 5:
            sb.save_screenshot(screenshot_name)
            last_screenshot_time = time.time()
        time.sleep(1)

    print("[WARN]   等待结果超时")
    sb.save_screenshot(screenshot_name)
    return {"status": "timeout", "screenshot": screenshot_name}


# ============================================================
#  SeleniumBase 页面交互（通用）
# ============================================================

def get_expiry_from_page(sb):
    try:
        page_text = sb.get_page_source()
        match = re.search(r'유통기한\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if match:
            return match.group(1).strip()
        match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if match:
            return match.group(1).strip()
        return "Unknown"
    except:
        return "Unknown"


def find_renewal_button(sb):
    for selector in RENEWAL_BUTTON_SELECTORS:
        try:
            if sb.is_element_present(selector):
                return selector
        except:
            continue
    return None


def check_renewal_button_enabled(sb):
    xpath = find_renewal_button(sb)
    if not xpath:
        return (False, False, None, "页面上未找到续期按钮")

    try:
        is_disabled = sb.execute_script(f"""
            var btn = document.evaluate("{xpath}", document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!btn) return null;
            return btn.disabled || btn.getAttribute('aria-disabled') === 'true'
                   || btn.classList.contains('disabled');
        """)
        if is_disabled is None:
            return (False, False, None, "按钮元素无法访问")
        if is_disabled:
            return (True, False, xpath, "续期按钮已禁用（可能在冷却期）")
    except:
        pass

    return (True, True, xpath, "")


def is_cloudflare_challenge(sb):
    """當前頁面係唔係 Cloudflare 驗證頁（唔係登入頁，亦唔代表 cookie 失效）。

    2026-09-18 定案：撞 CF 挑戰頁時 `is_logged_in()` 回 False，腳本誤報
    「Cookie 已失效」——實際 cookie 可能好地地，只係卡喺 CF 挑戰頁。
    CF 按瀏覽器語言出唔同字（實測韓文「보안 확인 수행 중」），所以同時認 DOM 特徵。
    """
    try:
        src = sb.get_page_source() or ""
    except:
        return False
    markers = (
        "보안 확인",            # 韓文：正在進行安全檢查
        "Just a moment",
        "cf-turnstile",
        "cf_chl_opt",
        "challenges.cloudflare.com",
        "Attention Required",
    )
    return any(m in src for m in markers)


def solve_cf_interstitial(sb, timeout=100):
    """處理 Cloudflare 攔截頁（보안 확인 수행 중 / Just a moment）。

    唔可以叫 handle_turnstile()：實測 CF 攔截頁上 `ts_exists()` 會回 False
    （cf-turnstile-response input 未 render／藏在未完成載入嘅 iframe），
    令重解邏輯一 call 就「無 Turnstile → 當通過」空轉（run 35329272778 實測：
    三次重解各只用 4 秒，全部都係空轉）。
    呢度改為唔靠 ts_exists，直接掄 UC 模式嘅 GUI 處理 + 檢查頁面係唔係仲係
    攔截頁。
    """
    if not is_cloudflare_challenge(sb):
        return True
    print("[INFO]   處理 Cloudflare 攔截頁...")
    cf_page_diag(sb, tag="[CF 攔截頁進入]")
    start = time.time()
    click_clock = 0
    rounds = 0
    # 2026-09-20：撳少啲、等耐啲。舊版每 5 秒撳一次（100 秒撳 ~20 次），
    # CF 每次都當成新一次互動 → 挑戰反覆重發（run 35497523552 死循環 26 輪）。
    # 真人係撳一次、等十零秒；所以跟真人節奏，最多撳 3 次。
    MAX_CLICKS = 3
    CLICK_INTERVALS = [0, 12, 30]
    clicks = 0
    while time.time() - start < timeout:
        rounds += 1
        if not is_cloudflare_challenge(sb) or ts_solved(sb):
            print("[INFO]   Cloudflare 攔截頁已通過 ✅")
            return True
        try:
            sb.uc_gui_handle_captcha()
        except:
            pass
        try:
            expand_turnstile(sb)
            focus_turnstile_area(sb)
        except:
            pass
        now = time.time()
        if clicks < MAX_CLICKS and now - click_clock > CLICK_INTERVALS[clicks]:
            print(f"[INFO]   第 {clicks + 1}/{MAX_CLICKS} 次點 CF widget（之後等 "
                  f"{CLICK_INTERVALS[clicks] if clicks else 12}s 再考慮補撳）")
            clicks += 1
            clicked = False
            # JS 睇唔到 widget（closed shadow DOM）→ 先用 CDP 穿透搵座標，再出真鼠標
            try:
                clicked = cdp_shadow_click(sb)
            except Exception as e:
                print(f"[WARN]   [CDP] 點擊異常: {repr(e)[:100]}")
            if not clicked:
                try:
                    clicked = click_turnstile_checkbox(sb)
                except Exception:
                    pass
            click_clock = now
        if rounds % 12 == 0:
            # 卡住 30 秒以上：刷新一次（CF 託管挑戰刷一刷有時就過，亦更新 challenge token）
            try:
                sb.refresh()
                print("[INFO]   卡住，刷新頁面再試")
                time.sleep(3)
            except Exception as e:
                print(f"[WARN]   刷新失敗: {repr(e)[:80]}")
        time.sleep(2)
    still_blocked = is_cloudflare_challenge(sb)
    if still_blocked:
        # 失敗現場留證：截圖 + 頁面形態（artifact 會 upload *.png）
        try:
            sb.save_screenshot("cf_fail.png")
            print("[INFO]   已保存失敗截圖 cf_fail.png")
        except Exception as e:
            print(f"[WARN]   截圖失敗: {repr(e)[:80]}")
        cf_page_diag(sb, tag=f"[CF 攔截頁失敗，共 {rounds} 輪]")
    return not still_blocked


def goto(sb, url, retries=2, wait=3):
    """導航到 url；撞 Cloudflare 挑戰就即場再解一次。

    原本全程用 `uc_open_with_reconnect()`，佢會 disconnect／reconnect CDP，
    CF 當成新 session 再出一次挑戰（09-15~09-18 四個 run 全部「cookie_invalid」
    嘅真因）。改用普通導航保住已解嘅 cf_clearance，撞到先補解。
    """
    for attempt in range(retries + 1):
        try:
            sb.get(url)
        except Exception as e:
            print(f"[WARN]   導航失敗: {repr(e)[:120]}")
        time.sleep(wait)
        if not is_cloudflare_challenge(sb):
            return True
        print(f"[WARN]   導航到 {url} 撞 Cloudflare，重解驗證（{attempt + 1}/{retries + 1}）...")
        if not solve_cf_interstitial(sb):
            print("[ERROR]   Cloudflare 驗證未通過")
            return False
    return not is_cloudflare_challenge(sb)


def is_logged_in(sb):
    try:
        url = sb.get_current_url()
        if "/login" in url or "/auth" in url:
            return False
        if get_expiry_from_page(sb) != "Unknown":
            return True
        if find_renewal_button(sb):
            return True
        if sb.is_element_present("//div[contains(@class,'ServerControls')]") or \
           sb.is_element_present("//a[contains(@href,'/server/')]"):
            return True
        return False
    except:
        return False


def check_and_update_cookie(sb, cookie_env, original_cookie_value, remark=""):
    try:
        cookies = sb.get_cookies()
        for cookie in cookies:
            if cookie.get("name", "").startswith("remember_web"):
                new_val = cookie.get("value", "")
                c_name = cookie.get("name", "")
                if new_val and new_val != original_cookie_value:
                    new_cookie_str = f"{c_name}={new_val}"
                    if remark:
                        new_secret_value = f"{remark}-----{new_cookie_str}"
                    else:
                        new_secret_value = new_cookie_str
                    print(f"[INFO]   Cookie 已变化，更新 {cookie_env}...")
                    if asyncio.run(update_github_secret(cookie_env, new_secret_value)):
                        print(f"[INFO]   ✅ {cookie_env} 已更新")
                        return True
                    else:
                        print(f"[ERROR]  ❌ {cookie_env} 更新失败")
                        return False
                break
    except Exception as e:
        print(f"[ERROR]  Cookie 检查失败: {e}")
    return False


# ============================================================
#  单个服务器续期处理
# ============================================================

def process_single_server(sb, server_info, cookie_name, cookie_value, cookie_str,
                          cookie_env, remark, screenshot_prefix):
    server_id = server_info.get("identifier", "Unknown")
    server_uuid = server_info.get("uuid", "")
    server_type = server_info.get("server_type", "notfree")
    server_name = server_info.get("name", "")
    api_expiry = server_info.get("expire", "Unknown")

    srv_result = {
        "server_id": server_id,
        "server_uuid": server_uuid,
        "server_type": server_type,
        "server_name": server_name,
        "status": "unknown",
        "original_expiry": api_expiry,
        "new_expiry": api_expiry,
        "message": "",
        "screenshot": None,
        "cookie_updated": False,
    }

    server_url = build_server_url(server_id)
    rd = get_remaining_days(api_expiry)
    dd = format_remaining_days(rd)

    print(f"\n  {'─' * 50}")
    print(f"  [INFO] 服务器: {mask_server_id(server_id)} [{server_type}] {server_name}")
    print(f"  [INFO] 到期: {api_expiry} | 剩余: {calculate_remaining_time(api_expiry)} ({dd}天)")
    print(f"  [INFO] 访问服务器页面...")

    try:
        goto(sb, server_url)

        if not is_logged_in(sb):
            sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
            goto(sb, server_url)

        if not is_logged_in(sb):
            ss_path = f"{screenshot_prefix}_login_fail.png"
            sb.save_screenshot(ss_path)
            msg = ("卡在 Cloudflare 验证页" if is_cloudflare_challenge(sb)
                   else "浏览器登录失败")
            srv_result.update(status="error", message=msg, screenshot=ss_path)
            print(f"  [ERROR] {msg}")
            return srv_result

        print(f"  [INFO] 登录成功")

        page_expiry = get_expiry_from_page(sb)
        if page_expiry != "Unknown":
            srv_result["original_expiry"] = page_expiry

        print(f"  [INFO] 检查续期按钮...")
        btn_found, btn_enabled, btn_xpath, btn_reason = check_renewal_button_enabled(sb)

        if not btn_found:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_no_btn.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result

        if not btn_enabled:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_btn_disabled.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result

        print(f"  [INFO] 续期按钮可用，执行续期")

        random_delay(1.0, 2.0)
        sb.click(btn_xpath)
        print(f"  [INFO] 已点击续期按钮，等待弹窗...")
        time.sleep(3)

        popup_result = handle_renewal_popup(sb, screenshot_prefix=screenshot_prefix, timeout=90)
        srv_result["screenshot"] = popup_result.get("screenshot")

        # 验证到期时间
        time.sleep(3)
        xsrf_token = get_xsrf_token_from_cookies(sb)
        if server_uuid:
            ep = f"/freeservers/{server_uuid}/info" if server_type == "free" else f"/notfreeservers/{server_uuid}/info"
            new_info = api_fetch_json(sb, f"{API_BASE_URL}{ep}", xsrf_token)
            if new_info and new_info.get("success"):
                new_expiry = new_info.get("data", {}).get("expire", srv_result["original_expiry"])
            else:
                goto(sb, server_url, wait=3)
                new_expiry = get_expiry_from_page(sb)
        else:
            goto(sb, server_url, wait=3)
            new_expiry = get_expiry_from_page(sb)

        srv_result["new_expiry"] = new_expiry

        original_dt = parse_expiry_to_datetime(srv_result["original_expiry"])
        new_dt = parse_expiry_to_datetime(new_expiry)

        if popup_result["status"] == "cooldown":
            srv_result.update(status="cooldown", message="冷却期内")
            print(f"  [INFO] 冷却期内")
        elif original_dt and new_dt and new_dt > original_dt:
            diff_h = (new_dt - original_dt).total_seconds() / 3600
            srv_result.update(status="success", message=f"延长了 {diff_h:.1f} 小时")
            print(f"  [INFO] ✅ 续期成功！延长 {diff_h:.1f} 小时")
        elif popup_result["status"] == "success":
            srv_result.update(status="success", message="操作完成")
            print(f"  [INFO] ✅ 续期成功")
        else:
            srv_result.update(status=popup_result["status"], message=popup_result.get("message", "未知"))
            print(f"  [WARN] 结果: {popup_result['status']}")

        if check_and_update_cookie(sb, cookie_env, cookie_value, remark):
            srv_result["cookie_updated"] = True

        if not srv_result["screenshot"] or not os.path.exists(srv_result["screenshot"]):
            final_ss = f"{screenshot_prefix}_final.png"
            sb.save_screenshot(final_ss)
            srv_result["screenshot"] = final_ss

    except Exception as e:
        import traceback
        print(f"  [ERROR] 异常: {repr(e)}")
        traceback.print_exc()
        srv_result.update(status="error", message=str(e)[:100])
        try:
            ss_path = f"{screenshot_prefix}_error.png"
            sb.save_screenshot(ss_path)
            srv_result["screenshot"] = ss_path
        except:
            pass

    return srv_result


# ============================================================
#  单个账号处理
# ============================================================

def process_single_account(sb, account, account_index):
    remark = account.get("remark", f"账号{account_index + 1}")
    cookie_env = account.get("cookie_env", "")
    cookie_str = account.get("cookie_str", "")
    cookie_name = account.get("cookie_name", "")
    cookie_value = account.get("cookie_value", "")

    result = {
        "remark": remark,
        "cookie_env": cookie_env,
        "email": "Unknown",
        "status": "unknown",
        "message": "",
        "servers": [],
        "cookie_updated": False,
    }

    print(f"\n{'=' * 60}")
    print(f"[INFO] 处理账号 [{account_index + 1}]: {mask_remark(remark)} ({cookie_env})")
    print(f"{'=' * 60}")

    # Step 1: 过 Cloudflare（登录阶段）
    print(f"[INFO] [步骤1] 访问站点并处理 Cloudflare 验证...")
    # 2026-09-20：改用普通導航。uc_open_with_reconnect() 會 disconnect 再 reconnect
    # CDP，之後所有 sb.execute_script() 都靜默回 None（run 35496427221 實測：連頁面
    # 診斷 JS 都攞唔到結果，前端只見到「无法获取 Turnstile 坐标」）——即係話之前
    # 根本冇辦法睇清頁面，所謂「過唔到 CF」係喺盲嘅狀態下判嘅。
    try:
        sb.get(f"https://{DOMAIN}/")
    except Exception as e:
        print(f"[WARN]   普通導航失敗({repr(e)[:90]})，退回 uc_open_with_reconnect")
        sb.uc_open_with_reconnect(f"https://{DOMAIN}/", reconnect_time=5)
    cf_page_diag(sb, tag="[導航後]")
    if not solve_cf_interstitial(sb):
        print(f"[ERROR] Cloudflare 验证失败")
        result["status"] = "error"
        result["message"] = "Cloudflare 验证页未通过"
        return result
    print(f"[INFO] ✅ CF 验证通过")

    # Step 2: 注入 Cookie 并登录
    # 注意：唔再用 uc_open_with_reconnect() —— 佢會 disconnect／reconnect CDP，
    # Cloudflare 當成新 session 再出一次挑戰頁，令下面 is_logged_in() 誤判
    # 「Cookie 失效」（09-15~09-18 四個 run 就係死喺呢度）。
    # 改用普通導航保住 Step 1 解到嘅 cf_clearance，撞到先補解。
    print(f"[INFO] [步骤2] 注入 Cookie 并登录...")
    sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
    goto(sb, f"https://{DOMAIN}/server/")

    if not is_logged_in(sb):
        if is_cloudflare_challenge(sb):
            ss_path = f"acc{account_index+1}_cf_blocked.png"
            sb.save_screenshot(ss_path)
            result["status"] = "cf_blocked"
            result["message"] = "卡在 Cloudflare 验证页（唔代表 Cookie 失效）"
            print("[ERROR]   卡在 Cloudflare 验证页，唔可以判定 Cookie 已失效")
            return result
        print("[WARN]   未检测到登录状态，尝试刷新...")
        goto(sb, f"https://{DOMAIN}/server/")

    if not is_logged_in(sb):
        ss_path = f"acc{account_index+1}_login_fail.png"
        sb.save_screenshot(ss_path)
        result["status"] = "cookie_invalid"
        result["message"] = "Cookie 失效或登录失败（已确认唔係 CF 挑战页）"
        return result

    xsrf_token = get_xsrf_token_from_cookies(sb)
    print(f"[INFO]   登录成功，已获取会话 Cookie")

    # Step 3: 获取信息
    print(f"[INFO] [步骤3] 获取账号信息...")
    server_data = api_fetch_json(sb, f"{API_BASE_URL}?page=1", xsrf_token)
    if not server_data or server_data.get("error") == "unauthorized":
        print(f"[ERROR]   获取服务器列表失败")
        result["status"] = "error"
        result["message"] = "无法获取服务器列表"
        return result

    email_data = api_fetch_json(sb,
        f"{API_BASE_URL}/account/activity?sort=-timestamp&page=1&include[]=actor",
        xsrf_token
    )
    email = None
    if email_data:
        for item in email_data.get("data", []):
            actor = item.get("attributes", {}).get("relationships", {}).get("actor", {})
            if actor.get("object") == "user":
                email = actor.get("attributes", {}).get("email")
                if email:
                    break
    result["email"] = email or "Unknown"

    servers = []
    for s in server_data.get("data", []):
        attrs = s.get("attributes", {})
        stype = attrs.get("server_type", "")
        info = {
            "identifier": attrs.get("identifier", ""),
            "uuid": attrs.get("uuid", ""),
            "name": attrs.get("name", ""),
            "server_type": stype,
            "expire": "Unknown",
            "add_hours": "Unknown",
        }
        if attrs.get("uuid") and stype in ("notfree", "free"):
            ep = f"/freeservers/{attrs['uuid']}/info" if stype == "free" else f"/notfreeservers/{attrs['uuid']}/info"
            si = api_fetch_json(sb, f"{API_BASE_URL}{ep}", xsrf_token)
            if si and si.get("success"):
                d = si.get("data", {})
                info["expire"] = d.get("expire", "Unknown")
                info["add_hours"] = d.get("addHours", "Unknown")
        servers.append(info)

    result["servers"] = servers

    if email and email != "Unknown":
        print(f"[INFO] 邮箱: {mask_email(email)}")

    if not servers:
        print(f"[WARN] 该账号下没有服务器")
        result["status"] = "no_server"
        result["message"] = "该账号下没有服务器"
        return result

    print(f"[INFO] 找到 {len(servers)} 个服务器:")
    for s in servers:
        print(f"  - {mask_server_id(s['identifier'])} [{s['server_type']}] {s['name']} | 到期: {s['expire']}")

    # Step 4: 逐个处理服务器
    print(f"[INFO] [步骤4] 逐个处理服务器续期...")
    server_results = []
    for srv_idx, server in enumerate(servers):
        ss_prefix = f"acc{account_index + 1}_srv{srv_idx + 1}"
        srv_result = process_single_server(
            sb, server, cookie_name, cookie_value, cookie_str, cookie_env, remark, ss_prefix
        )
        server_results.append(srv_result)
        if srv_result.get("cookie_updated"):
            result["cookie_updated"] = True
        if srv_idx < len(servers) - 1:
            wait = random.randint(2, 4) if srv_result.get("status") == "skipped" else random.randint(5, 10)
            print(f"\n  [INFO] 等待 {wait} 秒后处理下一个服务器...")
            time.sleep(wait)

    result["servers"] = server_results

    statuses = [s["status"] for s in server_results]
    if "success" in statuses:
        result["status"] = "success"
        result["message"] = f"{statuses.count('success')}/{len(statuses)} 个服务器续期成功"
    elif all(s == "skipped" for s in statuses):
        result["status"] = "skipped"
        result["message"] = "所有服务器均跳过"
    elif "cooldown" in statuses:
        result["status"] = "cooldown"
        result["message"] = "冷却期内"
    elif "error" in statuses or "timeout" in statuses:
        result["status"] = "error"
        err_count = statuses.count("error") + statuses.count("timeout")
        result["message"] = f"{err_count}/{len(statuses)} 个服务器失败"
    else:
        result["status"] = statuses[0] if statuses else "unknown"

    return result


# ============================================================
#  单账号 TG 通知
# ============================================================

def send_account_notification(result):
    email = result.get("email", "Unknown")
    remark = result.get("remark", "")
    cookie_updated = result.get("cookie_updated", False)
    servers = result.get("servers", [])
    status = result.get("status", "unknown")

    account_display = email if email and email != "Unknown" else remark
    lines = [f"账号：{account_display}"]

    if status == "cookie_invalid":
        lines.append("状态：⚠️ Cookie 已失效，请及时更新 WEIRDHOST_COOKIE_*")
        screenshot = None
    elif status == "cf_blocked":
        lines.append("状态：🛡️ 卡在 Cloudflare 验证页（唔等於 Cookie 失效，无需换 cookie）")
        screenshot = None
    elif status == "no_server":
        lines.append("状态：⚠️ 没有服务器")
        screenshot = None
    else:
        for s in servers:
            lines.append("")
            lines.append(f"服务器：{s.get('server_id', '')}")
            srv_status = s["status"]

            if srv_status == "success":
                lines.append("状态：🟢 续期成功")
                new_exp = s.get("new_expiry", "Unknown")
                lines.append(f"剩余：{calculate_remaining_time(new_exp)}")
                msg = s.get("message", "")
                if msg and "延长" in msg:
                    lines.append(f"延长：{msg}")
                else:
                    orig = s.get("original_expiry", "Unknown")
                    new = s.get("new_expiry", "Unknown")
                    if orig != "Unknown" and new != "Unknown":
                        odt = parse_expiry_to_datetime(orig)
                        ndt = parse_expiry_to_datetime(new)
                        if odt and ndt and ndt > odt:
                            diff_h = (ndt - odt).total_seconds() / 3600
                            lines.append(f"延长：延长{diff_h:.1f}h")
            elif srv_status == "cooldown":
                lines.append("状态：⏳ 冷却期")
                expiry = s.get("original_expiry", "Unknown")
                lines.append(f"剩余：{calculate_remaining_time(expiry)}")
                lines.append("提示：冷却中，请稍后再试")
            elif srv_status == "skipped":
                lines.append("状态：⏭️ 跳过")
                expiry = s.get("original_expiry", s.get("new_expiry", "Unknown"))
                lines.append(f"剩余：{calculate_remaining_time(expiry)}")
                lines.append(f"原因：{s.get('message', '未知')}")
            else:
                lines.append(f"状态：❌ {srv_status}")
                lines.append(f"信息：{s.get('message', '未知')}")

    if cookie_updated:
        lines.append("")
        lines.append("🔑 Cookie 已自动更新")

    lines.append("")
    lines.append("Weirdhost Auto Renew")

    message = "\n".join(lines)

    screenshot = None
    for s in servers:
        if s["status"] in ("success", "cooldown", "error", "timeout"):
            if s.get("screenshot") and os.path.exists(s["screenshot"]):
                screenshot = s["screenshot"]
                break

    if screenshot:
        sync_tg_notify_photo(screenshot, message)
    else:
        sync_tg_notify(message)


# ============================================================
#  主函数
# ============================================================

def add_server_time():
    accounts = detect_accounts()

    if not accounts:
        print("\n" + "=" * 60)
        print("[ERROR] 未检测到任何有效的账号配置")
        print("=" * 60)
        print("\n请在 GitHub Secrets 中设置 WEIRDHOST_COOKIE_1 ~ WEIRDHOST_COOKIE_5")
        print("\n格式: 备注-----remember_web_xxx=yyy")
        print("示例: 我的账号-----remember_web_59ba36addc2b2f940CCCC=XXXXXXXXXXX")
        print("\n也支持纯 Cookie 格式 (无备注):")
        print("  remember_web_59ba36addc2b2f940CCCC=XXXXXXXXXXX")
        print("=" * 60)

        sync_tg_notify(
            "🔔 <b>Weirdhost 续期</b>\n\n"
            "❌ 未检测到任何有效的 WEIRDHOST_COOKIE_N\n\n"
            "请在 GitHub Secrets 中设置:\n"
            "<code>WEIRDHOST_COOKIE_1</code>\n"
            "格式: <code>备注-----remember_web_xxx=yyy</code>"
        )
        sys.exit(1)

    print("=" * 60)
    print(f"[INFO] Weirdhost 自动续期")
    print(f"[INFO] 共 {len(accounts)} 个账号")
    print("=" * 60)

    results = []

    try:
        with SB(
            uc=True,
            test=True,
            locale="ko",
            headless=False,
            # 2026-09-20：移除 --disable-gpu/--disable-software-rasterizer。
            # run 35497214937 用 CDP pierce 已經點得到 CF widget（box 300x65，
            # 點 checkbox 位），但 widget id 一直換（cf-chl-widget-hfb6b →
            # vn9bo → ge3ya…）＝挑戰反覆重發，懷疑係 GPU/WebGL 被禁導致指紋
            # 異常過唔到託管挑戰。呢兩個 flag 對 CF 挑戰冇好處，先試除去。
            chromium_arg="--disable-dev-shm-usage,--no-sandbox,--disable-background-timer-throttling"
        ) as sb:
            print("\n[INFO] 浏览器已启动")

            for i, account in enumerate(accounts):
                result = process_single_account(sb, account, i)
                results.append(result)

                send_account_notification(result)

                if i < len(accounts) - 1:
                    if result.get("status") == "skipped":
                        wait_time = random.randint(2, 4)
                    else:
                        wait_time = random.randint(5, 10)
                    print(f"\n[INFO] 等待 {wait_time} 秒后处理下一个账号...")
                    time.sleep(wait_time)

    except Exception as e:
        import traceback
        print(f"\n[ERROR] 浏览器异常: {repr(e)}")
        traceback.print_exc()

        if not results:
            sync_tg_notify(f"🔔 <b>Weirdhost</b>\n\n❌ 浏览器启动失败\n\n<code>{repr(e)}</code>")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print("[INFO] 全部处理完成")
    print(f"{'=' * 60}")
    icons = {
        "success": "🟢", "cooldown": "🟡", "skipped": "🔵",
        "cookie_invalid": "🔒", "cf_blocked": "🛡️", "no_server": "📭",
        "error": "❌", "timeout": "⚠️",
    }
    for r in results:
        icon = icons.get(r["status"], "❓")
        srv_count = len(r.get("servers", []))
        email_display = mask_email(r.get("email", ""))
        remark_display = mask_remark(r.get("remark", "?"))
        print(f"  {icon} {remark_display} ({email_display}) | "
              f"{srv_count} 个服务器 | {r['status']} | {r.get('message', '')}")

    # 有實質失敗就 exit 非 0 —— 之前永世 exit 0，所以 09-15~09-18 四個 run
    # 全部「success」但一次都冇續到期，冇人為意。
    fatal = [r for r in results if r["status"] in ("error", "cookie_invalid", "cf_blocked")]
    if fatal:
        print(f"\n[ERROR] 有 {len(fatal)} 個帳號未完成，exit 1")
        sys.exit(1)
    if not results:
        print("\n[ERROR] 冇任何帳號被處理，exit 1")
        sys.exit(1)


if __name__ == "__main__":
    add_server_time()
