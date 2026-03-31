import re
import time
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from uiauto.models import UIAutoExecution, UIAutoStepRecord

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


class UIAutoRunner:
    def __init__(self, execution_id: int):
        self.execution_id = int(execution_id)
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self.stop_requested = False

    def run(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            self._update_execution_status("failed", {"error": "Playwright not installed"})
            return
        try:
            self._update_execution_status("running", {"status": "running", "engine": "uiauto_playwright"})
            with sync_playwright() as p:
                self.playwright = p
                headless = bool(getattr(settings, "UIAUTO_HEADLESS", True))
                self.browser = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"])
                self.context = self.browser.new_context(viewport={"width": 1440, "height": 900})
                self.page = self.context.new_page()
                execution = UIAutoExecution.objects.get(id=self.execution_id)
                steps = list(execution.case.steps.all().order_by("step_number"))
                if not steps:
                    self._update_execution_status("failed", {"error": "No steps found"})
                    return
                passed = 0
                failed = 0
                for step in steps:
                    if self._check_signals():
                        break
                    rec = UIAutoStepRecord.objects.create(
                        execution=execution,
                        step=step,
                        step_number=step.step_number,
                        description=step.description,
                        expected_result=step.expected_result or "",
                        status="pending",
                    )
                    ok, err, metrics = self._run_step(rec)
                    rec.metrics = metrics
                    if ok:
                        rec.status = "success"
                        passed += 1
                    else:
                        rec.status = "failed"
                        rec.error_message = str(err or "")
                        failed += 1
                        rec.save(update_fields=["status", "error_message", "metrics"])
                        self._update_execution_status("failed", {"passed": passed, "failed": failed, "error": str(err or "")})
                        break
                    rec.save(update_fields=["status", "metrics"])
                if failed == 0 and not self.stop_requested:
                    self._update_execution_status("completed", {"passed": passed, "failed": failed})
        except Exception as e:
            self._update_execution_status("failed", {"error": str(e)})
        finally:
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass

    def _run_step(self, step_record: UIAutoStepRecord):
        started = time.time()
        try:
            self._capture_screenshot(step_record, "before")
            action = self._parse_action(step_record.description or "")
            self._execute_action(action)
            self._verify_expected(step_record.expected_result or "")
            self._capture_screenshot(step_record, "after")
            elapsed_ms = int((time.time() - started) * 1000)
            return True, "", {"duration_ms": elapsed_ms, "action": action}
        except Exception as e:
            elapsed_ms = int((time.time() - started) * 1000)
            return False, str(e), {"duration_ms": elapsed_ms}

    def _parse_action(self, text: str) -> dict:
        s = str(text or "").strip()
        m_url = re.search(r"(https?://[^\s]+)", s, flags=re.I)
        if m_url:
            return {"type": "goto", "url": m_url.group(1)}
        if "等待" in s:
            m_wait = re.search(r"(\d+)\s*秒", s)
            sec = int(m_wait.group(1)) if m_wait else 1
            return {"type": "wait", "seconds": sec}
        if "输入" in s:
            m_val = re.search(r"[“\"]([^”\"]+)[”\"]", s)
            value = m_val.group(1) if m_val else ""
            m_target = re.search(r"(?:到|在)\s*[“\"]([^”\"]+)[”\"]", s)
            target = m_target.group(1) if m_target else ""
            return {"type": "fill", "target": target, "value": value}
        if "点击" in s:
            m_target = re.search(r"[“\"]([^”\"]+)[”\"]", s)
            target = m_target.group(1) if m_target else s.replace("点击", "").strip()
            return {"type": "click", "target": target}
        return {"type": "noop"}

    def _execute_action(self, action: dict) -> None:
        t = str(action.get("type") or "")
        if t == "goto":
            self.page.goto(str(action.get("url") or ""), wait_until="domcontentloaded", timeout=30000)
            return
        if t == "wait":
            sec = int(action.get("seconds") or 1)
            self.page.wait_for_timeout(max(0, sec) * 1000)
            return
        if t == "click":
            target = str(action.get("target") or "").strip()
            if not target:
                raise ValueError("点击动作缺少目标")
            self._click_with_fallback(target)
            return
        if t == "fill":
            value = str(action.get("value") or "")
            target = str(action.get("target") or "").strip()
            self._fill_with_fallback(target, value)
            return
        self.page.wait_for_timeout(300)

    def _click_with_fallback(self, target: str) -> None:
        candidates = [
            f"text={target}",
            f"button:has-text('{target}')",
            f"[aria-label*='{target}']",
            f"[title*='{target}']",
        ]
        last_err = None
        for sel in candidates:
            try:
                self.page.locator(sel).first.click(timeout=3500)
                return
            except Exception as e:
                last_err = e
        raise RuntimeError(str(last_err or "点击失败"))

    def _fill_with_fallback(self, target: str, value: str) -> None:
        if target:
            selectors = [
                f"input[placeholder*='{target}']",
                f"textarea[placeholder*='{target}']",
                f"input[aria-label*='{target}']",
                f"textarea[aria-label*='{target}']",
            ]
            for sel in selectors:
                try:
                    self.page.locator(sel).first.fill(value, timeout=3500)
                    return
                except Exception:
                    pass
        try:
            self.page.locator("input,textarea").first.fill(value, timeout=3500)
            return
        except Exception as e:
            raise RuntimeError(str(e))

    def _verify_expected(self, expected: str) -> None:
        exp = str(expected or "").strip()
        if not exp:
            return
        m = re.search(r"[“\"]([^”\"]+)[”\"]", exp)
        token = m.group(1) if m else exp
        token = token.strip()
        if not token:
            return
        content = self.page.content()
        if token not in content:
            raise AssertionError(f"未满足预期：{token}")

    def _capture_screenshot(self, step_record: UIAutoStepRecord, timing: str) -> None:
        try:
            image = self.page.screenshot()
            file_name = f"uiauto_{self.execution_id}_{step_record.id}_{timing}.png"
            if timing == "before":
                step_record.screenshot_before.save(file_name, ContentFile(image), save=False)
            else:
                step_record.screenshot_after.save(file_name, ContentFile(image), save=False)
            step_record.save()
        except Exception:
            pass

    def _check_signals(self) -> bool:
        execution = UIAutoExecution.objects.get(id=self.execution_id)
        if execution.stop_signal:
            self.stop_requested = True
            self._update_execution_status("stopped", {"stopped_at": datetime.now().isoformat()})
            return True
        if execution.pause_signal:
            self._update_execution_status("paused", {"status": "paused"})
            while True:
                time.sleep(0.5)
                execution.refresh_from_db()
                if execution.stop_signal:
                    self.stop_requested = True
                    self._update_execution_status("stopped", {"stopped_at": datetime.now().isoformat()})
                    return True
                if not execution.pause_signal:
                    self._update_execution_status("running", {"status": "running"})
                    break
        return False

    def _update_execution_status(self, status: str, summary: dict | None = None) -> None:
        execution = UIAutoExecution.objects.get(id=self.execution_id)
        execution.status = status
        if status in ("completed", "failed", "stopped"):
            execution.end_time = timezone.now()
        if summary is not None:
            execution.result_summary = summary
        fields = ["status", "result_summary"]
        if status in ("completed", "failed", "stopped"):
            fields.append("end_time")
        execution.save(update_fields=fields)
        try:
            if execution.case_id:
                if status == "completed":
                    execution.case.status = 2
                elif status in ("failed", "stopped"):
                    execution.case.status = 3 if status == "failed" else 5
                elif status == "running":
                    execution.case.status = 1
                execution.case.save(update_fields=["status"])
        except Exception:
            pass

