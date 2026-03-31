from django.urls import path

from . import views


urlpatterns = [
    path("manage/", views.user_manage, name="user_manage"),
    path("manage/<int:user_id>/edit/", views.user_edit, name="user_edit"),
    path("manage/<int:user_id>/password/", views.user_reset_password, name="user_reset_password"),
    path("manage/<int:user_id>/delete/", views.user_delete, name="user_delete"),
    path("profile/", views.user_profile, name="user_profile"),
    path("roles/", views.role_list, name="role_list"),
    path("roles/create/", views.role_create, name="role_create"),
    path("roles/<int:role_id>/edit/", views.role_edit, name="role_edit"),
    path("roles/<int:role_id>/delete/", views.role_delete, name="role_delete"),
    path("groups/", views.user_group_list, name="user_group_list"),
    path("groups/<int:group_id>/", views.user_group_detail, name="user_group_detail"),
    path("groups/<int:group_id>/add-member/", views.user_group_add_member, name="user_group_add_member"),
    path("groups/<int:group_id>/remove-member/<int:user_id>/", views.user_group_remove_member, name="user_group_remove_member"),
    path("groups/<int:group_id>/leave/", views.user_group_leave, name="user_group_leave"),
    path("groups/<int:group_id>/delete/", views.user_group_delete, name="user_group_delete"),
    path("groups/<int:group_id>/update-projects/", views.user_group_update_shared_projects, name="user_group_update_shared_projects"),
]

