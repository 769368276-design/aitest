from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class EvidenceItem:
    ts: float
    kind: str
    text: str
    meta: dict


class EvidenceBuffer:
    def __init__(self, maxlen: int = 240):
        self._maxlen = max(20, int(maxlen or 240))
        self._buf: deque[EvidenceItem] = deque(maxlen=self._maxlen)
        self._lock = threading.Lock()

    def add(self, kind: str, text: str, meta: dict | None = None, ts: float | None = None):
        k = str(kind or "").strip()[:40]
        t = str(text or "").strip()[:800]
        if not k or not t:
            return
        m = meta if isinstance(meta, dict) else {}
        item = EvidenceItem(ts=float(ts or time.time()), kind=k, text=t, meta=m)
        with self._lock:
            self._buf.append(item)

    def snapshot(self, max_items: int = 30) -> list[dict]:
        try:
            n = max(1, int(max_items or 30))
        except Exception:
            n = 30
        with self._lock:
            items = list(self._buf)[-n:]
        out = []
        for it in items:
            out.append({"ts": it.ts, "kind": it.kind, "text": it.text, "meta": it.meta})
        return out

    def last_texts(self, kind: str, n: int = 5) -> list[str]:
        k = str(kind or "").strip()
        if not k:
            return []
        try:
            n2 = max(1, int(n or 5))
        except Exception:
            n2 = 5
        with self._lock:
            items = [x for x in self._buf if x.kind == k]
        return [x.text for x in items][-n2:]

