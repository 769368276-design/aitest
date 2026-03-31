from django.db.models import Q

from projects.models import Project
from users.models import UserGroupMember


def is_admin_user(user) -> bool:
    return bool(getattr(user, "is_authenticated", False) and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)))


def visible_projects(user):
    if is_admin_user(user):
        return Project.objects.all()
    base = Project.objects.filter(Q(owner=user) | Q(members__user=user))
    group_ids = UserGroupMember.objects.filter(user=user).values_list("group_id", flat=True)
    shared = Project.objects.filter(shared_user_groups__id__in=group_ids)
    return (base | shared).distinct()

