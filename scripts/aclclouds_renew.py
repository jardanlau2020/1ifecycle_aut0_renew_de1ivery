#!/usr/bin/env python3
"""ACLClouds auto-renew via Selenium + Chromium (GitHub Actions compatible).

Reads "Temps restant" from the server console page and clicks "Renouveler"
when the renewal window is open (≤2 days before expiration).

Requirements:
  - ACLCLOUDS_SESSION_COOKIE env var (__Host-aclclouds_session)
  - chromium-browser installed
  - xvfb-run for headless display
"""
import os, sys, time, re
from selenium import webdriver
from selenium.webdriver.chromium.options import ChromiumOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException

COOKIE = os.environ.get("ACLCLOUDS_SESSION_COOKIE", "")
SERVER_ID = os.environ.get("ACLCLOUDS_SERVER_ID", "f743cf50")
BASE_URL = "https://aclclouds.com"
SERVER_URL = f"{BASE_URL}/server/{SERVER_ID}"

if not COOKIE:
    print("ERROR: ACLCLOUDS_SESSION_COOKIE not set")
    sys.exit(1)

print(f"[aclclouds] Starting browser automation for server {SERVER_ID}")

opts = ChromiumOptions()
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument("--disable-gpu")
opts.add_argument("--window-size=1920,1080")
opts.binary_location = "/usr/bin/chromium"

driver = webdriver.Chrome(options=opts)

try:
    # Step 1: Set cookie via CDP
    print("[aclclouds] Setting session cookie via CDP...")
    driver.get(BASE_URL)
    time.sleep(3)
    driver.execute_cdp_cmd("Network.setCookie", {
        "name": "__Host-aclclouds_session",
        "value": COOKIE,
        "domain": "aclclouds.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    })

    # Step 2: Navigate to server page
    print("[aclclouds] Navigating to server page...")
    driver.get(SERVER_URL)
    wait = WebDriverWait(driver, 20)
    # Vue SPA 需要等异步内容真正渲染，不能只靠固定 sleep
    try:
        wait.until(lambda d: d.find_element(By.TAG_NAME, "body").text.strip())
    except TimeoutException:
        print("[aclclouds] ❌ 页面无内容")
        driver.save_screenshot("/tmp/aclclouds-debug.png")
        sys.exit(1)
    time.sleep(2)

    current_url = driver.current_url
    page_text = driver.find_element("tag name", "body").text

    if "/auth/login" in current_url or "Connexion" in page_text:
        print(f"[aclclouds] ❌ Redirected to login: {current_url}")
        driver.save_screenshot("/tmp/aclclouds-login.png")
        sys.exit(1)

    if "Just a moment" in page_text or "cf-mitigated" in page_text:
        print("[aclclouds] ❌ Cloudflare challenge")
        sys.exit(1)

    print(f"[aclclouds] ✅ On server page: {current_url}")

    # Step 3: Parse Temps restant
    print("[aclclouds] Parsing Temps restant...")
    time.sleep(3)
    page_text = driver.find_element("tag name", "body").text

    # Parse "Temps restant: 3j 4h" or "Temps restant: 3j"
    temps_match = re.search(r'Temps restant\s*[:\s]*(\d+)\s*j\s*(?:(\d+)\s*h)?', page_text, re.IGNORECASE)
    if temps_match:
        days = int(temps_match.group(1))
        hours = int(temps_match.group(2)) if temps_match.group(2) else 0
        remaining_str = f"{days}j {hours}h" if hours else f"{days}j"
        print(f"[aclclouds] ⏱️ Temps restant: {remaining_str} ({days} days, {hours} hours)")
    else:
        print("[aclclouds] ⚠️ Could not parse Temps restant")
        print(f"[aclclouds] Page preview: {page_text[:600]}")
        days = None

    # Step 4: 找按钮（兼容 Vue 异步渲染、大小写及 role=button）
    renew_xpath = "//*[self::button or self::a or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renew') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'renouvel')]"
    try:
        renew_btn = wait.until(EC.element_to_be_clickable((By.XPATH, renew_xpath)))
        print(f"[aclclouds] ✅ Found clickable Renew element: {renew_btn.tag_name} / {renew_btn.text!r}")
    except TimeoutException:
        renew_btn = None

    if renew_btn:
        before = (days or 0) * 24 + (int(temps_match.group(2)) if temps_match and temps_match.group(2) else 0)
        print("[aclclouds] Clicking Renew...")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", renew_btn)
        try:
            renew_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", renew_btn)
        time.sleep(1)
        try:
            WebDriverWait(driver, 3).until(EC.alert_is_present())
            alert = driver.switch_to.alert
            print(f"[aclclouds] Confirm dialog: {alert.text[:200]!r}")
            alert.accept()
        except TimeoutException:
            pass
        for sel in ["//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]", "//button[contains(., 'Confirmer') or contains(., '确认') or contains(., 'Yes')]"]:
            try:
                WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, sel))).click()
                print("[aclclouds] ✅ Confirmed renewal dialog")
                break
            except TimeoutException:
                continue

        def remaining_hours(d):
            text = d.find_element(By.TAG_NAME, "body").text
            m = re.search(r'Temps restant\s*[:\s]*(\d+)\s*j\s*(?:(\d+)\s*h)?', text, re.IGNORECASE)
            return (((int(m.group(1)) * 24) + (int(m.group(2)) if m.group(2) else 0), text) if m else (None, text))
        try:
            after, new_text = WebDriverWait(driver, 12).until(lambda d: (lambda v: v if v[0] is not None and v[0] > before else False)(remaining_hours(d)))
            print(f"[aclclouds] ✅ Renewal verified: {after // 24}j {after % 24}h (before {before}h)")
        except TimeoutException:
            after, new_text = remaining_hours(driver)
            print(f"[aclclouds] ❌ Renewal NOT verified; remaining is {after}h (before {before}h)")
            driver.save_screenshot("/tmp/aclclouds-renew-failed.png")
            print(f"[aclclouds] Page preview: {new_text[:800]}")
            sys.exit(2)
    else:
        if days is not None and days <= 2:
            print("[aclclouds] ❌ Window open (≤2 days) but no clickable Renew button found")
            driver.save_screenshot("/tmp/aclclouds-renew-button-missing.png")
            sys.exit(2)
        print(f"[aclclouds] ℹ️ Renew window not open yet (need ≤2 days, {days}j remaining)")

    driver.save_screenshot("/tmp/aclclouds-debug.png")

finally:
    driver.quit()
    print("[aclclouds] Browser closed")