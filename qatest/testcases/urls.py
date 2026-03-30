from django.urls import path
from . import views

urlpatterns = [
    path('', views.case_list, name='case_list'),
    path('create/', views.case_create, name='case_create'),
    path('<int:pk>/', views.case_detail, name='case_detail'),
    path('<int:pk>/edit/', views.case_edit, name='case_edit'),
    path('<int:pk>/copy/', views.case_copy, name='case_copy'),
    path('<int:pk>/convert-advanced/', views.case_convert_advanced, name='case_convert_advanced'),
    path('<int:pk>/delete/', views.delete_case, name='delete_case'),
    path('batch-delete/', views.case_batch_delete, name='case_batch_delete'),
    path('expand/generate/', views.expand_generate, name='expand_generate'),
    path('expand/import/', views.expand_import, name='expand_import'),
    path('expand/', views.expand_import, name='expand_case'),
    path('step/<int:step_id>/update/', views.update_step, name='update_step'),
    path('step/<int:step_id>/upload-guide/', views.upload_step_guide, name='upload_step_guide'),
    path('step/<int:step_id>/upload-transfer-file/', views.upload_step_transfer_file, name='upload_step_transfer_file'),
    path('case/<int:case_id>/add-step/', views.add_step, name='add_step'),
    path('step/<int:step_id>/delete/', views.delete_step, name='delete_step'),
]
