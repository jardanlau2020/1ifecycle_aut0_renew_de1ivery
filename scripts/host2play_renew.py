#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Host2Play 自动续期脚本
- 目标: host2play.gratis 面板，手动 Renew → Start
- 服务器: mcf7022 (阿勒泰 SE-LARGE-S07)
- 约 8 小时生命周期，到期前自动续期
- 凭据从环境变量读取，不硬编码
"""

import os
import sys
import time
import json
import re
import random
import asyncio
import aiohttp
from datetime import datetime, timedelta
from urllib.parse import urljoin

sys.stdout.reconfigure(line_buffering=True)

# ── 配置 ──────────────────────────────────────────────
BASE_URL      = "https://host2play.gratis"
SERVER_ID     = os.environ.get("H2P_SERVER_ID", "mcf7022")
PANEL_URL     = f"{BASE_URL}/server/{SERVER_ID}"
USERNAME      = os.environ.get("H2P_USERNAME", "")
PASSWORD      = os.environ.get("H2P_PASSWORD", "")
TG_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
RENEW_BEFORE_H = int(os.environ.get("RENEW_BEFORE_H", "2"))  # 到期前 2 小时续期

# ── 日志 ──────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Telegram 通知 ─────────────────────────────────────
async def tg_send(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        log(f"TG 通知失败: {e}")

def sync_tg_send(text):
    try:
        asyncio.run(tg_send(text))
    except Exception:
        pass

# ── 浏览器自动化续期 ──────────────────────────────────
def do_renew():
    """使用 SeleniumBase 访问面板并续期"""
    try:
        from seleniumbase import SB
    except ImportError:
        log("❌ seleniumbase 未安装")
        return False

    log("🌐 启动浏览器...")
    try:
        with SB(uc=True, headless=True, test=True, locale="en") as sb:
            sb.open(BASE_URL)
            log("⏳ 等待 Cloudflare 验证...")

            # 等待页面加载，CF Turnstile 可能需要时间
            time.sleep(5)

            # 检查是否过了 CF
            title = sb.get_title()
            log(f"📄 页面标题: {title}")

            if "security verification" in title.lower() or "just a moment" in title.lower():
                log("❌ Cloudflare 验证未通过，无法继续")
                return False

            # Dump page HTML for debugging
            try:
                html = sb.get_html()
                with open("/tmp/h2p_page.html", "w") as f:
                    f.write(html)
                log("📄 页面 HTML 已保存到 /tmp/h2p_page.html")
            except Exception:
                pass

            # 尝试登录 - 先 dump HTML 找按钮
            log("🔑 尝试登录...")

            # 先 dump 页面 HTML 用于分析
            try:
                html = sb.get_html()
                with open("/tmp/h2p_page.html", "w") as f:
                    f.write(html)
                log("📄 页面 HTML 已保存")
                # 搜索登录相关元素
                for keyword in ["login", "signin", "sign-in", "Login", "Sign In", "登录", "account", "panel"]:
                    if keyword.lower() in html.lower():
                        # 找到包含 keyword 的行
                        for line in html.split("\n"):
                            if keyword.lower() in line.lower() and ("href" in line.lower() or "button" in line.lower() or "class" in line.lower()):
                                log(f"   🔍 找到: {line.strip()[:200]}")
            except Exception as e:
                log(f"   HTML dump 失败: {e}")

            # 直接尝试访问 /login
            log("🔑 直接访问 /login 页面...")
            sb.open(f"{BASE_URL}/login")
            time.sleep(3)

            title2 = sb.get_title()
            log(f"📄 登录页标题: {title2}")

            sb.type('input[type="email"], input[name="email"], #email', USERNAME)
            sb.type('input[type="password"], input[name="password"], #password', PASSWORD)
            sb.click('button[type="submit"], .btn-submit, #login-submit')
            time.sleep(4)

            # 检查是否登录成功
            if "login" in sb.get_title().lower():
                log("❌ 登录失败")
                return False

            log("✅ 登录成功")

            # 跳转到服务器面板
            sb.open(PANEL_URL)
            time.sleep(3)

            # 查找续期按钮
            log("🔍 查找续期按钮...")
            renew_selectors = [
                "//button[contains(text(), 'Renew')]",
                "//button[contains(text(), 'renew')]",
                "//button[contains(text(), '续期')]",
                "//button[contains(text(), 'RENEW')]",
                "//a[contains(text(), 'Renew')]",
                "button:contains('Renew')",
            ]

            clicked = False
            for sel in renew_selectors:
                try:
                    sb.click(sel)
                    log(f"✅ 点击了续期按钮: {sel}")
                    clicked = True
                    time.sleep(3)
                    break
                except Exception:
                    continue

            if not clicked:
                log("❌ 未找到续期按钮")
                return False

            # 查找 Start 按钮
            log("🔍 查找 Start 按钮...")
            start_selectors = [
                "//button[contains(text(), 'Start')]",
                "//button[contains(text(), 'start')]",
                "//button[contains(text(), 'START')]",
                "//a[contains(text(), 'Start')]",
                "button:contains('Start')",
            ]

            for sel in start_selectors:
                try:
                    sb.click(sel)
                    log(f"✅ 点击了 Start 按钮: {sel}")
                    time.sleep(3)
                    break
                except Exception:
                    continue

            log("✅ 续期+启动完成")
            return True

    except Exception as e:
        log(f"❌ 续期异常: {e}")
        return False

# ── 主流程 ────────────────────────────────────────────
def main():
    log("=" * 56)
    log("🔄 Host2Play 自动续期")
    log(f"   服务器: {SERVER_ID}")
    log("=" * 56)

    if not USERNAME or not PASSWORD:
        log("❌ 缺少 H2P_USERNAME / H2P_PASSWORD 环境变量")
        sys.exit(1)

    success = do_renew()
    if success:
        msg = f"✅ Host2Play 续期成功\n服务器: {SERVER_ID}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        sync_tg_send(msg)
    else:
        msg = f"❌ Host2Play 续期失败\n服务器: {SERVER_ID}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n可能原因: Cloudflare 挡截"
        sync_tg_send(msg)
        sys.exit(1)

if __name__ == "__main__":
    main()