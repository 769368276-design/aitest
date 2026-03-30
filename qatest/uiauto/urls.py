from django.urls import path

from uiauto import views


urlpatterns = [
    path("", views.entry, name="uiauto_entry"),
    path("run/<int:case_id>/", views.run_case, name="uiauto_run_case"),
    path("batch-run/", views.batch_run, name="uiauto_batch_run"),
    path("console/<int:execution_id>/", views.console, name="uiauto_console"),
    path("status/<int:execution_id>/", views.status, name="uiauto_status"),
    path("pause/<int:execution_id>/", views.pause, name="uiauto_pause"),
    path("resume/<int:execution_id>/", views.resume, name="uiauto_resume"),
    path("stop/<int:execution_id>/", views.stop, name="uiauto_stop"),
    path("reports/", views.report_list, name="uiauto_report_list"),
    path("reports/<int:execution_id>/", views.report_detail, name="uiauto_report_detail"),
]

