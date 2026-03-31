from django.contrib import admin

from uiauto.models import UIAutoExecution, UIAutoStepRecord


@admin.register(UIAutoExecution)
class UIAutoExecutionAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "status", "executor", "start_time", "end_time")
    list_filter = ("status", "start_time")
    search_fields = ("case__title", "executor__username")


@admin.register(UIAutoStepRecord)
class UIAutoStepRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "execution", "step_number", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("execution__case__title", "description")

