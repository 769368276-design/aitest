from django.contrib import admin
from .models import Bug, BugAttachment

class BugAttachmentInline(admin.TabularInline):
    model = BugAttachment
    extra = 1

@admin.register(Bug)
class BugAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'severity', 'priority', 'status', 'assignee')
    list_filter = ('project', 'status', 'severity', 'priority')
    search_fields = ('title', 'description')
    inlines = [BugAttachmentInline]
