import subprocess
import threading
import asyncio
from playwright.async_api import async_playwright
import json
import os
import time

PROFILE_DIR = "/opt/render/.cache/playwright-profile"
COOKIES_FILE = "cookies.json"

# ----------- Playwright Cookie Refresher ----------- #
async def refresh_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True
        )
        page = await browser.new_page()
        await page.goto("https://youtube.com")

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
    # 1. Refresh cookies once
    asyncio.run(refresh_cookies())

    # 2. Start converter
    threading.Thread(target=run_bot, args=("j_to_txt.py",)).start()

    # 3. Start Telegram bot
    threading.Thread(target=run_bot, args=("bot.py",)).start()

    # 4. Keep alive
    while True:
        time.sleep(60)
