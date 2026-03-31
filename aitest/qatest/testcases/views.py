from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import TestCase, TestCaseStep
from projects.models import Project
from requirements.models import Requirement
from django import forms
from django.db.models import Q, Exists, OuterRef
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.forms import inlineformset_factory
from django.http import JsonResponse, StreamingHttpResponse, HttpResponse
from django.http import HttpResponseForbidden
from django.urls import reverse
from django.utils import timezone
import asyncio
import json
import re
from django.views.decorators.csrf import csrf_exempt
from autotest.models import AutoTestExecution
from asgiref.sync import async_to_sync
from ai_assistant.utils.llms import get_model_client
from users.ai_config import AIKeyNotConfigured, resolve_testcase_params
try:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.messages import ModelClientStreamingChunkEvent
    from autogen_agentchat.base import TaskResult
except Exception:
    AssistantAgent = None
    ModelClientStreamingChunkEvent = None
    TaskResult = None
from django.core.files.uploadedfile import UploadedFile

from core.visibility import visible_projects, is_admin_user
from core.pagination import paginate
try:
    from .debug_utils import log_debug
except ImportError:
    log_debug = lambda x: None

class TestCaseForm(forms.ModelForm):
    class Meta:
        model = TestCase
        fields = ['project', 'requirement', 'title', 'pre_condition', 'type', 'execution_type', 'priority', 'status', 'case_mode', 'parameters']
        widgets = {
            "pre_condition": forms.Textarea(attrs={"rows": 3}),
            "parameters": forms.Textarea(attrs={"rows": 10}),
        }

    def clean(self):
        cleaned = super().clean()
        case_mode = (cleaned.get("case_mode") or "normal").strip().lower()
        params = cleaned.get("parameters") or {}
        if case_mode not in ("normal", "advanced"):
            self.add_error("case_mode", "用例模式不合法")
            return cleaned
        if case_mode == "advanced":
            if params is None:
                params = {}
            if not isinstance(params, dict):
                self.add_error("parameters", "参数集必须是 JSON 对象")
                return cleaned
            datasets = params.get("datasets") or []
            if not isinstance(datasets, list):
                self.add_error("parameters", "datasets 必须是数组")
                return cleaned
            for i, ds in enumerate(datasets[:200]):
                if not isinstance(ds, dict):
                    self.add_error("parameters", f"datasets[{i}] 必须是对象")
                    return cleaned
                vars_obj = ds.get("vars") or {}
                if not isinstance(vars_obj, dict):
                    self.add_error("parameters", f"datasets[{i}].vars 必须是对象")
                    return cleaned
        else:
            cleaned["parameters"] = {}
        return cleaned

