import asyncio
from playwright.async_api import async_playwright
import json

async def login_and_save_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # False = you see the login window
        context = await browser.new_context()

        # Open YouTube login page
        page = await context.new_page()
        await page.goto("https://accounts.google.com/")

        print("👉 Please log in manually in the browser window...")

        # Wait until login finishes (you reach YouTube home)
        await page.wait_for_url("https://myaccount.google.com/*", timeout=0)

        # Save cookies
        cookies = await context.cookies()
        with open("youtube_cookies.json", "w") as f:
            json.dump(cookies, f)

        print("✅ Cookies saved to youtube_cookies.json")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(login_and_save_cookies())
