from django.urls import path
from . import views

urlpatterns = [
    path('', views.requirement_list, name='requirement_list'),
    path('create/', views.requirement_create, name='requirement_create'),
    path('<int:pk>/', views.requirement_detail, name='requirement_detail'),
    path('<int:pk>/edit/', views.requirement_edit, name='requirement_edit'),
    path('<int:pk>/delete/', views.requirement_delete, name='requirement_delete'),
    path('bulk-delete/', views.requirement_bulk_delete, name='requirement_bulk_delete'),
]
