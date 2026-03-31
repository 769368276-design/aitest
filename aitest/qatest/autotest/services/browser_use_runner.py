import asyncio
import json
import logging
import os
import subprocess
import time
import tempfile
import socket
import shutil
import urllib.request
import urllib.error
import uuid
import re
import hashlib
import base64
import types
from html import unescape
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile

if os.name == "nt":
    try:
        import browser_use.browser.profile as bu_profile

        cls = getattr(bu_profile, "BrowserLaunchArgs", None)
        original = getattr(cls, "set_default_downloads_path", None) if cls is not None else None
        if original is not None:
            def _patched(self):  # type: ignore[no-untyped-def]
                if getattr(self, "downloads_path", None) is None:
                    import uuid as _uuid
                    from pathlib import Path

                    base = Path(tempfile.gettempdir()) / "browser-use-downloads"
                    unique_id = str(_uuid.uuid4())[:8]
                    downloads_path = base / f"browser-use-downloads-{unique_id}"
                    while downloads_path.exists():
                        unique_id = str(_uuid.uuid4())[:8]
                        downloads_path = base / f"browser-use-downloads-{unique_id}"
                    self.downloads_path = downloads_path
                    self.downloads_path.mkdir(parents=True, exist_ok=True)
                return self

            target = getattr(original, "__func__", None) or original
            target.__code__ = _patched.__code__
            target.__defaults__ = _patched.__defaults__
            target.__kwdefaults__ = _patched.__kwdefaults__
    except Exception:
        pass

from browser_use import Agent, Browser
from langchain_core.messages import HumanMessage

from autotest.models import AutoTestExecution, AutoTestStepRecord, AutoTestNetworkEntry
from bugs.models import Bug
from autotest.utils.llm_factory import get_llm_model
from autotest.utils.qa_tools import QATools
from autotest.services.evidence import EvidenceBuffer
from autotest.services.stop_policy import StopPolicy
from users.ai_config import resolve_ocr_params

logger = logging.getLogger(__name__)

class _AgentEarlyStop(Exception):
    pass

class _AgentManualStop(Exception):
    pass