class TestCaseStepForm(forms.ModelForm):
    guide_image_clear = forms.BooleanField(
        required=False,
        label="删除截图",
        widget=forms.HiddenInput(),
    )
    transfer_file_clear = forms.BooleanField(
        required=False,
        label="删除传输文件",
        widget=forms.HiddenInput(),
    )
    transfer_file_upload = forms.FileField(
        required=False,
        label="传输文件",
        widget=forms.FileInput(attrs={"class": "form-control form-control-sm transfer-file-input"}),
    )

    class Meta:
        model = TestCaseStep
        fields = ["step_number", "description", "expected_result", "guide_image", "smart_data_enabled"]
        widgets = {
            "step_number": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 0, "readonly": "readonly"}),
            "description": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "步骤描述"}),
            "expected_result": forms.Textarea(attrs={"class": "form-control form-control-sm", "rows": 2, "placeholder": "预期结果"}),
            "guide_image": forms.FileInput(attrs={"class": "form-control form-control-sm guide-input", "accept": "image/*"}),
            "smart_data_enabled": forms.CheckboxInput(attrs={"class": "form-check-input smart-switch"}),
        }

    def clean(self):
        cleaned = super().clean()
        clear = bool(cleaned.get("guide_image_clear"))
        new_file = cleaned.get("guide_image")
        if clear and not new_file:
            cleaned["guide_image"] = None
            log_debug(f"Form clean: guide_image cleared for step {self.instance.pk if self.instance else 'new'}")
        if bool(cleaned.get("transfer_file_clear")) and not cleaned.get("transfer_file_upload"):
            cleaned["transfer_file_upload"] = None
            log_debug(f"Form clean: transfer file cleared for step {self.instance.pk if self.instance else 'new'}")
        return cleaned

    def has_changed(self) -> bool:
        # Check if clear checkbox was changed (important for deletion)
        clear_key = self.add_prefix("guide_image_clear")
        transfer_clear_key = self.add_prefix("transfer_file_clear")
        clear_changed = bool(self.data.get(clear_key))
        transfer_clear_changed = bool(self.data.get(transfer_clear_key))
        if clear_changed or transfer_clear_changed:
            log_debug(f"has_changed: {clear_key}={self.data.get(clear_key)}, {transfer_clear_key}={self.data.get(transfer_clear_key)}")
            return True
            
        if not super().has_changed():
            return False
        if getattr(self.instance, "pk", None):
            return True
        try:
            desc = (self.data.get(self.add_prefix("description")) or "").strip()
        except Exception:
            desc = ""
        try:
            expected = (self.data.get(self.add_prefix("expected_result")) or "").strip()
        except Exception:
            expected = ""
        try:
            guide = self.files.get(self.add_prefix("guide_image"))
        except Exception:
            guide = None
        try:
            transfer = self.files.get(self.add_prefix("transfer_file_upload"))
        except Exception:
            transfer = None
        if desc or expected or guide or transfer:
            return True
        return not set(self.changed_data or []).issubset({"step_number"})

    def save(self, commit=True):
        instance = super().save(commit=False)
        img = self.cleaned_data.get("guide_image")
        if img:
            try:
                if hasattr(img, "read"):
                    import base64
                    img.seek(0)
                    data = img.read()
                    b64 = base64.b64encode(data).decode("utf-8")
                    instance.guide_image_base64 = b64
                    instance.guide_image_content_type = str(getattr(img, "content_type", "") or "")[:120]
                    # IMPORTANT: Reset pointer so ImageField can save it to disk too if needed
                    img.seek(0)
                    log_debug(f"Converted image to base64, len={len(b64)}")
            except Exception as e:
                log_debug(f"Error converting image to base64: {e}")
                print(f"Error converting image to base64: {e}")
        elif self.cleaned_data.get("guide_image_clear"):
            # Clear both ImageField and base64 fields
            instance.guide_image = None
            instance.guide_image_base64 = ""
            instance.guide_image_content_type = ""
            log_debug(f"Clearing guide image for step {instance.pk}")

        f = self.cleaned_data.get("transfer_file_upload")
        if f:
            try:
                import base64
                f.seek(0)
                raw = f.read()
                instance.transfer_file_base64 = base64.b64encode(raw).decode("utf-8")
                instance.transfer_file_name = str(getattr(f, "name", "") or "")[:255]
                instance.transfer_file_content_type = str(getattr(f, "content_type", "") or "")[:120]
                try:
                    instance.transfer_file_size = int(getattr(f, "size", 0) or 0)
                except Exception:
                    instance.transfer_file_size = 0
                f.seek(0)
            except Exception as e:
                log_debug(f"Error converting transfer file to base64: {e}")
        elif self.cleaned_data.get("transfer_file_clear"):
            instance.transfer_file_base64 = ""
            instance.transfer_file_name = ""
            instance.transfer_file_content_type = ""
            instance.transfer_file_size = 0
            
        if commit:
            instance.save()
        return instance

TestCaseStepFormSet = inlineformset_factory(
    TestCase, TestCaseStep,
    form=TestCaseStepForm,
    fields=['step_number', 'description', 'expected_result', 'guide_image', 'guide_image_clear', 'transfer_file_upload', 'transfer_file_clear', 'smart_data_enabled'],
    extra=1,
    can_delete=True
)

def _normalize_step_numbers(formset):
    candidates = []
    for i, f in enumerate(list(getattr(formset, "forms", []) or [])):
        try:
            cd = getattr(f, "cleaned_data", None) or {}
        except Exception:
            cd = {}
        if cd.get("DELETE"):
            continue
        if (not getattr(getattr(f, "instance", None), "pk", None)) and (not f.has_changed()):
            continue
        try:
            raw_no = int(cd.get("step_number") or 0)
        except Exception:
            raw_no = 0
        if raw_no <= 0:
            raw_no = 10**9 + i
        candidates.append((raw_no, i, f))
    candidates.sort(key=lambda x: (x[0], x[1]))
    for idx, (_raw, _i, f) in enumerate(candidates, start=1):
        try:
            f.instance.step_number = int(idx)
        except Exception:
            f.instance.step_number = idx

