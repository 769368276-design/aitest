from __future__ import annotations

import os
import threading
from typing import AsyncGenerator

from ai_assistant.services.testcase_generation.types import StreamEvent
from ai_assistant.services.testcase_generation.strategies import ImageStrategy, OpenAPIStrategy, PdfStrategy, TextStrategy


class TestCaseGenerationEngine:
    def __init__(self) -> None:
        self._image = ImageStrategy()
        self._pdf = PdfStrategy()
        self._openapi = OpenAPIStrategy()
        self._text = TextStrategy()

    async def generate(self, file_path: str, context: str, requirements: str, cancel_event: threading.Event | None = None, user=None) -> AsyncGenerator[StreamEvent, None]:
        ext = (os.path.splitext(file_path or "")[1] or "").lower().lstrip(".")
        if ext in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
            async for ev in self._image.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
                yield ev
            return
        if ext == "pdf":
            async for ev in self._pdf.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
                yield ev
            return
        if ext in ("json", "yaml", "yml"):
            async for ev in self._openapi.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
                yield ev
            return
        async for ev in self._text.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
            yield ev
