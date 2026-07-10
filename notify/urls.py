from django.urls import path

from . import views

urlpatterns = [
    path('', views.manage_rules, name='manage_notification_rules'),
    path('<int:rule_id>/edit', views.edit_rule, name='edit_notification_rule'),
    path('<int:rule_id>/delete', views.delete_rule, name='delete_notification_rule'),
    path('<int:rule_id>/test', views.test_rule, name='test_notification_rule'),
    path('log/', views.notification_log, name='notification_log'),
]