@login_required
def case_list(request):
    ai_exec_exists = AutoTestExecution.objects.filter(case_id=OuterRef("pk"))
    cases = (
        TestCase.objects.filter(project__in=visible_projects(request.user))
        .annotate(has_ai_report=Exists(ai_exec_exists))
        .order_by("-created_at")
    )
    projects = visible_projects(request.user)
    
    # Filter
    keyword = request.GET.get('keyword', '')
    project_id = request.GET.get('project', '')
    requirement_id = request.GET.get('requirement', '')
    date_start = request.GET.get('date_start', '')
    date_end = request.GET.get('date_end', '')
    
    # Fetch requirements filtered by selected project if project is selected
    if project_id:
        if projects.filter(id=project_id).exists():
            requirements = Requirement.objects.filter(project_id=project_id)
        else:
            requirements = Requirement.objects.none()
    else:
        requirements = Requirement.objects.filter(project__in=projects)

    if keyword:
        if keyword.isdigit():
            cases = cases.filter(Q(id=keyword) | Q(title__icontains=keyword))
        else:
            cases = cases.filter(title__icontains=keyword)
    
    if project_id:
        cases = cases.filter(project_id=project_id)
        
    if requirement_id:
        cases = cases.filter(requirement_id=requirement_id)
            
    if date_start:
        cases = cases.filter(created_at__gte=date_start)
    if date_end:
        cases = cases.filter(created_at__lte=date_end)

    # Stats
    total = cases.count()
    my_cases = cases.filter(creator=request.user).count()
    # Assuming 'Not Executed' is 0
    not_executed = cases.filter(status=0).count()

    pg = paginate(request, cases, per_page=20)
    context = {
        'cases': pg.page_obj,
        'page_obj': pg.page_obj,
        'paginator': pg.paginator,
        'is_paginated': pg.is_paginated,
        'page_range': pg.page_range,
        'projects': projects,
        'requirements': requirements,
        'total': total,
        'my_cases': my_cases,
        'not_executed': not_executed,
    }
    return render(request, 'testcases/case_list.html', context)

@login_required
def case_create(request):
    if request.method == 'POST':
        log_debug(f"CREATE POST: FILES={request.FILES.keys()}")
        form = TestCaseForm(request.POST)
        formset = TestCaseStepFormSet(request.POST, request.FILES)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["requirement"].queryset = Requirement.objects.filter(project__in=visible_projects(request.user))
        if form.is_valid() and formset.is_valid():
            case = form.save(commit=False)
            case.creator = request.user
            case.save()
            formset.instance = case
            _normalize_step_numbers(formset)
            formset.save()
            log_debug("CREATE SUCCESS")
            return redirect('case_list')
        else:
            log_debug(f"CREATE ERROR: form={form.errors} formset={formset.errors}")
    else:
        form = TestCaseForm()
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["requirement"].queryset = Requirement.objects.filter(project__in=visible_projects(request.user))
        formset = TestCaseStepFormSet()
    return render(request, 'testcases/case_form.html', {'form': form, 'formset': formset, 'title': '创建用例'})

@login_required
def case_detail(request, pk):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    return render(request, 'testcases/case_detail.html', {'case': case})

