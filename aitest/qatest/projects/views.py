from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Project
from django import forms
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.contrib import messages

from core.visibility import visible_projects, is_admin_user
from core.pagination import paginate

class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ['name', 'description', 'base_url', 'test_accounts', 'history_requirements', 'knowledge_base', 'status', 'start_time', 'end_time']
        widgets = {
            'start_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'end_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'test_accounts': forms.Textarea(attrs={'rows': 4}),
            'history_requirements': forms.Textarea(attrs={'rows': 4}),
            'knowledge_base': forms.Textarea(attrs={'rows': 6}),
        }

@login_required
def project_list(request):
    projects = visible_projects(request.user).order_by('-created_at')
    
    # Filter
    keyword = request.GET.get('keyword', '')
    date_start = request.GET.get('date_start', '')
    date_end = request.GET.get('date_end', '')
    
    if keyword:
        if keyword.isdigit():
            projects = projects.filter(Q(id=keyword) | Q(name__icontains=keyword))
        else:
            projects = projects.filter(name__icontains=keyword)
            
    if date_start:
        projects = projects.filter(created_at__gte=date_start)
    if date_end:
        projects = projects.filter(created_at__lte=date_end)

    # Stats
    total = projects.count()
    my_projects = projects.filter(owner=request.user).count()
    # Assuming 'Pending' status is 1
    pending_projects = projects.filter(status=1).count()

    pg = paginate(request, projects, per_page=20)
    context = {
        'projects': pg.page_obj,
        'page_obj': pg.page_obj,
        'paginator': pg.paginator,
        'is_paginated': pg.is_paginated,
        'page_range': pg.page_range,
        'total': total,
        'my_projects': my_projects,
        'pending_projects': pending_projects,
    }
    return render(request, 'projects/project_list.html', context)

@login_required
def project_create(request):
    if request.method == 'POST':
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.owner = request.user
            project.save()
            return redirect('project_list')
    else:
        form = ProjectForm()
    return render(request, 'projects/project_form.html', {'form': form, 'title': '创建项目'})

@login_required
def project_detail(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    return render(request, 'projects/project_detail.html', {'project': project})

@login_required
def project_edit(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if not is_admin_user(request.user) and project.owner_id != request.user.id:
        return HttpResponseForbidden("无权限编辑该项目")
    if request.method == 'POST':
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            return redirect('project_detail', pk=pk)
    else:
        form = ProjectForm(instance=project)
    return render(request, 'projects/project_form.html', {'form': form, 'title': '编辑项目'})

@login_required
@require_POST
def project_delete(request, pk):
    project = get_object_or_404(visible_projects(request.user), pk=pk)
    if not is_admin_user(request.user) and project.owner_id != request.user.id:
        return HttpResponseForbidden("无权限删除该项目")
    name = project.name
    project.delete()
    messages.success(request, f"已删除项目：{name}")
    return redirect("project_list")

@login_required
@require_POST
def project_bulk_delete(request):
    ids = request.POST.getlist("project_ids")
    ids = [i for i in ids if str(i).isdigit()]
    if not ids:
        messages.warning(request, "未选择任何项目")
        return redirect("project_list")

    qs = visible_projects(request.user).filter(id__in=ids)
    to_delete = []
    denied = 0
    for p in qs:
        if is_admin_user(request.user) or p.owner_id == request.user.id:
            to_delete.append(p)
        else:
            denied += 1

    deleted = 0
    for p in to_delete:
        p.delete()
        deleted += 1

    if deleted:
        messages.success(request, f"已删除 {deleted} 个项目")
    if denied:
        messages.warning(request, f"{denied} 个项目无权限删除，已跳过")
    return redirect("project_list")
