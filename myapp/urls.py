import uuid
from django.urls import path, include
from . import views
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth import views as auth_views
from django.contrib import admin

urlpatterns = (
    path("dashboard", views.dashboard, name="dashboard"),
    path('', views.show_items, name='show_items'),
    path('offline/', views.offline, name='offline'),
    path('ping/', views.ping, name='ping'),
    path('items/create/', views.create_item, name='create_item'),
    path('items/lookup-merchant-balance-url/', views.lookup_merchant_balance_url, name='lookup_merchant_balance_url'),
    path('items/check-duplicate/', views.check_duplicate_code, name='check_duplicate_code'),
    path('items/view/<uuid:item_uuid>', views.view_item, name='view_item'),
    path('items/edit/<uuid:item_uuid>', views.edit_item, name='edit_item'),
    path('items/duplicate/<uuid:item_uuid>', views.duplicate_item, name='duplicate_item'),
    path('items/delete/<uuid:item_uuid>', views.delete_item, name='delete_item'),
    path('items/toggle_status/<uuid:item_id>', views.toggle_item_status, name='toggle_item_status'),
    path('items/toggle_pin/<uuid:item_uuid>', views.toggle_pin_item, name='toggle_pin_item'),
    path('items/toggle_archive/<uuid:item_uuid>', views.toggle_archive_item, name='toggle_archive_item'),
    path('items/bulk-archive/', views.bulk_archive_items, name='bulk_archive_items'),
    path('items/bulk-delete/', views.bulk_delete_items, name='bulk_delete_items'),
    path('items/bulk-tag/', views.bulk_tag_items, name='bulk_tag_items'),
    path('items/bulk-move/', views.bulk_move_items, name='bulk_move_items'),
    path('items/share/<uuid:item_id>', views.share_item_view, name='share_item'),
    path('items/unshare/<uuid:item_id>/<int:user_id>', views.unshare_item, name='unshare_item'),
    path('items/<uuid:item_id>/public-share/', views.get_public_share_link, name='get_public_share_link'),
    path('items/<uuid:item_id>/public-share/regenerate/', views.regenerate_public_share_link, name='regenerate_public_share_link'),
    path('items/<uuid:item_id>/public-share/revoke/', views.revoke_public_share_link, name='revoke_public_share_link'),
    path('items/<uuid:item_id>/share-logo/', views.item_share_logo, name='item_share_logo'),
    path('s/<uuid:share_id>/', views.public_item_share, name='public_item_share'),
    path('s/<uuid:share_id>/logo/', views.public_item_share_logo, name='public_item_share_logo'),
    path('s/<uuid:share_id>/pkpass/', views.public_item_pkpass, name='public_item_pkpass'),
    path('items/view-image/<uuid:item_id>/', views.serve_image_file, name='serve_image_file'),
    path('items/<uuid:item_uuid>/documents/upload', views.upload_document, name='upload_document'),
    path('documents/<int:document_id>/download', views.download_document, name='download_document'),
    path('documents/<int:document_id>/delete', views.delete_document, name='delete_document'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('post-logout/', views.post_logout, name='post_logout'),
    path('user/edit/notifications', views.update_apprise_urls, name='update_apprise_urls'),
    path('user/toggle_view_mode', views.toggle_view_mode, name='toggle_view_mode'),
    path('transactions/delete/<uuid:transaction_id>', views.delete_transaction, name='delete_transaction'),
    path('transactions/update-date/<uuid:transaction_id>', views.update_transaction_date, name='update_transaction_date'),
    path('verify-apprise-urls/', views.verify_apprise_urls, name='verify_apprise_urls'),
    path('download/<uuid:item_id>/', views.download_file, name='download_file'),
    path('shared-items/', views.sharing_center, name='sharing_center'),
    path('api/get/stats', views.get_stats, name='get_stats'),
    path('user/edit/preferences', views.update_user_preferences, name='update_user_preferences'),
    path('wallets/', views.manage_wallets, name='manage_wallets'),
    path('wallets/<int:wallet_id>/edit', views.edit_wallet, name='edit_wallet'),
    path('wallets/<int:wallet_id>/delete', views.delete_wallet, name='delete_wallet'),
    path('wallets/<int:wallet_id>/share', views.share_wallet, name='share_wallet'),
    path('wallets/<int:wallet_id>/unshare/<int:user_id>', views.unshare_wallet, name='unshare_wallet'),
    path('wallets/<int:wallet_id>/leave', views.leave_shared_wallet, name='leave_shared_wallet'),
    path('tags/', views.manage_tags, name='manage_tags'),
    path('tags/<int:tag_id>/edit', views.edit_tag, name='edit_tag'),
    path('tags/<int:tag_id>/delete', views.delete_tag, name='delete_tag'),
    path('calendar/download/', views.download_ics, name='download_ics'),
    path('calendar/regenerate-token/', views.regenerate_ics_token, name='regenerate_ics_token'),
    path('calendar/<uuid:token>.ics', views.ics_feed, name='ics_feed'),
    path('admin-tools/redeploy/', views.trigger_portainer_redeploy, name='trigger_portainer_redeploy'),
    path('admin-tools/check-for-updates/', views.trigger_update_check, name='trigger_update_check'),
    path('admin-tools/check-upstream/', views.trigger_upstream_check, name='trigger_upstream_check'),
    path('admin-tools/site-settings/', views.site_settings, name='site_settings'),
    path('admin-tools/help/<str:doc_slug>/', views.view_doc, name='view_doc'),
)

admin.site.site_header = "VoucherVault Plus+"
admin.site.site_title = "VoucherVault Plus+"
admin.site.index_title = "Welcome to VoucherVault Plus+"