@login_required
def case_edit(request, pk):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    can_edit = is_admin_user(request.user) or case.creator_id == request.user.id or case.project.owner_id == request.user.id
    if not can_edit:
        return HttpResponseForbidden("无权限编辑该用例")
    if request.method == 'POST':
        log_debug(f"EDIT POST: FILES={request.FILES.keys()}")
        # Debug: log POST data for guide_image_clear
        for key in request.POST.keys():
            if 'guide_image_clear' in key or 'transfer_file_clear' in key:
                log_debug(f"POST key: {key} = {request.POST[key]}")
        form = TestCaseForm(request.POST, instance=case)
        formset = TestCaseStepFormSet(request.POST, request.FILES, instance=case)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["requirement"].queryset = Requirement.objects.filter(project__in=visible_projects(request.user))
        if form.is_valid() and formset.is_valid():
            form.save()
            # Debug: log formset forms data
            for i, form_sf in enumerate(formset.forms):
                if hasattr(form_sf.instance, 'pk') and form_sf.instance.pk:
                    log_debug(f"Formset form {i} (step {form_sf.instance.pk}): has_changed={form_sf.has_changed()}")
                    log_debug(f"  guide_image_clear in data={form_sf.data.get(f'steps-{i}-guide_image_clear', 'NOT SET')}")
                    log_debug(f"  cleaned guide_image_clear={form_sf.cleaned_data.get('guide_image_clear') if form_sf.is_valid() else 'N/A'}")
                    if form_sf.cleaned_data.get('guide_image_clear'):
                        log_debug(f"  >>> WILL CLEAR guide_image for step {form_sf.instance.pk}")
            _normalize_step_numbers(formset)
            # Use formset.save() which handles validation properly
            # But first ensure forms with clear fields are marked as changed
            formset.save()
            log_debug(f"EDIT SUCCESS")
            # Verify after save
            for i, form_sf in enumerate(formset.forms):
                if hasattr(form_sf.instance, 'pk') and form_sf.instance.pk:
                    form_sf.instance.refresh_from_db()
                    log_debug(f"After save - Step {form_sf.instance.pk}: guide_image_base64={bool(form_sf.instance.guide_image_base64)}, guide_image={form_sf.instance.guide_image}")
            return redirect('case_detail', pk=pk)
        else:
            log_debug(f"EDIT ERROR: form={form.errors} formset={formset.errors}")
            # Debug: log detailed formset errors
            for i, form_sf in enumerate(formset.forms):
                if form_sf.errors:
                    log_debug(f"Form {i} errors: {form_sf.errors}")
    else:
        form = TestCaseForm(instance=case)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["requirement"].queryset = Requirement.objects.filter(project__in=visible_projects(request.user))
        formset = TestCaseStepFormSet(instance=case)
    return render(request, 'testcases/case_form.html', {'form': form, 'formset': formset, 'title': '编辑用例'})

@csrf_exempt
@login_required
@require_POST
def case_copy(request, pk):
    try:
        src = TestCase.objects.filter(project__in=visible_projects(request.user)).prefetch_related("steps").get(pk=pk)
    except TestCase.DoesNotExist:
        return JsonResponse({"success": False, "error": "Case not found"}, status=404)

    base_title = str(src.title or "").strip()[:180] or "未命名用例"
    new_title = f"{base_title} - 副本"
    try:
        if TestCase.objects.filter(project_id=src.project_id, title=new_title).exists():
            new_title = f"{base_title} - 副本 {timezone.now().strftime('%m%d%H%M%S')}"[:200]
    except Exception:
        new_title = new_title[:200]

    dst = TestCase.objects.create(
        project_id=src.project_id,
        requirement_id=src.requirement_id,
        title=new_title[:200],
        pre_condition=src.pre_condition or "",
        type=src.type,
        execution_type=src.execution_type,
        priority=src.priority,
        status=0,
        creator=request.user,
        case_mode=getattr(src, "case_mode", "normal") or "normal",
        parameters=getattr(src, "parameters", {}) or {},
    )
    for s in (src.steps.all().order_by("step_number")):
        TestCaseStep.objects.create(
            case=dst,
            step_number=int(getattr(s, "step_number", 0) or 0),
            description=str(getattr(s, "description", "") or ""),
            expected_result=str(getattr(s, "expected_result", "") or ""),
            smart_data_enabled=bool(getattr(s, "smart_data_enabled", False)),
            guide_image=None,
            guide_image_base64=str(getattr(s, "guide_image_base64", "") or ""),
            transfer_file_name=str(getattr(s, "transfer_file_name", "") or "")[:255],
            transfer_file_content_type=str(getattr(s, "transfer_file_content_type", "") or "")[:120],
            transfer_file_size=int(getattr(s, "transfer_file_size", 0) or 0),
            transfer_file_base64=str(getattr(s, "transfer_file_base64", "") or ""),
            is_executed=False,
        )
    return JsonResponse({"success": True, "new_id": dst.id, "redirect_url": reverse("case_edit", kwargs={"pk": dst.id})})

