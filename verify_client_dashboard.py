import asyncio
from playwright.async_api import async_playwright
import os

async def verify_client_dashboard():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={'width': 1280, 'height': 800})
        page = await context.new_page()

        # Login as a test user
        # Note: We need a valid user from the DB. Based on previous runs, we might have one.
        # If not, we'll try to login with common credentials or check the DB.

        await page.goto('http://localhost:5000/login')
        await page.fill('#username', 'testuser') # Adjust if known
        await page.fill('#password', 'password123')
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(2000)

        # Take screenshot of dashboard
        await page.screenshot(path='/home/jules/verification/client_dashboard_main.png')
        print("Captured client_dashboard_main.png")

        # Go to settings
        await page.click('#tab-settings')
        await page.wait_for_timeout(1000)
        await page.screenshot(path='/home/jules/verification/client_settings.png')
        print("Captured client_settings.png")

        # Check broker selector
        await page.select_option('#broker-type-selector', 'upstox')
        await page.wait_for_timeout(500)
        await page.screenshot(path='/home/jules/verification/client_settings_upstox.png')
        print("Captured client_settings_upstox.png")

        await browser.close()

if __name__ == "__main__":
    if not os.path.exists('/home/jules/verification'):
        os.makedirs('/home/jules/verification')
    asyncio.run(verify_client_dashboard())
