import json
import time
import uuid
import os
from django import forms
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET, require_http_methods
from django.http import JsonResponse, HttpResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.urls import reverse
from django.http import HttpResponseForbidden
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.cache import cache
from django.core import signing

from .models import AutoTestExecution, AutoTestStepRecord, AutoTestNetworkEntry, AutoTestReportShare, AutoTestSchedule
from testcases.models import TestCase, TestCaseStep
from projects.models import Project
from requirements.models import Requirement
from .services.assertions import evaluate_execution_assertions
from .services.execution_queue import enqueue_execution
from urllib.parse import urlparse
from bugs.models import Bug
from core.visibility import visible_projects, is_admin_user
from core.pagination import paginate
import re
from users.ai_config import AIKeyNotConfigured, resolve_exec_params, resolve_ocr_params
from autotest.services.datasets import expand_dataset

User = get_user_model()


class AutoTestScheduleForm(forms.ModelForm):
    cases = forms.ModelMultipleChoiceField(
        label="AI 执行用例",
        queryset=TestCase.objects.none(),
        required=True,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )

    class Meta:
        model = AutoTestSchedule
        fields = ["project", "name", "enabled", "schedule_type", "interval_minutes", "daily_time", "run_at"]
        widgets = {
            "project": forms.Select(attrs={"class": "form-select"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "schedule_type": forms.Select(attrs={"class": "form-select"}),
            "interval_minutes": forms.NumberInput(attrs={"class": "form-control"}),
            "daily_time": forms.TimeInput(attrs={"class": "form-control", "type": "time"}),
            "run_at": forms.DateTimeInput(attrs={"class": "form-control", "type": "datetime-local"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        projects = visible_projects(user) if user else Project.objects.none()
        self.fields["project"].queryset = projects

        project_id = None
        if self.data and self.data.get("project"):
            try:
                project_id = int(self.data.get("project"))
            except Exception:
                project_id = None
        if project_id is None and getattr(self.instance, "project_id", None):
            project_id = int(self.instance.project_id)

        qs = TestCase.objects.filter(project__in=projects, execution_type=2).order_by("-id")
        if project_id:
            qs = qs.filter(project_id=project_id)
        self.fields["cases"].queryset = qs

        if self.instance and self.instance.pk:
            try:
                ids = self.instance.case_ids if isinstance(self.instance.case_ids, list) else []
                ids = [int(x) for x in ids]
                self.initial["cases"] = qs.filter(id__in=ids)
            except Exception:
                pass

    def clean(self):
        cleaned = super().clean()
        st = cleaned.get("schedule_type")
        if st == "daily":
            if not cleaned.get("daily_time"):
                raise forms.ValidationError("请选择每天执行时间")
        elif st == "once":
            if not cleaned.get("run_at"):
                raise forms.ValidationError("请选择单次执行时间")
        else:
            mins = cleaned.get("interval_minutes") or 0
            try:
                mins = int(mins)
            except Exception:
                mins = 0
            if mins < 1:
                raise forms.ValidationError("间隔分钟数至少为 1")
        cases = cleaned.get("cases") or []
        if not cases:
            raise forms.ValidationError("请选择至少一个 AI 执行用例")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        cases = list(self.cleaned_data.get("cases") or [])
        obj.case_ids = [int(c.id) for c in cases]
        if not obj.created_by_id and self.user:
            obj.created_by = self.user
        now = timezone.now()
        if obj.schedule_type == "once":
            ra = self.cleaned_data.get("run_at")
            if ra and ra < now:
                ra = now
            obj.next_run_at = ra
        else:
            obj.next_run_at = obj.compute_next_run_at(now)
        obj.locked_until = None
        if commit:
            obj.save()
        return obj

@login_required
def ai_direct_execute(request):
    projects = visible_projects(request.user)
    requirements = Requirement.objects.filter(project__in=projects)
    return render(request, 'autotest/ai_direct.html', {'projects': projects, 'requirements': requirements})

@login_required
@require_POST
def execute_direct(request):
    try:
        data = json.loads(request.body)
        title = data.get('title')
        steps = data.get('steps', [])
        project_id = data.get('project_id')
        requirement_id = data.get('requirement_id')
        
        if not title or not steps or not project_id:
            return JsonResponse({'success': False, 'error': 'Missing required fields'})

        try:
            resolve_exec_params(request.user)
            if bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)) or bool(getattr(settings, "AI_EXEC_GUIDE_HINT_ENABLED", True)):
                resolve_ocr_params(request.user)
        except AIKeyNotConfigured as e:
            return JsonResponse({'success': False, 'error': str(e) or "请先在个人中心配置 AI 执行相关 Key"}, status=400)
            
        project = get_object_or_404(visible_projects(request.user), pk=project_id)
        requirement = None
        if requirement_id:
            requirement = get_object_or_404(Requirement.objects.filter(project=project), pk=requirement_id)
        
        # 1. Create Test Case
        case = TestCase.objects.create(
            project=project,
            requirement=requirement,
            title=title,
            type=1, # Functional
            execution_type=2, # AI Automated
            priority=2,
            creator=request.user
        )
        
        # 2. Create Steps
        for i, step_desc in enumerate(steps):
            TestCaseStep.objects.create(
                case=case,
                step_number=i+1,
                description=step_desc,
                expected_result="AI自动验证",
                is_executed=False
            )
            
        # 3. Create Execution
        headless_override = None
        try:
            raw = request.body
            if raw:
                data0 = json.loads(raw)
                v = data0.get("headless")
                if isinstance(v, bool):
                    headless_override = v
                elif isinstance(v, str):
                    vv = v.strip().lower()
                    if vv in ("true", "1", "yes", "y"):
                        headless_override = True
                    elif vv in ("false", "0", "no", "n"):
                        headless_override = False
        except Exception:
            headless_override = None
        trigger_payload = {}
        if headless_override is not None:
            trigger_payload["headless"] = bool(headless_override)
        execution = AutoTestExecution.objects.create(
            case=case,
            executor=request.user,
            status='pending',
            trigger_payload=trigger_payload,
        )
        
        enqueue_execution(execution.id)
        
        return JsonResponse({'success': True, 'execution_id': execution.id})
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
@require_POST
def run_test(request, case_id):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=case_id)
    headless_override = None
    try:
        raw = request.body
        if raw:
            data0 = json.loads(raw)
            v = data0.get("headless")
            if isinstance(v, bool):
                headless_override = v
            elif isinstance(v, str):
                vv = v.strip().lower()
                if vv in ("true", "1", "yes", "y"):
                    headless_override = True
                elif vv in ("false", "0", "no", "n"):
                    headless_override = False
    except Exception:
        headless_override = None
    trigger_payload = {}
    if headless_override is not None:
        trigger_payload["headless"] = bool(headless_override)
    try:
        resolve_exec_params(request.user)
        if bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)) or bool(getattr(settings, "AI_EXEC_GUIDE_HINT_ENABLED", True)):
            resolve_ocr_params(request.user)
    except AIKeyNotConfigured as e:
        return JsonResponse({'success': False, 'error': str(e) or "请先在个人中心配置 AI 执行相关 Key"}, status=400)
    
    if getattr(case, "case_mode", "normal") == "advanced":
        params = getattr(case, "parameters", {}) or {}
        datasets = params.get("datasets") if isinstance(params, dict) else None
        if not isinstance(datasets, list) or not datasets:
            try:
                params2 = params if isinstance(params, dict) else {}
                params2["datasets"] = [{"name": "数据集1", "vars": {}}]
                if not isinstance(params2.get("max_runs"), int):
                    params2["max_runs"] = 10
                if "stop_on_fail" not in params2:
                    params2["stop_on_fail"] = False
                case.parameters = params2
                case.save(update_fields=["parameters"])
                params = params2
                datasets = params2.get("datasets")
            except Exception:
                return JsonResponse({'success': False, 'error': '高级用例未配置参数集 datasets'}, status=400)
        try:
            max_runs = int((params.get("max_runs") if isinstance(params, dict) else None) or 0)
        except Exception:
            max_runs = 0
        if max_runs <= 0:
            max_runs = 10
        stop_on_fail = bool(params.get("stop_on_fail")) if isinstance(params, dict) else False

        batch_id = uuid.uuid4()
        expanded = []
        for ds in datasets:
            expanded.extend(expand_dataset(ds, max_runs=max_runs))
            if len(expanded) >= max_runs:
                expanded = expanded[:max_runs]
                break
        run_total = min(len(expanded), max_runs)
        execution_ids = []
        for idx, ds in enumerate(expanded[:run_total]):
            if not isinstance(ds, dict):
                continue
            name = str(ds.get("name") or f"数据集{idx+1}")[:120]
            vars_obj = ds.get("vars") or {}
            if not isinstance(vars_obj, dict):
                vars_obj = {}
            ex = AutoTestExecution.objects.create(
                case=case,
                executor=request.user,
                status='pending',
                batch_id=batch_id,
                run_index=idx + 1,
                run_total=run_total,
                dataset_name=name,
                dataset_vars=vars_obj,
                trigger_payload=trigger_payload,
            )
            execution_ids.append(ex.id)
            enqueue_execution(ex.id)
            if stop_on_fail:
                pass
        if not execution_ids:
            return JsonResponse({'success': False, 'error': '参数集无有效数据'}, status=400)
        return JsonResponse({'success': True, 'execution_id': execution_ids[0], 'execution_ids': execution_ids, 'batch_id': str(batch_id)})

    execution = AutoTestExecution.objects.create(case=case, executor=request.user, status='pending', trigger_payload=trigger_payload)
    enqueue_execution(execution.id)
    return JsonResponse({'success': True, 'execution_id': execution.id})

