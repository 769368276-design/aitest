import asyncio
import json
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import requests
from asgiref.sync import sync_to_async
from django.core.files.base import ContentFile
from django.utils import timezone

from autotest.models import AutoTestExecution, AutoTestNetworkEntry, AutoTestStepRecord
from users.ai_config import AIKeyNotConfigured, resolve_exec_params


class PlaywrightAIRunner:
    def __init__(self, execution_id: int):
        self.execution_id = int(execution_id)
        self._llm_params = None
        self._steps = []
        self._base_url = ""
        self._default_username = ""
        self._default_password = ""
        self._current_step_record_id = 0
        self._current_step_number = 0
        self._pending_requests = {}
        self._net_lock = None

    def run(self):
        try:
            execution = AutoTestExecution.objects.select_related("case", "case__project", "executor").get(id=self.execution_id)
        except Exception:
            return
        if execution.status in ("completed", "failed", "stopped"):
            return
        try:
            execution.status = "running"
            execution.save(update_fields=["status"])
        except Exception:
            pass

        try:
            user = execution.executor
            if not getattr(user, "is_authenticated", False):
                raise AIKeyNotConfigured("请先登录后再使用 AI 功能", scope="exec")
            self._llm_params = resolve_exec_params(user)
            project = getattr(execution.case, "project", None)
            base_url = str(getattr(project, "base_url", "") or "").strip()
            if base_url and not re.match(r"^https?://", base_url, flags=re.I):
                base_url = "http://" + base_url
            self._base_url = base_url
            creds = self._extract_default_credentials(
                test_accounts=str(getattr(project, "test_accounts", "") or "") if project is not None else "",
                knowledge_base=str(getattr(project, "knowledge_base", "") or "") if project is not None else "",
                dataset_vars=getattr(execution, "dataset_vars", None) or {},
            )
            self._default_username = str(creds.get("username") or "").strip()
            self._default_password = str(creds.get("password") or "").strip()
            try:
                qs = execution.case.steps.all().order_by("step_number").values("id", "step_number", "description", "expected_result")
                self._steps = list(qs)
            except Exception:
                self._steps = []
            asyncio.run(self._run_async())
        except Exception as e:
            try:
                execution = AutoTestExecution.objects.get(id=self.execution_id)
                execution.status = "failed"
                execution.result_summary = {"error": str(e)[:800]}
                execution.end_time = timezone.now()
                execution.save(update_fields=["status", "result_summary", "end_time"])
                try:
                    if execution.case_id:
                        execution.case.status = 5
                        execution.case.save(update_fields=["status"])
                except Exception:
                    pass
            except Exception:
                pass

    async def _run_async(self):
        from playwright.async_api import async_playwright
        from django.conf import settings

        llm_params = self._llm_params
        provider = str(getattr(llm_params, "provider", "") or "").strip().lower() if llm_params else ""
        if provider in ("anthropic", "google"):
            raise AIKeyNotConfigured("Playwright 执行引擎暂不支持该模型提供方，请使用 OpenAI 兼容提供方", scope="exec")

        steps = list(self._steps or [])
        if not steps:
            raise RuntimeError("No steps found in test case")

        passed = 0
        failed = 0

        async with async_playwright() as p:
            headless = bool(getattr(settings, "AI_EXEC_HEADLESS", True))
            browser = await p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"])
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            page = await context.new_page()
            self._net_lock = asyncio.Lock()
            self._attach_network_capture(context)

            try:
                if self._base_url:
                    await page.goto(self._base_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            try:
                await self._db_update_execution(status="running")
            except Exception:
                pass

            for step in steps:
                flags = await self._db_get_flags()
                if bool(flags.get("stop_signal")):
                    await self._db_mark_stopped()
                    return
                if bool(flags.get("pause_signal")):
                    await self._db_wait_if_paused()

                step_id = int(step.get("id") or 0)
                step_number = int(step.get("step_number") or 0)
                description = str(step.get("description") or "")
                expected_result = str(step.get("expected_result") or "")

                step_record_id = await self._db_create_step_record(step_id, step_number, description)
                self._current_step_record_id = int(step_record_id or 0)
                self._current_step_number = int(step_number or 0)

                started = time.time()
                ok = False
                err = ""
                action = {}
                ai_thought = ""
                auto_login_applied = False
                try:
                    ok, err, action, ai_thought, auto_login_applied = await self._run_one_step(
                        page=page,
                        llm_params=llm_params,
                        step_number=step_number,
                        description=description,
                        expected_result=expected_result,
                        error_hint="",
                    )
                except Exception as e:
                    ok = False
                    err = str(e)[:800]

                if not ok:
                    try:
                        ok2, err2, action2, ai_thought2, auto_login_applied2 = await self._run_one_step(
                            page=page,
                            llm_params=llm_params,
                            step_number=step_number,
                            description=description,
                            expected_result=expected_result,
                            error_hint=err,
                        )
                        ok = bool(ok2)
                        action = action2
                        ai_thought = ai_thought2
                        auto_login_applied = bool(auto_login_applied or auto_login_applied2)
                        err = err2 or err
                    except Exception as e2:
                        ok = False
                        err = str(e2)[:800]

                screenshot_bytes = b""
                try:
                    screenshot_bytes = await page.screenshot(type="png", full_page=False)
                except Exception:
                    screenshot_bytes = b""

                await self._db_finish_step_record(
                    step_record_id=step_record_id,
                    ok=ok,
                    err=err,
                    ai_thought=ai_thought,
                    action=self._sanitize_action_for_storage(action),
                    elapsed_ms=int((time.time() - started) * 1000),
                    url=str(getattr(page, "url", "") or ""),
                    screenshot_bytes=screenshot_bytes,
                    auto_login_applied=bool(auto_login_applied),
                )

                if ok:
                    passed += 1
                else:
                    failed += 1
                    await self._db_mark_failed(passed=passed, failed=failed, error=err)
                    return

            await self._db_mark_completed(passed=passed, failed=failed)
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    async def _db_get_flags(self) -> dict:
        def _q():
            return (
                AutoTestExecution.objects.filter(id=self.execution_id)
                .values("stop_signal", "pause_signal")
                .first()
                or {"stop_signal": False, "pause_signal": False}
            )
        return await sync_to_async(_q)()

    async def _db_update_execution(self, **fields):
        def _u():
            AutoTestExecution.objects.filter(id=self.execution_id).update(**fields)
        return await sync_to_async(_u)()

    async def _db_create_network_entry(
        self,
        step_record_id: int,
        url: str,
        method: str,
        status_code: int,
        request_data: str,
        response_data: str,
    ):
        def _c():
            sr = AutoTestStepRecord.objects.filter(id=int(step_record_id or 0)).first()
            if sr is None:
                return
            AutoTestNetworkEntry.objects.create(
                step_record=sr,
                url=str(url or "")[:2000],
                method=str(method or "")[:10],
                status_code=int(status_code or 0),
                request_data=str(request_data or "")[:20000],
                response_data=str(response_data or "")[:20000],
            )
        await sync_to_async(_c)()

    async def _db_mark_stopped(self):
        def _u():
            AutoTestExecution.objects.filter(id=self.execution_id).update(
                status="stopped",
                end_time=timezone.now(),
                result_summary={"reason": "manual_stop"},
            )
        await sync_to_async(_u)()

    async def _db_wait_if_paused(self):
        await self._db_update_execution(status="paused")
        while True:
            await asyncio.sleep(0.5)
            flags = await self._db_get_flags()
            if bool(flags.get("stop_signal")):
                await self._db_mark_stopped()
                raise RuntimeError("stopped")
            if not bool(flags.get("pause_signal")):
                await self._db_update_execution(status="running")
                return

    async def _db_create_step_record(self, step_id: int, step_number: int, description: str) -> int:
        def _c():
            now_ms = int(time.time() * 1000)
            r = AutoTestStepRecord.objects.create(
                execution_id=self.execution_id,
                step_id=step_id or None,
                step_number=int(step_number),
                description=str(description or ""),
                status="pending",
                ai_thought="AI 规划中...",
                metrics={"started_at_ms": now_ms},
            )
            return int(r.id)
        return await sync_to_async(_c)()

    async def _db_update_step_record_partial(self, step_record_id: int, **fields):
        sid = int(step_record_id or 0)
        if sid <= 0:
            return
        safe = {}
        for k, v in (fields or {}).items():
            if k in ("ai_thought", "action_script", "status", "error_message", "metrics"):
                safe[k] = v
        if not safe:
            return

        def _u():
            AutoTestStepRecord.objects.filter(id=sid).update(**safe)
        await sync_to_async(_u)()

    async def _db_finish_step_record(
        self,
        step_record_id: int,
        ok: bool,
        err: str,
        ai_thought: str,
        action: dict,
        elapsed_ms: int,
        url: str,
        screenshot_bytes: bytes,
        auto_login_applied: bool,
    ):
        def _u():
            r = AutoTestStepRecord.objects.get(id=int(step_record_id))
            r.ai_thought = str(ai_thought or "")[:2000]
            r.action_script = json.dumps(action or {}, ensure_ascii=False)
            r.metrics = {
                "elapsed_ms": int(elapsed_ms),
                "url": str(url or ""),
                "auto_login_applied": bool(auto_login_applied),
                "default_username_present": bool(self._default_username),
                "default_password_present": bool(self._default_password),
            }
            r.status = "success" if ok else "failed"
            r.error_message = "" if ok else str(err or "")[:800]
            if screenshot_bytes:
                r.screenshot_after = ContentFile(
                    screenshot_bytes,
                    name=f"step_{int(r.step_number)}_{uuid.uuid4().hex}.png",
                )
            r.save()
        await sync_to_async(_u)()

    async def _db_mark_failed(self, passed: int, failed: int, error: str):
        def _u():
            AutoTestExecution.objects.filter(id=self.execution_id).update(
                status="failed",
                end_time=timezone.now(),
                result_summary={"passed": int(passed), "failed": int(failed), "error": str(error or "")[:800]},
            )
        await sync_to_async(_u)()

    async def _db_mark_completed(self, passed: int, failed: int):
        def _u():
            AutoTestExecution.objects.filter(id=self.execution_id).update(
                status="completed",
                end_time=timezone.now(),
                result_summary={"passed": int(passed), "failed": int(failed)},
            )
        await sync_to_async(_u)()

    def _extract_default_credentials(self, test_accounts: str, knowledge_base: str, dataset_vars: dict) -> dict:
        dv = dataset_vars if isinstance(dataset_vars, dict) else {}
        dv_u = ""
        dv_p = ""
        if dv:
            dv_u = str(
                dv.get("username")
                or dv.get("user")
                or dv.get("account")
                or dv.get("用户名")
                or dv.get("账号")
                or dv.get("账户")
                or ""
            ).strip()
            dv_p = str(dv.get("password") or dv.get("pwd") or dv.get("密码") or "").strip()

        a = self._parse_project_test_accounts(test_accounts or "")
        b = self._parse_project_test_accounts(knowledge_base or "")
        base = a if (a.get("username") or a.get("password")) else b
        base_source = "test_accounts" if base is a and (a.get("username") or a.get("password")) else ("knowledge_base" if (b.get("username") or b.get("password")) else "")

        u = dv_u or str(base.get("username") or "").strip()
        p = dv_p or str(base.get("password") or "").strip()

        source = ""
        if dv_u or dv_p:
            source = "dataset_vars"
            if base_source and (not dv_u or not dv_p):
                source = source + "+" + base_source
        else:
            source = base_source
        return {"username": u, "password": p, "source": source}

    def _parse_project_test_accounts(self, text: str) -> dict:
        s = (text or "").strip()
        if not s:
            return {"username": "", "password": ""}
        lead = s[:1]
        if lead in ("{", "["):
            try:
                obj = json.loads(s)
            except Exception:
                obj = None
            if isinstance(obj, list):
                for it in obj[:50]:
                    if not isinstance(it, dict):
                        continue
                    u = str(
                        it.get("username")
                        or it.get("user")
                        or it.get("account")
                        or it.get("用户名")
                        or it.get("账号")
                        or it.get("账户")
                        or ""
                    ).strip()
                    p = str(it.get("password") or it.get("pwd") or it.get("密码") or "").strip()
                    if u or p:
                        return {"username": u, "password": p}
            if isinstance(obj, dict):
                u = str(
                    obj.get("username")
                    or obj.get("user")
                    or obj.get("account")
                    or obj.get("用户名")
                    or obj.get("账号")
                    or obj.get("账户")
                    or ""
                ).strip()
                p = str(obj.get("password") or obj.get("pwd") or obj.get("密码") or "").strip()
                if u or p:
                    return {"username": u, "password": p}
        m = re.search(
            r"(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*[`'\"“”]?\s*([A-Za-z0-9_.@-]{2,80})[`'\"“”]?.*?(?:密码|password|pwd)\s*[:：]?\s*[`'\"“”]?\s*([^\s`'\"“”]{2,120})",
            s,
            flags=re.I | re.S,
        )
        if m:
            return {"username": (m.group(1) or "").strip(), "password": (m.group(2) or "").strip()}
        m = re.search(
            r"(?:密码|password|pwd)\s*[:：]?\s*[`'\"“”]?\s*([^\s`'\"“”]{2,120})[`'\"“”]?.*?(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*[`'\"“”]?\s*([A-Za-z0-9_.@-]{2,80})",
            s,
            flags=re.I | re.S,
        )
        if m:
            return {"username": (m.group(2) or "").strip(), "password": (m.group(1) or "").strip()}

        m = re.search(
            r"\"(?:用户名|账号|账户|user(?:name)?)\"\s*:\s*\"([A-Za-z0-9_.@-]{2,80})\".*?\"(?:密码|password|pwd)\"\s*:\s*\"([^\s\"\\]{2,120})\"",
            s,
            flags=re.I | re.S,
        )
        if m:
            return {"username": (m.group(1) or "").strip(), "password": (m.group(2) or "").strip()}

        u = ""
        p = ""
        for line in s.splitlines():
            line = (line or "").strip()
            if not line:
                continue
            if not u:
                m = re.search(r"(?:用户名|账号|账户|user(?:name)?)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,80})", line, flags=re.I)
                if m:
                    u = (m.group(1) or "").strip()
            if not p:
                m = re.search(r"(?:密码|password|pwd)\s*[:：]?\s*([^\s]{2,120})", line, flags=re.I)
                if m:
                    p = (m.group(1) or "").strip()
            if u and p:
                break
            if not u:
                m = re.search(r"(?:username|user|account)\s*[:：]?\s*([A-Za-z0-9_.@-]{2,80})", line, flags=re.I)
                if m:
                    u = (m.group(1) or "").strip()
            if not p:
                m = re.search(r"(?:password|pwd)\s*[:：]?\s*([^\s]{2,120})", line, flags=re.I)
                if m:
                    p = (m.group(1) or "").strip()
            if u and p:
                break
        return {"username": u, "password": p}

    async def _maybe_autofill_login(self, page, step_desc: str) -> bool:
        u = (self._default_username or "").strip()
        p = (self._default_password or "").strip()
        if not (u or p):
            return False
        try:
            url_l = (str(getattr(page, "url", "") or "")).lower()
        except Exception:
            url_l = ""
        try:
            likely = await page.evaluate(
                """() => {
  const body = document.body;
  const t = (body && (body.innerText || body.textContent) ? (body.innerText || body.textContent) : '').toLowerCase();
  const hasPw = !!document.querySelector('input[type=\"password\"],input[placeholder*=\"密码\"],input[aria-label*=\"密码\"]');
  if (!hasPw) return false;
  if (t.includes('登录') || t.includes('login') || t.includes('sign in') || t.includes('signin')) return true;
  const btns = Array.from(document.querySelectorAll('button,[role=\"button\"],a'));
  for (const el of btns.slice(0,80)) {
    const s = ((el.innerText || el.textContent || '')).trim().toLowerCase();
    if (!s) continue;
    if (s.includes('登录') || s.includes('login') || s.includes('sign in') || s.includes('signin')) return true;
  }
  return false;
}"""
            )
        except Exception:
            likely = False
        if not bool(likely) and not any(k in url_l for k in ["login", "signin", "auth"]):
            return False
        password_loc = page.locator("input[type='password'],input[placeholder*='密码'],input[aria-label*='密码']").first
        try:
            await password_loc.wait_for(state="visible", timeout=1500)
        except Exception:
            return False

        try:
            pv = await password_loc.input_value()
        except Exception:
            pv = ""
        if pv and p:
            return False

        username_selectors = [
            "input[name*='user' i]",
            "input[id*='user' i]",
            "input[placeholder*='用户']",
            "input[placeholder*='账号']",
            "input[placeholder*='手机号']",
            "input[type='text']",
            "input:not([type])",
        ]
        user_loc = None
        for sel in username_selectors:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=600)
                user_loc = loc
                break
            except Exception:
                continue
        if user_loc is not None:
            try:
                uv = await user_loc.input_value()
            except Exception:
                uv = ""
            if not uv and u:
                try:
                    await user_loc.fill(u)
                except Exception:
                    try:
                        await user_loc.click()
                        await page.keyboard.type(u)
                    except Exception:
                        pass
        if p:
            try:
                await password_loc.fill(p)
            except Exception:
                try:
                    await password_loc.click()
                    await page.keyboard.type(p)
                except Exception:
                    pass
        return True

    async def _collect_context(self, page) -> dict:
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            url = page.url
        except Exception:
            url = ""
        try:
            elements = await page.evaluate(
                """() => {
  const els = [];
  const push = (el, kind) => {
    try {
      const r = el.getBoundingClientRect();
      if (!r || r.width < 4 || r.height < 4) return;
      const style = getComputedStyle(el);
      if (style && (style.visibility === 'hidden' || style.display === 'none')) return;
      const txt = (el.innerText || el.textContent || '').trim().replace(/\\s+/g,' ').slice(0,140);
      els.push({
        kind,
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        name: el.getAttribute('name') || '',
        type: el.getAttribute('type') || '',
        placeholder: el.getAttribute('placeholder') || '',
        aria: el.getAttribute('aria-label') || '',
        role: el.getAttribute('role') || '',
        text: txt,
      });
    } catch (e) {}
  };
  document.querySelectorAll('input,textarea,select,[contenteditable=\"true\"]').forEach(el => push(el,'input'));
  document.querySelectorAll('button,a,[role=\"button\"],[role=\"link\"],[role=\"menuitem\"],[onclick],.ant-menu-item,.el-menu-item,.menu-item').forEach(el => push(el,'action'));
  return els.slice(0,120);
}"""
            )
        except Exception:
            elements = []
        try:
            import base64
            png = await page.screenshot(type="png", full_page=False)
            screenshot_b64 = base64.b64encode(png).decode("utf-8")
        except Exception:
            screenshot_b64 = ""
        return {
            "url": url,
            "title": title,
            "elements": elements,
            "screenshot_b64": screenshot_b64,
            "default_login": {
                "username": self._default_username,
                "has_password": bool(self._default_password),
                "password_handling": "password_autofill",
            },
        }

    def _should_assert_expected(self, expected_result: str) -> bool:
        exp = str(expected_result or "").strip()
        if not exp:
            return False
        if exp == "AI自动验证":
            return False
        lower = exp.lower()
        neg = ["错误", "失败", "不正确", "无权限", "格式错误", "无效", "不合法", "invalid", "error", "failed", "unauthorized", "forbidden", "denied"]
        if any(k in exp for k in neg) or any(k in lower for k in neg):
            return True
        if "提示" in exp or "toast" in lower or "message" in lower or "alert" in lower:
            return True
        return False

    async def _post_action_assert(self, page, action: dict, expected_result: str = "") -> str:
        try:
            invalid = await page.evaluate(
                """() => {
  const el = document.querySelector(':invalid');
  if (!el) return null;
  const msg = el.validationMessage || '';
  return { msg, name: el.getAttribute('name')||'', id: el.id||'', type: el.getAttribute('type')||'', value: (el.value||'') };
}"""
            )
        except Exception:
            invalid = None
        if isinstance(invalid, dict):
            msg = str(invalid.get("msg") or "").strip()
            if msg:
                return f"表单校验未通过：{msg}"

        exp = str(expected_result or "").strip()
        if self._should_assert_expected(exp):
            aerr = await self._assert_expected_text(page, exp)
            if aerr:
                return aerr

        try:
            pv = await page.locator("input[type='password']").first.input_value()
        except Exception:
            pv = ""
        if not pv and bool(self._default_password):
            try:
                await self._maybe_autofill_login(page, "")
                pv2 = await page.locator("input[type='password']").first.input_value()
                if pv2:
                    return ""
            except Exception:
                pass
        if not pv:
            try:
                pw_required = await page.evaluate(
                    """() => {
  const el = document.querySelector('input[type=\"password\"]');
  if (!el) return false;
  if (el.required) return true;
  const aria = (el.getAttribute('aria-required')||'').toLowerCase();
  return aria === 'true';
}"""
                )
            except Exception:
                pw_required = False
            if pw_required and ("click" in str(action.get("action") or "").lower() or "press" in str(action.get("action") or "").lower()):
                if bool(self._default_password):
                    return "登录密码仍为空：已尝试自动填充但未成功"
                return "登录密码为空：未从项目测试账号/资料库解析到密码"
        return ""

    async def _assert_expected_text(self, page, expected: str) -> str:
        exp = str(expected or "").strip()
        if not exp:
            return ""
        if len(exp) < 2:
            return ""
        if any(x in exp for x in ["AI自动验证", "无需验证", "页面打开"]):
            return ""
        tokens = [t.strip() for t in re.split(r"[,，;；。\\n]+", exp) if t.strip()]
        tokens = [t for t in tokens if len(t) >= 2][:3]
        if not tokens:
            return ""
        try:
            toast_text = await self._collect_toast_text(page)
        except Exception:
            toast_text = ""
        blob = (toast_text or "")
        if not blob:
            try:
                blob = await page.evaluate("() => (document.body && (document.body.innerText||'')) ? document.body.innerText.slice(0,4000) : ''")
            except Exception:
                blob = ""
        for t in tokens:
            if t and t in blob:
                return ""
        return f"未观察到预期结果：{tokens[0]}"

    async def _collect_toast_text(self, page) -> str:
        try:
            return await page.evaluate(
                """() => {
  const roots = [document];
  const texts = [];
  const sels = [
    '[role=\"alert\"]',
    '[aria-live]',
    '.toast',
    '.snackbar',
    '.ant-message',
    '.ant-message-notice-content',
    '.ant-notification',
    '.ant-notification-notice-message',
    '.ant-notification-notice-description',
    '.ant-alert',
    '.ant-alert-message',
    '.el-message',
    '.el-message__content',
    '.el-notification',
    '.el-form-item__error',
    '.el-alert',
    '.el-alert__description',
    '.notification',
    '.message',
  ];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(el => {
      try {
        const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g,' ');
        if (t) texts.push(t.slice(0,200));
      } catch (e) {}
    });
  }
  return texts.join('\\n');
}"""
            )
        except Exception:
            return ""

    async def _maybe_autofill_form_fields(self, page, fields: dict) -> list[dict]:
        f = fields if isinstance(fields, dict) else {}
        actions = []
        mapping = [
            ("username", ["input[name*='user' i]", "input[id*='user' i]", "input[placeholder*='用户']", "input[placeholder*='账号']", "input[aria-label*='用户']", "input[aria-label*='账号']"]),
            ("password", ["input[type='password']", "input[name*='pass' i]", "input[id*='pass' i]", "input[placeholder*='密码']", "input[aria-label*='密码']"]),
            ("phone", ["input[name*='phone' i]", "input[name*='mobile' i]", "input[id*='phone' i]", "input[id*='mobile' i]", "input[placeholder*='手机']", "input[placeholder*='电话']", "input[placeholder*='手机号']", "input[aria-label*='手机']", "input[aria-label*='电话']"]),
            ("email", ["input[type='email']", "input[name*='mail' i]", "input[id*='mail' i]", "input[placeholder*='邮箱']", "input[aria-label*='邮箱']"]),
            ("name", ["input[name*='name' i]", "input[id*='name' i]", "input[placeholder*='姓名']", "input[aria-label*='姓名']"]),
        ]
        for k, selectors in mapping:
            v = str(f.get(k) or "").strip()
            if not v:
                continue
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    await loc.wait_for(state="visible", timeout=600)
                except Exception:
                    continue
                try:
                    cur = await loc.input_value()
                except Exception:
                    cur = ""
                if cur:
                    break
                try:
                    await loc.fill(v, timeout=2000)
                except Exception:
                    try:
                        await loc.click(timeout=2000)
                        await page.keyboard.type(v)
                    except Exception:
                        pass
                actions.append({"action": "type", "by": "css", "selector": sel, "text": v})
                break
        return actions

    def _sanitize_action_for_storage(self, action: dict) -> dict:
        def looks_like_password(a: dict) -> bool:
            try:
                sel = str(a.get("selector") or "")
            except Exception:
                sel = ""
            try:
                name = str(a.get("name") or "")
            except Exception:
                name = ""
            low = sel.lower()
            return ("password" in low) or ("pwd" in low) or ("密码" in sel) or ("密码" in name)

        def scrub(obj):
            if isinstance(obj, list):
                return [scrub(x) for x in obj]
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    kl = str(k).lower()
                    if kl in ("password", "pwd"):
                        out[k] = "***" if v else v
                        continue
                    if k == "text" and isinstance(v, str) and looks_like_password(obj):
                        out[k] = "***" if v else v
                        continue
                    out[k] = scrub(v)
                return out
            return obj

        return scrub(action if isinstance(action, dict) else {})

    async def _run_one_step(
        self,
        page,
        llm_params,
        step_number: int,
        description: str,
        expected_result: str,
        error_hint: str = "",
    ):
        ctx = await self._collect_context(page)
        auto_login_applied = await self._maybe_autofill_login(page, description)
        auto_login_clicked = False
        try:
            if await self._is_likely_login_step(page, description, expected_result):
                auto_login_clicked = await self._maybe_click_login(page)
        except Exception:
            auto_login_clicked = False
        ctx["default_login"] = {
            "username": self._default_username,
            "has_password": bool(self._default_password),
            "password_handling": "password_autofill",
            "auto_login_applied": bool(auto_login_applied),
            "auto_login_clicked": bool(auto_login_clicked),
        }
        ctx["extracted_fields"] = self._extract_fields_from_step(description)

        executed = []
        if auto_login_clicked:
            executed.append({"action": "click", "by": "css", "selector": "button[type='submit']"})
            aerr = await self._post_action_assert(page, {"action": "click", "by": "css", "selector": "button[type='submit']"}, expected_result="")
            if aerr:
                out = {"thought": "auto_login_clicked", "actions": executed}
                return False, aerr, out, "auto_login_clicked", True
            try:
                await page.wait_for_timeout(600)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

        try:
            executed.extend(await self._maybe_autofill_form_fields(page, ctx.get("extracted_fields") or {}))
        except Exception:
            pass

        try:
            await self._db_update_step_record_partial(
                self._current_step_record_id,
                ai_thought="AI 规划动作中...",
            )
        except Exception:
            pass

        plan = await self._plan_action(llm_params, step_number, description, expected_result, ctx, error_hint=error_hint)
        thought = str(plan.get("thought") or "")[:2000]
        actions = plan.get("actions")
        if not isinstance(actions, list) or not actions:
            actions = [plan]
        ok = True
        last_err = ""
        for a in actions[:8]:
            if not isinstance(a, dict):
                continue
            executed.append(a)
            try:
                ok = await self._execute_action(page, a)
            except Exception as e:
                ok = False
                last_err = str(e)[:240] or "action_exception"
                break
            if not ok:
                last_err = "action_failed"
                break
            try:
                aerr = await self._post_action_assert(page, a, expected_result=expected_result)
            except Exception as e:
                aerr = str(e)[:240] or "assert_exception"
            if aerr:
                ok = False
                last_err = aerr
                break
        out = {"thought": thought, "actions": executed}
        try:
            await self._db_update_step_record_partial(
                self._current_step_record_id,
                ai_thought=(thought or "AI 已生成动作"),
                action_script=json.dumps(self._sanitize_action_for_storage(out), ensure_ascii=False),
            )
        except Exception:
            pass
        return ok, last_err, out, thought, auto_login_applied


    async def _is_likely_login_step(self, page, description: str, expected_result: str) -> bool:
        desc = str(description or "")
        exp = str(expected_result or "")
        try:
            url_l = (str(getattr(page, "url", "") or "")).lower()
        except Exception:
            url_l = ""
        if any(k in url_l for k in ["login", "signin", "auth"]):
            return True
        if any(k in desc for k in ["登录", "登陆"]) or ("login" in desc.lower()) or ("signin" in desc.lower()):
            return True
        if any(k in exp for k in ["登录", "登陆"]) or ("login" in exp.lower()):
            return True
        return False

    async def _maybe_click_login(self, page) -> bool:
        candidates = [
            ("css", "button[type='submit']"),
            ("text", "登录"),
            ("text", "Login"),
            ("text", "Sign in"),
        ]
        for by, v in candidates:
            try:
                if by == "css":
                    loc = page.locator(v).first
                else:
                    loc = page.get_by_text(v, exact=False).first
                await loc.wait_for(state="visible", timeout=1200)
                await loc.click(timeout=3000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                try:
                    pw = page.locator("input[type='password']").first
                    await pw.wait_for(state="hidden", timeout=5000)
                except Exception:
                    pass
                return True
            except Exception:
                continue
        return False

    def _chat_url_candidates(self, base_url: str) -> list[str]:
        u = (base_url or "").strip().rstrip("/")
        if not u:
            return []
        if u.endswith("/v1"):
            return [u + "/chat/completions"]
        return [u + "/chat/completions", u + "/v1/chat/completions"]

    def _call_llm_json(self, llm_params, messages: list[dict], temperature: float = 0.0) -> dict:
        provider = str(getattr(llm_params, "provider", "") or "").strip().lower()
        base_url = str(getattr(llm_params, "base_url", "") or "")
        api_key = str(getattr(llm_params, "api_key", "") or "")
        model = str(getattr(llm_params, "model", "") or "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_l = base_url.strip().lower()
        model_l = model.strip().lower()
        eff_temp = float(temperature)
        if provider == "kimi" or ("moonshot" in base_l) or model_l.startswith("kimi-"):
            eff_temp = 1.0
        body = {"model": model, "messages": messages, "temperature": float(eff_temp)}
        body["response_format"] = {"type": "json_object"}

        last_err = None
        for url in self._chat_url_candidates(base_url):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=60)
                if resp.status_code >= 400:
                    last_err = f"http_{resp.status_code}:{resp.text[:200]}"
                    continue
                data = resp.json()
                content = ""
                try:
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    content = ""
                return self._parse_json_object(content)
            except Exception as e:
                last_err = str(e)
                continue
        raise RuntimeError(f"llm_call_failed:{last_err}")

    def _parse_json_object(self, s: str) -> dict:
        raw = (s or "").strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    async def _plan_action(self, llm_params, step_number: int, description: str, expected_result: str, ctx: dict, error_hint: str) -> dict:
        system = (
            "你是资深网页自动化工程师。根据用户步骤描述与页面信息，输出一个 JSON 对象，只能包含 JSON，不要输出解释。"
            "你可以输出 actions（动作数组），每个动作字段：action, selector, by, role, name, text, key, url, wait_ms, thought。"
            "允许 action：navigate/click/type/press/scroll/wait。actions 最多 6 个，按顺序执行。"
            "定位优先：by=role（role+name）、by=text（text）、selector（CSS）。"
            "如果页面是登录页且 default_login.has_password=true，登录密码会由系统自动填充；不要在输出里包含默认登录密码，只需点击登录按钮或完成后续动作。"
            "当需要在表单填写数据时，优先使用 extracted_fields 里的值（username/password/phone/email/name），不要随意编造；如果缺少就返回 wait 并在 thought 说明缺失字段。"
        )
        user = {
            "step_number": int(step_number),
            "step": str(description or ""),
            "expected_result": str(expected_result or ""),
            "page_url": ctx.get("url") or "",
            "page_title": ctx.get("title") or "",
            "elements": ctx.get("elements") or [],
            "extracted_fields": ctx.get("extracted_fields") or {},
            "error_hint": error_hint or "",
            "default_login": ctx.get("default_login") or {},
        }
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ]
        out = await asyncio.to_thread(self._call_llm_json, llm_params, messages, 0.0)
        out = out or {}
        if not isinstance(out, dict):
            out = {}
        actions = out.get("actions")
        if isinstance(actions, list) and actions:
            fixed = []
            for a in actions[:8]:
                if not isinstance(a, dict):
                    continue
                fixed.append(self._normalize_action(a))
            out["actions"] = fixed
        else:
            out = self._normalize_action(out)
        return out

    def _normalize_action(self, out: dict) -> dict:
        obj = out if isinstance(out, dict) else {}
        act = str(obj.get("action") or "").strip().lower()
        if act not in ("navigate", "click", "type", "press", "scroll", "wait"):
            act = "wait"
        obj["action"] = act
        by = str(obj.get("by") or "").strip().lower()
        if by not in ("role", "text", "css", ""):
            try:
                if "by" in obj:
                    del obj["by"]
            except Exception:
                pass
            by = ""
        if by:
            obj["by"] = by
        return obj

    def _extract_fields_from_step(self, desc: str) -> dict:
        s = str(desc or "")
        out = {}
        m = re.search(r"(?:用户名|账号)[:：]?\s*([A-Za-z0-9_.@-]{2,80})", s)
        if m:
            out["username"] = (m.group(1) or "").strip()
        m = re.search(r"(?:电话|手机号|手机号码)[:：]?\s*([0-9]{4,20})", s)
        if m:
            out["phone"] = (m.group(1) or "").strip()
        m = re.search(r"(?:邮箱|email)[:：]?\s*([A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+)", s, flags=re.I)
        if m:
            out["email"] = (m.group(1) or "").strip()
        m = re.search(r"(?:密码|password|pwd)[:：]?\s*([^\s，。；;]{2,120})", s, flags=re.I)
        if m:
            out["password"] = (m.group(1) or "").strip()
        return out

    async def _execute_action(self, page, action: dict) -> bool:
        act = str(action.get("action") or "").strip().lower()
        if act == "navigate":
            url = str(action.get("url") or "").strip()
            if not url:
                return False
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return True
        if act == "wait":
            ms = action.get("wait_ms") or 1200
            try:
                ms = int(ms)
            except Exception:
                ms = 1200
            if ms < 0:
                ms = 0
            await page.wait_for_timeout(ms)
            return True
        if act == "scroll":
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(800)
            return True
        if act == "press":
            key = str(action.get("key") or "Enter")
            await page.keyboard.press(key)
            await page.wait_for_timeout(600)
            return True

        selector = str(action.get("selector") or "").strip()
        by = str(action.get("by") or "").strip().lower()
        role = str(action.get("role") or "").strip()
        name = str(action.get("name") or "").strip()
        text = str(action.get("text") or "")

        if act == "click":
            return await self._smart_click(page, selector=selector, by=by, role=role, name=name, text=text)
        if act == "type":
            loc = await self._smart_locator(page, selector=selector, by=by, role=role, name=name, text=text, require_visible=True)
            if loc is None:
                return False
            await loc.click(timeout=8000)
            try:
                await loc.fill(text, timeout=8000)
            except Exception:
                try:
                    await page.keyboard.type(text)
                except Exception:
                    pass
            if bool(action.get("press_enter")):
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)
            return True
        return False

    async def _smart_click(self, page, selector: str, by: str, role: str, name: str, text: str) -> bool:
        candidates = []
        if by == "role" and role and name:
            candidates.append(("role", {"by": "role", "role": role, "name": name}))
        if by == "text" and text:
            candidates.append(("text", {"by": "text", "text": text}))
        if selector:
            candidates.append(("css", {"by": "css", "selector": selector}))

        inferred = self._infer_target_text(selector) or text or name
        if inferred:
            candidates.append(("text", {"by": "text", "text": inferred}))
            candidates.append(("role", {"by": "role", "role": "button", "name": inferred}))

        menu_hint = self._infer_menu_from_target(inferred)
        if menu_hint:
            try:
                await self._ensure_menu(page, menu_hint)
            except Exception:
                pass

        last_err = None
        for _kind, payload in candidates[:10]:
            try:
                loc = await self._smart_locator(page, require_visible=True, **payload)
                if loc is None:
                    continue
                try:
                    await loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                await loc.click(timeout=8000)
                await page.wait_for_timeout(600)
                return True
            except Exception as e:
                last_err = e
                if menu_hint:
                    try:
                        await self._ensure_menu(page, menu_hint)
                    except Exception:
                        pass
                continue
        if last_err:
            raise last_err
        return False

    async def _smart_locator(
        self,
        page,
        selector: str = "",
        by: str = "",
        role: str = "",
        name: str = "",
        text: str = "",
        require_visible: bool = False,
    ):
        loc = None
        if by == "role" and role and name:
            try:
                loc = page.get_by_role(role, name=name).first
            except Exception:
                loc = None
        if loc is None and text:
            try:
                loc = page.get_by_text(text, exact=False).first
            except Exception:
                loc = None
        if loc is None and selector:
            try:
                loc = page.locator(selector).first
            except Exception:
                loc = None
        if loc is None:
            return None
        if require_visible:
            await loc.wait_for(state="visible", timeout=8000)
        return loc

    def _attach_network_capture(self, context):
        async def on_request(request):
            try:
                rid = id(request)
                url = str(getattr(request, "url", "") or "")
                method = str(getattr(request, "method", "") or "").upper()
                rtype = ""
                try:
                    rtype = str(request.resource_type or "")
                except Exception:
                    rtype = ""
                if not self._should_capture_network(url, method, rtype):
                    return
                headers = {}
                try:
                    headers = dict(request.headers or {})
                except Exception:
                    headers = {}
                post_data = ""
                try:
                    post_data = request.post_data or ""
                except Exception:
                    post_data = ""
                payload = {
                    "url": url,
                    "method": method,
                    "headers": headers,
                    "post_data": post_data,
                    "ts": time.time(),
                    "step_record_id": int(self._current_step_record_id or 0),
                    "step_number": int(self._current_step_number or 0),
                }
                if self._net_lock:
                    async with self._net_lock:
                        self._pending_requests[rid] = payload
                else:
                    self._pending_requests[rid] = payload
            except Exception:
                return

        async def on_response(response):
            try:
                req = response.request
                rid = id(req)
                if self._net_lock:
                    async with self._net_lock:
                        info = self._pending_requests.pop(rid, None)
                else:
                    info = self._pending_requests.pop(rid, None)
                status = 0
                try:
                    status = int(response.status or 0)
                except Exception:
                    status = 0
                url = str(getattr(req, "url", "") or "") if req else str(getattr(response, "url", "") or "")
                method = str(getattr(req, "method", "") or "").upper() if req else ""
                if info is None:
                    if status < 400:
                        return
                    info = {"url": url, "method": method, "headers": {}, "post_data": "", "ts": time.time(), "step_record_id": int(self._current_step_record_id or 0)}
                step_record_id = int(info.get("step_record_id") or self._current_step_record_id or 0)
                started = float(info.get("ts") or time.time())
                duration_ms = int(max(0.0, (time.time() - started) * 1000.0))

                resp_headers = {}
                try:
                    resp_headers = dict(response.headers or {})
                except Exception:
                    resp_headers = {}
                body_text = ""
                try:
                    body_text = await response.text()
                except Exception:
                    body_text = ""
                req_data = self._encode_request_payload(url, info.get("headers") or {}, info.get("post_data") or "")
                resp_data = self._encode_response_payload(status, resp_headers, body_text, duration_ms=duration_ms)
                await self._db_create_network_entry(
                    step_record_id=step_record_id,
                    url=url,
                    method=method,
                    status_code=status,
                    request_data=req_data,
                    response_data=resp_data,
                )
            except Exception:
                return

        try:
            context.on("request", on_request)
            context.on("response", on_response)
        except Exception:
            pass

    def _should_capture_network(self, url: str, method: str, resource_type: str) -> bool:
        u = (url or "").lower()
        m = (method or "").upper()
        rt = (resource_type or "").lower()
        if rt in ("xhr", "fetch"):
            return True
        if m in ("POST", "PUT", "PATCH", "DELETE"):
            return True
        if any(k in u for k in ["login", "signin", "auth", "token", "session", "oauth", "refresh"]):
            return True
        return False

    def _encode_request_payload(self, url: str, headers: dict, post_data: str) -> str:
        safe_headers = self._mask_headers(headers or {})
        query = {}
        try:
            q = urlsplit(url).query
            if q:
                query = {k: (v[-1] if isinstance(v, list) and v else v) for k, v in parse_qs(q, keep_blank_values=True).items()}
                query = self._mask_kv(query)
        except Exception:
            query = {}
        out = {"url": str(url or ""), "headers": safe_headers}
        if query:
            out["query"] = query
        body = (post_data or "").strip()
        if body:
            bj = self._try_parse_json(body)
            if bj is not None:
                out["body_json"] = self._mask_json(bj)
            else:
                bf = self._try_parse_form(body)
                if bf:
                    out["body_form"] = self._mask_json(bf)
                else:
                    out["body_raw"] = self._mask_text(body)[:10000]
        return json.dumps(out, ensure_ascii=False)

    def _encode_response_payload(self, status: int, headers: dict, body_text: str, duration_ms: int = 0) -> str:
        safe_headers = self._mask_headers(headers or {})
        if duration_ms:
            safe_headers = dict(safe_headers)
            safe_headers["x_duration_ms"] = int(duration_ms)
        out = {"status": int(status or 0), "headers": safe_headers}
        body = (body_text or "").strip()
        if body:
            bj = self._try_parse_json(body)
            if bj is not None:
                out["body_json"] = self._mask_json(bj)
            else:
                out["body_text"] = self._mask_text(body)[:20000]
        return json.dumps(out, ensure_ascii=False)

    def _mask_headers(self, headers: dict) -> dict:
        out = {}
        for k, v in (headers or {}).items():
            key = str(k or "")
            low = key.lower()
            if low in ("authorization", "cookie", "set-cookie", "x-api-key", "apikey", "proxy-authorization"):
                out[key] = "***"
            else:
                out[key] = str(v)[:500]
        return out

    def _mask_kv(self, obj: dict) -> dict:
        out = {}
        for k, v in (obj or {}).items():
            key = str(k or "")
            low = key.lower()
            if any(x in low for x in ["token", "authorization", "cookie", "password", "pwd", "secret", "key"]):
                out[key] = "***"
            else:
                out[key] = v
        return out

    def _try_parse_json(self, s: str):
        try:
            return json.loads(s)
        except Exception:
            return None

    def _try_parse_form(self, s: str) -> dict:
        try:
            parsed = parse_qs(s, keep_blank_values=True)
            out = {}
            for k, v in parsed.items():
                if isinstance(v, list):
                    out[k] = v[-1] if v else ""
                else:
                    out[k] = v
            return out
        except Exception:
            return {}

    def _mask_json(self, obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key = str(k or "")
                low = key.lower()
                if any(x in low for x in ["token", "authorization", "cookie", "password", "pwd", "secret", "key"]):
                    out[key] = "***"
                else:
                    out[key] = self._mask_json(v)
            return out
        if isinstance(obj, list):
            return [self._mask_json(x) for x in obj[:200]]
        if isinstance(obj, str):
            return self._mask_text(obj)
        return obj

    def _mask_text(self, s: str) -> str:
        raw = str(s or "")
        if not raw:
            return raw
        raw = re.sub(r"(?i)(authorization|token|access_token|refresh_token)\\s*[:=]\\s*([A-Za-z0-9._-]{6,})", r"\\1=***", raw)
        raw = re.sub(r"(?i)(password|pwd)\\s*[:=]\\s*([^\\s&]{2,})", r"\\1=***", raw)
        return raw

    def _infer_target_text(self, selector: str) -> str:
        s = str(selector or "").strip()
        if not s:
            return ""
        m = re.search(r"(?:title|aria-label)\\s*\\*?=\\s*['\\\"]([^'\\\"]{2,60})['\\\"]", s, flags=re.I)
        if m:
            return (m.group(1) or "").strip()
        m = re.search(r"\\[(?:title|aria-label)\\s*\\*?=\\s*['\\\"]([^'\\\"]{2,60})['\\\"]\\]", s, flags=re.I)
        if m:
            return (m.group(1) or "").strip()
        m = re.search(r"\\[(?:title|aria-label)\\s*\\*?=\\s*([^\\]]+)\\]", s, flags=re.I)
        if m:
            inner = (m.group(1) or "").strip()
            m2 = re.search(r"['\\\"]([^'\\\"]{2,60})['\\\"]", inner)
            if m2:
                return (m2.group(1) or "").strip()
        m = re.search(r"['\\\"]([^'\\\"]*[\\u4e00-\\u9fff][^'\\\"]*)['\\\"]", s)
        if m:
            return (m.group(1) or "").strip()[:60]
        return ""

    def _infer_menu_from_target(self, target: str) -> str:
        t = str(target or "").strip()
        if not t:
            return ""
        if "用户" in t:
            return "用户管理"
        if "学员" in t:
            return "学员管理"
        if "订单" in t:
            return "订单管理"
        return ""

    async def _ensure_menu(self, page, menu_text: str) -> bool:
        mt = str(menu_text or "").strip()
        if not mt:
            return False
        for sel in [
            f"text={mt}",
            f"text={mt[:2]}",
        ]:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=1500)
                await loc.click(timeout=4000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(300)
                return True
            except Exception:
                continue
        return False
