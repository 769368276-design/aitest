import os
import json
import time
import asyncio
import logging
import threading
from django.utils import timezone
from django.conf import settings
from django.core.files.base import ContentFile
from asgiref.sync import sync_to_async

from autotest.models import AutoTestExecution, AutoTestStepRecord, AutoTestNetworkEntry
from autotest.services.ai_agent import AIAgent

logger = logging.getLogger(__name__)

# Try to import playwright
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed. AutoTest will fail.")

class PlaywrightRunner:
    def __init__(self, execution_id):
        self.execution_id = execution_id
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        user = None
        try:
            execution = AutoTestExecution.objects.select_related("executor").get(id=self.execution_id)
            user = execution.executor
        except Exception:
            user = None
        self.ai_agent = AIAgent(user=user)
        self.stop_requested = False
        self.pause_requested = False

    def run(self):
        """
        Main execution entry point. Should be called in a separate thread.
        """
        if not PLAYWRIGHT_AVAILABLE:
            self._update_execution_status('failed', summary={"error": "Playwright not installed"})
            return

        try:
            self._update_execution_status('running')
            
            with sync_playwright() as p:
                self.playwright = p
                headless = bool(getattr(settings, "AI_EXEC_HEADLESS", True))
                self.browser = p.chromium.launch(headless=headless, args=['--no-sandbox', '--disable-setuid-sandbox'])
                self.context = self.browser.new_context(
                    viewport={'width': 1280, 'height': 720},
                    record_video_dir=None # Could add video later
                )
                self.page = self.context.new_page()
                
                # Setup network logging
                self.page.on("request", self._on_request)
                self.page.on("response", self._on_response)

                execution = AutoTestExecution.objects.get(id=self.execution_id)
                steps = execution.case.steps.all().order_by('step_number')
                
                if not steps.exists():
                     self._update_execution_status('failed', summary={"error": "No steps found in test case"})
                     return
                
                passed_steps = 0
                failed_steps = 0
                
                for step in steps:
                    # Check control signals
                    if self._check_signals():
                        break
                        
                    # Create step record
                    step_record = AutoTestStepRecord.objects.create(
                        execution=execution,
                        step=step,
                        step_number=step.step_number,
                        description=step.description,
                        status='pending'
                    )
                    
                    # Run step
                    success = self._run_step(step_record)
                    
                    if success:
                        passed_steps += 1
                        step_record.status = 'success'
                    else:
                        failed_steps += 1
                        step_record.status = 'failed'
                        step_record.save()
                        # Stop on failure? For now, yes.
                        self._update_execution_status('failed')
                        break
                        
                    step_record.save()

                if failed_steps == 0 and not self.stop_requested:
                    self._update_execution_status('completed')
                
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            self._update_execution_status('failed', summary={"error": str(e)})
        finally:
            if self.browser:
                self.browser.close()

    def _run_step(self, step_record):
        """
        Run a single step using AI.
        """
        try:
            logger.info(f"Running step {step_record.step_number}: {step_record.description}")
            # 1. Capture State Before
            screenshot_mode = (getattr(settings, "AI_EXEC_SCREENSHOT_MODE", "all") or "all").lower()
            if screenshot_mode == "all":
                self._capture_screenshot(step_record, 'before')
            
            # 2. Get Page Context
            context = self._get_page_context()
            
            # 3. Ask AI
            max_retries = int(getattr(settings, "AI_EXEC_MAX_RETRIES", 3) or 3)
            retry_sleep_ms = int(getattr(settings, "AI_EXEC_RETRY_SLEEP_MS", 200) or 200)
            for attempt in range(max_retries):
                logger.info(f"Asking AI for step {step_record.id}, attempt {attempt+1}")
                # Use async_to_sync instead of asyncio.run to avoid Django async context issues
                from asgiref.sync import async_to_sync
                try:
                    action_plan = async_to_sync(self.ai_agent.get_action)(step_record.description, context)
                except Exception as e:
                    logger.error(f"AI Agent call failed: {e}")
                    raise e
                
                step_record.ai_thought = json.dumps(action_plan, ensure_ascii=False)
                step_record.save()
                
                if action_plan.get('action') == 'error':
                    step_record.error_message = action_plan.get('reason')
                    return False

                # 4. Execute Action
                try:
                    self._execute_playwright_action(action_plan)
                    break # Success
                except Exception as e:
                    logger.error(f"Playwright action failed: {e}")
                    if attempt == max_retries - 1:
                        step_record.error_message = f"Failed after {max_retries} attempts: {str(e)}"
                        return False
                    # Retry logic could be added here (feedback loop to AI)
                    time.sleep(max(0, retry_sleep_ms) / 1000.0)

            # 5. Capture State After
            if screenshot_mode == "all":
                self._capture_screenshot(step_record, 'after')
            return True

        except Exception as e:
            logger.error(f"Step execution error: {e}")
            step_record.error_message = str(e)
            return False

    def _execute_playwright_action(self, action_plan):
        action = action_plan.get('action')
        selector = action_plan.get('selector')
        value = action_plan.get('value')

        if action == 'goto':
            # For goto, selector might be the url or value might be the url
            url = selector if selector and selector.startswith('http') else value
            if not url:
                raise ValueError("No URL provided for goto")
            self.page.goto(url)
        
        elif action == 'click':
            self.page.click(selector)
            
        elif action == 'fill':
            self.page.fill(selector, value)
            
        elif action == 'press':
            self.page.press(selector, value)
            
        elif action == 'wait':
            self.page.wait_for_selector(selector, state=value or 'visible')
            
        else:
            raise ValueError(f"Unknown action: {action}")
            
        # Wait a bit for animations/network
        step_wait_ms = int(getattr(settings, "AI_EXEC_STEP_WAIT_MS", 250) or 250)
        if step_wait_ms > 0:
            try:
                self.page.wait_for_timeout(step_wait_ms)
            except Exception:
                pass
        networkidle_timeout_ms = int(getattr(settings, "AI_EXEC_NETWORKIDLE_TIMEOUT_MS", 1000) or 1000)
        if networkidle_timeout_ms > 0:
            try:
                self.page.wait_for_load_state('networkidle', timeout=networkidle_timeout_ms)
            except Exception:
                pass

    def _get_page_context(self):
        try:
            # Simplified accessibility tree
            snapshot = self.page.accessibility.snapshot()
            return {
                "url": self.page.url,
                "title": self.page.title(),
                "accessibility_tree": snapshot
            }
        except:
            return {
                "url": self.page.url,
                "title": self.page.title(),
                "accessibility_tree": "Unavailable"
            }

    def _capture_screenshot(self, step_record, timing):
        try:
            screenshot_bytes = self.page.screenshot()
            file_name = f"exec_{self.execution_id}_step_{step_record.id}_{timing}.png"
            if timing == 'before':
                step_record.screenshot_before.save(file_name, ContentFile(screenshot_bytes), save=False)
            else:
                step_record.screenshot_after.save(file_name, ContentFile(screenshot_bytes), save=False)
            step_record.save()
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")

    def _check_signals(self):
        """
        Check DB for pause/stop signals.
        """
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        if execution.stop_signal:
            self.stop_requested = True
            self._update_execution_status('stopped')
            return True
            
        if execution.pause_signal:
            self._update_execution_status('paused')
            while True:
                time.sleep(0.5)
                execution.refresh_from_db()
                if not execution.pause_signal:
                    self._update_execution_status('running')
                    break
                if execution.stop_signal:
                    self.stop_requested = True
                    self._update_execution_status('stopped')
                    return True
        return False

    def _update_execution_status(self, status, summary=None):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        execution.status = status
        if status in ['completed', 'failed', 'stopped']:
            execution.end_time = timezone.now()
        if summary:
            execution.result_summary = summary
        execution.save()

    def _on_request(self, request):
        # We need to map this to the current step record.
        # This is tricky in async callbacks.
        # Simplification: Store in a temporary list and flush to the latest active step.
        pass

    def _on_response(self, response):
        try:
            # Find the latest pending/running step record
            step_record = AutoTestStepRecord.objects.filter(
                execution_id=self.execution_id, 
                status='pending'
            ).last()
            
            if step_record:
                AutoTestNetworkEntry.objects.create(
                    step_record=step_record,
                    url=response.url,
                    method=response.request.method,
                    status_code=response.status,
                    # Limit data size
                    # request_data=response.request.post_data or '', 
                    # response_data=... (response.text() can be large and slow)
                )
        except:
            pass
