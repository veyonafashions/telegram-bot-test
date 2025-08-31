import asyncio
from playwright.async_api import async_playwright
import json

async def run():
    email = os.environ["YT_EMAIL"]
    password = os.environ["YT_PASSWORD"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Go to YouTube login
        await page.goto("https://accounts.google.com/ServiceLogin")

        # Enter email
        await page.fill("input[type=email]", email)
        await page.click("button:has-text('Next')")
        await page.wait_for_timeout(2000)

        # Enter password
        await page.fill("input[type=password]", password)
        await page.click("button:has-text('Next')")
        await page.wait_for_timeout(5000)

        # Save cookies
        cookies = await context.cookies()
        with open("cookies.json", "w") as f:
            json.dump(cookies, f, indent=2)

        print("✅ Cookies refreshed and saved to cookies.json")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
