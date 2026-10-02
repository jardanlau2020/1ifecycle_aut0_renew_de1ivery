#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ACLClouds 自动续期 —— 已迁移到 renew-kit。

公共部分（配置读取 / 结果分类 / Telegram / 报告排版 / 退出码）交给 renewkit，
本文件只留 ACLClouds 的业务逻辑：

    CDP 注入会话 Cookie → 打开 /server/<id> → 读「Temps restant」
    → 窗口开启（≤ ACLCLOUDS_WINDOW_DAYS 天）时点 Renouveler
    → 校验剩余时间确实变长

迁移带来的行为变化：
    · 退出码统一。原来 1（没配 Cookie）/ 2（点了但没验证过）/ 3（人机验证）各说各话，
      workflow 分不清「配置错」和「上游抽风」。现在只有 FAILED 才 exit 1。
    · Cloudflare 挑战页 → TRANSIENT，exit 0，不标红（等下次排程）。
    · 未到续期窗口 → SKIPPED，exit 0。
    · 读不到 Temps restant → UNKNOWN（而不是当成「没到窗口」的假绿），并且不点按钮。
    · 新增 DRY_RUN=1：只读剩余时间、不点按钮，用来验证 Cookie 还有没有效。
    · 新增 Telegram 通知（原先完全没有）。
    · 失败截图落在工作目录（供 workflow 上传 artifact），不再丢 /tmp。

用法（环境变量）：
    ACLCLOUDS_SESSION_COOKIE   __Host-aclclouds_session 的值（必填）
    ACLCLOUDS_SERVER_ID        默认 f743cf50
    ACLCLOUDS_BASE_URL         默认 https://aclclouds.com
    ACLCLOUDS_CHROMIUM         浏览器可执行文件路径；留空则交给 Selenium Manager 自动解析
    ACLCLOUDS_WINDOW_DAYS      续期窗口天数，默认 2（剩余 ≤N 天才点）
    ACLCLOUDS_SHOT_DIR         截图目录，默认当前目录
    TG_BOT_TOKEN / TG_CHAT_ID  Telegram 通知（可选）
    DRY_RUN=1                  只检查不点击
