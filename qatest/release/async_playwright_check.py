import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
    del os.environ["PLAYWRIGHT_BROWSERS_PATH"]

pw = Path(str(settings.BASE_DIR)) / ".playwright"
if pw.is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw)


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        await b.close()


asyncio.run(main())
print("ok")

