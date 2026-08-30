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
# 面板 URL 格式从 HTML dump 中确认: /panel/server/{id}
PANEL_URL     = f"{BASE_URL}/panel/server/{SERVER_ID}"
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

            # 直接访问 /sign-in（从 HTML dump 中发现真实登录 URL）
            log("🔑 直接访问 /sign-in 页面...")
            sb.open(f"{BASE_URL}/sign-in")
            time.sleep(3)

            title2 = sb.get_title()
            log(f"📄 登录页标题: {title2}")

            # 尝试多种输入框选择器
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                '#email',
                'input[name="username"]',
                'input[name="login"]',
                'input[type="text"]',
            ]
            pwd_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                '#password',
                'input[name="pass"]',
            ]

            email_filled = False
            for sel in email_selectors:
                try:
                    sb.type(sel, USERNAME, timeout=3)
                    log(f"✅ 用户名已填入: {sel}")
                    email_filled = True
                    break
                except Exception:
                    continue

            if not email_filled:
                log("❌ 未找到用户名输入框")
                return False

            pwd_filled = False
            for sel in pwd_selectors:
                try:
                    sb.type(sel, PASSWORD, timeout=3)
                    log(f"✅ 密码已填入: {sel}")
                    pwd_filled = True
                    break
                except Exception:
                    continue

            if not pwd_filled:
                log("❌ 未找到密码输入框")
                return False

            # 提交登录
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                '.btn-submit',
                '#login-submit',
                'button:contains("Sign In")',
                'button:contains("Login")',
                'button:contains("登录")',
                '.btn-primary',
            ]
            for sel in submit_selectors:
                try:
                    sb.click(sel, timeout=3)
                    log(f"✅ 点击了提交: {sel}")
                    time.sleep(4)
                    break
                except Exception:
                    continue

            # 检查是否登录成功
            title3 = sb.get_title()
            log(f"📄 当前页面: {title3}")

            if "login" in title3.lower() or "sign-in" in title3.lower():
                log("❌ 登录失败，仍在登录页")
                # Dump HTML for debugging
                try:
                    html = sb.get_html()
                    with open("/tmp/h2p_login_failed.html", "w") as f:
                        f.write(html)
                    log("📄 登录失败页面 HTML 已保存")
                    # Look for error messages
                    for keyword in ["error", "invalid", "wrong", "incorrect", "failed", "captcha", "verified"]:
                        if keyword.lower() in html.lower():
                            for line in html.split("\n"):
                                if keyword.lower() in line.lower() and ("alert" in line.lower() or "error" in line.lower() or "class" in line.lower()):
                                    log(f"   ⚠️ {line.strip()[:200]}")
                except Exception:
                    pass
                return False

            log("✅ 登录成功")

            # 跳转到服务器面板
            log(f"🌐 跳转到面板: {PANEL_URL}")
            sb.open(PANEL_URL)
            time.sleep(3)

            title4 = sb.get_title()
            log(f"📄 面板标题: {title4}")

            # 查找续期按钮 - 多种选择器
            log("🔍 查找续期按钮...")
            renew_selectors = [
                "//button[contains(text(), 'Renew')]",
                "//button[contains(text(), 'renew')]",
                "//button[contains(text(), '续期')]",
                "//button[contains(text(), 'RENEW')]",
                "//a[contains(text(), 'Renew')]",
                "//a[contains(text(), 'renew')]",
                "button:contains('Renew')",
                "button:contains('renew')",
                "button:contains('Extend')",
                "button:contains('extend')",
                "[class*='renew']",
                "[class*='Renew']",
                "[class*='extend']",
                "[class*='Extend']",
            ]

            clicked = False
            for sel in renew_selectors:
                try:
                    sb.click(sel, timeout=3)
                    log(f"✅ 点击了续期按钮: {sel}")
                    clicked = True
                    time.sleep(3)
                    break
                except Exception:
                    continue

            if not clicked:
                log("⚠️ 未找到续期按钮，dump HTML 分析...")
                try:
                    html = sb.get_html()
                    with open("/tmp/h2p_panel.html", "w") as f:
                        f.write(html)
                    for keyword in ["renew", "extend", "续期", "start", "stop", "restart"]:
                        if keyword.lower() in html.lower():
                            for line in html.split("\n"):
                                if keyword.lower() in line.lower() and ("button" in line.lower() or "href" in line.lower()):
                                    log(f"   🔍 {line.strip()[:200]}")
                except Exception:
                    pass
                return False

            # 查找 Start 按钮
            log("🔍 查找 Start 按钮...")
            start_selectors = [
                "//button[contains(text(), 'Start')]",
                "//button[contains(text(), 'start')]",
                "//button[contains(text(), 'START')]",
                "//a[contains(text(), 'Start')]",
                "button:contains('Start')",
                "button:contains('start')",
                "[class*='start']",
                "[class*='Start']",
            ]

            for sel in start_selectors:
                try:
                    sb.click(sel, timeout=3)
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