@login_required
@require_POST
def batch_run_test(request):
    try:
        data = json.loads(request.body)
        case_ids = data.get('case_ids', [])
        trigger_payload = {"headless": True}
        try:
            resolve_exec_params(request.user)
            if bool(getattr(settings, "AI_EXEC_TOAST_OCR_ENABLED", True)) or bool(getattr(settings, "AI_EXEC_GUIDE_HINT_ENABLED", True)):
                resolve_ocr_params(request.user)
        except AIKeyNotConfigured as e:
            return JsonResponse({'success': False, 'error': str(e) or "请先在个人中心配置 AI 执行相关 Key"}, status=400)
        
        execution_ids = []
        for case_id in case_ids:
            case = TestCase.objects.get(pk=case_id, project__in=visible_projects(request.user))
            if getattr(case, "case_mode", "normal") == "advanced":
                params = getattr(case, "parameters", {}) or {}
                datasets = params.get("datasets") if isinstance(params, dict) else None
                if not isinstance(datasets, list) or not datasets:
                    continue
                try:
                    max_runs = int((params.get("max_runs") if isinstance(params, dict) else None) or 0)
                except Exception:
                    max_runs = 0
                if max_runs <= 0:
                    max_runs = 10
                batch_id = uuid.uuid4()
                expanded = []
                for ds in datasets:
                    expanded.extend(expand_dataset(ds, max_runs=max_runs))
                    if len(expanded) >= max_runs:
                        expanded = expanded[:max_runs]
                        break
                run_total = min(len(expanded), max_runs)
                for idx, ds in enumerate(expanded[:run_total]):
                    if not isinstance(ds, dict):
                        continue
                    name = str(ds.get("name") or f"数据集{idx+1}")[:120]
                    vars_obj = ds.get("vars") or {}
                    if not isinstance(vars_obj, dict):
                        vars_obj = {}
                    execution = AutoTestExecution.objects.create(
                        case=case,
                        executor=request.user,
                        status='pending',
                        batch_id=batch_id,
                        run_index=idx + 1,
                        run_total=run_total,
                        dataset_name=name,
                        dataset_vars=vars_obj,
                        trigger_payload=trigger_payload,
                    )
                    execution_ids.append(execution.id)
                    enqueue_execution(execution.id)
            else:
                execution = AutoTestExecution.objects.create(case=case, executor=request.user, status='pending', trigger_payload=trigger_payload)
                execution_ids.append(execution.id)
                enqueue_execution(execution.id)
            
        return JsonResponse({'success': True, 'execution_ids': execution_ids})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
