
import logging
import os
from pathlib import Path
logger = logging.getLogger(__name__)

def log_debug(msg):
    text = str(msg)
    try:
        logger.debug(text)
    except Exception:
        pass
    log_path = (os.getenv("TESTCASES_DEBUG_LOG_FILE", "") or "").strip()
    if not log_path:
        return
    try:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        return
