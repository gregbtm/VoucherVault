from django.urls import path

from . import views

urlpatterns = [
    path('', views.upload_import, name='upload_import'),
    path('jobs/<uuid:job_id>/', views.import_job_status, name='import_job_status'),
    path('export/csv/', views.export_csv, name='export_csv'),
    path('export/json/', views.export_json, name='export_json'),
    path('export/full-backup/', views.export_full_backup_view, name='export_full_backup'),
    path('import/full-backup/', views.import_full_backup_view, name='import_full_backup'),
]