@require_POST
def pause_test(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    if execution.status == 'running':
        execution.pause_signal = True
        execution.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Test is not running'})

@login_required
@require_POST
def resume_test(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    if execution.status == 'paused':
        execution.pause_signal = False
        execution.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Test is not paused'})

@login_required
@require_POST
def stop_test(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    if execution.status in ['running', 'paused', 'pending', 'queued']:
        execution.stop_signal = True
        execution.save()
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Test cannot be stopped'})

@login_required
def get_execution_status(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    
    steps = execution.step_records.all().order_by('step_number')
    steps_data = []
    for step in steps:
        metrics = step.metrics or {}
        dur = metrics.get("duration_ms")
        if dur is None and step.status == "pending":
            try:
                started_at_ms = metrics.get("started_at_ms")
                if started_at_ms:
                    dur = int(time.time() * 1000 - int(started_at_ms))
            except Exception:
                dur = None
        steps_data.append({
            'id': step.id,
            'step_number': step.step_number,
            'description': step.description,
            'status': step.status,
            'ai_thought': step.ai_thought,
            'error_message': step.error_message,
            'screenshot_after': step.screenshot_after.url if step.screenshot_after else None,
            'ocr_screenshot': step.ocr_screenshot.url if getattr(step, "ocr_screenshot", None) else None,
            'metrics': metrics,
            'duration_ms': dur,
        })

    def _mask_vars(obj):
        if not isinstance(obj, dict):
            return {}
        out = {}
        for k, v in obj.items():
            ks = str(k).lower()
            if any(x in ks for x in ["pass", "pwd", "token", "secret", "key"]):
                out[k] = "******"
            else:
                out[k] = v
        return out

    return JsonResponse({
        'status': execution.status,
        'screenshot_mode': str(getattr(settings, "AI_EXEC_SCREENSHOT_MODE", "all") or "all").strip().lower(),
        'steps': steps_data,
        'summary': execution.result_summary or {},
        'stop_signal': execution.stop_signal,
        'pause_signal': execution.pause_signal,
        'run': {
            'batch_id': str(getattr(execution, "batch_id", "") or ""),
            'run_index': int(getattr(execution, "run_index", 1) or 1),
            'run_total': int(getattr(execution, "run_total", 1) or 1),
            'dataset_name': str(getattr(execution, "dataset_name", "") or ""),
            'dataset_vars': _mask_vars(getattr(execution, "dataset_vars", {}) or {}),
        },
    })

@login_required
def report_list(request):
    executions = AutoTestExecution.objects.select_related("case", "executor").filter(case__project__in=visible_projects(request.user))
    case_id = request.GET.get("case_id")
    selected_case = None
    if case_id and str(case_id).isdigit():
        selected_case = TestCase.objects.filter(id=int(case_id), project__in=visible_projects(request.user)).first()
        executions = executions.filter(case_id=int(case_id))
    executions = executions.order_by("-start_time")
    pg = paginate(request, executions, per_page=20)
    return render(
        request,
        'autotest/report_list.html',
        {'executions': pg.page_obj, 'selected_case': selected_case, 'page_obj': pg.page_obj, 'paginator': pg.paginator, 'is_paginated': pg.is_paginated, 'page_range': pg.page_range},
    )

@login_required
@ensure_csrf_cookie
def report_detail(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    network_rows = list(
        AutoTestNetworkEntry.objects.filter(step_record__execution=execution)
        .order_by("id")
        .values("url", "method", "status_code", "request_data", "response_data")
    )
    assertions = evaluate_execution_assertions(network_rows)
    assertions_failed = [a for a in (assertions or []) if not bool(a.get("passed"))]
    bug = None
    try:
        bug_id = (execution.result_summary or {}).get("bug_id")
        if not bug_id:
            for s in execution.step_records.all().order_by("-step_number"):
                try:
                    m = s.metrics or {}
                except Exception:
                    m = {}
                try:
                    bid = (m or {}).get("bug_id")
                except Exception:
                    bid = None
                if bid:
                    bug_id = bid
                    break
        if bug_id:
            bug = Bug.objects.filter(id=int(bug_id), project_id=execution.case.project_id).first()
    except Exception:
        bug = None

    severe_codes = {400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504}
    severe_counts = {}
    severe_endpoints = {}
    for r in network_rows:
        try:
            sc = int(r.get("status_code") or 0)
        except Exception:
            sc = 0
        if sc in severe_codes or sc >= 500:
            severe_counts[str(sc)] = severe_counts.get(str(sc), 0) + 1
            u = r.get("url") or ""
            try:
                parsed = urlparse(u)
                key = (parsed.netloc + parsed.path) if parsed.netloc else parsed.path
                if not key:
                    key = u
            except Exception:
                key = u
            key = key[:200]
            severe_endpoints[key] = severe_endpoints.get(key, 0) + 1

    severe_endpoints_top = sorted(severe_endpoints.items(), key=lambda x: (-x[1], x[0]))[:12]
    return render(
        request,
        'autotest/report_detail.html',
        {
            'execution': execution,
            'assertions_failed': assertions_failed,
            'severe_counts': severe_counts,
            'severe_endpoints_top': severe_endpoints_top,
            'bug': bug,
        },
    )


@login_required
def report_export_json(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    steps = []
    for step in execution.step_records.all().order_by("step_number"):
        steps.append(
            {
                "id": step.id,
                "step_number": step.step_number,
                "description": step.description,
                "status": step.status,
                "ai_thought": step.ai_thought,
                "action_script": step.action_script,
                "error_message": step.error_message,
                "screenshot_before": step.screenshot_before.url if step.screenshot_before else None,
                "screenshot_after": step.screenshot_after.url if step.screenshot_after else None,
            }
        )

    network_rows = list(
        AutoTestNetworkEntry.objects.filter(step_record__execution=execution)
        .order_by("id")
        .values("url", "method", "status_code", "request_data", "response_data", "timestamp")
    )
    assertions = evaluate_execution_assertions(
        [
            {
                "url": r.get("url"),
                "method": r.get("method"),
                "status_code": r.get("status_code"),
                "request_data": r.get("request_data"),
                "response_data": r.get("response_data"),
            }
            for r in network_rows
        ]
    )

    payload = {
        "execution": {
            "id": execution.id,
            "case_id": execution.case_id,
            "case_title": execution.case.title if execution.case else "",
            "status": execution.status,
            "executor": getattr(execution.executor, "username", None),
            "start_time": execution.start_time.isoformat() if execution.start_time else None,
            "end_time": execution.end_time.isoformat() if execution.end_time else None,
            "result_summary": execution.result_summary,
            "har_url": execution.har_file.url if execution.har_file else None,
        },
        "assertions": assertions,
        "steps": steps,
        "network_entries": [
            {
                "url": r.get("url"),
                "method": r.get("method"),
                "status_code": r.get("status_code"),
                "request_data": r.get("request_data"),
                "response_data": r.get("response_data"),
                "timestamp": r.get("timestamp").isoformat() if r.get("timestamp") else None,
            }
            for r in network_rows
        ],
    }

    content = json.dumps(payload, ensure_ascii=False, indent=2)
    resp = HttpResponse(content, content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="report_{execution.id}.json"'
    return resp


@login_required
def report_export_md(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)

    steps = list(execution.step_records.all().order_by("step_number"))
    network_rows = list(
        AutoTestNetworkEntry.objects.filter(step_record__execution=execution)
        .order_by("id")
        .values("url", "method", "status_code", "request_data", "response_data", "timestamp")
    )
    assertions = evaluate_execution_assertions(
        [
            {
                "url": r.get("url"),
                "method": r.get("method"),
                "status_code": r.get("status_code"),
                "request_data": r.get("request_data"),
                "response_data": r.get("response_data"),
            }
            for r in network_rows
        ]
    )

    lines = []
    lines.append(f"# 执行报告：{execution.case.title if execution.case else ''} (#{execution.id})")
    lines.append("")
    lines.append("## 概览")
    lines.append(f"- 状态：{execution.get_status_display()}")
    lines.append(f"- 执行人：{getattr(execution.executor, 'username', '')}")
    lines.append(f"- 开始时间：{execution.start_time}")
    lines.append(f"- 结束时间：{execution.end_time}")
    if execution.result_summary:
        rs = execution.result_summary
        if isinstance(rs, dict) and rs.get("highlights"):
            lines.append("- 结论要点：")
            for h in (rs.get("highlights") or [])[:20]:
                lines.append(f"  - {h}")
            if rs.get("detail"):
                lines.append("")
                lines.append("### 详细结论")
                lines.append(str(rs.get("detail") or "").strip())
        else:
            lines.append(f"- 结果汇总：{execution.result_summary}")
    lines.append("")

    if assertions:
        lines.append("## 关键接口断言")
        lines.append("")
        lines.append("| 断言 | 结果 | 说明 |")
        lines.append("| --- | --- | --- |")
        for a in assertions:
            name = str(a.get("name") or "")
            passed = "PASS" if a.get("passed") else "FAIL"
            detail = str(a.get("detail") or "")
            lines.append(f"| {name} | {passed} | {detail} |")
        lines.append("")

    lines.append("## 执行步骤")
    lines.append("")
    lines.append("| 序号 | 状态 | 描述 |")
    lines.append("| --- | --- | --- |")
    for s in steps:
        lines.append(f"| {s.step_number} | {s.get_status_display()} | {str(s.description).replace(chr(10), ' ')} |")
    lines.append("")

    if network_rows:
        lines.append("## 网络请求（前 200 条）")
        lines.append("")
        lines.append("| 时间 | 方法 | 状态码 | URL |")
        lines.append("| --- | --- | --- | --- |")
        for r in network_rows[:200]:
            ts = r.get("timestamp").isoformat() if r.get("timestamp") else ""
            method = r.get("method") or ""
            status_code = r.get("status_code") or ""
            url = (r.get("url") or "").replace("|", "%7C")
            lines.append(f"| {ts} | {method} | {status_code} | {url} |")
        lines.append("")

    content = "\n".join(lines)
    resp = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="report_{execution.id}.md"'
    return resp

@login_required
@require_POST
def report_share_create(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    share, created = AutoTestReportShare.objects.get_or_create(
        execution=execution,
        defaults={"created_by": request.user},
    )
    if not share.created_by and request.user:
        share.created_by = request.user
        share.save(update_fields=["created_by"])
    url = request.build_absolute_uri(reverse("report_shared", args=[share.token]))
    return JsonResponse({"success": True, "url": url, "token": str(share.token)})


@login_required
def report_shared(request, token):
    share = get_object_or_404(AutoTestReportShare, token=token)
    execution = share.execution
    if not is_admin_user(request.user):
        if not visible_projects(request.user).filter(id=execution.case.project_id).exists():
            return HttpResponseForbidden("无权限查看该报告")
    network_rows = list(
        AutoTestNetworkEntry.objects.filter(step_record__execution=execution)
        .order_by("id")
        .values("url", "method", "status_code", "request_data", "response_data")
    )
    assertions = evaluate_execution_assertions(network_rows)
    assertions_failed = [a for a in (assertions or []) if not bool(a.get("passed"))]
    bug = None
    try:
        bug_id = (execution.result_summary or {}).get("bug_id")
        if bug_id:
            bug = Bug.objects.filter(id=int(bug_id), project_id=execution.case.project_id).first()
    except Exception:
        bug = None
    return render(
        request,
        'autotest/report_detail.html',
        {'execution': execution, 'assertions_failed': assertions_failed, 'is_shared': True, 'bug': bug},
    )

@login_required
def console(request, execution_id):
    execution = get_object_or_404(AutoTestExecution.objects.filter(case__project__in=visible_projects(request.user)), pk=execution_id)
    return render(request, 'autotest/console.html', {'execution': execution, 'poll_ms': getattr(settings, "AI_EXEC_CONSOLE_POLL_MS", 1000)})

@login_required
def upload_sandbox(request):
    if not is_admin_user(request.user):
        return HttpResponseForbidden("无权限访问")
    return render(request, "autotest/upload_sandbox.html", {})


@login_required
def schedule_list(request):
    schedules = AutoTestSchedule.objects.filter(created_by=request.user).order_by("-created_at", "-id")
    pg = paginate(request, schedules, per_page=20)
    return render(request, "autotest/schedule_list.html", {"schedules": pg.page_obj, "page_obj": pg.page_obj, "paginator": pg.paginator, "is_paginated": pg.is_paginated, "page_range": pg.page_range})


@login_required
def schedule_create(request):
    if request.method == "POST":
        form = AutoTestScheduleForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "计划任务已创建")
            return redirect("autotest_schedule_list")
    else:
        form = AutoTestScheduleForm(user=request.user)
    return render(request, "autotest/schedule_form.html", {"form": form, "title": "新建计划任务"})


@login_required
def schedule_edit(request, schedule_id: int):
    s = get_object_or_404(AutoTestSchedule, id=int(schedule_id), created_by=request.user)
    if request.method == "POST":
        form = AutoTestScheduleForm(request.POST, instance=s, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "计划任务已更新")
            return redirect("autotest_schedule_list")
    else:
        form = AutoTestScheduleForm(instance=s, user=request.user)
    return render(request, "autotest/schedule_form.html", {"form": form, "title": "编辑计划任务"})


@login_required
@require_POST
def schedule_toggle(request, schedule_id: int):
    s = get_object_or_404(AutoTestSchedule, id=int(schedule_id), created_by=request.user)
    s.enabled = not bool(s.enabled)
    if s.enabled:
        now = timezone.now()
        if s.schedule_type == "once":
            ra = s.run_at or now
            if ra < now:
                ra = now
            s.next_run_at = ra
        else:
            s.next_run_at = s.compute_next_run_at(now)
    s.locked_until = None
    s.save(update_fields=["enabled", "next_run_at", "locked_until"])
    messages.success(request, "已启用" if s.enabled else "已停用")
    return redirect("autotest_schedule_list")


@login_required
@require_POST
def schedule_delete(request, schedule_id: int):
    s = get_object_or_404(AutoTestSchedule, id=int(schedule_id), created_by=request.user)
    s.delete()
    messages.success(request, "计划任务已删除")
    return redirect("autotest_schedule_list")


@login_required
@require_POST
def schedule_run_now(request, schedule_id: int):
    s = get_object_or_404(AutoTestSchedule, id=int(schedule_id), created_by=request.user)
    ids = s.case_ids if isinstance(s.case_ids, list) else []
    ids = [int(x) for x in ids if str(x).strip().isdigit()]
    cases = list(TestCase.objects.filter(id__in=ids, project__in=visible_projects(request.user)))
    created_ids = []
    for case in cases:
        if getattr(case, "case_mode", "normal") == "advanced":
            params = getattr(case, "parameters", {}) or {}
            datasets = params.get("datasets") if isinstance(params, dict) else None
            if not isinstance(datasets, list) or not datasets:
                continue
            try:
                max_runs = int((params.get("max_runs") if isinstance(params, dict) else None) or 0)
            except Exception:
                max_runs = 0
            if max_runs <= 0:
                max_runs = 10
            batch_id = uuid.uuid4()
            expanded = []
            for ds in datasets:
                expanded.extend(expand_dataset(ds, max_runs=max_runs))
                if len(expanded) >= max_runs:
                    expanded = expanded[:max_runs]
                    break
            run_total = min(len(expanded), max_runs)
            for idx, ds in enumerate(expanded[:run_total]):
                if not isinstance(ds, dict):
                    continue
                name = str(ds.get("name") or f"数据集{idx+1}")[:120]
                vars_obj = ds.get("vars") or {}
                if not isinstance(vars_obj, dict):
                    vars_obj = {}
                ex = AutoTestExecution.objects.create(
                    case=case,
                    executor=request.user,
                    status="pending",
                    batch_id=batch_id,
                    run_index=idx + 1,
                    run_total=run_total,
                    dataset_name=name,
                    dataset_vars=vars_obj,
                    trigger_source="manual",
                    trigger_payload={"schedule_id": int(s.id), "case_id": int(case.id)},
                    schedule=s,
                )
                enqueue_execution(ex.id)
                created_ids.append(ex.id)
        else:
            ex = AutoTestExecution.objects.create(
                case=case,
                executor=request.user,
                status="pending",
                trigger_source="manual",
                trigger_payload={"schedule_id": int(s.id), "case_id": int(case.id)},
                schedule=s,
            )
            enqueue_execution(ex.id)
            created_ids.append(ex.id)
    if created_ids:
        messages.success(request, f"已触发执行：{len(created_ids)} 条")
    else:
        messages.error(request, "未触发执行：无可执行用例或参数配置不完整")
    return redirect("autotest_schedule_list")



def _ci_token_ok(request) -> bool:
    want = (getattr(settings, "AI_EXEC_CI_TOKEN", "") or os.getenv("AI_EXEC_CI_TOKEN", "") or "").strip()
    got = (request.headers.get("X-CI-TOKEN") or request.headers.get("Authorization") or "").strip()
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    if want and bool(got) and got == want:
        return True
    try:
        from users.models import UserCICredential
        return bool(got) and UserCICredential.objects.filter(token=got, enabled=True).exists()
    except Exception:
        return False


def _ci_user_from_token(request):
    got = (request.headers.get("X-CI-TOKEN") or request.headers.get("Authorization") or "").strip()
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    want = (getattr(settings, "AI_EXEC_CI_TOKEN", "") or os.getenv("AI_EXEC_CI_TOKEN", "") or "").strip()
    if want and got == want:
        return None
    try:
        from users.models import UserCICredential
        cred = UserCICredential.objects.select_related("user").filter(token=got, enabled=True).first()
        if cred:
            try:
                cred.last_used_at = timezone.now()
                cred.save(update_fields=["last_used_at"])
            except Exception:
                pass
            return cred.user
    except Exception:
        return None
    return None


@login_required
def cicd_page(request):
    from users.models import UserCICredential

    cred = UserCICredential.objects.filter(user=request.user).first()
    schedules = AutoTestSchedule.objects.filter(created_by=request.user).order_by("-created_at", "-id")
    projects = visible_projects(request.user)
    cases = TestCase.objects.filter(project__in=projects, execution_type=2).order_by("-id")[:500]

    selected_schedule_id = 0
    try:
        selected_schedule_id = int(request.GET.get("schedule_id") or 0)
    except Exception:
        selected_schedule_id = 0

    selected_case_ids = []
    raw_case_ids = (request.GET.get("case_ids") or "").strip()
    if raw_case_ids:
        for part in raw_case_ids.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                selected_case_ids.append(int(part))
    selected_case_ids = [x for x in selected_case_ids if x > 0][:200]

    token_hint = ""
    if cred and cred.token:
        token_hint = cred.token[:4] + "****" + cred.token[-4:]

    base_url = (request.build_absolute_uri("/") or "").rstrip("/")
    trigger_url = base_url + "/autotest/ci/trigger/"
    status_url = base_url + "/autotest/ci/status/${EXECUTION_ID}/"

    payload = {}
    if selected_schedule_id:
        payload = {"schedule_id": selected_schedule_id}
    elif selected_case_ids:
        payload = {"case_ids": selected_case_ids}
    else:
        payload = {"schedule_id": "${SCHEDULE_ID}"}

    curl = (
        "curl -X POST \"" + trigger_url + "\" \\\n"
        + "  -H \"Content-Type: application/json\" \\\n"
        + "  -H \"X-CI-TOKEN: ${QA_CI_TOKEN}\" \\\n"
        + "  -d '" + json.dumps(payload, ensure_ascii=False) + "'\n"
    )

    gha = (
        "name: QA Autotest\n\n"
        "on:\n"
        "  workflow_dispatch:\n\n"
        "jobs:\n"
        "  run:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Trigger\n"
        "        env:\n"
        "          QA_CI_TOKEN: ${{ secrets.QA_CI_TOKEN }}\n"
        "        run: |\n"
        "          " + curl.replace("\n", "\n          ")
    )

    return render(
        request,
        "autotest/cicd.html",
        {
            "cred": cred,
            "token_hint": token_hint,
            "schedules": schedules,
            "cases": cases,
            "selected_schedule_id": selected_schedule_id,
            "selected_case_ids": selected_case_ids,
            "trigger_url": trigger_url,
            "status_url": status_url,
            "curl_snippet": curl,
            "gha_snippet": gha,
        },
    )


@login_required
@require_POST
def cicd_token_rotate(request):
    from users.models import UserCICredential
    import secrets

    token = secrets.token_urlsafe(36)[:80]
    cred, _ = UserCICredential.objects.get_or_create(user=request.user, defaults={"token": token, "enabled": True})
    if cred.token != token:
        cred.token = token
        cred.enabled = True
        cred.last_used_at = None
        cred.save(update_fields=["token", "enabled", "last_used_at"])
    messages.success(request, "CI Token 已生成/更新（请复制后保存到你的 CI Secrets）")
    request.session["cicd_last_token_plain"] = token
    try:
        request.session.modified = True
    except Exception:
        pass
    return redirect("cicd_page")


@login_required
@ensure_csrf_cookie
def playwright_record_page(request):
    projects = visible_projects(request.user)
    project_id = 0
    try:
        project_id = int(request.GET.get("project_id") or 0)
    except Exception:
        project_id = 0
    project = None
    if project_id:
        project = projects.filter(id=project_id).first()
    if project is None:
        project = projects.order_by("id").first()

    url = (request.GET.get("url") or "").strip()
    if not url and project is not None:
        url = str(getattr(project, "base_url", "") or "").strip()
    if url and not re.match(r"^https?://", url, flags=re.I):
        url = "http://" + url

    target = (request.GET.get("target") or "python").strip().lower()
    if target not in ("python", "ts", "js"):
        target = "python"

    out = (request.GET.get("output") or "").strip()
    if not out:
        out = f"recordings/playwright_codegen_{timezone.now().strftime('%Y%m%d_%H%M%S')}.{('py' if target=='python' else 'ts')}"

    win_cmd = (
        ".\\.venv\\Scripts\\python -m playwright codegen "
        + f"\"{url}\" --target {target} -o \"{out}\""
    )
    mac_cmd = (
        "./.venv/bin/python -m playwright codegen "
        + f"\"{url}\" --target {target} -o \"{out}\""
    )

    return render(
        request,
        "autotest/playwright_record.html",
        {
            "projects": projects,
            "project_id": getattr(project, "id", 0) if project else 0,
            "url": url,
            "target": target,
            "output": out,
            "win_cmd": win_cmd,
            "mac_cmd": mac_cmd,
        },
    )


@login_required
def playwright_record_download(request, kind: str):
    kind = str(kind or "").strip().lower()
    if kind not in ("ps1", "sh"):
        return HttpResponse("not found", status=404)

    url = (request.GET.get("url") or "").strip()
    target = (request.GET.get("target") or "python").strip().lower()
    output = (request.GET.get("output") or "").strip()
    if not output:
        output = f"recordings/playwright_codegen_{timezone.now().strftime('%Y%m%d_%H%M%S')}.{('py' if target=='python' else 'ts')}"
    if target not in ("python", "ts", "js"):
        target = "python"

    if kind == "ps1":
        body = (
            "$ErrorActionPreference = \"Stop\"\r\n"
            "if (!(Test-Path \".venv\")) { python -m venv .venv }\r\n"
            ".\\.venv\\Scripts\\python -m pip install -U pip\r\n"
            ".\\.venv\\Scripts\\pip install -r requirements.txt\r\n"
            ".\\.venv\\Scripts\\python -m playwright install chromium\r\n"
            f".\\.venv\\Scripts\\python -m playwright codegen \"{url}\" --target {target} -o \"{output}\"\r\n"
        )
        resp = HttpResponse(body, content_type="text/plain; charset=utf-8")
        resp["Content-Disposition"] = "attachment; filename=playwright_record.ps1"
        return resp

    body = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "if [ ! -d \".venv\" ]; then python3 -m venv .venv || python -m venv .venv; fi\n"
        "./.venv/bin/python -m pip install -U pip\n"
        "./.venv/bin/pip install -r requirements.txt\n"
        "./.venv/bin/python -m playwright install chromium\n"
        f"./.venv/bin/python -m playwright codegen \"{url}\" --target {target} -o \"{output}\"\n"
    )
    resp = HttpResponse(body, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = "attachment; filename=playwright_record.sh"
    return resp


def _recorder_cache_key(token: str) -> str:
    return "qa_recorder_session:" + str(token or "")

def _recorder_cmd_cache_key(token: str) -> str:
    return "qa_recorder_cmds:" + str(token or "")

def _strip_recorder_params(url: str) -> str:
    try:
        u = str(url or "").strip()
        if not u:
            return ""
        p = urlparse(u)
        if not p.scheme:
            return u
        qs = []
        for kv in (p.query or "").split("&"):
            if not kv:
                continue
            k = kv.split("=", 1)[0]
            if k in ("__qa_recorder_token", "__qa_recorder_host"):
                continue
            qs.append(kv)
        new_q = "&".join(qs)
        return p._replace(query=new_q).geturl()
    except Exception:
        return str(url or "").strip()


def _recorder_sign(payload: dict) -> str:
    return signing.dumps(payload, salt="qa_recorder_v1")


def _recorder_unsign(token: str, max_age_seconds: int = 6 * 3600) -> dict | None:
    try:
        obj = signing.loads(str(token or ""), salt="qa_recorder_v1", max_age=max_age_seconds)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


@login_required
@require_POST
def recorder_session_new(request):
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    try:
        project_id = int((data or {}).get("project_id") or 0)
    except Exception:
        project_id = 0
    token = _recorder_sign({"u": int(request.user.id), "pid": int(project_id), "nonce": uuid.uuid4().hex, "ts": int(time.time())})
    key = _recorder_cache_key(token)
    cache.set(
        key,
        {
            "user_id": int(request.user.id),
            "project_id": int(project_id),
            "created_at_ms": int(time.time() * 1000),
            "events": [],
        },
        timeout=6 * 3600,
    )
    return JsonResponse({"success": True, "token": token})


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def recorder_session_event(request, token: str):
    origin = ""
    try:
        origin = str(request.headers.get("Origin") or "").strip()
    except Exception:
        origin = ""
    allow_origin = origin if origin else "*"
    if request.method == "OPTIONS":
        resp = HttpResponse("", status=204)
        resp["Access-Control-Allow-Origin"] = allow_origin
        resp["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp["Access-Control-Allow-Headers"] = "content-type"
        resp["Access-Control-Max-Age"] = "600"
        return resp

    meta = _recorder_unsign(token)
    if not meta:
        resp = JsonResponse({"success": False, "error": "invalid_token"}, status=403)
        resp["Access-Control-Allow-Origin"] = allow_origin
        return resp
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    step = data.get("step") if isinstance(data, dict) else None
    if not isinstance(step, dict):
        resp = JsonResponse({"success": False, "error": "invalid_payload"}, status=400)
        resp["Access-Control-Allow-Origin"] = allow_origin
        return resp

    evt = {
        "ts_ms": int(step.get("ts_ms") or int(time.time() * 1000)),
        "url": _strip_recorder_params(str(step.get("url") or ""))[:2000],
        "action": str(step.get("action") or "")[:40],
        "by": str(step.get("by") or "")[:20],
        "selector": str(step.get("selector") or "")[:400],
        "value": _strip_recorder_params(str(step.get("value") or ""))[:300],
    }

    key = _recorder_cache_key(token)
    sess = cache.get(key) or {}
    if not isinstance(sess, dict):
        sess = {}
    events = sess.get("events")
    if not isinstance(events, list):
        events = []
    events.append(evt)
    if len(events) > 800:
        events = events[-800:]
    sess["events"] = events
    cache.set(key, sess, timeout=6 * 3600)
    resp = JsonResponse({"success": True, "count": len(events)})
    resp["Access-Control-Allow-Origin"] = allow_origin
    return resp


@login_required
@require_GET
def recorder_session_poll(request, token: str):
    meta = _recorder_unsign(token)
    if not meta:
        return JsonResponse({"success": False, "error": "invalid_token"}, status=403)
    if int(meta.get("u") or 0) != int(request.user.id):
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    try:
        since = int(request.GET.get("since") or 0)
    except Exception:
        since = 0
    key = _recorder_cache_key(token)
    sess = cache.get(key) or {}
    events = sess.get("events") if isinstance(sess, dict) else None
    if not isinstance(events, list):
        events = []
    if since < 0:
        since = 0
    items = events[since:]
    next_since = len(events)
    return JsonResponse({"success": True, "events": items, "next_since": next_since})


@login_required
@require_GET
def recorder_session_export(request, token: str):
    meta = _recorder_unsign(token)
    if not meta:
        return JsonResponse({"success": False, "error": "invalid_token"}, status=403)
    if int(meta.get("u") or 0) != int(request.user.id):
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    key = _recorder_cache_key(token)
    sess = cache.get(key) or {}
    events = sess.get("events") if isinstance(sess, dict) else None
    if not isinstance(events, list):
        events = []
    start_url = ""
    for e in events:
        if isinstance(e, dict) and str(e.get("action") or "") == "goto":
            start_url = str(e.get("value") or e.get("url") or "")
            break
    payload = {"version": "qa-recorder-0.1", "start_url": start_url, "steps": events}
    return JsonResponse({"success": True, "payload": payload})


@login_required
@require_POST
def recorder_session_commands_set(request, token: str):
    meta = _recorder_unsign(token)
    if not meta:
        return JsonResponse({"success": False, "error": "invalid_token"}, status=403)
    if int(meta.get("u") or 0) != int(request.user.id):
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    commands = (data or {}).get("commands")
    if not isinstance(commands, list):
        commands = []
    out = []
    for c in commands[:2000]:
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "action": str(c.get("action") or "")[:40],
                "by": str(c.get("by") or "")[:20],
                "selector": str(c.get("selector") or "")[:400],
                "value": str(c.get("value") or "")[:500],
                "url": str(c.get("url") or "")[:2000],
                "wait_ms": int(c.get("wait_ms") or 0) if str(c.get("wait_ms") or "").strip() else 0,
            }
        )
    cache.set(_recorder_cmd_cache_key(token), {"commands": out, "cursor": 0, "run_id": uuid.uuid4().hex}, timeout=6 * 3600)
    return JsonResponse({"success": True, "count": len(out)})


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def recorder_session_commands_poll(request, token: str):
    origin = ""
    try:
        origin = str(request.headers.get("Origin") or "").strip()
    except Exception:
        origin = ""
    allow_origin = origin if origin else "*"
    if request.method == "OPTIONS":
        resp = HttpResponse("", status=204)
        resp["Access-Control-Allow-Origin"] = allow_origin
        resp["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp["Access-Control-Allow-Headers"] = "content-type"
        resp["Access-Control-Max-Age"] = "600"
        return resp

    meta = _recorder_unsign(token)
    if not meta:
        resp = JsonResponse({"success": False, "error": "invalid_token"}, status=403)
        resp["Access-Control-Allow-Origin"] = allow_origin
        return resp
    box = cache.get(_recorder_cmd_cache_key(token)) or {}
    cmds = box.get("commands") if isinstance(box, dict) else None
    if not isinstance(cmds, list):
        cmds = []
    cursor = 0
    run_id = ""
    try:
        cursor = int((box or {}).get("cursor") or 0)
    except Exception:
        cursor = 0
    try:
        run_id = str((box or {}).get("run_id") or "")
    except Exception:
        run_id = ""
    if cursor < 0:
        cursor = 0
    items = cmds[cursor:cursor + 30]
    next_cursor = cursor + len(items)
    if isinstance(box, dict):
        box["cursor"] = next_cursor
        cache.set(_recorder_cmd_cache_key(token), box, timeout=6 * 3600)
    resp = JsonResponse({"success": True, "commands": items, "next_since": next_cursor, "run_id": run_id, "done": next_cursor >= len(cmds)})
    resp["Access-Control-Allow-Origin"] = allow_origin
    return resp


@login_required
@require_POST
def recorder_session_save_case(request, token: str):
    meta = _recorder_unsign(token)
    if not meta:
        return JsonResponse({"success": False, "error": "invalid_token"}, status=403)
    if int(meta.get("u") or 0) != int(request.user.id):
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    title = str((data or {}).get("title") or "").strip()[:200]
    try:
        project_id = int((data or {}).get("project_id") or 0)
    except Exception:
        project_id = 0
    try:
        requirement_id = int((data or {}).get("requirement_id") or 0)
    except Exception:
        requirement_id = 0
    if not title:
        title = "录制用例-" + timezone.now().strftime("%Y%m%d_%H%M%S")
    projects = visible_projects(request.user)
    project = get_object_or_404(projects, pk=project_id)
    requirement = None
    if requirement_id:
        requirement = Requirement.objects.filter(project=project, id=requirement_id).first()

    sess = cache.get(_recorder_cache_key(token)) or {}
    events = sess.get("events") if isinstance(sess, dict) else None
    if not isinstance(events, list) or not events:
        return JsonResponse({"success": False, "error": "no_events"}, status=400)

    safe_events = []
    for e in events:
        if not isinstance(e, dict):
            continue
        safe_events.append(
            {
                "ts_ms": int(e.get("ts_ms") or 0) if str(e.get("ts_ms") or "").strip() else int(time.time() * 1000),
                "url": _strip_recorder_params(str(e.get("url") or ""))[:2000],
                "action": str(e.get("action") or "")[:40],
                "by": str(e.get("by") or "")[:20],
                "selector": str(e.get("selector") or "")[:400],
                "value": _strip_recorder_params(str(e.get("value") or ""))[:300],
            }
        )
    payload = {"version": "qa-recorder-0.1", "start_url": "", "steps": safe_events}
    steps_text = _qa_recorder_to_steps(json.dumps(payload, ensure_ascii=False))
    if not steps_text:
        return JsonResponse({"success": False, "error": "no_steps"}, status=400)

    case = TestCase.objects.create(
        project=project,
        requirement=requirement,
        title=title,
        type=1,
        execution_type=2,
        priority=2,
        creator=request.user,
        parameters={"recorder_script": payload},
    )
    for i, step_desc in enumerate(steps_text, start=1):
        TestCaseStep.objects.create(
            case=case,
            step_number=i,
            description=str(step_desc or ""),
            expected_result="AI自动验证",
            is_executed=False,
        )
    return JsonResponse({"success": True, "case_id": int(case.id), "redirect_url": reverse("case_detail", kwargs={"pk": case.id})})


def _qa_recorder_to_steps(payload_text: str) -> list[str]:
    raw = str(payload_text or "").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    steps = obj.get("steps")
    if not isinstance(steps, list):
        return []
    out = []
    for s in steps[:2000]:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "").strip().lower()
        by = str(s.get("by") or "").strip().lower()
        selector = str(s.get("selector") or "").strip()
        value = s.get("value")
        if value is None:
            value = ""
        value = str(value)
        url = str(s.get("url") or "").strip()
        if action == "goto":
            u = _strip_recorder_params(value.strip() or url)
            if u:
                out.append(f"打开页面：{u}")
            continue
        if action == "click":
            if by == "text" and selector:
                out.append(f"点击「{selector}」")
            elif selector:
                out.append(f"点击元素（{selector}）")
            else:
                out.append("点击")
            continue
        if action == "type":
            v = value
            if v == "***":
                out.append(f"在「{selector or '输入框'}」输入：***")
            else:
                out.append(f"在「{selector or '输入框'}」输入：{v}")
            continue
        if action == "select":
            out.append(f"在「{selector or '下拉框'}」选择：{value}")
            continue
        if action == "press":
            out.append(f"按键：{value}")
            continue
        if action == "wait":
            ms = value.strip()
            out.append(f"等待 {ms}ms" if ms else "等待")
            continue
        if action == "scroll":
            out.append("滚动页面")
            continue
        meta = s.get("meta")
        if meta and isinstance(meta, dict):
            meta_s = json.dumps(meta, ensure_ascii=False)[:200]
            out.append(f"备注：未支持动作 {action}（{meta_s}）")
        else:
            out.append(f"备注：未支持动作 {action}")
    return [x for x in out if str(x).strip()]


@login_required
def recorder_import_page(request):
    projects = visible_projects(request.user)
    requirements = Requirement.objects.filter(project__in=projects)
    try:
        project_id = int(request.GET.get("project_id") or 0)
    except Exception:
        project_id = 0
    if not project_id:
        p0 = projects.order_by("id").first()
        project_id = int(getattr(p0, "id", 0) or 0)
    default_title = "录制用例-" + timezone.now().strftime("%Y%m%d_%H%M%S")
    return render(
        request,
        "autotest/recorder_import.html",
        {"projects": projects, "requirements": requirements, "project_id": project_id, "default_title": default_title},
    )


@login_required
@require_POST
def recorder_import_submit(request):
    projects = visible_projects(request.user)
    requirements = Requirement.objects.filter(project__in=projects)
    try:
        project_id = int(request.POST.get("project_id") or 0)
    except Exception:
        project_id = 0
    if project_id <= 0:
        messages.error(request, "请选择项目")
        return redirect("recorder_import_page")
    try:
        requirement_id = int(request.POST.get("requirement_id") or 0)
    except Exception:
        requirement_id = 0
    title = str(request.POST.get("title") or "").strip()[:200]
    payload = str(request.POST.get("payload") or "")
    steps = _qa_recorder_to_steps(payload)
    if not title:
        title = "录制用例-" + timezone.now().strftime("%Y%m%d_%H%M%S")
    if not steps:
        messages.error(request, "录制 JSON 无法解析或没有步骤")
        return render(
            request,
            "autotest/recorder_import.html",
            {
                "projects": projects,
                "requirements": requirements,
                "project_id": project_id,
                "default_title": title,
            },
        )
    project = get_object_or_404(projects, pk=project_id)
    requirement = None
    if requirement_id:
        requirement = requirements.filter(id=requirement_id).first()
    case = TestCase.objects.create(
        project=project,
        requirement=requirement,
        title=title,
        type=1,
        execution_type=2,
        priority=2,
        creator=request.user,
    )
    for i, step_desc in enumerate(steps, start=1):
        TestCaseStep.objects.create(
            case=case,
            step_number=i,
            description=str(step_desc or ""),
            expected_result="AI自动验证",
            is_executed=False,
        )
    messages.success(request, f"已导入 {len(steps)} 步：{title}")
    return redirect("case_detail", pk=case.id)


@csrf_exempt
@require_POST
def ci_trigger(request):
    if not _ci_token_ok(request):
        return JsonResponse({"success": False, "error": "unauthorized"}, status=403)
    try:
        payload = json.loads(request.body or "{}")
    except Exception:
        payload = {}

    schedule_id = int(payload.get("schedule_id") or 0)
    case_ids = payload.get("case_ids") or []
    executor_username = (payload.get("executor") or "").strip()
    ci_user = _ci_user_from_token(request)

    schedule = None
    if schedule_id > 0:
        if ci_user is not None:
            schedule = get_object_or_404(AutoTestSchedule, id=schedule_id, created_by=ci_user)
        else:
            schedule = get_object_or_404(AutoTestSchedule, id=schedule_id)
        case_ids = schedule.case_ids if isinstance(schedule.case_ids, list) else []

    ids = []
    if isinstance(case_ids, list):
        for x in case_ids:
            try:
                ids.append(int(x))
            except Exception:
                pass
    ids = [x for x in ids if x > 0]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return JsonResponse({"success": False, "error": "empty case_ids"}, status=400)

    executor = None
    if executor_username:
        try:
            executor = User.objects.filter(username=executor_username).first()
        except Exception:
            executor = None
    if not executor and schedule and schedule.created_by_id:
        executor = schedule.created_by
    if not executor and ci_user is not None:
        executor = ci_user
    if not executor:
        executor = User.objects.filter(is_superuser=True).order_by("id").first() or User.objects.filter(is_staff=True).order_by("id").first()

    created_ids = []
    cases = list(TestCase.objects.filter(id__in=ids))
    for case in cases:
        if getattr(case, "case_mode", "normal") == "advanced":
            params = getattr(case, "parameters", {}) or {}
            datasets = params.get("datasets") if isinstance(params, dict) else None
            if not isinstance(datasets, list) or not datasets:
                continue
            try:
                max_runs = int((params.get("max_runs") if isinstance(params, dict) else None) or 0)
            except Exception:
                max_runs = 0
            if max_runs <= 0:
                max_runs = 10
            batch_id = uuid.uuid4()
            expanded = []
            for ds in datasets:
                expanded.extend(expand_dataset(ds, max_runs=max_runs))
                if len(expanded) >= max_runs:
                    expanded = expanded[:max_runs]
                    break
            run_total = min(len(expanded), max_runs)
            for idx, ds in enumerate(expanded[:run_total]):
                if not isinstance(ds, dict):
                    continue
                name = str(ds.get("name") or f"数据集{idx+1}")[:120]
                vars_obj = ds.get("vars") or {}
                if not isinstance(vars_obj, dict):
                    vars_obj = {}
                ex = AutoTestExecution.objects.create(
                    case=case,
                    executor=executor,
                    status="pending",
                    batch_id=batch_id,
                    run_index=idx + 1,
                    run_total=run_total,
                    dataset_name=name,
                    dataset_vars=vars_obj,
                    trigger_source="ci",
                    trigger_payload={"schedule_id": schedule_id, "case_id": int(case.id)},
                    schedule=schedule,
                )
                enqueue_execution(ex.id)
                created_ids.append(ex.id)
        else:
            ex = AutoTestExecution.objects.create(
                case=case,
                executor=executor,
                status="pending",
                trigger_source="ci",
                trigger_payload={"schedule_id": schedule_id, "case_id": int(case.id)},
                schedule=schedule,
            )
            enqueue_execution(ex.id)
            created_ids.append(ex.id)

    if not created_ids:
        return JsonResponse({"success": False, "error": "no executions created"}, status=400)
    return JsonResponse({"success": True, "execution_ids": created_ids})


@csrf_exempt
@require_GET
def ci_status(request, execution_id: int):
    if not _ci_token_ok(request):
        return JsonResponse({"success": False, "error": "unauthorized"}, status=403)
    execution = get_object_or_404(AutoTestExecution, id=int(execution_id))
    summary = execution.result_summary or {}
    return JsonResponse(
        {
            "success": True,
            "id": execution.id,
            "case_id": int(getattr(execution.case, "id", 0) or 0),
            "status": execution.status,
            "start_time": execution.start_time.isoformat() if execution.start_time else None,
            "end_time": execution.end_time.isoformat() if execution.end_time else None,
            "result_summary": summary,
        }
    )
