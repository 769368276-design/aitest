from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Requirement
from projects.models import Project
from django import forms
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.contrib import messages

from core.visibility import visible_projects, is_admin_user
from core.pagination import paginate

class RequirementForm(forms.ModelForm):
    class Meta:
        model = Requirement
        fields = ['project', 'title', 'description', 'type', 'priority', 'status', 'expected_finish_time']
        widgets = {
            'expected_finish_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

@login_required
def requirement_list(request):
    projects = visible_projects(request.user)
    requirements = Requirement.objects.filter(project__in=projects).order_by('-created_at')
    
    # Filter
    keyword = request.GET.get('keyword', '')
    project_id = request.GET.get('project', '')
    date_start = request.GET.get('date_start', '')
    date_end = request.GET.get('date_end', '')
    
    if keyword:
        if keyword.isdigit():
            requirements = requirements.filter(Q(id=keyword) | Q(title__icontains=keyword))
        else:
            requirements = requirements.filter(title__icontains=keyword)
            
    if project_id:
        requirements = requirements.filter(project_id=project_id)

    if date_start:
        requirements = requirements.filter(created_at__gte=date_start)
    if date_end:
        requirements = requirements.filter(created_at__lte=date_end)

    # Stats
    total = requirements.count()
    my_reqs = requirements.filter(creator=request.user).count()
    # Assuming 'Pending Review' is 1
    pending_review = requirements.filter(status=1).count()

    pg = paginate(request, requirements, per_page=20)
    context = {
        'requirements': pg.page_obj,
        'page_obj': pg.page_obj,
        'paginator': pg.paginator,
        'is_paginated': pg.is_paginated,
        'page_range': pg.page_range,
        'projects': projects,
        'total': total,
        'my_reqs': my_reqs,
        'pending_review': pending_review,
    }
    return render(request, 'requirements/requirement_list.html', context)

@login_required
def requirement_create(request):
    if request.method == 'POST':
        form = RequirementForm(request.POST)
        form.fields["project"].queryset = visible_projects(request.user)
        if form.is_valid():
            req = form.save(commit=False)
            req.creator = request.user
            req.save()
            return redirect('requirement_list')
    else:
        form = RequirementForm()
        form.fields["project"].queryset = visible_projects(request.user)
    return render(request, 'requirements/requirement_form.html', {'form': form, 'title': '创建需求'})

@login_required
def requirement_detail(request, pk):
    requirement = get_object_or_404(Requirement.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    return render(request, 'requirements/requirement_detail.html', {'requirement': requirement})

@login_required
def requirement_edit(request, pk):
    requirement = get_object_or_404(Requirement.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    can_edit = is_admin_user(request.user) or requirement.creator_id == request.user.id or requirement.project.owner_id == request.user.id
    if not can_edit:
        return HttpResponseForbidden("无权限编辑该需求")
    if request.method == 'POST':
        form = RequirementForm(request.POST, instance=requirement)
        form.fields["project"].queryset = visible_projects(request.user)
        if form.is_valid():
            form.save()
            return redirect('requirement_detail', pk=pk)
    else:
        form = RequirementForm(instance=requirement)
        form.fields["project"].queryset = visible_projects(request.user)
    return render(request, 'requirements/requirement_form.html', {'form': form, 'title': '编辑需求'})

@login_required
@require_POST
def requirement_delete(request, pk):
    requirement = get_object_or_404(Requirement.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    can_delete = is_admin_user(request.user) or requirement.creator_id == request.user.id or requirement.project.owner_id == request.user.id
    if not can_delete:
        return HttpResponseForbidden("无权限删除该需求")
    title = requirement.title
    requirement.delete()
    messages.success(request, f"已删除需求：{title}")
    return redirect("requirement_list")

@login_required
@require_POST
def requirement_bulk_delete(request):
    ids = request.POST.getlist("requirement_ids")
    ids = [i for i in ids if str(i).isdigit()]
    if not ids:
        messages.warning(request, "未选择任何需求")
        return redirect("requirement_list")

    qs = Requirement.objects.filter(project__in=visible_projects(request.user), id__in=ids)
    to_delete = []
    denied = 0
    for r in qs:
        can_delete = is_admin_user(request.user) or r.creator_id == request.user.id or r.project.owner_id == request.user.id
        if can_delete:
            to_delete.append(r)
        else:
            denied += 1

    deleted = 0
    for r in to_delete:
        r.delete()
        deleted += 1

    if deleted:
        messages.success(request, f"已删除 {deleted} 个需求")
    if denied:
        messages.warning(request, f"{denied} 个需求无权限删除，已跳过")
    return redirect("requirement_list")
