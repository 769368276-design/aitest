import logging
import queue
import threading
import time

from django.db import close_old_connections
from django.conf import settings

from autotest.models import AutoTestExecution

logger = logging.getLogger(__name__)

_QUEUE: "queue.Queue[int]" = queue.Queue()
_WORKER_STARTED = False
_LOCK = threading.Lock()
_WORKERS: list[threading.Thread] = []


def enqueue_execution(execution_id: int) -> None:
    execution = AutoTestExecution.objects.get(id=execution_id)
    if execution.status == "running":
        return
    if execution.status in ("completed", "failed", "stopped"):
        return
    execution.status = "queued"
    execution.save(update_fields=["status"])
    try:
        if execution.case_id:
            execution.case.status = 1
            execution.case.save(update_fields=["status"])
    except Exception:
        pass
    _QUEUE.put(execution_id)


def start_worker(workers: int | None = None, daemon: bool = True) -> list[threading.Thread]:
    global _WORKER_STARTED
    with _LOCK:
        if _WORKER_STARTED:
            return list(_WORKERS)
        _WORKER_STARTED = True

    n = workers
    if n is None:
        n = int(getattr(settings, "AI_EXEC_WORKERS", 2) or 2)
    if n < 1:
        n = 1
    ts: list[threading.Thread] = []
    for i in range(n):
        t = threading.Thread(target=_worker_loop, daemon=daemon, name=f"autotest-execution-worker-{i+1}")
        t.start()
        ts.append(t)
    _WORKERS.extend(ts)
    return ts


def _worker_loop() -> None:
    while True:
        execution_id = None
        try:
            execution_id = _QUEUE.get(timeout=2)
        except Exception:
            execution_id = _pick_next_queued_execution_id()
            if execution_id is None:
                time.sleep(0.5)
                continue
        try:
            close_old_connections()
            claimed = AutoTestExecution.objects.filter(id=execution_id, status="queued").update(status="running")
            if not claimed:
                continue
            execution = AutoTestExecution.objects.get(id=execution_id)
            runner = build_runner(execution)
            runner.run()
        except Exception:
            import traceback
            err = traceback.format_exc()
            logger.error("Execution worker failed execution_id=%s\n%s", execution_id, err)
            try:
                execution = AutoTestExecution.objects.get(id=execution_id)
                execution.status = "failed"
                try:
                    rs = execution.result_summary if isinstance(execution.result_summary, dict) else {}
                    rs = dict(rs or {})
                    rs.setdefault("error", "execution_worker_failed")
                    rs.setdefault("traceback", err[-18000:])
                    execution.result_summary = rs
                except Exception:
                    pass
                execution.save(update_fields=["status", "result_summary"])
                try:
                    if execution.case_id:
                        execution.case.status = 5
                        execution.case.save(update_fields=["status"])
                except Exception:
                    pass
            except Exception:
                pass
        finally:
            try:
                close_old_connections()
            except Exception:
                pass
            try:
                _QUEUE.task_done()
            except Exception:
                pass


def _pick_next_queued_execution_id() -> int | None:
    try:
        qs = AutoTestExecution.objects.filter(status="queued").order_by("id").values_list("id", flat=True)[:1]
        return int(qs[0]) if qs else None
    except Exception:
        return None


def build_runner(execution: AutoTestExecution):
    from autotest.services.browser_use_runner import BrowserUseRunner
    return BrowserUseRunner(int(execution.id))