"""
from __future__ import annotations

import os
import re
import sys
import time

from renewkit import Outcome, RenewReport, shorten
from renewkit import env

SERVICE = "ACLClouds"

COOKIE = env.get("ACLCLOUDS_SESSION_COOKIE")
SERVER_ID = env.get("ACLCLOUDS_SERVER_ID", "f743cf50")
BASE_URL = env.get("ACLCLOUDS_BASE_URL", "https://aclclouds.com").rstrip("/")
CHROMIUM = env.get("ACLCLOUDS_CHROMIUM")
WINDOW_DAYS = env.get_int("ACLCLOUDS_WINDOW_DAYS", 2)
SHOT_DIR = env.get("ACLCLOUDS_SHOT_DIR", ".") or "."
DRY_RUN = env.dry_run()

SERVER_URL = f"{BASE_URL}/server/{SERVER_ID}"
COOKIE_NAME = "__Host-aclclouds_session"

# 页面上的「Temps restant: 3j 4h」或「Temps restant: 3j」
_REMAIN_RE = re.compile(r"Temps restant\s*[:\s]*(\d+)\s*j\s*(?:(\d+)\s*h)?", re.IGNORECASE)

# ACLClouds 的人机验证：不能自动勾选，检测到就明确失败并留截图
_ANTIBOT_MARKERS = ("anti-bot confirmation", "i am not a robot",
                    "click on vps", "secured by aclclouds")
# Cloudflare 交互式挑战：属上游/出口 IP 问题，等下次
_CF_MARKERS = ("just a moment", "cf-mitigated", "performing security verification")

# 兼容 Vue 异步渲染、大小写、以及 role=button 的伪按钮
_RENEW_XPATH = (
    "//*[self::button or self::a or @role='button']"
    "[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renew')"
    " or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renouvel')]"
)
_CONFIRM_XPATHS = (
    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]",
    "//button[contains(., 'Confirmer') or contains(., '确认') or contains(., 'Yes')]",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def shot(driver, name: str) -> str:
    """保存截图，返回路径（失败不抛）。"""
    path = os.path.join(SHOT_DIR, name)
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        driver.save_screenshot(path)
        log(f"  📸 截图: {path}")
    except Exception as exc:                                  # 截图失败不该影响结论
        log(f"  ⚠️ 截图失败({type(exc).__name__}): {name}")
    return path


# ---------------------------------------------------------------- 纯逻辑（可单测）

def parse_remaining_hours(text: str) -> int | None:
    """从页面文本解析剩余小时数；解析不到返回 None（保守：不点按钮）。"""
    m = _REMAIN_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 24 + (int(m.group(2)) if m.group(2) else 0)


def format_remaining(hours: int | None) -> str:
    if hours is None:
        return "未知"
    return f"{hours // 24}j {hours % 24}h"


def classify_page_detail(url: str, text: str) -> tuple[Outcome | None, str]:
    """识别「页面本身就不对」的情况，返回 (结果, 建议截图名)；正常页面返回 (None, "")。

    顺序有讲究：先判 Cloudflare（可能整页都是挑战文案），再判人机验证，
    最后判登录跳转——否则一个 CF 挑战页会因为正文里恰好有「Connexion」被误判成掉登录。
    """
    low = (text or "").lower()
    if any(m in low for m in _CF_MARKERS):
        return Outcome.TRANSIENT, "aclclouds-cf.png"
    if any(m in low for m in _ANTIBOT_MARKERS):
        return Outcome.FAILED, "aclclouds-antibot.png"
    if "/auth/login" in (url or ""):
        return Outcome.FAILED, "aclclouds-login.png"
    return None, ""


def classify_page(url: str, text: str) -> Outcome | None:
    """只要结论的版本（截图名交给 run() 用）。"""
    return classify_page_detail(url, text)[0]


def window_open(hours: int | None, window_days: int = WINDOW_DAYS) -> bool:
    """剩余小时数是否已进入续期窗口。读不到一律视为「不开」，避免瞎点。"""
    return hours is not None and hours <= window_days * 24


# ---------------------------------------------------------------- 浏览器

def build_driver():
    """构造 headless Chromium。CHROMIUM 留空时交给 Selenium Manager 解析。

    原实现硬编码 /usr/bin/chromium，在 ubuntu-latest 上并不存在
    （22.04+ 的 chromium-browser 是 snap 包装，容器里跑不起来）。
    """
    from selenium import webdriver
    from selenium.webdriver.chromium.options import ChromiumOptions

    opts = ChromiumOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    if CHROMIUM:
        opts.binary_location = CHROMIUM
    return webdriver.Chrome(options=opts)


def open_server_page(driver):
    """注入 Cookie → 打开服务器页 → 等正文渲染。返回 (page_text, error)。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(BASE_URL)
    time.sleep(3)
    driver.execute_cdp_cmd("Network.setCookie", {
        "name": COOKIE_NAME,
        "value": COOKIE,
        "domain": "aclclouds.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    })

    driver.get(SERVER_URL)
    # Vue SPA 要等异步内容真渲染，不能只靠固定 sleep
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.find_element(By.TAG_NAME, "body").text.strip())
    except Exception as exc:
        return "", f"页面无内容（{type(exc).__name__}）"
    time.sleep(3)
    return driver.find_element(By.TAG_NAME, "body").text, ""


def click_renew(driver) -> str:
    """点 Renouveler（含确认弹窗）。返回说明；没找到按钮返回空串。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    wait = WebDriverWait(driver, 20)
    try:
        btn = wait.until(EC.element_to_be_clickable((By.XPATH, _RENEW_XPATH)))
    except Exception:
        return ""

    log(f"  🖱️ 点击 {btn.tag_name} / {shorten(btn.text, 40)!r}")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    try:
        btn.click()
    except Exception:
        driver.execute_script("arguments[0].click();", btn)
    time.sleep(1)

    try:                                            # 原生 confirm 弹窗
        WebDriverWait(driver, 3).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        log(f"  📩 确认弹窗: {shorten(alert.text, 120)!r}")
        alert.accept()
    except Exception:
        pass

    for sel in _CONFIRM_XPATHS:                     # 页面内确认按钮
        try:
            WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, sel))).click()
            log("  ✅ 已确认续期弹窗")
            break
        except Exception:
            continue
    return "已点击 Renouveler"


def wait_until_increased(driver, before: int | None, timeout: float = 12.0):
    """等剩余小时数超过 before。返回 (after, text)；超时则返回当前读数。"""
    from selenium.webdriver.common.by import By

    deadline = time.time() + timeout
    while time.time() < deadline:
        text = driver.find_element(By.TAG_NAME, "body").text
        after = parse_remaining_hours(text)
        if after is not None and (before is None or after > before):
            return after, text
        time.sleep(1)
    text = driver.find_element(By.TAG_NAME, "body").text
    return parse_remaining_hours(text), text


# ---------------------------------------------------------------- 主流程

def run(report: RenewReport) -> None:
    driver = None
    try:
        driver = build_driver()
        text, err = open_server_page(driver)
        if err:
            shot(driver, "aclclouds-page-empty.png")
            report.add(SERVICE, Outcome.TRANSIENT, detail=err)
            return

        bad, bad_shot = classify_page_detail(driver.current_url, text)
        if bad is Outcome.TRANSIENT:
            shot(driver, bad_shot)
            report.add(SERVICE, Outcome.TRANSIENT,
                       detail="Cloudflare 交互式验证页，本次跳过")
            return
        if bad is Outcome.FAILED:
            shot(driver, bad_shot)
            report.add(SERVICE, Outcome.FAILED,
                       detail=f"会话失效或需人工人机验证（{shorten(driver.current_url, 60)}）")
            return

        log(f"  ✅ 已进入服务器页: {driver.current_url}")
        before = parse_remaining_hours(text)
        log(f"  ⏱️ Temps restant: {format_remaining(before)}（{before}h）")

        if before is None:
            shot(driver, "aclclouds-parse-failed.png")
            report.add(SERVICE, Outcome.UNKNOWN,
                       detail=f"读不到 Temps restant，未点按钮｜页面: {shorten(text, 120)}")
            return

        if DRY_RUN:
            report.add(SERVICE, Outcome.SKIPPED, expire=before / 24,
                       detail=f"dry-run：未点击（剩 {format_remaining(before)}）")
            return

        if not window_open(before):
            report.add(SERVICE, Outcome.SKIPPED, expire=before / 24,
                       detail=f"未到续期窗口（剩 {format_remaining(before)}，需 ≤{WINDOW_DAYS} 天）")
            return

        if not click_renew(driver):
            shot(driver, "aclclouds-button-missing.png")
            report.add(SERVICE, Outcome.FAILED, expire=before / 24,
                       detail=f"窗口已开（剩 {format_remaining(before)}）但找不到可点的 Renouveler")
            return

        after, new_text = wait_until_increased(driver, before)
        if after is not None and after > before:
            log(f"  ✅ 续期确认: {format_remaining(before)} → {format_remaining(after)}")
            report.add(SERVICE, Outcome.RENEWED, expire=after / 24)
            return

        shot(driver, "aclclouds-renew-failed.png")
        report.add(SERVICE, Outcome.FAILED, expire=before / 24,
                   detail=(f"已点 Renouveler 但剩余未增加（{format_remaining(before)}"
                           f" → {format_remaining(after)}）｜页面: {shorten(new_text, 100)}"))
    except Exception as exc:                        # 浏览器/驱动异常
        report.add(SERVICE, Outcome.FAILED,
                   detail=f"{type(exc).__name__}: {shorten(str(exc), 140)}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def main() -> int:
    if not COOKIE:
        report = RenewReport(SERVICE)
        report.add("凭证", Outcome.FAILED, detail="缺少 ACLCLOUDS_SESSION_COOKIE")
        return report.finish()

    log(f"🚀 {SERVICE} 续期 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"服务器: {SERVER_ID} | 面板: {BASE_URL} | 窗口: ≤{WINDOW_DAYS} 天 | dry_run={DRY_RUN}")

    report = RenewReport(SERVICE)
    run(report)
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
