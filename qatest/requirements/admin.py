from django.contrib import admin
from .models import Requirement

@admin.register(Requirement)
class RequirementAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'priority', 'status', 'creator', 'created_at')
    list_filter = ('project', 'status', 'priority')
    search_fields = ('title', 'description')
