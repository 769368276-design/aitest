from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='ai_index'),
    path('generate/', views.generate, name='ai_generate'),
    path('stop/', views.stop_generation, name='ai_stop'),
    path('import/', views.import_cases, name='ai_import'),
    path('export/excel/', views.export_excel, name='ai_export_excel'),
    path('export/xmind/', views.export_xmind, name='ai_export_xmind'),
    path('jobs/start/', views.job_start, name='ai_job_start'),
    path('jobs/<int:job_id>/status/', views.job_status, name='ai_job_status'),
    path('jobs/<int:job_id>/poll/', views.job_poll, name='ai_job_poll'),
    path('jobs/<int:job_id>/stop/', views.job_stop, name='ai_job_stop'),
    path('jobs/<int:job_id>/clear/', views.job_clear, name='ai_job_clear'),
    path('jobs/<int:job_id>/import/', views.job_import, name='ai_job_import'),
    path('jobs/<int:job_id>/export/excel/', views.job_export_excel, name='ai_job_export_excel'),
    path('jobs/<int:job_id>/export/xmind/', views.job_export_xmind, name='ai_job_export_xmind'),
]
