import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--ignore-certificate-errors",
                "--ignore-ssl-errors",
                "--disable-web-security",
                "--no-sandbox",
                "--ignore-certificate-errors-spki-list",
            ]
        )
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        try:
            await page.goto("https://ywtb.cuit.edu.cn/file/apps/yypt/index.html#/ic/home",
                           wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            print("URL:", page.url)
            print("Title:", await page.title())
            input("Press Enter to close...")
        except Exception as e:
            print("Error:", type(e).__name__, str(e)[:200])
            input("Press Enter to close...")
        finally:
            await browser.close()

asyncio.run(test())
