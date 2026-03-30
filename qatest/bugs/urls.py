from django.urls import path
from . import views

urlpatterns = [
    path('', views.bug_list, name='bug_list'),
    path('create/', views.bug_create, name='bug_create'),
    path('<int:pk>/', views.bug_detail, name='bug_detail'),
    path('<int:pk>/edit/', views.bug_edit, name='bug_edit'),
    path('batch-delete/', views.bug_batch_delete, name='bug_batch_delete'),
]