@login_required
def delete_case(request, pk):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    case.delete()
    messages.success(request, '用例已成功删除。')
    return redirect('case_list')

@login_required
@require_POST
def case_batch_delete(request):
    case_ids = request.POST.getlist('case_ids')
    if case_ids:
        TestCase.objects.filter(project__in=visible_projects(request.user), id__in=case_ids).delete()
        messages.success(request, f'成功删除了 {len(case_ids)} 个用例。')
    else:
        messages.warning(request, '未选择任何用例。')
    return redirect('case_list')

@csrf_exempt
@login_required
@require_POST
def update_step(request, step_id):
    try:
        step = TestCaseStep.objects.select_related("case", "case__project").get(id=step_id, case__project__in=visible_projects(request.user))
        data = json.loads(request.body)
        
        # Update fields if provided
        if 'description' in data:
            step.description = data['description']
        if 'expected_result' in data:
            step.expected_result = data['expected_result']
        if 'is_executed' in data:
            step.is_executed = data['is_executed']
        if 'smart_data_enabled' in data:
            step.smart_data_enabled = bool(data['smart_data_enabled'])
            
        step.save()
        return JsonResponse({'success': True})
    except TestCaseStep.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Step not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def upload_step_guide(request, step_id):
    try:
        step = TestCaseStep.objects.select_related("case", "case__project").get(
            id=step_id,
            case__project__in=visible_projects(request.user),
        )
        img: UploadedFile | None = request.FILES.get("guide_image")  # type: ignore[assignment]
        if not img:
            return JsonResponse({"success": False, "error": "未收到图片文件"}, status=400)
        if getattr(img, "size", 0) and int(img.size) > 3 * 1024 * 1024:
            return JsonResponse({"success": False, "error": "图片过大（最大 3MB）"}, status=400)

        step.guide_image = img
        try:
            import base64
            img.seek(0)
            raw = img.read()
            step.guide_image_base64 = base64.b64encode(raw).decode("utf-8")
            step.guide_image_content_type = str(getattr(img, "content_type", "") or "")[:120]
            img.seek(0)
        except Exception as e:
            log_debug(f"UPLOAD GUIDE base64 error: {e}")

        step.save()
        data_uri = ""
        try:
            content_type = step.guide_image_content_type or getattr(img, "content_type", "") or "image/png"
            if step.guide_image_base64:
                data_uri = f"data:{content_type};base64,{step.guide_image_base64}"
        except Exception:
            data_uri = ""
        return JsonResponse(
            {
                "success": True,
                "step_id": step.id,
                "data_uri": data_uri,
                "url": getattr(getattr(step, "guide_image", None), "url", "") or "",
            }
        )
    except TestCaseStep.DoesNotExist:
        return JsonResponse({"success": False, "error": "Step not found"}, status=404)
    except Exception as e:
        log_debug(f"UPLOAD GUIDE error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def upload_step_transfer_file(request, step_id):
    try:
        step = TestCaseStep.objects.select_related("case", "case__project").get(
            id=step_id,
            case__project__in=visible_projects(request.user),
        )
        f: UploadedFile | None = request.FILES.get("transfer_file")  # type: ignore[assignment]
        if not f:
            return JsonResponse({"success": False, "error": "未收到文件"}, status=400)
        if getattr(f, "size", 0) and int(f.size) > 10 * 1024 * 1024:
            return JsonResponse({"success": False, "error": "文件过大（最大 10MB）"}, status=400)

        try:
            import base64
            f.seek(0)
            raw = f.read()
            step.transfer_file_base64 = base64.b64encode(raw).decode("utf-8")
            step.transfer_file_name = str(getattr(f, "name", "") or "")[:255]
            step.transfer_file_content_type = str(getattr(f, "content_type", "") or "")[:120]
            try:
                step.transfer_file_size = int(getattr(f, "size", 0) or 0)
            except Exception:
                step.transfer_file_size = 0
            f.seek(0)
        except Exception as e:
            log_debug(f"UPLOAD TRANSFER base64 error: {e}")
            return JsonResponse({"success": False, "error": "文件读取失败"}, status=500)

        step.save(update_fields=["transfer_file_base64", "transfer_file_name", "transfer_file_content_type", "transfer_file_size"])
        return JsonResponse(
            {
                "success": True,
                "step_id": step.id,
                "file_name": step.transfer_file_name,
                "size": int(step.transfer_file_size or 0),
                "content_type": step.transfer_file_content_type,
            }
        )
    except TestCaseStep.DoesNotExist:
        return JsonResponse({"success": False, "error": "Step not found"}, status=404)
    except Exception as e:
        log_debug(f"UPLOAD TRANSFER error: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
@login_required
@require_POST
def add_step(request, case_id):
    try:
        case = TestCase.objects.select_related("project").get(id=case_id, project__in=visible_projects(request.user))
        # Get next step number
        last_step = case.steps.order_by('-step_number').first()
        next_number = (last_step.step_number + 1) if last_step else 1
        
        step = TestCaseStep.objects.create(
            case=case,
            step_number=next_number,
            description="新步骤",
            expected_result="预期结果",
            smart_data_enabled=False,
            is_executed=False
        )
        
        return JsonResponse({
            'success': True,
            'step': {
                'id': step.id,
                'step_number': step.step_number,
                'description': step.description,
                'expected_result': step.expected_result,
                'smart_data_enabled': step.smart_data_enabled,
                'is_executed': step.is_executed
            }
        })
    except TestCase.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Case not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
@login_required
@require_POST
def delete_step(request, step_id):
    try:
        step = TestCaseStep.objects.select_related("case", "case__project").get(
            id=step_id, case__project__in=visible_projects(request.user)
        )
        can_edit = is_admin_user(request.user) or step.case.creator_id == request.user.id or step.case.project.owner_id == request.user.id
        if not can_edit:
            return JsonResponse({"success": False, "error": "无权限删除该步骤"}, status=403)
        step.delete()
        # Reorder remaining steps
        case_steps = TestCaseStep.objects.filter(case=step.case).order_by('step_number')
        for index, s in enumerate(case_steps):
            s.step_number = index + 1
            s.save()
            
        return JsonResponse({'success': True})
    except TestCaseStep.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Step not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def case_convert_advanced(request, pk):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    can_edit = is_admin_user(request.user) or case.creator_id == request.user.id or case.project.owner_id == request.user.id
    if not can_edit:
        return HttpResponseForbidden("无权限编辑该用例")
    if case.case_mode != "advanced":
        case.case_mode = "advanced"
        params = case.parameters if isinstance(case.parameters, dict) else {}
        datasets = params.get("datasets") if isinstance(params, dict) else None
        if not isinstance(datasets, list) or not datasets:
            params["datasets"] = [{"name": "数据集1", "vars": {}}]
        if not isinstance(params.get("max_runs"), int):
            params["max_runs"] = 10
        if "stop_on_fail" not in params:
            params["stop_on_fail"] = False
        case.parameters = params
        case.save(update_fields=["case_mode", "parameters"])
    return redirect('case_edit', pk=pk)


def _build_expand_prompt(base_case: TestCase, base_steps: list[dict]) -> str:
    return (
        "请基于下面的“基础用例”进行扩写，生成更多测试用例。\n"
        "扩写方法：等价类划分、边界值分析、场景法。\n"
        "数量：由你自行判断（不少于 5 条，不多于 12 条）。\n"
        "不要重复输出基础用例本身，只输出新增的扩写用例。\n"
        "重要：请严格按照本系统可解析的 Markdown 用例格式输出（不要输出任何解释文字）。\n"
        "格式要求：\n"
        "1) 每条用例以二级标题开始：## TC-001: 用户登录功能测试\n"
        "2) 必须包含字段（加粗）：**优先级:** 高/中/低；**描述:** ...；**前置条件:** ...\n"
        "3) 测试步骤必须使用标准 Markdown 表格（可加上 ### 测试步骤 标题）：\n"
        "| # | 步骤描述 | 预期结果 |\n"
        "| --- | --- | --- |\n"
        "| 1 | ... | ... |\n\n"
        "基础用例：\n"
        f"- 标题：{base_case.title}\n"
        f"- 前置条件：{base_case.pre_condition or ''}\n"
        f"- 步骤：{json.dumps(base_steps, ensure_ascii=False)}\n"
    )


def _expand_system_message() -> str:
    return (
        "你是一个专业的测试用例生成器，擅长对既有用例进行扩写。\n"
        "关键要求：必须严格按指定的 Markdown 用例格式输出，不要输出任何解释或额外文字。"
    )


def _parse_cases_from_markdown(markdown_text: str) -> list[dict]:
    cases: list[dict] = []
    lines = markdown_text.splitlines()
    current: dict | None = None
    in_table = False

    def save():
        nonlocal current
        if not current:
            return
        if current.get("title") and current.get("steps_list"):
            cases.append(current)
        current = None

    for raw_line in lines:
        line = raw_line.strip()
        m = re.match(r"^#{0,6}\s*\*?TC-([\w-]+)[:：]?\s*(.*?)\*?$", line, flags=re.I)
        if m:
            if current:
                save()
            current = {
                "title": f"TC-{m.group(1)}: {m.group(2)}",
                "pre_condition": "",
                "priority_text": "",
                "description_text": "",
                "steps_list": [],
            }
            in_table = False
            continue

        if not current:
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*\*\s*优先级\s*[:：]\s*\*\*\s*(.*)\s*$", line)
        if not m_field:
            m_field = re.match(r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*优先级\s*(?:\*\*)?\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["priority_text"] = (m_field.group(1) or "").strip()
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*\*\s*描述\s*[:：]\s*\*\*\s*(.*)\s*$", line)
        if not m_field:
            m_field = re.match(r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*描述\s*(?:\*\*)?\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["description_text"] = (m_field.group(1) or "").strip()
            continue

        m_field = re.match(r"^\s*(?:[-*]\s*)?\*\*\s*前置条件\s*[:：]\s*\*\*\s*(.*)\s*$", line)
        if not m_field:
            m_field = re.match(r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*前置条件\s*(?:\*\*)?\s*[:：]\s*(.*)\s*$", line)
        if m_field:
            current["pre_condition"] = (m_field.group(1) or "").strip()
            continue

        if re.match(r"^\s*\|?[\s\-:]+\|[\s\-:]+", line):
            in_table = True
            continue

        if in_table:
            if "|" not in line:
                if line != "":
                    in_table = False
                continue

            if (("步骤" in line) or ("Step" in line)) and (("预期" in line) or ("Result" in line)):
                continue

            cols = [c.strip() for c in line.split("|")]
            if cols and cols[0] == "":
                cols = cols[1:]
            if cols and cols[-1] == "":
                cols = cols[:-1]

            if len(cols) >= 2:
                if len(cols) >= 3:
                    step = cols[1]
                    result = cols[2]
                else:
                    step = cols[0]
                    result = cols[1]
                if step.strip():
                    current["steps_list"].append({"description": step.strip(), "expected_result": result.strip()})

    if current:
        save()
    return cases


@login_required
@require_POST
def expand_generate(request):
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    case_id = data.get("case_id")
    if not str(case_id).isdigit():
        return JsonResponse({"success": False, "error": "请选择一条用例"}, status=400)

    base_case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), id=int(case_id))
    try:
        resolve_testcase_params(request.user)
    except AIKeyNotConfigured as e:
        return JsonResponse({"success": False, "error": str(e) or "请先在个人中心配置用例生成 API Key"}, status=400)
    if AssistantAgent is None:
        return JsonResponse({"success": False, "error": "扩写功能依赖的 AI 组件未安装或导入失败（autogen-agentchat）"}, status=500)
    try:
        model_client = get_model_client(user=request.user)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e) or "AI 客户端初始化失败，请检查 API Key/模型配置"}, status=400)
    base_steps = list(
        base_case.steps.all()
        .order_by("step_number")
        .values("step_number", "description", "expected_result")
    )
    prompt = _build_expand_prompt(base_case, base_steps)
    system_message = _expand_system_message()

    async def stream_generator():
        try:
            agent = AssistantAgent(
                name="expand_case_agent",
                model_client=model_client,
                system_message=system_message,
                model_client_stream=True,
            )
            async for event in agent.run_stream(task=prompt):
                if isinstance(event, ModelClientStreamingChunkEvent):
                    yield event.content
                elif isinstance(event, TaskResult):
                    break
        except Exception as e:
            msg = str(e) or "unknown"
            msg = re.sub(r"\s+", " ", msg).strip()[:300]
            yield f"\n\n[ERROR] 扩写生成失败：{msg}\n"

    def sync_stream_generator():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        gen = stream_generator()
        try:
            while True:
                try:
                    chunk = loop.run_until_complete(gen.__anext__())
                    yield chunk
                except StopAsyncIteration:
                    break
                except Exception as e:
                    msg = str(e) or "unknown"
                    msg = re.sub(r"\s+", " ", msg).strip()[:300]
                    yield f"\n\n[ERROR] 扩写生成失败：{msg}\n"
                    break
        finally:
            loop.close()

    return StreamingHttpResponse(sync_stream_generator(), content_type="text/plain; charset=utf-8")


