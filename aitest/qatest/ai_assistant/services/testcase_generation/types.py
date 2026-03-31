from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncGenerator, Literal, Optional


EventType = Literal["meta", "delta", "progress", "final", "error", "done"]


@dataclass
class StreamEvent:
    type: EventType
    text: str = ""
    message: str = ""
    code: str = ""
    page: Optional[int] = None


Stream = AsyncGenerator[StreamEvent, None]
