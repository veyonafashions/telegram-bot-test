import asyncio
from playwright.async_api import async_playwright
import json

EMAIL = "veyonafashions@gmail.com"
PASSWORD = "-3d5TcQWJ5Z2XYk"

async def login_and_save_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headless=True if you don’t want window
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://accounts.google.com/")

        # Enter email
        await page.fill('input[type="email"]', EMAIL)
        await page.click("#identifierNext")
        await page.wait_for_timeout(2000)

        # Enter password
        await page.fill('input[type="password"]', PASSWORD)
        await page.click("#passwordNext")

        print("👉 If CAPTCHA/2FA pops up, solve it manually...")

        # Wait until login completes
        await page.wait_for_url("https://myaccount.google.com/*", timeout=0)

        # Save cookies
        cookies = await context.cookies()
        with open("youtube_cookies.json", "w") as f:
            json.dump(cookies, f)

        print("✅ Cookies saved to youtube_cookies.json")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(login_and_save_cookies())
