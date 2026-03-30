from __future__ import annotations

import threading
from typing import AsyncGenerator, Protocol

from ai_assistant.services.testcase_generation.types import StreamEvent


class GenerationStrategy(Protocol):
    async def generate(self, file_path: str, context: str, requirements: str, cancel_event: threading.Event | None = None, user=None) -> AsyncGenerator[StreamEvent, None]:
        ...
