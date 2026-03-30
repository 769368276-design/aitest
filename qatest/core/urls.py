from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('diagnostics/', views.diagnostics, name='diagnostics'),
    path('diagnostics/browser-check/', views.diagnostics_browser_check, name='diagnostics_browser_check'),
    path('diagnostics/media-check/', views.diagnostics_media_check, name='diagnostics_media_check'),
]
