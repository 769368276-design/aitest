from django.contrib import admin
from .models import TestCase, CaseExecution

class CaseExecutionInline(admin.TabularInline):
    model = CaseExecution
    extra = 0

@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'requirement', 'priority', 'status', 'creator')
    list_filter = ('project', 'status', 'priority', 'type')
    search_fields = ('title', 'steps')
    inlines = [CaseExecutionInline]
