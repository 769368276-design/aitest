from django.contrib import admin
from .models import AutoTestExecution, AutoTestStepRecord, AutoTestNetworkEntry, AutoTestSchedule

@admin.register(AutoTestExecution)
class AutoTestExecutionAdmin(admin.ModelAdmin):
    list_display = ('id', 'case', 'status', 'executor', 'start_time')
    list_filter = ('status', 'start_time')

@admin.register(AutoTestStepRecord)
class AutoTestStepRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'execution', 'step_number', 'status', 'created_at')
    list_filter = ('status', 'created_at')

@admin.register(AutoTestNetworkEntry)
class AutoTestNetworkEntryAdmin(admin.ModelAdmin):
    list_display = ('id', 'url', 'method', 'status_code', 'timestamp')


@admin.register(AutoTestSchedule)
class AutoTestScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "enabled", "schedule_type", "project", "next_run_at", "last_run_at", "created_by", "created_at")
    list_filter = ("enabled", "schedule_type", "project")
