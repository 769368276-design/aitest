from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST

from core.permissions import can_manage_users, system_roles_qs, system_role_label
from users.models import UserGroup, UserGroupMember, UserAIModelConfig
from core.visibility import visible_projects
from core.pagination import paginate

User = get_user_model()


class CreateUserForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    password1 = forms.CharField(label="密码", widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}))
    password2 = forms.CharField(label="确认密码", widget=forms.PasswordInput(attrs={"class": "form-control", "autocomplete": "new-password"}))
    role = forms.ModelChoiceField(label="系统角色", required=False, queryset=Group.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    is_staff = forms.BooleanField(label="设为管理员", required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = system_roles_qs()
        self.fields["role"].label_from_instance = lambda obj: system_role_label(getattr(obj, "name", ""))

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("用户名不能为空")
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("用户名已存在")
        return username

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""
        if p1 != p2:
            raise forms.ValidationError("两次输入的密码不一致")
        if len(p1) < 6:
            raise forms.ValidationError("密码长度至少 6 位")
        return cleaned


class UpdateUserForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(attrs={"class": "form-control"}))
    role = forms.ModelChoiceField(label="系统角色", required=False, queryset=Group.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    is_staff = forms.BooleanField(label="设为管理员", required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))
    is_active = forms.BooleanField(label="启用账号", required=False, widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def __init__(self, *args, user_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_obj = user_obj
        self.fields["role"].queryset = system_roles_qs()
        self.fields["role"].label_from_instance = lambda obj: system_role_label(getattr(obj, "name", ""))

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("用户名不能为空")
        qs = User.objects.filter(username=username)
        if self.user_obj:
            qs = qs.exclude(id=self.user_obj.id)
        if qs.exists():
            raise forms.ValidationError("用户名已存在")
        return username


class ResetPasswordForm(forms.Form):
    password1 = forms.CharField(label="新密码", widget=forms.PasswordInput(attrs={"class": "form-control"}))
    password2 = forms.CharField(label="确认新密码", widget=forms.PasswordInput(attrs={"class": "form-control"}))

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""
        if p1 != p2:
            raise forms.ValidationError("两次输入的密码不一致")
        if len(p1) < 6:
            raise forms.ValidationError("密码长度至少 6 位")
        return cleaned


def _admin_required(request):
    if not can_manage_users(request.user):
        return HttpResponseForbidden("无权限访问")
    return None

@login_required
def user_manage(request):
    resp = _admin_required(request)
    if resp:
        return resp
    if request.method == "POST":
        form = CreateUserForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
            )
            user.role = form.cleaned_data.get("role")
            user.is_staff = bool(form.cleaned_data.get("is_staff")) or bool(getattr(user.role, "name", "") == "System Admin")
            user.save(update_fields=["is_staff", "role"])
            messages.success(request, "用户创建成功")
            return redirect("user_manage")
    else:
        form = CreateUserForm()

    keyword = (request.GET.get("keyword") or "").strip()
    users = User.objects.all().order_by("-date_joined", "id")
    if keyword:
        users = users.filter(username__icontains=keyword)
    pg = paginate(request, users, per_page=20)
    return render(
        request,
        "users/user_manage.html",
        {"form": form, "users": pg.page_obj, "keyword": keyword, "page_obj": pg.page_obj, "paginator": pg.paginator, "is_paginated": pg.is_paginated, "page_range": pg.page_range},
    )


@login_required
def user_edit(request, user_id: int):
    from django.db import models
    resp = _admin_required(request)
    if resp:
        return resp
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == "POST":
        form = UpdateUserForm(request.POST, user_obj=user_obj)
        if form.is_valid():
            username = form.cleaned_data["username"]
            role = form.cleaned_data.get("role")
            is_staff = bool(form.cleaned_data.get("is_staff"))
            is_active = bool(form.cleaned_data.get("is_active"))
            if user_obj.is_superuser:
                is_staff = True
                is_active = True
                role = user_obj.role
            else:
                if role and getattr(role, "name", "") == "System Admin":
                    is_staff = True
            if (user_obj.is_staff or user_obj.is_superuser) and (not is_staff) and (not user_obj.is_superuser):
                admin_count = User.objects.filter(is_active=True).filter(models.Q(is_staff=True) | models.Q(is_superuser=True)).count()
                if admin_count <= 1:
                    messages.error(request, "至少保留一个管理员账号")
                    return redirect("user_edit", user_id=user_id)
            user_obj.username = username
            user_obj.role = role
            user_obj.is_staff = is_staff
            user_obj.is_active = is_active
            user_obj.save(update_fields=["username", "role", "is_staff", "is_active"])
            messages.success(request, "用户信息已更新")
            return redirect("user_manage")
    else:
        form = UpdateUserForm(
            initial={
                "username": user_obj.username,
                "role": user_obj.role_id,
                "is_staff": user_obj.is_staff or user_obj.is_superuser,
                "is_active": user_obj.is_active,
            },
            user_obj=user_obj,
        )
    return render(request, "users/user_edit.html", {"form": form, "u": user_obj})


@login_required
def user_reset_password(request, user_id: int):
    resp = _admin_required(request)
    if resp:
        return resp
    user_obj = get_object_or_404(User, id=user_id)
    if request.method == "POST":
        form = ResetPasswordForm(request.POST)
        if form.is_valid():
            user_obj.set_password(form.cleaned_data["password1"])
            user_obj.save(update_fields=["password"])
            messages.success(request, "密码已重置")
            return redirect("user_manage")
    else:
        form = ResetPasswordForm()
    return render(request, "users/user_password.html", {"form": form, "u": user_obj})


@login_required
@require_POST
def user_delete(request, user_id: int):
    from django.db import models
    resp = _admin_required(request)
    if resp:
        return resp
    if int(user_id) == int(request.user.id):
        messages.error(request, "不能删除当前登录用户")
        return redirect("user_manage")
    user_obj = get_object_or_404(User, id=user_id)
    if (user_obj.is_staff or user_obj.is_superuser):
        admin_count = User.objects.filter(is_active=True).filter(models.Q(is_staff=True) | models.Q(is_superuser=True)).count()
        if admin_count <= 1:
            messages.error(request, "至少保留一个管理员账号")
            return redirect("user_manage")
    user_obj.delete()
    messages.success(request, "用户已删除")
    return redirect("user_manage")


class CreateUserGroupForm(forms.Form):
    name = forms.CharField(label="用户组名称", max_length=80, widget=forms.TextInput(attrs={"class": "form-control"}))
    shared_projects = forms.ModelMultipleChoiceField(
        label="共享项目",
        required=False,
        queryset=None,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "8"}),
    )

    def __init__(self, *args, owner=None, **kwargs):
        projects_qs = kwargs.pop("projects_qs", None)
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fields["shared_projects"].queryset = projects_qs if projects_qs is not None else visible_projects(owner) if owner else User.objects.none()

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("用户组名称不能为空")
        if len(name) > 80:
            raise forms.ValidationError("用户组名称过长")
        if self.owner and UserGroup.objects.filter(owner=self.owner, name=name).exists():
            raise forms.ValidationError("你已创建同名用户组")
        return name


class AddUserGroupMemberForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150, widget=forms.TextInput(attrs={"class": "form-control"}))
    role = forms.ChoiceField(
        label="组内角色",
        choices=(("member", "成员"), ("admin", "管理员")),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("用户名不能为空")
        return username


def _get_my_group_member(group_id: int, user):
    try:
        return UserGroupMember.objects.select_related("group").get(group_id=group_id, user=user)
    except UserGroupMember.DoesNotExist:
        return None


@login_required
def user_group_list(request):
    if request.method == "POST":
        form = CreateUserGroupForm(request.POST, owner=request.user, projects_qs=visible_projects(request.user))
        if form.is_valid():
            g = UserGroup.objects.create(name=form.cleaned_data["name"], owner=request.user)
            UserGroupMember.objects.create(group=g, user=request.user, role="owner")
            g.shared_projects.set(form.cleaned_data.get("shared_projects") or [])
            messages.success(request, "用户组已创建")
            return redirect("user_group_list")
    else:
        form = CreateUserGroupForm(owner=request.user, projects_qs=visible_projects(request.user))

    memberships = (
        UserGroupMember.objects.filter(user=request.user)
        .select_related("group", "group__owner")
        .order_by("-group__created_at", "-group_id")
    )
    pg = paginate(request, memberships, per_page=20)
    return render(request, "users/user_groups.html", {"form": form, "memberships": pg.page_obj, "page_obj": pg.page_obj, "paginator": pg.paginator, "is_paginated": pg.is_paginated, "page_range": pg.page_range})


@login_required
def user_group_detail(request, group_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    is_owner = group.owner_id == request.user.id
    is_manager = is_owner or mem.role in ("owner", "admin")
    members = (
        UserGroupMember.objects.filter(group=group)
        .select_related("user")
        .order_by("-role", "user__username", "id")
    )
    pg_members = paginate(request, members, per_page=20)
    add_form = AddUserGroupMemberForm()
    projects_qs = visible_projects(request.user)
    shared_projects = group.shared_projects.all().order_by("-created_at", "-id")
    return render(
        request,
        "users/user_group_detail.html",
        {
            "group": group,
            "mem": mem,
            "members": pg_members.page_obj,
            "page_obj": pg_members.page_obj,
            "paginator": pg_members.paginator,
            "is_paginated": pg_members.is_paginated,
            "page_range": pg_members.page_range,
            "is_manager": is_manager,
            "is_owner": is_owner,
            "add_form": add_form,
            "projects_qs": projects_qs,
            "shared_projects": shared_projects,
        },
    )


@login_required
@require_POST
def user_group_add_member(request, group_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    is_owner = group.owner_id == request.user.id
    is_manager = is_owner or mem.role in ("owner", "admin")
    if not is_manager:
        return HttpResponseForbidden("无权限添加成员")

    form = AddUserGroupMemberForm(request.POST)
    if not form.is_valid():
        messages.error(request, "添加失败：输入不合法")
        return redirect("user_group_detail", group_id=group_id)
    username = form.cleaned_data["username"]
    role = form.cleaned_data["role"]
    if role not in ("member", "admin"):
        role = "member"
    try:
        user_obj = User.objects.get(username=username)
    except User.DoesNotExist:
        messages.error(request, "添加失败：用户不存在")
        return redirect("user_group_detail", group_id=group_id)
    if int(user_obj.id) == int(group.owner_id):
        messages.warning(request, "该用户已是组创建人")
        return redirect("user_group_detail", group_id=group_id)
    if UserGroupMember.objects.filter(group=group, user=user_obj).exists():
        messages.warning(request, "该用户已在组内")
        return redirect("user_group_detail", group_id=group_id)
    UserGroupMember.objects.create(group=group, user=user_obj, role=role)
    messages.success(request, "成员已添加")
    return redirect("user_group_detail", group_id=group_id)


@login_required
@require_POST
def user_group_remove_member(request, group_id: int, user_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    is_owner = group.owner_id == request.user.id
    is_manager = is_owner or mem.role in ("owner", "admin")
    if not is_manager:
        return HttpResponseForbidden("无权限移除成员")
    if int(user_id) == int(group.owner_id):
        messages.error(request, "不能移除创建人")
        return redirect("user_group_detail", group_id=group_id)
    if int(user_id) == int(request.user.id) and not is_owner:
        UserGroupMember.objects.filter(group=group, user=request.user).delete()
        messages.success(request, "已退出用户组")
        return redirect("user_group_list")
    UserGroupMember.objects.filter(group=group, user_id=user_id).delete()
    messages.success(request, "成员已移除")
    return redirect("user_group_detail", group_id=group_id)


@login_required
@require_POST
def user_group_leave(request, group_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    if int(group.owner_id) == int(request.user.id):
        messages.error(request, "创建人不能退出用户组，请删除用户组或移交后再退出")
        return redirect("user_group_detail", group_id=group_id)
    UserGroupMember.objects.filter(group=group, user=request.user).delete()
    messages.success(request, "已退出用户组")
    return redirect("user_group_list")


@login_required
@require_POST
def user_group_delete(request, group_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    if int(group.owner_id) != int(request.user.id):
        return HttpResponseForbidden("仅创建人可删除用户组")
    group.delete()
    messages.success(request, "用户组已删除")
    return redirect("user_group_list")


@login_required
@require_POST
def user_group_update_shared_projects(request, group_id: int):
    mem = _get_my_group_member(group_id, request.user)
    if not mem:
        return HttpResponseForbidden("无权限访问该用户组")
    group = mem.group
    is_owner = group.owner_id == request.user.id
    is_manager = is_owner or mem.role in ("owner", "admin")
    if not is_manager:
        return HttpResponseForbidden("无权限更新共享项目")
    ids = request.POST.getlist("shared_project_ids")
    ids = [i for i in ids if str(i).isdigit()]
    allowed = visible_projects(request.user).filter(id__in=ids)
    group.shared_projects.set(list(allowed))
    messages.success(request, "共享项目已更新")
    return redirect("user_group_detail", group_id=group_id)


class UserProfileForm(forms.Form):
    first_name = forms.CharField(label="昵称/姓名", max_length=150, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    phone = forms.CharField(label="手机号", max_length=20, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    password = forms.CharField(label="新密码 (留空不修改)", required=False, widget=forms.PasswordInput(attrs={"class": "form-control"}))
    confirm_password = forms.CharField(label="确认密码", required=False, widget=forms.PasswordInput(attrs={"class": "form-control"}))

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password")
        p2 = cleaned.get("confirm_password")
        if p1 and p1 != p2:
            raise forms.ValidationError("两次密码输入不一致")
        return cleaned


class UserAIModelConfigForm(forms.Form):
    testcase_provider = forms.ChoiceField(label="用例生成-提供方", choices=UserAIModelConfig.PROVIDER_CHOICES, required=True, widget=forms.Select(attrs={"class": "form-select"}))
    testcase_model = forms.CharField(label="用例生成-模型名称", max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择或输入支持视觉的模型"}))
    testcase_base_url = forms.CharField(label="用例生成-Base URL", max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择模型后自动填充，可手动修改"}))
    testcase_api_key = forms.CharField(label="用例生成-API Key（留空不修改）", required=False, widget=forms.PasswordInput(attrs={"class": "form-control"}))

    exec_provider = forms.ChoiceField(label="AI执行-提供方", choices=UserAIModelConfig.PROVIDER_CHOICES, required=True, widget=forms.Select(attrs={"class": "form-select"}))
    exec_model = forms.CharField(label="AI执行-模型名称", max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择或输入支持视觉的模型"}))
    exec_base_url = forms.CharField(label="AI执行-Base URL", max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择模型后自动填充，可手动修改"}))
    exec_api_key = forms.CharField(label="AI执行-API Key（留空不修改）", required=False, widget=forms.PasswordInput(attrs={"class": "form-control"}))

    ocr_provider = forms.ChoiceField(label="OCR-提供方", choices=UserAIModelConfig.PROVIDER_CHOICES, required=True, widget=forms.Select(attrs={"class": "form-select"}))
    ocr_model = forms.CharField(label="OCR-模型名称", max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择或输入支持视觉的模型"}))
    ocr_base_url = forms.CharField(label="OCR-Base URL", max_length=255, required=False, widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "选择模型后自动填充，可手动修改"}))
    ocr_api_key = forms.CharField(label="OCR-API Key（留空不修改）", required=False, widget=forms.PasswordInput(attrs={"class": "form-control"}))

    def __init__(self, *args, instance: UserAIModelConfig | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance is not None and not args and not kwargs.get("data"):
            self.initial.update(
                {
                    "testcase_provider": instance.testcase_provider,
                    "testcase_model": instance.testcase_model,
                    "testcase_base_url": instance.testcase_base_url,
                    "exec_provider": instance.exec_provider,
                    "exec_model": instance.exec_model,
                    "exec_base_url": instance.exec_base_url,
                    "ocr_provider": instance.ocr_provider,
                    "ocr_model": instance.ocr_model,
                    "ocr_base_url": instance.ocr_base_url,
                }
            )
            if (instance.testcase_api_key or "").strip():
                self.fields["testcase_api_key"].widget.attrs["placeholder"] = "已配置（留空不修改）"
            if (instance.exec_api_key or "").strip():
                self.fields["exec_api_key"].widget.attrs["placeholder"] = "已配置（留空不修改）"
            if (instance.ocr_api_key or "").strip():
                self.fields["ocr_api_key"].widget.attrs["placeholder"] = "已配置（留空不修改）"

    def clean(self):
        cleaned = super().clean()
        for scope in ("testcase", "exec", "ocr"):
            provider = str(cleaned.get(f"{scope}_provider") or "").strip().lower()
            model = str(cleaned.get(f"{scope}_model") or "").strip()
            base_url = str(cleaned.get(f"{scope}_base_url") or "").strip()
            base_url = base_url.strip("`").strip().strip('"').strip("'").strip()
            cleaned[f"{scope}_base_url"] = base_url
            if provider == "openai_compatible" and (not model or not base_url):
                raise forms.ValidationError(f"{scope} 选择“自定义 OpenAI 兼容”时必须填写模型名称与 Base URL")
            if provider in ("anthropic", "google") and scope in ("testcase", "ocr") and (not model or not base_url):
                raise forms.ValidationError(f"{scope} 选择“{provider}”用于用例生成/OCR时必须提供 OpenAI 兼容 Base URL 与模型名称（建议使用 OpenRouter 或兼容网关）")
            if provider == "deepseek" and model and ("vl" not in model.lower()) and ("vision" not in model.lower()):
                raise forms.ValidationError(f"{scope} 的 DeepSeek 模型需为视觉模型（例如名称包含 vl/vision）")
            if provider == "kimi" and model and (" " in model):
                raise forms.ValidationError(f"{scope} 的 Kimi 模型名不要包含空格（示例：kimi-k2.5 或 moonshot-v1-32k）")
            if provider == "minimax" and model and (" " in model):
                raise forms.ValidationError(f"{scope} 的 MiniMax 模型名不要包含空格")
            if provider == "doubao" and model and (" " in model):
                raise forms.ValidationError(f"{scope} 的 豆包 模型名不要包含空格")
            b = base_url.lower()
            if provider == "kimi" and base_url and (("dashscope" in b) or ("aliyuncs.com" in b)):
                raise forms.ValidationError(f"{scope} 选择 Kimi 时 Base URL 不能是 DashScope：请改为 https://api.moonshot.cn/v1")
            if provider == "qwen" and base_url and (("moonshot" in b) or ("api.moonshot" in b)):
                raise forms.ValidationError(f"{scope} 选择 Qwen 时 Base URL 不能是 Moonshot：请改为 https://dashscope.aliyuncs.com/compatible-mode/v1")
            if provider == "doubao" and base_url and ("volces" not in b) and ("ark.cn-" not in b):
                if base_url:
                    raise forms.ValidationError(f"{scope} 选择 豆包 时 Base URL 建议为火山引擎 Ark 网关（示例：https://ark.cn-beijing.volces.com/api/v3）")
        return cleaned

    def save(self, user) -> UserAIModelConfig:
        obj, _ = UserAIModelConfig.objects.get_or_create(user=user)
        cd = self.cleaned_data
        obj.testcase_provider = cd["testcase_provider"]
        obj.testcase_model = (cd.get("testcase_model") or "").strip()
        obj.testcase_base_url = str(cd.get("testcase_base_url") or "").strip().strip("`").strip().strip('"').strip("'").strip()
        if (cd.get("testcase_api_key") or "").strip():
            obj.testcase_api_key = cd["testcase_api_key"].strip()

        obj.exec_provider = cd["exec_provider"]
        obj.exec_model = (cd.get("exec_model") or "").strip()
        obj.exec_base_url = str(cd.get("exec_base_url") or "").strip().strip("`").strip().strip('"').strip("'").strip()
        if (cd.get("exec_api_key") or "").strip():
            obj.exec_api_key = cd["exec_api_key"].strip()

        obj.ocr_provider = cd["ocr_provider"]
        obj.ocr_model = (cd.get("ocr_model") or "").strip()
        obj.ocr_base_url = str(cd.get("ocr_base_url") or "").strip().strip("`").strip().strip('"').strip("'").strip()
        if (cd.get("ocr_api_key") or "").strip():
            obj.ocr_api_key = cd["ocr_api_key"].strip()
        obj.save()
        return obj


@login_required
def user_profile(request):
    user = request.user
    cfg, _ = UserAIModelConfig.objects.get_or_create(user=user)
    if request.method == "POST":
        form_type = (request.POST.get("form_type") or "").strip()
        if form_type == "ai_model":
            model_form = UserAIModelConfigForm(request.POST, instance=cfg)
            profile_form = UserProfileForm(initial={"first_name": user.first_name, "phone": getattr(user, "phone", "")})
            if model_form.is_valid():
                model_form.save(user)
                messages.success(request, "模型配置已更新")
                return redirect("user_profile")
        else:
            profile_form = UserProfileForm(request.POST)
            model_form = UserAIModelConfigForm(instance=cfg)
            if profile_form.is_valid():
                user.first_name = profile_form.cleaned_data["first_name"]
                user.phone = profile_form.cleaned_data["phone"]
                p = profile_form.cleaned_data.get("password")
                if p:
                    user.set_password(p)
                user.save(update_fields=["first_name", "phone"] + (["password"] if p else []))
                messages.success(request, "个人资料已更新" + ("，请重新登录" if p else ""))
                if p:
                    return redirect("login")
                return redirect("user_profile")
    else:
        profile_form = UserProfileForm(initial={"first_name": user.first_name, "phone": getattr(user, "phone", "")})
        model_form = UserAIModelConfigForm(instance=cfg)
    key_status = {
        "testcase": bool((cfg.testcase_api_key or "").strip()),
        "exec": bool((cfg.exec_api_key or "").strip()),
        "ocr": bool((cfg.ocr_api_key or "").strip()),
    }
    return render(request, "users/profile.html", {"profile_form": profile_form, "model_form": model_form, "key_status": key_status})


class RoleForm(forms.Form):
    name = forms.CharField(label="角色名称", max_length=80, widget=forms.TextInput(attrs={"class": "form-control"}))
    permissions = forms.ModelMultipleChoiceField(
        label="权限",
        queryset=Permission.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"class": "form-select", "size": "10"})
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
    
    def clean_name(self):
        name = self.cleaned_data["name"]
        qs = Group.objects.filter(name=name)
        if self.instance:
            qs = qs.exclude(id=self.instance.id)
        if qs.exists():
            raise forms.ValidationError("角色名称已存在")
        return name


@login_required
def role_list(request):
    resp = _admin_required(request)
    if resp: return resp
    roles = Group.objects.all().order_by("name")
    pg = paginate(request, roles, per_page=20)
    return render(request, "users/role_list.html", {"roles": pg.page_obj, "page_obj": pg.page_obj, "paginator": pg.paginator, "is_paginated": pg.is_paginated, "page_range": pg.page_range})


@login_required
def role_create(request):
    resp = _admin_required(request)
    if resp: return resp
    if request.method == "POST":
        form = RoleForm(request.POST)
        if form.is_valid():
            g = Group.objects.create(name=form.cleaned_data["name"])
            g.permissions.set(form.cleaned_data["permissions"])
            messages.success(request, "角色已创建")
            return redirect("role_list")
    else:
        form = RoleForm()
    return render(request, "users/role_form.html", {"form": form, "title": "创建角色"})


@login_required
def role_edit(request, role_id):
    resp = _admin_required(request)
    if resp: return resp
    role = get_object_or_404(Group, id=role_id)
    if request.method == "POST":
        form = RoleForm(request.POST, instance=role)
        if form.is_valid():
            role.name = form.cleaned_data["name"]
            role.save()
            role.permissions.set(form.cleaned_data["permissions"])
            messages.success(request, "角色已更新")
            return redirect("role_list")
    else:
        form = RoleForm(initial={
            "name": role.name,
            "permissions": role.permissions.all()
        }, instance=role)
    return render(request, "users/role_form.html", {"form": form, "title": "编辑角色"})


@login_required
@require_POST
def role_delete(request, role_id):
    resp = _admin_required(request)
    if resp: return resp
    role = get_object_or_404(Group, id=role_id)
    if role.name == "System Admin":
        messages.error(request, "不能删除系统管理员角色")
        return redirect("role_list")
    role.delete()
    messages.success(request, "角色已删除")
    return redirect("role_list")
