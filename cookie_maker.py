import subprocess
import threading
import asyncio
from playwright.async_api import async_playwright
import json
import os
import time

PROFILE_DIR = "/opt/render/.cache/playwright-profile"
COOKIES_FILE = "cookies.json"

EMAIL = os.getenv("YT_EMAIL")
PASSWORD = os.getenv("YT_PASSWORD")

# ----------- Playwright Cookie Refresher ----------- #
async def refresh_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True
        )
        page = await browser.new_page()

        # Open login page
        await page.goto("https://accounts.google.com/ServiceLogin")

        # Fill email
        await page.fill("input[type='email']", EMAIL)
        await page.click("button:has-text('Next')")
        await page.wait_for_timeout(3000)

        # Fill password
        await page.fill("input[type='password']", PASSWORD)
        await page.click("button:has-text('Next')")
        await page.wait_for_timeout(5000)  # wait for redirect to YouTube

        # Go to YouTube to confirm login
        await page.goto("https://youtube.com", wait_until="domcontentloaded")

        # Save cookies
        cookies = await browser.cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f, indent=2)

        print(f"✅ Cookies refreshed and saved to {COOKIES_FILE}")
        await browser.close()

# ----------- Run Bots as Subprocesses ----------- #
def run_bot(file):
    subprocess.Popen(["python3", file])

# ----------- Main ----------- #
if __name__ == "__main__":
    asyncio.run(refresh_cookies())
    time.sleep(2)


    while True:
        time.sleep(60)
