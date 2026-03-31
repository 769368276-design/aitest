import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    out = Path(__file__).resolve().parent / "_ui_home.png"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        await page.goto("http://localhost:8003/accounts/login/", wait_until="networkidle")
        await page.locator('input[name="username"]').fill("admin")
        await page.locator('input[name="password"]').fill("123456")
        await page.get_by_role("button", name="登录").click()
        await page.wait_for_load_state("networkidle")
        await page.goto("http://localhost:8003/", wait_until="networkidle")
        await page.screenshot(path=str(out), full_page=True)
        await browser.close()
    print(str(out))


if __name__ == "__main__":
    asyncio.run(main())
