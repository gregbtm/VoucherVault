from django.urls import path
from . import views

app_name = 'dms'

urlpatterns = [
    path('', views.providers, name='providers'),
    path('add/', views.add_provider, name='add_provider'),
    path('<uuid:provider_id>/edit/', views.edit_provider, name='edit_provider'),
    path('<uuid:provider_id>/delete/', views.delete_provider, name='delete_provider'),

    # AJAX
    path('<uuid:provider_id>/test/', views.test_connection, name='test_connection'),
    path('<uuid:provider_id>/poll/', views.poll_config, name='poll_config'),
    path('<uuid:provider_id>/browse/', views.browse, name='browse'),

    # Push
    path('<uuid:provider_id>/push/document/<int:document_id>/', views.push_document, name='push_document'),
    path('<uuid:provider_id>/push/item/<uuid:item_uuid>/', views.push_item_file, name='push_item_file'),

    # Pull
    path('<uuid:provider_id>/pull/', views.pull_document, name='pull_document'),

    # Logs
    path('logs/', views.sync_logs, name='sync_logs'),
]
