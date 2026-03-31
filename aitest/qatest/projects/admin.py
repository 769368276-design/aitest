from django.contrib import admin
from .models import Project, ProjectMember

class ProjectMemberInline(admin.TabularInline):
    model = ProjectMember
    extra = 1

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'owner', 'start_time', 'end_time')
    list_filter = ('status',)
    search_fields = ('name',)
    inlines = [ProjectMemberInline]
