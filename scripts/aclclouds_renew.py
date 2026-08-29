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
from selenium.common.exceptions import TimeoutException, NoSuchElementException

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
    time.sleep(5)

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

    # Step 4: Look for "Renouveler" button
    # The button appears when ≤2 days remaining
    renew_btn = None
    for text in ["Renouveler", "Renew", "renouveler"]:
        try:
            elements = driver.find_elements(By.XPATH, f"//button[normalize-space()='{text}'] | //a[normalize-space()='{text}']")
            if elements:
                renew_btn = elements[0]
                print(f"[aclclouds] ✅ Found Renew button: '{text}'")
                break
        except:
            continue

    if renew_btn:
        print("[aclclouds] Clicking Renouveler...")
        renew_btn.click()
        time.sleep(5)
        new_text = driver.find_element("tag name", "body").text
        temps_match2 = re.search(r'Temps restant\s*[:\s]*(\d+)\s*j\s*(?:(\d+)\s*h)?', new_text, re.IGNORECASE)
        if temps_match2:
            d2 = int(temps_match2.group(1))
            h2 = int(temps_match2.group(2)) if temps_match2.group(2) else 0
            print(f"[aclclouds] ✅ After Renew - Temps restant: {d2}j {h2}h")
        else:
            print("[aclclouds] ⚠️ Could not verify renewal result")
    else:
        if days is not None and days <= 2:
            print("[aclclouds] ⚠️ Window open (≤2 days) but no Renew button found!")
        else:
            window_days = 2
            if days is not None:
                window_days = max(0, days - 2)
            print(f"[aclclouds] ℹ️ Renew window not open yet (need ≤2 days, {days}j remaining)")

    driver.save_screenshot("/tmp/aclclouds-debug.png")

finally:
    driver.quit()
    print("[aclclouds] Browser closed")