@login_required
@require_POST
def expand_import(request):
    try:
        data = json.loads(request.body or "{}")
        case_id = data.get("case_id")
        markdown = data.get("markdown") or ""

        if not str(case_id).isdigit():
            return JsonResponse({"success": False, "error": "请选择一条用例"}, status=400)
        if not markdown.strip():
            return JsonResponse({"success": False, "error": "扩写内容为空"}, status=400)

        base_case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), id=int(case_id))
        generated_cases = _parse_cases_from_markdown(markdown)
        if not generated_cases:
            return JsonResponse({"success": False, "error": "未解析到有效用例，请检查格式"}, status=400)

        priority_map = {"高": 1, "中": 2, "低": 3}
        created_ids = []
        created_count = 0
        used_titles = set()
        for item in generated_cases:
            title = (item.get("title") or "").strip()
            pre_condition = item.get("pre_condition") or ""
            description_text = (item.get("description_text") or "").strip()
            if description_text:
                description_text = re.sub(r"^[\s*+\-]+", "", description_text).strip()
            priority_text = (item.get("priority_text") or "中").strip()
            priority = priority_map.get(priority_text, 2)
            steps_list = item.get("steps_list") or []
            if not title or not steps_list:
                continue
            if title.strip() == (base_case.title or "").strip():
                continue

            try:
                m = re.match(r"^\s*(TC[-_ ]?\d+)\s*[:：]\s*(.*)\s*$", title, flags=re.IGNORECASE)
            except Exception:
                m = None
            if m:
                tc_code = (m.group(1) or "").strip().upper().replace(" ", "")
                tc_name = (m.group(2) or "").strip()
            else:
                tc_code = ""
                tc_name = title

            if description_text:
                tc_name = description_text
                title = f"{tc_code}: {tc_name}" if tc_code else tc_name
            else:
                normalized_name = re.sub(r"\s+", "", (tc_name or ""))
                if (not normalized_name) or normalized_name in ("测试标题", "用例标题", "标题", "测试用例"):
                    suffix = f"扩写场景{created_count + 1}"
                    base_name = (base_case.title or "扩写用例").strip()
                    tc_name = f"{base_name} - {suffix}"
                    title = f"{tc_code}: {tc_name}" if tc_code else tc_name

            title = title.strip()[:200]
            if title in used_titles:
                title = f"{title} ({created_count + 1})"[:200]
            used_titles.add(title)

            test_case = TestCase.objects.create(
                project=base_case.project,
                requirement=base_case.requirement,
                title=title,
                pre_condition=pre_condition,
                type=base_case.type,
                execution_type=base_case.execution_type,
                priority=priority,
                status=0,
                creator=request.user,
            )

            step_number = 1
            for step in steps_list:
                desc = (step.get("description") or "").strip()
                exp = (step.get("expected_result") or "").strip()
                if not desc:
                    continue
                TestCaseStep.objects.create(
                    case=test_case,
                    step_number=step_number,
                    description=desc,
                    expected_result=exp,
                    smart_data_enabled=False,
                    is_executed=False,
                )
                step_number += 1

            created_ids.append(test_case.id)
            created_count += 1

        return JsonResponse(
            {"success": True, "created_count": created_count, "created_case_ids": created_ids}
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
