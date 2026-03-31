from django.contrib.auth.models import Group

from core.visibility import is_admin_user


SYSTEM_ROLE_NAMES = ("System Admin", "Project Admin", "Tester", "Developer", "Normal User")
SYSTEM_ROLE_LABELS_ZH = {
    "System Admin": "系统管理员",
    "Project Admin": "项目管理员",
    "Tester": "测试人员",
    "Developer": "开发人员",
    "Normal User": "普通用户",
}


def system_roles_qs():
    return Group.objects.filter(name__in=SYSTEM_ROLE_NAMES).order_by("name")

def system_role_label(name: str) -> str:
    return SYSTEM_ROLE_LABELS_ZH.get(name or "", name or "")


def can_manage_users(user) -> bool:
    if is_admin_user(user):
        return True
    role = getattr(user, "role", None)
    return bool(role and getattr(role, "name", "") == "System Admin")
