import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")

import django  # noqa: E402

django.setup()

if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
    del os.environ["PLAYWRIGHT_BROWSERS_PATH"]

from core.views import _ensure_playwright_browsers_path  # noqa: E402

_ensure_playwright_browsers_path()

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    b.close()

print("ok")