class BrowserUseRunner:
    def __init__(self, execution_id):
        self.execution_id = execution_id
        # We fetch execution only when needed to avoid stale data
        self.stop_requested = False
        self.browser_process = None
        self.user_data_dir = None
        self._persistent_profile_dir = None
        self._using_persistent_profile = False
        self._preflight_switched = False
        self._current_step_number = 0
        self._pw = None
        self._pw_browser = None
        self._pw_contexts = []
        self._pending_requests = {}
        self._seen_message_keys = set()
        self._testcase_steps = []
        self._runtime_messages = []
        self._message_poller_task = None
        self._signal_poller_task = None
        self._pause_event = None
        self._llm = None
        self._executor_user = None
        self._vision_checked_steps = set()
        self._cdp_sessions = []
        self._cdp_request_map = {}
        self._cdp_response_map = {}
        self._stop_policy = StopPolicy.from_settings(settings)
        self._evidence = EvidenceBuffer(int(getattr(settings, "AI_EXEC_EVIDENCE_MAXLEN", 240) or 240))
        self._toast_vision_checked_steps = set()
        self._toast_vision_found = False
        self._early_stop_triggered = False
        self._seen_auth_response = False
        self._last_auth_step_number = None
        self._last_auth_status = None
        self._last_auth_url = None
        self._last_auth_body_norm = None
        self._last_vision_toast_ts = 0.0
        self._case_title = ""
        self._ai_stop_checked_steps = set()
        self._stopped_by_ai = False
        self._covered_pages = set()
        self._covered_slides = set()
        self._required_slide_total = None
        self._wants_full_slides_cached = None
        self._page_state_last = None
        self._page_state_stable = 0
        self._pagination_attempts = 0
        self._step_started_at = {}
        self._agent_step_seq = 0
        self._current_agent_step = 0
        self._step_timeout_tasks = {}
        self._last_stop_check_ts = 0.0
        self._last_stop_check_sig = ""
        self._stop_check_running = False
        self._early_history = None
        self._active_agent = None
        self._stop_reason = ""
        self._forced_bug_id = None
        self._assert_failed_step_number = 0
        self._assert_failed_summary = ""
        self._case_steps_total = 0
        self._case_steps_done = set()
        self._case_step_last_seen = 0
        self._case_steps_asserted = set()
        self._case_steps_soft_checked = set()
        self._non_blocking_by_case_step = {}
        self._non_blocking_escalated = set()
        self._login_attempted = False
        self._login_attempted_ts = 0.0
        self._auto_login_done = False
        self._expected_login_username = ""
        self._expected_login_password = ""
        self._last_auth_req_username = ""
        self._last_auth_req_password = ""
        self._last_auth_req_password_sha256_12 = ""
        self._unexpected_login_creds = None
        self._non_blocking_issue_notes = []
        self._scroll_policy_violations = 0
        self._confirm_loop_counts = {}
        self._hint_overlay_last_ts = 0.0
        self._transfer_file_applied_steps = set()
        self._transfer_file_disk_paths = {}
        self._agent_available_file_paths = []
        self._consecutive_wait_actions = 0
        self._filechooser_page_id = None
        self._filechooser_payload = None
        self._filechooser_case_step_no = None
        self._filechooser_hit_count = 0
        self._last_created_page = None
        self._last_created_page_ts = 0.0
        self._blank_page_open_ts = []
        self._last_page_urls = []
        self._submit_repeat_sig = ""
        self._submit_repeat_count = 0
        self._submit_repeat_last_url = ""
        self._submit_repeat_last_msg_count = 0
        self._action_repeat_sig = ""
        self._action_repeat_count = 0
        self._action_repeat_last_url = ""
        self._action_repeat_last_msg_count = 0
        self._last_effect_url = ""
        self._last_effect_net_count = None
        self._submit_repeat_last_effect = ""
        self._action_repeat_last_effect = ""
        self._no_effect_token_last = ""
        self._no_effect_streak = 0
        self._case_step_hold_no = 0
        self._case_step_hold_actions = 0
        self._save_like_no_success = 0
        self._save_like_last_effect = ""
        self._save_like_started_at = 0.0
        self._save_like_no_prompt_clicks = 0

    def _is_blank_like_url(self, url: str) -> bool:
        u = str(url or "").strip().lower()
        if not u:
            return True
        if u.startswith("about:"):
            return True
        if u.startswith("chrome://") or u.startswith("edge://"):
            return True
        return False

    def _sanitize_goto_url(self, url: str) -> str:
        u = str(url or "").strip()
        if not u:
            return ""
        m = re.match(r"^(https?://)\s*(https?://.+)$", u, flags=re.I)
        if m:
            return str(m.group(2) or "").strip()
        return u

    def _is_feedback_message(self, text: str) -> bool:
        s = str(text or "").strip()
        if not s:
            return False
        if len(s) <= 2:
            return False
        low = s.lower()
        if any(k in s for k in ["成功", "失败", "错误", "异常", "无效", "不正确", "格式", "必填", "不能为空", "已存在", "重复", "无权限", "超时", "请", "提示"]):
            return True
        if any(k in low for k in ["error", "failed", "invalid", "required", "forbidden", "unauthorized", "timeout", "please"]):
            return True
        return False

    def _record_save_like_observation(self, prompt_seen: bool, response_seen: bool, save_effect: str) -> bool:
        try:
            if prompt_seen:
                self._save_like_no_prompt_clicks = 0
            else:
                self._save_like_no_prompt_clicks = int(self._save_like_no_prompt_clicks or 0) + 1
        except Exception:
            pass
        if response_seen:
            self._save_like_no_success = 0
            self._save_like_last_effect = ""
            self._save_like_started_at = 0.0
        else:
            try:
                if str(save_effect or "") and str(save_effect or "") == str(self._save_like_last_effect or ""):
                    self._save_like_no_success = int(self._save_like_no_success or 0) + 1
                else:
                    self._save_like_last_effect = str(save_effect or "")[:900]
                    self._save_like_no_success = 1
                    self._save_like_started_at = float(time.time())
            except Exception:
                pass
        try:
            return int(self._save_like_no_prompt_clicks or 0) >= 2
        except Exception:
            return False

    def _humanize_llm_auth_error(self, msg: str) -> str:
        s = str(msg or "").strip()
        if not s:
            return s
        low = s.lower()
        hit = False
        if ("invalid_api_key" in low) or ("incorrect api key" in low) or ("apikey_error" in low):
            hit = True
        if (not hit) and ("401" in low) and ("api" in low) and ("key" in low):
            hit = True
        if not hit:
            return s
        try:
            if bool(getattr(self, "_llm_auth_hint_added", False)):
                return s
        except Exception:
            pass
        try:
            setattr(self, "_llm_auth_hint_added", True)
        except Exception:
            pass
        provider = ""
        base_url = ""
        model = ""
        try:
            from users.ai_config import resolve_exec_params
            p = resolve_exec_params(getattr(self, "_executor_user", None))
            provider = str(getattr(p, "provider", "") or "").strip().lower()
            base_url = str(getattr(p, "base_url", "") or "").strip()
            model = str(getattr(p, "model", "") or "").strip()
        except Exception:
            provider = ""
            base_url = ""
            model = ""
        hint = "AI 执行模型鉴权失败（401）。请到「个人中心 → 模型配置 → AI执行」检查：提供方/Base URL/模型名/Key 是否匹配。"
        try:
            b = (base_url or "").lower()
            if (provider == "qwen") or ("dashscope" in b) or ("aliyuncs.com" in b):
                hint = "AI 执行模型鉴权失败（401）。当前看起来像 Qwen（DashScope）网关：请确认你填的是 DashScope 的 Key（不是 Kimi/OpenAI/其它平台的 Key）。"
            elif provider == "kimi" or ("moonshot" in b):
                hint = "AI 执行模型鉴权失败（401）。当前看起来像 Kimi（Moonshot）网关：请确认 Key 来自 Moonshot 控制台，Base URL 为 https://api.moonshot.cn/v1，模型名建议 kimi-k2.5 或 moonshot-v1-32k。"
            elif provider == "openai" or ("api.openai.com" in b):
                hint = "AI 执行模型鉴权失败（401）。当前看起来像 OpenAI：请确认 Key 正确且未过期/未禁用。"
            elif provider == "openrouter" or ("openrouter.ai" in b):
                hint = "AI 执行模型鉴权失败（401）。当前看起来像 OpenRouter：请确认 Key 正确，并且模型名形如 openai/gpt-4o。"
            elif provider == "doubao" or ("volces" in b) or ("ark.cn-" in b):
                hint = "AI 执行模型鉴权失败（401）。当前看起来像 豆包（火山引擎 Ark）：请确认 Key 正确，Base URL 为 https://ark.cn-beijing.volces.com/api/v3，模型名为你的 Endpoint/模型名。"
        except Exception:
            pass
        conf = "；".join([x for x in [f"provider={provider}" if provider else "", f"model={model}" if model else "", f"base_url={base_url}" if base_url else ""] if x])[:400]
        if conf:
            return f"{s}\n\n{hint}\n当前配置：{conf}"
        return f"{s}\n\n{hint}"

    def _detect_gateway_error_code(self, title: str, body_text: str) -> int:
        t = str(title or "").strip().lower()
        b = str(body_text or "").strip().lower()
        s = (t + "\n" + b)[:6000]
        if ("bad gateway" in s and "502" in s) or ("502 bad gateway" in s):
            return 502
        if ("gateway timeout" in s and "504" in s) or ("504 gateway timeout" in s):
            return 504
        if "nginx" in s and "bad gateway" in s:
            return 502
        return 0

    def _classify_failure_reason(self, stop_reason: str, final_status: str) -> tuple[str, str]:
        reason = str(stop_reason or "").strip().lower()
        status = str(final_status or "").strip().lower()
        if status == "completed":
            return "success", "success"
        if reason == "manual_stop":
            return "manual_stop", "manual"
        if reason in ("assert_failed", "non_blocking_bug", "blocking_bug"):
            return reason, "assertion"
        if reason == "login_failed":
            return "login_failed", "auth"
        if reason == "max_steps":
            return "max_steps", "limit"
        if reason.startswith("http_"):
            try:
                code = int(reason.split("_", 1)[1])
            except Exception:
                code = 0
            if code >= 500:
                return "http_5xx", "network"
            if code >= 400:
                return "http_4xx", "network"
            return "http_error", "network"
        if reason in ("blank_page_loop", "submit_no_effect", "save_no_response", "no_effect_streak", "action_loop_no_effect", "case_step_not_progressing"):
            return "no_effect", "interaction"
        if "timeout" in reason:
            return "timeout", "timeout"
        if reason in ("steps_completed", "ai_early_done"):
            return reason, "flow"
        if status == "stopped":
            return "manual_stop", "manual"
        if status == "failed":
            return "unknown_failure", "unknown"
        return "unknown", "unknown"

    @sync_to_async
    def _first_failed_step_no_async(self) -> int:
        try:
            execution = AutoTestExecution.objects.get(id=self.execution_id)
            s = (
                AutoTestStepRecord.objects.filter(execution=execution, status="failed")
                .order_by("step_number")
                .values_list("step_number", flat=True)
                .first()
            )
            if s is None:
                return 0
            return int(s or 0)
        except Exception:
            return 0

    @sync_to_async
    def _probe_http_status_async(self, url: str) -> dict:
        u = str(url or "").strip()
        if not u or (not u.lower().startswith("http")):
            return {"ok": False, "error": "invalid_url"}
        try:
            req = urllib.request.Request(
                u,
                method="GET",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                st = int(getattr(resp, "status", 0) or 0)
                return {"ok": True, "status": st}
        except urllib.error.HTTPError as e:
            try:
                return {"ok": True, "status": int(getattr(e, "code", 0) or 0)}
            except Exception:
                return {"ok": True, "status": 0}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}

    @sync_to_async
    def _get_network_entry_count_async(self) -> int:
        try:
            return int(
                AutoTestNetworkEntry.objects.filter(step_record__execution_id=int(self.execution_id)).count()
            )
        except Exception:
            return 0

    async def _compute_dom_effect_sig_async(self, page) -> str:
        if not page:
            return ""
        js = r"""
        () => {
          const isVisible = (el) => {
            try {
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
              const r = el.getBoundingClientRect();
              if (!r || r.width < 6 || r.height < 6) return false;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
              return true;
            } catch (e) { return false; }
          };
          const pickText = (el) => {
            try {
              const t = String((el.innerText || el.textContent || '')).replace(/\s+/g,' ').trim();
              return t.replace(/\d+/g, '#');
            } catch (e) { return ''; }
          };
          const takeTexts = (selector, limit) => {
            const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
            const out = [];
            for (const n of nodes) {
              const t = pickText(n);
              if (!t) continue;
              out.push(t.slice(0, 120));
              if (out.length >= limit) break;
            }
            return out;
          };
          const headings = takeTexts('h1,h2,.page-title,.ant-page-header-heading-title,.el-page-header__title,.card-title', 3);
          const alerts = takeTexts('[role="alert"],[aria-live="assertive"],[aria-live="polite"],.toast,.ant-message,.ant-notification,.el-message,.el-notification', 3);
          const modals = Array.from(document.querySelectorAll('[role="dialog"],.modal.show,.ant-modal-wrap,.el-dialog__wrapper,.swal2-container')).filter(isVisible).length;
          const errNodes = Array.from(document.querySelectorAll('.invalid-feedback,.ant-form-item-explain-error,.el-form-item__error,[aria-invalid="true"]')).filter(isVisible).length;
          return {
            p: String(location.pathname || '') + String(location.search || ''),
            t: String(document.title || '').replace(/\d+/g, '#').slice(0, 80),
            r: String(document.readyState || ''),
            h: headings,
            a: alerts,
            m: Number(modals || 0),
            e: Number(errNodes || 0)
          };
        }
        """
        try:
            out = await page.evaluate(js)
            if not isinstance(out, dict):
                return ""
            raw = json.dumps(out, ensure_ascii=False, sort_keys=True)[:8000]
            return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
        except Exception:
            return ""

    async def _build_no_effect_token_async(self, page, url_now: str, net_now: int) -> str:
        dom_sig = ""
        try:
            dom_sig = await self._compute_dom_effect_sig_async(page)
        except Exception:
            dom_sig = ""
        return f"u={str(url_now or '')[:300]}|d={str(dom_sig or '')}|n={int(net_now or 0)}"

    def _gen_alt_value_for_smart_data(self, banned_value: str) -> tuple[str, str]:
        s = str(banned_value or "").strip()
        lower = s.lower()
        salt = uuid.uuid4().hex[:6]
        if re.fullmatch(r"\d{8,15}", s):
            prefix = "13" if (salt[0] in "01234567") else ("15" if (salt[0] in "89ab") else "18")
            digits = (prefix + re.sub(r"\D", "", salt + uuid.uuid4().hex))[:11]
            if digits == s:
                digits = (prefix + re.sub(r"\D", "", uuid.uuid4().hex + salt))[:11]
            return digits, "phone"
        if ("@" in s) and ("." in s) and len(s) <= 80:
            return f"autotest_{salt}@example.com", "email"
        if len(s) >= 8 and any(c.isalpha() for c in s) and any(c.isdigit() for c in s):
            return f"Pw{salt}A9!", "password"
        if re.fullmatch(r"[A-Za-z0-9_]{3,24}", s):
            return f"autotest_{salt}", "username"
        if "pass" in lower or "pwd" in lower or "密码" in s:
            return f"Pw{salt}A9!", "password"
        return f"autotest_{salt}", "text"

    async def _replace_input_value_async(self, page, old_value: str, new_value: str) -> bool:
        js = """
(oldV, newV) => {
  try {
    const isVisible = (el) => {
      try {
        const st = window.getComputedStyle(el);
        if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 6 || r.height < 6) return false;
        if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
        return true;
      } catch (e) { return false; }
    };
    const setVal = (el) => {
      const tag = String(el.tagName || '').toLowerCase();
      if (!(tag === 'input' || tag === 'textarea' || el.isContentEditable)) return false;
      if (el.isContentEditable) el.innerText = String(newV || '');
      else el.value = String(newV || '');
      try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
      try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
      return true;
    };
    const oldS = String(oldV || '');
    const active = document.activeElement;
    if (active && isVisible(active)) {
      const tag = String(active.tagName || '').toLowerCase();
      const cur = active.isContentEditable ? String(active.innerText || '') : String(active.value || '');
      if ((tag === 'input' || tag === 'textarea' || active.isContentEditable) && cur.trim() === oldS.trim()) {
        if (setVal(active)) return { ok: true, mode: 'active' };
      }
    }
    const nodes = Array.from(document.querySelectorAll('input,textarea,[contenteditable="true"]')).filter(isVisible);
    for (const el of nodes) {
      const cur = el.isContentEditable ? String(el.innerText || '') : String(el.value || '');
      if (cur.trim() === oldS.trim()) {
        if (setVal(el)) return { ok: true, mode: 'scan' };
      }
    }
    return { ok: false, reason: 'not_found' };
  } catch (e) {
    return { ok: false, reason: String(e && e.message ? e.message : e) };
  }
}
"""
        try:
            out = await page.evaluate(js, str(old_value or ""), str(new_value or ""))
            return bool(isinstance(out, dict) and out.get("ok"))
        except Exception:
            return False

    async def _remember_new_page_async(self, page, source: str = ""):
        try:
            self._last_created_page = page
            self._last_created_page_ts = time.time()
            try:
                u = str(getattr(page, "url", "") or "")
            except Exception:
                u = ""
            if u:
                self._last_page_urls.append(u[:500])
                self._last_page_urls = self._last_page_urls[-12:]
        except Exception:
            pass

    async def _ensure_foreground_page_async(self):
        candidate = None
        try:
            p = self._last_created_page
            if p is not None:
                try:
                    _ = p.url
                    candidate = p
                except Exception:
                    candidate = None
        except Exception:
            candidate = None
        if candidate is not None:
            try:
                recent = False
                try:
                    recent = (time.time() - float(self._last_created_page_ts or 0.0)) <= 5.0
                except Exception:
                    recent = False
                if recent or (not self._is_blank_like_url(getattr(candidate, "url", "") or "")):
                    try:
                        await candidate.bring_to_front()
                    except Exception:
                        pass
                    return candidate
            except Exception:
                pass

        page = await self._get_active_page_async()
        if page is not None:
            try:
                await page.bring_to_front()
            except Exception:
                pass
        return page

    @sync_to_async
    def _get_control_signals_async(self):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        return {
            "status": str(execution.status),
            "stop_signal": bool(execution.stop_signal),
            "pause_signal": bool(execution.pause_signal),
        }

    @sync_to_async
    def _get_executor_user_async(self):
        try:
            exe = AutoTestExecution.objects.select_related("executor", "executor__ai_model_config").get(id=self.execution_id)
            return exe.executor
        except Exception:
            return None

    def _chat_completions_url(self, base_url: str) -> str:
        u = (base_url or "").strip().rstrip("/")
        if not u:
            return ""
        if u.endswith("/v1"):
            return u + "/chat/completions"
        return u + "/v1/chat/completions"

    async def _apply_control_signals_async(self, hook_agent: Agent | None, step_number: int | None = None):
        try:
            sig = await self._get_control_signals_async()
        except Exception:
            return

        if sig.get("stop_signal"):
            self.stop_requested = True
            self._stop_reason = "manual_stop"
            try:
                csn = int(self._case_step_last_seen or 0)
            except Exception:
                csn = 0
            if csn > 0:
                try:
                    ok2, summary2, bug_id2 = await self._assert_expected_for_case_step_async(int(csn), int(step_number or self._current_step_number or 0), strict=False)
                except Exception:
                    ok2, summary2, bug_id2 = True, "", None
                if not ok2:
                    try:
                        self._stop_reason = "assert_failed"
                        try:
                            if bug_id2:
                                self._forced_bug_id = int(bug_id2)
                        except Exception:
                            pass
                        try:
                            self._assert_failed_step_number = int(step_number or self._current_step_number or 0)
                            self._assert_failed_summary = str(summary2 or "")[:1800]
                        except Exception:
                            pass
                        try:
                            if step_number is not None and self._forced_bug_id:
                                em = f"发现缺陷：BUG-{int(self._forced_bug_id)}"
                                if summary2:
                                    em = (em + "\n" + str(summary2)).strip()
                                await self._mark_step_failed_async(int(step_number), em)
                        except Exception:
                            pass
                        if step_number is not None:
                            await self._append_step_note_async(int(step_number), f"阻断：预期不满足（用例步骤{int(csn)}），已登记缺陷后停止")
                            if summary2:
                                await self._append_step_note_async(int(step_number), summary2)
                            await self._merge_stop_metrics_async(
                                int(step_number),
                                {"stopped_reason": "assert_failed", "assert_case_step": int(csn), "bug_id": int(bug_id2 or 0)},
                            )
                    except Exception:
                        pass
            try:
                if step_number is not None and self._stop_reason == "manual_stop":
                    await self._append_step_note_async(int(step_number), "收到手动停止信号，终止执行")
                    await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "manual_stop"})
            except Exception:
                pass
            try:
                await self._update_status_async("stopped", {"reason": str(self._stop_reason or "manual_stop")})
            except Exception:
                pass
            try:
                if hook_agent:
                    hook_agent.stop()
                if self._active_agent:
                    self._active_agent.stop()
            except Exception:
                pass
            try:
                self._early_history = getattr(hook_agent or self._active_agent, "history", None)
            except Exception:
                self._early_history = None
            try:
                self._cleanup_browser()
            except Exception:
                pass
            raise _AgentManualStop()

        if sig.get("pause_signal"):
            if self._pause_event and not self._pause_event.is_set():
                return
            if self._pause_event and self._pause_event.is_set():
                self._pause_event.clear()
            try:
                await self._update_status_async("paused", {"reason": "paused"})
            except Exception:
                pass
        else:
            if self._pause_event and not self._pause_event.is_set():
                self._pause_event.set()
            try:
                if sig.get("status") == "paused":
                    await self._update_status_async("running", {"status": "running"})
            except Exception:
                pass

    async def _wait_if_paused_async(self, hook_agent: Agent | None, step_number: int | None = None):
        if not self._pause_event:
            return
        while True:
            try:
                await self._apply_control_signals_async(hook_agent, step_number)
            except _AgentManualStop:
                raise
            if self._pause_event.is_set():
                return
            await asyncio.sleep(0.25)

    async def _signal_poller_loop(self):
        try:
            while True:
                try:
                    await self._apply_control_signals_async(self._active_agent, int(self._current_step_number or 0))
                except _AgentManualStop:
                    return
                except Exception:
                    pass
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return

    def _build_stop_check_sig(self, step_number: int) -> str:
        try:
            msgs = list(self._runtime_messages or [])[-6:]
        except Exception:
            msgs = []
        parts = [
            str(step_number),
            "seen_auth=" + ("1" if self._seen_auth_response else "0"),
            "auth_status=" + str(self._last_auth_status),
            "auth_url=" + str(self._last_auth_url),
            "toast_found=" + ("1" if self._toast_vision_found else "0"),
            "msgs=" + "|".join([self._norm_text(x) for x in msgs if x]),
        ]
        return "#".join(parts)[:1200]

    def _extract_page_numbers(self, text: str) -> set[int]:
        if not text:
            return set()
        try:
            s = str(text)
        except Exception:
            return set()
        out = set()
        for m in re.finditer(r"(?:第\s*)?(\d{1,3})\s*页", s):
            try:
                n = int(m.group(1))
            except Exception:
                continue
            if 1 <= n <= 300:
                out.add(n)
        return out

    def _apply_vars_to_text(self, text: str, vars_obj: dict) -> str:
        if not text:
            return ""
        if not vars_obj or not isinstance(vars_obj, dict):
            return str(text)
        s = str(text)

        def repl(m):
            key = (m.group(1) or "").strip()
            if not key:
                return m.group(0)
            if key not in vars_obj:
                return m.group(0)
            v = vars_obj.get(key)
            if v is None:
                return ""
            return str(v)

        return re.sub(r"\{\{\s*([a-zA-Z_][\w\-]*)\s*\}\}", repl, s)

    def _apply_vars_to_steps(self, steps_list, vars_obj: dict):
        if not vars_obj or not isinstance(vars_obj, dict):
            return steps_list
        out = []
        for s in steps_list or []:
            try:
                out.append(
                    types.SimpleNamespace(
                        step_number=getattr(s, "step_number", 0),
                        description=self._apply_vars_to_text(getattr(s, "description", "") or "", vars_obj),
                        expected_result=self._apply_vars_to_text(getattr(s, "expected_result", "") or "", vars_obj),
                    )
                )
            except Exception:
                continue
        return out

    def _extract_first_url(self, text: str) -> str:
        s = str(text or "")
        if not s:
            return ""
        m = re.search(r"(https?://[^\s'\"<>]+|www\.[^\s'\"<>]+|localhost:\d{2,5}[^\s'\"<>]*)", s, flags=re.I)
        if not m:
            return ""
        u = (m.group(1) or "").strip().rstrip(").,，。；;")
        if not u:
            return ""
        if u.lower().startswith("www."):
            return "http://" + u
        if re.match(r"^localhost:\d{2,5}\b", u, flags=re.I):
            return "http://" + u
        return u

    def _extract_login_from_steps(self, steps_list) -> dict:
        url = ""
        username = ""
        password = ""
        in_login_ctx = False
        ctx_left = 0
        for s in steps_list or []:
            try:
                desc = str(getattr(s, "description", "") or "")
            except Exception:
                desc = ""
            low = desc.lower()
            if not url:
                url = self._extract_first_url(desc)
                try:
                    ul = (url or "").lower()
                    if any(k in ul for k in ["login", "signin", "auth"]):
                        in_login_ctx = True
                        ctx_left = max(ctx_left, 4)
                except Exception:
                    pass
            if not in_login_ctx:
                try:
                    if any(k in low for k in [" login", "login", "signin", "sign in"]) or ("登录" in desc) or ("登陆" in desc) or ("登录页" in desc):
                        in_login_ctx = True
                        ctx_left = max(ctx_left, 4)
                except Exception:
                    pass
            if not in_login_ctx:
                continue
            if not username:
                m = re.search(r"(?:登录账号|登录用户名|用户名|账号|账户|user(?:name)?|login)\s*[:：]?\s*[`'\"“”]?\s*([A-Za-z0-9_.@-]{2,80})", desc, flags=re.I)
                if m:
                    username = (m.group(1) or "").strip()
            if not password:
                m = re.search(r"(?:登录密码|密码|password|pwd)\s*[:：]?\s*[`'\"“”]?\s*([^\s`'\"“”，。；;:：]{2,120})", desc, flags=re.I)
                if m:
                    password = (m.group(1) or "").strip().split("，", 1)[0].split(",", 1)[0].strip()
            if url and username and password:
                break
            if ctx_left > 0:
                ctx_left -= 1
            else:
                in_login_ctx = False
        return {"url": url, "username": username, "password": password}

    def _parse_project_test_accounts(self, text: str) -> dict:
        raw = str(text or "").strip()
        if not raw:
            return {"username": "", "password": ""}
        lead = raw[:1]
        if lead in ("{", "["):
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, list):
                for it in obj[:50]:
                    if not isinstance(it, dict):
                        continue
                    u = str(it.get("username") or it.get("user") or it.get("account") or "").strip()
                    p = str(it.get("password") or it.get("pwd") or "").strip()
                    if u or p:
                        return {"username": u, "password": p}
            elif isinstance(obj, dict):
                u = str(obj.get("username") or obj.get("user") or obj.get("account") or "").strip()
                p = str(obj.get("password") or obj.get("pwd") or "").strip()
                if u or p:
                    return {"username": u, "password": p}
        m = re.search(
            r"(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,80}).*?(?:密码|password|pwd)\s*[:：]?\s*([^\s]{2,120})",
            raw,
            flags=re.I | re.S,
        )
        if m:
            return {"username": (m.group(1) or "").strip(), "password": (m.group(2) or "").strip()}
        m = re.search(
            r"(?:密码|password|pwd)\s*[:：]?\s*([^\s]{2,120}).*?(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,80})",
            raw,
            flags=re.I | re.S,
        )
        if m:
            return {"username": (m.group(2) or "").strip(), "password": (m.group(1) or "").strip()}
        u = ""
        p = ""
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            if not u:
                m = re.search(r"(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,80})", s, flags=re.I)
                if m:
                    u = (m.group(1) or "").strip()
            if not p:
                m = re.search(r"(?:密码|password|pwd)\s*[:：]?\s*([^\s]{2,120})", s, flags=re.I)
                if m:
                    p = (m.group(1) or "").strip()
            if u and p:
                break
            if not (u and p):
                m = re.match(r"^\s*([A-Za-z0-9_.@-]{2,80})\s*[/:\s]\s*([^\s]{2,120})\s*$", s)
                if m:
                    u = u or (m.group(1) or "").strip()
                    p = p or (m.group(2) or "").strip()
            if u and p:
                break
        return {"username": u, "password": p}

    @sync_to_async
    def _get_project_login_defaults_async(self) -> dict:
        try:
            exe = AutoTestExecution.objects.select_related("case__project").get(id=self.execution_id)
        except Exception:
            return {"url": "", "username": "", "password": ""}
        proj = getattr(getattr(exe, "case", None), "project", None)
        base_url = ""
        try:
            base_url = str(getattr(proj, "base_url", "") or "").strip()
        except Exception:
            base_url = ""
        if base_url and not re.match(r"^https?://", base_url, flags=re.I):
            base_url = "http://" + base_url
        base_url = self._sanitize_goto_url(base_url)
        accounts = ""
        try:
            accounts = str(getattr(proj, "test_accounts", "") or "").strip()
        except Exception:
            accounts = ""
        parsed = self._parse_project_test_accounts(accounts)
        return {"url": base_url, "username": parsed.get("username") or "", "password": parsed.get("password") or ""}

    def _infer_required_max_page(self) -> int | None:
        steps = self._testcase_steps or []
        if not steps:
            return None
        texts = []
        for s in steps:
            try:
                texts.append(str(getattr(s, "description", "") or ""))
            except Exception:
                pass
            try:
                texts.append(str(getattr(s, "expected_result", "") or ""))
            except Exception:
                pass
        blob = "\n".join([t for t in texts if t])
        if not blob:
            return None
        lower = blob.lower()
        wants_full = self._wants_full_pagination()
        max_page = None
        for m in re.finditer(r"(?:第\s*)?1\s*[-~到至]\s*(\d{1,3})\s*页", blob):
            try:
                max_page = max(int(m.group(1)), max_page or 0)
            except Exception:
                continue
        for m in re.finditer(r"(?:共|总)\s*(\d{1,3})\s*页", blob):
            try:
                max_page = max(int(m.group(1)), max_page or 0)
            except Exception:
                continue
        if wants_full and max_page and 2 <= max_page <= 300:
            return int(max_page)
        return None

    def _wants_full_pagination(self) -> bool:
        steps = self._testcase_steps or []
        if not steps:
            return False
        texts = []
        for s in steps:
            try:
                texts.append(str(getattr(s, "description", "") or ""))
            except Exception:
                pass
            try:
                texts.append(str(getattr(s, "expected_result", "") or ""))
            except Exception:
                pass
        blob = "\n".join([t for t in texts if t])
        if not blob:
            return False
        lower = blob.lower()
        return any(k in blob for k in ["全部", "所有", "逐页", "遍历", "每页", "每一页", "翻页", "页码", "一页页"]) or any(
            k in lower for k in ["pagination", "page ", "pages"]
        )

    def _wants_full_slides(self) -> bool:
        cached = getattr(self, "_wants_full_slides_cached", None)
        if cached is not None:
            return bool(cached)
        steps = self._testcase_steps or []
        if not steps:
            self._wants_full_slides_cached = False
            return False
        texts = []
        for s in steps:
            try:
                texts.append(str(getattr(s, "description", "") or ""))
            except Exception:
                pass
            try:
                texts.append(str(getattr(s, "expected_result", "") or ""))
            except Exception:
                pass
        blob = "\n".join([t for t in texts if t])
        if not blob:
            self._wants_full_slides_cached = False
            return False
        lower = blob.lower()
        keys = ["所有幻灯片", "全部幻灯片", "每张幻灯片", "每一张幻灯片", "逐张", "逐一检查幻灯片", "遍历幻灯片"]
        en_keys = ["all slides", "each slide", "every slide", "slide by slide"]
        out = any(k in blob for k in keys) or any(k in lower for k in en_keys)
        self._wants_full_slides_cached = bool(out)
        return bool(out)

    def _case_step_requires_full_slides(self, step_no: int) -> bool:
        try:
            n = int(step_no or 0)
        except Exception:
            return False
        if n <= 0:
            return False
        target = None
        for s in (self._testcase_steps or []):
            try:
                if int(getattr(s, "step_number", 0) or 0) == n:
                    target = s
                    break
            except Exception:
                continue
        if not target:
            return False
        try:
            blob = f"{str(getattr(target, 'description', '') or '')}\n{str(getattr(target, 'expected_result', '') or '')}"
        except Exception:
            blob = ""
        if not blob:
            return False
        lower = blob.lower()
        keys = ["所有幻灯片", "全部幻灯片", "每张幻灯片", "每一张幻灯片", "逐张", "逐一", "遍历幻灯片", "检查所有幻灯片", "检查每张幻灯片", "幻灯片样式"]
        en_keys = ["all slides", "each slide", "every slide", "slide by slide"]
        return any(k in blob for k in keys) or any(k in lower for k in en_keys)

    def _find_case_step_by_number(self, step_no: int):
        try:
            n = int(step_no or 0)
        except Exception:
            return None
        if n <= 0:
            return None
        for s in (self._testcase_steps or []):
            try:
                if int(getattr(s, "step_number", 0) or 0) == n:
                    return s
            except Exception:
                continue
        return None

    def _case_step_requires_upload_file(self, step_no: int) -> bool:
        s = self._find_case_step_by_number(step_no)
        if not s:
            return False
        try:
            if bool(str(getattr(s, "transfer_file_base64", None) or "").strip()):
                return True
        except Exception:
            pass
        return False

    def _get_next_pending_transfer_file_step_no(self) -> int:
        best = 0
        for s in (self._testcase_steps or []):
            try:
                sn = int(getattr(s, "step_number", 0) or 0)
            except Exception:
                continue
            if sn <= 0:
                continue
            try:
                has_b64 = bool(str(getattr(s, "transfer_file_base64", None) or "").strip())
            except Exception:
                has_b64 = False
            if not has_b64:
                continue
            try:
                if sn in set(self._transfer_file_applied_steps or set()):
                    continue
            except Exception:
                pass
            if (best == 0) or (sn < best):
                best = sn
        return int(best)

    def _get_transfer_file_payload(self, step) -> dict | None:
        if not step:
            return None
        b64 = getattr(step, "transfer_file_base64", None)
        name = str(getattr(step, "transfer_file_name", "") or "").strip()[:255]
        ctype = str(getattr(step, "transfer_file_content_type", "") or "").strip()[:120]
        if not b64:
            return None
        try:
            raw = base64.b64decode(str(b64))
        except Exception:
            return None
        if not raw:
            return None
        if not name:
            name = "upload.bin"
        if not ctype:
            ctype = "application/octet-stream"
        return {"name": name, "mimeType": ctype, "buffer": raw}

    def _ensure_transfer_file_disk_path(self, step_no: int, step) -> str | None:
        try:
            n = int(step_no or 0)
        except Exception:
            n = 0
        if n <= 0 or not step:
            return None
        try:
            existing = (self._transfer_file_disk_paths or {}).get(int(n))
            if existing and os.path.exists(str(existing)):
                return str(existing)
        except Exception:
            pass

        payload = self._get_transfer_file_payload(step)
        if not payload:
            return None
        raw = payload.get("buffer") or b""
        if not raw:
            return None
        name = str(payload.get("name") or "upload.bin").strip()
        name = os.path.basename(name).replace("\\", "_").replace("/", "_").strip()[:120] or "upload.bin"
        try:
            base_dir = os.path.join(tempfile.gettempdir(), "qa_ai_transfer_files", f"execution_{int(self.execution_id)}")
        except Exception:
            base_dir = os.path.join(tempfile.gettempdir(), "qa_ai_transfer_files", "execution_unknown")
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            return None

        path = os.path.join(base_dir, f"{int(n)}_{name}")
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except Exception:
            return None
        try:
            self._transfer_file_disk_paths[int(n)] = str(path)
        except Exception:
            self._transfer_file_disk_paths = {int(n): str(path)}
        try:
            cur = list(self._agent_available_file_paths or [])
            if str(path) not in cur:
                cur.append(str(path))
            self._agent_available_file_paths = cur
        except Exception:
            self._agent_available_file_paths = [str(path)]
        return str(path)

    def _humanize_action_script(self, action_script: str) -> str:
        t = str(action_script or "").strip()
        if not t:
            return ""
        lower = t.lower()
        try:
            if lower == "timeout":
                return "执行超时"
            if "go_to_url" in lower or lower.startswith("goto") or "goto(" in lower:
                m = re.search(r"(?:url\s*=\s*|goto\()\s*['\"]([^'\"]+)['\"]", t, flags=re.IGNORECASE)
                u = (m.group(1) if m else "").strip()
                if u:
                    return f"打开链接：{u[:160]}"
                return "打开链接"
            if "open_tab" in lower:
                return "打开新标签页"
            if "switch_tab" in lower:
                m = re.search(r"(?:tab|index)\s*=\s*(\d+)", t, flags=re.IGNORECASE)
                return f"切换标签页：{m.group(1)}" if m else "切换标签页"
            if "click_element" in lower or lower.startswith("click"):
                m = re.search(r"index\s*=\s*(\d+)", t, flags=re.IGNORECASE)
                return f"点击页面元素（#{m.group(1)}）" if m else "点击页面元素"
            if "input_text" in lower:
                idx = re.search(r"index\s*=\s*(\d+)", t, flags=re.IGNORECASE)
                txt = re.search(r"(?:text|value)\s*=\s*['\"]([^'\"]*)['\"]", t, flags=re.IGNORECASE)
                v = (txt.group(1) if txt else "")
                v = (v[:30] + "…" if len(v) > 30 else v)
                if idx and v:
                    return f"输入文本到元素（#{idx.group(1)}）：{v}"
                if idx:
                    return f"输入文本到元素（#{idx.group(1)}）"
                return "输入文本"
            if "send_keys" in lower:
                return "键盘输入/快捷键"
            if "scroll" in lower:
                return "滚动页面"
            if "extract" in lower:
                return "提取页面内容"
            if "done" in lower:
                return "结束执行"
        except Exception:
            return ""
        return "AI 操作"

    def _seen_pagination_end_signal(self) -> bool:
        texts = []
        try:
            texts.extend(list(self._runtime_messages or [])[-12:])
        except Exception:
            pass
        blob = "；".join([str(x) for x in texts if x])[:2000]
        if not blob:
            return False
        lower = blob.lower()
        keys = ["最后一页", "末页", "已经到底", "没有更多", "无更多", "end of", "no more"]
        return any(k in blob for k in keys) or any(k in lower for k in ["no more", "end of"])

    def _seen_slides_end_signal(self) -> bool:
        texts = []
        try:
            texts.extend(list(self._runtime_messages or [])[-12:])
        except Exception:
            pass
        blob = "；".join([str(x) for x in texts if x])[:2000]
        if not blob:
            return False
        lower = blob.lower()
        keys = ["最后一张", "末张", "已到最后一张", "已经是最后一张", "no more slide", "last slide"]
        return any(k in blob for k in keys) or any(k in lower for k in ["last slide", "no more slide"])

    def _qwen_api_key_available(self) -> bool:
        try:
            api_key = (
                os.getenv("DASHSCOPE_API_KEY")
                or os.getenv("AI_QWEN_API_KEY")
                or getattr(settings, "DASHSCOPE_API_KEY", "")
                or getattr(settings, "AI_QWEN_API_KEY", "")
            )
            return bool(api_key)
        except Exception:
            return False

    @sync_to_async
    def _merge_step_metrics_async(self, step_number: int, patch: dict):
        try:
            execution = AutoTestExecution.objects.get(id=self.execution_id)
            step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
            if not step_record:
                return
            base = step_record.metrics or {}
            if not isinstance(base, dict):
                base = {}
            if not isinstance(patch, dict):
                patch = {}
            base.update(patch)
            step_record.metrics = base
            step_record.save(update_fields=["metrics"])
        except Exception:
            return

    async def _merge_stop_metrics_async(self, step_number: int, patch: dict):
        try:
            p = patch if isinstance(patch, dict) else {}
            out = dict(p)
            out.update(self._build_stop_evidence_patch())
            await self._merge_step_metrics_async(int(step_number), out)
        except Exception:
            return

    async def _maybe_ai_stop_from_async_trigger(self, step_number: int, trigger: str):
        if not self._active_agent or self._stopped_by_ai:
            return
        if not self._llm:
            return
        if self._stop_check_running:
            return
        try:
            if not self._stop_policy.should_run_stop_check(int(step_number)):
                return
        except Exception:
            return
        try:
            sig = (self._build_stop_check_sig(int(step_number)) + "#" + self._norm_text(trigger or ""))[:1400]
            now_ts = time.time()
            if sig == self._last_stop_check_sig:
                return
            if (now_ts - float(self._last_stop_check_ts or 0.0)) < 1.0:
                return
            self._stop_check_running = True
            self._last_stop_check_sig = sig
            self._last_stop_check_ts = now_ts
            stop_now, blocking, reason = await self._ai_should_stop_now_async(self._active_agent, int(step_number))
            if stop_now and blocking:
                req_max = self._infer_required_max_page()
                wants_full = self._wants_full_pagination()
                if wants_full and not req_max and len(self._covered_pages) < 2:
                    try:
                        await self._append_step_note_async(
                            int(step_number),
                            "AI拟停止但检测到需遍历全部页码：当前仅覆盖到第1页附近，继续执行",
                        )
                    except Exception:
                        pass
                    return
                if wants_full and not req_max and not self._seen_pagination_end_signal():
                    try:
                        await self._append_step_note_async(
                            int(step_number),
                            f"AI拟停止但检测到需遍历全部页码：已覆盖页={sorted(list(self._covered_pages))[:6]}... 未确认到末页，继续执行",
                        )
                    except Exception:
                        pass
                    return
                if req_max and len(self._covered_pages) < int(req_max):
                    try:
                        await self._append_step_note_async(
                            int(step_number),
                            f"AI拟停止但检测到需遍历全部页码：已覆盖 {len(self._covered_pages)}/{int(req_max)} 页，继续执行",
                        )
                    except Exception:
                        pass
                    return
                wants_slides = self._wants_full_slides()
                if wants_slides:
                    try:
                        page = await self._get_active_page_async()
                        ss = await self._detect_slide_sidebar_state_async(page)
                        cnt = ss.get("count") if isinstance(ss, dict) else None
                        if cnt and int(cnt) > 0:
                            self._required_slide_total = int(cnt)
                        idx = ss.get("index") if isinstance(ss, dict) else None
                        if idx and int(idx) > 0:
                            self._covered_slides.add(int(idx))
                    except Exception:
                        pass
                    req_slide_total = int(self._required_slide_total or 0)
                    covered_cnt = int(len(self._covered_slides or []))
                    if req_slide_total and covered_cnt < req_slide_total:
                        try:
                            await self._append_step_note_async(
                                int(step_number),
                                f"AI拟停止但检测到需遍历全部幻灯片：已覆盖 {covered_cnt}/{req_slide_total} 张，继续执行",
                            )
                        except Exception:
                            pass
                        return
                    if (not req_slide_total) and covered_cnt < 2:
                        try:
                            await self._append_step_note_async(
                                int(step_number),
                                "AI拟停止但检测到需遍历全部幻灯片：当前覆盖过少，继续执行",
                            )
                        except Exception:
                            pass
                        return
                    if (not req_slide_total) and (not self._seen_slides_end_signal()):
                        try:
                            await self._append_step_note_async(
                                int(step_number),
                                f"AI拟停止但检测到需遍历全部幻灯片：已覆盖={sorted(list(self._covered_slides))[:10]}... 未确认到末张，继续执行",
                            )
                        except Exception:
                            pass
                        return
                if not self._stop_reason:
                    self._stop_reason = "blocking_bug"
                try:
                    await self._merge_stop_metrics_async(
                        int(step_number),
                        {"stopped_reason": str(self._stop_reason), "blocking": True, "blocking_reason": str(reason or "")[:300]},
                    )
                except Exception:
                    pass
                self._stopped_by_ai = True
                try:
                    await self._append_step_note_async(int(step_number), "AI判断已获得足够证据，结束执行")
                    if reason:
                        await self._append_step_note_async(int(step_number), reason)
                except Exception:
                    pass
                try:
                    self._early_history = getattr(self._active_agent, "history", None)
                except Exception:
                    self._early_history = None
                try:
                    self._active_agent.stop()
                except Exception:
                    pass
        except Exception:
            return
        finally:
            self._stop_check_running = False

    async def _scroll_eval_state_async(self, page):
        js = """
(() => {
  const getDoc = () => document.scrollingElement || document.documentElement;
  const doc = getDoc();
  const docInfo = {
    top: Number(doc && doc.scrollTop ? doc.scrollTop : 0),
    height: Number(doc && doc.scrollHeight ? doc.scrollHeight : 0),
    client: Number(doc && doc.clientHeight ? doc.clientHeight : 0),
  };
  const overflowHidden = (v) => v === 'hidden' || v === 'clip';
  const findScrollable = () => {
    const x = Math.max(0, Math.floor(window.innerWidth / 2));
    const y = Math.max(0, Math.floor(window.innerHeight / 2));
    let el = document.elementFromPoint(x, y);
    const seen = new Set();
    while (el && el !== document.body && !seen.has(el)) {
      seen.add(el);
      const st = window.getComputedStyle(el);
      const oy = (st && st.overflowY) ? String(st.overflowY) : '';
      const can = (oy === 'auto' || oy === 'scroll') && (el.scrollHeight > el.clientHeight + 4);
      if (can) return el;
      el = el.parentElement;
    }
    return null;
  };
  const isVisible = (el) => {
    try {
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 40 || r.height < 40) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const findBestScrollable = () => {
    try {
      const nodes = document.querySelectorAll('body *');
      const limit = Math.min(nodes.length, 1400);
      let best = null;
      let bestScore = 0;
      for (let i = 0; i < limit; i++) {
        const el = nodes[i];
        if (!el) continue;
        const st = window.getComputedStyle(el);
        const oy = st && st.overflowY ? String(st.overflowY) : '';
        if (oy !== 'auto' && oy !== 'scroll') continue;
        if (!isVisible(el)) continue;
        const diff = (el.scrollHeight || 0) - (el.clientHeight || 0);
        if (diff <= 8) continue;
        if (diff > bestScore) {
          best = el;
          bestScore = diff;
        }
      }
      return best;
    } catch (e) {
      return null;
    }
  };
  const cont = findScrollable() || findBestScrollable();
  const contInfo = cont ? {
    top: Number(cont.scrollTop || 0),
    height: Number(cont.scrollHeight || 0),
    client: Number(cont.clientHeight || 0),
  } : null;
  const docScrollable = docInfo.height > docInfo.client + 4;
  const bodyOy = document.body ? String(getComputedStyle(document.body).overflowY || '') : '';
  const htmlOy = document.documentElement ? String(getComputedStyle(document.documentElement).overflowY || '') : '';
  const docAllowed = docScrollable && !(overflowHidden(bodyOy) && overflowHidden(htmlOy));
  const target = docAllowed ? 'doc' : (cont ? 'container' : 'none');
  return { doc: docInfo, container: contInfo, target };
})()
"""
        try:
            out = await page.evaluate(js)
            if isinstance(out, dict):
                return out
        except Exception:
            return {}
        return {}

    async def _scroll_eval_by_async(self, page, delta_y: float):
        js = """
(deltaY) => {
  const getDoc = () => document.scrollingElement || document.documentElement;
  const doc = getDoc();
  const findScrollable = () => {
    const x = Math.max(0, Math.floor(window.innerWidth / 2));
    const y = Math.max(0, Math.floor(window.innerHeight / 2));
    let el = document.elementFromPoint(x, y);
    const seen = new Set();
    while (el && el !== document.body && !seen.has(el)) {
      seen.add(el);
      const st = window.getComputedStyle(el);
      const oy = (st && st.overflowY) ? String(st.overflowY) : '';
      const can = (oy === 'auto' || oy === 'scroll') && (el.scrollHeight > el.clientHeight + 4);
      if (can) return el;
      el = el.parentElement;
    }
    return null;
  };
  const isVisible = (el) => {
    try {
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 40 || r.height < 40) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const findBestScrollable = () => {
    try {
      const nodes = document.querySelectorAll('body *');
      const limit = Math.min(nodes.length, 1400);
      let best = null;
      let bestScore = 0;
      for (let i = 0; i < limit; i++) {
        const el = nodes[i];
        if (!el) continue;
        const st = window.getComputedStyle(el);
        const oy = st && st.overflowY ? String(st.overflowY) : '';
        if (oy !== 'auto' && oy !== 'scroll') continue;
        if (!isVisible(el)) continue;
        const diff = (el.scrollHeight || 0) - (el.clientHeight || 0);
        if (diff <= 8) continue;
        if (diff > bestScore) {
          best = el;
          bestScore = diff;
        }
      }
      return best;
    } catch (e) {
      return null;
    }
  };
  const cont = findScrollable() || findBestScrollable();
  const docScrollable = doc && (doc.scrollHeight > doc.clientHeight + 4);
  const dy = Number(deltaY || 0);
  if (docScrollable && doc) {
    const before = Number(doc.scrollTop || 0);
    try { doc.scrollBy(0, dy); } catch (e) {}
    const after = Number(doc.scrollTop || 0);
    if (after !== before) {
      return { ok: true, target: 'doc' };
    }
  }
  if (cont) {
    const before = Number(cont.scrollTop || 0);
    try { cont.scrollBy(0, dy); } catch (e) {}
    const after = Number(cont.scrollTop || 0);
    if (after !== before) {
      return { ok: true, target: 'container' };
    }
  }
  return { ok: false, target: cont ? 'container' : (docScrollable ? 'doc' : 'none') };
  try {
    return { ok: false, target: cont ? 'container' : 'none' };
  } catch (e) {
    try {
      return { ok: false, target: cont ? 'container' : 'none' };
    } catch (e2) {
      return { ok: false, target: cont ? 'container' : 'none' };
    }
  }
}
"""
        try:
            out = await page.evaluate(js, float(delta_y))
            if isinstance(out, dict):
                return out
        except Exception:
            return {"ok": False, "target": "none"}
        return {"ok": False, "target": "none"}

    async def _detect_page_state_async(self, page):
        js = """
(() => {
  const isVisible = (el) => {
    try {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };

  const pickText = (el) => {
    try { return String((el.innerText || el.textContent || '')).trim(); } catch (e) { return ''; }
  };

  const toInt = (s) => {
    const m = String(s || '').match(/\\d+/);
    return m ? Number(m[0]) : null;
  };

  const findActivePageNum = () => {
    const el = document.querySelector('[aria-current=\"page\"]');
    if (el && isVisible(el)) return toInt(pickText(el));
    const ant = document.querySelector('.ant-pagination-item-active');
    if (ant && isVisible(ant)) return toInt(pickText(ant));
    const elp = document.querySelector('.el-pager .active');
    if (elp && isVisible(elp)) return toInt(pickText(elp));
    const act = document.querySelector('.pagination .active');
    if (act && isVisible(act)) return toInt(pickText(act));
    return null;
  };

  const findTotalByMaxPageItem = () => {
    const nodes = Array.from(document.querySelectorAll('a,button,li,span')).filter(isVisible);
    let max = null;
    for (const n of nodes) {
      const t = pickText(n);
      if (!t) continue;
      if (t.length > 6) continue;
      const v = toInt(t);
      if (!v || v < 1 || v > 500) continue;
      if (max === null || v > max) max = v;
    }
    return max;
  };

  const findNextButton = () => {
    const cands = Array.from(document.querySelectorAll('button,a,[role=button]')).filter(isVisible);
    const score = (el) => {
      const t = pickText(el).toLowerCase();
      const al = String(el.getAttribute('aria-label') || '').toLowerCase();
      const ti = String(el.getAttribute('title') || '').toLowerCase();
      const s = [t, al, ti].join(' ');
      if (!s) return 0;
      if (s.includes('下一页') || s.includes('next') || s.includes('›') || s.includes('»') || s.includes('下页')) return 10;
      if (s.includes('pager') && s.includes('next')) return 8;
      return 0;
    };
    let best = null;
    let bestS = 0;
    for (const el of cands) {
      const sc = score(el);
      if (sc > bestS) { best = el; bestS = sc; }
    }
    return best;
  };

  const isDisabled = (el) => {
    try {
      if (!el) return false;
      const aria = String(el.getAttribute('aria-disabled') || '');
      if (aria === 'true') return true;
      if (el.hasAttribute('disabled')) return true;
      const cls = String(el.className || '').toLowerCase();
      if (cls.includes('disabled')) return true;
      return false;
    } catch (e) { return false; }
  };

  const current = findActivePageNum();
  const total = findTotalByMaxPageItem();
  const nextBtn = findNextButton();
  const nextDisabled = nextBtn ? isDisabled(nextBtn) : null;
  const mode = current !== null ? 'pagination' : 'unknown';
  return { mode, current, total, nextDisabled };
})()
"""
        try:
            out = await page.evaluate(js)
            if isinstance(out, dict):
                return out
        except Exception:
            return {}

    async def _detect_loading_indicators_async(self, page) -> dict:
        if not page:
            return {}
        js = r"""
        () => {
          const isVisible = (el) => {
            try {
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
              const r = el.getBoundingClientRect();
              if (!r || r.width < 6 || r.height < 6) return false;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
              return true;
            } catch(e) { return false; }
          };
          const pickText = (el) => {
            try { return String((el.innerText || el.textContent || '')).replace(/\s+/g,' ').trim(); } catch(e) { return ''; }
          };
          const kws = [
            'loading','generating','processing','please wait','progress',
            '加载','加载中','正在加载','处理中','生成中','正在生成','请稍候','请稍等','进度'
          ];
          const nodes = Array.from(document.querySelectorAll('div,span,p,button,a,[role="status"],[role="alert"],[role="progressbar"]')).filter(isVisible);
          const hits = [];
          for (const n of nodes) {
            const t = pickText(n);
            if (!t) continue;
            const low = t.toLowerCase();
            if (kws.some(k => low.includes(k))) {
              hits.push(t.slice(0, 80));
              if (hits.length >= 6) break;
            }
            if (/%\s*$/.test(t) && /\d{1,3}\s*%/.test(t)) {
              hits.push(t.slice(0, 80));
              if (hits.length >= 6) break;
            }
          }
          const ariaBusy = !!document.querySelector('[aria-busy="true"]');
          const roleProgress = !!document.querySelector('[role="progressbar"]');
          const clsSpinner = !!document.querySelector('.spinner,.loading,.ant-spin,.el-loading-mask,[data-loading="true"]');
          return { hits, ariaBusy, roleProgress, clsSpinner };
        }
        """
        try:
            out = await page.evaluate(js)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    async def _detect_basic_interactivity_async(self, page) -> dict:
        if not page:
            return {}
        js = r"""
        () => {
          const isVisible = (el) => {
            try {
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
              const r = el.getBoundingClientRect();
              if (!r || r.width < 10 || r.height < 10) return false;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
              return true;
            } catch(e) { return false; }
          };
          const isDisabled = (el) => {
            try {
              if (!el) return true;
              if (el.hasAttribute('disabled')) return true;
              const aria = String(el.getAttribute('aria-disabled') || '');
              if (aria === 'true') return true;
              const st = window.getComputedStyle(el);
              if (st && st.pointerEvents === 'none') return true;
              const cls = String(el.className || '').toLowerCase();
              if (cls.includes('disabled') || cls.includes('is-disabled')) return true;
              return false;
            } catch(e) { return true; }
          };
          const cands = Array.from(document.querySelectorAll('button,a,[role="button"],input,textarea,select')).filter(isVisible);
          let enabled = 0;
          for (const el of cands) {
            if (!isDisabled(el)) { enabled++; if (enabled >= 3) break; }
          }
          return { enabled_controls: enabled, ready_state: String(document.readyState || '') };
        }
        """
        try:
            out = await page.evaluate(js)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}
        return {}

    async def _click_next_page_async(self, step_number: int, max_tries: int = 3):
        page = await self._get_active_page_async()
        if not page:
            return {"ok": False, "reason": "no_page"}
        before = await self._detect_page_state_async(page)
        before_cur = before.get("current") if isinstance(before, dict) else None
        for attempt in range(max(1, int(max_tries))):
            js_click = """
(() => {
  const isVisible = (el) => {
    try {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const pickText = (el) => {
    try { return String((el.innerText || el.textContent || '')).trim(); } catch (e) { return ''; }
  };
  const isDisabled = (el) => {
    try {
      if (!el) return false;
      const aria = String(el.getAttribute('aria-disabled') || '');
      if (aria === 'true') return true;
      if (el.hasAttribute('disabled')) return true;
      const cls = String(el.className || '').toLowerCase();
      if (cls.includes('disabled')) return true;
      return false;
    } catch (e) { return false; }
  };
  const cands = Array.from(document.querySelectorAll('button,a,[role=button]')).filter(isVisible);
  const score = (el) => {
    const t = pickText(el).toLowerCase();
    const al = String(el.getAttribute('aria-label') || '').toLowerCase();
    const ti = String(el.getAttribute('title') || '').toLowerCase();
    const s = [t, al, ti].join(' ');
    if (!s) return 0;
    if (s.includes('下一页') || s.includes('next') || s.includes('›') || s.includes('»') || s.includes('下页')) return 10;
    if (s.includes('pager') && s.includes('next')) return 8;
    return 0;
  };
  let best = null;
  let bestS = 0;
  for (const el of cands) {
    const sc = score(el);
    if (sc > bestS) { best = el; bestS = sc; }
  }
  if (!best) return { clicked: false, reason: 'not_found' };
  if (isDisabled(best)) return { clicked: false, reason: 'disabled' };
  const label = (String(best.getAttribute('aria-label') || best.getAttribute('title') || pickText(best) || '')).slice(0, 60);
  try { best.click(); } catch (e) { return { clicked: false, reason: 'click_error' }; }
  return { clicked: true, label };
})()
"""
            click_out = {}
            try:
                click_out = await page.evaluate(js_click)
            except Exception:
                click_out = {"clicked": False, "reason": "evaluate_failed"}
            if not (isinstance(click_out, dict) and click_out.get("clicked")):
                await self._smart_scroll_async(step_number, until_bottom=(attempt >= 1), prefer_container=True)
                continue
            changed = False
            after = None
            for _ in range(16):
                await page.wait_for_timeout(250)
                after = await self._detect_page_state_async(page)
                after_cur = after.get("current") if isinstance(after, dict) else None
                if before_cur is not None and after_cur is not None and int(after_cur) != int(before_cur):
                    changed = True
                    break
            if changed:
                return {"ok": True, "before": before, "after": after, "click": click_out, "attempt": attempt + 1}
            await self._smart_scroll_async(step_number, until_bottom=(attempt >= 1), prefer_container=True)
        after2 = await self._detect_page_state_async(page)
        return {"ok": False, "before": before, "after": after2, "reason": "no_change"}

    async def _goto_page_async(self, step_number: int, target_page: int, max_tries: int = 3):
        page = await self._get_active_page_async()
        if not page:
            return {"ok": False, "reason": "no_page"}
        try:
            target = int(target_page)
        except Exception:
            return {"ok": False, "reason": "bad_target"}
        if target < 1 or target > 500:
            return {"ok": False, "reason": "bad_target"}
        before = await self._detect_page_state_async(page)
        for attempt in range(max(1, int(max_tries))):
            js_click = """
(tp) => {
  const target = Number(tp || 0);
  const isVisible = (el) => {
    try {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
      const r = el.getBoundingClientRect();
      if (!r || r.width < 6 || r.height < 6) return false;
      if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
      return true;
    } catch (e) { return false; }
  };
  const pickText = (el) => {
    try { return String((el.innerText || el.textContent || '')).trim(); } catch (e) { return ''; }
  };
  const cands = Array.from(document.querySelectorAll('button,a,[role=button],li,span')).filter(isVisible);
  const eq = (t) => String(t || '').replace(/\\s+/g,'').toLowerCase();
  const t1 = String(target);
  const t2 = '第' + t1 + '页';
  for (const el of cands) {
    const txt = pickText(el);
    if (!txt) continue;
    const e = eq(txt);
    if (e === eq(t1) || e === eq(t2)) {
      try { el.click(); return { clicked: true, text: txt.slice(0, 30) }; } catch (e2) { return { clicked: false, reason: 'click_error' }; }
    }
    const al = eq(el.getAttribute('aria-label') || '');
    const ti = eq(el.getAttribute('title') || '');
    if (al.includes(eq(t2)) || al.includes('page' + t1) || ti.includes(eq(t2)) || ti.includes('page' + t1)) {
      try { el.click(); return { clicked: true, text: (el.getAttribute('aria-label')||el.getAttribute('title')||txt||'').slice(0, 30) }; } catch (e3) { return { clicked: false, reason: 'click_error' }; }
    }
  }
  return { clicked: false, reason: 'not_found' };
}
"""
            click_out = {}
            try:
                click_out = await page.evaluate(js_click, int(target))
            except Exception:
                click_out = {"clicked": False, "reason": "evaluate_failed"}
            if not (isinstance(click_out, dict) and click_out.get("clicked")):
                await self._smart_scroll_async(step_number, until_bottom=(attempt >= 1), prefer_container=True)
                continue
            ok = False
            after = None
            for _ in range(16):
                await page.wait_for_timeout(250)
                after = await self._detect_page_state_async(page)
                cur = after.get("current") if isinstance(after, dict) else None
                if cur is not None and int(cur) == int(target):
                    ok = True
                    break
            if ok:
                return {"ok": True, "before": before, "after": after, "click": click_out, "attempt": attempt + 1}
            await self._smart_scroll_async(step_number, until_bottom=(attempt >= 1), prefer_container=True)
        after2 = await self._detect_page_state_async(page)
        return {"ok": False, "before": before, "after": after2, "reason": "no_change"}

    async def _smart_scroll_async(self, step_number: int, until_bottom: bool = True, prefer_container: bool = False):
        page = await self._get_active_page_async()
        if not page:
            return
        max_rounds = int(getattr(settings, "AI_EXEC_SCROLL_MAX_ROUNDS", 14) or 14)
        settle_rounds = int(getattr(settings, "AI_EXEC_SCROLL_SETTLE_ROUNDS", 2) or 2)
        per_wait_ms = int(getattr(settings, "AI_EXEC_SCROLL_WAIT_MS", 220) or 220)

        before = await self._scroll_eval_state_async(page)
        target_kind = (before.get("target") if isinstance(before, dict) else "") or "none"
        doc = (before.get("doc") if isinstance(before, dict) else {}) or {}
        cont = (before.get("container") if isinstance(before, dict) else {}) or {}
        try:
            if prefer_container and isinstance(cont, dict):
                if int(cont.get("height") or 0) > int(cont.get("client") or 0) + 4:
                    target_kind = "container"
        except Exception:
            pass
        client = None
        try:
            if target_kind == "container":
                client = int(cont.get("client") or 0)
            else:
                client = int(doc.get("client") or 0)
        except Exception:
            client = 0
        if not client:
            client = 800
        delta = int(max(200, client * 0.90))

        stable = 0
        last_h = None
        rounds = 0
        while rounds < max_rounds:
            rounds += 1
            try:
                r = await self._scroll_eval_by_async(page, delta)
                if isinstance(r, dict) and r.get("target"):
                    target_kind = str(r.get("target") or target_kind)
            except Exception:
                pass
            try:
                await page.wait_for_timeout(int(per_wait_ms))
            except Exception:
                pass
            cur = await self._scroll_eval_state_async(page)
            doc2 = (cur.get("doc") if isinstance(cur, dict) else {}) or {}
            cont2 = (cur.get("container") if isinstance(cur, dict) else {}) or {}
            if target_kind == "container":
                top = int(cont2.get("top") or 0)
                height = int(cont2.get("height") or 0)
                client2 = int(cont2.get("client") or 0)
            else:
                top = int(doc2.get("top") or 0)
                height = int(doc2.get("height") or 0)
                client2 = int(doc2.get("client") or 0)
            if client2 <= 0:
                client2 = client
            at_bottom = (top + client2) >= max(0, height - 3)
            if not until_bottom:
                break
            if at_bottom:
                if last_h is None or height != last_h:
                    stable = 0
                    last_h = height
                else:
                    stable += 1
                if stable >= settle_rounds:
                    break
            else:
                stable = 0
                last_h = height

        after = await self._scroll_eval_state_async(page)
        try:
            await self._merge_step_metrics_async(
                int(step_number),
                {
                    "scroll_target": target_kind,
                    "scroll_rounds": int(rounds),
                    "scroll_before": before,
                    "scroll_after": after,
                },
            )
        except Exception:
            pass

    def _build_expected_match_hints(self) -> dict:
        steps = self._testcase_steps or []
        if not steps:
            return {"matched": [], "messages_tail": []}
        try:
            messages = list(self._runtime_messages or [])[-30:]
        except Exception:
            messages = []
        norm_messages = [self._norm_text(m) for m in messages if m]
        matched = []
        seen = set()
        for s in steps:
            expected_raw = getattr(s, "expected_result", "") or ""
            phrases = self._extract_expect_phrases(expected_raw)
            for ph in phrases:
                for a in self._expand_phrase_aliases(ph):
                    na = self._norm_text(a)
                    if not na:
                        continue
                    if any(na in nm for nm in norm_messages):
                        key = self._norm_text(ph)
                        if key and key not in seen:
                            seen.add(key)
                            matched.append(ph[:120])
                        break
            if len(matched) >= 6:
                break
        return {"matched": matched[:6], "messages_tail": messages[-6:]}

    async def _enhance_interactive_elements_async(self, agent: Agent):
        try:
            if not agent or not hasattr(agent, "browser_context"):
                return
            page = await agent.browser_context.get_current_page()
            js = r"""
            (function() {
                try {
                    // 1. Enhance empty buttons/links with ID/Class as aria-label
                    const interactives = document.querySelectorAll('button, a, [role="button"], [role="link"], input, textarea, select, [contenteditable="true"]');
                    interactives.forEach(el => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5 || getComputedStyle(el).visibility === 'hidden') return;
                        
                        const tagName = el.tagName.toLowerCase();
                        if (!el.getAttribute('data-ai-original-tag')) {
                            el.setAttribute('data-ai-original-tag', tagName);
                        }

                        let text = el.innerText || el.textContent || el.placeholder || el.value;
                        if (tagName === 'select') {
                             // For select, use the selected option text or first option
                             if (el.options && el.options.length > 0) {
                                 text = el.options[el.selectedIndex]?.text || el.options[0].text;
                             }
                        }

                        if (!text || text.trim().length === 0) {
                            const label = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('name');
                            if (!label) {
                                let bestGuess = el.id || '';
                                try {
                                    if (tagName === 'input' && String(el.type || '').toLowerCase() === 'file') {
                                        el.setAttribute('data-ai-file-input', 'true');
                                        let labText = '';
                                        try {
                                            const eid = el.id || '';
                                            if (eid && window.CSS && CSS.escape) {
                                                const lab = document.querySelector(`label[for="${CSS.escape(eid)}"]`);
                                                labText = (lab && (lab.innerText || lab.textContent)) ? String(lab.innerText || lab.textContent).trim() : '';
                                            }
                                        } catch(e) {}
                                        if (!labText) {
                                            try {
                                                const lab = el.closest('label');
                                                labText = (lab && (lab.innerText || lab.textContent)) ? String(lab.innerText || lab.textContent).trim() : '';
                                            } catch(e) {}
                                        }
                                        if (labText && labText.length <= 60) {
                                            bestGuess = `上传文件 ${labText}`;
                                        }
                                    }
                                } catch(e) {}
                                if (!bestGuess) {
                                    const cls = (el.className || '').toString();
                                    // Extract meaningful parts from class
                                    const parts = cls.split(/\s+/).filter(c => 
                                        c.includes('icon') || c.includes('btn') || c.includes('submit') || c.includes('add') || c.includes('edit') || c.includes('search') || c.includes('input')
                                    );
                                    if (parts.length > 0) bestGuess = parts.join(' ');
                                }
                                if (bestGuess && bestGuess.length > 2) {
                                    el.setAttribute('aria-label', bestGuess);
                                    el.setAttribute('data-ai-generated-label', bestGuess);
                                }
                            }
                        }
                    });
                } catch(e) {}
            })();
            """
            await page.evaluate(js)
        except Exception:
            pass

    async def _enhance_sidebar_thumbnails_async(self, agent: Agent):
        try:
            if not agent or not hasattr(agent, "browser_context"):
                return
            page = await agent.browser_context.get_current_page()
            js = """
            (function() {
                try {
                    const isLeftSidebar = (r) => r.left >= 0 && r.left < 50 && r.width > 50 && r.width < 400 && r.height > 300;
                    const allDivs = document.querySelectorAll('div, section, aside, nav, ul');
                    let candidates = [];
                    for (const d of allDivs) {
                        const r = d.getBoundingClientRect();
                        if (isLeftSidebar(r)) {
                            const imgs = d.querySelectorAll('img, canvas, [role="img"], [class*="slide"], [class*="thumb"]');
                            if (imgs.length >= 3) {
                                candidates.push({el: d, count: imgs.length});
                            }
                        }
                    }
                    candidates.sort((a, b) => b.count - a.count);
                    const best = candidates[0];
                    if (best) {
                        const container = best.el;
                        if (!container.getAttribute('data-ai-enhanced')) {
                            container.setAttribute('data-ai-enhanced', 'true');
                            container.setAttribute('data-ai-region', '左侧缩略图列表');
                            container.setAttribute('role', 'list');
                            container.setAttribute('aria-label', '幻灯片缩略图列表');
                            container.style.outline = '3px dashed #ff00ff';
                            container.style.outlineOffset = '-3px';
                            const items = container.querySelectorAll('img, canvas, [role="img"], [class*="slide"], [class*="thumb"]');
                            let idx = 0;
                            let total = items.length;
                            container.setAttribute('data-ai-total-count', total);
                            items.forEach(it => {
                                const r = it.getBoundingClientRect();
                                if (r.width > 20 && r.height > 20) {
                                    idx++;
                                    let wrapper = it;
                                    if (it.parentElement && it.parentElement.getBoundingClientRect().width < r.width * 1.5) {
                                        wrapper = it.parentElement;
                                    }
                                    wrapper.setAttribute('data-ai-label', `Thumbnail_${idx}`);
                                    wrapper.setAttribute('role', 'listitem');
                                    wrapper.setAttribute('aria-label', `第 ${idx} 张缩略图 (共 ${total} 张)`);
                                    if (!wrapper.querySelector('.ai-thumb-idx')) {
                                        const badge = document.createElement('div');
                                        badge.className = 'ai-thumb-idx';
                                        badge.textContent = `${idx}`;
                                        badge.style.cssText = 'position:absolute;left:0;top:0;background:red;color:white;font-weight:bold;font-size:14px;padding:2px 6px;z-index:9999;pointer-events:none;border-radius:4px;box-shadow:0 0 4px rgba(0,0,0,0.5);';
                                        if (getComputedStyle(wrapper).position === 'static') {
                                            wrapper.style.position = 'relative';
                                        }
                                        wrapper.appendChild(badge);
                                    }
                                }
                            });
                        }
                    }
                } catch(e) {}
            })();
            """
            await page.evaluate(js)
        except Exception:
            pass

    async def _read_step_guide_png_bytes_async(self, step) -> bytes | None:
        try:
            # 1. Try Base64 field first
            b64 = getattr(step, "guide_image_base64", None)
            if b64:
                try:
                    return base64.b64decode(b64)
                except Exception:
                    pass

            # 2. Try ImageField
            f = getattr(step, "guide_image", None)
            if not f:
                return None
            try:
                f.open("rb")
            except Exception:
                pass
            raw = f.read()
            if not raw:
                return None
            if len(raw) > 3 * 1024 * 1024:
                raw = raw[: 3 * 1024 * 1024]
            try:
                from PIL import Image
                import io
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                return buf.getvalue()
            except Exception:
                return raw
        except Exception:
            return None

    async def _guard_step_timeout_async(self, step_number: int, hook_agent: Agent):
        timeout_s = int(getattr(settings, "AI_EXEC_AGENT_STEP_TIMEOUT_S", 90) or 90)
        if timeout_s <= 0:
            return
        try:
            await asyncio.sleep(timeout_s)
        except asyncio.CancelledError:
            return
        try:
            execution = AutoTestExecution.objects.get(id=self.execution_id)
            step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
            if not step_record:
                return
            if step_record.status != "pending":
                return
            step_record.status = "failed"
            step_record.error_message = f"单步执行超时（>{timeout_s}s），已中止以避免卡死"
            try:
                step_record.metrics = (step_record.metrics or {}) | {"duration_ms": timeout_s * 1000}
            except Exception:
                pass
            step_record.save(update_fields=["status", "error_message", "metrics"])
            try:
                hook_agent.stop()
            except Exception:
                pass
        except Exception:
            return

    def _mask_sensitive_obj(self, obj):
        if isinstance(obj, dict):
            masked = {}
            for k, v in obj.items():
                key = str(k).lower()
                if any(t in key for t in ["token", "authorization", "cookie"]):
                    masked[k] = "***"
                else:
                    masked[k] = self._mask_sensitive_obj(v)
            return masked
        if isinstance(obj, list):
            return [self._mask_sensitive_obj(x) for x in obj]
        if isinstance(obj, str):
            return self._mask_sensitive_text(obj)
        return obj

    def _mask_sensitive_text(self, text: str) -> str:
        if not text:
            return ""
        s = str(text)
        s = re.sub(r'(?i)(token|authorization)=([^&\\s]+)', r'\\1=***', s)
        return s

    def _safe_headers(self, headers: dict) -> dict:
        if not headers:
            return {}
        safe = {}
        for k, v in headers.items():
            key = str(k).lower()
            if key in ("authorization", "cookie", "set-cookie"):
                safe[k] = "***"
            else:
                safe[k] = v
        return safe

    def _encode_request_payload(self, url: str, headers: dict, post_data: str) -> str:
        payload = {
            "url": url,
            "headers": self._safe_headers(headers or {}),
        }
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            if qs:
                payload["query"] = self._mask_sensitive_obj(qs)
        except Exception:
            pass

        if post_data:
            raw = post_data
            parsed_body = None
            try:
                parsed_body = json.loads(raw)
            except Exception:
                parsed_body = None
            if parsed_body is not None:
                payload["body_json"] = self._mask_sensitive_obj(parsed_body)
            else:
                try:
                    body_qs = parse_qs(raw)
                    if body_qs:
                        payload["body_form"] = self._mask_sensitive_obj(body_qs)
                    else:
                        payload["body_raw"] = self._mask_sensitive_text(raw[:10000])
                except Exception:
                    payload["body_raw"] = self._mask_sensitive_text(raw[:10000])
        return json.dumps(payload, ensure_ascii=False)

    def _encode_response_payload(self, status: int, headers: dict, body_text: str) -> str:
        payload = {
            "status": status,
            "headers": self._safe_headers(headers or {}),
        }
        text = body_text or ""
        if len(text) > 20000:
            text = text[:20000] + "\n...<truncated>..."
        parsed_json = None
        try:
            parsed_json = json.loads(text)
        except Exception:
            parsed_json = None
        if parsed_json is not None:
            payload["body_json"] = self._mask_sensitive_obj(parsed_json)
        else:
            payload["body_text"] = self._mask_sensitive_text(text)
        return json.dumps(payload, ensure_ascii=False)

    def run(self):
        """
        Main entry point, running in a thread.
        """
        try:
            self._update_status('running', summary={"status": "running"})
            async_to_sync(self._run_async)()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            msg = str(e).strip() or type(e).__name__
            logger.error("BrowserUseRunner failed: %s\n%s", msg, tb)
            self._update_status('failed', summary={"error": msg, "traceback": tb[-18000:]})
            traceback.print_exc()
        finally:
            try:
                if self._pw or self._pw_browser:
                    async_to_sync(self._stop_network_capture_async)()
            except Exception:
                pass
            try:
                if self._message_poller_task:
                    self._message_poller_task.cancel()
                if self._signal_poller_task:
                    self._signal_poller_task.cancel()
            except Exception:
                pass
            self._cleanup_browser()

    def stop(self):
        """
        Signal to stop execution.
        """
        self.stop_requested = True
        self._cleanup_browser()

    def _find_chrome_executable(self):
        env_path = (os.environ.get("AI_EXEC_CHROME_PATH") or "").strip()
        if env_path and os.path.exists(env_path):
            return env_path

        preferred = (os.environ.get("AI_EXEC_BROWSER") or "").strip().lower()
        if os.name == "nt":
            chrome_candidates = [
                os.path.join(os.environ.get("PROGRAMFILES") or "", "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)") or "", "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA") or "", "Google", "Chrome", "Application", "chrome.exe"),
            ]
            edge_candidates = [
                os.path.join(os.environ.get("PROGRAMFILES") or "", "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)") or "", "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA") or "", "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
            candidates = []
            if preferred in ("edge", "msedge"):
                candidates.extend(edge_candidates)
                candidates.extend(chrome_candidates)
            elif preferred in ("chrome", "google-chrome"):
                candidates.extend(chrome_candidates)
                candidates.extend(edge_candidates)
            else:
                candidates.extend(chrome_candidates)
                candidates.extend(edge_candidates)
            for pth in candidates:
                if pth and os.path.exists(pth):
                    return pth

        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            try:
                path = getattr(p.chromium, "executable_path", "") or ""
                if path and os.path.exists(path):
                    return path
            finally:
                try:
                    p.stop()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            if os.name == "nt":
                candidates = [
                    os.path.join(os.environ.get("LOCALAPPDATA") or "", "ms-playwright"),
                    os.path.join(os.environ.get("USERPROFILE") or "", "AppData", "Local", "ms-playwright"),
                ]
                for base in candidates:
                    if not base or not os.path.exists(base):
                        continue
                    for item in os.listdir(base):
                        if not item.startswith("chromium-"):
                            continue
                        for sub in ("chrome-win64", "chrome-win"):
                            pth = os.path.join(base, item, sub, "chrome.exe")
                            if os.path.exists(pth):
                                return pth
        except Exception:
            pass

        try:
            if os.name == "posix":
                mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                if os.path.exists(mac):
                    return mac
        except Exception:
            pass

        try:
            pth = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
            if pth:
                return pth
        except Exception:
            pass

        return None

    def _get_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    def _default_persistent_profile_dir(self, executor_id: int, project_id: int) -> str:
        base = os.path.join(tempfile.gettempdir(), "qa_platform", "chrome_profile")
        return os.path.join(base, f"executor_{int(executor_id or 0)}", f"project_{int(project_id or 0)}")

    @sync_to_async
    def _get_executor_and_project_ids_async(self) -> tuple[int, int]:
        exe = AutoTestExecution.objects.select_related("executor", "case__project").get(id=self.execution_id)
        return (int(getattr(exe, "executor_id", 0) or 0), int(getattr(getattr(exe, "case", None), "project_id", 0) or 0))

    @sync_to_async
    def _get_trigger_payload_async(self) -> dict:
        try:
            exe = AutoTestExecution.objects.get(id=self.execution_id)
            payload = getattr(exe, "trigger_payload", None)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _ensure_persistent_profile_dir(self):
        if self._persistent_profile_dir:
            p = str(self._persistent_profile_dir)
        else:
            p = ""
        if not p:
            return ""
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            return ""
        return p

    def _launch_browser_process(self, user_data_dir: str | None = None, headless_override: bool | None = None):
        chrome_path = self._find_chrome_executable()
        if not chrome_path:
            raise FileNotFoundError("Could not find Chrome executable installed by Playwright.")

        port = self._get_free_port()
        if user_data_dir:
            self.user_data_dir = str(user_data_dir)
            self._using_persistent_profile = True
        else:
            self.user_data_dir = tempfile.mkdtemp()
            self._using_persistent_profile = False
        headless = bool(getattr(settings, "AI_EXEC_HEADLESS", True)) if headless_override is None else bool(headless_override)
        use_new_headless = bool(getattr(settings, "AI_EXEC_CHROME_USE_NEW_HEADLESS", True))
        disable_bg_net = bool(getattr(settings, "AI_EXEC_CHROME_DISABLE_BACKGROUND_NETWORKING", False))
        ignore_cert = bool(getattr(settings, "AI_EXEC_CHROME_IGNORE_CERT_ERRORS", False))
        extra = (os.environ.get("AI_EXEC_CHROME_EXTRA_ARGS") or "").strip()
        extra_args = []
        if extra:
            try:
                import shlex
                extra_args = [a for a in shlex.split(extra) if a]
            except Exception:
                extra_args = [a for a in str(extra).split(" ") if a]
        
        cmd = [
            chrome_path,
            f'--remote-debugging-port={port}',
            '--remote-debugging-address=127.0.0.1',
            '--no-sandbox',
            '--disable-gpu',
            ('--headless=new' if use_new_headless else '--headless') if headless else '',
            '--window-size=1440,900',
            '--force-device-scale-factor=1',
            f'--user-data-dir={self.user_data_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-dev-shm-usage',
            '--disable-software-rasterizer',
        ]
        if disable_bg_net:
            cmd.append("--disable-background-networking")
        if ignore_cert:
            cmd.extend(["--ignore-certificate-errors", "--allow-insecure-localhost"])
        if extra_args:
            cmd.extend(list(extra_args))
        cmd = [x for x in cmd if x]

        try:
            self._last_launch_info = {
                "exe": str(chrome_path),
                "exe_name": os.path.basename(str(chrome_path)),
                "headless": bool(headless),
                "headless_arg": ("--headless=new" if use_new_headless else "--headless") if headless else "",
                "using_persistent_profile": bool(self._using_persistent_profile),
            }
        except Exception:
            self._last_launch_info = {"headless": bool(headless)}
        
        print(f"Launching Chrome: {' '.join(cmd)}")
        self.browser_process = subprocess.Popen(cmd)
        
        return f"http://127.0.0.1:{port}"

    def _cleanup_browser(self):
        if self.browser_process:
            print("Terminating Chrome process...")
            try:
                self.browser_process.terminate()
                self.browser_process.wait(timeout=5)
            except:
                self.browser_process.kill()
            self.browser_process = None
            
        if (not self._using_persistent_profile) and self.user_data_dir and os.path.exists(self.user_data_dir):
            try:
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
            except:
                pass

    async def _run_async(self):
        # 1. Initialize LLM
        if self._executor_user is None:
            try:
                self._executor_user = await self._get_executor_user_async()
            except Exception:
                self._executor_user = None
        try:
            logger.info(
                "BrowserUseRunner executor_user=%s id=%s is_authenticated=%s",
                type(self._executor_user).__name__ if self._executor_user is not None else None,
                getattr(self._executor_user, "id", None),
                getattr(self._executor_user, "is_authenticated", None),
            )
        except Exception:
            pass
        llm = get_llm_model(temperature=0.0, user=self._executor_user)
        self._llm = llm
        try:
            from users.ai_config import resolve_exec_params
            p0 = resolve_exec_params(self._executor_user)
            self._exec_llm_info = {
                "provider": str(getattr(p0, "provider", "") or ""),
                "model": str(getattr(p0, "model", "") or ""),
                "base_url": str(getattr(p0, "base_url", "") or ""),
            }
        except Exception:
            self._exec_llm_info = {}

        # 2. Launch Browser manually
        try:
            sig = await self._get_control_signals_async()
            if sig.get("stop_signal"):
                self._stop_reason = "manual_stop"
                await self._update_status_async("stopped", {"reason": "manual_stop"})
                return
            if sig.get("pause_signal"):
                self._pause_event = asyncio.Event()
                self._pause_event.clear()
                await self._update_status_async("paused", {"reason": "paused"})
                await self._wait_if_paused_async(None, 0)
        except Exception:
            pass
        try:
            executor_id, project_id = await self._get_executor_and_project_ids_async()
            self._persistent_profile_dir = self._default_persistent_profile_dir(executor_id, project_id)
        except Exception:
            self._persistent_profile_dir = None
        headless_override = None
        try:
            payload = await self._get_trigger_payload_async()
            if isinstance(payload, dict) and ("headless" in payload):
                headless_override = bool(payload.get("headless"))
                self._forced_headless = headless_override
        except Exception:
            headless_override = None
        cdp_url = self._launch_browser_process(headless_override=headless_override)
        launch_info = getattr(self, "_last_launch_info", None)
        if not isinstance(launch_info, dict):
            launch_info = {}
        launch_headless = launch_info.get("headless")
        launch_exe = str(launch_info.get("exe_name") or launch_info.get("exe") or "")
        llm_info = getattr(self, "_exec_llm_info", None)
        if not isinstance(llm_info, dict):
            llm_info = {}
        llm_provider = str(llm_info.get("provider") or "")
        llm_model = str(llm_info.get("model") or "")
        llm_base_url = str(llm_info.get("base_url") or "")
        llm_line = ""
        if llm_provider or llm_model or llm_base_url:
            llm_line = ("llm_provider=%s\nllm_model=%s\nllm_base_url=%s" % (llm_provider, llm_model, llm_base_url)).strip()
        
        await self._upsert_step_record_async(
            step_number=0,
            description="启动浏览器并连接",
            ai_thought=("\n".join([x for x in [("cdp_url=%s\nheadless=%s\nbrowser=%s" % (cdp_url, str(launch_headless), launch_exe)).strip(), llm_line] if x])).strip(),
            action_script="launch_chrome_and_probe_cdp",
            status="pending",
            error_message="",
        )

        await self._probe_cdp_ready_async(cdp_url, timeout_seconds=15)

        await self._upsert_step_record_async(
            step_number=0,
            description="启动浏览器并连接",
            ai_thought=f"cdp_url={cdp_url}",
            action_script="launch_chrome_and_probe_cdp",
            status="success",
            error_message="",
        )

        await self._start_network_capture_async(cdp_url)

        preflight_url = ""
        try:
            project_defaults0 = await self._get_project_login_defaults_async()
            preflight_url = str((project_defaults0 or {}).get("url") or "").strip()
        except Exception:
            preflight_url = ""
        if preflight_url:
            ok0, info0 = await self._preflight_base_url_async(preflight_url)
            if (not ok0) and (not self._preflight_switched) and self._should_switch_to_persistent_profile(info0) and self._persistent_profile_dir:
                self._preflight_switched = True
                try:
                    await self._append_step_note_async(0, "检测到内网访问失败，切换为持久化专用浏览器资料目录后重试")
                except Exception:
                    pass
                headless_override2 = headless_override
                try:
                    cat0 = str((info0 or {}).get("category") or "").strip().lower()
                except Exception:
                    cat0 = ""
                if cat0 in ("dns", "proxy", "cert", "timeout", "refused") and self._can_try_headful_fallback():
                    headless_override2 = False
                    try:
                        await self._append_step_note_async(0, "提示：该内网环境可能依赖企业插件/系统代理/证书交互，已临时切换为有头浏览器以提升兼容性")
                    except Exception:
                        pass
                try:
                    await self._stop_network_capture_async()
                except Exception:
                    pass
                try:
                    self._cleanup_browser()
                except Exception:
                    pass
                pdir = self._ensure_persistent_profile_dir()
                if pdir:
                    cdp_url2 = self._launch_browser_process(user_data_dir=pdir, headless_override=headless_override2)
                    launch_info2 = getattr(self, "_last_launch_info", None)
                    if not isinstance(launch_info2, dict):
                        launch_info2 = {}
                    launch_headless2 = launch_info2.get("headless")
                    launch_exe2 = str(launch_info2.get("exe_name") or launch_info2.get("exe") or "")
                    await self._upsert_step_record_async(
                        step_number=0,
                        description="启动浏览器并连接",
                        ai_thought=("cdp_url=%s\nheadless=%s\nbrowser=%s" % (cdp_url2, str(launch_headless2), launch_exe2)).strip(),
                        action_script="launch_chrome_and_probe_cdp",
                        status="pending",
                        error_message="",
                    )
                    await self._probe_cdp_ready_async(cdp_url2, timeout_seconds=15)
                    await self._upsert_step_record_async(
                        step_number=0,
                        description="启动浏览器并连接",
                        ai_thought=f"cdp_url={cdp_url2}",
                        action_script="launch_chrome_and_probe_cdp",
                        status="success",
                        error_message="",
                    )
                    await self._start_network_capture_async(cdp_url2)
                    ok1, info1 = await self._preflight_base_url_async(preflight_url)
                    if not ok1:
                        msg = str((info1 or {}).get("brief") or (info1 or {}).get("raw") or "无法访问项目地址").strip()
                        await self._upsert_step_record_async(
                            step_number=0,
                            description="启动浏览器并连接",
                            ai_thought=f"cdp_url={cdp_url2}",
                            action_script="launch_chrome_and_probe_cdp",
                            status="failed",
                            error_message=msg[:800],
                            metrics={"preflight": info1},
                        )
                        raise RuntimeError(msg)

        self._message_poller_task = asyncio.create_task(self._message_poller_loop())
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._signal_poller_task = asyncio.create_task(self._signal_poller_loop())

        # 3. Connect Browser-Use to it
        browser = Browser(cdp_url=cdp_url)

        # 4. Construct Task
        title, steps, exec_ctx = await self._get_steps_and_title_async()
        vars_obj = (exec_ctx or {}).get("dataset_vars") if isinstance(exec_ctx, dict) else {}
        if isinstance(vars_obj, dict):
            vars_obj = dict(vars_obj)
        else:
            vars_obj = {}
        try:
            policy_overrides = vars_obj.get("ai_exec_policy") or vars_obj.get("_ai_exec_policy") or vars_obj.get("__ai_exec_policy") or {}
            if isinstance(policy_overrides, dict) and policy_overrides:
                self._stop_policy = StopPolicy.from_settings_and_overrides(settings, policy_overrides)
                if "evidence_maxlen" in policy_overrides:
                    self._evidence = EvidenceBuffer(int(policy_overrides.get("evidence_maxlen") or getattr(settings, "AI_EXEC_EVIDENCE_MAXLEN", 240) or 240))
        except Exception:
            pass
        case_mode = (exec_ctx or {}).get("case_mode") if isinstance(exec_ctx, dict) else "normal"
        project_defaults = await self._get_project_login_defaults_async()
        if str(case_mode or "").lower() == "advanced" and isinstance(project_defaults, dict):
            if project_defaults.get("url") and ("base_url" not in vars_obj) and ("url" not in vars_obj):
                vars_obj["base_url"] = project_defaults.get("url")
            if project_defaults.get("username") and ("username" not in vars_obj) and ("account" not in vars_obj) and ("user" not in vars_obj):
                vars_obj["username"] = project_defaults.get("username")
            if project_defaults.get("password") and ("password" not in vars_obj) and ("pwd" not in vars_obj):
                vars_obj["password"] = project_defaults.get("password")
        if str(case_mode or "").lower() == "advanced" and isinstance(vars_obj, dict) and vars_obj:
            steps = self._apply_vars_to_steps(steps, vars_obj)
            ds_name = str((exec_ctx or {}).get("dataset_name") or "").strip()
            run_index = int((exec_ctx or {}).get("run_index") or 1)
            run_total = int((exec_ctx or {}).get("run_total") or 1)
            if ds_name or run_total > 1:
                suffix = f" [第{run_index}/{run_total}轮 {ds_name}]".strip()
                title = (title or "") + suffix
        self._testcase_steps = steps or []
        self._case_title = title or ""
        self._case_steps_total = int(len(self._testcase_steps or []))
        self._case_steps_done = set()
        self._case_step_last_seen = 0
        self._case_steps_asserted = set()
        self._case_steps_soft_checked = set()
        self._non_blocking_by_case_step = {}
        self._non_blocking_escalated = set()
        self._assert_failed_step_number = 0
        self._assert_failed_summary = ""
        self._non_blocking_issue_notes = []
        self._scroll_policy_violations = 0
        if not steps:
            raise ValueError("No steps found")

        guide_hints = {}
        for s in steps:
            try:
                gi = getattr(s, "guide_image", None)
                if not gi:
                    continue
                png_bytes = await self._read_step_guide_png_bytes_async(s)
                if not png_bytes:
                    continue
                hint = await self._qwen_vl_extract_guide_hint_async(png_bytes)
                if hint:
                    try:
                        guide_hints[str(int(getattr(s, "step_number", 0) or 0))] = {
                            "hint": hint,
                            "image": str(getattr(gi, "url", "") or ""),
                        }
                    except Exception:
                        pass
            except Exception:
                continue
        if guide_hints:
            try:
                await self._merge_execution_summary_async({"guide_hints": guide_hints})
            except Exception:
                pass

        transfer_files = {}
        for s in steps:
            try:
                sn = str(int(getattr(s, "step_number", 0) or 0))
                name = str(getattr(s, "transfer_file_name", "") or "")
                b64 = getattr(s, "transfer_file_base64", None)
                if not name or not b64:
                    continue
                disk_path = self._ensure_transfer_file_disk_path(int(sn or 0), s) or ""
                transfer_files[sn] = {
                    "name": name,
                    "content_type": str(getattr(s, "transfer_file_content_type", "") or ""),
                    "size": int(getattr(s, "transfer_file_size", 0) or 0),
                    "path": disk_path,
                }
            except Exception:
                continue
        if transfer_files:
            try:
                await self._merge_execution_summary_async({"transfer_files": transfer_files})
            except Exception:
                pass

        login_from_steps = self._extract_login_from_steps(steps)
        try:
            u0 = str((login_from_steps or {}).get("username") or "").strip()
            p0 = str((login_from_steps or {}).get("password") or "").strip()
            if u0 and p0:
                self._expected_login_username = u0
                self._expected_login_password = p0
            else:
                du = str((project_defaults or {}).get("username") or "").strip()
                dp = str((project_defaults or {}).get("password") or "").strip()
                if du and dp:
                    self._expected_login_username = du
                    self._expected_login_password = dp
        except Exception:
            self._expected_login_username = ""
            self._expected_login_password = ""
        need_default_url = (not (str((login_from_steps or {}).get("url") or "").strip())) and bool((project_defaults or {}).get("url"))
        need_default_creds = (not (str((login_from_steps or {}).get("username") or "").strip() and str((login_from_steps or {}).get("password") or "").strip())) and bool(
            (project_defaults or {}).get("username") and (project_defaults or {}).get("password")
        )
        login_block = ""
        if need_default_url or need_default_creds:
            lines = []
            lines.append("默认登录信息（当用例步骤未明确给出时使用；若步骤写了，以步骤为准）：")
            if need_default_url:
                lines.append(f"- 访问地址：{(project_defaults or {}).get('url')}")
            if need_default_creds:
                lines.append(f"- 账号：{(project_defaults or {}).get('username')}")
                lines.append(f"- 密码：{(project_defaults or {}).get('password')}")
            login_block = "\n".join([x for x in lines if x]).strip() + "\n\n"
            try:
                await self._merge_execution_summary_async(
                    {
                        "login_defaults": {
                            "source": "project",
                            "url": (project_defaults or {}).get("url") if need_default_url else "",
                            "username": (project_defaults or {}).get("username") if need_default_creds else "",
                            "has_password": bool((project_defaults or {}).get("password")) if need_default_creds else False,
                        }
                    }
                )
            except Exception:
                pass

        task_description = f"请执行以下测试用例：{title}\n\n{login_block}操作步骤（必须按序执行，不允许跳过；除非遇到阻塞性问题才允许提前结束）：\n"
        smart_steps = []
        for step in steps:
            expected_raw = getattr(step, "expected_result", "") or ""
            expected = self._format_expected_result(expected_raw)
            smart_on = False
            try:
                smart_on = bool(getattr(step, "smart_data_enabled", False))
            except Exception:
                smart_on = False
            try:
                hint = ""
                if guide_hints:
                    hint = str((guide_hints.get(str(int(getattr(step, 'step_number', 0) or 0))) or {}).get("hint") or "")
            except Exception:
                hint = ""
            tf_name = ""
            tf_size = 0
            tf_path = ""
            try:
                tf_name = str(getattr(step, "transfer_file_name", "") or "")
                tf_size = int(getattr(step, "transfer_file_size", 0) or 0)
                if tf_name and bool(getattr(step, "transfer_file_base64", None)):
                    tf_path = self._ensure_transfer_file_disk_path(int(getattr(step, "step_number", 0) or 0), step) or ""
            except Exception:
                tf_name = ""
                tf_size = 0
                tf_path = ""
            desc = str(getattr(step, "description", "") or "")
            if smart_on:
                try:
                    smart_steps.append(int(getattr(step, "step_number", 0) or 0))
                except Exception:
                    pass
                try:
                    desc2, changed = self._smart_data_rewrite_description(desc, expected_raw)
                    if changed and desc2:
                        desc = desc2
                except Exception:
                    pass
            if expected:
                task_description += f"{step.step_number}. {desc}\n"
                if hint:
                    task_description += f"   参考截图提示：{hint}\n"
                if tf_name:
                    task_description += f"   传输文件：{tf_name}（{tf_size} bytes）\n"
                    if tf_path:
                        task_description += f"   上传文件路径：{tf_path}\n"
                if smart_on:
                    task_description += "   数据策略：本步骤开启智能数据生成。若步骤描述中存在具体值，请把这些值视为示例并自行生成新数据；若描述已被标记为【智能生成：字段】，则必须按字段语义生成新数据。\n"
                task_description += f"   预期（仅作为参考，用于判断是否存在缺陷）：{expected}\n"
            else:
                task_description += f"{step.step_number}. {desc}\n"
                if hint:
                    task_description += f"   参考截图提示：{hint}\n"
                if tf_name:
                    task_description += f"   传输文件：{tf_name}（{tf_size} bytes）\n"
                    if tf_path:
                        task_description += f"   上传文件路径：{tf_path}\n"
                if smart_on:
                    task_description += "   数据策略：本步骤开启智能数据生成。若步骤描述中存在具体值，请把这些值视为示例并自行生成新数据；若描述已被标记为【智能生成：字段】，则必须按字段语义生成新数据。\n"
        try:
            smart_steps2 = []
            try:
                smart_steps2 = sorted([int(x) for x in smart_steps if int(x) > 0])
            except Exception:
                smart_steps2 = []
            await self._merge_execution_summary_async({"smart_data_steps": smart_steps2})
            if smart_steps2:
                task_description = f"智能数据生成已开启步骤：{smart_steps2}\n" + task_description
        except Exception:
            smart_steps2 = []
        req_max_page = self._infer_required_max_page()
        wants_full_pagination = self._wants_full_pagination()
        task_description += (
            "\n要求：\n"
            "1) 必须严格按操作步骤顺序执行，直到最后一步执行完成并观察到结果后才允许 done/结束；除非遇到阻塞性问题（无法继续执行）才允许提前结束。\n"
            "2) 不要扩展额外测试点（只执行用例步骤）；不要跳步。\n"
            "3) 每执行用例步骤时，你必须在 AI 思考里写明进度：用例步骤X/Y，并在完成后写明：完成用例步骤X（附关键证据：toast/弹窗/页面跳转/接口状态码等）。\n"
            "4) 预期结果仅作为参考，不是绝对依据：结合页面提示/接口状态/页面是否跳转等证据拟人化判断；但不得编造未发生的结果。\n"
            "5) 过程中遇到异常要尝试自恢复（重试/短等待/刷新/重新定位元素），单次等待尽量不超过2秒；若仍无法继续且属于阻塞性问题，说明原因后结束。\n"
            "6) 滚动策略：禁止无目的滚动。仅在以下情况才允许滚动：A) 当前用例步骤明确要求滚动/翻页；B) 为寻找元素且当前视口找不到；C) 触发懒加载/加载更多。每次滚动前需在思考中说明原因，连续滚动不超过2次。\n"
            "7) 定位策略：不要依赖“元素 index=xxx”这种易失定位；若出现 Element index not found：先重新提取页面状态/重新定位，再继续。若文本定位失败，请观察图标/颜色/布局位置进行定位（已开启视觉辅助）。\n"
            "8) 防止反复确认：当你已经获得足够证据确认当前用例步骤已完成（例如已成功切换到目标模板/目标幻灯片/目标页面），必须立即写“完成用例步骤X（证据…）”并进入下一步；不允许在同一步骤反复确认超过1次。\n"
            "9) 最终输出必须使用中文，总结结论与关键证据；若未执行完全部步骤，必须明确说明未完成的步骤与原因，禁止伪造“已测试完”。\n"
            "10) 理解测试意图：操作不仅是点击，更重要的是达到预期的页面状态。例如'登录'意味着'点击登录并等待首页加载'；'检查错误'意味着'找到错误提示文本'。\n"
            "11) 列表/幻灯片遍历警告：如果用例要求'检查每一页'、'遍历所有幻灯片'或'对每个项目执行'，你必须严格滚动并逐一处理，直到明确看到'最后一页'或'列表底部'。绝对禁止只检查前几项就提前结束！你必须确认已处理的总数与页面显示的每一项一致。\n"
            "12) 上传/导入文件证据要求：当用例步骤涉及上传/导入时，完成该步骤必须给出证据（至少满足其一）：A) 页面已展示所选文件名；B) 出现上传/导入进度并完成；C) 出现成功 toast/提示；D) 触发相关上传接口且返回 2xx。若未看到证据，禁止写“上传成功/已导入”，必须重试或说明阻塞原因。\n"
            "13) 若页面出现登录表单且上方提供了“默认登录信息”，你必须先按默认登录信息完成登录（输入账号/密码并点击登录，观察页面跳转/接口状态码），否则不得 done/结束。若无可用账号密码，必须明确说明阻塞原因。"
        )
        if wants_full_pagination:
            if req_max_page:
                task_description += (
                    f"\n分页遍历要求：此用例需要遍历全部页码（至少 1-{int(req_max_page)} 页）。"
                    "在未覆盖到最后一页前，绝不允许 done/结束。"
                    "每次翻页后必须在 AI 思考里明确写出：已到第X页。\n"
                )
            else:
                task_description += (
                    "\n分页遍历要求：此用例需要遍历全部页码（直到最后一页/无更多页）。"
                    "在未明确确认到末页前，绝不允许 done/结束。"
                    "每次翻页后必须在 AI 思考里明确写出：已到第X页。\n"
                )

        print(f"Starting Agent: title={title} steps={len(steps)}")
         
        smart_line = f"   - 智能数据生成已开启的用例步骤编号：{smart_steps2}。对这些步骤：步骤描述中冒号/等号后面的具体值（例如“填写用户名：user007”里的 user007）视为示例，禁止原样复用；你必须基于字段语义自行生成新值，并在执行输入前先在思考中写出将输入的值。\n"
        extend_system_message = (
                "ROLE: You are a Senior QA Automation Engineer. Your goal is to execute the test case faithfully and robustly.\n"
                "CORE RULES:\n"
                "1. [OUTPUT] You must use CHINESE for all thoughts, explanations, and final results.\n"
                "2. [EXECUTION] Strictly follow the 'Test Steps' sequence. Do NOT skip steps or combine them unless trivial.\n"
                "   - 进度标记（必须严格使用此格式）：用例步骤X/Y。\n"
                "   - 步骤完成标记（必须严格使用此格式）：完成用例步骤X（关键证据：toast/弹窗/页面跳转/截图/接口状态码等）。\n"
                "3. [POSITIONING] If you cannot find an element:\n"
                "   - Do NOT hallucinate. Look for alternative identifiers (icons, placeholders, aria-labels).\n"
                "   - Use relative positioning (e.g. 'the button next to input X').\n"
                "   - If it might be off-screen, scroll gently.\n"
                "   - If all else fails, report 'Element not found' honestly rather than clicking random things.\n"
                "4. [VERIFICATION] Every action implies a verification.\n"
                "   - After clicking, CHECK if the page updated (URL change? New modal? Spinner stopped?).\n"
                "   - If nothing happened, retry with a JS click or different selector.\n"
                "   - If a click opens a NEW TAB/POPUP, you MUST switch to the new page and continue there.\n"
                "5. [INTENT] Understand the 'Test Intent'.\n"
                "   - If step says 'Check Error Message', finding the text IS the success.\n"
                "   - If step says 'Login', success means seeing the Dashboard, not just clicking 'Login'.\n"
                "6. [RESTRICTIONS]\n"
                "   - Unless blocked, do not stop early.\n"
                "   - Do not make up evidence.\n"
                "   - Avoid infinite scrolling.\n"
                "   - 数据使用规则（非常重要）：默认情况下必须使用“用例步骤里明确给出的数据”（例如用户名/手机号/密码等）原样输入；当且仅当该用例步骤开启了智能数据生成开关时，才允许自行生成测试数据。\n"
                + smart_line +
                "   - 严禁调用 done/完成 动作来结束任务，除非你已输出所有步骤的“完成用例步骤X”标记并确认全部用例步骤已执行。\n"
                "   - Do NOT guess default credentials. If username/password is not provided in steps, use the '默认登录信息' if present; otherwise report blocked.\n"
                "   - Handle 'index not found' by re-assessing the page DOM.\n"
                "   - 文件上传规则（非常重要）：如果某个步骤要求上传/导入文件，且步骤里给出了“上传文件路径”，你必须优先使用 upload_file 动作（不要 click 打开文件选择器）。upload_file 需要：index（上传按钮/区域的元素索引）+ path（步骤里给出的上传文件路径）。\n"
        )
        base_task_description = str(task_description or "")
        agent = None
        history = None
        self._active_agent = None

        # 5. Run Agent
        async def on_step_start(hook_agent: Agent):
            await self._ensure_foreground_page_async()
            await self._enhance_interactive_elements_async(hook_agent)
            await self._enhance_sidebar_thumbnails_async(hook_agent)
            self._agent_step_seq += 1
            step_number = int(self._agent_step_seq)
            self._current_agent_step = step_number
            self._current_step_number = step_number
            await self._wait_if_paused_async(hook_agent, step_number)
            await self._apply_control_signals_async(hook_agent, step_number)
            started_ts = None
            try:
                started_ts = time.time()
                self._step_started_at[int(step_number)] = started_ts
            except Exception:
                pass
            try:
                t = self._step_timeout_tasks.pop(int(step_number), None)
                if t:
                    t.cancel()
            except Exception:
                pass
            try:
                self._step_timeout_tasks[int(step_number)] = asyncio.create_task(self._guard_step_timeout_async(step_number, hook_agent))
            except Exception:
                pass
            await self._upsert_step_record_async(
                step_number=step_number,
                description=f"Step {step_number} 执行中...",
                ai_thought="",
                action_script="",
                status="pending",
                error_message="",
                metrics={"started_at_ms": int((started_ts or time.time()) * 1000)},
            )
            try:
                await self._auto_login_if_needed_async(int(step_number))
            except Exception:
                pass
            try:
                case_step_no = int(self._case_step_last_seen or 0)
                if case_step_no <= 0:
                    case_step_no = int(self._get_next_pending_transfer_file_step_no() or 0)
                if case_step_no > 0 and self._case_step_requires_upload_file(case_step_no):
                    s = self._find_case_step_by_number(case_step_no)
                    payload = self._get_transfer_file_payload(s)
                    if payload:
                        page = await self._get_active_page_async()
                        try:
                            await self._ensure_filechooser_autofill_async(page, payload, int(case_step_no))
                        except Exception:
                            pass
                        try:
                            already = int(case_step_no) in set(self._transfer_file_applied_steps or set())
                        except Exception:
                            already = False
                        if not already:
                            try:
                                retry_count = 0
                                res = await self._try_apply_transfer_file_async(page, payload)
                                if (not res.get("success")) and str(res.get("reason") or "") == "no_file_input":
                                    for _ in range(8):
                                        retry_count += 1
                                        try:
                                            await page.wait_for_timeout(250)
                                        except Exception:
                                            pass
                                        res = await self._try_apply_transfer_file_async(page, payload)
                                        if res.get("success"):
                                            break
                                if res.get("success"):
                                    sel = {}
                                    try:
                                        await page.wait_for_timeout(200)
                                    except Exception:
                                        pass
                                    try:
                                        sel = await self._detect_file_input_selection_async(page, str(payload.get("name") or ""))
                                    except Exception:
                                        sel = {}
                                    matched_cnt = 0
                                    try:
                                        matched_cnt = int((sel or {}).get("matchedCount") or 0)
                                    except Exception:
                                        matched_cnt = 0
                                    level = "selected_ok" if matched_cnt > 0 else "selected_uncertain"
                                    try:
                                        self._transfer_file_applied_steps.add(int(case_step_no))
                                    except Exception:
                                        pass
                                    await self._append_step_note_async(step_number, f"预置上传：已自动选择文件 {payload.get('name')}（{level}）")
                                    await self._merge_step_metrics_async(
                                        int(step_number),
                                        {
                                            "transfer_file_prefill": True,
                                            "transfer_file_name": str(payload.get("name") or ""),
                                            "transfer_file_retry": int(retry_count),
                                            "file_input_pick": res,
                                            "transfer_file_level": str(level),
                                            "transfer_file_selection": sel,
                                        },
                                    )
                                else:
                                    await self._merge_step_metrics_async(
                                        int(step_number),
                                        {
                                            "transfer_file_prefill": False,
                                            "transfer_file_name": str(payload.get("name") or ""),
                                            "transfer_file_retry": int(retry_count),
                                            "transfer_file_prefill_error": str(res.get("reason") or "")[:200],
                                            "file_input_pick": res,
                                        },
                                    )
                            except Exception:
                                pass
            except Exception:
                pass

        async def on_step_end(hook_agent: Agent):
            if not hook_agent.history.history:
                return

            item = hook_agent.history.history[-1]
            step_number = int(self._current_agent_step or self._agent_step_seq or 1)
            self._current_step_number = step_number
            await self._ensure_foreground_page_async()

            description = "AI 动作"
            ai_thought = ""
            action_script = ""
            action_value = None

            model_output = getattr(item, "model_output", None)
            if model_output:
                if isinstance(model_output, dict):
                    current_state = model_output.get("current_state")
                    if isinstance(current_state, dict):
                        eval_goal = current_state.get("evaluation_previous_goal") or ""
                        memory = current_state.get("memory") or ""
                        next_goal = current_state.get("next_goal") or ""
                        ai_thought = "\n".join([t for t in [eval_goal, memory, next_goal] if t])
                    if not ai_thought:
                        ai_thought = model_output.get("reasoning") or ""
                    action_value = model_output.get("action")
                else:
                    current_state = getattr(model_output, "current_state", None)
                    if current_state is not None:
                        eval_goal = getattr(current_state, "evaluation_previous_goal", "") or ""
                        memory = getattr(current_state, "memory", "") or ""
                        next_goal = getattr(current_state, "next_goal", "") or ""
                        ai_thought = "\n".join([t for t in [eval_goal, memory, next_goal] if t])
                    if not ai_thought:
                        ai_thought = getattr(model_output, "reasoning", "") or ""
                    action_value = getattr(model_output, "action", None)

                if action_value is not None:
                    try:
                        action_script = json.dumps(action_value, ensure_ascii=False)
                    except Exception:
                        action_script = str(action_value)
                    desc = self._summarize_action(action_value)
                    if desc:
                        description = desc

            status = "success"
            error_message = ""
            results = getattr(item, "result", None) or []
            for r in results:
                err = getattr(r, "error", None)
                if err:
                    status = "failed"
                    error_message = self._humanize_llm_auth_error(str(err))
                    break
            metrics = {}
            try:
                started = self._step_started_at.pop(int(step_number), None)
                if started:
                    metrics["duration_ms"] = int((time.time() - float(started)) * 1000)
            except Exception:
                metrics = {}
            try:
                t = self._step_timeout_tasks.pop(int(step_number), None)
                if t:
                    t.cancel()
            except Exception:
                pass

            screenshot_data = None
            screenshot_path = None
            state_obj = getattr(item, "state", None)
            if state_obj is not None:
                screenshot_path = getattr(state_obj, "screenshot_path", None)
                get_screenshot = getattr(state_obj, "get_screenshot", None)
                if callable(get_screenshot):
                    screenshot_data = get_screenshot()

            await self._upsert_step_record_async(
                step_number=step_number,
                description=description or "AI 动作",
                ai_thought=ai_thought,
                action_script=action_script,
                status=status,
                error_message=error_message,
                screenshot_data=screenshot_data,
                screenshot_path=screenshot_path,
                metrics=metrics,
            )
            try:
                case_step_no = int(self._get_next_pending_transfer_file_step_no() or 0)
                if case_step_no > 0 and self._case_step_requires_upload_file(case_step_no):
                    s = self._find_case_step_by_number(case_step_no)
                    payload = self._get_transfer_file_payload(s)
                    if payload:
                        page = await self._get_active_page_async()
                        try:
                            await self._ensure_filechooser_autofill_async(page, payload, int(case_step_no))
                        except Exception:
                            pass
                        try:
                            already = int(case_step_no) in set(self._transfer_file_applied_steps or set())
                        except Exception:
                            already = False
                        if not already:
                            try:
                                res = await self._try_apply_transfer_file_async(page, payload)
                                if res.get("success"):
                                    try:
                                        self._transfer_file_applied_steps.add(int(case_step_no))
                                    except Exception:
                                        pass
                                    await self._merge_step_metrics_async(int(step_number), {"transfer_file_prefill": True, "transfer_file_name": str(payload.get("name") or ""), "file_input_pick": res, "transfer_file_prefill_stage": "step_end"})
                                else:
                                    await self._merge_step_metrics_async(int(step_number), {"transfer_file_prefill": False, "transfer_file_name": str(payload.get("name") or ""), "transfer_file_prefill_error": str(res.get("reason") or "")[:200], "file_input_pick": res, "transfer_file_prefill_stage": "step_end"})
                            except Exception:
                                pass
            except Exception:
                pass
            try:
                is_open_like = False
                low_desc = str(description or "").lower()
                low_act = str(action_script or "").lower()
                if ("open_tab" in low_act) or ("switch_tab" in low_act) or ("go_to_url" in low_act) or ("goto" in low_act):
                    is_open_like = True
                if "打开新标签页" in str(description or "") or "切换标签页" in str(description or ""):
                    is_open_like = True
                if is_open_like:
                    page = await self._get_active_page_async()
                    url_now = ""
                    try:
                        url_now = str(getattr(page, "url", "") or "")
                    except Exception:
                        url_now = ""
                    if self._is_blank_like_url(url_now):
                        now = time.time()
                        self._blank_page_open_ts.append(now)
                        self._blank_page_open_ts = [t for t in self._blank_page_open_ts if (now - float(t)) <= 30.0][-10:]
                        if len(self._blank_page_open_ts) >= 3:
                            urls = []
                            try:
                                for ctx in list(self._pw_contexts or []):
                                    for p in list(getattr(ctx, "pages", []) or []):
                                        try:
                                            urls.append(str(p.url or "")[:500])
                                        except Exception:
                                            continue
                            except Exception:
                                urls = []
                            urls = [u for u in urls if u][:12]
                            msg = "检测到新标签页/弹窗反复打开但停留在空白页（about:blank/新建页），无法继续识别页面。可能原因：第三方系统登录跳转被拦截（禁止弹窗/跨域限制/必须在容器内打开）或目标链接不可直接访问。"
                            try:
                                await self._append_step_note_async(int(step_number), msg)
                            except Exception:
                                pass
                            try:
                                await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "blank_page_loop", "page_urls": urls, "current_url": url_now[:500]})
                            except Exception:
                                pass
                            try:
                                await self._mark_step_failed_async(int(step_number), msg)
                            except Exception:
                                pass
                            try:
                                hook_agent.stop()
                            except Exception:
                                pass
                            try:
                                self._early_history = getattr(hook_agent, "history", None)
                            except Exception:
                                self._early_history = None
                            self._stop_reason = "blank_page_loop"
                            raise _AgentEarlyStop()
                    else:
                        if url_now:
                            self._last_page_urls.append(url_now[:500])
                            self._last_page_urls = self._last_page_urls[-12:]
                        self._blank_page_open_ts = []
            except _AgentEarlyStop:
                raise
            except Exception:
                pass
            try:
                if status != "failed" and not self._stop_reason:
                    page = await self._get_active_page_async()
                    url_now = ""
                    try:
                        url_now = str(getattr(page, "url", "") or "")
                    except Exception:
                        url_now = ""
                    title = ""
                    try:
                        title = str(await page.title())[:180] if page else ""
                    except Exception:
                        title = ""
                    body_text = ""
                    try:
                        body_text = await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''") if page else ""
                    except Exception:
                        body_text = ""
                    code = 0
                    try:
                        code = int(self._detect_gateway_error_code(title, body_text))
                    except Exception:
                        code = 0
                    if int(code or 0) in (502, 504):
                        recent_net = []
                        try:
                            recent_net = await self._collect_recent_network_entries_async(int(step_number), limit=30)
                        except Exception:
                            recent_net = []
                        severe = []
                        try:
                            for e in recent_net or []:
                                st = int(e.get("status") or 0)
                                if st >= 500:
                                    severe.append(f"{str(e.get('method') or '').upper()} {st} {str(e.get('url') or '')[:180]}")
                        except Exception:
                            severe = []
                        probe = {}
                        try:
                            probe = await self._probe_http_status_async(url_now)
                        except Exception:
                            probe = {}
                        snippet = " ".join(str(body_text or "").split())[:260]
                        summary = f"页面出现网关错误：HTTP {int(code)}（Bad Gateway/Gateway Timeout）。"
                        if url_now:
                            summary = (summary + f"\nURL：{url_now[:500]}").strip()
                        if title:
                            summary = (summary + f"\n标题：{title}").strip()
                        if snippet:
                            summary = (summary + f"\n页面内容片段：{snippet}").strip()
                        if probe:
                            summary = (summary + f"\n执行端直连探测：{json.dumps(probe, ensure_ascii=False)[:300]}").strip()
                        if severe:
                            summary = (summary + "\n近期 5xx：" + "；".join(severe[:6])).strip()
                        created_bug_id = None
                        try:
                            created_bug_id = await self._create_bug_for_assertion_async(
                                assertion_summary=summary[:1800],
                                suggested_title=f"[AI执行][网关错误{int(code)}] {self._case_title or ''}".strip()[:120],
                                suggested_description="自动化执行过程中页面返回网关错误（nginx 502/504）。可能原因：执行端机器网络/代理/VPN与用户不一致、WAF/反爬策略对自动化访问返回错误、或上游服务在该链路下不稳定。",
                            )
                        except Exception:
                            created_bug_id = None
                        if created_bug_id:
                            try:
                                self._forced_bug_id = int(created_bug_id)
                            except Exception:
                                pass
                        try:
                            em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                            await self._mark_step_failed_async(int(step_number), em[:1800])
                        except Exception:
                            pass
                        try:
                            await self._append_step_note_async(int(step_number), f"阻断：检测到 HTTP {int(code)} 网关错误，已登记缺陷并停止")
                            await self._merge_stop_metrics_async(
                                int(step_number),
                                {"stopped_reason": f"http_{int(code)}", "current_url": url_now[:500], "page_title": title, "probe": probe, "recent_5xx": severe[:8]},
                            )
                        except Exception:
                            pass
                        try:
                            hook_agent.stop()
                        except Exception:
                            pass
                        try:
                            self._early_history = getattr(hook_agent, "history", None)
                        except Exception:
                            self._early_history = None
                        self._stop_reason = f"http_{int(code)}"
                        raise _AgentEarlyStop()
            except _AgentEarlyStop:
                raise
            except Exception:
                pass
            try:
                is_wait_action = False
                try:
                    is_wait_action = str(description or "").startswith("等待:")
                    if not is_wait_action:
                        is_wait_action = "\"wait\"" in str(action_script or "").lower()
                except Exception:
                    is_wait_action = False
                if is_wait_action and status != "failed":
                    self._consecutive_wait_actions = int(self._consecutive_wait_actions or 0) + 1
                else:
                    self._consecutive_wait_actions = 0
                if int(self._consecutive_wait_actions or 0) >= 3:
                    page = await self._get_active_page_async()
                    loading = await self._detect_loading_indicators_async(page) if page else {}
                    inter = await self._detect_basic_interactivity_async(page) if page else {}
                    has_loading = False
                    try:
                        has_loading = bool((loading or {}).get("ariaBusy") or (loading or {}).get("roleProgress") or (loading or {}).get("clsSpinner") or (loading or {}).get("hits"))
                    except Exception:
                        has_loading = False
                    enabled_controls = 0
                    try:
                        enabled_controls = int((inter or {}).get("enabled_controls") or 0)
                    except Exception:
                        enabled_controls = 0
                    if (not has_loading) and enabled_controls >= 1:
                        self._consecutive_wait_actions = 0
                        await self._append_step_note_async(step_number, "等待监控：页面未检测到加载/进度提示且已可交互，提示 AI 继续下一步操作")
                        await self._merge_step_metrics_async(
                            int(step_number),
                            {"wait_nudge": True, "wait_nudge_loading": loading, "wait_nudge_interactive": inter},
                        )
                        try:
                            await self._inject_hint_overlay_async(page, "检测到页面已可继续操作（进度/加载提示已消失）。请不要继续等待，直接进行下一步点击/输入/确认。")
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                combo_progress = f"{ai_thought}\n{description}"
                cur_i, total_i, done_set = self._extract_case_step_progress(combo_progress)
                if cur_i is not None:
                    self._case_step_last_seen = int(cur_i)
                    await self._merge_step_metrics_async(int(step_number), {"case_step_current": int(cur_i), "case_step_total_hint": int(total_i or 0)})
                    try:
                        total_steps2 = int(self._case_steps_total or len(self._testcase_steps or []))
                    except Exception:
                        total_steps2 = int(len(self._testcase_steps or []))
                    try:
                        if total_steps2 > 0 and int(cur_i) >= int(total_steps2) and not self._stop_reason:
                            okf, sumf, _ = await self._assert_expected_for_case_step_async(
                                int(total_steps2),
                                int(step_number),
                                strict=True,
                                create_bug=False,
                            )
                            if okf and sumf:
                                try:
                                    self._case_steps_done.update(set(range(1, int(total_steps2) + 1)))
                                except Exception:
                                    pass
                                self._stop_reason = "steps_completed"
                                self._stopped_by_ai = True
                                await self._append_step_note_async(step_number, f"最终步骤断言通过（用例步骤{int(total_steps2)}），自动结束执行")
                                try:
                                    await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "steps_completed", "by": "final_assert_progress", "assert_case_step": int(total_steps2)})
                                except Exception:
                                    pass
                                try:
                                    hook_agent.stop()
                                except Exception:
                                    pass
                                try:
                                    self._early_history = getattr(hook_agent, "history", None)
                                except Exception:
                                    self._early_history = None
                                raise _AgentEarlyStop()
                    except _AgentEarlyStop:
                        raise
                    except Exception:
                        pass
                    try:
                        if (not is_wait_action) and status != "failed":
                            if int(self._case_step_hold_no or 0) == int(cur_i):
                                self._case_step_hold_actions = int(self._case_step_hold_actions or 0) + 1
                            else:
                                self._case_step_hold_no = int(cur_i)
                                self._case_step_hold_actions = 1
                        else:
                            if int(self._case_step_hold_no or 0) != int(cur_i):
                                self._case_step_hold_no = int(cur_i)
                                self._case_step_hold_actions = 0
                    except Exception:
                        pass
                    try:
                        if int(self._case_step_hold_actions or 0) >= 18:
                            summary = f"检测到疑似卡死：同一用例步骤 {int(cur_i)} 连续执行 {int(self._case_step_hold_actions)} 次仍未推进。"
                            try:
                                page = await self._get_active_page_async()
                                url_now = str(getattr(page, 'url', '') or '') if page else ''
                            except Exception:
                                url_now = ''
                            if url_now:
                                summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                            created_bug_id = None
                            try:
                                created_bug_id = await self._create_bug_for_assertion_async(
                                    assertion_summary=summary[:1800],
                                    suggested_title=f"[AI执行][步骤未推进] {self._case_title or ''}".strip()[:120],
                                    suggested_description="AI在同一用例步骤内反复尝试但未能推进，疑似页面无响应/校验阻塞/按钮无效/弹窗未关闭等。",
                                )
                            except Exception:
                                created_bug_id = None
                            if created_bug_id:
                                try:
                                    self._forced_bug_id = int(created_bug_id)
                                except Exception:
                                    pass
                            try:
                                em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                                await self._mark_step_failed_async(int(step_number), em[:1800])
                            except Exception:
                                pass
                            try:
                                await self._append_step_note_async(int(step_number), "阻断：同一用例步骤长时间未推进，已登记缺陷并停止（避免无限重试）")
                                await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "case_step_not_progressing", "case_step": int(cur_i), "repeat": int(self._case_step_hold_actions), "current_url": str(url_now or '')[:500]})
                            except Exception:
                                pass
                            try:
                                hook_agent.stop()
                            except Exception:
                                pass
                            try:
                                self._early_history = getattr(hook_agent, "history", None)
                            except Exception:
                                self._early_history = None
                            self._stop_reason = "case_step_not_progressing"
                            raise _AgentEarlyStop()
                    except _AgentEarlyStop:
                        raise
                    except Exception:
                        pass
            except _AgentEarlyStop:
                raise
            except Exception:
                pass
            try:
                submit_like = self._is_submit_like_action(description or "", action_script or "", ai_thought or "")
            except Exception:
                submit_like = False
            if submit_like:
                url_now = ""
                try:
                    page = await self._get_active_page_async()
                    url_now = str(getattr(page, "url", "") or "")
                except Exception:
                    url_now = ""
                net_now = 0
                try:
                    net_now = int(await self._get_network_entry_count_async())
                except Exception:
                    net_now = 0
                new_msgs = []
                try:
                    new_msgs = await self._collect_new_runtime_messages_async()
                except Exception:
                    new_msgs = []
                if new_msgs:
                    try:
                        self._runtime_messages.extend(new_msgs)
                    except Exception:
                        pass
                sig = "SUBMIT_LIKE"
                effect_token = ""
                try:
                    effect_token = await self._build_no_effect_token_async(page, url_now, net_now)
                except Exception:
                    effect_token = f"u={str(url_now or '')[:300]}|n={int(net_now or 0)}"
                try:
                    if str(effect_token or "") and str(effect_token or "") == str(self._no_effect_token_last or ""):
                        self._no_effect_streak = int(self._no_effect_streak or 0) + 1
                    else:
                        self._no_effect_token_last = str(effect_token or "")[:900]
                        self._no_effect_streak = 1 if str(effect_token or "") else 0
                except Exception:
                    pass
                if str(effect_token or "") and str(effect_token or "") == str(self._submit_repeat_last_effect or ""):
                    self._submit_repeat_count = int(self._submit_repeat_count or 0) + 1
                else:
                    self._submit_repeat_sig = sig
                    self._submit_repeat_last_url = str(url_now or "")[:500]
                    try:
                        self._submit_repeat_last_msg_count = int(len(self._runtime_messages or []))
                    except Exception:
                        self._submit_repeat_last_msg_count = 0
                    self._submit_repeat_last_effect = str(effect_token or "")[:900]
                    self._submit_repeat_count = 1
                self._last_effect_url = str(url_now or "")[:500]
                try:
                    self._last_effect_net_count = int(net_now)
                except Exception:
                    self._last_effect_net_count = int(net_now)
                try:
                    desc_s = str(description or "")
                    thought_s = str(ai_thought or "")
                    combo_s = (desc_s + "\n" + thought_s).strip()
                    combo_l = combo_s.lower()
                    is_save_like = any(k in combo_s for k in ["保存", "提交", "确认", "确定"]) or any(k in combo_l for k in ["save", "submit", "confirm", "ok"])
                except Exception:
                    is_save_like = False
                if is_save_like:
                    try:
                        wait_ms = int(self._stop_policy.submit_wait_ms())
                    except Exception:
                        wait_ms = 800
                    try:
                        wait_ms = max(200, min(int(wait_ms), 1200))
                    except Exception:
                        wait_ms = 800
                    try:
                        if page and wait_ms > 0:
                            await page.wait_for_timeout(int(wait_ms))
                    except Exception:
                        pass
                    new_msgs2 = []
                    try:
                        new_msgs2 = await self._collect_new_runtime_messages_async()
                    except Exception:
                        new_msgs2 = []
                    if new_msgs2:
                        try:
                            self._runtime_messages.extend(new_msgs2)
                        except Exception:
                            pass
                    all_msgs = []
                    try:
                        all_msgs = [str(x) for x in (list(new_msgs or []) + list(new_msgs2 or [])) if x]
                    except Exception:
                        all_msgs = list(new_msgs or [])
                        dom_sig = ""
                        try:
                            dom_sig = await self._compute_dom_effect_sig_async(page)
                        except Exception:
                            dom_sig = ""
                        save_effect = f"u={str(url_now or '')[:300]}|d={str(dom_sig or '')[:900]}"
                        success_msg = ""
                        try:
                            for m in reversed((all_msgs or [])[-8:]):
                                ms = str(m or "").strip()
                                if not ms:
                                    continue
                                lower = ms.lower()
                                if any(k in ms for k in ["成功", "新增成功", "创建成功", "保存成功", "提交成功", "操作成功"]) or ("success" in lower):
                                    success_msg = ms[:200]
                                    break
                            if not success_msg:
                                for m in reversed((self._runtime_messages or [])[-12:]):
                                    ms = str(m or "").strip()
                                    if not ms:
                                        continue
                                    lower = ms.lower()
                                    if any(k in ms for k in ["成功", "新增成功", "创建成功", "保存成功", "提交成功", "操作成功"]) or ("success" in lower):
                                        success_msg = ms[:200]
                                        break
                        except Exception:
                            success_msg = ""
                        submit_net = ""
                        try:
                            recent_net = await self._collect_recent_network_entries_async(int(step_number), limit=60)
                            for e in recent_net or []:
                                u = str(e.get("url") or "").lower()
                                m = str(e.get("method") or "").upper()
                                st = int(e.get("status") or 0)
                                if m in ("POST", "PUT", "PATCH"):
                                    if any(k in u for k in ["login", "auth", "token", "session"]):
                                        continue
                                    submit_net = f"{m} {st} {str(e.get('url') or '')[:180]}"
                                    break
                        except Exception:
                            submit_net = ""
                        try:
                            feedback_msgs = []
                            try:
                                feedback_msgs = [m for m in (all_msgs or []) if self._is_feedback_message(m)]
                            except Exception:
                                feedback_msgs = []
                            prompt_seen = bool(success_msg) or bool(feedback_msgs)
                            response_seen = bool(submit_net) or prompt_seen
                            should_stop_save = self._record_save_like_observation(prompt_seen=prompt_seen, response_seen=response_seen, save_effect=save_effect)
                        except Exception:
                            should_stop_save = False
                        try:
                            elapsed = 0.0
                            try:
                                if float(self._save_like_started_at or 0.0) > 0:
                                    elapsed = float(time.time() - float(self._save_like_started_at or 0.0))
                            except Exception:
                                elapsed = 0.0
                            if bool(should_stop_save):
                                summary = "点击保存/提交两次后仍无任何提示（成功/失败/校验错误等），疑似按钮无效或前端校验/后端异常未反馈。"
                                if url_now:
                                    summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                                created_bug_id = None
                                try:
                                    created_bug_id = await self._create_bug_for_assertion_async(
                                        assertion_summary=summary[:1800],
                                        suggested_title=f"[AI执行][保存无响应] {self._case_title or ''}".strip()[:120],
                                        suggested_description="连续两次点击保存/提交后仍无任何可见提示（成功/失败/校验错误等），疑似按钮无效、校验无提示或异常未反馈。",
                                    )
                                except Exception:
                                    created_bug_id = None
                                if created_bug_id:
                                    try:
                                        self._forced_bug_id = int(created_bug_id)
                                    except Exception:
                                        pass
                                try:
                                    em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                                    await self._mark_step_failed_async(int(step_number), em[:1800])
                                except Exception:
                                    pass
                                try:
                                    await self._append_step_note_async(int(step_number), "阻断：两次保存/提交无提示，已登记缺陷并停止（避免无限重试）")
                                    await self._merge_stop_metrics_async(
                                        int(step_number),
                                        {"stopped_reason": "save_no_response", "save_no_prompt_clicks": int(self._save_like_no_prompt_clicks), "repeat": int(self._save_like_no_success), "current_url": str(url_now or "")[:500]},
                                    )
                                except Exception:
                                    pass
                                try:
                                    hook_agent.stop()
                                except Exception:
                                    pass
                                try:
                                    self._early_history = getattr(hook_agent, "history", None)
                                except Exception:
                                    self._early_history = None
                                self._stop_reason = "save_no_response"
                                raise _AgentEarlyStop()
                        except _AgentEarlyStop:
                            raise
                        except Exception:
                            pass
                    if int(self._submit_repeat_count or 0) >= 3:
                        exp_hint = ""
                        try:
                            csn = int(self._case_step_last_seen or 0)
                        except Exception:
                            csn = 0
                        if csn > 0:
                            try:
                                s0 = self._find_case_step_by_number(int(csn))
                                exp_hint = self._format_expected_result(str(getattr(s0, "expected_result", "") or ""))
                            except Exception:
                                exp_hint = ""
                        summary = f"提交/保存操作疑似无响应：连续{int(self._submit_repeat_count)}次执行“{description}”，页面未出现预期提示/状态变化。"
                        if exp_hint:
                            summary = (summary + f"\n预期：{exp_hint}").strip()
                        if url_now:
                            summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                        created_bug_id = None
                        try:
                            created_bug_id = await self._create_bug_for_assertion_async(
                                assertion_summary=summary[:1800],
                                suggested_title=f"[AI执行][提交无响应] {self._case_title or ''}".strip()[:120],
                                suggested_description="多次点击保存/提交无效且未出现预期提示/状态变化，疑似前端按钮无效/校验无提示/接口未触发。",
                            )
                        except Exception:
                            created_bug_id = None
                        if created_bug_id:
                            try:
                                self._forced_bug_id = int(created_bug_id)
                            except Exception:
                                pass
                        try:
                            em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                            await self._mark_step_failed_async(int(step_number), em[:1800])
                        except Exception:
                            pass
                        try:
                            await self._append_step_note_async(int(step_number), "阻断：检测到提交/保存反复无响应，已登记缺陷并停止（避免无限重试）")
                            await self._merge_stop_metrics_async(
                                int(step_number),
                                {"stopped_reason": "submit_no_effect", "submit_repeat": int(self._submit_repeat_count), "current_url": str(url_now or "")[:500]},
                            )
                        except Exception:
                            pass
                        try:
                            hook_agent.stop()
                        except Exception:
                            pass
                        try:
                            self._early_history = getattr(hook_agent, "history", None)
                        except Exception:
                            self._early_history = None
                        self._stop_reason = "submit_no_effect"
                        raise _AgentEarlyStop()
                else:
                    self._submit_repeat_sig = ""
                    self._submit_repeat_count = 0
                    self._submit_repeat_last_url = ""
                    self._submit_repeat_last_msg_count = 0
                    self._submit_repeat_last_effect = ""
                    try:
                        is_wait_action = str(description or "").startswith("等待:")
                    except Exception:
                        is_wait_action = False
                    if (not is_wait_action) and (("click" in str(action_script or "").lower()) or ("press_key" in str(action_script or "").lower()) or ("点击" in str(description or ""))):
                        url_now = ""
                        try:
                            page = await self._get_active_page_async()
                            url_now = str(getattr(page, "url", "") or "")
                        except Exception:
                            url_now = ""
                        net_now = 0
                        try:
                            net_now = int(await self._get_network_entry_count_async())
                        except Exception:
                            net_now = 0
                        new_msgs = []
                        try:
                            new_msgs = await self._collect_new_runtime_messages_async()
                        except Exception:
                            new_msgs = []
                        if new_msgs:
                            try:
                                self._runtime_messages.extend(new_msgs)
                            except Exception:
                                pass
                        sig = ""
                        try:
                            act0 = action_value
                            if isinstance(act0, list) and act0:
                                act0 = act0[0]
                            if isinstance(act0, dict) and act0:
                                name0 = next(iter(act0.keys()), "")
                                payload0 = act0.get(name0) if name0 else None
                                if name0 == "click_element_by_index":
                                    sig = "click_element_by_index"
                                elif name0 == "press_key" and isinstance(payload0, dict):
                                    sig = f"press_key:{str(payload0.get('key') or '')[:24]}"
                                elif name0 == "input_text":
                                    sig = "input_text"
                                else:
                                    sig = str(name0 or "")
                        except Exception:
                            sig = ""
                        if not sig:
                            sig = f"{self._norm_text(description or '')}|{self._norm_text(action_script or '')}"[:240]
                        click_like = False
                        try:
                            click_like = ("click" in str(sig).lower()) or ("press_key" in str(sig).lower()) or ("点击" in str(description or ""))
                        except Exception:
                            click_like = False
                        if click_like:
                            sig = "CLICK_LIKE"
                        effect_token = ""
                        try:
                            effect_token = await self._build_no_effect_token_async(page, url_now, net_now)
                        except Exception:
                            effect_token = f"u={str(url_now or '')[:300]}|n={int(net_now or 0)}"
                        try:
                            if str(effect_token or "") and str(effect_token or "") == str(self._no_effect_token_last or ""):
                                self._no_effect_streak = int(self._no_effect_streak or 0) + 1
                            else:
                                self._no_effect_token_last = str(effect_token or "")[:900]
                                self._no_effect_streak = 1 if str(effect_token or "") else 0
                        except Exception:
                            pass
                        if (sig == str(self._action_repeat_sig or "")) and str(effect_token or "") and (str(effect_token or "") == str(self._action_repeat_last_effect or "")):
                            self._action_repeat_count = int(self._action_repeat_count or 0) + 1
                        else:
                            self._action_repeat_sig = sig
                            self._action_repeat_last_url = str(url_now or "")[:500]
                            try:
                                self._action_repeat_last_msg_count = int(len(self._runtime_messages or []))
                            except Exception:
                                self._action_repeat_last_msg_count = 0
                            self._action_repeat_last_effect = str(effect_token or "")[:900]
                            self._action_repeat_count = 1
                        self._last_effect_url = str(url_now or "")[:500]
                        try:
                            self._last_effect_net_count = int(net_now)
                        except Exception:
                            self._last_effect_net_count = int(net_now)
                        try:
                            desc_s = str(description or "")
                            thought_s = str(ai_thought or "")
                            combo_s = (desc_s + "\n" + thought_s).strip()
                            combo_l = combo_s.lower()
                            is_save_like = any(k in combo_s for k in ["保存", "提交", "确认", "确定"]) or any(k in combo_l for k in ["save", "submit", "confirm", "ok"])
                        except Exception:
                            is_save_like = False
                        if is_save_like:
                            dom_sig = ""
                            try:
                                dom_sig = await self._compute_dom_effect_sig_async(page)
                            except Exception:
                                dom_sig = ""
                            save_effect = f"u={str(url_now or '')[:300]}|d={str(dom_sig or '')[:900]}"
                            success_msg = ""
                            try:
                                for m in reversed((new_msgs or [])[-8:]):
                                    ms = str(m or "").strip()
                                    if not ms:
                                        continue
                                    lower = ms.lower()
                                    if any(k in ms for k in ["成功", "新增成功", "创建成功", "保存成功", "提交成功", "操作成功"]) or ("success" in lower):
                                        success_msg = ms[:200]
                                        break
                                if not success_msg:
                                    for m in reversed((self._runtime_messages or [])[-12:]):
                                        ms = str(m or "").strip()
                                        if not ms:
                                            continue
                                        lower = ms.lower()
                                        if any(k in ms for k in ["成功", "新增成功", "创建成功", "保存成功", "提交成功", "操作成功"]) or ("success" in lower):
                                            success_msg = ms[:200]
                                            break
                            except Exception:
                                success_msg = ""
                            submit_net = ""
                            try:
                                recent_net = await self._collect_recent_network_entries_async(int(step_number), limit=60)
                                for e in recent_net or []:
                                    u = str(e.get("url") or "").lower()
                                    m = str(e.get("method") or "").upper()
                                    st = int(e.get("status") or 0)
                                    if m in ("POST", "PUT", "PATCH"):
                                        if any(k in u for k in ["login", "auth", "token", "session"]):
                                            continue
                                        submit_net = f"{m} {st} {str(e.get('url') or '')[:180]}"
                                        break
                            except Exception:
                                submit_net = ""
                            try:
                                feedback_msgs = []
                                try:
                                    feedback_msgs = [m for m in (new_msgs or []) if self._is_feedback_message(m)]
                                except Exception:
                                    feedback_msgs = []
                                prompt_seen = bool(success_msg) or bool(feedback_msgs)
                                response_seen = bool(submit_net) or prompt_seen
                                should_stop_save = self._record_save_like_observation(prompt_seen=prompt_seen, response_seen=response_seen, save_effect=save_effect)
                            except Exception:
                                should_stop_save = False
                            try:
                                elapsed = 0.0
                                try:
                                    if float(self._save_like_started_at or 0.0) > 0:
                                        elapsed = float(time.time() - float(self._save_like_started_at or 0.0))
                                except Exception:
                                    elapsed = 0.0
                                if bool(should_stop_save):
                                    summary = "点击保存/提交两次后仍无任何提示（成功/失败/校验错误等），疑似按钮无效或前端校验/后端异常未反馈。"
                                    if url_now:
                                        summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                                    created_bug_id = None
                                    try:
                                        created_bug_id = await self._create_bug_for_assertion_async(
                                            assertion_summary=summary[:1800],
                                            suggested_title=f"[AI执行][保存无响应] {self._case_title or ''}".strip()[:120],
                                            suggested_description="连续两次点击保存/提交后仍无任何可见提示（成功/失败/校验错误等），疑似按钮无效、校验无提示或异常未反馈。",
                                        )
                                    except Exception:
                                        created_bug_id = None
                                    if created_bug_id:
                                        try:
                                            self._forced_bug_id = int(created_bug_id)
                                        except Exception:
                                            pass
                                    try:
                                        em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                                        await self._mark_step_failed_async(int(step_number), em[:1800])
                                    except Exception:
                                        pass
                                    try:
                                        await self._append_step_note_async(int(step_number), "阻断：两次保存/提交无提示，已登记缺陷并停止（避免无限重试）")
                                        await self._merge_stop_metrics_async(
                                            int(step_number),
                                            {"stopped_reason": "save_no_response", "save_no_prompt_clicks": int(self._save_like_no_prompt_clicks), "repeat": int(self._save_like_no_success), "current_url": str(url_now or "")[:500]},
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        hook_agent.stop()
                                    except Exception:
                                        pass
                                    try:
                                        self._early_history = getattr(hook_agent, "history", None)
                                    except Exception:
                                        self._early_history = None
                                    self._stop_reason = "save_no_response"
                                    raise _AgentEarlyStop()
                            except _AgentEarlyStop:
                                raise
                            except Exception:
                                pass
                        loop_thresh = 6
                        try:
                            if str(sig) in ("click_element_by_index", "CLICK_LIKE"):
                                loop_thresh = 4
                        except Exception:
                            loop_thresh = 6
                        try:
                            if int(self._no_effect_streak or 0) >= 8:
                                summary = f"检测到疑似无响应死循环：连续{int(self._no_effect_streak)}次点击/按键后页面无明显变化（无提示/无跳转）。"
                                if url_now:
                                    summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                                created_bug_id = None
                                try:
                                    created_bug_id = await self._create_bug_for_assertion_async(
                                        assertion_summary=summary[:1800],
                                        suggested_title=f"[AI执行][无响应死循环] {self._case_title or ''}".strip()[:120],
                                        suggested_description="AI多次尝试点击/按键但页面无提示/无跳转/无状态变化，疑似按钮无效、事件未绑定或接口未触发。",
                                    )
                                except Exception:
                                    created_bug_id = None
                                if created_bug_id:
                                    try:
                                        self._forced_bug_id = int(created_bug_id)
                                    except Exception:
                                        pass
                                try:
                                    em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                                    await self._mark_step_failed_async(int(step_number), em[:1800])
                                except Exception:
                                    pass
                                try:
                                    await self._append_step_note_async(int(step_number), "阻断：检测到持续无响应死循环，已登记缺陷并停止（避免无限重试）")
                                    await self._merge_stop_metrics_async(
                                        int(step_number),
                                        {"stopped_reason": "no_effect_streak", "repeat": int(self._no_effect_streak), "current_url": str(url_now or "")[:500]},
                                    )
                                except Exception:
                                    pass
                                try:
                                    hook_agent.stop()
                                except Exception:
                                    pass
                                try:
                                    self._early_history = getattr(hook_agent, "history", None)
                                except Exception:
                                    self._early_history = None
                                self._stop_reason = "no_effect_streak"
                                raise _AgentEarlyStop()
                        except _AgentEarlyStop:
                            raise
                        except Exception:
                            pass
                        if int(self._action_repeat_count or 0) >= int(loop_thresh):
                            summary = f"检测到疑似死循环：连续{int(self._action_repeat_count)}次执行相同行为“{description}”，页面无明显变化（无提示/无跳转）。"
                            if url_now:
                                summary = (summary + f"\n当前URL：{url_now[:500]}").strip()
                            created_bug_id = None
                            try:
                                created_bug_id = await self._create_bug_for_assertion_async(
                                    assertion_summary=summary[:1800],
                                    suggested_title=f"[AI执行][操作无响应] {self._case_title or ''}".strip()[:120],
                                    suggested_description="AI重复执行相同行为但页面无提示/无跳转/无状态变化，疑似按钮无效、事件未绑定或接口未触发。",
                                )
                            except Exception:
                                created_bug_id = None
                            if created_bug_id:
                                try:
                                    self._forced_bug_id = int(created_bug_id)
                                except Exception:
                                    pass
                            try:
                                em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{summary}".strip() if self._forced_bug_id else summary
                                await self._mark_step_failed_async(int(step_number), em[:1800])
                            except Exception:
                                pass
                            try:
                                await self._append_step_note_async(int(step_number), "阻断：检测到重复动作无效（疑似死循环），已登记缺陷并停止")
                                await self._merge_stop_metrics_async(
                                    int(step_number),
                                    {"stopped_reason": "action_loop_no_effect", "repeat": int(self._action_repeat_count), "current_url": str(url_now or "")[:500]},
                                )
                            except Exception:
                                pass
                            try:
                                hook_agent.stop()
                            except Exception:
                                pass
                            try:
                                self._early_history = getattr(hook_agent, "history", None)
                            except Exception:
                                self._early_history = None
                            self._stop_reason = "action_loop_no_effect"
                            raise _AgentEarlyStop()
                    else:
                        if not is_wait_action:
                            self._action_repeat_sig = ""
                            self._action_repeat_count = 0
                            self._action_repeat_last_url = ""
                            self._action_repeat_last_effect = ""
                            self._no_effect_token_last = ""
                            self._no_effect_streak = 0
                if done_set:
                    self._case_steps_done.update(set(int(x) for x in done_set if int(x) > 0))
                    await self._merge_step_metrics_async(int(step_number), {"case_steps_done_count": int(len(self._case_steps_done or []))})
                    await self._append_step_note_async(step_number, f"用例步骤完成标记：{sorted(list(self._case_steps_done))}")
                    newly_done = []
                    try:
                        newly_done = [int(x) for x in done_set if int(x) > 0 and int(x) not in set(self._case_steps_asserted or set())]
                    except Exception:
                        newly_done = []
                    for sn in sorted(list(set(newly_done))):
                        try:
                            ok, summary, bug_id = await self._assert_expected_for_case_step_async(int(sn), int(step_number))
                        except Exception:
                            ok, summary, bug_id = True, "", None
                        if ok:
                            try:
                                if int(sn) not in set(self._non_blocking_escalated or set()):
                                    notes = (self._non_blocking_by_case_step or {}).get(int(sn)) or []
                                    decision_nb = self._stop_policy.decide_after_non_blocking_escalation(
                                        self._stop_policy.should_escalate_non_blocking_on_step_done(bool(notes))
                                    )
                                    if decision_nb.stop:
                                        evidence = "；".join([str(x) for x in notes][-6:])[:800]
                                        escalation_summary = f"步骤{int(sn)} 非阻塞问题在步骤完成后仍存在，判定缺陷\n证据：{evidence}"
                                        try:
                                            created_bug_id = await self._create_bug_for_assertion_async(
                                                assertion_summary=escalation_summary,
                                                suggested_title=f"[AI执行][非阻塞转阻断] {self._case_title or ''}".strip()[:120],
                                                suggested_description="非阻塞问题在用例步骤完成后仍存在，按规则升级为阻塞并停止。",
                                            )
                                        except Exception:
                                            created_bug_id = None
                                        if created_bug_id:
                                            try:
                                                self._forced_bug_id = int(created_bug_id)
                                            except Exception:
                                                pass
                                            try:
                                                self._assert_failed_step_number = int(step_number)
                                                self._assert_failed_summary = str(escalation_summary or "")[:1800]
                                            except Exception:
                                                pass
                                            try:
                                                em = f"发现缺陷：BUG-{int(self._forced_bug_id)}\n{escalation_summary}".strip()
                                                await self._mark_step_failed_async(int(step_number), em)
                                            except Exception:
                                                pass
                                            try:
                                                self._stop_reason = "non_blocking_bug"
                                                await self._append_step_note_async(step_number, f"阻断：非阻塞问题在用例步骤{int(sn)}完成后仍存在，终止执行并登记缺陷")
                                                await self._append_step_note_async(step_number, escalation_summary)
                                                await self._merge_stop_metrics_async(
                                                    int(step_number),
                                                    {
                                                        "stopped_reason": "non_blocking_bug",
                                                        "assert_case_step": int(sn),
                                                        "assert_summary": str(escalation_summary or "")[:1200],
                                                        "bug_id": int(self._forced_bug_id or 0),
                                                    },
                                                )
                                            except Exception:
                                                pass
                                            try:
                                                self._non_blocking_escalated.add(int(sn))
                                            except Exception:
                                                pass
                                            try:
                                                hook_agent.stop()
                                            except Exception:
                                                pass
                                            try:
                                                self._early_history = getattr(hook_agent, "history", None)
                                            except Exception:
                                                self._early_history = None
                                            raise _AgentEarlyStop()
                            except _AgentEarlyStop:
                                raise
                            except Exception:
                                pass
                            try:
                                self._case_steps_asserted.add(int(sn))
                            except Exception:
                                pass
                            continue
                        try:
                            self._case_steps_asserted.add(int(sn))
                        except Exception:
                            pass
                        try:
                            if bug_id:
                                self._forced_bug_id = int(bug_id)
                        except Exception:
                            pass
                        try:
                            self._assert_failed_step_number = int(step_number)
                            self._assert_failed_summary = str(summary or "")[:1800]
                        except Exception:
                            pass
                        try:
                            em = f"发现缺陷：BUG-{int(self._forced_bug_id)}"
                            if summary:
                                em = (em + "\n" + str(summary)).strip()
                            await self._mark_step_failed_async(int(step_number), em)
                        except Exception:
                            pass
                        try:
                            self._stop_reason = self._stop_reason or "assert_failed"
                            await self._append_step_note_async(step_number, f"阻断：预期不满足（用例步骤{int(sn)}），终止执行并登记缺陷")
                            if summary:
                                await self._append_step_note_async(step_number, summary)
                            await self._merge_stop_metrics_async(
                                int(step_number),
                                {
                                    "stopped_reason": "assert_failed",
                                    "assert_case_step": int(sn),
                                    "assert_summary": str(summary or "")[:1200],
                                    "bug_id": int(bug_id or 0),
                                },
                            )
                        except Exception:
                            pass
                        try:
                            hook_agent.stop()
                        except Exception:
                            pass
                        try:
                            self._early_history = getattr(hook_agent, "history", None)
                        except Exception:
                            self._early_history = None
                        raise _AgentEarlyStop()

                    total_steps2 = 0
                    try:
                        total_steps2 = int(self._case_steps_total or len(self._testcase_steps or []))
                    except Exception:
                        total_steps2 = int(len(self._testcase_steps or []))
                    try:
                        if total_steps2 > 0 and int(len(self._case_steps_done or [])) >= int(total_steps2) and not self._stop_reason:
                            self._stop_reason = "steps_completed"
                            self._stopped_by_ai = True
                            await self._append_step_note_async(step_number, f"已完成全部用例步骤（{int(total_steps2)}/{int(total_steps2)}），自动停止执行")
                            try:
                                await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "steps_completed", "case_steps_total": int(total_steps2), "case_steps_done": int(len(self._case_steps_done or []))})
                            except Exception:
                                pass
                            try:
                                hook_agent.stop()
                            except Exception:
                                pass
                            try:
                                self._early_history = getattr(hook_agent, "history", None)
                            except Exception:
                                self._early_history = None
                            raise _AgentEarlyStop()
                    except _AgentEarlyStop:
                        raise
                    except Exception:
                        pass

                case_step_no = int(self._case_step_last_seen or 0)
                if case_step_no > 0 and int(case_step_no) not in set(self._case_steps_asserted or set()):
                    did_submit_like_check = False
                    try:
                        if self._is_submit_like_action(description or "", action_script or "", ai_thought or ""):
                            did_submit_like_check = True
                            total_steps0 = 0
                            try:
                                total_steps0 = int(self._case_steps_total or len(self._testcase_steps or []))
                            except Exception:
                                total_steps0 = int(len(self._testcase_steps or []))
                            try:
                                combo = f"{str(description or '')}\n{str(ai_thought or '')}"
                                combo_l = combo.lower()
                                save_like = any(k in combo for k in ["保存", "提交", "确认", "确定"]) or any(k in combo_l for k in ["save", "submit", "confirm", "ok"])
                            except Exception:
                                save_like = False
                            if save_like and total_steps0 > 0:
                                try:
                                    if int(case_step_no) < int(total_steps0):
                                        nxt = self._find_case_step_by_number(int(case_step_no) + 1)
                                        nxt_desc = str(getattr(nxt, "description", "") or "")
                                        if nxt_desc and any(k in nxt_desc for k in ["保存", "提交", "确认", "确定"]):
                                            case_step_no = int(case_step_no) + 1
                                except Exception:
                                    pass
                            page = await self._get_active_page_async()
                            try:
                                if page:
                                    await page.wait_for_timeout(int(self._stop_policy.submit_wait_ms()))
                            except Exception:
                                pass
                            okb, sumb, bugb = await self._assert_expected_for_case_step_async(
                                int(case_step_no),
                                int(step_number),
                                strict=True,
                                create_bug=True,
                            )
                            try:
                                if bool(okb) and bool(sumb) and int(total_steps0 or 0) > 0 and int(case_step_no) >= int(total_steps0) and not self._stop_reason:
                                    try:
                                        self._case_steps_done.update(set(range(1, int(total_steps0) + 1)))
                                    except Exception:
                                        pass
                                    self._stop_reason = "steps_completed"
                                    self._stopped_by_ai = True
                                    await self._append_step_note_async(step_number, f"最终步骤断言通过（用例步骤{int(case_step_no)}），自动结束执行")
                                    try:
                                        await self._merge_stop_metrics_async(
                                            int(step_number),
                                            {"stopped_reason": "steps_completed", "by": "final_assert", "assert_case_step": int(case_step_no), "case_steps_total": int(total_steps0)},
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        hook_agent.stop()
                                    except Exception:
                                        pass
                                    try:
                                        self._early_history = getattr(hook_agent, "history", None)
                                    except Exception:
                                        self._early_history = None
                                    raise _AgentEarlyStop()
                            except _AgentEarlyStop:
                                raise
                            except Exception:
                                pass
                            decision = self._stop_policy.decide_after_blocking_check(bool(okb))
                            if decision.stop:
                                try:
                                    self._case_steps_asserted.add(int(case_step_no))
                                except Exception:
                                    pass
                                try:
                                    if bugb:
                                        self._forced_bug_id = int(bugb)
                                except Exception:
                                    pass
                                try:
                                    self._assert_failed_step_number = int(step_number)
                                    self._assert_failed_summary = str(sumb or "")[:1800]
                                except Exception:
                                    pass
                                try:
                                    em = f"发现缺陷：BUG-{int(self._forced_bug_id)}"
                                    if sumb:
                                        em = (em + "\n" + str(sumb)).strip()
                                    await self._mark_step_failed_async(int(step_number), em)
                                except Exception:
                                    pass
                                try:
                                    self._stop_reason = self._stop_reason or "assert_failed"
                                    await self._append_step_note_async(step_number, f"阻断：预期不满足（用例步骤{int(case_step_no)}），终止执行并登记缺陷")
                                    if sumb:
                                        await self._append_step_note_async(step_number, sumb)
                                    await self._merge_stop_metrics_async(
                                        int(step_number),
                                        {
                                            "stopped_reason": "assert_failed",
                                            "assert_case_step": int(case_step_no),
                                            "assert_summary": str(sumb or "")[:1200],
                                            "bug_id": int(bugb or 0),
                                        },
                                    )
                                except Exception:
                                    pass
                                try:
                                    hook_agent.stop()
                                except Exception:
                                    pass
                                try:
                                    self._early_history = getattr(hook_agent, "history", None)
                                except Exception:
                                    self._early_history = None
                                raise _AgentEarlyStop()

                            if int(case_step_no) not in set(self._case_steps_soft_checked or set()):
                                ok_soft, sum_soft, _ = await self._assert_expected_for_case_step_async(
                                    int(case_step_no),
                                    int(step_number),
                                    strict=True,
                                    create_bug=False,
                                )
                                if not ok_soft:
                                    try:
                                        self._case_steps_soft_checked.add(int(case_step_no))
                                    except Exception:
                                        pass
                                    try:
                                        self._note_non_blocking_issue(f"Step{int(step_number)}: 预期暂未满足（用例步骤{int(case_step_no)}）")
                                    except Exception:
                                        pass
                                    try:
                                        await self._append_step_note_async(
                                            step_number,
                                            f"非阻塞断言：暂未观测到预期提示/状态（用例步骤{int(case_step_no)}），将继续执行；若用例步骤完成后仍不满足则登记缺陷并停止",
                                        )
                                        if sum_soft:
                                            await self._append_step_note_async(step_number, sum_soft)
                                        await self._merge_step_metrics_async(
                                            int(step_number),
                                            {"non_blocking_assert": True, "non_blocking_case_step": int(case_step_no)},
                                        )
                                    except Exception:
                                        pass
                    except _AgentEarlyStop:
                        raise
                    except Exception:
                        pass
                    if not did_submit_like_check:
                        try:
                            ok2, summary2, bug_id2 = await self._assert_expected_for_case_step_async(int(case_step_no), int(step_number), strict=False)
                        except Exception:
                            ok2, summary2, bug_id2 = True, "", None
                    else:
                        ok2, summary2, bug_id2 = True, "", None
                    if not ok2:
                        try:
                            self._case_steps_asserted.add(int(case_step_no))
                        except Exception:
                            pass
                        try:
                            if bug_id2:
                                self._forced_bug_id = int(bug_id2)
                        except Exception:
                            pass
                        try:
                            self._assert_failed_step_number = int(step_number)
                            self._assert_failed_summary = str(summary2 or "")[:1800]
                        except Exception:
                            pass
                        try:
                            em = f"发现缺陷：BUG-{int(self._forced_bug_id)}"
                            if summary2:
                                em = (em + "\n" + str(summary2)).strip()
                            await self._mark_step_failed_async(int(step_number), em)
                        except Exception:
                            pass
                        try:
                            self._stop_reason = self._stop_reason or "assert_failed"
                            await self._append_step_note_async(step_number, f"阻断：预期不满足（用例步骤{int(case_step_no)}），终止执行并登记缺陷")
                            if summary2:
                                await self._append_step_note_async(step_number, summary2)
                            await self._merge_stop_metrics_async(
                                int(step_number),
                                {
                                    "stopped_reason": "assert_failed",
                                    "assert_case_step": int(case_step_no),
                                    "assert_summary": str(summary2 or "")[:1200],
                                    "bug_id": int(bug_id2 or 0),
                                },
                            )
                        except Exception:
                            pass
                        try:
                            hook_agent.stop()
                        except Exception:
                            pass
                        try:
                            self._early_history = getattr(hook_agent, "history", None)
                        except Exception:
                            self._early_history = None
                        raise _AgentEarlyStop()
            try:
                case_step_no = int(self._case_step_last_seen or 0)
                if case_step_no > 0 and self._case_step_requires_upload_file(case_step_no) and int(case_step_no) not in set(self._transfer_file_applied_steps or set()):
                    s = self._find_case_step_by_number(case_step_no)
                    payload = self._get_transfer_file_payload(s)
                    if payload:
                        page = await self._get_active_page_async()
                        try:
                            await self._ensure_filechooser_autofill_async(page, payload, int(case_step_no))
                        except Exception:
                            pass
                        res = await self._try_apply_transfer_file_async(page, payload)
                        if res.get("success"):
                            sel = {}
                            net = {}
                            loading = {}
                            try:
                                await page.wait_for_timeout(250)
                            except Exception:
                                pass
                            try:
                                sel = await self._detect_file_input_selection_async(page, str(payload.get("name") or ""))
                            except Exception:
                                sel = {}
                            try:
                                net = self._detect_recent_upload_request_suspect(int(step_number), str(payload.get("name") or ""))
                            except Exception:
                                net = {}
                            try:
                                loading = await self._detect_loading_indicators_async(page)
                            except Exception:
                                loading = {}
                            matched_cnt = 0
                            try:
                                matched_cnt = int((sel or {}).get("matchedCount") or 0)
                            except Exception:
                                matched_cnt = 0
                            level = "selected_uncertain"
                            if matched_cnt > 0:
                                level = "selected_ok"
                            try:
                                hits = [str(x).lower() for x in ((loading or {}).get("hits") or [])]
                                if any(("upload" in h) or ("上传" in h) or ("uploading" in h) or ("generating" in h) for h in hits):
                                    if level == "selected_ok":
                                        level = "upload_suspected"
                            except Exception:
                                pass
                            try:
                                if (net or {}).get("suspected") and level == "selected_ok":
                                    level = "upload_suspected"
                            except Exception:
                                pass
                            if matched_cnt > 0:
                                self._transfer_file_applied_steps.add(int(case_step_no))
                            await self._append_step_note_async(step_number, f"已为用例步骤{int(case_step_no)}自动选择文件：{payload.get('name')}（{level}）")
                            await self._merge_step_metrics_async(
                                int(step_number),
                                {
                                    "transfer_file_used": True,
                                    "transfer_file_name": str(payload.get("name") or ""),
                                    "transfer_file_content_type": str(payload.get("mimeType") or ""),
                                    "transfer_file_size": int(len(payload.get("buffer") or b"")),
                                    "file_input_count": int(res.get("file_input_count") or 0),
                                    "transfer_file_level": str(level),
                                    "transfer_file_selection": sel,
                                    "transfer_file_network_suspect": net,
                                    "transfer_file_loading": loading,
                                },
                            )
                            try:
                                if level == "selected_ok":
                                    await self._inject_hint_overlay_async(page, f"已自动选择文件：{payload.get('name')}。请确认页面出现文件名/上传进度，再继续点击下一步。")
                                else:
                                    await self._inject_hint_overlay_async(page, f"已尝试自动选择文件：{payload.get('name')}，但未看到选中文件证据。请点击“Browse/上传/选择文件”后再重试。")
                            except Exception:
                                pass
                        else:
                            await self._merge_step_metrics_async(
                                int(step_number),
                                {
                                    "transfer_file_used": False,
                                    "transfer_file_name": str(payload.get("name") or ""),
                                    "file_input_count": int(res.get("file_input_count") or 0),
                                    "transfer_file_error": str(res.get("reason") or "")[:200],
                                },
                            )
                    else:
                        await self._merge_step_metrics_async(int(step_number), {"transfer_file_missing": True, "case_step_current": int(case_step_no)})
            except Exception:
                pass
            try:
                s = (action_script or "").lower()
                is_done_action = ("\"done\"" in s) or ("'done'" in s) or (str(description or "") == "完成")
                if is_done_action and not self._stop_reason:
                    total_steps = int(self._case_steps_total or len(self._testcase_steps or []))
                    done_cnt = int(len(self._case_steps_done or []))
                    if total_steps and done_cnt < total_steps:
                        self._stop_reason = "ai_early_done"
                        await self._append_step_note_async(step_number, f"检测到 AI 提前结束：已完成用例步骤 {done_cnt}/{total_steps}，视为未按步骤执行")
                        await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": "ai_early_done", "case_steps_done": done_cnt, "case_steps_total": total_steps})
            except Exception:
                pass
            try:
                s = (action_script or "").lower()
                is_scroll_action = ("scroll" in s) or ("滚动" in str(description or ""))
                if is_scroll_action:
                    total_steps = int(self._case_steps_total or len(self._testcase_steps or []))
                    cur_step_no = int(self._case_step_last_seen or 0)
                    cur_step_desc = ""
                    try:
                        if cur_step_no > 0:
                            for ss in (self._testcase_steps or []):
                                if int(getattr(ss, "step_number", 0) or 0) == cur_step_no:
                                    cur_step_desc = str(getattr(ss, "description", "") or "")
                                    break
                    except Exception:
                        cur_step_desc = ""
                    combo = f"{str(description or '')}\n{str(ai_thought or '')}\n{cur_step_desc}"
                    allowed = False
                    try:
                        if any(k in combo for k in ["滚动", "下拉", "翻页", "加载更多", "懒加载", "查看更多"]):
                            allowed = True
                        if any(k in combo for k in ["找", "寻找", "查找", "未看到", "看不到", "元素不在视口", "需要滚动"]):
                            allowed = True
                        if "element index" in (error_message or "").lower():
                            allowed = True
                    except Exception:
                        allowed = True
                    if not allowed:
                        self._scroll_policy_violations = int(self._scroll_policy_violations or 0) + 1
                        await self._append_step_note_async(step_number, "滚动策略提醒：检测到无明确原因的滚动（建议仅在需要找元素/步骤要求时滚动）")
                        await self._merge_step_metrics_async(int(step_number), {"scroll_policy_violations": int(self._scroll_policy_violations)})
            except Exception:
                pass
            try:
                s = (action_script or "").lower()
                is_click = ("click_element" in s) or ("点击" in str(description or "")) or ("click" in s)
                if is_click:
                    cur_step_no = int(self._case_step_last_seen or 0)
                    cur_step_desc = ""
                    try:
                        if cur_step_no > 0:
                            for ss in (self._testcase_steps or []):
                                if int(getattr(ss, "step_number", 0) or 0) == cur_step_no:
                                    cur_step_desc = str(getattr(ss, "description", "") or "")
                                    break
                    except Exception:
                        cur_step_desc = ""
                    combo = f"{cur_step_desc}\n{str(description or '')}\n{str(ai_thought or '')}"
                    is_slide = ("幻灯片" in combo) or ("slide" in combo.lower()) or ("slides" in combo.lower())
                    if is_slide:
                        nums = []
                        try:
                            nums = [int(x) for x in re.findall(r"第\s*(\d{1,3})\s*张", combo)]
                            if not nums:
                                nums = [int(x) for x in re.findall(r"(\d{1,3})\s*张", combo)]
                        except Exception:
                            nums = []
                        expected_set = set([n for n in nums if 1 <= n <= 300])
                        if expected_set:
                            page = await self._get_active_page_async()
                            sel = await self._detect_selected_item_index_async(page)
                            got = sel.get("index") if isinstance(sel, dict) else None
                            if got and int(got) not in expected_set:
                                await self._append_step_note_async(step_number, f"聚焦校验：期望聚焦到幻灯片{sorted(list(expected_set))}，但检测到当前疑似聚焦为第{int(got)}项（{sel.get('text') or ''}）")
                                self._note_non_blocking_issue(f"聚焦不一致：期望{sorted(list(expected_set))}，实际{int(got)}")
                                await self._merge_step_metrics_async(int(step_number), {"focus_expected": sorted(list(expected_set))[:10], "focus_detected": int(got), "focus_detected_text": str(sel.get('text') or '')[:80]})
                            elif got and int(got) in expected_set:
                                key = f"slide:{int(self._case_step_last_seen or 0)}:{','.join([str(x) for x in sorted(list(expected_set))])}"
                                self._confirm_loop_counts[key] = int(self._confirm_loop_counts.get(key) or 0) + 1
                                await self._merge_step_metrics_async(int(step_number), {"focus_expected": sorted(list(expected_set))[:10], "focus_detected": int(got), "focus_confirm_count": int(self._confirm_loop_counts[key])})
                                if int(self._confirm_loop_counts[key]) >= 2:
                                    try:
                                        await self._inject_hint_overlay_async(page, f"已检测到当前聚焦为第{int(got)}项，避免反复确认，请继续下一用例步骤。")
                                    except Exception:
                                        pass
                    is_template = any(k in combo.lower() for k in ["template", "templates"]) or any(k in combo for k in ["模板", "模版"])
                    if is_template:
                        expected_name = self._extract_expected_name_from_text(combo)
                        page = await self._get_active_page_async()
                        cur_name = await self._detect_current_template_name_async(page)
                        if expected_name and cur_name and expected_name.lower() in cur_name.lower():
                            key = f"tpl:{int(self._case_step_last_seen or 0)}:{expected_name.lower()}"
                            self._confirm_loop_counts[key] = int(self._confirm_loop_counts.get(key) or 0) + 1
                            await self._merge_step_metrics_async(int(step_number), {"template_expected": expected_name, "template_current": cur_name, "template_confirm_count": int(self._confirm_loop_counts[key])})
                            if int(self._confirm_loop_counts[key]) >= 2:
                                try:
                                    await self._inject_hint_overlay_async(page, f"已确认模板为 {cur_name}，避免反复确认，请继续下一用例步骤。")
                                except Exception:
                                    pass
            except Exception:
                pass
            try:
                if self._wants_full_slides():
                    page = await self._get_active_page_async()
                    ss = await self._detect_slide_sidebar_state_async(page)
                    idx = ss.get("index") if isinstance(ss, dict) else None
                    cnt = ss.get("count") if isinstance(ss, dict) else None
                    try:
                        if cnt and int(cnt) > 0:
                            self._required_slide_total = int(cnt)
                    except Exception:
                        pass
                    try:
                        if idx and int(idx) > 0:
                            self._covered_slides.add(int(idx))
                    except Exception:
                        pass
                    try:
                        step_no = int(self._case_step_last_seen or 0)
                        req_slide_total = int(self._required_slide_total or 0)
                        covered_cnt = int(len(self._covered_slides or []))
                        if step_no > 0 and step_no in (self._case_steps_done or set()) and self._case_step_requires_full_slides(step_no):
                            not_enough = False
                            if req_slide_total > 0 and covered_cnt < req_slide_total:
                                not_enough = True
                            if req_slide_total <= 0 and covered_cnt < 2:
                                not_enough = True
                            if not_enough:
                                try:
                                    self._case_steps_done.discard(int(step_no))
                                except Exception:
                                    pass
                                try:
                                    await self._append_step_note_async(
                                        int(step_number),
                                        f"步骤完成标记校验：用例步骤{int(step_no)}要求遍历全部幻灯片，但当前覆盖不足（{covered_cnt}/{req_slide_total or '未知'}），暂不计为完成",
                                    )
                                except Exception:
                                    pass
                                try:
                                    await self._merge_step_metrics_async(
                                        int(step_number),
                                        {
                                            "case_step_force_incomplete": int(step_no),
                                            "slides_covered_count": covered_cnt,
                                            "slides_total": req_slide_total,
                                        },
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        await self._merge_step_metrics_async(
                            int(step_number),
                            {
                                "slide_index": int(idx or 0),
                                "slide_total": int(cnt or 0),
                                "slides_covered_count": int(len(self._covered_slides or [])),
                            },
                        )
                    except Exception:
                        pass
                    try:
                        if cnt and idx:
                            await self._append_step_note_async(
                                int(step_number),
                                f"幻灯片遍历进度：当前第{int(idx)}张 / 共{int(cnt)}张；已覆盖={sorted(list(self._covered_slides))[:12]}",
                            )
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._covered_pages.update(self._extract_page_numbers(ai_thought))
                self._covered_pages.update(self._extract_page_numbers(description))
            except Exception:
                pass
            try:
                new_msgs = await self._collect_new_runtime_messages_async()
                if new_msgs:
                    self._runtime_messages.extend(new_msgs)
                    try:
                        csn = int(self._case_step_last_seen or 0)
                    except Exception:
                        csn = 0
                    try:
                        for m in (new_msgs or [])[-10:]:
                            is_toast = False
                            try:
                                is_toast = self._is_relevant_toast_text(m)
                            except Exception:
                                is_toast = False
                            self._evidence.add("toast" if is_toast else "msg", str(m or "")[:400], {"step_number": int(step_number or 0), "case_step_no": int(csn or 0)})
                    except Exception:
                        pass
                    try:
                        for m in new_msgs[-6:]:
                            self._covered_pages.update(self._extract_page_numbers(m))
                    except Exception:
                        pass
                    await self._append_step_note_async(step_number, "提示/消息: " + "；".join(new_msgs[-5:]))
                    try:
                        for m in (new_msgs or [])[-6:]:
                            if self._is_relevant_toast_text(m):
                                self._note_non_blocking_issue(f"Step{int(step_number)}: {m}", case_step_no=int(csn or 0))
                                await self._append_step_note_async(step_number, f"发现疑似问题提示（非阻塞，将继续执行）：{m}")
                    except Exception:
                        pass
            except Exception:
                new_msgs = []
            await self._wait_if_paused_async(hook_agent, step_number)
            await self._apply_control_signals_async(hook_agent, step_number)
            try:
                if not self.stop_requested and not self._stop_reason:
                    login_failed, login_brief = await self._check_login_failed_async()
                    if login_failed and not self._case_expects_login_fail():
                        self._stop_reason = "login_failed"
                        await self._append_step_note_async(step_number, f"阻断：无法登录（{login_brief}），终止执行并登记缺陷")
                        try:
                            await self._merge_stop_metrics_async(
                                step_number,
                                {"stopped_reason": "login_failed", "login_brief": login_brief},
                            )
                        except Exception:
                            pass
                        try:
                            self._forced_bug_id = await self._create_bug_from_ai_async(
                                ai_summary=f"阻断：无法登录（{login_brief}）",
                                suggested_title=f"[AI执行][阻断] 无法登录 - {self._case_title or ''}".strip()[:120],
                                suggested_description=f"阻断：无法登录。\n原因：{login_brief}\n执行记录：AutoTestExecution#{self.execution_id}",
                            )
                        except Exception:
                            self._forced_bug_id = None
                        try:
                            hook_agent.stop()
                        except Exception:
                            pass
                        try:
                            self._early_history = getattr(hook_agent, "history", None)
                        except Exception:
                            self._early_history = None
                        raise _AgentEarlyStop()
            except _AgentManualStop:
                raise
            except _AgentEarlyStop:
                raise
            except Exception:
                pass
            try:
                relevant_msgs = False
                try:
                    relevant_msgs = any(self._is_relevant_toast_text(x) for x in (new_msgs or []))
                except Exception:
                    relevant_msgs = False
                if not relevant_msgs:
                    await self._maybe_capture_toast_from_step_screenshot_async(step_number, screenshot_data)
            except Exception:
                pass
            try:
                s = (action_script or "").lower()
                is_scroll_action = ("scroll" in s) or ("滚动" in str(description or ""))
                is_scroll_down = is_scroll_action and ("down=false" not in s)
                is_scroll_index_err = ("element index" in (error_message or "").lower()) and ("not found" in (error_message or "").lower())
                combo = f"{str(description or '')}\n{str(ai_thought or '')}"
                intent_bottom = any(k in combo for k in ["到底", "底部", "最底", "末尾", "最后", "加载更多", "懒加载", "触发加载"])
                wants_full_pagination = self._wants_full_pagination()
                if (is_scroll_index_err or (is_scroll_down and intent_bottom)) and int(step_number) >= 2:
                    until_bottom = bool(intent_bottom or wants_full_pagination)
                    await self._smart_scroll_async(step_number, until_bottom=until_bottom)
                    await self._append_step_note_async(
                        step_number,
                        "滚动补偿：已尝试滚动以恢复可见区域"
                        + ("并触发懒加载（滚到真正底部）" if until_bottom else "（不强制滚到最底）"),
                    )
                    try:
                        more_msgs = await self._collect_new_runtime_messages_async()
                        if more_msgs:
                            self._runtime_messages.extend(more_msgs)
                            await self._append_step_note_async(step_number, "滚动后提示/消息: " + "；".join(more_msgs[-3:]))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                page = await self._get_active_page_async()
                page_state = await self._detect_page_state_async(page) if page else {}
                if page_state:
                    await self._merge_step_metrics_async(int(step_number), {"page_state": page_state})
                cur = page_state.get("current") if isinstance(page_state, dict) else None
                try:
                    self._evidence.add(
                        "page_state",
                        f"current={cur}",
                        {"step_number": int(step_number or 0), "case_step_no": int(self._case_step_last_seen or 0)},
                    )
                except Exception:
                    pass
                if cur is not None:
                    if isinstance(self._page_state_last, dict) and self._page_state_last.get("current") is not None:
                        if int(self._page_state_last.get("current")) == int(cur):
                            self._page_state_stable = int(self._page_state_stable or 0) + 1
                        else:
                            self._page_state_stable = 0
                    self._page_state_last = page_state
                else:
                    self._page_state_stable = 0
                    self._page_state_last = page_state

                if self._wants_full_pagination() and int(step_number) >= 2:
                    desired_page = None
                    try:
                        t = str(combo or "")
                        if any(k in t for k in ["切换", "翻到", "跳转", "到第", "进入第"]):
                            m = re.search(r"(\d{1,3})\s*页", t)
                            if m:
                                desired_page = int(m.group(1))
                    except Exception:
                        desired_page = None

                    if desired_page and cur is not None and int(cur) != int(desired_page):
                        out = await self._goto_page_async(int(step_number), int(desired_page), max_tries=3)
                        self._pagination_attempts = int(self._pagination_attempts or 0) + 1
                        await self._merge_step_metrics_async(
                            int(step_number),
                            {"pagination_attempts": int(self._pagination_attempts), "pagination_goto": out},
                        )
                        if isinstance(out, dict) and out.get("ok"):
                            try:
                                await self._append_step_note_async(step_number, f"翻页兜底：已跳转到第{desired_page}页")
                            except Exception:
                                pass
                            try:
                                self._page_state_last = out.get("after") or self._page_state_last
                                self._page_state_stable = 0
                            except Exception:
                                pass
                    elif is_scroll_action and int(self._page_state_stable or 0) >= 2:
                        out = await self._click_next_page_async(int(step_number), max_tries=3)
                        self._pagination_attempts = int(self._pagination_attempts or 0) + 1
                        await self._merge_step_metrics_async(
                            int(step_number),
                            {"pagination_attempts": int(self._pagination_attempts), "pagination_next": out},
                        )
                        if isinstance(out, dict) and out.get("ok"):
                            try:
                                after = out.get("after") or {}
                                after_cur = after.get("current") if isinstance(after, dict) else None
                                if after_cur is not None:
                                    await self._append_step_note_async(step_number, f"翻页兜底：已翻到第{after_cur}页")
                            except Exception:
                                pass
                            try:
                                self._page_state_last = out.get("after") or self._page_state_last
                                self._page_state_stable = 0
                            except Exception:
                                pass
                        else:
                            try:
                                await self._append_step_note_async(step_number, "翻页兜底：尝试下一页未成功，将继续按后续步骤执行")
                            except Exception:
                                pass
            except _AgentManualStop:
                raise
            except _AgentEarlyStop:
                raise
            except Exception:
                pass
            try:
                should_check = False
                if int(step_number) >= 2:
                    should_check = True
                if int(step_number) >= len(self._testcase_steps or []):
                    should_check = True
                if self._seen_auth_response and int(step_number) >= 2:
                    should_check = True
                if self._toast_vision_found and int(step_number) >= 2:
                    should_check = True
                try:
                    recent = list(self._runtime_messages or [])[-6:]
                    if any(self._is_relevant_toast_text(x) for x in recent) and int(step_number) >= 2:
                        should_check = True
                except Exception:
                    pass

                if should_check and not self._stop_check_running:
                    sig = self._build_stop_check_sig(step_number)
                    now_ts = time.time()
                    if sig != self._last_stop_check_sig and (now_ts - float(self._last_stop_check_ts or 0.0)) >= 1.0:
                        self._stop_check_running = True
                        try:
                            self._last_stop_check_sig = sig
                            self._last_stop_check_ts = now_ts
                            stop_now, blocking, reason = await self._ai_should_stop_now_async(hook_agent, step_number)
                            if stop_now and blocking:
                                req_max = self._infer_required_max_page()
                                if req_max and len(self._covered_pages) < int(req_max):
                                    await self._append_step_note_async(
                                        step_number,
                                        f"检测到阻塞拟停止，但用例要求遍历全部页码：已覆盖 {len(self._covered_pages)}/{int(req_max)} 页，继续执行",
                                    )
                                    stop_now = False
                                if stop_now:
                                    if not self._stop_reason:
                                        self._stop_reason = "blocking_bug"
                                    await self._append_step_note_async(step_number, "检测到阻塞性问题，终止执行")
                                    if reason:
                                        await self._append_step_note_async(step_number, reason)
                                    try:
                                        await self._merge_stop_metrics_async(int(step_number), {"stopped_reason": str(self._stop_reason), "blocking": True, "blocking_reason": str(reason or "")[:300]})
                                    except Exception:
                                        pass
                                    try:
                                        hook_agent.stop()
                                    except Exception:
                                        pass
                                    try:
                                        self._early_history = getattr(hook_agent, "history", None)
                                    except Exception:
                                        self._early_history = None
                                    raise _AgentEarlyStop()
                        finally:
                            self._stop_check_running = False
            except Exception:
                pass

        wants_full_pagination = self._wants_full_pagination()
        req_max_page = self._infer_required_max_page()
        min_steps = int(getattr(settings, "AI_EXEC_AGENT_MIN_STEPS", 10) or 10)
        steps_per_case_step = int(getattr(settings, "AI_EXEC_AGENT_STEPS_PER_CASE_STEP", 10) or 10)
        max_steps_cap = int(getattr(settings, "AI_EXEC_AGENT_MAX_STEPS", 280) or 280)
        pagination_steps_per_page = int(getattr(settings, "AI_EXEC_PAGINATION_STEPS_PER_PAGE", 10) or 10)

        computed_steps = max(min_steps, int(len(steps) * steps_per_case_step))
        if wants_full_pagination:
            if req_max_page:
                computed_steps = max(computed_steps, int(req_max_page) * int(pagination_steps_per_page) + 30)
            else:
                computed_steps = max(computed_steps, int(max_steps_cap))
        max_steps = int(min(computed_steps, max_steps_cap)) if max_steps_cap > 0 else int(computed_steps)
        total_timeout_s = int(getattr(settings, "AI_EXEC_AGENT_TOTAL_TIMEOUT_S", 1800) or 1800)
        for attempt in range(2):
            retry_suffix = ""
            if attempt == 1:
                total_steps = int(self._case_steps_total or len(self._testcase_steps or []))
                done_steps = set(int(x) for x in (self._case_steps_done or set()) if int(x) > 0)
                missing = []
                try:
                    if total_steps > 0:
                        missing = [i for i in range(1, total_steps + 1) if i not in done_steps]
                except Exception:
                    missing = []
                retry_suffix = (
                    "\n\n继续执行：你刚才提前结束了任务，但用例步骤未全部完成。"
                    + (f"当前缺失步骤：{missing[:30]}。请从最小缺失步骤开始继续，严格补齐剩余步骤；不要调用 done。" if missing else "请继续按顺序补齐剩余步骤；不要调用 done。")
                )
                self._stop_reason = ""
                self._stopped_by_ai = False
            agent = Agent(
                task=(base_task_description + retry_suffix),
                llm=llm,
                browser=browser,
                use_vision=True,
                extend_system_message=extend_system_message,
                tools=QATools(self, display_files_in_done_text=True),
                available_file_paths=list(self._agent_available_file_paths or []),
            )
            self._active_agent = agent
            try:
                if total_timeout_s > 0:
                    history = await asyncio.wait_for(
                        agent.run(max_steps=max_steps, on_step_start=on_step_start, on_step_end=on_step_end),
                        timeout=total_timeout_s,
                    )
                else:
                    history = await agent.run(max_steps=max_steps, on_step_start=on_step_start, on_step_end=on_step_end)
            except _AgentEarlyStop:
                history = self._early_history or getattr(agent, "history", None)
            except _AgentManualStop:
                history = self._early_history or getattr(agent, "history", None)
            except asyncio.TimeoutError:
                await self._upsert_step_record_async(
                    step_number=int(self._current_agent_step or self._agent_step_seq or 1),
                    description="AI 执行超时",
                    ai_thought="执行总时长超出限制，已中止以避免卡死",
                    action_script="timeout",
                    status="failed",
                    error_message=f"执行总时长超时（>{total_timeout_s}s）",
                    metrics={},
                )
                try:
                    agent.stop()
                except Exception:
                    pass
                raise
            finally:
                self._active_agent = None
                try:
                    if self._signal_poller_task:
                        self._signal_poller_task.cancel()
                except Exception:
                    pass
            if str(self._stop_reason or "") == "ai_early_done" and attempt == 0:
                continue
            break
        
        # 6. Process Result
        try:
            reached_max = False
            try:
                reached_max = (not self._stopped_by_ai) and (not history.is_done()) and (int(self._agent_step_seq or 0) >= int(max_steps))
            except Exception:
                reached_max = False
            if reached_max:
                self._stop_reason = "max_steps"
                step_no = int(self._current_agent_step or self._agent_step_seq or 1)
                await self._append_step_note_async(
                    step_no,
                    f"达到最大步数限制（max_steps={int(max_steps)}），可能未完成检查/遍历；可提高 AI_EXEC_AGENT_MAX_STEPS/倍率后重试",
                )
                try:
                    await self._merge_stop_metrics_async(
                        step_no,
                        {"stopped_reason": "max_steps", "max_steps": int(max_steps), "executed_steps": int(self._agent_step_seq or 0)},
                    )
                except Exception:
                    pass
        except Exception:
            pass
        await self._save_history_to_db_async(history)
        try:
            await self._normalize_steps_after_assert_failed_async()
        except Exception:
            pass
        ai_judgement = None
        bug_id = None
        if self._stop_reason == "login_failed":
            ai_judgement = {"bug_found": True, "summary": "阻断：无法登录", "bug": {"title": "", "description": ""}}
            bug_id = self._forced_bug_id
        elif self._stop_reason == "assert_failed":
            ai_judgement = {"bug_found": True, "summary": "阻断：预期不满足，已终止执行", "bug": {"title": "", "description": ""}}
            bug_id = self._forced_bug_id
        elif self._stop_reason == "non_blocking_bug":
            ai_judgement = {"bug_found": True, "summary": "阻断：非阻塞问题在步骤完成后仍存在，已终止执行", "bug": {"title": "", "description": ""}}
            bug_id = self._forced_bug_id
        elif self._stop_reason == "blocking_bug":
            ai_judgement = {"bug_found": True, "summary": "阻断：检测到阻塞性问题，已终止执行", "bug": {"title": "", "description": ""}}
            try:
                bug_id = await self._create_bug_from_ai_async(
                    ai_summary=ai_judgement.get("summary") or "",
                    suggested_title=f"[AI执行][阻断] {self._case_title or ''}".strip()[:120],
                    suggested_description="阻断：执行过程中检测到无法继续的阻塞性问题。\n执行记录：AutoTestExecution#%s" % str(self.execution_id),
                )
            except Exception:
                bug_id = None
        elif self._stop_reason == "manual_stop":
            ai_judgement = {"bug_found": False, "summary": "已手动停止执行", "bug": {"title": "", "description": ""}}
            bug_id = None
        elif self._stop_reason == "steps_completed":
            ai_judgement = {"bug_found": False, "summary": "已完成全部用例步骤，自动停止执行", "bug": {"title": "", "description": ""}}
            bug_id = None
        elif self._stop_reason == "ai_early_done":
            ai_judgement = {"bug_found": False, "summary": "AI 提前结束：未按用例步骤完整执行", "bug": {"title": "", "description": ""}}
            bug_id = None
        else:
            ai_judgement = await self._ai_judge_execution_async(history)
            if ai_judgement and ai_judgement.get("bug_found"):
                try:
                    bug_id = await self._create_bug_from_ai_async(
                        ai_summary=ai_judgement.get("summary") or "",
                        suggested_title=(ai_judgement.get("bug") or {}).get("title"),
                        suggested_description=(ai_judgement.get("bug") or {}).get("description"),
                    )
                except Exception:
                    bug_id = None

        strict_step_tracking = bool(getattr(settings, "AI_EXEC_STRICT_STEP_TRACKING", False))
        final_status = "completed"
        if self._stop_reason == "manual_stop":
            final_status = "stopped"
        if final_status != "stopped" and not (history.is_done() or self._stopped_by_ai):
            final_status = "failed"
        if ai_judgement and ai_judgement.get("bug_found"):
            final_status = "failed"
        try:
            final_text_probe = history.final_result() or ""
            _, _, done_from_final = self._extract_case_step_progress(final_text_probe)
            if done_from_final:
                self._case_steps_done.update(set(int(x) for x in done_from_final if int(x) > 0))
        except Exception:
            pass
        total_steps = int(self._case_steps_total or len(self._testcase_steps or []))
        done_steps = int(len(self._case_steps_done or []))
        if strict_step_tracking and final_status != "stopped" and total_steps > 0 and done_steps < total_steps:
            final_status = "failed"
        wants_slides = False
        req_slide_total = 0
        covered_slide_cnt = 0
        slide_missing = []
        try:
            wants_slides = self._wants_full_slides()
            req_slide_total = int(self._required_slide_total or 0)
            covered_slide_cnt = int(len(self._covered_slides or []))
            if wants_slides and req_slide_total > 0 and covered_slide_cnt < req_slide_total:
                slide_missing = [i for i in range(1, req_slide_total + 1) if i not in set(int(x) for x in (self._covered_slides or set()))]
                if strict_step_tracking and final_status != "stopped":
                    final_status = "failed"
            if wants_slides and req_slide_total <= 0 and covered_slide_cnt < 2:
                if strict_step_tracking and final_status != "stopped":
                    final_status = "failed"
        except Exception:
            wants_slides = False
            req_slide_total = 0
            covered_slide_cnt = 0
            slide_missing = []

        summary_text = (ai_judgement.get("summary") if ai_judgement else "") or ""
        if not summary_text:
            final_text = history.final_result() or ""
            summary_text = f"AI结论：{final_text}".strip()
        missing = []
        if total_steps > 0 and done_steps < total_steps:
            missing = []
            try:
                missing = [i for i in range(1, total_steps + 1) if i not in set(int(x) for x in (self._case_steps_done or set()))]
            except Exception:
                missing = []
            if missing:
                summary_text = (summary_text + f"\n\n执行提示：未能确认全部用例步骤完成标记（已识别 {done_steps}/{total_steps}，缺失：{missing[:20]}）").strip()
            else:
                summary_text = (summary_text + f"\n\n执行提示：未能确认全部用例步骤完成标记（已识别 {done_steps}/{total_steps}）").strip()
        if wants_slides:
            if req_slide_total > 0 and covered_slide_cnt < req_slide_total:
                summary_text = (
                    summary_text
                    + f"\n\n执行提示：用例要求遍历全部幻灯片，但当前仅覆盖 {covered_slide_cnt}/{req_slide_total} 张"
                    + (f"，缺失：{slide_missing[:20]}" if slide_missing else "")
                ).strip()
            if req_slide_total <= 0 and covered_slide_cnt < 2:
                summary_text = (summary_text + "\n\n执行提示：用例要求遍历全部幻灯片，但未能确认已覆盖全部（覆盖过少）").strip()
        if self._non_blocking_issue_notes:
            tail = "；".join([str(x) for x in (self._non_blocking_issue_notes or [])][-8:])
            summary_text = (summary_text + "\n\n执行提示：发现非阻塞疑似问题（已记录继续执行）：" + tail).strip()
        if self._stop_reason == "max_steps":
            summary_text = (summary_text + "\n\n执行提示：达到最大步数限制，可能未完成检查/遍历").strip()
        if self._stop_reason == "login_failed":
            summary_text = (summary_text + "\n\n执行提示：登录失败阻断，已终止执行").strip()
        if self._stop_reason == "blocking_bug":
            summary_text = (summary_text + "\n\n执行提示：阻塞性问题触发自动退出").strip()
        if self._stop_reason == "ai_early_done":
            summary_text = (summary_text + "\n\n执行提示：AI 未按步骤完整执行，已判定失败").strip()
        if self._stop_reason == "manual_stop":
            summary_text = (summary_text + "\n\n执行提示：已手动停止").strip()
        if bug_id:
            summary_text = f"{summary_text}\n\n已登记缺陷：BUG-{bug_id}".strip()
        highlights = []
        try:
            verdict = "通过" if final_status == "completed" else ("停止" if final_status == "stopped" else "失败")
            highlights.append(f"结论：{verdict}")
            if bug_id:
                highlights.append(f"已登记缺陷：BUG-{bug_id}")
            if self._stop_reason:
                highlights.append(f"停止原因：{self._stop_reason}")
            if total_steps > 0:
                highlights.append(f"步骤完成：{done_steps}/{total_steps}")
            if missing:
                highlights.append(f"缺失步骤：{missing[:20]}")
            if wants_slides:
                if req_slide_total > 0:
                    highlights.append(f"幻灯片遍历：{covered_slide_cnt}/{req_slide_total}")
                else:
                    highlights.append(f"幻灯片遍历：已覆盖 {covered_slide_cnt} 张（总数未知）")
            if self._non_blocking_issue_notes:
                highlights.append(f"非阻塞疑似问题：{len(self._non_blocking_issue_notes)} 条")
        except Exception:
            highlights = []
        failure_reason_code, failure_reason_group = self._classify_failure_reason(self._stop_reason, final_status)
        first_failed_step = await self._first_failed_step_no_async()

        await self._merge_execution_summary_async(
            {
                "final_status": final_status,
                "stop_reason": self._stop_reason,
                "failure_reason_code": failure_reason_code,
                "failure_reason_group": failure_reason_group,
                "first_failed_step": int(first_failed_step or 0),
                "bug_id": bug_id,
                "highlights": highlights,
                "detail": summary_text,
                "step_completion": {"done": done_steps, "total": total_steps, "missing": missing[:200]},
                "slide_completion": {"covered": covered_slide_cnt, "total": req_slide_total, "missing": slide_missing[:200]},
                "non_blocking_issues": list(self._non_blocking_issue_notes or [])[-30:],
            }
        )
        await self._update_status_async(final_status, None)
         
        # Manually close connection (but we kill process anyway)
        await browser.stop()
        await self._stop_network_capture_async()
        if self._message_poller_task:
            try:
                self._message_poller_task.cancel()
                await asyncio.sleep(0)
            except Exception:
                pass

    @sync_to_async
    def _get_steps_and_title_async(self):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        case = execution.case
             
        title = case.title
        steps_list = list(case.steps.all().order_by('step_number'))
        ctx = {
            "batch_id": str(getattr(execution, "batch_id", "") or ""),
            "run_index": int(getattr(execution, "run_index", 1) or 1),
            "run_total": int(getattr(execution, "run_total", 1) or 1),
            "dataset_name": str(getattr(execution, "dataset_name", "") or ""),
            "dataset_vars": getattr(execution, "dataset_vars", {}) or {},
            "case_mode": str(getattr(case, "case_mode", "normal") or "normal"),
        }
        return title, steps_list, ctx

    @sync_to_async
    def _save_history_to_db_async(self, history):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
         
        for i, item in enumerate(history.history):
            description = "AI 动作"
            ai_thought = ""
            action_script = ""
            if item.model_output:
                current_state = getattr(item.model_output, "current_state", None)
                if current_state is not None:
                    ai_thought = getattr(current_state, "evaluation_previous_goal", "") or ""
                if not ai_thought:
                    ai_thought = getattr(item.model_output, "reasoning", "") or ""

                action_value = getattr(item.model_output, "action", None)
                if action_value:
                    action_script = str(action_value)
                    description = self._humanize_action_script(action_script) or action_script
            
            # Result
            status = 'success'
            error_message = ""
            if item.result:
                # item.result is list[ActionResult]
                last_result = item.result[-1]
                if last_result.error:
                    status = "failed"
                    error_message = str(last_result.error)

            step_record, _ = AutoTestStepRecord.objects.update_or_create(
                execution=execution,
                step_number=i+1,
                defaults={
                    "description": description or "AI 动作",
                    "ai_thought": ai_thought,
                    "action_script": action_script,
                    "status": status,
                    "error_message": error_message,
                },
            )
            
            # Save screenshot
            state_obj = getattr(item, "state", None)
            screenshot_data = None
            screenshot_path = None
            if state_obj is not None:
                screenshot_path = getattr(state_obj, "screenshot_path", None)
                get_screenshot = getattr(state_obj, "get_screenshot", None)
                if callable(get_screenshot):
                    screenshot_data = get_screenshot()

            if screenshot_data:
                import base64
                try:
                    img_data = screenshot_data
                    if ';base64,' in img_data:
                        format, imgstr = img_data.split(';base64,') 
                        ext = format.split('/')[-1] 
                    else:
                        imgstr = img_data
                        ext = 'png'
                        
                    data = ContentFile(base64.b64decode(imgstr), name=f'step_{i}.{ext}')
                    step_record.screenshot_after = data
                    step_record.save()
                except Exception as e:
                    logger.error(f"Failed to save screenshot: {e}")
            elif screenshot_path and os.path.exists(screenshot_path):
                try:
                    with open(screenshot_path, "rb") as f:
                        data = ContentFile(f.read(), name=f'step_{i}.png')
                    step_record.screenshot_after = data
                    step_record.save()
                except Exception as e:
                    logger.error(f"Failed to save screenshot from path: {e}")

    def _update_status(self, status, summary=None):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        execution.status = status
        if summary:
            execution.result_summary = summary
        if status in ['completed', 'failed', 'stopped']:
            execution.end_time = timezone.now()
        execution.save()
        try:
            if execution.case_id and status in ("completed", "failed", "stopped"):
                execution.case.status = 5
                execution.case.save(update_fields=["status"])
        except Exception:
            pass

    @sync_to_async
    def _update_status_async(self, status, summary=None):
        self._update_status(status, summary)

    @sync_to_async
    def _merge_execution_summary_async(self, patch: dict):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        base = execution.result_summary or {}
        if not isinstance(base, dict):
            base = {}
        if patch and isinstance(patch, dict):
            for k, v in patch.items():
                base[k] = v
        execution.result_summary = base
        execution.save(update_fields=["result_summary"])

    @sync_to_async
    def _upsert_step_record_async(
        self,
        step_number,
        description,
        ai_thought="",
        action_script="",
        status="pending",
        error_message="",
        screenshot_data=None,
        screenshot_path=None,
        metrics=None,
    ):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        if metrics is None:
            metrics = {}
        try:
            desc = str(description or "").strip()
            act = str(action_script or "").strip()
            looks_code = False
            if act and (desc == act):
                looks_code = True
            if desc and not looks_code:
                if "(" in desc and ")" in desc and ("index=" in desc.lower() or "action" in desc.lower() or desc.lower().startswith(("click", "input", "scroll", "goto"))):
                    looks_code = True
            if looks_code and act:
                description = self._humanize_action_script(act) or description
        except Exception:
            pass
        step_record, _ = AutoTestStepRecord.objects.update_or_create(
            execution=execution,
            step_number=step_number,
            defaults={
                "description": description,
                "ai_thought": ai_thought,
                "action_script": action_script,
                "status": status,
                "error_message": error_message,
                "metrics": metrics,
            },
        )

        if screenshot_data or screenshot_path:
            try:
                screenshot_mode = (getattr(settings, "AI_EXEC_SCREENSHOT_MODE", "all") or "all").strip().lower()
                allow_screenshot = screenshot_mode == "all" or (screenshot_mode == "failed" and status == "failed")
                if not allow_screenshot:
                    return
                if screenshot_data:
                    import base64
                    img_data = screenshot_data
                    if ';base64,' in img_data:
                        format_part, imgstr = img_data.split(';base64,')
                        ext = format_part.split('/')[-1]
                    else:
                        imgstr = img_data
                        ext = 'png'
                    data = ContentFile(base64.b64decode(imgstr), name=f'step_{step_number}_{uuid.uuid4().hex}.{ext}')
                    step_record.screenshot_after = data
                    step_record.save()
                elif screenshot_path and os.path.exists(screenshot_path):
                    with open(screenshot_path, "rb") as f:
                        data = ContentFile(f.read(), name=f'step_{step_number}_{uuid.uuid4().hex}.png')
                    step_record.screenshot_after = data
                    step_record.save()
            except Exception as e:
                logger.error(f"Failed to save screenshot: {e}")

    async def _probe_cdp_ready_async(self, cdp_url: str, timeout_seconds: int = 15):
        deadline = time.time() + timeout_seconds
        url = cdp_url.rstrip("/") + "/json/version"
        last_error = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception as e:
                last_error = e
            await asyncio.sleep(0.5)
        raise TimeoutError(f"CDP not ready: {url} ({last_error})")

    def _classify_nav_error(self, raw: str) -> dict:
        s = str(raw or "")
        lower = s.lower()
        cat = "unknown"
        if "err_name_not_resolved" in lower or "name_not_resolved" in lower or "could not resolve" in lower or "dns" in lower:
            cat = "dns"
        elif "err_proxy_connection_failed" in lower or "err_tunnel_connection_failed" in lower or "proxy" in lower:
            cat = "proxy"
        elif "err_cert" in lower or "certificate" in lower or "ssl" in lower:
            cat = "cert"
        elif "err_connection_refused" in lower or "connection refused" in lower:
            cat = "refused"
        elif "err_connection_timed_out" in lower or "timeout" in lower or "timed out" in lower:
            cat = "timeout"
        brief = {
            "dns": "域名解析失败（DNS）",
            "proxy": "代理连接失败/需要代理认证",
            "cert": "证书不受信任/HTTPS校验失败",
            "refused": "连接被拒绝（服务不可达/端口未开放）",
            "timeout": "连接超时（网络不可达/被防火墙拦截）",
            "unknown": "无法访问（未知网络错误）",
        }.get(cat, "无法访问（未知网络错误）")
        hint = ""
        if cat == "dns":
            hint = "建议：确认已连接企业VPN/内网DNS可用；首次可在自动化专用浏览器窗口直接打开该地址验证。"
        elif cat == "proxy":
            hint = "建议：确认代理/VPN已生效；若内网必须依赖企业插件/代理扩展，请在自动化专用浏览器窗口完成安装与登录后重试。"
        elif cat == "cert":
            hint = "建议：若为企业自签证书/HTTPS拦截，需在自动化专用浏览器窗口导入/信任证书后重试。"
        elif cat in ("timeout", "refused"):
            hint = "建议：确认目标服务在该机器网络可达且端口开放；检查防火墙/安全软件拦截。"
        return {"category": cat, "brief": brief, "hint": hint, "raw": s[:1200]}

    def _should_switch_to_persistent_profile(self, info: dict) -> bool:
        try:
            cat = str((info or {}).get("category") or "").strip().lower()
        except Exception:
            cat = ""
        if cat in ("dns", "proxy", "cert", "timeout"):
            return True
        raw = str((info or {}).get("raw") or "").lower()
        if "net::" in raw and any(k in raw for k in ["err_name_not_resolved", "err_proxy", "err_cert"]):
            return True
        return False

    def _can_try_headful_fallback(self) -> bool:
        try:
            if getattr(self, "_forced_headless", None) is not None:
                return False
        except Exception:
            pass
        if not bool(getattr(settings, "AI_EXEC_HEADFUL_FALLBACK_ON_NETWORK_ERROR", True)):
            return False
        if bool(getattr(settings, "AI_EXEC_HEADLESS", True)) is False:
            return False
        if os.name == "nt":
            return True
        try:
            return bool(os.environ.get("DISPLAY"))
        except Exception:
            return False

    async def _ensure_active_page_for_preflight_async(self):
        try:
            p = await self._get_active_page_async()
        except Exception:
            p = None
        if p:
            return p
        try:
            if self._pw_contexts:
                return await self._pw_contexts[0].new_page()
        except Exception:
            return None
        return None

    async def _preflight_base_url_async(self, url: str) -> tuple[bool, dict]:
        u = str(url or "").strip()
        if not u:
            return True, {"ok": True, "url": ""}
        page = await self._ensure_active_page_for_preflight_async()
        if not page:
            return True, {"ok": True, "url": u, "note": "no_page"}
        started = time.time()
        try:
            timeout_ms = 45000
            try:
                timeout_ms = int(getattr(settings, "AI_EXEC_PREFLIGHT_TIMEOUT_MS", 45000) or 45000)
            except Exception:
                timeout_ms = 45000
            resp = await page.goto(u, wait_until="domcontentloaded", timeout=int(timeout_ms))
            st = 0
            try:
                st = int(getattr(resp, "status", 0) or 0)
            except Exception:
                st = 0
            if st >= 400:
                title = ""
                try:
                    title = str(await page.title())[:180]
                except Exception:
                    title = ""
                info = {
                    "ok": False,
                    "url": u,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "profile": "persistent" if bool(self._using_persistent_profile) else "temp",
                    "http_status": int(st),
                    "page_title": title,
                    "brief": f"HTTP {int(st)}（页面返回错误状态码）",
                    "hint": "建议：检查目标站点是否对自动化/无Cookie访问返回网关错误；核对执行端机器的网络/代理/VPN与用户本机一致。",
                }
                try:
                    await self._merge_step_metrics_async(0, {"preflight": info})
                except Exception:
                    pass
                try:
                    await self._append_step_note_async(0, f"项目地址预检失败：{str(info.get('brief') or '')}")
                except Exception:
                    pass
                return False, info
            info = {
                "ok": True,
                "url": u,
                "elapsed_ms": int((time.time() - started) * 1000),
                "profile": "persistent" if bool(self._using_persistent_profile) else "temp",
            }
            try:
                await self._merge_step_metrics_async(0, {"preflight": info})
            except Exception:
                pass
            return True, info
        except Exception as e:
            classified = self._classify_nav_error(str(e))
            info = {
                "ok": False,
                "url": u,
                "elapsed_ms": int((time.time() - started) * 1000),
                "profile": "persistent" if bool(self._using_persistent_profile) else "temp",
            }
            info.update(classified or {})
            try:
                await self._merge_step_metrics_async(0, {"preflight": info})
            except Exception:
                pass
            try:
                brief = str(info.get("brief") or "").strip()
                hint = str(info.get("hint") or "").strip()
                msg = "；".join([x for x in [brief, hint] if x]).strip()
                if msg:
                    await self._append_step_note_async(0, f"项目地址预检失败：{msg}")
            except Exception:
                pass
            return False, info

    async def _start_network_capture_async(self, cdp_url: str):
        self._pw = await async_playwright().start()
        self._pw_browser = await self._pw.chromium.connect_over_cdp(cdp_url)
        contexts = list(self._pw_browser.contexts or [])
        if not contexts:
            contexts = [await self._pw_browser.new_context()]
        self._pw_contexts = contexts

        init_script = """
(() => {
  const MAX = 400;
  const push = (text) => {
    try {
      if (!text) return;
      const t = String(text).replace(/\\s+/g, ' ').trim();
      if (!t) return;
      const arr = (window.__qa_messages = window.__qa_messages || []);
      const last = arr.length ? arr[arr.length - 1].text : '';
      if (t === last) return;
      arr.push({ ts: Date.now(), text: t.slice(0, 500) });
      if (arr.length > MAX) window.__qa_messages = arr.slice(-MAX);
    } catch (e) {}
  };
  const getRoots = () => {
    const roots = [];
    try { roots.push(document); } catch (e) {}
    try {
      const arr = (window.__qa_shadow_roots = window.__qa_shadow_roots || []);
      for (const r of arr) roots.push(r);
    } catch (e) {}
    return roots;
  };
  const trackShadowRoot = (sr) => {
    try {
      if (!sr) return;
      const arr = (window.__qa_shadow_roots = window.__qa_shadow_roots || []);
      if (arr.indexOf(sr) === -1) arr.push(sr);
    } catch (e) {}
  };
  const __qaPatchValueSetter = (Proto) => {
    try {
      const desc = Object.getOwnPropertyDescriptor(Proto, 'value');
      if (!desc || !desc.configurable || typeof desc.set !== 'function' || typeof desc.get !== 'function') return;
      if (Proto.__qa_patched_value_setter) return;
      Object.defineProperty(Proto, 'value', {
        configurable: true,
        enumerable: desc.enumerable,
        get: function() { return desc.get.call(this); },
        set: function(v) {
          desc.set.call(this, v);
          try {
            const host = (location && location.hostname) ? location.hostname : '';
            if (host.indexOf('baidu.com') !== -1) {
              if (document && document.activeElement !== this && typeof this.focus === 'function') {
                this.focus({ preventScroll: true });
              }
            }
          } catch (e) {}
          try { this.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
          try { this.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
        }
      });
      Proto.__qa_patched_value_setter = true;
    } catch (e) {}
  };
  try { __qaPatchValueSetter(HTMLInputElement.prototype); } catch (e) {}
  try { __qaPatchValueSetter(HTMLTextAreaElement.prototype); } catch (e) {}
  const scan = () => {
    try {
      const nodes = [];
      const pushNode = (el) => {
        try {
          if (!el) return;
          const txt = el.innerText || el.textContent || el.getAttribute?.('aria-label') || el.getAttribute?.('title') || '';
          if (txt) push(txt);
        } catch (e) {}
      };
      for (const root of getRoots()) {
        try {
          root.querySelectorAll('[role="alert"],[aria-live]').forEach((el) => nodes.push(el));
        } catch (e) {}
        try {
          root.querySelectorAll('.toast,.snackbar,.ant-message,.ant-message-notice-content,.el-message,.el-message__content,.el-notification,.notification,.message').forEach((el) => nodes.push(el));
        } catch (e) {}
      }
      for (const el of nodes) pushNode(el);
    } catch (e) {}
  };
  const start = () => {
    try {
      if (window.__qa_message_observer_started) return;
      window.__qa_message_observer_started = true;
      try {
        const orig = Element.prototype.attachShadow;
        if (orig && !Element.prototype.__qa_patched_attachShadow) {
          Element.prototype.__qa_patched_attachShadow = true;
          Element.prototype.attachShadow = function(init) {
            const sr = orig.call(this, init);
            try { trackShadowRoot(sr); } catch (e) {}
            try { scan(); } catch (e) {}
            return sr;
          };
        }
      } catch (e) {}
      const target = document.documentElement || document.body;
      if (!target) return;
      const isToastLike = (el) => {
        try {
          const cls = (el.className || '').toString();
          return /(toast|snackbar|message|notification|ant-message|el-message|el-notification)/i.test(cls);
        } catch (e) { return false; }
      };
      const handleNode = (n) => {
        try {
          if (!n) return;
          if (n.nodeType === 3) {
            push(n.textContent || '');
            return;
          }
          if (n.nodeType !== 1) return;
          const el = n;
          try { if (el.shadowRoot) trackShadowRoot(el.shadowRoot); } catch (e) {}
          const role = el.getAttribute && el.getAttribute('role');
          const ariaLive = el.getAttribute && el.getAttribute('aria-live');
          if (role === 'alert' || ariaLive || isToastLike(el)) {
            push(el.innerText || el.textContent || el.getAttribute?.('aria-label') || el.getAttribute?.('title') || '');
          }
          if (el.querySelectorAll) {
            el.querySelectorAll('[role="alert"],[aria-live]').forEach((c) => push(c.innerText || c.textContent || ''));
          }
        } catch (e) {}
      };
      const observer = new MutationObserver((muts) => {
        for (const m of muts) {
          if (m.type === 'childList') {
            m.addedNodes && m.addedNodes.forEach(handleNode);
          } else if (m.type === 'characterData') {
            handleNode(m.target);
          } else if (m.type === 'attributes') {
            handleNode(m.target);
          }
        }
      });
      observer.observe(target, { childList: true, subtree: true, characterData: true, attributes: true });
      try {
        document.addEventListener('click', () => { try { scan(); } catch(e){} }, true);
        document.addEventListener('click', () => { try { setTimeout(scan, 0); setTimeout(scan, 5); } catch(e){} }, true);
      } catch (e) {}
      scan();
      setInterval(scan, 50);
    } catch (e) {}
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
        """

        def should_capture(request) -> bool:
            try:
                rt = request.resource_type
                if rt in ("xhr", "fetch"):
                    return True
                if request.method and request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                    return True
                if "login" in (request.url or "").lower():
                    return True
                return False
            except Exception:
                return False

        def should_capture_cdp(url: str, method: str, resource_type: str, status: int | None = None) -> bool:
            try:
                u = (url or "").lower()
                m = (method or "").upper()
                rt = (resource_type or "").lower()
                if status is not None:
                    try:
                        if int(status) >= 400:
                            return True
                    except Exception:
                        pass
                if rt in ("xhr", "fetch"):
                    return True
                if m in ("POST", "PUT", "PATCH", "DELETE"):
                    return True
                if any(k in u for k in ["login", "auth", "token", "session", "oauth", "refresh", "graphql"]):
                    return True
                return False
            except Exception:
                return False

        async def install_to_page(page):
            try:
                await page.evaluate(init_script)
            except Exception:
                pass

        async def attach_cdp_network(ctx, page):
            try:
                client = await ctx.new_cdp_session(page)
            except Exception:
                return
            try:
                await client.send("Network.enable", {})
            except Exception:
                return
            self._cdp_sessions.append(client)

            def on_req(ev):
                try:
                    rid = ev.get("requestId")
                    req = ev.get("request") or {}
                    if not rid:
                        return
                    try:
                        if not self._login_attempted:
                            u0 = str(req.get("url") or "").lower()
                            m0 = str(req.get("method") or "").upper()
                            if any(k in u0 for k in ["login", "auth", "token", "session", "oauth", "refresh"]) and m0 in ("POST", "PUT", "PATCH"):
                                self._login_attempted = True
                                self._login_attempted_ts = time.time()
                    except Exception:
                        pass
                    try:
                        import hashlib
                        u0 = str(req.get("url") or "").lower()
                        m0 = str(req.get("method") or "").upper()
                        pd = str(req.get("postData") or "")
                        if any(k in u0 for k in ["login", "auth", "token", "session"]) and m0 in ("POST", "PUT", "PATCH") and pd:
                            try:
                                body = json.loads(pd)
                            except Exception:
                                body = None
                            if isinstance(body, dict):
                                au = str(body.get("username") or body.get("user") or body.get("account") or "").strip()
                                ap = str(body.get("password") or body.get("pwd") or "").strip()
                                if au:
                                    self._last_auth_req_username = au
                                if ap:
                                    self._last_auth_req_password_sha256_12 = hashlib.sha256(ap.encode("utf-8", "ignore")).hexdigest()[:12]
                                    self._last_auth_req_password = ap
                                exp_u = str(self._expected_login_username or "").strip()
                                exp_p = str(self._expected_login_password or "").strip()
                                if exp_u and exp_p and au and ap:
                                    if au != exp_u or ap != exp_p:
                                        self._unexpected_login_creds = {
                                            "expected_username": exp_u,
                                            "expected_password": exp_p,
                                            "expected_password_sha256_12": hashlib.sha256(exp_p.encode("utf-8", "ignore")).hexdigest()[:12],
                                            "actual_username": au,
                                            "actual_password": ap,
                                            "actual_password_sha256_12": hashlib.sha256(ap.encode("utf-8", "ignore")).hexdigest()[:12],
                                            "url": str(req.get("url") or "")[:500],
                                        }
                    except Exception:
                        pass
                    step_no = int(self._current_agent_step or self._agent_step_seq or self._current_step_number or 0)
                    self._cdp_request_map[rid] = {
                        "url": req.get("url", ""),
                        "method": req.get("method", ""),
                        "headers": dict(req.get("headers") or {}),
                        "post_data": (req.get("postData") or ""),
                        "ts": time.time(),
                        "step_number": step_no,
                        "resource_type": ev.get("type") or "",
                    }
                except Exception:
                    return

            async def _decode_cdp_body(rid: str) -> str:
                try:
                    import base64
                    body = await client.send("Network.getResponseBody", {"requestId": rid})
                    raw_body = body.get("body") or ""
                    if body.get("base64Encoded"):
                        try:
                            raw_bytes = base64.b64decode(raw_body)
                            return raw_bytes.decode("utf-8", "ignore")
                        except Exception:
                            return ""
                    return str(raw_body)
                except Exception:
                    return ""

            async def handle_finished(rid: str):
                info = self._cdp_request_map.pop(rid, None) or {}
                resp = self._cdp_response_map.pop(rid, None) or {}
                if not info and not resp:
                    return
                url = info.get("url") or resp.get("url") or ""
                method = info.get("method") or ""
                status = resp.get("status")
                resource_type = info.get("resource_type") or resp.get("type") or ""
                try:
                    status_i = int(status or 0)
                except Exception:
                    status_i = 0
                if not should_capture_cdp(url, method, resource_type, status=status_i):
                    return
                body_text = await _decode_cdp_body(rid)
                duration_ms = None
                if info.get("ts"):
                    try:
                        duration_ms = int((time.time() - info["ts"]) * 1000)
                    except Exception:
                        duration_ms = None
                step_no = int(info.get("step_number") or self._current_agent_step or self._agent_step_seq or self._current_step_number or 0)
                response_headers = dict(resp.get("headers") or {})
                if duration_ms is not None:
                    response_headers = {**response_headers, "x_duration_ms": str(duration_ms)}
                await self._create_network_entry_async(
                    step_number=step_no,
                    url=url,
                    method=method or "GET",
                    status_code=status_i,
                    request_data=self._encode_request_payload(
                        url=url,
                        headers=info.get("headers") or {},
                        post_data=info.get("post_data") or "",
                    ),
                    response_data=self._encode_response_payload(
                        status=status_i,
                        headers=response_headers,
                        body_text=body_text,
                    ),
                )
                url_l = (url or "").lower()
                if any(k in url_l for k in ["login", "token", "session", "auth"]):
                    try:
                        self._seen_auth_response = True
                        self._last_auth_step_number = int(step_no)
                        self._last_auth_status = int(status_i or 0)
                        self._last_auth_url = str(url or "")[:500]
                        self._last_auth_body_norm = self._norm_text(body_text or "")[:2000]
                    except Exception:
                        pass
                    asyncio.create_task(self._capture_toast_after_auth_async(step_no))

            def on_resp(ev):
                try:
                    rid = ev.get("requestId")
                    resp = ev.get("response") or {}
                    if not rid:
                        return
                    self._cdp_response_map[rid] = {
                        "url": resp.get("url") or "",
                        "status": resp.get("status"),
                        "headers": dict(resp.get("headers") or {}),
                        "type": ev.get("type") or "",
                        "ts": time.time(),
                    }
                except Exception:
                    return

            def on_finished(ev):
                try:
                    rid = ev.get("requestId")
                    if not rid:
                        return
                except Exception:
                    return
                asyncio.create_task(handle_finished(rid))

            try:
                client.on("Network.requestWillBeSent", on_req)
                client.on("Network.responseReceived", on_resp)
                client.on("Network.loadingFinished", on_finished)
                client.on("Network.loadingFailed", on_finished)
            except Exception:
                return

        async def setup_context(ctx):
            try:
                if ctx not in self._pw_contexts:
                    self._pw_contexts.append(ctx)
            except Exception:
                pass
            try:
                await ctx.add_init_script(init_script)
            except Exception:
                pass
            try:
                for p in list(ctx.pages or []):
                    await install_to_page(p)
                    await attach_cdp_network(ctx, p)
                    try:
                        await self._install_filechooser_listener_async(p)
                    except Exception:
                        pass
                    try:
                        p.on("popup", lambda pp: asyncio.create_task(self._remember_new_page_async(pp, source="popup")))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                ctx.on("page", lambda p: asyncio.create_task(install_to_page(p)))
                ctx.on("page", lambda p: asyncio.create_task(attach_cdp_network(ctx, p)))
                ctx.on("page", lambda p: asyncio.create_task(self._remember_new_page_async(p, source="page")))
                ctx.on("page", lambda p: asyncio.create_task(self._install_filechooser_listener_async(p)))
                ctx.on(
                    "page",
                    lambda p: (
                        p.on("popup", lambda pp: asyncio.create_task(self._remember_new_page_async(pp, source="popup")))
                        if p is not None
                        else None
                    ),
                )
            except Exception:
                pass
            try:
                ctx.on("request", on_request)
                ctx.on("response", lambda resp: asyncio.create_task(on_response(resp)))
            except Exception:
                pass

        def on_request(request):
            try:
                if not should_capture(request):
                    return
                key = id(request)
                self._pending_requests[key] = {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers or {}),
                    "post_data": request.post_data or "",
                    "ts": time.time(),
                }
            except Exception:
                return

        async def on_response(response):
            try:
                request = response.request
                if not should_capture(request) and int(getattr(response, "status", 0) or 0) < 400:
                    return
                key = id(request)
                info = self._pending_requests.pop(key, None) or {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers or {}),
                    "post_data": request.post_data or "",
                    "ts": None,
                }

                response_headers = dict(response.headers or {})
                body_text = ""
                try:
                    body_text = await response.text()
                except Exception:
                    body_text = ""

                duration_ms = None
                if info.get("ts"):
                    duration_ms = int((time.time() - info["ts"]) * 1000)

                await self._create_network_entry_async(
                    step_number=self._current_step_number,
                    url=info.get("url", request.url),
                    method=info.get("method", request.method),
                    status_code=response.status,
                    request_data=self._encode_request_payload(
                        url=info.get("url", request.url),
                        headers=info.get("headers", {}),
                        post_data=info.get("post_data", "") or "",
                    ),
                    response_data=self._encode_response_payload(
                        status=response.status,
                        headers=response_headers,
                        body_text=body_text,
                    ) if duration_ms is None else self._encode_response_payload(
                        status=response.status,
                        headers={**response_headers, "x_duration_ms": str(duration_ms)},
                        body_text=body_text,
                    ),
                )
                try:
                    url_l = (request.url or "").lower()
                    if any(k in url_l for k in ["login", "token", "session", "auth"]):
                        try:
                            self._seen_auth_response = True
                            self._last_auth_step_number = int(self._current_step_number or 0)
                            self._last_auth_status = int(getattr(response, "status", 0) or 0)
                            self._last_auth_url = str(request.url or "")[:500]
                            self._last_auth_body_norm = self._norm_text(body_text or "")[:2000]
                        except Exception:
                            pass
                        asyncio.create_task(self._capture_toast_after_auth_async(int(self._current_step_number or 0)))
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Failed to capture response: {e}")

        for ctx in contexts:
            await setup_context(ctx)
        try:
            self._pw_browser.on("context", lambda ctx: asyncio.create_task(setup_context(ctx)))
        except Exception:
            pass

    async def _stop_network_capture_async(self):
        try:
            await asyncio.sleep(0.2)
        except Exception:
            pass
        try:
            if self._pw_browser:
                await self._pw_browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw_browser = None
        self._pw = None
        self._pw_contexts = []
        self._pending_requests = {}
        self._seen_message_keys = set()

    def _format_expected_result(self, expected: str) -> str:
        if not expected:
            return ""
        s = unescape(str(expected))
        s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > 200:
            s = s[:200] + "..."
        return s

    def _expect_email_error(self, expected: str) -> bool:
        s = str(expected or "")
        lower = s.lower()
        if "邮箱" in s and any(k in s for k in ["错误", "不正确", "不对", "格式", "无效", "不合法"]):
            return True
        if "email" in lower and any(k in lower for k in ["invalid", "incorrect", "error", "format"]):
            return True
        return False

    def _smart_data_rewrite_description(self, desc: str, expected: str = "") -> tuple[str, bool]:
        s = str(desc or "").strip()
        if not s:
            return s, False
        low = s.lower()
        if "智能生成" in s or "smart_data" in low:
            return s, False

        neg_phone = self._expect_phone_error(expected) or self._expect_phone_error(s)
        neg_email = self._expect_email_error(expected) or self._expect_email_error(s)

        def clean_field(field: str) -> str:
            f = str(field or "").strip()
            f = re.sub(r"^[\{\[\(（【\"'“‘]+", "", f)
            f = re.sub(r"[\}\]\)）】\"'”’]+$", "", f)
            return f.strip()

        def token_for(field: str, value: str) -> str:
            f = clean_field(field)
            fl = f.lower()
            if any(k in f for k in ["密码", "pass", "pwd"]) or any(k in fl for k in ["password", "passwd"]):
                return "【智能生成：密码】"
            if any(k in f for k in ["账号", "用户名", "用户", "登录账号", "登录用户名"]) or any(k in fl for k in ["username", "user", "account", "login"]):
                return "【智能生成：账号】"
            if any(k in f for k in ["手机号", "手机号码", "手机", "电话", "联系电话"]) or any(k in fl for k in ["phone", "mobile", "tel"]):
                return "【智能生成：无效手机号】" if neg_phone else "【智能生成：手机号】"
            if any(k in f for k in ["邮箱", "邮件"]) or "email" in fl:
                return "【智能生成：无效邮箱】" if neg_email else "【智能生成：邮箱】"
            if any(k in f for k in ["姓名", "名字", "名称", "标题", "备注", "地址", "公司", "部门"]):
                f2 = re.sub(r"\s+", "", f)[:8]
                return f"【智能生成：{f2}】" if f2 else "【智能生成】"
            return "【智能生成】"

        def known_field(field: str) -> bool:
            f = clean_field(field)
            fl = f.lower()
            if not f:
                return False
            if any(k in f for k in ["密码", "账号", "用户名", "用户", "登录账号", "登录用户名", "手机号", "手机号码", "手机", "电话", "联系电话", "邮箱", "邮件"]):
                return True
            if any(k in fl for k in ["password", "passwd", "username", "account", "login", "phone", "mobile", "tel", "email"]):
                return True
            return False

        def should_replace(field: str, value: str) -> bool:
            f = str(field or "").strip()
            v = str(value or "").strip()
            if not (f and v):
                return False
            vl = v.lower()
            if v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return False
            if vl.startswith("http://") or vl.startswith("https://"):
                return False
            if "打开" in f or "访问" in f or "跳转" in f:
                return False
            if f.lower().startswith(("client_id", "redirect_uri", "scope", "state", "response_type")):
                return False
            return True

        pair_pat = re.compile(r"([A-Za-z0-9_#\u4e00-\u9fff]{1,20})\s*([:：=])\s*([^\s，,；;。]+)")
        matches = list(pair_pat.finditer(s))
        if matches:
            parts = []
            last = 0
            changed = False
            for m in matches:
                field = m.group(1) or ""
                sep = m.group(2) or ""
                value = m.group(3) or ""
                if should_replace(field, value):
                    parts.append(s[last:m.start(1)])
                    parts.append(str(field))
                    parts.append(str(sep))
                    parts.append(token_for(field, value))
                    last = m.end(3)
                    changed = True
                else:
                    continue
            if changed:
                parts.append(s[last:])
                out = "".join(parts).strip()
                return out, True

        pair_ws_pat = re.compile(r"([A-Za-z0-9_#\u4e00-\u9fff]{1,20})(\s+)([^\s，,；;。]+)")
        matches2 = list(pair_ws_pat.finditer(s))
        if matches2:
            parts = []
            last = 0
            changed = False
            for m in matches2:
                field = m.group(1) or ""
                sep = m.group(2) or " "
                value = (m.group(3) or "").strip().strip('"').strip("'")
                if not known_field(field):
                    continue
                if should_replace(field, value):
                    parts.append(s[last:m.start(1)])
                    parts.append(str(field))
                    parts.append(str(sep))
                    parts.append(token_for(field, value))
                    last = m.end(3)
                    changed = True
            if changed:
                parts.append(s[last:])
                out = "".join(parts).strip()
                return out, True

        field_act_pat = re.compile(
            r"([\{\[\(（【\"'“‘]?[A-Za-z0-9_#\u4e00-\u9fff-]{1,30}[\}\]\)）】\"'”’]?)"
            r"(\s*(?:输入|填写|填入|填充|选择|选中)\s*[:：]?\s*)"
            r"([^\s，,；;。]+)"
        )
        matches3 = list(field_act_pat.finditer(s))
        if matches3:
            parts = []
            last = 0
            changed = False
            for m in matches3:
                field = m.group(1) or ""
                op = m.group(2) or ""
                value = (m.group(3) or "").strip().strip('"').strip("'")
                if not known_field(field):
                    continue
                if should_replace(field, value):
                    parts.append(s[last:m.start(1)])
                    parts.append(str(field))
                    parts.append(str(op))
                    parts.append(token_for(field, value))
                    last = m.end(3)
                    changed = True
            if changed:
                parts.append(s[last:])
                out = "".join(parts).strip()
                return out, True

        label_pat = re.compile(
            r"(用户名|账号|密码|手机号|手机号码|手机|电话|联系电话|邮箱|邮件|email|phone|mobile|tel|username|account|password)"
            r"\s*(?:[:：=]|为|是)?\s*([^\s，,；;。]+)",
            flags=re.I,
        )
        matches4 = list(label_pat.finditer(s))
        if matches4:
            parts = []
            last = 0
            changed = False
            for m in matches4:
                field = m.group(1) or ""
                value = (m.group(2) or "").strip().strip('"').strip("'")
                if not value:
                    continue
                if value.lower().startswith(("http://", "https://")):
                    continue
                if value in ("【智能生成】", "(智能生成)", "（智能生成）"):
                    continue
                parts.append(s[last:m.start(2)])
                parts.append(token_for(field, value))
                last = m.end(2)
                changed = True
            if changed:
                parts.append(s[last:])
                out = "".join(parts).strip()
                return out, True

        m = re.search(r"^(.*?(?:输入|填写|填入|填充)\s*[:：]\s*)(.+)$", s)
        if m:
            v = str(m.group(2) or "").strip()
            if not v or v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return s, False
            if v.lower().startswith(("http://", "https://")):
                return s, False
            token = "【智能生成：密码】" if (v in ("***", "******")) else ("【智能生成：无效手机号】" if (neg_phone and any(k in s for k in ["手机", "电话", "手机号"])) else "【智能生成】")
            return (str(m.group(1) or "") + token).strip(), True

        m = re.search(r"^(.*?(?:输入|填写|填入|填充)\s+)([^\s，,；;。]+)$", s)
        if m:
            v = str(m.group(2) or "").strip().strip('"').strip("'")
            if not v or v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return s, False
            if v.lower().startswith(("http://", "https://")):
                return s, False
            token = "【智能生成：无效手机号】" if (neg_phone and any(k in s for k in ["手机", "电话", "手机号"])) else "【智能生成】"
            return (str(m.group(1) or "") + token).strip(), True

        m = re.search(r"^(.*?(?:选择|选中)\s*[:：]\s*)(.+)$", s)
        if m:
            v = str(m.group(2) or "").strip()
            if not v or v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return s, False
            return (str(m.group(1) or "") + "【智能生成】").strip(), True

        m = re.search(r"^(.*?(?:选择|选中)\s+)([^\s，,；;。]+)$", s)
        if m:
            v = str(m.group(2) or "").strip().strip('"').strip("'")
            if not v or v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return s, False
            if v.lower().startswith(("http://", "https://")):
                return s, False
            return (str(m.group(1) or "") + "【智能生成】").strip(), True

        m = re.search(r"^(.*?=\s*)(.+)$", s)
        if m and any(k in low for k in ["输入", "select", "填写", "填入"]):
            v = str(m.group(2) or "").strip()
            if not v or v in ("【智能生成】", "(智能生成)", "（智能生成）"):
                return s, False
            if v.lower().startswith(("http://", "https://")):
                return s, False
            return (str(m.group(1) or "") + "【智能生成】").strip(), True
        return s, False

    def _norm_text(self, text: str) -> str:
        if text is None:
            return ""
        s = unescape(str(text))
        s = s.lower()
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"[\s\r\n\t]+", "", s)
        s = re.sub(r"[\"“”'‘’`]", "", s)
        s = re.sub(r"[，。；：、】【（）()、!！?？,.;:]", "", s)
        return s

    def _expand_phrase_aliases(self, phrase: str) -> list[str]:
        p = str(phrase or "").strip()
        if not p:
            return []
        aliases = [p]
        if "密码错误" in p or ("密码" in p and "错误" in p):
            aliases.extend(
                [
                    "passwordincorrect",
                    "incorrectpassword",
                    "passwordisincorrect",
                    "theemailorpasswordyouenteredisincorrect",
                    "theemailorpasswordyouenteredisincorrect.",
                    "emailorpasswordisincorrect",
                    "wrongpassword",
                ]
            )
        if "邮箱" in p and "密码" in p and ("错误" in p or "不正确" in p):
            aliases.extend(
                [
                    "theemailorpasswordyouenteredisincorrect",
                    "emailorpasswordisincorrect",
                ]
            )
        if ("手机号" in p or "手机号码" in p or "电话" in p) and any(k in p for k in ["错误", "不正确", "不对", "格式", "无效", "不合法"]):
            aliases.extend(
                [
                    "手机号错误",
                    "手机号格式错误",
                    "手机号格式不正确",
                    "手机号码格式错误",
                    "手机号码格式不正确",
                    "请输入正确手机号",
                    "请输入正确的手机号",
                    "请输入正确手机号码",
                    "请输入正确的手机号码",
                    "手机号无效",
                    "手机号码无效",
                    "invalidphone",
                    "invalidmobilenumber",
                    "phonenumberisinvalid",
                    "invalidphonenumber",
                    "phoneformaterror",
                ]
            )
        if "incorrect" in p.lower():
            aliases.append("不正确")
        out = []
        seen = set()
        for x in aliases:
            nx = self._norm_text(x)
            if not nx or nx in seen:
                continue
            seen.add(nx)
            out.append(x)
        return out[:8]

    def _expect_password_error(self, expected: str) -> bool:
        s = str(expected or "")
        lower = s.lower()
        if "密码错误" in s:
            return True
        if ("密码" in s and ("错误" in s or "不正确" in s or "不对" in s)):
            return True
        if "wrong password" in lower or "incorrect password" in lower:
            return True
        if "email or password" in lower and "incorrect" in lower:
            return True
        return False

    def _expect_phone_error(self, expected: str) -> bool:
        s = str(expected or "")
        lower = s.lower()
        if any(k in s for k in ["手机号", "手机号码", "电话"]):
            if any(k in s for k in ["错误", "不正确", "不对", "格式", "无效", "不合法", "请输入正确", "正确的"]):
                return True
        if "phone" in lower and any(k in lower for k in ["invalid", "incorrect", "error", "format"]):
            return True
        if "mobile" in lower and any(k in lower for k in ["invalid", "incorrect", "error", "format"]):
            return True
        return False

    def _expect_login_fail(self, expected: str) -> bool:
        s = str(expected or "")
        lower = s.lower()
        if any(k in s for k in ["无法登录", "不能登录", "不允许登录", "登录失败", "登录不成功", "无法登陆", "不能登陆"]):
            return True
        if "login" in lower and any(k in lower for k in ["fail", "failed", "unsuccessful", "cannot"]):
            return True
        return False

    def _case_expects_login_fail(self) -> bool:
        for s in (self._testcase_steps or []):
            try:
                exp = str(getattr(s, "expected_result", "") or "")
            except Exception:
                exp = ""
            try:
                desc = str(getattr(s, "description", "") or "")
            except Exception:
                desc = ""
            if self._expect_login_fail(exp) or self._expect_login_fail(desc):
                return True
        return False

    def _is_relevant_toast_text(self, text: str) -> bool:
        t = self._norm_text(text or "")
        if not t:
            return False
        keywords = [
            "error",
            "failed",
            "invalid",
            "incorrect",
            "unauthorized",
            "forbidden",
            "denied",
            "wrongpassword",
            "passwordincorrect",
            "emailorpassword",
            "密码",
            "错误",
            "不正确",
            "失败",
            "无权限",
        ]
        return any(k in t for k in keywords)

    async def _check_login_failed_async(self):
        try:
            if not self._seen_auth_response and not bool(self._login_attempted):
                return False, "未尝试登录"
        except Exception:
            pass
        try:
            if self._last_auth_status is not None:
                if int(self._last_auth_status) >= 400:
                    return True, f"登录接口状态码={self._last_auth_status}"
                if 200 <= int(self._last_auth_status) < 300:
                    return False, f"登录接口状态码={self._last_auth_status}"
        except Exception:
            pass
        try:
            if self._last_auth_body_norm and any(k in self._last_auth_body_norm for k in ["incorrect", "wrongpassword", "passwordincorrect", "invalid", "unauthorized", "forbidden"]):
                return True, "登录接口返回包含错误信息"
        except Exception:
            pass
        page = await self._get_active_page_async()
        if not page:
            return (False, "未触发登录/鉴权接口") if not self._seen_auth_response else (False, "")
        try:
            url_l = (page.url or "").lower()
            if any(k in url_l for k in ["login", "signin"]):
                try:
                    if not self._seen_auth_response:
                        since = float(time.time() - float(self._login_attempted_ts or 0.0))
                        if since < 1.2:
                            return False, "登录尝试中"
                except Exception:
                    pass
                return True, f"仍停留在登录页: {page.url}"
        except Exception:
            pass
        try:
            still_has_pwd = await page.evaluate(
                "() => !!(document.querySelector('input[type=password]') || document.querySelector('input[name*=pass]') || document.querySelector('input[placeholder*=Pass]'))"
            )
            if still_has_pwd:
                try:
                    if not self._seen_auth_response:
                        since = float(time.time() - float(self._login_attempted_ts or 0.0))
                        if since < 1.2:
                            return False, "登录尝试中"
                except Exception:
                    pass
                return True, "页面仍存在密码输入框"
        except Exception:
            pass
        if not self._seen_auth_response:
            return False, "未触发登录/鉴权接口"
        return False, ""

    def _extract_case_step_progress(self, text: str):
        t = str(text or "")
        cur = None
        total = None
        done_steps = set()
        try:
            m = re.search(r"(?:用例步骤|步骤)\s*(\d{1,3})\s*/\s*(\d{1,3})", t)
            if m:
                cur = int(m.group(1))
                total = int(m.group(2))
        except Exception:
            cur = None
            total = None
        try:
            if cur is None or total is None:
                m0 = re.search(r"(?:Step)\s*(\d{1,3})\s*(?:/|of)\s*(\d{1,3})", t, flags=re.IGNORECASE)
                if m0:
                    cur = int(m0.group(1))
                    total = int(m0.group(2))
        except Exception:
            pass
        try:
            for m2 in re.finditer(r"(?:完成|已完成|DONE)\s*(?:用例步骤|步骤)\s*(\d{1,3})", t, flags=re.IGNORECASE):
                done_steps.add(int(m2.group(1)))
        except Exception:
            pass
        try:
            for m3 in re.finditer(r"(?:用例步骤|步骤)\s*(\d{1,3})\s*(?:完成|已完成)", t):
                done_steps.add(int(m3.group(1)))
        except Exception:
            pass
        try:
            for m33 in re.finditer(r"(?:执行完成|执行完毕|已执行|已跑完)\s*(?:用例步骤|步骤)\s*(\d{1,3})", t):
                done_steps.add(int(m33.group(1)))
        except Exception:
            pass
        try:
            for m4 in re.finditer(r"(?:Completed|Done)\s*Step\s*(\d{1,3})", t, flags=re.IGNORECASE):
                done_steps.add(int(m4.group(1)))
        except Exception:
            pass
        try:
            for m5 in re.finditer(r"^\s*(?:✅|☑|✔|\*|-|\d+\.)?\s*步骤\s*(\d{1,3})\s*[:：]", t, flags=re.MULTILINE):
                done_steps.add(int(m5.group(1)))
        except Exception:
            pass
        try:
            for m55 in re.finditer(r"^\s*(?:✅|☑|✔|\*|-|\d+\.)?\s*Step\s*(\d{1,3})\s*[:：]", t, flags=re.MULTILINE | re.IGNORECASE):
                done_steps.add(int(m55.group(1)))
        except Exception:
            pass
        try:
            if cur is None:
                m6 = re.findall(r"(?:用例步骤|步骤)\s*(\d{1,3})\s*[:：]", t)
                if m6:
                    cur = int(m6[-1])
        except Exception:
            pass
        try:
            if cur is None:
                m7 = re.findall(r"Step\s*(\d{1,3})\s*[:：]", t, flags=re.IGNORECASE)
                if m7:
                    cur = int(m7[-1])
        except Exception:
            pass
        return cur, total, done_steps

    def _note_non_blocking_issue(self, msg: str, case_step_no: int | None = None):
        m = str(msg or "").strip()
        if not m:
            return
        self._non_blocking_issue_notes.append(m[:300])
        if len(self._non_blocking_issue_notes) > 30:
            self._non_blocking_issue_notes = self._non_blocking_issue_notes[-30:]
        try:
            csn = int(case_step_no or 0)
        except Exception:
            csn = 0
        if csn <= 0:
            try:
                csn = int(self._case_step_last_seen or 0)
            except Exception:
                csn = 0
        if csn > 0:
            try:
                base = self._non_blocking_by_case_step.get(int(csn)) or []
                base.append(m[:300])
                try:
                    keep = int(getattr(getattr(self._stop_policy, "config", None), "non_blocking_note_max", 20) or 20)
                except Exception:
                    keep = 20
                self._non_blocking_by_case_step[int(csn)] = base[-max(1, keep):]
            except Exception:
                pass

    def _build_stop_evidence_patch(self) -> dict:
        try:
            return {
                "stop_evidence": self._evidence.snapshot(30),
                "stop_toasts": self._evidence.last_texts("toast", 6),
                "stop_network": self._evidence.last_texts("network", 6),
            }
        except Exception:
            return {}

    async def _detect_selected_item_index_async(self, page):
        if not page:
            return {}
        js = r"""
        () => {
          const selSelectors = [
            '[aria-selected="true"]',
            '[aria-current="true"]',
            '[aria-current="page"]',
            '.selected',
            '.is-selected',
            '.active',
            '.is-active',
            '.current',
            '.is-current'
          ];
          const selected = [];
          for (const s of selSelectors) {
            document.querySelectorAll(s).forEach(el => {
              if (el && el.getBoundingClientRect && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0) {
                selected.push(el);
              }
            });
          }
          function pickCandidate(el) {
            let cur = el;
            for (let i = 0; i < 6 && cur; i++) {
              const p = cur.parentElement;
              if (!p) break;
              if (p.children && p.children.length >= 4) return {container: p, item: cur};
              cur = p;
            }
            return null;
          }
          let best = null;
          for (const el of selected) {
            const cand = pickCandidate(el);
            if (!cand) continue;
            const count = cand.container.children ? cand.container.children.length : 0;
            if (!best || count > best.count) {
              best = {count, container: cand.container, item: cand.item};
            }
          }
          if (!best) return {};
          const children = Array.from(best.container.children || []);
          let idx = children.indexOf(best.item);
          if (idx < 0) {
            idx = children.findIndex(c => c.contains(best.item));
          }
          const text = (best.item.innerText || best.item.textContent || '').trim().slice(0, 80);
          return {index: (idx >= 0 ? idx + 1 : null), count: best.count, text};
        }
        """
        try:
            out = await page.evaluate(js)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    async def _detect_slide_sidebar_state_async(self, page):
        if not page:
            return {}
        js = r"""
        () => {
          try {
            const container = document.querySelector('[data-ai-region="左侧缩略图列表"]');
            if (!container) return {};
            const items = Array.from(container.querySelectorAll('[role="listitem"]'));
            const count = items.length;
            if (!count) return {};
            const selSelectors = [
              '[aria-selected="true"]',
              '[aria-current="true"]',
              '[aria-current="page"]',
              '.selected',
              '.is-selected',
              '.active',
              '.is-active',
              '.current',
              '.is-current'
            ];
            let selectedIdx = null;
            let selectedText = '';
            for (let i = 0; i < items.length; i++) {
              const it = items[i];
              for (const s of selSelectors) {
                if (it.matches(s) || it.querySelector(s)) {
                  selectedIdx = i + 1;
                  selectedText = (it.innerText || it.textContent || '').trim().slice(0, 80);
                  break;
                }
              }
              if (selectedIdx) break;
            }
            return {index: selectedIdx, count, text: selectedText};
          } catch(e) { return {}; }
        }
        """
        try:
            out = await page.evaluate(js)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    async def _try_apply_transfer_file_async(self, page, payload: dict) -> dict:
        if not page or not isinstance(payload, dict):
            return {"success": False, "reason": "no_page_or_payload"}
        pick_js = r"""
        () => {
          const isVisible = (el) => {
            try {
              if (!el) return false;
              const st = window.getComputedStyle(el);
              if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
              const r = el.getBoundingClientRect();
              if (!r || r.width < 2 || r.height < 2) return false;
              if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
              return true;
            } catch(e) { return false; }
          };
          const pickText = (el) => {
            try { return String((el.innerText || el.textContent || '')).replace(/\s+/g,' ').trim(); } catch(e) { return ''; }
          };
          const hasUploadHint = (root) => {
            try {
              if (!root) return false;
              const t = pickText(root).toLowerCase();
              if (!t) return false;
              const keys = ['browse files','choose file','select file','drag','drop','upload','import','上传','选择文件','拖拽','导入'];
              return keys.some(k => t.includes(k));
            } catch(e) { return false; }
          };
          const nearUploadRegion = (el) => {
            try {
              let cur = el;
              for (let i = 0; i < 6 && cur; i++) {
                if (hasUploadHint(cur)) return true;
                cur = cur.parentElement;
              }
              return false;
            } catch(e) { return false; }
          };
          const byLabel = (el) => {
            try {
              const id = el.id ? String(el.id) : '';
              if (!id) return false;
              const lab = document.querySelector(`label[for="${CSS && CSS.escape ? CSS.escape(id) : id}"]`);
              if (lab && isVisible(lab)) return true;
              return false;
            } catch(e) { return false; }
          };
          const scoreOne = (el) => {
            let s = 0;
            try {
              if (el.disabled) return -999;
              s += 5;
              if (nearUploadRegion(el)) s += 50;
              if (byLabel(el)) s += 20;
              if (isVisible(el)) s += 10;
              const cls = String(el.className || '').toLowerCase();
              if (cls.includes('upload') || cls.includes('file')) s += 5;
              const accept = String(el.getAttribute('accept') || '').toLowerCase();
              if (accept) s += 2;
            } catch(e) { return -999; }
            return s;
          };
          const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
          if (!inputs.length) return { ok: false, total: 0 };
          let best = { idx: 0, score: -999 };
          for (let i = 0; i < inputs.length; i++) {
            const sc = scoreOne(inputs[i]);
            if (sc > best.score) best = { idx: i, score: sc };
          }
          const el = inputs[best.idx];
          const meta = {
            visible: isVisible(el),
            inUploadRegion: nearUploadRegion(el),
            byLabel: byLabel(el),
            accept: String(el.getAttribute('accept') || '').slice(0, 120),
            id: String(el.id || '').slice(0, 80),
            name: String(el.getAttribute('name') || '').slice(0, 80),
          };
          return { ok: best.score > -900, total: inputs.length, picked: best.idx, score: best.score, meta };
        }
        """
        frames = []
        try:
            frames = list(page.frames or [])
        except Exception:
            frames = []
        if not frames:
            frames = [page.main_frame] if getattr(page, "main_frame", None) else []
        best = None
        best_frame = None
        for fr in frames:
            try:
                out = await fr.evaluate(pick_js)
            except Exception:
                continue
            if not isinstance(out, dict):
                continue
            if not out.get("ok"):
                continue
            try:
                sc = int(out.get("score") or 0)
            except Exception:
                sc = 0
            if (best is None) or (sc > int(best.get("score") or 0)):
                best = out
                best_frame = fr
        if not best or not best_frame:
            return {"success": False, "reason": "no_file_input", "file_input_count": 0}
        try:
            total = int(best.get("total") or 0)
        except Exception:
            total = 0
        try:
            picked = int(best.get("picked") or 0)
        except Exception:
            picked = 0
        try:
            chosen = best_frame.locator('input[type="file"]').nth(int(picked))
        except Exception:
            return {"success": False, "reason": "no_file_input", "file_input_count": int(total)}
        try:
            await chosen.set_input_files(payload)
            return {
                "success": True,
                "file_input_count": int(total),
                "picked_index": int(picked) + 1,
                "picked_score": int(best.get("score") or 0),
                "picked_meta": best.get("meta") or {},
                "frame_url": str(getattr(best_frame, "url", "") or "")[:200],
            }
        except Exception as e:
            return {"success": False, "reason": str(e), "file_input_count": int(total), "picked_index": int(picked) + 1, "picked_meta": best.get("meta") or {}}

    async def _ensure_filechooser_autofill_async(self, page, payload: dict, case_step_no: int):
        if not page or not isinstance(payload, dict):
            return
        self._filechooser_payload = payload
        try:
            self._filechooser_case_step_no = int(case_step_no or 0) or None
        except Exception:
            self._filechooser_case_step_no = None
        try:
            await self._install_filechooser_listener_async(page)
        except Exception:
            return

    async def _install_filechooser_listener_async(self, page):
        if not page:
            return
        try:
            pid = int(id(page))
        except Exception:
            pid = None
        if pid is None:
            return
        try:
            s = getattr(self, "_filechooser_pages", None)
            if not isinstance(s, set):
                s = set()
                self._filechooser_pages = s
        except Exception:
            s = set()
            self._filechooser_pages = s
        if pid in s:
            return

        def _handler(fc):
            try:
                p = self._filechooser_payload
                if not isinstance(p, dict) or not p.get("buffer"):
                    return
                self._filechooser_hit_count = int(self._filechooser_hit_count or 0) + 1
                cs = int(self._filechooser_case_step_no or 0)
                asyncio.create_task(self._on_filechooser_selected_async(fc, p, cs))
            except Exception:
                return

        try:
            page.on("filechooser", _handler)
            s.add(pid)
        except Exception:
            return

    async def _on_filechooser_selected_async(self, fc, payload: dict, case_step_no: int):
        try:
            await fc.set_files(payload)
        except Exception:
            return
        try:
            step_number = int(self._current_step_number or self._current_agent_step or self._agent_step_seq or 0) or 0
        except Exception:
            step_number = 0
        page = None
        try:
            page = await self._get_active_page_async()
        except Exception:
            page = None
        sel = {}
        try:
            if page:
                sel = await self._detect_file_input_selection_async(page, str(payload.get("name") or ""))
        except Exception:
            sel = {}
        matched_cnt = 0
        try:
            matched_cnt = int((sel or {}).get("matchedCount") or 0)
        except Exception:
            matched_cnt = 0
        try:
            if int(case_step_no or 0) > 0 and matched_cnt > 0:
                self._transfer_file_applied_steps.add(int(case_step_no))
        except Exception:
            pass
        if step_number > 0:
            try:
                level = "selected_ok" if matched_cnt > 0 else "selected_uncertain"
                await self._append_step_note_async(step_number, f"文件选择器兜底：已自动选择文件 {str(payload.get('name') or '')}（{level}）")
            except Exception:
                pass
            try:
                await self._merge_step_metrics_async(step_number, {"filechooser_autofill": True, "filechooser_hits": int(self._filechooser_hit_count or 0), "transfer_file_selection": sel, "transfer_file_level": "selected_ok" if matched_cnt > 0 else "selected_uncertain"})
            except Exception:
                pass

    async def _detect_file_input_selection_async(self, page, filename: str) -> dict:
        if not page:
            return {}
        name = str(filename or "").strip()
        if not name:
            return {}
        js = r"""
        (name) => {
          try {
            const out = [];
            const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
            for (let i = 0; i < inputs.length; i++) {
              const el = inputs[i];
              const files = el && el.files ? Array.from(el.files) : [];
              if (!files.length) continue;
              const names = files.map(f => String(f && f.name ? f.name : '')).filter(Boolean).slice(0, 3);
              if (!names.length) continue;
              out.push({ idx: i, names });
            }
            const matched = out.filter(x => (x.names || []).some(n => String(n) === String(name)));
            return { ok: true, total: inputs.length, withFiles: out.length, matchedCount: matched.length, matched: matched.slice(0, 5), withFilesList: out.slice(0, 5) };
          } catch(e) { return {}; }
        }
        """
        frames = []
        try:
            frames = list(page.frames or [])
        except Exception:
            frames = []
        if not frames:
            frames = [page.main_frame] if getattr(page, "main_frame", None) else []
        agg = {"matchedCount": 0, "withFiles": 0, "details": []}
        for fr in frames:
            try:
                out = await fr.evaluate(js, name)
            except Exception:
                continue
            if not isinstance(out, dict) or not out.get("ok"):
                continue
            try:
                agg["matchedCount"] += int(out.get("matchedCount") or 0)
                agg["withFiles"] += int(out.get("withFiles") or 0)
            except Exception:
                pass
            try:
                agg["details"].append(
                    {
                        "frame_url": str(getattr(fr, "url", "") or "")[:200],
                        "matched": out.get("matched") or [],
                        "withFilesList": out.get("withFilesList") or [],
                        "total": int(out.get("total") or 0),
                    }
                )
            except Exception:
                pass
        return agg

    def _detect_recent_upload_request_suspect(self, step_number: int, filename: str) -> dict:
        try:
            now = float(time.time())
        except Exception:
            now = 0.0
        sn = int(step_number or 0)
        fname = str(filename or "").lower().strip()
        hits = []
        for _, r in list((self._cdp_request_map or {}).items())[-200:]:
            try:
                if int(r.get("step_number") or 0) != sn:
                    continue
                ts = float(r.get("ts") or 0.0)
                if now and ts and (now - ts) > 12.0:
                    continue
                url = str(r.get("url") or "")
                method = str(r.get("method") or "").upper()
                if method not in ("POST", "PUT", "PATCH"):
                    continue
                u = url.lower()
                if not any(k in u for k in ["upload", "import", "file", "asset", "media", "attachment", "ingest"]):
                    continue
                pd = str(r.get("post_data") or "")
                if fname and fname in pd.lower():
                    hits.append({"url": url[:200], "method": method, "matched_file": True})
                else:
                    hits.append({"url": url[:200], "method": method, "matched_file": False})
            except Exception:
                continue
        return {"suspected": bool(hits), "hits": hits[:5]}

    async def _inject_hint_overlay_async(self, page, text: str):
        if not page:
            return
        now_ts = time.time()
        try:
            if (now_ts - float(self._hint_overlay_last_ts or 0.0)) < 2.0:
                return
        except Exception:
            pass
        self._hint_overlay_last_ts = now_ts
        msg = str(text or "").strip()[:200]
        if not msg:
            return
        js = r"""
        (msg) => {
          try {
            const id = '__qa_assistant_hint';
            let el = document.getElementById(id);
            if (!el) {
              el = document.createElement('div');
              el.id = id;
              el.style.position = 'fixed';
              el.style.right = '14px';
              el.style.bottom = '14px';
              el.style.zIndex = '2147483647';
              el.style.maxWidth = '420px';
              el.style.padding = '10px 12px';
              el.style.borderRadius = '12px';
              el.style.border = '1px solid rgba(59,130,246,.35)';
              el.style.background = 'rgba(59,130,246,.10)';
              el.style.color = 'rgba(15,23,42,.88)';
              el.style.fontSize = '13px';
              el.style.boxShadow = '0 6px 24px rgba(15,23,42,.10)';
              el.style.backdropFilter = 'blur(6px)';
              el.style.pointerEvents = 'none';
              document.body.appendChild(el);
            }
            el.textContent = msg;
            el.style.display = 'block';
            clearTimeout(el.__qaTimer);
            el.__qaTimer = setTimeout(() => { try { el.style.display='none'; } catch(e){} }, 3500);
          } catch(e) {}
        }
        """
        try:
            await page.evaluate(js, msg)
        except Exception:
            return

    async def _detect_current_template_name_async(self, page):
        if not page:
            return ""
        js = r"""
        () => {
          const nodes = Array.from(document.querySelectorAll('body *')).slice(0, 2000);
          const label = nodes.find(n => (n.textContent || '').trim() === 'Template');
          if (!label) return '';
          let cur = label;
          for (let i = 0; i < 5 && cur; i++) {
            const p = cur.parentElement;
            if (!p) break;
            const text = (p.innerText || '').trim();
            if (text && text.length < 200 && text.includes('Template') && text.split('\n').length <= 6) {
              const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
              const idx = lines.indexOf('Template');
              if (idx >= 0 && lines[idx + 1]) return lines[idx + 1].slice(0, 80);
            }
            cur = p;
          }
          return '';
        }
        """
        try:
            out = await page.evaluate(js)
            return str(out or "").strip()[:80]
        except Exception:
            return ""

    def _extract_expected_name_from_text(self, text: str) -> str:
        t = str(text or "")
        cand = []
        for pat in [r"[\"“”']([^\"“”']{2,60})[\"“”']", r"「([^」]{2,60})」", r"《([^》]{2,60})》"]:
            try:
                for m in re.finditer(pat, t):
                    s = str(m.group(1) or "").strip()
                    if 2 <= len(s) <= 40:
                        cand.append(s)
            except Exception:
                pass
        if not cand:
            return ""
        cand.sort(key=lambda x: len(x), reverse=True)
        return cand[0][:40]

    async def _ai_should_stop_now_async(self, hook_agent: Agent, step_number: int):
        if not self._llm:
            return False, False, ""
        title = self._case_title or ""
        step_lines = []
        for s in (self._testcase_steps or []):
            exp = self._format_expected_result(getattr(s, "expected_result", "") or "")
            if exp:
                step_lines.append(f"{s.step_number}. {s.description}（预期参考：{exp}）")
            else:
                step_lines.append(f"{s.step_number}. {s.description}")
        steps_text = "\n".join(step_lines)[:2500]

        actions = []
        try:
            h = getattr(hook_agent, "history", None)
            hist = getattr(h, "history", None) if h else None
            if hist:
                for it in hist[-3:]:
                    mo = getattr(it, "model_output", None)
                    act = None
                    if isinstance(mo, dict):
                        act = mo.get("action")
                    else:
                        act = getattr(mo, "action", None) if mo else None
                    if act is not None:
                        actions.append(self._summarize_action(act))
        except Exception:
            actions = []

        msgs = list(self._runtime_messages or [])[-6:]
        expected_hint = self._build_expected_match_hints()
        matched_phrases = expected_hint.get("matched") or []
        recent_msgs_6 = expected_hint.get("messages_tail") or []
        login_failed, login_brief = await self._check_login_failed_async()
        auth_info = {
            "seen_auth_response": bool(self._seen_auth_response),
            "auth_status": self._last_auth_status,
            "auth_url": self._last_auth_url,
            "login_failed_inferred": login_failed,
            "login_brief": login_brief,
        }

        latest_msg_text = "；".join([str(x) for x in recent_msgs_6 if x][-3:])[:400]
        done_cnt = int(len(self._case_steps_done or []))
        total_cnt = int(self._case_steps_total or len(self._testcase_steps or []))
        prompt = (
            "你是自动化测试执行助手。注意：本系统要求严格按用例步骤执行，除非出现阻塞性问题，否则不能提前结束。\n"
            "现在请判断：是否存在“阻塞性问题（无法继续执行）”，需要立即停止并退出？\n"
            "只输出 JSON，不要输出解释："
            "{\"stop\": true/false, \"blocking\": true/false, \"verdict\": \"fail/uncertain\", \"reason\": \"...\"}\n"
            "规则：\n"
            "1) 只有在 blocking=true 且 stop=true 时才允许停止。\n"
            "2) 非阻塞性问题（例如页面提示错误但仍可继续操作）必须 stop=false，blocking=false。\n"
            "3) 仅凭“疑似已满足预期片段”不能停止；除非它同时意味着无法继续（例如鉴权失败导致无法进入系统）。\n\n"
            f"用例：{title}\n"
            f"已完成用例步骤（自报，仅供参考）：{done_cnt}/{total_cnt}\n"
            f"最近3个AI动作：{actions}\n"
            f"已捕获提示（最近6条）：{msgs}\n"
            f"最近提示（最近3条合并）：{latest_msg_text}\n"
            f"疑似已满足的预期片段（仅供参考）：{matched_phrases}\n"
            f"登录/鉴权信息：{json.dumps(auth_info, ensure_ascii=False)}\n"
            f"当前步骤序号：{step_number}\n"
        )

        try:
            resp = None
            if hasattr(self._llm, "ainvoke"):
                try:
                    resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
                except Exception:
                    resp = await self._llm.ainvoke(prompt)
            else:
                return False, False, ""
            text = getattr(resp, "content", None)
            if text is None:
                text = str(resp)
            raw = str(text).strip()
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return False, False, ""
            obj = json.loads(m.group(0))
            if not isinstance(obj, dict):
                return False, False, ""
            stop_v = bool(obj.get("stop"))
            blocking_v = bool(obj.get("blocking"))
            verdict = str(obj.get("verdict") or "").strip().lower()
            reason = str(obj.get("reason") or "").strip()[:300]
            if verdict:
                reason = f"verdict={verdict}; {reason}".strip()[:300]
            if not (stop_v and blocking_v):
                return False, False, reason
            return True, True, reason
        except Exception:
            return False, False, ""

    async def _ai_judge_execution_async(self, history):
        if not self._llm:
            return None
        title = self._case_title or ""
        step_lines = []
        for s in (self._testcase_steps or []):
            exp = self._format_expected_result(getattr(s, "expected_result", "") or "")
            if exp:
                step_lines.append(f"{s.step_number}. {s.description}\n   预期参考：{exp}")
            else:
                step_lines.append(f"{s.step_number}. {s.description}")
        steps_text = "\n".join(step_lines)[:5000]

        msgs = list(self._runtime_messages or [])[-12:]
        network_texts = []
        try:
            network_texts = await self._collect_network_texts_async()
        except Exception:
            network_texts = []
        auth_summary = {
            "seen_auth_response": bool(self._seen_auth_response),
            "auth_status": self._last_auth_status,
            "auth_url": self._last_auth_url,
        }
        login_failed, login_brief = await self._check_login_failed_async()
        auth_summary["login_failed_inferred"] = login_failed
        auth_summary["login_brief"] = login_brief

        final_text = ""
        try:
            final_text = (history.final_result() or "")[:800]
        except Exception:
            final_text = ""

        prompt = (
            "你是资深测试工程师。请根据用例操作步骤、预期参考、页面提示/接口信息，像人一样判断本次测试是否通过，是否存在缺陷。\n"
            "注意：预期只是参考，你需要结合证据做结论。若发现缺陷，生成缺陷标题和现象描述（避免泄露账号/密码/token）。\n"
            "重要：若未执行完全部用例步骤（步骤完成标记未覆盖全部步骤），则 passed 必须为 false，并在 summary 说明未完成的原因/缺失步骤。\n"
            "只输出 JSON，不要输出解释：\n"
            "{\"passed\": true/false, \"summary\": \"...\", \"bug_found\": true/false, \"bug\": {\"title\": \"...\", \"description\": \"...\"}}\n"
            "summary 2-6 句中文，包含关键证据（提示文案/接口状态码/页面是否仍在登录页等）。\n\n"
            f"用例：{title}\n"
            f"步骤完成标记：{int(len(self._case_steps_done or []))}/{int(self._case_steps_total or len(self._testcase_steps or []))}\n"
            f"非阻塞疑似问题记录（可能为空）：{list(self._non_blocking_issue_notes or [])[-8:]}\n"
            f"操作步骤与预期参考：\n{steps_text}\n\n"
            f"AI执行输出（可能为空）：{final_text}\n"
            f"捕获到的提示/Toast（最近12条）：{msgs}\n"
            f"登录/鉴权摘要：{json.dumps(auth_summary, ensure_ascii=False)}\n"
            "最近接口响应片段（可能为空，已脱敏）：\n"
            + "\n".join([str(x)[:400] for x in (network_texts or [])[:12]])
        )

        try:
            resp = None
            if hasattr(self._llm, "ainvoke"):
                try:
                    resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
                except Exception:
                    resp = await self._llm.ainvoke(prompt)
            else:
                return None
            text = getattr(resp, "content", None)
            if text is None:
                text = str(resp)
            raw = str(text).strip()
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return None
            obj = json.loads(m.group(0))
            if not isinstance(obj, dict):
                return None
            passed = bool(obj.get("passed"))
            bug_found = bool(obj.get("bug_found"))
            summary = str(obj.get("summary") or "").strip()[:2000]
            bug = obj.get("bug") if isinstance(obj.get("bug"), dict) else {}
            title_v = str(bug.get("title") or "").strip()[:120]
            desc_v = str(bug.get("description") or "").strip()[:6000]
            if not bug_found:
                title_v = ""
                desc_v = ""
            return {
                "passed": passed,
                "bug_found": bug_found,
                "summary": summary,
                "bug": {"title": title_v, "description": desc_v},
            }
        except Exception:
            return None

    @sync_to_async
    def _create_bug_from_ai_async(self, ai_summary: str, suggested_title: str = None, suggested_description: str = None):
        execution = AutoTestExecution.objects.select_related("case", "case__project").get(id=self.execution_id)
        case = execution.case
        project = case.project
        signature = f"AUTO_TEST_EXECUTION={execution.id}"
        existing = Bug.objects.filter(project=project, case=case, description__contains=signature).first()
        if existing:
            if not existing.assignee_id and execution.executor_id:
                try:
                    existing.assignee_id = execution.executor_id
                    existing.save(update_fields=["assignee"])
                except Exception:
                    pass
            return existing.id

        steps = list(case.steps.all().order_by("step_number"))
        reproduce_lines = []
        for s in steps:
            reproduce_lines.append(f"{s.step_number}. {s.description}")
            exp = self._format_expected_result(getattr(s, 'expected_result', '') or '')
            if exp:
                reproduce_lines.append(f"   预期参考: {exp}")
        reproduce_steps = "\n".join(reproduce_lines)[:8000]

        recent_msgs = "；".join((self._runtime_messages or [])[-12:])
        ds_name = str(getattr(execution, "dataset_name", "") or "").strip()
        ds_vars = getattr(execution, "dataset_vars", {}) or {}
        ds_line = ""
        try:
            if ds_name or (isinstance(ds_vars, dict) and ds_vars):
                ds_line = f"\n数据集：{ds_name or '-'}\n变量：{json.dumps(ds_vars, ensure_ascii=False)[:1200]}"
        except Exception:
            ds_line = ""
        base_description = (
            f"{signature}\n"
            f"执行记录：AutoTestExecution#{execution.id}\n"
            f"AI结论：\n{(ai_summary or '').strip()}\n\n"
            f"近期提示/Toast：{recent_msgs}"
            f"{ds_line}"
        ).strip()[:7800]

        title = (suggested_title or "").strip()
        if not title:
            title = f"[AI执行][疑似缺陷] {case.title}".strip()
        if len(title) > 120:
            title = title[:120]

        description = (suggested_description or "").strip()
        if description:
            description = (description + "\n\n" + base_description).strip()[:8000]
        else:
            description = base_description[:8000]

        bug = Bug.objects.create(
            project=project,
            case=case,
            title=title,
            description=description,
            reproduce_steps=reproduce_steps,
            creator=execution.executor,
            assignee=execution.executor,
            severity=3,
            priority=2,
            status=1,
        )
        return bug.id

    def _summarize_action(self, action_value) -> str:
        try:
            if not action_value:
                return ""
            act = action_value
            if isinstance(act, list) and act:
                act = act[0]
            if isinstance(act, dict) and act:
                name = next(iter(act.keys()), "")
                payload = act.get(name) if name else None
                if isinstance(payload, dict):
                    if name == "go_to_url":
                        url = payload.get("url") or ""
                        return ("打开URL: " + str(url))[:160]
                    if name == "click_element_by_index":
                        idx = payload.get("index")
                        return f"点击元素索引: {idx}"
                    if name == "input_text":
                        txt = payload.get("text") or ""
                        return ("输入文本: " + (str(txt)[:60]))[:160]
                    if name == "scroll_down":
                        amt = payload.get("amount")
                        return f"向下滚动: {amt}"
                    if name == "scroll_up":
                        amt = payload.get("amount")
                        return f"向上滚动: {amt}"
                    if name == "press_key":
                        k = payload.get("key") or ""
                        return ("按键: " + str(k))[:80]
                    if name == "wait":
                        s = payload.get("seconds")
                        return f"等待: {s}s"
                    if name == "done":
                        return "完成"
                    short = json.dumps(payload, ensure_ascii=False)
                    return (name + ": " + short)[:180]
                return (str(name) or "")[:120]
            return str(action_value)[:180]
        except Exception:
            try:
                return str(action_value)[:180]
            except Exception:
                return ""

    def _is_submit_like_action(self, description: str, action_script: str, ai_thought: str) -> bool:
        desc = str(description or "").strip()
        if not desc:
            return False
        desc_l = desc.lower()
        act_l = str(action_script or "").lower()
        thought_l = str(ai_thought or "").lower()
        click_like = False
        try:
            click_like = (
                ("click" in act_l)
                or ("press_key" in act_l)
                or ("enter" in act_l)
                or ("点击" in desc)
                or ("按键" in desc)
                or ("click" in desc_l)
                or ("clicked" in desc_l)
            )
        except Exception:
            click_like = False
        if not click_like:
            return False
        if (
            any(k in desc for k in ["保存", "提交", "确认", "确定", "登录"])
            or any(k in desc_l for k in ["save", "submit", "confirm", "ok", "login", "signin"])
            or any(k in ai_thought for k in ["保存", "提交", "确认", "确定", "登录"])
            or any(k in thought_l for k in ["save", "submit", "confirm", "ok", "login", "signin"])
        ):
            return True
        return False

    async def _quick_assert_expected_results_async(self):
        steps = self._testcase_steps or []
        if not steps:
            return False, ""
        messages = list(self._runtime_messages or [])
        try:
            messages.extend(await self._collect_new_runtime_messages_async())
        except Exception:
            pass
        network_texts = []
        try:
            network_texts = await self._collect_network_texts_async()
        except Exception:
            network_texts = []
        norm_messages = [self._norm_text(m) for m in (messages or []) if m]
        norm_network = [self._norm_text(n) for n in (network_texts or []) if n]
        auth_body = self._last_auth_body_norm or ""

        pw_aliases = self._expand_phrase_aliases("密码错误")
        found_password_error = False
        for a in pw_aliases:
            na = self._norm_text(a)
            if not na:
                continue
            if any(na in nm for nm in norm_messages) or any(na in nn for nn in norm_network) or (auth_body and na in auth_body):
                found_password_error = True
                break

        login_failed, login_brief = await self._check_login_failed_async()

        missing = []
        for step in steps:
            expected_raw = getattr(step, "expected_result", "") or ""
            if self._expect_password_error(expected_raw) and not found_password_error:
                missing.append("密码错误提示")
            if self._expect_login_fail(expected_raw) and not login_failed:
                missing.append("无法登录成功" + (f"（{login_brief}）" if login_brief else ""))
            phrases = self._extract_expect_phrases(expected_raw)
            if not phrases:
                continue
            for ph in phrases:
                if self._expect_login_fail(ph):
                    continue
                aliases = self._expand_phrase_aliases(ph)
                found = False
                for a in aliases:
                    na = self._norm_text(a)
                    if not na:
                        continue
                    if any(na in nm for nm in norm_messages):
                        found = True
                        break
                    if any(na in nn for nn in norm_network):
                        found = True
                        break
                if not found:
                    missing.append(ph)
        if not missing:
            brief = ""
            if messages:
                brief = "断言：通过（已捕获提示：" + "；".join(messages[-3:]) + "）"
            return True, brief
        return False, ""

    @sync_to_async
    def _collect_recent_network_entries_async(self, step_number: int, limit: int = 40) -> list[dict]:
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        try:
            s = int(step_number or 0)
        except Exception:
            s = 0
        min_step = max(0, s - 2)
        qs = (
            AutoTestNetworkEntry.objects.filter(step_record__execution=execution, step_record__step_number__gte=min_step)
            .order_by("-timestamp")[: int(limit or 40)]
        )
        out = []
        for e in qs:
            out.append(
                {
                    "url": str(getattr(e, "url", "") or "")[:500],
                    "method": str(getattr(e, "method", "") or "")[:12],
                    "status": int(getattr(e, "status_code", 0) or 0),
                }
            )
        return out

    async def _assert_expected_for_case_step_async(self, case_step_no: int, step_number: int, strict: bool = True, create_bug: bool = True):
        step = self._find_case_step_by_number(case_step_no)
        if not step:
            return True, "", None
        expected_raw = getattr(step, "expected_result", "") or ""
        expected_norm = self._format_expected_result(expected_raw)
        phrases = self._extract_expect_phrases(expected_raw)
        if not phrases and not self._expect_password_error(expected_raw) and not self._expect_login_fail(expected_raw) and not self._expect_phone_error(expected_raw):
            return True, "", None

        messages = list(self._runtime_messages or [])
        try:
            messages.extend(await self._collect_new_runtime_messages_async())
        except Exception:
            pass
        page = await self._get_active_page_async()
        body_text = ""
        if page:
            try:
                body_text = await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
            except Exception:
                body_text = ""
        network_texts = []
        try:
            network_texts = await self._collect_network_texts_async()
        except Exception:
            network_texts = []

        norm_messages = [self._norm_text(m) for m in (messages or []) if m]
        norm_body = self._norm_text(body_text or "")
        norm_network = [self._norm_text(n) for n in (network_texts or []) if n]
        auth_body = self._last_auth_body_norm or ""

        recent_msgs = "；".join([str(x) for x in (messages or [])[-8:] if x])[:800]
        success_msg = ""
        try:
            for m in reversed((messages or [])[-12:]):
                ms = str(m or "").strip()
                if not ms:
                    continue
                lower = ms.lower()
                if any(k in ms for k in ["成功", "新增成功", "创建成功", "保存成功", "提交成功", "操作成功"]) or ("success" in lower):
                    success_msg = ms[:200]
                    break
        except Exception:
            success_msg = ""

        recent_net = []
        try:
            recent_net = await self._collect_recent_network_entries_async(int(step_number), limit=60)
        except Exception:
            recent_net = []
        success_net = ""
        try:
            for e in recent_net or []:
                u = str(e.get("url") or "").lower()
                m = str(e.get("method") or "").upper()
                st = int(e.get("status") or 0)
                if m in ("POST", "PUT", "PATCH") and 200 <= st < 300:
                    if any(k in u for k in ["login", "auth", "token", "session"]):
                        continue
                    success_net = f"{m} {st} {str(e.get('url') or '')[:180]}"
                    break
        except Exception:
            success_net = ""

        neg_fail = ""
        if self._expect_phone_error(expected_raw):
            if success_msg or success_net:
                evidence = "；".join([x for x in [success_msg, success_net] if x])[:360]
                neg_fail = f"预期：{expected_norm or expected_raw}\n实际：疑似新增/保存成功（{evidence}）"
        if (not neg_fail) and (
            any(k in str(expected_raw or "") for k in ["错误", "失败", "不正确", "无权限", "格式错误", "无效", "不合法"])
            or any(k in str(expected_raw or "").lower() for k in ["invalid", "error", "failed", "unauthorized", "forbidden"])
        ):
            if success_msg or success_net:
                evidence = "；".join([x for x in [success_msg, success_net] if x])[:360]
                neg_fail = f"预期：{expected_norm or expected_raw}\n实际：疑似成功（{evidence}）"

        pw_aliases = self._expand_phrase_aliases("密码错误")
        found_password_error = False
        for a in pw_aliases:
            na = self._norm_text(a)
            if not na:
                continue
            if any(na in nm for nm in norm_messages) or (norm_body and na in norm_body) or any(na in nn for nn in norm_network) or (auth_body and na in auth_body):
                found_password_error = True
                break
        login_failed, login_brief = await self._check_login_failed_async()

        missing = []
        if self._expect_password_error(expected_raw) and not found_password_error:
            missing.append("密码错误提示")
        if self._expect_login_fail(expected_raw) and not login_failed:
            missing.append("无法登录成功" + (f"（{login_brief}）" if login_brief else ""))
        for ph in phrases:
            if self._expect_login_fail(ph):
                continue
            aliases = self._expand_phrase_aliases(ph)
            found = False
            for a in aliases:
                na = self._norm_text(a)
                if not na:
                    continue
                if any(na in nm for nm in norm_messages) or (norm_body and na in norm_body) or any(na in nn for nn in norm_network):
                    found = True
                    break
            if not found:
                missing.append(ph)

        ok = (not neg_fail) and ((not missing) or (not strict))
        summary = ""
        if ok:
            summary = f"步骤{int(case_step_no)} 断言：通过"
        else:
            parts = [f"步骤{int(case_step_no)} 断言：失败"]
            if neg_fail:
                parts.append(neg_fail)
            if missing:
                parts.append("缺失：" + "、".join([str(x) for x in missing[:8]]))
            if recent_msgs:
                parts.append("提示：" + recent_msgs)
            if success_net:
                parts.append("接口：" + success_net)
            summary = "\n".join([p for p in parts if p]).strip()[:2000]

        created_bug_id = None
        if (not ok) and bool(create_bug):
            try:
                execution = await sync_to_async(AutoTestExecution.objects.select_related("case").get)(id=self.execution_id)
                case_title = execution.case.title
                suggested = await self._suggest_bug_content_async(
                    case_title=case_title,
                    assertion_summary=summary,
                    recent_messages=recent_msgs,
                    network_snippets=network_texts[:10],
                )
                created_bug_id = await self._create_bug_for_assertion_async(
                    assertion_summary=summary,
                    suggested_title=(suggested or {}).get("title"),
                    suggested_description=(suggested or {}).get("description"),
                )
            except Exception:
                created_bug_id = None

        return ok, summary, created_bug_id

    async def _get_active_page_async(self):
        if not self._pw_contexts:
            return None
        try:
            pages = []
            for ctx in list(self._pw_contexts or []):
                try:
                    pages.extend(list(ctx.pages))
                except Exception:
                    continue
            if not pages:
                return None
            for p in reversed(pages):
                try:
                    url = p.url or ""
                    if url and not url.startswith("about:"):
                        return p
                except Exception:
                    continue
            return pages[-1]
        except Exception:
            return None

    async def _is_login_page_async(self, page) -> bool:
        if not page:
            return False
        try:
            return bool(
                await page.evaluate(
                    """() => {
  const pwd = document.querySelector('input[type=password],input[name*=pass],input[placeholder*=密],input[aria-label*=密]');
  if (!pwd) return false;
  const user = document.querySelector('input[name*=user],input[name*=name],input[placeholder*=用户],input[placeholder*=账号],input[aria-label*=用户],input[aria-label*=账号],input[type=email],input[type=text]');
  if (!user) return true;
  return true;
}"""
                )
            )
        except Exception:
            try:
                url_l = (page.url or "").lower()
                return ("login" in url_l) or ("signin" in url_l)
            except Exception:
                return False

    async def _auto_login_if_needed_async(self, step_number: int):
        if self._auto_login_done:
            return
        try:
            if self._case_expects_login_fail():
                return
        except Exception:
            pass
        try:
            from_steps = self._extract_login_from_steps(self._testcase_steps or [])
            if str((from_steps or {}).get("username") or "").strip() or str((from_steps or {}).get("password") or "").strip():
                return
        except Exception:
            pass
        defaults = await self._get_project_login_defaults_async()
        username = str((defaults or {}).get("username") or "").strip()
        password = str((defaults or {}).get("password") or "").strip()
        if not (username and password):
            return
        page = await self._get_active_page_async()
        if not (await self._is_login_page_async(page)):
            return
        self._auto_login_done = True
        self._login_attempted = True
        self._login_attempted_ts = time.time()

        async def fill_first(selectors: list[str], value: str) -> bool:
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() <= 0:
                        continue
                    await loc.first.fill(value, timeout=1200)
                    return True
                except Exception:
                    continue
            return False

        filled_user = await fill_first(
            [
                'input[placeholder*="用户名"]',
                'input[placeholder*="账号"]',
                'input[aria-label*="用户名"]',
                'input[aria-label*="账号"]',
                'input[name*="user"]',
                'input[name*="name"]',
                "input[type=email]",
                "input[type=text]",
            ],
            username,
        )
        filled_pwd = await fill_first(
            [
                'input[placeholder*="密码"]',
                'input[aria-label*="密码"]',
                "input[type=password]",
                'input[name*="pass"]',
            ],
            password,
        )

        clicked = False
        for sel in [
            'button:has-text("登录")',
            'button:has-text("登陆")',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
            "button[type=submit]",
            "input[type=submit]",
        ]:
            try:
                loc = page.locator(sel)
                if await loc.count() <= 0:
                    continue
                await loc.first.click(timeout=1200)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            try:
                await page.locator("input[type=password]").first.press("Enter", timeout=800)
            except Exception:
                pass
        pwd_len = 0
        pwd_fp = ""
        try:
            import hashlib
            v = await page.evaluate(
                """() => {
  const u = document.querySelector('input[placeholder*=\"用户名\"],input[placeholder*=\"账号\"],input[aria-label*=\"用户名\"],input[aria-label*=\"账号\"],input[name*=\"user\"],input[name*=\"name\"],input[type=email],input[type=text]');
  const p = document.querySelector('input[placeholder*=\"密码\"],input[aria-label*=\"密码\"],input[type=password],input[name*=\"pass\"]');
  return { u: u ? (u.value || '') : '', p: p ? (p.value || '') : '' };
}"""
            )
            pv = str((v or {}).get("p") or "")
            pwd_len = len(pv)
            if pv:
                pwd_fp = hashlib.sha256(pv.encode("utf-8", "ignore")).hexdigest()[:12]
        except Exception:
            pwd_len = 0
            pwd_fp = ""
        try:
            await page.wait_for_timeout(900)
        except Exception:
            pass
        try:
            await self._append_step_note_async(int(step_number), f"已自动使用项目默认账号发起登录：{username}/{password}")
            await self._merge_step_metrics_async(
                int(step_number),
                {
                    "auto_login": True,
                    "auto_login_filled_user": bool(filled_user),
                    "auto_login_filled_pwd": bool(filled_pwd),
                    "auto_login_clicked": bool(clicked),
                    "auto_login_username": username[:80],
                    "auto_login_password": password[:120],
                    "auto_login_password_len": int(pwd_len),
                    "auto_login_password_sha256_12": str(pwd_fp),
                },
            )
        except Exception:
            pass

    async def _collect_new_runtime_messages_async(self) -> list[str]:
        page = await self._get_active_page_async()
        if not page:
            return []
        messages = []
        for frame in getattr(page, "frames", []) or []:
            try:
                messages.extend(await frame.evaluate("() => (window.__qa_messages || []).map(x => x.text)"))
            except Exception:
                continue
        if not messages:
            try:
                messages.extend(await self._collect_accessibility_alerts_async(page))
            except Exception:
                pass
        new_messages = []
        for t in messages or []:
            try:
                text = str(t).strip()
            except Exception:
                continue
            if not text:
                continue
            key = f"{len(text)}:{hash(text)}"
            if key in self._seen_message_keys:
                continue
            self._seen_message_keys.add(key)
            new_messages.append(text)
        return new_messages

    async def _collect_accessibility_alerts_async(self, page):
        try:
            snap = await page.accessibility.snapshot()
        except Exception:
            return []
        if not isinstance(snap, dict):
            return []
        out = []
        roles = {"alert", "status", "dialog", "alertdialog", "tooltip"}

        def walk(node):
            if not isinstance(node, dict):
                return
            role = (node.get("role") or "").lower()
            name = (node.get("name") or "").strip()
            if role in roles and name:
                name = re.sub(r"\s+", " ", name).strip()
                if 3 <= len(name) <= 200:
                    out.append(name)
            for ch in node.get("children") or []:
                walk(ch)

        walk(snap)
        uniq = []
        seen = set()
        for x in out:
            k = self._norm_text(x)
            if not k or k in seen:
                continue
            seen.add(k)
            uniq.append(x)
        return uniq[:10]

    async def _capture_toast_after_auth_async(self, step_number: int):
        try:
            self._seen_auth_response = True
            self._last_auth_step_number = int(step_number)
        except Exception:
            pass
        try:
            await asyncio.sleep(0.005)
        except Exception:
            pass
        try:
            new_msgs = await self._collect_new_runtime_messages_async()
            if new_msgs:
                self._runtime_messages.extend(new_msgs)
                await self._append_step_note_async(
                    step_number,
                    "登录/鉴权后立即捕获提示: " + "；".join(new_msgs[-5:]),
                )
                return
        except Exception:
            pass

        if not bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)):
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "disabled"})
            except Exception:
                pass
            return
        if step_number in self._vision_checked_steps:
            return
        self._vision_checked_steps.add(step_number)
        try:
            now_ts = time.time()
            min_interval = float(getattr(settings, "AI_EXEC_TOAST_OCR_MIN_INTERVAL_S", 2) or 2)
            if now_ts - float(self._last_vision_toast_ts or 0.0) < max(0.0, min_interval):
                try:
                    await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "throttled"})
                except Exception:
                    pass
                return
            if not self._qwen_api_key_available():
                try:
                    await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "missing_api_key"})
                except Exception:
                    pass
                return
            toast_text, evidence = await self._try_capture_toast_by_qwen_vl_async()
            if toast_text:
                self._runtime_messages.append(toast_text)
                await self._append_step_note_async(step_number, "Qwen-VL截图识别提示: " + toast_text)
                try:
                    patch = {"ocr_result": "found", "ocr_text": toast_text}
                    if evidence:
                        await self._attach_step_ocr_screenshot_async(step_number, evidence)
                    await self._merge_step_metrics_async(step_number, patch)
                except Exception:
                    pass
            else:
                try:
                    await self._merge_step_metrics_async(step_number, {"ocr_result": "empty"})
                except Exception:
                    pass
            self._last_vision_toast_ts = now_ts
        except Exception:
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "error"})
            except Exception:
                pass
            return

    async def _try_capture_toast_by_qwen_vl_async(self):
        page = await self._get_active_page_async()
        if not page:
            return None, None
        try:
            img_bytes = await page.screenshot(type="png", full_page=False)
        except Exception:
            return None, None
        if not img_bytes:
            return None, None
        crop_bytes = img_bytes
        try:
            crop_bytes = await self._crop_top_region_async(img_bytes)
        except Exception:
            crop_bytes = img_bytes

        text = await self._qwen_vl_extract_toast_text_async(crop_bytes)
        evidence = None
        try:
            import base64
            evidence = "data:image/png;base64," + base64.b64encode(crop_bytes).decode("ascii")
        except Exception:
            evidence = None
        return text, evidence

    async def _maybe_capture_toast_from_step_screenshot_async(self, step_number: int, screenshot_data: str):
        if not bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)):
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "disabled"})
            except Exception:
                pass
            return
        if self._toast_vision_found:
            return
        if not screenshot_data:
            return
        if step_number in self._toast_vision_checked_steps:
            return
        if step_number > 10:
            return
        self._toast_vision_checked_steps.add(step_number)

        png_bytes = None
        try:
            import base64
            raw = screenshot_data
            if ";base64," in raw:
                _, b64 = raw.split(";base64,")
            else:
                b64 = raw
            png_bytes = base64.b64decode(b64)
        except Exception:
            png_bytes = None
        if not png_bytes:
            return

        now_ts = time.time()
        min_interval = float(getattr(settings, "AI_EXEC_TOAST_OCR_MIN_INTERVAL_S", 2) or 2)
        if now_ts - float(self._last_vision_toast_ts or 0.0) < max(0.0, min_interval):
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "throttled"})
            except Exception:
                pass
            return
        if not self._qwen_api_key_available():
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "skipped", "ocr_skip_reason": "missing_api_key"})
            except Exception:
                pass
            return
        toast_text = await self._qwen_vl_extract_toast_text_async(png_bytes)
        if not toast_text:
            toast_text = await self._qwen_vl_extract_toast_text_async(await self._crop_top_region_async(png_bytes))
        if not toast_text:
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "empty"})
            except Exception:
                pass
            return

        self._toast_vision_found = True
        self._last_vision_toast_ts = now_ts
        self._runtime_messages.append(toast_text)
        await self._append_step_note_async(step_number, "Qwen-VL(截图定位)识别提示: " + toast_text)
        try:
            import base64
            cropped = await self._crop_top_region_async(png_bytes)
            evidence = "data:image/png;base64," + base64.b64encode(cropped).decode("ascii")
            await self._attach_step_ocr_screenshot_async(step_number, evidence)
            await self._merge_step_metrics_async(step_number, {"ocr_result": "found", "ocr_text": toast_text})
        except Exception:
            try:
                await self._merge_step_metrics_async(step_number, {"ocr_result": "found", "ocr_text": toast_text})
            except Exception:
                pass

    async def _crop_top_region_async(self, png_bytes: bytes) -> bytes:
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            w, h = im.size
            if w <= 0 or h <= 0:
                return png_bytes
            crop = im.crop((int(w * 0.05), 0, int(w * 0.95), int(h * 0.35)))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return png_bytes

    async def _qwen_vl_extract_toast_text_async(self, png_bytes: bytes):
        if not bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)):
            return None
        params = None
        try:
            if self._executor_user is not None:
                params = resolve_ocr_params(self._executor_user)
        except Exception:
            params = None
        api_key = (getattr(params, "api_key", "") if params else "") or ""
        base_url = (getattr(params, "base_url", "") if params else "") or ""
        model_name = (getattr(params, "model", "") if params else "") or ""
        url = self._chat_completions_url(base_url)
        if not (api_key and url and model_name):
            return None
        try:
            import base64
            data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        except Exception:
            return None

        prompt = (
            "请从截图中识别页面的 toast/错误提示/弹窗提示 文案（通常在页面顶部）。"
            "如果存在，请输出最可能的一条提示文案；如果没有，输出空。"
            "只输出 JSON：{\"found\": true/false, \"text\": \"...\"}，text 不超过 200 字。"
        )
        req_body = {
            "model": model_name,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }

        def do_request():
            import urllib.request
            data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            timeout_s = int(getattr(settings, "AI_EXEC_TOAST_OCR_TIMEOUT_S", 8) or 8)
            with urllib.request.urlopen(req, timeout=max(1, timeout_s)) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return raw

        try:
            raw = await asyncio.to_thread(do_request)
        except Exception:
            return None
        try:
            obj = json.loads(raw)
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                return None
            s = str(content).strip()
            m = re.search(r"\{[\s\S]*\}", s)
            if m:
                j = json.loads(m.group(0))
                if isinstance(j, dict) and j.get("found") and j.get("text"):
                    t = str(j.get("text")).strip()
                    t = re.sub(r"\s+", " ", t).strip()
                    if 3 <= len(t) <= 220:
                        return t
                return None
            t = re.sub(r"\s+", " ", s).strip()
            if 3 <= len(t) <= 220:
                return t
        except Exception:
            return None
        return None

    async def _qwen_vl_extract_guide_hint_async(self, png_bytes: bytes):
        if not bool(getattr(settings, "AI_EXEC_GUIDE_HINT_ENABLED", True)):
            return None
        params = None
        try:
            if self._executor_user is not None:
                params = resolve_ocr_params(self._executor_user)
        except Exception:
            params = None
        api_key = (getattr(params, "api_key", "") if params else "") or ""
        base_url = (getattr(params, "base_url", "") if params else "") or ""
        model_name = (getattr(params, "model", "") if params else "") or ""
        url = self._chat_completions_url(base_url)
        if not (api_key and url and model_name):
            return None
        try:
            import base64
            data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        except Exception:
            return None

        prompt = (
            "你是自动化测试执行助手。用户提供的是“参考截图/原型截图/标注截图”，"
            "截图中可能有红框/红色标注来指出需要操作的目标区域。\n"
            "请你：\n"
            "1) 识别红框/标注指向的目标（按钮/输入框/下拉/链接/列表项等）；\n"
            "2) 给出可执行的定位提示（目标附近可见文字、标签、占位符、图标含义、相对位置等）；\n"
            "3) 如果需要输入，请指出输入应来自用例变量（如 {{var}}）还是固定文本。\n"
            "只输出 JSON：{\"hint\": \"...\"}，hint 不超过 220 字。"
        )
        req_body = {
            "model": model_name,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }

        def do_request():
            import urllib.request
            data = json.dumps(req_body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            timeout_s = int(getattr(settings, "AI_EXEC_GUIDE_HINT_TIMEOUT_S", 12) or 12)
            with urllib.request.urlopen(req, timeout=max(1, timeout_s)) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return raw

        try:
            raw = await asyncio.to_thread(do_request)
        except Exception:
            return None
        try:
            obj = json.loads(raw)
            content = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                return None
            s = str(content).strip()
            m = re.search(r"\{[\s\S]*\}", s)
            if m:
                j = json.loads(m.group(0))
                if isinstance(j, dict) and j.get("hint"):
                    t = str(j.get("hint")).strip()
                    t = re.sub(r"\s+", " ", t).strip()
                    if 3 <= len(t) <= 240:
                        return t[:220]
                return None
            t = re.sub(r"\s+", " ", s).strip()
            if 3 <= len(t) <= 220:
                return t
        except Exception:
            return None
        return None

    @sync_to_async
    def _attach_step_screenshot_after_async(self, step_number: int, screenshot_data: str):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
        if not step_record:
            return

    @sync_to_async
    def _attach_step_ocr_screenshot_async(self, step_number: int, screenshot_data: str):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
        if not step_record:
            return
        if not screenshot_data:
            return
        try:
            import base64
            img_data = screenshot_data
            imgstr = None
            ext = "png"
            if ";base64," in img_data:
                format_part, imgstr = img_data.split(";base64,")
                ext = format_part.split("/")[-1] or "png"
            else:
                imgstr = img_data
            data = ContentFile(base64.b64decode(imgstr), name=f'ocr_{step_number}_{uuid.uuid4().hex}.{ext}')
            step_record.ocr_screenshot = data
            step_record.save(update_fields=["ocr_screenshot"])
        except Exception:
            return
        if not screenshot_data:
            return
        try:
            import base64
            img_data = screenshot_data
            imgstr = None
            ext = "png"
            if ";base64," in img_data:
                format_part, imgstr = img_data.split(";base64,")
                ext = format_part.split("/")[-1] or "png"
            else:
                imgstr = img_data
            data = ContentFile(base64.b64decode(imgstr), name=f'toast_{step_number}_{uuid.uuid4().hex}.{ext}')
            step_record.screenshot_after = data
            step_record.save(update_fields=["screenshot_after"])
        except Exception:
            return

    async def _message_poller_loop(self):
        try:
            while True:
                try:
                    new_msgs = await self._collect_new_runtime_messages_async()
                    if new_msgs:
                        self._runtime_messages.extend(new_msgs)
                        try:
                            trigger_text = "；".join([str(x) for x in new_msgs[-3:] if x])[:300]
                            await self._maybe_ai_stop_from_async_trigger(int(self._current_step_number or 0), trigger_text)
                        except Exception:
                            pass
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return

    @sync_to_async
    def _append_step_note_async(self, step_number: int, note: str):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
        if not step_record:
            return
        existing = step_record.ai_thought or ""
        if note in existing:
            return
        step_record.ai_thought = (existing + "\n" + note).strip() if existing else note
        step_record.save(update_fields=["ai_thought"])

    @sync_to_async
    def _mark_step_failed_async(self, step_number: int, error_message: str):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=int(step_number)).first()
        if not step_record:
            return
        step_record.status = "failed"
        step_record.error_message = str(error_message or "")[:2000]
        step_record.save(update_fields=["status", "error_message"])

    @sync_to_async
    def _normalize_steps_after_assert_failed_async(self):
        if self._stop_reason not in ("assert_failed", "non_blocking_bug"):
            return
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_no = 0
        try:
            step_no = int(self._assert_failed_step_number or 0)
        except Exception:
            step_no = 0
        if step_no <= 0:
            try:
                cand = (
                    AutoTestStepRecord.objects.filter(execution=execution)
                    .order_by("-step_number")
                    .only("step_number", "metrics")
                )
                for s in cand:
                    m = s.metrics or {}
                    if (m or {}).get("bug_id") or (m or {}).get("stopped_reason") in ("assert_failed", "non_blocking_bug"):
                        step_no = int(s.step_number)
                        break
            except Exception:
                step_no = 0
        if step_no <= 0:
            return
        bug_id = 0
        try:
            bug_id = int(self._forced_bug_id or 0)
        except Exception:
            bug_id = 0
        summary = str(self._assert_failed_summary or "").strip()[:1600]
        if bug_id > 0:
            msg = f"发现缺陷：BUG-{bug_id}"
            if summary:
                msg = (msg + "\n" + summary).strip()
            AutoTestStepRecord.objects.filter(execution=execution, step_number=step_no).update(status="failed", error_message=msg[:2000])
        AutoTestStepRecord.objects.filter(execution=execution, step_number__gt=step_no).update(
            status="skipped", error_message="已发现缺陷，后续步骤未执行"
        )

    def _extract_expect_phrases(self, expected: str) -> list[str]:
        if not expected:
            return []
        s = unescape(str(expected))
        s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        s = re.sub(r"<[^>]+>", " ", s)
        lower = s.lower()
        has_quote = re.search(r"[\"“”'‘’][^\"“”'‘’]{2,120}[\"“”'‘’]", s) is not None
        has_keyword = any(
            k in s for k in ["提示", "弹窗", "toast", "错误", "失败", "不正确", "无权限", "不存在", "已存在"]
        ) or any(k in lower for k in ["toast", "snackbar", "incorrect", "error", "failed", "unauthorized", "forbidden"])
        if not (has_quote or has_keyword):
            return []
        parts = re.split(r"[\n;；。.!！?？]+", s)
        phrases = []
        for p in parts:
            p = re.sub(r"\s+", " ", p).strip()
            if not p:
                continue

            m = re.search(r"[\"“”'‘’]([^\"“”'‘’]{2,120})[\"“”'‘’]", p)
            if m:
                phrases.append(m.group(1).strip())

            m2 = re.search(r"(?:toast|提示|弹窗|提示信息|message|notification)[^:：]{0,20}[:：]?\s*([^;；。.!！?？\n]{2,120})", p, flags=re.I)
            if m2:
                phrases.append(m2.group(1).strip())

            phrases.append(p)

        stop = {
            "成功",
            "失败",
            "错误",
            "提示",
            "toast",
            "弹窗",
            "页面",
            "显示",
            "出现",
            "系统提示",
            "提示信息",
            "登录成功",
            "登录失败",
        }
        cleaned = []
        for x in phrases:
            x = re.sub(r"\s+", " ", x).strip()
            x = re.sub(r"^(?:\d+[\.\、]\s*)", "", x).strip()
            x = re.sub(r"^(?:应|应该|需要|必须)\s*", "", x).strip()
            x = re.sub(r"^(?:页面|系统)\s*", "", x).strip()
            x = re.sub(r"^(?:toast|提示|弹窗|提示信息)\s*", "", x, flags=re.I).strip()
            x = re.sub(r"^(?:toast|提示|弹窗|提示信息)[:：]\s*", "", x, flags=re.I).strip()
            if not x:
                continue
            if x.lower() in stop or x in stop:
                continue
            if len(x) < 3:
                continue
            if len(x) > 120:
                x = x[:120]
            cleaned.append(x)

        cleaned.sort(key=lambda t: (-len(t), t))
        out = []
        seen = set()
        for x in cleaned:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
            if len(out) >= 6:
                break
        return out

    async def _assert_expected_results_async(self):
        steps = self._testcase_steps or []
        if not steps:
            return True, "", None

        messages = list(self._runtime_messages or [])
        try:
            messages.extend(await self._collect_new_runtime_messages_async())
        except Exception:
            pass
        page = await self._get_active_page_async()
        body_text = ""
        if page:
            try:
                body_text = await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
            except Exception:
                body_text = ""
        network_texts = await self._collect_network_texts_async()
        norm_messages = [self._norm_text(m) for m in (messages or []) if m]
        norm_body = self._norm_text(body_text or "")
        norm_network = [self._norm_text(n) for n in (network_texts or []) if n]
        auth_body = self._last_auth_body_norm or ""

        pw_aliases = self._expand_phrase_aliases("密码错误")
        found_password_error = False
        for a in pw_aliases:
            na = self._norm_text(a)
            if not na:
                continue
            if any(na in nm for nm in norm_messages) or (norm_body and na in norm_body) or any(na in nn for nn in norm_network) or (auth_body and na in auth_body):
                found_password_error = True
                break
        login_failed, login_brief = await self._check_login_failed_async()

        missing = []
        matched = []
        for step in steps:
            expected_raw = getattr(step, "expected_result", "") or ""
            expected_norm = self._format_expected_result(expected_raw)
            phrases = self._extract_expect_phrases(expected_raw)
            if not phrases:
                continue
            step_missing = []
            step_matched = []
            if self._expect_password_error(expected_raw) and not found_password_error:
                step_missing.append("密码错误提示")
            if self._expect_login_fail(expected_raw) and not login_failed:
                step_missing.append("无法登录成功" + (f"（{login_brief}）" if login_brief else ""))
            for ph in phrases:
                if self._expect_login_fail(ph):
                    continue
                aliases = self._expand_phrase_aliases(ph)
                found = False
                for a in aliases:
                    na = self._norm_text(a)
                    if not na:
                        continue
                    if any(na in nm for nm in norm_messages):
                        found = True
                        break
                    if norm_body and na in norm_body:
                        found = True
                        break
                    if any(na in nn for nn in norm_network):
                        found = True
                        break
                if found:
                    step_matched.append(ph)
                else:
                    step_missing.append(ph)
            if step_missing:
                missing.append(
                    {
                        "step_number": getattr(step, "step_number", ""),
                        "expected": expected_norm,
                        "missing": step_missing,
                    }
                )
            if step_matched:
                matched.append(
                    {
                        "step_number": getattr(step, "step_number", ""),
                        "matched": step_matched[:5],
                    }
                )

        ok = len(missing) == 0
        summary = ""
        if ok:
            if messages:
                summary = "断言：通过（已捕获提示/消息：" + "；".join(messages[-5:]) + "）"
            else:
                summary = "断言：通过"
        else:
            top = missing[:3]
            detail = []
            for m in top:
                detail.append(f"步骤{m['step_number']} 缺失: " + "、".join(m["missing"]))
            summary = "断言：失败\n" + "\n".join(detail)

        created_bug_id = None
        if not ok:
            try:
                execution = await sync_to_async(AutoTestExecution.objects.select_related("case").get)(id=self.execution_id)
                case_title = execution.case.title
                recent_msgs = "；".join((self._runtime_messages or [])[-8:])
                suggested = await self._suggest_bug_content_async(
                    case_title=case_title,
                    assertion_summary=summary,
                    recent_messages=recent_msgs,
                    network_snippets=network_texts[:10],
                )
                created_bug_id = await self._create_bug_for_assertion_async(
                    assertion_summary=summary,
                    suggested_title=(suggested or {}).get("title"),
                    suggested_description=(suggested or {}).get("description"),
                )
            except Exception:
                created_bug_id = None

        try:
            execution = AutoTestExecution.objects.get(id=self.execution_id)
            last_step = AutoTestStepRecord.objects.filter(execution=execution).order_by("-step_number").first()
            step_number = (last_step.step_number if last_step else 0) + 1
            await self._upsert_step_record_async(
                step_number=step_number,
                description="断言检查",
                ai_thought="",
                action_script="assert_expected_results",
                status="success" if ok else "failed",
                error_message="" if ok else summary,
            )
            if created_bug_id:
                await self._upsert_step_record_async(
                    step_number=step_number + 1,
                    description="缺陷登记",
                    ai_thought="",
                    action_script="create_bug",
                    status="success",
                    error_message=f"已登记缺陷：BUG-{created_bug_id}",
                )
        except Exception:
            pass

        return ok, summary, created_bug_id

    @sync_to_async
    def _collect_network_texts_async(self) -> list[str]:
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        entries = AutoTestNetworkEntry.objects.filter(step_record__execution=execution).order_by("-timestamp")[:60]
        texts = []

        def extract_strings(obj):
            out = []
            if obj is None:
                return out
            if isinstance(obj, str):
                s = obj.strip()
                if s:
                    out.append(s)
                return out
            if isinstance(obj, (int, float, bool)):
                return out
            if isinstance(obj, list):
                for x in obj:
                    out.extend(extract_strings(x))
                return out
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, str) and k.lower() in ("password", "passwd", "pwd", "token", "authorization"):
                        continue
                    out.extend(extract_strings(v))
                return out
            try:
                out.append(str(obj))
            except Exception:
                pass
            return out

        for e in entries:
            payload = None
            try:
                payload = json.loads(e.response_data or "{}")
            except Exception:
                payload = None
            body_text = ""
            body_strings = []
            if isinstance(payload, dict):
                if payload.get("body_json") is not None:
                    bj = payload.get("body_json")
                    body_strings = extract_strings(bj)
                    try:
                        body_text = json.dumps(bj, ensure_ascii=False)
                    except Exception:
                        body_text = ""
                else:
                    body_text = str(payload.get("body_text") or "")
            else:
                body_text = str(e.response_data or "")
            if body_strings:
                texts.extend(body_strings[:30])
            elif body_text:
                texts.append(body_text[:2000])
            texts.append(f"{e.method} {e.status_code} {e.url}")
        return texts

    async def _suggest_bug_content_async(self, case_title: str, assertion_summary: str, recent_messages: str, network_snippets: list[str]):
        if not self._llm:
            return None
        prompt = (
            "你是测试缺陷分析助手。请根据下面信息，生成一个简洁、准确的缺陷标题和现象描述。\n"
            "要求：\n"
            "1) 只输出 JSON，不要输出 Markdown/代码块/解释；\n"
            "2) JSON 格式：{\"title\": \"...\", \"description\": \"...\"}\n"
            "3) title 不超过 50 字，包含关键现象（例如登录失败提示缺失/接口返回异常等）；\n"
            "4) description 2-4 句，描述预期与实际差异，避免泄露账号/密码等敏感信息。\n\n"
            f"用例标题：{case_title}\n"
            f"断言失败摘要：{assertion_summary}\n"
            f"捕获到的提示/Toast（可能为空）：{recent_messages}\n"
            "最近接口响应片段（可能为空，已脱敏）：\n"
            + "\n".join([str(x)[:400] for x in (network_snippets or [])])
        )
        try:
            resp = None
            if hasattr(self._llm, "ainvoke"):
                try:
                    resp = await self._llm.ainvoke([HumanMessage(content=prompt)])
                except Exception:
                    resp = await self._llm.ainvoke(prompt)
            else:
                return None
            text = getattr(resp, "content", None)
            if text is None:
                text = str(resp)
            raw = str(text).strip()
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return None
            obj = json.loads(m.group(0))
            if not isinstance(obj, dict):
                return None
            title = str(obj.get("title") or "").strip()
            desc = str(obj.get("description") or "").strip()
            if title:
                title = re.sub(r"\s+", " ", title).strip()[:80]
            if desc:
                desc = desc.strip()[:2000]
            if not title and not desc:
                return None
            return {"title": title, "description": desc}
        except Exception:
            return None

    @sync_to_async
    def _create_bug_for_assertion_async(self, assertion_summary: str, suggested_title: str = None, suggested_description: str = None):
        execution = AutoTestExecution.objects.select_related("case", "case__project").get(id=self.execution_id)
        case = execution.case
        project = case.project
        signature = f"AUTO_TEST_EXECUTION={execution.id}"
        existing = Bug.objects.filter(project=project, case=case, description__contains=signature).first()
        if existing:
            if not existing.assignee_id and execution.executor_id:
                try:
                    existing.assignee_id = execution.executor_id
                    existing.save(update_fields=["assignee"])
                except Exception:
                    pass
            return existing.id

        steps = list(case.steps.all().order_by("step_number"))
        reproduce_lines = []
        for s in steps:
            reproduce_lines.append(f"{s.step_number}. {s.description}")
            exp = self._format_expected_result(getattr(s, 'expected_result', '') or '')
            if exp:
                reproduce_lines.append(f"   预期: {exp}")
        reproduce_steps = "\n".join(reproduce_lines)[:8000]

        recent_msgs = "；".join((self._runtime_messages or [])[-8:])
        base_description = (
            f"{signature}\n"
            f"执行记录：AutoTestExecution#{execution.id}\n"
            f"断言结果：\n{assertion_summary}\n\n"
            f"近期提示/Toast：{recent_msgs}"
        ).strip()[:7800]

        title = (suggested_title or "").strip()
        if not title:
            title = f"[AI执行][断言失败] {case.title}".strip()
        if len(title) > 120:
            title = title[:120]

        description = (suggested_description or "").strip()
        if description:
            description = (description + "\n\n" + base_description).strip()[:8000]
        else:
            description = base_description[:8000]

        bug = Bug.objects.create(
            project=project,
            case=case,
            title=title,
            description=description,
            reproduce_steps=reproduce_steps,
            creator=execution.executor,
            assignee=execution.executor,
            severity=3,
            priority=2,
            status=1,
        )
        return bug.id

    @sync_to_async
    def _create_network_entry_async(self, step_number: int, url: str, method: str, status_code: int, request_data: str, response_data: str):
        execution = AutoTestExecution.objects.get(id=self.execution_id)
        step_record = AutoTestStepRecord.objects.filter(execution=execution, step_number=step_number).first()
        if not step_record:
            step_record, _ = AutoTestStepRecord.objects.update_or_create(
                execution=execution,
                step_number=step_number,
                defaults={
                    "description": f"Step {step_number}",
                    "status": "pending",
                },
            )
        AutoTestNetworkEntry.objects.create(
            step_record=step_record,
            url=url,
            method=method,
            status_code=int(status_code) if status_code is not None else 0,
            request_data=request_data,
            response_data=response_data,
        )
        try:
            short_url = str(url or "")[:260]
            m = str(method or "").upper()[:12]
            sc = int(status_code) if status_code is not None else 0
            self._evidence.add("network", f"{m} {sc} {short_url}", {"step_number": int(step_number or 0)})
        except Exception:
            pass
