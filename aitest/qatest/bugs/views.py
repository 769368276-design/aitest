from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Bug
from projects.models import Project
from testcases.models import TestCase
from django import forms
from django.db.models import Q
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import HttpResponseForbidden

from core.visibility import visible_projects, is_admin_user
from core.pagination import paginate

class BugForm(forms.ModelForm):
    class Meta:
        model = Bug
        fields = ['project', 'case', 'title', 'description', 'reproduce_steps', 'severity', 'priority', 'status', 'assignee', 'affected_version', 'fixed_version']

@login_required
def bug_list(request):
    projects = visible_projects(request.user)
    bugs = Bug.objects.filter(project__in=projects).order_by('-created_at')
    
    # Filter
    keyword = request.GET.get('keyword', '')
    project_id = request.GET.get('project', '')
    case_id = request.GET.get('case', '')
    date_start = request.GET.get('date_start', '')
    date_end = request.GET.get('date_end', '')
    
    # Fetch cases filtered by selected project if project is selected
    if project_id:
        if projects.filter(id=project_id).exists():
            cases = TestCase.objects.filter(project_id=project_id)
        else:
            cases = TestCase.objects.none()
    else:
        cases = TestCase.objects.filter(project__in=projects)

    if keyword:
        if keyword.isdigit():
            bugs = bugs.filter(Q(id=keyword) | Q(title__icontains=keyword))
        else:
            bugs = bugs.filter(title__icontains=keyword)
            
    if project_id:
        bugs = bugs.filter(project_id=project_id)
    
    if case_id:
        bugs = bugs.filter(case_id=case_id)

    if date_start:
        bugs = bugs.filter(created_at__gte=date_start)
    if date_end:
        bugs = bugs.filter(created_at__lte=date_end)

    # Stats
    total = bugs.count()
    my_bugs = bugs.filter(assignee=request.user).count()
    # Assuming 'Fixed' is 4, 'Closed' is 6. Not fixed = exclude(4,6)
    not_fixed = bugs.exclude(status__in=[4, 6]).count()

    pg = paginate(request, bugs, per_page=20)
    context = {
        'bugs': pg.page_obj,
        'page_obj': pg.page_obj,
        'paginator': pg.paginator,
        'is_paginated': pg.is_paginated,
        'page_range': pg.page_range,
        'projects': projects,
        'cases': cases,
        'total': total,
        'my_bugs': my_bugs,
        'not_fixed': not_fixed,
    }
    return render(request, 'bugs/bug_list.html', context)

@login_required
def bug_create(request):
    if request.method == 'POST':
        form = BugForm(request.POST)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["case"].queryset = TestCase.objects.filter(project__in=visible_projects(request.user))
        if form.is_valid():
            bug = form.save(commit=False)
            bug.creator = request.user
            bug.save()
            return redirect('bug_list')
    else:
        form = BugForm()
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["case"].queryset = TestCase.objects.filter(project__in=visible_projects(request.user))
    return render(request, 'bugs/bug_form.html', {'form': form, 'title': '提交缺陷'})

@login_required
def bug_detail(request, pk):
    bug = get_object_or_404(Bug.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    return render(request, 'bugs/bug_detail.html', {'bug': bug})

@login_required
def bug_edit(request, pk):
    bug = get_object_or_404(Bug.objects.filter(project__in=visible_projects(request.user)), pk=pk)
    can_edit = is_admin_user(request.user) or bug.creator_id == request.user.id or bug.project.owner_id == request.user.id
    if not can_edit:
        return HttpResponseForbidden("无权限编辑该缺陷")
    if request.method == 'POST':
        form = BugForm(request.POST, instance=bug)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["case"].queryset = TestCase.objects.filter(project__in=visible_projects(request.user))
        if form.is_valid():
            form.save()
            return redirect('bug_detail', pk=pk)
    else:
        form = BugForm(instance=bug)
        form.fields["project"].queryset = visible_projects(request.user)
        form.fields["case"].queryset = TestCase.objects.filter(project__in=visible_projects(request.user))
    return render(request, 'bugs/bug_form.html', {'form': form, 'title': '编辑缺陷'})

@login_required
@require_POST
def bug_batch_delete(request):
    bug_ids = request.POST.getlist('bug_ids')
    if bug_ids:
        Bug.objects.filter(project__in=visible_projects(request.user), id__in=bug_ids).delete()
        messages.success(request, f'成功删除了 {len(bug_ids)} 个缺陷。')
    else:
        messages.warning(request, '未选择任何缺陷。')
    return redirect('bug_list')
