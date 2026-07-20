import os
import json
import logging
import secrets
import unicodedata
import mimetypes
import uuid
import datetime as dt
import requests
from django.db import IntegrityError
from django.db.models import Q
from django.utils.safestring import mark_safe
from .forms import *
from .ics_calendar import build_ics_calendar
from .models import *
from .utils import generate_code_image_base64, get_fixer_rates, convert_currency, levenshtein_distance
from .imagehash import compute_dhash, hamming_distance
from .scan_learning import record_scan_corrections
from django.db.models import Sum
from django.utils import timezone
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from csp.decorators import csp_replace
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework.authtoken.models import Token
from django.contrib import messages
from django.utils.timezone import now
from django.utils.http import url_has_allowed_host_and_scheme
from .decorators import require_authorization_header_with_api_token
from .analytics import build_expiry_calendar, get_active_today_item, get_expiring_soon_items, get_items_by_wallet, get_next_up_items
from .avatar import generate_initial_avatar, normalize_logo_image
from .merchant_logos import fetch_merchant_logo, get_cached_balance_check_url, get_cached_logo, get_cached_logos_for_issuers, merchant_logos_enabled, remember_balance_check_url
from .nearby_places import find_nearby_issuer_matches, nearby_places_enabled
from .portainer import PortainerRedeployError, trigger_redeploy
from .update_check import _is_newer, check_for_update, check_upstream_version
from .help_docs import render_doc
from .public_share import is_link_preview_bot, pin_attempt_rate_limited, view_rate_limited
from .tasks import extract_document_text_task, fetch_merchant_logo_task
from imports.exporters.google_wallet import generate_google_wallet_save_url, google_wallet_enabled
from imports.exporters.pkpass import generate_pkpass, pkpass_enabled
from imports.tasks import update_google_wallet_pass_task
from notify.tasks import notify_balance_changed, notify_item_archived, notify_item_created, notify_item_shared, notify_item_used, _find_firefly_rule
from .webhooks import fire_user_webhooks
from ocr.backends import ocr_enabled
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth
from django.db.models.functions import Coalesce, Lower, Trim
from django.db.models import Value
from django.utils.text import get_valid_filename

logger = logging.getLogger(__name__)

apprise_txt = _('Apprise URLs were already configured. Will not display them again here to protect secrets. You can freely re-configure the URLs now and hit update though.')

def _queue_google_wallet_update(item):
    """
    Fire-and-forget push of an item's balance/expiry/name/used-archived
    state to its already-issued Google Wallet object, wherever one of
    those fields just changed. A no-op when Google Wallet export isn't
    configured, and best-effort like fetch_merchant_logo_task - a broker
    outage shouldn't block the request that triggered it.
    """
    if not google_wallet_enabled():
        return
    try:
        update_google_wallet_pass_task.delay(item.id)
    except Exception:
        logger.warning('Could not queue Google Wallet update for item %s', item.id, exc_info=True)

def has_wallet_access(wallet, user):
    """True if `user` owns `wallet` or is a collaborator it's been shared with."""
    if wallet is None:
        return False
    return wallet.user_id == user.id or wallet.shared_with.filter(pk=user.id).exists()

def has_item_access(item, user):
    """
    True if `user` can view/edit this item: they own it, it was individually
    shared with them (ItemShare), or it lives in a wallet they collaborate on.
    """
    return (
        item.user == user
        or item.shared_with.filter(shared_with_user=user).exists()
        or has_wallet_access(item.wallet, user)
    )

@require_GET
def post_logout(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    else:
        return render(request, 'registration/post-logout.html')

@require_GET
def offline(request):
    """Offline page for PWA"""
    return render(request, 'offline.html')

@require_GET
def service_worker(request):
    """
    Overrides django-pwa's serviceworker view, which serves the file
    byte-for-byte with no templating. Interpolating settings.VERSION here
    ties the SW's cache name to the app version that CI already bumps on
    every merge, so a stale cache-first install always detects a changed
    script and rebuilds its caches on the next deploy instead of serving
    old CSS/logo/pages forever.
    """
    with open(settings.PWA_SERVICE_WORKER_PATH) as f:
        content = f.read().replace('__APP_VERSION__', settings.VERSION)
    return HttpResponse(content, content_type='application/javascript')

@require_GET
def ping(request):
    return HttpResponse('', status=204)

@require_GET
@login_required
def dashboard(request):
    user = request.user

    # Load preferences and site config once — analytics helpers accept explicit
    # values so they don't each fire their own SiteConfiguration.load() call.
    preferences, _ = UserPreference.objects.get_or_create(user=user)
    fixer_api_key = preferences.fixer_api_key
    default_currency = preferences.default_currency or 'GBP'

    site_cfg = SiteConfiguration.load()
    threshold_days = site_cfg.expiry_threshold_days
    now_dt = timezone.now()
    soon_expiry_date = now_dt + timedelta(days=threshold_days)

    # Single aggregate replaces 9 separate COUNT queries
    counts = Item.objects.filter(user=user).aggregate(
        total_items=Count('id', filter=Q(is_used=False)),
        available_items=Count('id', filter=Q(is_used=False, expiry_date__gte=now_dt)),
        used_items=Count('id', filter=Q(is_used=True)),
        expired_items=Count('id', filter=Q(is_used=False, expiry_date__lt=now_dt)),
        coupons_count=Count('id', filter=Q(is_used=False, type='coupon', expiry_date__gte=now_dt)),
        vouchers_count=Count('id', filter=Q(is_used=False, type='voucher', expiry_date__gte=now_dt)),
        giftcards_count=Count('id', filter=Q(is_used=False, type='giftcard', expiry_date__gte=now_dt)),
        loyaltycards_count=Count('id', filter=Q(is_used=False, type='loyaltycard', expiry_date__gte=now_dt)),
        soon_expiring_items=Count('id', filter=Q(is_used=False, expiry_date__gte=now_dt, expiry_date__lt=soon_expiry_date)),
    )
    total_items = counts['total_items']
    available_items = counts['available_items']
    used_items = counts['used_items']
    expired_items = counts['expired_items']
    coupons_count = counts['coupons_count']
    vouchers_count = counts['vouchers_count']
    giftcards_count = counts['giftcards_count']
    loyaltycards_count = counts['loyaltycards_count']
    soon_expiring_items = counts['soon_expiring_items']

    # Calculate the current total value of available money-type items
    items = Item.objects.with_current_balance().filter(
        user=user, is_used=False, value_type='money', expiry_date__gte=timezone.now()
    )
    items = items.exclude(type='loyaltycard')

    currencies_used = set(items.values_list('currency', flat=True).distinct())

    total_value = None
    total_currency = None
    at_risk_value = None
    currency_conversion_failed = False
    needs_fixer_key = False

    if currencies_used:
        if len(currencies_used) == 1:
            # All items share the same currency — sum directly
            single_currency = next(iter(currencies_used))
            total_value = 0
            at_risk_value = 0
            for item in items:
                item_value = float(item.current_balance)
                total_value += item_value
                if item.expiry_date < timezone.localtime(soon_expiry_date).date():
                    at_risk_value += item_value
            total_value = round(total_value, 2)
            at_risk_value = round(at_risk_value, 2)
            total_currency = single_currency
        elif fixer_api_key:
            # Mixed currencies — convert all to default_currency via Fixer.io
            rates = get_fixer_rates(fixer_api_key)
            if rates:
                total_value = 0
                at_risk_value = 0
                for item in items:
                    item_value = float(item.current_balance)
                    converted = convert_currency(item_value, item.currency, default_currency, rates)
                    if converted is None:
                        currency_conversion_failed = True
                        total_value = None
                        at_risk_value = None
                        break
                    total_value += converted
                    if item.expiry_date < timezone.localtime(soon_expiry_date).date():
                        at_risk_value += converted
                if total_value is not None:
                    total_value = round(total_value, 2)
                    at_risk_value = round(at_risk_value, 2)
                total_currency = default_currency
            else:
                currency_conversion_failed = True
        else:
            # Mixed currencies, no API key
            needs_fixer_key = True

    # Count the number of items shared by the user
    shared_items_count_by_you = ItemShare.objects.filter(shared_by=user).values('item').distinct().count()
    shared_items_count_with_you = ItemShare.objects.filter(
        shared_with_user=user,
        item__is_used=False,
        item__expiry_date__gte=timezone.localtime().date()
    ).exclude(item__user=user).values('item').distinct().count()

    # Pass site_cfg values explicitly so helpers skip their own SiteConfiguration.load() calls
    items_by_wallet = get_items_by_wallet(user, limit=site_cfg.wallet_chart_limit)
    expiring_soon_list = get_expiring_soon_items(
        user, days=threshold_days, limit=site_cfg.expiring_soon_limit
    )
    expiry_calendar = build_expiry_calendar(user, months_ahead=site_cfg.calendar_months_ahead)

    context = {
        'total_items': total_items,
        'available_items': available_items,
        'used_items': used_items,
        'total_value': total_value,
        'total_currency': total_currency,
        'at_risk_value': at_risk_value,
        'needs_fixer_key': needs_fixer_key,
        'currency_conversion_failed': currency_conversion_failed,
        'coupons_count': coupons_count,
        'vouchers_count': vouchers_count,
        'giftcards_count': giftcards_count,
        'loyaltycards_count': loyaltycards_count,
        'expired_items': expired_items,
        'soon_expiring_items': soon_expiring_items,
        'items_by_wallet': items_by_wallet,
        'wallet_chart_height': max(200, len(items_by_wallet) * 40 + 60),
        'expiring_soon_list': expiring_soon_list,
        'expiry_threshold_days': threshold_days,
        'expiry_calendar': expiry_calendar,
        'shared_items_count_by_you': shared_items_count_by_you,
        'shared_items_count_with_you': shared_items_count_with_you,
    }
    return render(request, 'dashboard.html', context)

def _get_wallet_budget(wallet_id, user):
    """
    Returns a dict {budget, spent, percent, remaining} for the selected wallet
    if it has a budget set, or None.  `spent` is the absolute value of all
    negative transactions on items in that wallet during the current calendar
    month.
    """
    if not wallet_id or not str(wallet_id).isdigit():
        return None
    try:
        wallet = Wallet.objects.get(pk=wallet_id, user=user)
    except Wallet.DoesNotExist:
        return None
    if not wallet.budget_amount:
        return None
    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = (
        Transaction.objects.filter(
            item__wallet=wallet,
            item__user=user,
            date__gte=month_start,
            value__lt=0,
        ).aggregate(total=Sum('value'))['total'] or 0
    )
    spent = abs(spent)
    budget = wallet.budget_amount
    percent = min(int(spent / budget * 100), 100) if budget else 0
    return {
        'budget': budget,
        'spent': spent,
        'remaining': max(budget - spent, 0),
        'percent': percent,
        'over_budget': spent > budget,
    }


@require_GET
@login_required
def show_items(request):
    user = request.user
    item_type = request.GET.get('type')
    filter_value = request.GET.get('status', 'available')  # Get the combined filter value
    search_query = request.GET.get('query', '')
    wallet_id = request.GET.get('wallet')
    tag_ids = [t for t in request.GET.getlist('tag') if t.isdigit()]

    # Retrieve or create user preferences (only once)
    preferences, _ = UserPreference.objects.get_or_create(user=user)

    next_up_items = get_next_up_items(preferences.next_up_wallets.all(), preferences.next_up_max_items)
    active_today_item = get_active_today_item(
        user, preferences.active_today_enabled, preferences.commute_home_station, preferences.active_today_cutoff_time,
    )

    # Calculate counts for filters (owned items plus items in wallets shared with the
    # user; archived items are hidden from every default view/count, only reachable
    # via the dedicated "Archived" filter)
    all_accessible_items = Item.objects.filter(Q(user=user) | Q(wallet__shared_with=user)).distinct()
    user_items = all_accessible_items.exclude(is_archived=True)
    threshold_days = SiteConfiguration.load().expiry_threshold_days
    now_dt = timezone.now()
    soon_expiry_date = now_dt + timedelta(days=threshold_days)

    # Two aggregates replace 9 separate COUNT queries.
    # COUNT(DISTINCT id) is correct for queries through the wallet M2M join.
    item_counts = user_items.aggregate(
        available=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt)),
        soon_expiring=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt, expiry_date__lt=soon_expiry_date)),
        used=Count('id', distinct=True, filter=Q(is_used=True)),
        expired=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__lt=now_dt)),
        voucher=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt, type='voucher')),
        giftcard=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt, type='giftcard')),
        coupon=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt, type='coupon')),
        loyaltycard=Count('id', distinct=True, filter=Q(is_used=False, expiry_date__gte=now_dt, type='loyaltycard')),
    )
    archived_count = all_accessible_items.filter(is_archived=True).distinct().count()

    available_count = item_counts['available']
    soon_expiring_count = item_counts['soon_expiring']
    used_count = item_counts['used']
    expired_count = item_counts['expired']
    voucher_count = item_counts['voucher']
    giftcard_count = item_counts['giftcard']
    coupon_count = item_counts['coupon']
    loyaltycard_count = item_counts['loyaltycard']

    # Base query
    if filter_value == 'shared_by_me':
        items = Item.objects.filter(shared_with__shared_by=user).exclude(is_archived=True).distinct()
    elif filter_value == 'shared_with_me':
        items = Item.objects.filter(
            shared_with__shared_with_user=user,
            is_used=False,
            expiry_date__gte=timezone.localtime().date()  # Only not expired
        ).exclude(user=user).exclude(is_archived=True).distinct()
    elif filter_value == 'archived':
        items = all_accessible_items.filter(is_archived=True)
    elif filter_value == 'soon_expiring':
        items = user_items.filter(is_used=False, expiry_date__gte=now(), expiry_date__lt=soon_expiry_date)
    else:
        items = user_items

        # Apply additional status filters to owned + shared-wallet items
        if filter_value == 'available':
            items = user_items.filter(is_used=False, expiry_date__gte=timezone.now()).distinct()
        elif filter_value == 'used':
            items = items.filter(is_used=True)
        elif filter_value == 'expired':
            items = items.filter(expiry_date__lt=timezone.now(), is_used=False)

    # Apply the item_type filter if provided
    if item_type:
        items = items.filter(type=item_type)

    # Apply the wallet filter if provided
    if wallet_id:
        items = items.filter(wallet_id=wallet_id)

    # Apply the tag filter if provided — an item matching ANY selected tag
    # is included (OR), which is what users expect from clickable tag chips
    if tag_ids:
        items = items.filter(tags__id__in=tag_ids).distinct()

    # Apply search query filter if provided
    if search_query:
        items = items.filter(
            Q(name__icontains=search_query) |
            Q(issuer__icontains=search_query) |
            Q(redeem_code__icontains=search_query) |
            Q(card_number__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(notes__icontains=search_query)
        )

    # Apply sorting based on user preference
    sort_by = preferences.sort_by
    sort_order = preferences.sort_order
    
    # Determine sort direction
    order_prefix = '-' if sort_order == 'desc' else ''
    
    # Apply sorting - pinned items first, then by user preference
    items = items.with_current_balance().select_related('wallet').prefetch_related('tags') \
        .order_by('-is_pinned', f'{order_prefix}{sort_by}')

    items_with_qr = []
    issuers = [i.issuer for i in items]
    issuers.extend(i.issuer for i in next_up_items)
    if active_today_item:
        issuers.append(active_today_item.issuer)
    merchant_logos = get_cached_logos_for_issuers(issuers)

    for item in items:
        items_with_qr.append({
            'item': item,
            'qr_code_base64': item.qr_code_base64,
            'current_value': item.current_balance,
            'merchant_logo_url': merchant_logos.get(item.issuer.strip().lower()),
        })

    next_up_with_logos = [
        {'item': item, 'merchant_logo_url': merchant_logos.get(item.issuer.strip().lower())}
        for item in next_up_items
    ]

    active_today_with_logo = None
    if active_today_item:
        active_today_with_logo = {
            'item': active_today_item,
            'merchant_logo_url': merchant_logos.get(active_today_item.issuer.strip().lower()),
        }

    context = {
        'items_with_qr': items_with_qr,
        'next_up_items': next_up_with_logos,
        'active_today_item': active_today_with_logo,
        'item_type': item_type,  # Add the item_type to the context
        'item_status': filter_value,  # Reuse item_status to hold the combined filter value
        'search_query': search_query,
        'current_date': timezone.now(),
        'preferences': preferences,
        # Filter counts
        'available_count': available_count,
        'soon_expiring_count': soon_expiring_count,
        'used_count': used_count,
        'expired_count': expired_count,
        'archived_count': archived_count,
        # Type counts
        'voucher_count': voucher_count,
        'giftcard_count': giftcard_count,
        'coupon_count': coupon_count,
        'loyaltycard_count': loyaltycard_count,
        'all_types_count': available_count,
        # Wallet filter
        'wallets': Wallet.objects.filter(Q(user=user) | Q(shared_with=user)).distinct().annotate(item_count=Count('items')),
        'selected_wallet_id': int(wallet_id) if wallet_id and wallet_id.isdigit() else None,
        'wallet_budget': _get_wallet_budget(wallet_id, user),
        # Tag filter — counts reflect the user's own non-archived accessible
        # items, same base set "All Items" browses by default
        'all_tags': Tag.objects.filter(user=user).annotate(
            item_count=Count('items', filter=Q(items__in=user_items), distinct=True)
        ).order_by('name'),
        'selected_tag_ids': [int(t) for t in tag_ids],
        # Appended to the static status/type filter chip hrefs so switching
        # status or type doesn't silently drop the active tag filter.
        # tag_ids is already validated as digit-only strings, safe to mark.
        'tag_query_params': mark_safe(''.join(f'&tag={t}' for t in tag_ids)),  # nosec
    }
    return render(request, 'inventory.html', context)


@require_GET
@login_required
def expiry_timeline(request):
    """Expiry timeline view: items grouped into time bands for a quick at-a-glance."""
    user = request.user
    today = timezone.localtime().date()
    in_7 = today + dt.timedelta(days=7)
    in_30 = today + dt.timedelta(days=30)
    in_90 = today + dt.timedelta(days=90)

    base = (
        Item.objects.filter(Q(user=user) | Q(wallet__shared_with=user))
        .filter(is_used=False, is_archived=False, expiry_date__gte=today)
        .distinct()
        .select_related('wallet')
        .prefetch_related('tags')
        .order_by('expiry_date', 'name')
    )

    # Evaluate once — splitting in Python avoids 4 separate DB round-trips
    all_items = list(base)
    bands = [
        {'label': 'This week',       'items': [i for i in all_items if i.expiry_date <= in_7]},
        {'label': 'This month',      'items': [i for i in all_items if in_7 < i.expiry_date <= in_30]},
        {'label': 'Next 3 months',   'items': [i for i in all_items if in_30 < i.expiry_date <= in_90]},
        {'label': 'Beyond 3 months', 'items': [i for i in all_items if i.expiry_date > in_90]},
    ]

    issuers = [item.issuer for band in bands for item in band['items']]
    merchant_logos = get_cached_logos_for_issuers(issuers)

    for band in bands:
        band['items_with_logos'] = [
            {'item': item, 'merchant_logo_url': merchant_logos.get(item.issuer.strip().lower())}
            for item in band['items']
        ]

    return render(request, 'expiry-timeline.html', {
        'bands': bands,
        'today': today,
        'current_date': timezone.now(),
    })


@login_required
def view_item(request, item_uuid):
    item = get_object_or_404(Item.objects.select_related('wallet', 'user'), id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    # True only for the item's creator: gates owner-only actions like
    # individually sharing (ItemShare) or duplicating the item.
    is_owner = item.user == request.user
    # True for the creator and for wallet collaborators: gates edit/delete/
    # add-transaction actions, which a shared wallet grants read/write for.
    can_edit = is_owner or has_wallet_access(item.wallet, request.user)

    if request.method == 'GET':
        Item.objects.filter(pk=item.pk).update(last_used_at=timezone.now())
        item.last_used_at = timezone.now()

    # Check if the item has been shared
    is_shared = item.shared_with.exists()

    transactions = item.transactions.all()
    total_value = item.get_current_balance(transactions)

    if request.method == 'POST':
        if not can_edit:
            # Read-only viewers should not be able to make POST requests (e.g., add transactions)
            return redirect('view_item', item_uuid=item.id)

        form = TransactionForm(request.POST, item=item)
        if form.is_valid():
            transaction = form.save(commit=False)
            transaction.item = item
            transaction.save()
            total_value += transaction.value
            notify_balance_changed(item, transaction)
            fire_user_webhooks(item.user, 'item_balance_changed', item)

            if total_value <= 0:
                item.is_used = True
                item.save()
                notify_item_used(item)
                fire_user_webhooks(item.user, 'item_used', item)
            _queue_google_wallet_update(item)
            return redirect('view_item', item_uuid=item.id)
    else:
        form = TransactionForm(item=item)

    cached_merchant = get_cached_logo(item.issuer)
    preferences, _ = UserPreference.objects.get_or_create(user=request.user)

    google_wallet_save_url = None
    if google_wallet_enabled():
        try:
            google_wallet_save_url = generate_google_wallet_save_url(item)
        except Exception as exc:
            logger.warning('Google Wallet link generation failed for item %s: %s', item.id, exc, exc_info=True)

    firefly_url = None
    firefly_synced_count = 0
    firefly_pending_count = 0
    if item.firefly_account_id:
        firefly_rule = _find_firefly_rule(item)
        if firefly_rule:
            base_url = (firefly_rule.config.get('url') or '').rstrip('/')
            if base_url:
                firefly_url = f'{base_url}/accounts/{item.firefly_account_id}'
        for tx in transactions:
            if tx.firefly_transaction_id:
                firefly_synced_count += 1
            else:
                firefly_pending_count += 1

    context = {
        'item': item,
        'transactions': transactions,
        'total_value': total_value,
        'qr_code_base64': item.qr_code_base64,
        'form': form,
        'current_date': timezone.now(),
        'is_owner': is_owner,
        'can_edit': can_edit,
        'is_shared': is_shared,
        'merchant_logo_url': cached_merchant.logo_url if cached_merchant else None,
        'pkpass_enabled': pkpass_enabled(),
        'google_wallet_save_url': google_wallet_save_url,
        'document_form': DocumentForm(),
        'preferences': preferences,
        'public_share': ItemPublicShare.objects.filter(item=item).first(),
        'firefly_url': firefly_url,
        'firefly_synced_count': firefly_synced_count,
        'firefly_pending_count': firefly_pending_count,
    }
    return render(request, 'view-item.html', context)


def _known_issuers(user):
    """
    A user's own past issuer names, for an autocomplete on the Issuer field.
    Manually typing "Amazon" one time and "Amazom" the next silently splits
    what should be one merchant across two spellings - breaking merchant
    logo matching, the balance-check URL suggestion, and "value by issuer"
    analytics grouping, all of which key off this exact string. Scoped to
    the user's own items, not every issuer ever seen on the instance, so it
    doesn't leak other users' merchant names in a shared/multi-user setup.
    """
    return list(
        Item.objects.filter(user=user).exclude(issuer='').order_by('issuer')
        .values_list('issuer', flat=True).distinct()
    )

def _record_scan_learning(request, item):
    """
    If this save round-tripped through an AI photo scan (the form JS
    captures the raw extraction into ai_scan_snapshot), diff it against
    what was actually saved so future scans can self-correct - see
    myapp/scan_learning.py. A missing/garbled snapshot just means nothing
    to learn from.
    """
    snapshot_raw = request.POST.get('ai_scan_snapshot', '')
    if not snapshot_raw:
        return
    try:
        snapshot = json.loads(snapshot_raw)
    except (ValueError, TypeError):
        return
    record_scan_corrections(request.user, snapshot, item)


@login_required
def create_item(request):
    if request.method == 'POST':
        form = ItemForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            item = form.save(commit=False)
            item.user = request.user  # Set the user from the session
            if not item.wallet_id:
                item.wallet = Wallet.match_for_issuer(request.user, item.issuer)

            try:
                item.qr_code_base64, item.code_type = generate_code_image_base64(item)
                item.file = None
                item.save()  # Save the item after generating the barcode
                form.save_m2m()  # Persist the selected tags (Item is now saved)
                for tag_name in form.cleaned_data.get('new_tags', []):
                    tag, _ = Tag.objects.get_or_create(user=request.user, name=tag_name)
                    item.tags.add(tag)
            except Exception as e:
                # Print the error for debugging and add a user-friendly error to the form
                form.add_error(None, f'Failed to generate barcode. Error: {str(e)}')
                form.add_error(None, f'Use the browser\'s back button to refill previous file uploads')
                # Return the form filled with the user's previously entered data and errors
                return render(request, 'create-item.html', {'form': form, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})

            # Handle file upload
            if 'file' in request.FILES:
                file = request.FILES['file']
                username = str(item.user)
                user_folder = os.path.join('uploads', username)
                
                raw_name = os.path.basename(file.name)
                safe_name = get_valid_filename(raw_name)
                file_name = f"{item.id}_{safe_name}"
                relative_path = os.path.join(user_folder, file_name)
                item.file.save(relative_path, file)

            if item.issuer:
                try:
                    fetch_merchant_logo_task.delay(item.issuer, item.logo_slug)
                except Exception:
                    # Best-effort: a broker outage shouldn't block saving the item.
                    logger.warning('Could not queue merchant logo fetch for %r', item.issuer, exc_info=True)

            remember_balance_check_url(item.issuer, item.balance_check_url)
            _record_scan_learning(request, item)
            notify_item_created(item)
            fire_user_webhooks(item.user, 'item_created', item)

            return redirect('show_items')
        else:
            # If form is not valid, render the form with validation errors
            return render(request, 'create-item.html', {'form': form, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})
    else:
        # If not a POST request, initialize form with user's preferred currency
        preferences, _ = UserPreference.objects.get_or_create(user=request.user)
        initial = {'currency': preferences.default_currency or 'GBP'}

        # Pre-populate from Web Share Target params (PWA share_target in manifest.json)
        shared_title = request.GET.get('shared_title', '').strip()
        shared_text = request.GET.get('shared_text', '').strip()
        shared_url = request.GET.get('shared_url', '').strip()
        if shared_title:
            initial['name'] = shared_title
        if shared_text or shared_url:
            initial['notes'] = '\n'.join(filter(None, [shared_text, shared_url]))

        form = ItemForm(initial=initial, user=request.user)

    return render(request, 'create-item.html', {'form': form, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})

@login_required
def edit_item(request, item_uuid):
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)
    if not _check_item_edit_permission(item, request.user):
        return HttpResponse("Forbidden: viewer role cannot edit items", status=403)
    original_redeem_code = item.redeem_code # Store the original redeem code
    original_code_type = item.code_type  # Store the original code type
    old_file_path = item.file.path if item.file else None  # Store the old file path

    if request.method == 'POST':
        form = ItemForm(request.POST, request.FILES, instance=item, user=request.user)
        if form.is_valid():
            item = form.save(commit=False)

            # Check if redeem code has changed
            if original_code_type != item.code_type or original_redeem_code != item.redeem_code:
                # Generate new QR code or barcode and save it as base64
                try:
                    item.qr_code_base64, item.code_type = generate_code_image_base64(item)
                    item.save()  # Save the item after generating the barcode
                except Exception as e:
                    form.add_error(None, f'Failed to generate barcode. Error: {str(e)}')
                    # Return the form filled with the user's previously entered data and errors
                    return render(request, 'edit-item.html', {'form': form, 'item': item, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})
                    
            # Handle file upload
            if 'file' in request.FILES:
                file = request.FILES['file']
                username = str(item.user)
                user_folder = os.path.join('uploads', username)
                raw_name = os.path.basename(file.name)
                safe_name = get_valid_filename(raw_name)
                file_name = f"{item.id}_{safe_name}"
                relative_path = os.path.join(user_folder, file_name)

                # Delete the old file if it exists and a new file is provided
                if old_file_path and os.path.isfile(old_file_path):
                    os.remove(old_file_path)

                item.file.save(relative_path, file)

            item.save()
            form.save_m2m()  # Persist the selected tags
            for tag_name in form.cleaned_data.get('new_tags', []):
                tag, _ = Tag.objects.get_or_create(user=request.user, name=tag_name)
                item.tags.add(tag)

            if item.issuer:
                try:
                    fetch_merchant_logo_task.delay(item.issuer, item.logo_slug)
                except Exception:
                    # Best-effort: a broker outage shouldn't block saving the item.
                    logger.warning('Could not queue merchant logo fetch for %r', item.issuer, exc_info=True)

            remember_balance_check_url(item.issuer, item.balance_check_url)
            _record_scan_learning(request, item)
            _queue_google_wallet_update(item)

            return redirect('view_item', item_uuid=item.id)
    else:
        form = ItemForm(instance=item, user=request.user)

    return render(request, 'edit-item.html', {'form': form, 'item': item, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})

@require_GET
@login_required
def lookup_merchant_balance_url(request):
    """
    Read-only AJAX helper for the create/edit item forms: given an issuer
    name, returns any balance-check URL already remembered for that
    merchant (see remember_balance_check_url), so a gift card from a
    merchant you've entered one for before can suggest it automatically.
    """
    issuer = request.GET.get('issuer', '')
    return JsonResponse({'balance_check_url': get_cached_balance_check_url(issuer)})

_SUGGESTABLE_FIELDS = {'issuer', 'logo_slug', 'wallet', 'discount_applied'}


@require_GET
@login_required
def suggest_field_options(request):
    """
    Read-only AJAX helper backing the "suggest" button next to a handful
    of item-form fields (see field-suggest.js): given an item type and one
    of _SUGGESTABLE_FIELDS, returns up to 5 distinct values for that field
    drawn from this user's own recent items of that type, ranked by how
    often each value appears (ties broken by recency) so one odd one-off
    item doesn't outrank an actual habit. Interactive by design - the
    button that triggers this only ever appears next to a field the AI
    scan (or the user) has left blank, and picking a suggestion is always
    an explicit click, never a silent fill.
    """
    field = request.GET.get('field', '')
    item_type = request.GET.get('type', '')
    if field not in _SUGGESTABLE_FIELDS:
        return JsonResponse({'options': []})

    recent = list(
        Item.objects.filter(user=request.user, type=item_type)
        .select_related('wallet')
        .order_by('-created_at')[:25]
    )

    ranked = {}
    for index, item in enumerate(recent):
        if field == 'wallet':
            if not item.wallet_id:
                continue
            key = str(item.wallet_id)
            label = item.wallet.name
        else:
            raw = (getattr(item, field) or '').strip()
            if not raw:
                continue
            key = raw.lower()
            label = raw
        entry = ranked.setdefault(key, {'count': 0, 'index': index, 'label': label, 'value': key if field == 'wallet' else raw})
        entry['count'] += 1

    top = sorted(ranked.values(), key=lambda entry: (-entry['count'], entry['index']))[:5]
    return JsonResponse({'options': [{'value': entry['value'], 'label': entry['label']} for entry in top]})

@require_GET
@login_required
def check_duplicate_code(request):
    """
    Read-only AJAX helper for the create/edit item forms: warns (never
    blocks - some duplicates are intentional, e.g. a re-issued loyalty
    card) when the redeem code just typed or scanned matches an active
    item the user already has access to. Scoped the same way show_items'
    "owned or shared-wallet" query is - a duplicate on someone else's
    item you can't see isn't actionable.
    """
    code = request.GET.get('code', '').strip()
    if not code:
        return JsonResponse({'duplicate': False})

    # Case/whitespace-normalized comparison - a code typed by hand and one
    # OCR-scanned off the same physical card can legitimately come back
    # with different casing (or stray whitespace) despite being the exact
    # same code, and an exact-string match would silently miss that.
    items = Item.objects.annotate(
        redeem_code_normalized=Lower(Trim('redeem_code')),
    ).filter(
        Q(user=request.user) | Q(wallet__shared_with=request.user),
        redeem_code_normalized=code.lower(), is_used=False, is_archived=False,
    ).distinct()
    exclude_id = request.GET.get('exclude', '')
    if exclude_id:
        items = items.exclude(id=exclude_id)

    match = items.first()
    if match:
        return JsonResponse({
            'duplicate': True,
            'item_name': match.name,
            'item_url': reverse('view_item', kwargs={'item_uuid': match.id}),
        })

    # No exact match - a softer "possible duplicate" nudge for a code
    # that's suspiciously close to one already in the vault (edit
    # distance <=2), the kind of gap an exact/normalized match can't
    # catch: two scans of the same physical card that land on genuinely
    # different strings because of a single misread character. Distance
    # threshold is intentionally tight (2, not more) and length-gated to
    # avoid flagging two legitimately different short/similar codes.
    candidates = Item.objects.filter(
        Q(user=request.user) | Q(wallet__shared_with=request.user),
        is_used=False, is_archived=False,
    ).distinct()
    if exclude_id:
        candidates = candidates.exclude(id=exclude_id)

    code_lower = code.lower()
    near_match = None
    near_distance = None
    for candidate in candidates.only('id', 'name', 'redeem_code'):
        candidate_code = candidate.redeem_code.strip().lower()
        if abs(len(candidate_code) - len(code_lower)) > 2:
            continue
        distance = levenshtein_distance(code_lower, candidate_code)
        if distance and distance <= 2 and (near_distance is None or distance < near_distance):
            near_match, near_distance = candidate, distance

    if near_match:
        return JsonResponse({
            'duplicate': False,
            'near_duplicate': True,
            'item_name': near_match.name,
            'item_url': reverse('view_item', kwargs={'item_uuid': near_match.id}),
        })
    return JsonResponse({'duplicate': False})


@require_POST
@login_required
def check_duplicate_image(request):
    """
    AJAX helper mirroring check_duplicate_code, but for the photo itself
    rather than the extracted text: warns when the uploaded image looks
    like one already attached to an active item the user has access to.
    Catches the case an OCR-text comparison structurally can't - the same
    physical card re-scanned from the same photo, where the AI extraction
    itself misread a character and produced a genuinely different code
    each time (see FORK_CHANGES.md's OCR non-determinism fix).
    """
    upload = request.FILES.get('image')
    if not upload:
        return JsonResponse({'duplicate': False})

    new_hash = compute_dhash(upload.read())
    if not new_hash:
        return JsonResponse({'duplicate': False})

    items = Item.objects.filter(
        Q(user=request.user) | Q(wallet__shared_with=request.user),
        is_used=False, is_archived=False,
    ).exclude(file='').distinct()
    exclude_id = request.GET.get('exclude', '')
    if exclude_id:
        items = items.exclude(id=exclude_id)

    duplicate_threshold = SiteConfiguration.load().duplicate_photo_threshold
    best_match = None
    best_distance = None
    for item in items:
        item_hash = item.image_phash
        if not item_hash:
            # Backfill lazily rather than via a bulk migration - a
            # heavy one-time read-every-file-in-storage pass is more
            # failure-prone than computing it the first time it's
            # actually needed for a comparison, and it's a no-op on
            # every subsequent check once every active item has one.
            try:
                item.file.open('rb')
                item_hash = compute_dhash(item.file.read())
                item.file.close()
            except Exception:
                continue
            if item_hash:
                item.image_phash = item_hash
                item.save(update_fields=['image_phash'])
        distance = hamming_distance(new_hash, item_hash)
        if distance <= duplicate_threshold and (best_distance is None or distance < best_distance):
            best_match, best_distance = item, distance

    if not best_match:
        return JsonResponse({'duplicate': False})
    return JsonResponse({
        'duplicate': True,
        'item_name': best_match.name,
        'item_url': reverse('view_item', kwargs={'item_uuid': best_match.id}),
    })

@require_GET
@login_required
def duplicate_item(request, item_uuid):
    original_item = get_object_or_404(Item, id=item_uuid, user=request.user)

    # Prepopulate the form with original item's data
    initial_data = {
        'name': original_item.name,
        'issuer': original_item.issuer,
        'redeem_code': original_item.redeem_code,
        'card_number': original_item.card_number,
        'pin': original_item.pin,
        'issue_date': original_item.issue_date,
        'expiry_date': original_item.expiry_date,
        'description': original_item.description,
        'logo_slug': original_item.logo_slug,
        'type': original_item.type,
        'value': original_item.value,
        'value_type': original_item.value_type,
        'code_type': original_item.code_type,
        'tile_color': original_item.tile_color,
    }

    form = ItemForm(initial=initial_data)
    return render(request, 'create-item.html', {
        'form': form,
        'ocr_enabled': ocr_enabled(),
        'known_issuers': _known_issuers(request.user),
    })

@require_POST
@login_required
def delete_item(request, item_uuid):
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)
    if not _check_item_edit_permission(item, request.user):
        return HttpResponse("Forbidden: viewer role cannot delete items", status=403)

    # Delete the associated file if it exists
    if item.file:
        if os.path.isfile(item.file.path):
            os.remove(item.file.path)

    item.delete()
    return redirect('show_items')

@require_POST
@login_required
def delete_transaction(request, transaction_id):
    transaction = get_object_or_404(Transaction, id=transaction_id)
    item = transaction.item
    # Delete the transaction
    transaction.delete()

    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def update_transaction_date(request, transaction_id):
    transaction = get_object_or_404(Transaction, id=transaction_id)
    if transaction.item.user != request.user:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    date_str = request.POST.get('date', '').strip()
    if not date_str:
        return JsonResponse({'error': 'Date is required'}, status=400)
    try:
        new_date = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
        transaction.date = timezone.make_aware(dt.datetime.combine(new_date, dt.time.min))
        transaction.save(update_fields=['date'])
        return JsonResponse({'date': transaction.date.strftime('%Y-%m-%d')})
    except ValueError:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

@require_GET
@login_required
def download_file(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    if item.file:
        file_name = os.path.basename(item.file.name)
        response = HttpResponse(item.file, content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{file_name}"'
        return response
    else:
        return HttpResponse("No file found", status=404)

def _previewable_mime_type(file_field, allow_pdf=False):
    """Mime type for a FileField if it's safe to render inline in the
    browser (an image, or a PDF when allow_pdf is set), else None."""
    if not file_field:
        return None
    mime_type, _ = mimetypes.guess_type(file_field.name)
    if not mime_type:
        return None
    if mime_type.startswith('image/') or (allow_pdf and mime_type == 'application/pdf'):
        return mime_type
    return None

@require_GET
@login_required
def serve_image_file(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    if not item.file:
        raise Http404("No file attached.")

    mime_type = _previewable_mime_type(item.file)
    if not mime_type:
        return HttpResponse("File is not an image", status=400)

    return HttpResponse(item.file, content_type=mime_type)

@require_GET
@login_required
@csp_replace({"frame-ancestors": ["'self'"]})
def view_original_file(request, item_id):
    """Serves the item's original upload inline (image or PDF) for the
    view-in-overlay button - unlike download_file, no attachment header.
    frame-ancestors is relaxed to 'self' just for this response, since the
    site's own CSP_FRAME_ANCESTORS default ('none') would otherwise block
    the page's own same-origin <iframe> preview of a PDF."""
    item = get_object_or_404(Item, id=item_id)

    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    mime_type = _previewable_mime_type(item.file, allow_pdf=True)
    if not mime_type:
        return HttpResponse("File cannot be previewed", status=400)

    return HttpResponse(item.file, content_type=mime_type)

@require_GET
@login_required
@csp_replace({"frame-ancestors": ["'self'"]})
def view_document_file(request, document_id):
    """Serves an attached receipt/document inline (image or PDF) for the
    view-in-overlay button - unlike download_document, no attachment header.
    See view_original_file for why frame-ancestors is relaxed here."""
    document = get_object_or_404(Document, id=document_id)

    if not has_item_access(document.item, request.user):
        return HttpResponse("Unauthorized", status=403)

    mime_type = _previewable_mime_type(document.file, allow_pdf=True)
    if not mime_type:
        return HttpResponse("File cannot be previewed", status=400)

    return HttpResponse(document.file, content_type=mime_type)

@require_POST
@login_required
def upload_document(request, item_uuid):
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    form = DocumentForm(request.POST, request.FILES)
    if form.is_valid():
        document = form.save(commit=False)
        document.item = item
        document.save()
        try:
            extract_document_text_task.delay(document.id)
        except Exception:
            pass
        messages.success(request, _('Document uploaded successfully!'))
    else:
        for error in form.errors.get('file', []):
            messages.error(request, error)

    return redirect('view_item', item_uuid=item.id)

@require_GET
@login_required
def download_document(request, document_id):
    document = get_object_or_404(Document, id=document_id)
    if not has_item_access(document.item, request.user):
        return HttpResponse("Unauthorized", status=403)

    file_name = os.path.basename(document.file.name)
    response = HttpResponse(document.file, content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{file_name}"'
    return response

@require_POST
@login_required
def delete_document(request, document_id):
    document = get_object_or_404(Document, id=document_id)
    item = document.item
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    if document.file and os.path.isfile(document.file.path):
        os.remove(document.file.path)
    document.delete()

    messages.success(request, _('Document deleted.'))
    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def toggle_item_status(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if not _check_item_edit_permission(item, request.user):
        return HttpResponse("Forbidden", status=403)
    desc_txt = _('Marked as used, removing remaining value')

    if item.is_used:
        # If item is currently marked as used, re-toggle to available
        item.is_used = False

        # Remove the previously created "Mark as used" transaction
        transaction = Transaction.objects.filter(item=item, description=desc_txt).all()
        if transaction:
            transaction.delete()
    else:
        # If item is available, mark as used and create a transaction
        item.is_used = True
        value_to_remove = item.get_current_balance()

        transaction = Transaction(
            item=item,
            description=desc_txt,
            value=-value_to_remove  # This will be a negative value to reduce the item value
        )
        transaction.save()
        notify_item_used(item)
        fire_user_webhooks(item.user, 'item_used', item)

    item.save()
    _queue_google_wallet_update(item)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_used': item.is_used})

    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('view_item', item_uuid=item.id)

@login_required
def update_apprise_urls(request):
    user_profile = request.user.userprofile
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=user_profile)
        if form.is_valid():
            apprise_urls = form.cleaned_data['apprise_urls']
            
            if apprise_urls != apprise_txt:
                user_profile.apprise_urls = apprise_urls
                form.save()
            return redirect('show_items')  # Redirect to 'show_items' after saving
    else:
        # Mask the apprise_urls in the form
        initial_data = {
            'apprise_urls': apprise_txt if user_profile.apprise_urls else '',
        }
        form = UserProfileForm(instance=user_profile, initial=initial_data)
    return render(request, 'update_apprise_urls.html', {'form': form})

@require_POST
@login_required
def verify_apprise_urls(request):
    data = json.loads(request.body)
    apprise_urls = data.get('apprise_urls', '')
    apprise_error_msg = _('No Apprise URLs provided.')

    # if the user sent no apprise urls
    if not apprise_urls:
        return JsonResponse({'success': False, 'message': apprise_error_msg})

    # if the user just wants to test the previously configured apprise urls
    if apprise_urls == apprise_txt:
        user_settings = get_object_or_404(UserProfile, user=request.user)
        apprise_urls = user_settings.apprise_urls

    # obtain the individual apprise urls
    apprise_urls = apprise_urls.split(',')
    apobj = apprise.Apprise()
    invalid_urls = []

    for url in apprise_urls:
        url = url.strip()
        try:
            apobj.add(url)
        except apprise.AppriseAssetException:
            invalid_urls.append(url)

    if invalid_urls:
        apprise_error_msg = _('Invalid Apprise URLs:')
        return JsonResponse({'success': False, 'message': f'{apprise_error_msg}: {", ".join(invalid_urls)}'})

    # Send a test notification if all URLs are valid
    try:
        msg_body = _('This is an Apprise test notification.')
        msg_title = _('Test Notification by VoucherVault Plus+')
        msg_success = _('Test notification to at least one Apprise URL sent successfully.')
        msg_failure = _('Failed to send test notification for every Apprise URL given.')

        success = apobj.notify(
            body=msg_body,
            title=msg_title,
            notify_type=apprise.NotifyType.INFO
        )

        if success:
            return JsonResponse({'success': True, 'message': msg_success})
        else:
            return JsonResponse({'success': False, 'message': msg_failure})
        
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Failed to send test notification: {str(e)}'})

@login_required
def download_ics(request):
    """One-off .ics download for the logged-in user's active items."""
    calendar = build_ics_calendar(request.user, request)
    response = HttpResponse(calendar, content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="vouchervault.ics"'
    return response

def ics_feed(request, token):
    """
    Subscribe-able feed: the token in the URL is the auth, not a session -
    calendar apps fetch this URL directly, unauthenticated, on their own
    refresh schedule. No login_required by design.
    """
    profile = get_object_or_404(UserProfile, ics_token=token)
    calendar = build_ics_calendar(profile.user, request)
    response = HttpResponse(calendar, content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = 'inline; filename="vouchervault.ics"'
    return response

@require_POST
@login_required
def regenerate_ics_token(request):
    """Rotates the subscribe URL's secret, invalidating the old one (e.g. if it leaked)."""
    profile = request.user.userprofile
    profile.ics_token = uuid.uuid4()
    profile.save(update_fields=['ics_token'])
    messages.success(request, _('Your calendar feed link has been regenerated. Update it in any calendar apps you use.'))
    return redirect('upload_import')

@require_POST
@login_required
def trigger_portainer_redeploy(request):
    """
    Superuser-only "Redeploy now" button on the update-available banner
    (see myapp/portainer.py). Deliberately not gated behind an API
    token/permission class since it's a same-session web UI action, but
    is_superuser is checked explicitly here rather than relying on the
    button simply being hidden - the banner's visibility is a template
    concern, this is the actual authorization check.
    """
    if not request.user.is_superuser:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Only administrators can trigger a redeploy.'))}, status=403)
        messages.error(request, _('Only administrators can trigger a redeploy.'))
        return redirect('show_items')

    try:
        trigger_redeploy()
    except PortainerRedeployError as exc:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Redeploy request failed: %(error)s') % {'error': exc})}, status=503)
        messages.error(request, _('Redeploy request failed: %(error)s') % {'error': exc})
    else:
        if _wants_json(request):
            return JsonResponse({'success': True, 'message': str(_('Redeploy triggered.'))})
        messages.success(request, _('Redeploy triggered. The app will restart once Portainer finishes rebuilding.'))

    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(referer)
    return redirect('show_items')

@require_POST
@login_required
def trigger_update_check(request):
    """
    Superuser-only "Check for updates now" button - runs check_for_update()
    synchronously instead of waiting on the daily periodic task. Exists
    because the periodic task itself can silently never run at all on an
    already-initialized deployment (see docker/entrypoint.sh) - this gives
    a direct, immediate way to confirm the check itself actually works,
    independent of whether Celery Beat's schedule is registered correctly.

    Supports an AJAX JSON round-trip (see _wants_json) so the Site Settings
    page can show the result as a toast and update the version display in
    place, instead of a full page reload.
    """
    if not request.user.is_superuser:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Only administrators can check for updates.'))}, status=403)
        messages.error(request, _('Only administrators can check for updates.'))
        return redirect('show_items')

    check_for_update()
    status = UpdateCheckStatus.load()
    if status.update_available:
        message = _('Update available: %(version)s.') % {'version': status.latest_version}
    else:
        message = _('Checked for updates - you\'re on the latest version.')

    if _wants_json(request):
        return JsonResponse({
            'message': message,
            'installed_version': settings.VERSION,
            'latest_version': status.latest_version,
            'latest_release_url': status.latest_release_url,
            'update_available': status.update_available,
            'checked_at': status.checked_at.isoformat() if status.checked_at else None,
            'last_check_error': status.last_check_error,
        })

    messages.success(request, message)
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(referer)
    return redirect('site_settings')

@require_POST
@login_required
def trigger_upstream_check(request):
    """
    Superuser-only "Check now" button for the Upstream Sync card - same
    rationale as trigger_update_check above: the periodic task can
    silently never run at all on an already-initialized deployment, and
    unlike the fork's own update check, this section previously had no
    manual fallback, so its "GitHub connectivity" badge could get stuck
    on "Never checked" indefinitely with no way to confirm or fix it
    from the UI.
    """
    if not request.user.is_superuser:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Only administrators can check for updates.'))}, status=403)
        messages.error(request, _('Only administrators can check for updates.'))
        return redirect('show_items')

    check_upstream_version()
    status = UpstreamSyncStatus.load()
    if status.last_check_error:
        message = _('Could not reach GitHub: %(error)s') % {'error': status.last_check_error}
    else:
        message = _('Checked upstream - latest release is %(version)s.') % {'version': status.latest_version or '?'}

    if _wants_json(request):
        return JsonResponse({
            'message': message,
            'latest_version': status.latest_version,
            'latest_release_url': status.latest_release_url,
            'checked_at': status.checked_at.isoformat() if status.checked_at else None,
            'last_check_error': status.last_check_error,
            'upstream_behind': _is_newer(status.latest_version, settings.UPSTREAM_VERSION),
        })

    messages.success(request, message)
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(referer)
    return redirect('site_settings')

def _integration_status(config):
    """
    Cheap, synchronous readiness checks for the Site Settings page - "is
    this section actually usable right now" (e.g. does the certificate
    file this path points to actually exist), not just "are the fields
    filled in". No network calls here - the update-check status shown
    alongside these is read from the last background/manual check
    (myapp/update_check.py), never triggered by loading this page.
    """
    status = {'ocr': None, 'pkpass': None, 'google_wallet': None}

    if config.ocr_backend == 'claude':
        status['ocr'] = {
            'ready': bool(config.anthropic_api_key),
            'detail': _('Anthropic API key is set.') if config.anthropic_api_key else _('Missing Anthropic API key above.'),
        }
    elif config.ocr_backend == 'openai':
        status['ocr'] = {
            'ready': bool(config.openai_api_key),
            'detail': _('OpenAI API key is set.') if config.openai_api_key else _('Missing OpenAI API key above.'),
        }
    elif config.ocr_backend == 'tesseract':
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            status['ocr'] = {'ready': True, 'detail': _('tesseract binary found in the container.')}
        except Exception:
            status['ocr'] = {'ready': False, 'detail': _('tesseract binary not found in the container.')}

    if config.pkpass_cert_path:
        ready = pkpass_enabled()
        status['pkpass'] = {
            'ready': ready,
            'detail': _('Certificate file found - Apple Wallet export is live.') if ready
            else _('No certificate file found at that path yet - check the volume mount.'),
        }

    if config.google_wallet_service_account_key_path:
        ready = google_wallet_enabled()
        status['google_wallet'] = {
            'ready': ready,
            'detail': _('Service account key file found - Google Wallet export is live.') if ready
            else _('No key file found at that path yet - check the volume mount.'),
        }

    return status

# Docs about a personal integration any user can set up for their own
# account (their own API token, their own n8n/MCP client) - not gated to
# superuser like every other doc here, which are all server/deployment-
# level concerns (cert paths, service accounts, Portainer webhooks) only
# an admin should be looking at.
_SELF_SERVICE_DOCS = {'n8n', 'mcp-server'}


@login_required
def view_doc(request, doc_slug):
    """
    Renders one of docs/*.md in-app for the "?" help buttons next to Site
    Settings sections (superuser-only, since Site Settings itself is) and
    the API Access page (any logged-in user, see _SELF_SERVICE_DOCS above)
    - rendered locally (see help_docs.py) rather than out to GitHub so
    it's available on a fully offline deployment too.

    Supports an AJAX JSON round-trip (see _wants_json) so the help link can
    open the guide in an in-page modal instead of navigating away from
    whatever settings section the admin was reading - the full-page
    doc_viewer.html render below stays as a fallback for a direct link/
    bookmark, or if JS is unavailable.
    """
    if doc_slug not in _SELF_SERVICE_DOCS and not request.user.is_superuser:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Only administrators can view setup guides.'))}, status=403)
        messages.error(request, _('Only administrators can view setup guides.'))
        return redirect('show_items')

    result = render_doc(doc_slug)
    if result is None:
        if _wants_json(request):
            return JsonResponse({'error': str(_('Unknown help topic.'))}, status=404)
        raise Http404('Unknown help topic.')
    title, html = result
    if _wants_json(request):
        return JsonResponse({'title': title, 'body_html': html})
    return render(request, 'doc_viewer.html', {'title': title, 'body_html': html})

@login_required
def site_settings(request):
    """
    Superuser-only page for editing SiteConfiguration - everything that
    used to require going into Portainer and setting an env var (OCR
    backend/keys, PKPASS/Google Wallet config, notification defaults,
    backup schedule, update check, the Portainer webhook URL itself) now
    lives in the database and takes effect immediately, no redeploy
    needed. See myapp/models.py::SiteConfiguration for what's deliberately
    NOT here - bootstrap/infra settings a process needs before it can even
    reach the database stay Portainer/env-var only.
    """
    if not request.user.is_superuser:
        messages.error(request, _('Only administrators can view site settings.'))
        return redirect('show_items')

    config = SiteConfiguration.load()
    if request.method == 'POST':
        form = SiteConfigurationForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            if _wants_json(request):
                return JsonResponse({'success': True, 'message': str(_('Settings saved.'))})
            messages.success(request, _('Site settings saved. Changes take effect immediately.'))
            return redirect('site_settings')
        if _wants_json(request):
            return JsonResponse({'success': False, 'errors': form.errors.get_json_data(escape_html=False)}, status=400)
    else:
        form = SiteConfigurationForm(instance=config)

    return render(request, 'site_settings.html', {
        'form': form,
        'config': config,
        'update_check_status': UpdateCheckStatus.load(),
        'installed_version': settings.VERSION,
        'integration_status': _integration_status(config),
    })

@require_GET
@login_required
def sharing_center(request):
    current_user = request.user
    today = timezone.localtime().date()
    
    # Get filter and search parameters
    filter_type = request.GET.get('filter', 'all')
    search_query = request.GET.get('query', '').strip()

    # Retrieve or create user preferences
    preferences, _ = UserPreference.objects.get_or_create(user=current_user)

    shares = ItemShare.objects.filter(
        Q(shared_with_user=current_user) | Q(shared_by=current_user)
    ).select_related('item', 'shared_by', 'shared_with_user') \
     .prefetch_related('item__transactions') \
     .order_by('item__expiry_date')

    unique_items = {}

    for share in shares:
        item = share.item
        if item.id not in unique_items:
            current_value = item.get_current_balance()
            if share.shared_with_user == current_user and not item.is_used and item.expiry_date >= today:
                # You are the receiver
                unique_items[item.id] = {
                    'item': item,
                    'qr_code_base64': item.qr_code_base64,
                    'shared_by': share.shared_by,
                    'shared_with_me': True,
                    'current_value': current_value
                }
            elif share.shared_by == current_user:
                # You are the sender
                unique_items[item.id] = {
                    'item': item,
                    'qr_code_base64': item.qr_code_base64,
                    'shared_with_me': False,
                    'current_value': current_value
                }

    shared_items = list(unique_items.values())
    
    # Sort by expiry date
    shared_items.sort(key=lambda x: x['item'].expiry_date)
    
    total_count = len(shared_items)
    with_me_count = len([item for item in shared_items if item.get('shared_with_me', False)])
    by_me_count = len([item for item in shared_items if not item.get('shared_with_me', False)])
    
    # Apply filter
    if filter_type == 'with_me':
        shared_items = [item for item in shared_items if item.get('shared_with_me', False)]
    elif filter_type == 'by_me':
        shared_items = [item for item in shared_items if not item.get('shared_with_me', False)]
    
    # Apply search
    if search_query:
        shared_items = [
            item for item in shared_items 
            if search_query.lower() in item['item'].name.lower() 
            or search_query.lower() in item['item'].issuer.lower()
        ]

    return render(request, 'sharing_center.html', {
        'shared_items': shared_items,
        'current_date': timezone.now(),
        'preferences': preferences,
        'current_filter': filter_type,
        'search_query': search_query,
        'total_count': total_count,
        'with_me_count': with_me_count,
        'by_me_count': by_me_count,
    })

@login_required
def share_item_view(request, item_id):
    item = get_object_or_404(Item, id=item_id, user=request.user)

    if request.method == 'POST':
        selected_users = request.POST.getlist('shared_users')
        if selected_users:
            for user_id in selected_users:
                recipient = User.objects.get(id=user_id)
                _share, created = ItemShare.objects.get_or_create(item=item, shared_with_user=recipient, shared_by=request.user)
                if created:
                    notify_item_shared(item, recipient.username)
            messages.success(request, _('Item shared successfully!'))
        else:
            messages.error(request, _('Please select at least one user to share with.'))

        return redirect('view_item', item_uuid=item.id)

    users = User.objects.exclude(id=request.user.id)
    return render(request, 'share_item.html', {'item': item, 'users': users})

@require_POST
@login_required
def unshare_item(request, item_id, user_id):
    # Get the item and ensure the current user is the owner
    item = get_object_or_404(Item, id=item_id, user=request.user)
    
    # Find the ItemShare record for the specified user
    item_share = get_object_or_404(ItemShare, item=item, shared_with_user__id=user_id)

    # Delete the ItemShare record to unshare the item
    item_share.delete()
    
    # Display a success message
    messages.success(request, _("Item has been unshared successfully."))
    
    # Redirect back to the item view page
    return redirect('view_item', item_uuid=item.id)

def _public_share_payload(request, item, share):
    """
    JSON shape returned to voucher-share.js by both the get-or-create and
    regenerate endpoints - everything the "Share via..." chooser needs to
    build either a bare-link share or a full-details share client-side,
    without embedding the code/PIN/balance of every item in the page's
    HTML up front (e.g. on the Inventory grid, which can list hundreds).
    Deliberately does NOT include share.access_pin - see _create_public_share.
    """
    return {
        'url': request.build_absolute_uri(reverse('public_item_share', args=[share.id])),
        'merchant': item.issuer,
        'name': item.name,
        # Always the redeem code, never card_number - card_number is a
        # secondary "printed member/account number, if different" field
        # (see Item.card_number's help_text) that's already visible on the
        # item's own page and the public share link; the code actually
        # needed to redeem the voucher must never be the one silently
        # dropped from the shared text.
        'code': item.redeem_code,
        'pin': item.pin or '',
        'balance': str(item.get_current_balance()) if item.type == 'giftcard' else None,
        'currency': item.currency,
        'logo_image_url': request.build_absolute_uri(reverse('item_share_logo', args=[item.id])),
    }

def _resolve_merchant_share_image(item):
    """
    Returns (content_bytes, content_type) for whichever of these is
    available: the merchant's real logo (fetched here rather than handed
    to the caller as a bare third-party URL - see item_share_logo/
    public_item_share_logo below for why), or, if nothing can be
    resolved, a generated initial-letter avatar for the item's issuer.
    Shared by both the authenticated (item_share_logo) and public
    (public_item_share_logo) endpoints so they can't drift into different
    fallback behaviour.

    This always calls fetch_merchant_logo() synchronously (item.logo_slug,
    an OCR-extracted domain, is passed through as a hint when present)
    rather than only relying on the async fetch_merchant_logo_task queued
    on save: that task may never have had a chance to run (no worker, or
    the item predates logo_slug), may have run once with a worse guess
    before logo_slug existed and never been re-triggered since a save is
    the only thing that queues it again, or may have cached a result from
    the Clearbit/Google fallback before a logo.dev key was configured -
    none of which self-correct until something re-saves the item.
    fetch_merchant_logo() already skips its own network call when its
    cache is fresh, the domain hasn't changed, and no better source has
    newly become available, so this only actually hits the network on a
    genuine cache miss or one of those signals - not on every share.

    Deliberately never falls back to VoucherVault's own app icon - a
    share/link-preview showing our own branding in place of a specific
    merchant's would misrepresent whose voucher is being shared.

    The fetched logo is passed through avatar.normalize_logo_image before
    being returned - some sources (Google's favicon service especially)
    return whatever native resolution a domain's favicon actually has,
    often just 32-48px, which looks blockily pixelated once stretched to
    fill a chat bubble or share preview otherwise.
    """
    if merchant_logos_enabled():
        try:
            fetch_merchant_logo(item.issuer, domain_hint=item.logo_slug or None)
        except Exception:
            logger.warning('Synchronous merchant logo refresh failed for item %s', item.id, exc_info=True)

    cached_merchant = get_cached_logo(item.issuer)
    logo_url = cached_merchant.logo_url if cached_merchant else None

    if logo_url:
        try:
            response = requests.get(logo_url, timeout=5)
            if response.status_code == 200 and response.content:
                return normalize_logo_image(response.content), 'image/png'
        except requests.RequestException:
            logger.warning('Merchant logo proxy fetch failed for %r via %s', item.issuer, logo_url, exc_info=True)

    return generate_initial_avatar(item.issuer), 'image/png'

@require_GET
@login_required
def item_share_logo(request, item_id):
    """
    Proxies the merchant's logo image (or a generated fallback avatar)
    same-origin, for the "Share via..." chooser's image+details option
    (voucher-share.js) to fetch() into a Blob and attach as a real image
    file via the Web Share API - going straight to logo.clearbit.com/
    Google favicons from the browser risks a silent CORS failure depending
    on that host's response headers, and proxying it here also means it's
    gated by the same has_item_access check as everything else about this
    item.
    """
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    content, content_type = _resolve_merchant_share_image(item)
    response = HttpResponse(content, content_type=content_type)
    # Short-lived on purpose (not the usual day-long asset cache) - a
    # merchant's resolved image can change on its own (a logo.dev key
    # gets added, a stale wrong-domain guess gets corrected) without the
    # URL itself changing, and a long client-side cache would keep
    # serving the pre-fix bytes for the rest of that window regardless.
    response['Cache-Control'] = 'private, max-age=300'
    return response

@require_GET
def public_item_share_logo(request, share_id):
    """
    Same image as item_share_logo above, but reachable without login and
    keyed by share_id - this is what public_item.html's on-page <img> and
    og:image link-preview meta tag both point at, so an anonymous WhatsApp/
    Slack crawler (which never has a session) and the page itself always
    show the same merchant-relevant image. No PIN/expiry gate: the main
    public_item_share view already exposes this same image unconditionally
    across every one of its states (crawler preview, expired, PIN-required,
    unlocked) since it carries no sensitive data, just a brand image.
    """
    share = get_object_or_404(ItemPublicShare.objects.select_related('item'), id=share_id)
    if view_rate_limited(request, share.id):
        return HttpResponse('Too many requests', status=429)

    content, content_type = _resolve_merchant_share_image(share.item)
    response = HttpResponse(content, content_type=content_type)
    # See item_share_logo above for why this is short-lived rather than a
    # long asset cache. Note this only governs *our own* response caching
    # - it has no effect on a chat app's own separate server-side link-
    # preview cache (WhatsApp in particular can keep showing a preview it
    # already scraped for a previously-sent link regardless of this
    # header; sending the link again, or a freshly-regenerated one, is
    # what picks up a corrected image there).
    response['Cache-Control'] = 'public, max-age=300'
    return response

def _wants_json(request):
    """
    Both voucher-share.js (fetch, for the share chooser) and plain HTML
    forms (the "Public Share Link" card on the item detail page) hit these
    three endpoints. fetch() calls send this header explicitly so the view
    can return JSON to one and a normal redirect-with-message to the other.
    """
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'

def _create_public_share(item, user):
    """
    Builds a fresh ItemPublicShare with expiry/access-PIN computed from the
    current SiteConfiguration - centralized here so get_public_share_link
    and regenerate_public_share_link (which both create a share row) can't
    drift out of sync on this logic.

    The PIN is deliberately never returned to the caller for inclusion in
    the "Share via..." text/link (see _public_share_payload) - it's only
    ever surfaced via share.access_pin on the item detail page, for the
    owner to relay through a separate channel. Bundling it into the same
    message as the link would defeat the point of a second factor for the
    common case (message forwarded/leaked as a whole), even though it
    still helps against the other leak vectors (search indexing, a link
    scraped without its surrounding message, link-preview caching).
    """
    config = SiteConfiguration.load()
    expires_at = None
    if config.share_link_expiry_days:
        expires_at = timezone.now() + dt.timedelta(days=config.share_link_expiry_days)
    access_pin = f'{secrets.randbelow(10000):04d}' if config.share_link_pin_enabled else ''
    return ItemPublicShare.objects.create(
        item=item, created_by=user, expires_at=expires_at, access_pin=access_pin,
    )

@require_POST
@login_required
def get_public_share_link(request, item_id):
    """
    Get-or-create the one public share link for this item, for the "Share
    via... -> Share details" flow (voucher-share.js) and the "Create link
    now" button on the item detail page. Anyone who can already view the
    item (owner, ItemShare recipient, wallet collaborator) can fetch/
    create its link - same audience the "Share via..." button itself is
    already shown to.
    """
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
        logger.warning('get_public_share_link: user %s denied access to item %s', request.user, item_id)
        return HttpResponse("Unauthorized", status=403)

    try:
        try:
            share = item.public_share
        except ItemPublicShare.DoesNotExist:
            try:
                share = _create_public_share(item, request.user)
            except IntegrityError:
                # Lost a race with a concurrent request creating the same
                # OneToOne row - the other request's create() won, use it.
                share = item.public_share
        payload = _public_share_payload(request, item, share)
    except Exception:
        logger.exception('get_public_share_link failed for item %s (user %s)', item_id, request.user)
        raise
    if _wants_json(request):
        return JsonResponse(payload)
    messages.success(request, _('Public share link created.'))
    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def regenerate_public_share_link(request, item_id):
    """Invalidates the old public link (e.g. if it leaked) and issues a fresh one."""
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
        logger.warning('regenerate_public_share_link: user %s denied access to item %s', request.user, item_id)
        return HttpResponse("Unauthorized", status=403)

    try:
        ItemPublicShare.objects.filter(item=item).delete()
        share = _create_public_share(item, request.user)
        payload = _public_share_payload(request, item, share)
    except Exception:
        logger.exception('regenerate_public_share_link failed for item %s (user %s)', item_id, request.user)
        raise
    if _wants_json(request):
        return JsonResponse(payload)
    messages.success(request, _('Public share link regenerated. The old link no longer works.'))
    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def revoke_public_share_link(request, item_id):
    """Deletes the public link outright; the next 'Share details' tap creates a new one."""
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
        logger.warning('revoke_public_share_link: user %s denied access to item %s', request.user, item_id)
        return HttpResponse("Unauthorized", status=403)

    ItemPublicShare.objects.filter(item=item).delete()
    if _wants_json(request):
        return JsonResponse({'revoked': True})
    messages.success(request, _('Public share link revoked.'))
    return redirect('view_item', item_uuid=item.id)

@csrf_exempt
def public_item_share(request, share_id):
    """
    Read-only, unauthenticated redemption summary: merchant, code/card
    number, PIN, remaining balance, and the barcode/QR image. No
    login_required by design - the id in the URL is the auth, the same
    pattern as the .ics calendar feed's token. Deliberately excludes
    notes, documents, and any edit/delete affordance - this is a page for
    someone who was handed a voucher, not a VoucherVault account holder.

    csrf_exempt because the optional PIN-entry form below POSTs from a page
    with no authenticated session to forge actions against - the worst
    case of a forged cross-site POST here is a guessed PIN, which the rate
    limiter below already has to defend against regardless of CSRF.

    Layered protections, in the order they're checked:
    - Link-preview/crawler user agents (WhatsApp, Slack, etc.) get a
      metadata-only response - merchant name/logo for the preview card,
      never the code/PIN/balance - and don't count as a real view or need
      the PIN. Without this, sending the link via any of these apps would
      both leak the sensitive fields to that app's own fetch and inflate
      the "opened N times" counter before a human ever saw it.
    - An expired link (SiteConfiguration.share_link_expiry_days) shows a
      plain "expired" state instead of any item content.
    - An optional access PIN (SiteConfiguration.share_link_pin_enabled),
      checked with a constant-time comparison and a strict rate limit on
      wrong guesses, gates everything past that point for one browser
      session at a time.
    - A separate, looser rate limit caps how often the full content can be
      fetched at all, independent of the PIN.
    """
    try:
        share = ItemPublicShare.objects.select_related('item').get(id=share_id)
    except (ItemPublicShare.DoesNotExist, ValueError):
        return render(request, 'public_item_revoked.html', {}, status=410)
    item = share.item

    if is_link_preview_bot(request.META.get('HTTP_USER_AGENT', '')):
        return render(request, 'public_item.html', {
            'item': item, 'crawler_preview': True, 'share_id': share.id,
        })

    if share.is_expired():
        return render(request, 'public_item.html', {
            'item': item, 'expired': True, 'share_id': share.id,
        })

    unlock_key = f'unlocked_share_{share.id}'
    pin_error = None
    if share.access_pin and not request.session.get(unlock_key):
        if request.method == 'POST':
            if pin_attempt_rate_limited(request, share.id):
                pin_error = _('Too many attempts. Try again in a few minutes.')
            elif secrets.compare_digest(request.POST.get('access_pin', ''), share.access_pin):
                request.session[unlock_key] = True
            else:
                share.failed_pin_attempts += 1
                share.save(update_fields=['failed_pin_attempts'])
                pin_error = _('Incorrect code.')

        if not request.session.get(unlock_key):
            return render(request, 'public_item.html', {
                'item': item, 'pin_required': True, 'pin_error': pin_error,
                'share_id': share.id,
            })

    if view_rate_limited(request, share.id):
        return HttpResponse('Too many requests', status=429)

    google_wallet_save_url = None
    if google_wallet_enabled():
        try:
            google_wallet_save_url = generate_google_wallet_save_url(item)
        except Exception as exc:
            logger.warning('Public Google Wallet link generation failed for share %s: %s', share.id, exc, exc_info=True)

    share.record_view()
    return render(request, 'public_item.html', {
        'item': item,
        'share_id': share.id,
        'current_balance': item.get_current_balance(),
        'show_balance': item.type == 'giftcard',
        'pkpass_enabled': pkpass_enabled(),
        'google_wallet_save_url': google_wallet_save_url,
    })

def public_item_pkpass(request, share_id):
    """
    Apple Wallet .pkpass download for the public share page's "Add to Apple
    Wallet" button - a binary file, so unlike Google Wallet's save link
    (computed inline above) it needs its own endpoint. Only reachable once
    a visitor has already passed public_item_share's own checks: a crawler,
    an expired link, or a PIN-gated link that hasn't been unlocked in this
    session all get turned away here too, since this endpoint has no PIN
    form of its own to show them.
    """
    share = get_object_or_404(ItemPublicShare.objects.select_related('item'), id=share_id)
    item = share.item

    if is_link_preview_bot(request.META.get('HTTP_USER_AGENT', '')):
        return HttpResponse(status=403)
    if share.is_expired():
        return HttpResponse(status=403)
    if share.access_pin and not request.session.get(f'unlocked_share_{share.id}'):
        return HttpResponse(status=403)
    if view_rate_limited(request, share.id):
        return HttpResponse('Too many requests', status=429)
    if not pkpass_enabled():
        return HttpResponse(status=404)

    try:
        data = generate_pkpass(item)
    except Exception as exc:
        logger.warning('Public pkpass generation failed for share %s: %s', share.id, exc, exc_info=True)
        return HttpResponse(status=503)

    response = HttpResponse(data, content_type='application/vnd.apple.pkpass')
    response['Content-Disposition'] = f'attachment; filename="{item.id}.pkpass"'
    return response

# API

@require_authorization_header_with_api_token

def get_items_by_type(request, item_type):
    authenticate_general_api_key(request)
    items = Item.objects.filter(type=item_type).values()
    return JsonResponse(list(items), safe=False)

@require_authorization_header_with_api_token
def get_stats(request):
    try:
        username = request.GET.get('user', None)
        threshold_days = SiteConfiguration.load().expiry_threshold_days
        soon_expiry_date = now() + timedelta(days=threshold_days)

        if username:
            try:
                user = User.objects.get(username=username)
                items_query = Item.objects.filter(user=user)
                users_filtered = True
            except ObjectDoesNotExist:
                return JsonResponse({"error": f"User '{username}' not found."}, status=404)
        else:
            items_query = Item.objects.all()
            users_filtered = False

        # Only valid, unused, and non-expired items are used for transaction-based value calc
        items_with_transaction_values = items_query.filter(
            is_used=False, expiry_date__gte=now()
        ).with_current_balance()

        # Summing `current_balance` directly across items of different
        # currencies produces a meaningless number (see upstream
        # l4rm4nd/VoucherVault#135) - only do it when every item shares one
        # currency. For a mixed-currency queryset, convert via the
        # requested user's own Fixer.io key if one was requested (mirrors
        # the dashboard's own per-user conversion, see dashboard() above);
        # there's no single applicable key for a cross-user aggregate, so
        # in that case report the accurate per-currency breakdown instead
        # of a silently wrong total.
        currencies_used = set(items_with_transaction_values.values_list('currency', flat=True).distinct())
        total_value = 0.0
        total_currency = None
        total_value_by_currency = None
        currency_conversion_note = None

        if len(currencies_used) == 1:
            total_currency = next(iter(currencies_used))
            total_value = round(float(items_with_transaction_values.aggregate(
                total_value=Sum('current_balance'))['total_value'] or 0), 2)
        elif currencies_used:
            # current_balance is itself an aggregate annotation (from
            # with_current_balance()), so it can't be summed via a grouped
            # .values().annotate() call in one query (Django raises
            # "Cannot compute Sum('current_balance'): 'current_balance' is
            # an aggregate") - one plain .aggregate() per currency instead,
            # the same pattern the single-currency branch above already uses.
            by_currency = {}
            for currency in currencies_used:
                total = items_with_transaction_values.filter(currency=currency).aggregate(
                    total=Sum('current_balance'))['total']
                if total is not None:
                    by_currency[currency] = total
            total_value_by_currency = {currency: round(float(amount), 2) for currency, amount in by_currency.items()}

            fixer_api_key = None
            default_currency = None
            if username:
                prefs = UserPreference.objects.filter(user=user).first()
                if prefs:
                    fixer_api_key = prefs.fixer_api_key
                    default_currency = prefs.default_currency or 'GBP'

            total_value = None
            if fixer_api_key:
                rates = get_fixer_rates(fixer_api_key)
                if rates:
                    converted_total = 0.0
                    for currency, amount in by_currency.items():
                        converted = convert_currency(amount, currency, default_currency, rates)
                        if converted is None:
                            converted_total = None
                            break
                        converted_total += converted
                    if converted_total is not None:
                        total_value = round(converted_total, 2)
                        total_currency = default_currency
                    else:
                        currency_conversion_note = "Currency conversion failed for one or more currencies; see total_value_by_currency."
                else:
                    currency_conversion_note = "Currency conversion failed; see total_value_by_currency."
            else:
                currency_conversion_note = "Items span multiple currencies with no Fixer.io key available to convert them; see total_value_by_currency."

        # Item stats
        item_stats = {
            "total_items": items_query.count(),
            "total_value": total_value,
            "total_value_currency": total_currency,
            "vouchers": items_query.filter(type='voucher').count(),
            "giftcards": items_query.filter(type='giftcard').count(),
            "coupons": items_query.filter(type='coupon').count(),
            "loyaltycards": items_query.filter(type='loyaltycard').count(),
            "used_items": items_query.filter(is_used=True).count(),
            "available_items": items_query.filter(is_used=False).count() - items_query.filter(expiry_date__lt=now()).count(),
            "expired_items": items_query.filter(expiry_date__lt=now()).count(),
            "soon_expiring_items": items_query.filter(expiry_date__gte=now(), expiry_date__lt=soon_expiry_date).count(),
        }
        if total_value_by_currency is not None:
            item_stats["total_value_by_currency"] = total_value_by_currency
        if currency_conversion_note:
            item_stats["currency_conversion_note"] = currency_conversion_note

        # Return global user_stats
        user_stats = {
            "total_users": User.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "disabled_users": User.objects.filter(is_active=False).count(),
            "superusers": User.objects.filter(is_superuser=True).count(),
            "staff_members": User.objects.filter(is_staff=True).count(),
        }

        # Issuer stats - grouped by (issuer, currency) rather than issuer
        # alone, so total_value is always an accurate same-currency sum
        # without needing any conversion (the same issuer name can span
        # multiple currencies across different items/users).
        issuer_transaction_totals = (
            items_query.filter(is_used=False, expiry_date__gte=now())
            .values('issuer', 'currency')
            .annotate(
                transaction_total=Coalesce(
                    Sum('transactions__value', output_field=models.DecimalField()),
                    Value(0, output_field=models.DecimalField())
                )
            )
        )
        issuer_transaction_map = {
            (item['issuer'], item['currency']): item['transaction_total'] for item in issuer_transaction_totals
        }

        issuers = (
            items_query.filter(is_used=False, expiry_date__gte=now())
            .values('issuer', 'currency')
            .annotate(
                count=Count('issuer'),
                base_value=Coalesce(
                    Sum('value', output_field=models.DecimalField()),
                    Value(0, output_field=models.DecimalField())
                )
            )
            .order_by('-count')
        )

        issuer_stats = [
            {
                "issuer": issuer["issuer"],
                "currency": issuer["currency"],
                "count": issuer["count"],
                "total_value": round((issuer["base_value"] + issuer_transaction_map.get(
                    (issuer["issuer"], issuer["currency"]), 0)), 2),
            }
            for issuer in issuers
        ]

        # Individual item detail dump
        item_details = list(
            items_query.values(
                'id',
                'type',
                'name',
                'redeem_code',
                'code_type',
                'pin',
                'issuer',
                'value',
                'value_type',
                'currency',
                'issue_date',
                'expiry_date',
                'description',
                'is_used',
                'is_pinned',
                'user__username'
            )
        )

        response_data = {
            "item_stats": item_stats,
            "item_details": item_details,
            "issuer_stats": issuer_stats,
        }

        if user_stats is not None:
            response_data["user_stats"] = user_stats

        return JsonResponse(response_data, status=200)

    except Exception as e:
        return JsonResponse({"error": "An unexpected error occurred.", "details": str(e)}, status=500)

@login_required
def update_user_preferences(request):
    preferences, _ = UserPreference.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        was_offline_cache_enabled = preferences.offline_cache_enabled
        form = UserPreferenceForm(request.POST, instance=preferences)
        if form.is_valid():
            form.save()
            # A preference change can affect what /dashboard renders (e.g. the
            # Fixer.io API key warning), so tell the service worker to drop its
            # cached copy rather than waiting on the cache's own TTL. If offline
            # caching was just turned off, purge everything outright.
            redirect_url = reverse('show_items') + '?prefs_saved=1'
            if was_offline_cache_enabled and not form.cleaned_data['offline_cache_enabled']:
                redirect_url += '&cache_purge=1'
            return redirect(redirect_url)
    else:
        form = UserPreferenceForm(instance=preferences)

    return render(request, 'update_preferences.html', {'form': form})


@login_required
def api_access(request):
    """
    Lets a user generate/regenerate/revoke their own REST API token from
    the GUI - the same token type docs/N8N_SETUP.md and
    docs/MCP_SERVER_SETUP.md ask for, previously only obtainable via
    `docker compose exec app python manage.py drf_create_token` or POSTing
    a password to /api/v1/auth/token/.

    The raw key is only ever handed back to the browser immediately after
    a generate/regenerate action, via a one-shot session value popped (and
    so cleared) on the very next render - this view could technically read
    the key back out of the DB at any time (DRF's Token model stores it in
    plaintext, unlike a hashed password), but not resurfacing it on every
    page load limits shoulder-surfing/screen-share exposure the same way a
    GitHub/GitLab personal access token page does.
    """
    token = Token.objects.filter(user=request.user).first()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('generate', 'regenerate'):
            if token:
                token.delete()
            token = Token.objects.create(user=request.user)
            request.session['just_generated_api_token'] = token.key
            messages.success(
                request,
                _('API token regenerated - update anywhere the old one was used, it no longer works.')
                if action == 'regenerate' else _('API token generated.')
            )
        elif action == 'revoke' and token:
            token.delete()
            messages.success(request, _('API token revoked.'))
        return redirect('api_access')

    revealed_token = request.session.pop('just_generated_api_token', None)

    return render(request, 'api_access.html', {
        'token': token,
        'revealed_token': revealed_token,
    })


@require_POST
@login_required
def toggle_pin_item(request, item_uuid):
    """Toggle the pinned status of an item"""
    item = get_object_or_404(Item, id=item_uuid, user=request.user)
    item.is_pinned = not item.is_pinned
    item.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_pinned': item.is_pinned})

    # Support redirect back to previous page
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('show_items')

@require_POST
@login_required
def nearby_items(request):
    """
    One-shot proxy for the opt-in 'Nearby' widget: the client posts its
    current coordinates once (never stored, never logged - see
    nearby_places.py), the server checks OpenStreetMap for a matching shop
    near that point, and only the matched item IDs come back. Off by
    default at both the per-user (UserPreference.nearby_items_enabled) and
    site (SiteConfiguration.nearby_places_enabled) level.
    """
    preferences, _ = UserPreference.objects.get_or_create(user=request.user)
    if not preferences.nearby_items_enabled or not nearby_places_enabled():
        return JsonResponse({'items': []})

    try:
        lat = float(request.POST.get('lat', ''))
        lon = float(request.POST.get('lon', ''))
    except ValueError:
        return JsonResponse({'error': 'Invalid coordinates'}, status=400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return JsonResponse({'error': 'Invalid coordinates'}, status=400)

    candidates = Item.objects.filter(
        Q(user=request.user) | Q(wallet__shared_with=request.user),
        is_used=False, is_archived=False,
    ).exclude(issuer='').distinct()
    issuers = list({item.issuer for item in candidates})

    matched_issuers = find_nearby_issuer_matches(lat, lon, preferences.nearby_radius_m, issuers)
    if not matched_issuers:
        return JsonResponse({'items': []})

    matched_items = candidates.filter(issuer__in=matched_issuers).order_by('issuer', 'name')
    return JsonResponse({
        'items': [
            {
                'id': str(item.id),
                'name': item.name,
                'issuer': item.issuer,
                'url': reverse('view_item', kwargs={'item_uuid': item.id}),
            }
            for item in matched_items
        ],
    })

@require_POST
@login_required
def toggle_mute_notifications(request, item_uuid):
    """Toggle notifications_muted on an item. Only the item owner can mute."""
    item = get_object_or_404(Item, id=item_uuid, user=request.user)
    item.notifications_muted = not item.notifications_muted
    item.save(update_fields=['notifications_muted'])
    return JsonResponse({'success': True, 'notifications_muted': item.notifications_muted})


@require_POST
@login_required
def toggle_archive_item(request, item_uuid):
    """Toggle the archived status of an item: hides it from the default
    inventory views without marking it used or deleting it."""
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)
    if not _check_item_edit_permission(item, request.user):
        return HttpResponse("Forbidden: viewer role cannot archive items", status=403)

    item.is_archived = not item.is_archived
    item.save(update_fields=['is_archived'])
    if item.is_archived:
        notify_item_archived(item)
        fire_user_webhooks(item.user, 'item_archived', item)
    _queue_google_wallet_update(item)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'is_archived': item.is_archived})

    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('view_item', item_uuid=item.id)


def _bulk_selected_items(request):
    """
    Parses {"item_ids": [...]} from a JSON POST body. Items the user
    doesn't have access to are silently skipped rather than failing the
    whole batch - the same has_item_access gate every single-item action
    already uses. Returns (accessible_items, skipped_count, body_dict).
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        data = {}
    item_ids = data.get('item_ids') or []
    items = list(Item.objects.filter(id__in=item_ids))
    accessible = [item for item in items if _check_item_edit_permission(item, request.user)]
    skipped = len(item_ids) - len(accessible)
    return accessible, skipped, data

@require_POST
@login_required
def bulk_archive_items(request):
    """Same is_archived + notify_item_archived logic as toggle_archive_item, looped over a selection."""
    items, skipped, _data = _bulk_selected_items(request)
    processed = 0
    # Checked once rather than inside the loop - google_wallet_enabled() does a
    # SiteConfiguration lookup + filesystem stat, which can't change mid-request.
    wallet_updates_enabled = google_wallet_enabled()
    for item in items:
        if not item.is_archived:
            item.is_archived = True
            item.save(update_fields=['is_archived'])
            notify_item_archived(item)
            if wallet_updates_enabled:
                _queue_google_wallet_update(item)
            processed += 1
    return JsonResponse({'success': True, 'processed': processed, 'skipped': skipped})

@require_POST
@login_required
def bulk_delete_items(request):
    """Same file-cleanup + delete logic as delete_item, looped over a selection."""
    items, skipped, _data = _bulk_selected_items(request)
    for item in items:
        if item.file and os.path.isfile(item.file.path):
            os.remove(item.file.path)
        item.delete()
    return JsonResponse({'success': True, 'processed': len(items), 'skipped': skipped})

@require_POST
@login_required
def bulk_tag_items(request):
    """Same Tag.get_or_create + item.tags.add logic edit_item uses for its new_tags field."""
    items, skipped, data = _bulk_selected_items(request)
    tag_names = [name.strip() for name in (data.get('tags') or '').split(',') if name.strip()]
    if not tag_names:
        return JsonResponse({'success': False, 'message': _('No tags provided.')}, status=400)

    tags = [Tag.objects.get_or_create(user=request.user, name=name)[0] for name in tag_names]
    for item in items:
        item.tags.add(*tags)
    return JsonResponse({'success': True, 'processed': len(items), 'skipped': skipped})

@require_POST
@login_required
def bulk_move_items(request):
    """Same wallet assignment + scoping ItemForm already does (own wallets or ones shared with you)."""
    items, skipped, data = _bulk_selected_items(request)
    wallet_id = data.get('wallet_id')
    wallet = None
    if wallet_id:
        wallet = Wallet.objects.filter(Q(user=request.user) | Q(shared_with=request.user), pk=wallet_id).distinct().first()
        if wallet is None:
            return JsonResponse({'success': False, 'message': _('Wallet not found.')}, status=400)

    for item in items:
        item.wallet = wallet
        item.save(update_fields=['wallet'])
    return JsonResponse({'success': True, 'processed': len(items), 'skipped': skipped})

@require_POST
@login_required
def toggle_view_mode(request):
    """Toggle between compact and standard view modes"""
    preferences, _ = UserPreference.objects.get_or_create(user=request.user)
    preferences.view_mode = 'standard' if preferences.view_mode == 'compact' else 'compact'
    preferences.save()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'view_mode': preferences.view_mode})

    return redirect('show_items')

# --- Wallets ---

@login_required
def manage_wallets(request):
    """List, create wallets. Editing/deleting happens inline via the same page."""
    if request.method == 'POST':
        form = WalletForm(request.POST, user=request.user)
        if form.is_valid():
            wallet = form.save(commit=False)
            wallet.user = request.user
            wallet.save()
            messages.success(request, _('Wallet created successfully!'))
            return redirect('manage_wallets')
    else:
        form = WalletForm(user=request.user)

    wallets = Wallet.objects.filter(user=request.user).annotate(item_count=Count('items'))
    shared_wallets = Wallet.objects.filter(shared_with=request.user).annotate(item_count=Count('items'))
    return render(request, 'manage-wallets.html', {
        'form': form,
        'wallets': wallets,
        'shared_wallets': shared_wallets,
    })

@login_required
def edit_wallet(request, wallet_id):
    wallet = get_object_or_404(Wallet, id=wallet_id, user=request.user)
    if request.method == 'POST':
        form = WalletForm(request.POST, instance=wallet, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Wallet updated successfully!'))
            return redirect('manage_wallets')
    else:
        form = WalletForm(instance=wallet, user=request.user)

    wallets = Wallet.objects.filter(user=request.user).annotate(item_count=Count('items'))
    shared_wallets = Wallet.objects.filter(shared_with=request.user).annotate(item_count=Count('items'))
    return render(request, 'manage-wallets.html', {
        'form': form,
        'wallets': wallets,
        'shared_wallets': shared_wallets,
        'editing_wallet': wallet,
        'share_form': WalletShareForm(wallet=wallet),
    })

@require_POST
@login_required
def delete_wallet(request, wallet_id):
    wallet = get_object_or_404(Wallet, id=wallet_id, user=request.user)
    wallet.delete()  # Items in this wallet are kept; their `wallet` FK is set to NULL.
    messages.success(request, _('Wallet deleted. Its items were kept and unassigned from any wallet.'))
    return redirect('manage_wallets')

@require_POST
@login_required
def share_wallet(request, wallet_id):
    """Owner invites another user to collaborate on this wallet."""
    wallet = get_object_or_404(Wallet, id=wallet_id, user=request.user)
    form = WalletShareForm(request.POST, wallet=wallet)
    if form.is_valid():
        collaborator = form.cleaned_data['user']
        role = request.POST.get('role', WalletMembership.ROLE_EDITOR)
        if role not in (WalletMembership.ROLE_VIEWER, WalletMembership.ROLE_EDITOR):
            role = WalletMembership.ROLE_EDITOR
        wallet.shared_with.add(collaborator)
        WalletMembership.objects.update_or_create(
            wallet=wallet, user=collaborator,
            defaults={'role': role},
        )
        WalletActivity.objects.create(
            wallet=wallet, actor=request.user, action='member_added',
            detail=f"{collaborator.username} ({role})",
        )
        messages.success(request, _('Wallet shared with %(username)s.') % {'username': form.cleaned_data['username']})
    else:
        for error in form.errors.get('username', []):
            messages.error(request, error)
    return redirect('edit_wallet', wallet_id=wallet.id)

@require_POST
@login_required
def unshare_wallet(request, wallet_id, user_id):
    """Owner revokes a collaborator's access to this wallet."""
    wallet = get_object_or_404(Wallet, id=wallet_id, user=request.user)
    collaborator = get_object_or_404(User, id=user_id)
    wallet.shared_with.remove(collaborator)
    WalletMembership.objects.filter(wallet=wallet, user=collaborator).delete()
    WalletActivity.objects.create(
        wallet=wallet, actor=request.user, action='member_removed',
        detail=collaborator.username,
    )
    messages.success(request, _('Removed %(username)s from this wallet.') % {'username': collaborator.username})
    return redirect('edit_wallet', wallet_id=wallet.id)

@require_POST
@login_required
def leave_shared_wallet(request, wallet_id):
    """A collaborator removes themselves from a wallet shared with them."""
    wallet = get_object_or_404(Wallet, id=wallet_id, shared_with=request.user)
    wallet.shared_with.remove(request.user)
    WalletMembership.objects.filter(wallet=wallet, user=request.user).delete()
    messages.success(request, _('You have left the wallet "%(name)s".') % {'name': wallet.name})
    return redirect('manage_wallets')

# --- Tags ---

@login_required
def manage_tags(request):
    """List, create tags. Editing/deleting happens inline via the same page."""
    if request.method == 'POST':
        form = TagForm(request.POST, user=request.user)
        if form.is_valid():
            tag = form.save(commit=False)
            tag.user = request.user
            tag.save()
            messages.success(request, _('Tag created successfully!'))
            return redirect('manage_tags')
    else:
        form = TagForm(user=request.user)

    tags = Tag.objects.filter(user=request.user).annotate(item_count=Count('items'))
    return render(request, 'manage-tags.html', {'form': form, 'tags': tags})

@login_required
def edit_tag(request, tag_id):
    tag = get_object_or_404(Tag, id=tag_id, user=request.user)
    if request.method == 'POST':
        form = TagForm(request.POST, instance=tag, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Tag updated successfully!'))
            return redirect('manage_tags')
    else:
        form = TagForm(instance=tag, user=request.user)

    tags = Tag.objects.filter(user=request.user).annotate(item_count=Count('items'))
    return render(request, 'manage-tags.html', {'form': form, 'tags': tags, 'editing_tag': tag})

@require_POST
@login_required
def delete_tag(request, tag_id):
    tag = get_object_or_404(Tag, id=tag_id, user=request.user)
    tag.delete()  # Items keep existing; only the tag association is removed.
    messages.success(request, _('Tag deleted successfully!'))
    return redirect('manage_tags')


@require_GET
@login_required
def analytics(request):
    user = request.user
    now_dt = timezone.now()
    today = timezone.localtime(now_dt).date()

    # Build 12-month label sequence, oldest → newest
    months_seq = []
    for i in range(11, -1, -1):
        m_offset = today.month - i
        y_offset = 0
        while m_offset <= 0:
            m_offset += 12
            y_offset -= 1
        months_seq.append(dt.date(today.year + y_offset, m_offset, 1).strftime('%b %Y'))

    twelve_months_ago = now_dt - timedelta(days=366)

    monthly_added = (
        Item.objects.filter(user=user, created_at__gte=twelve_months_ago)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )
    added_by_month = {row['month'].strftime('%b %Y'): row['count'] for row in monthly_added if row['month']}
    monthly_added_data = [added_by_month.get(m, 0) for m in months_seq]

    monthly_used = (
        Item.objects.filter(user=user, is_used=True, last_used_at__gte=twelve_months_ago)
        .annotate(month=TruncMonth('last_used_at'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )
    used_by_month = {row['month'].strftime('%b %Y'): row['count'] for row in monthly_used if row['month']}
    monthly_used_data = [used_by_month.get(m, 0) for m in months_seq]

    value_by_type = list(
        Item.objects.filter(user=user, is_used=False, value_type='money', expiry_date__gte=now_dt)
        .values('type')
        .annotate(count=Count('id'), total_value=Sum('value'))
        .order_by('-total_value')
    )
    for row in value_by_type:
        row['total_value'] = float(row['total_value'] or 0)

    top_issuers = list(
        Item.objects.filter(user=user, is_used=False)
        .exclude(issuer='')
        .values('issuer')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    currency_breakdown = list(
        Item.objects.filter(user=user, is_used=False, value_type='money', expiry_date__gte=now_dt)
        .exclude(type='loyaltycard')
        .values('currency')
        .annotate(count=Count('id'), total=Sum('value'))
        .order_by('-total')
    )
    for row in currency_breakdown:
        row['total'] = float(row['total'] or 0)

    kpis = Item.objects.filter(user=user).aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_used=False, expiry_date__gte=now_dt)),
        used=Count('id', filter=Q(is_used=True)),
        expired=Count('id', filter=Q(is_used=False, expiry_date__lt=now_dt)),
        archived=Count('id', filter=Q(is_archived=True)),
    )

    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    wallet_budgets = []
    for wallet in Wallet.objects.filter(user=user, budget_amount__isnull=False):
        spent = abs(
            Transaction.objects.filter(
                item__wallet=wallet, item__user=user,
                date__gte=month_start, value__lt=0,
            ).aggregate(total=Sum('value'))['total'] or 0
        )
        budget = float(wallet.budget_amount)
        percent = min(int(float(spent) / budget * 100), 100) if budget else 0
        wallet_budgets.append({
            'name': wallet.name,
            'color': wallet.color,
            'budget': budget,
            'spent': float(spent),
            'remaining': max(budget - float(spent), 0),
            'percent': percent,
            'over_budget': float(spent) > budget,
        })

    type_labels = {
        'voucher': 'Vouchers', 'giftcard': 'Gift Cards', 'coupon': 'Coupons',
        'loyaltycard': 'Loyalty Cards', 'travelpass': 'Travel Passes',
    }

    context = {
        'kpis': kpis,
        'months_seq_json': json.dumps(months_seq),
        'monthly_added_json': json.dumps(monthly_added_data),
        'monthly_used_json': json.dumps(monthly_used_data),
        'value_by_type': value_by_type,
        'value_by_type_json': json.dumps([
            {'type': type_labels.get(r['type'], r['type']), 'value': r['total_value'], 'count': r['count']}
            for r in value_by_type
        ]),
        'top_issuers': top_issuers,
        'top_issuers_json': json.dumps([{'name': r['issuer'], 'count': r['count']} for r in top_issuers]),
        'currency_breakdown': currency_breakdown,
        'wallet_budgets': wallet_budgets,
    }
    return render(request, 'analytics.html', context)


# ── Phase D: Advanced Collaboration ─────────────────────────────────────────

def _log_wallet_activity(wallet, actor, action, item=None, detail=''):
    """Record a WalletActivity row; silently no-op if wallet is None."""
    if wallet is None:
        return
    WalletActivity.objects.create(
        wallet=wallet,
        actor=actor,
        action=action,
        item=item,
        item_name=item.name if item else '',
        detail=detail,
    )


def _check_item_edit_permission(item, user):
    """
    Returns True when user may write to item (create/edit/delete/toggle).
    Viewer-role WalletMembership collaborators are denied write access.
    The old wallet.shared_with M2M grants full edit access (backward compat).
    """
    if item.user == user:
        return True
    if item.shared_with.filter(shared_with_user=user).exists():
        return True
    if item.wallet is None:
        return False
    if item.wallet.user == user:
        return True
    # Old sharing system: all M2M collaborators have full edit access.
    if item.wallet.shared_with.filter(pk=user.id).exists():
        return True
    # New WalletMembership system: only editors may write.
    try:
        membership = WalletMembership.objects.get(wallet=item.wallet, user=user)
        return membership.role == WalletMembership.ROLE_EDITOR
    except WalletMembership.DoesNotExist:
        return False


@login_required
def wallet_activity_feed(request, wallet_id):
    wallet = get_object_or_404(Wallet, id=wallet_id)
    if not has_wallet_access(wallet, request.user):
        return HttpResponse("Unauthorized", status=403)
    activities = wallet.activities.select_related('actor', 'item').order_by('-timestamp')[:100]
    return render(request, 'wallet_activity.html', {'wallet': wallet, 'activities': activities})


# ── Phase E: Integrations & Automation (Webhooks) ────────────────────────────

class _WebhookForm:
    """Minimal in-view form helper for UserWebhook — no Django Form subclass needed."""
    ALL_EVENTS = [e[0] for e in UserWebhook.EVENT_CHOICES]

    @staticmethod
    def from_post(post, instance=None):
        errors = {}
        name = post.get('name', '').strip()
        url = post.get('url', '').strip()
        secret = post.get('secret', '').strip()
        events = [e for e in _WebhookForm.ALL_EVENTS if post.get(f'event_{e}')]
        enabled = bool(post.get('enabled'))

        if not name:
            errors['name'] = 'Name is required.'
        if not url or not url.startswith(('http://', 'https://')):
            errors['url'] = 'A valid URL is required.'
        if not events:
            errors['events'] = 'Select at least one event.'
        return {
            'name': name, 'url': url, 'secret': secret,
            'events': events, 'enabled': enabled,
        }, errors


@login_required
def manage_webhooks(request):
    hooks = UserWebhook.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'webhooks.html', {
        'hooks': hooks,
        'event_choices': UserWebhook.EVENT_CHOICES,
    })


@require_POST
@login_required
def create_webhook(request):
    data, errors = _WebhookForm.from_post(request.POST)
    if errors:
        for msg in errors.values():
            messages.error(request, msg)
    else:
        UserWebhook.objects.create(user=request.user, **data)
        messages.success(request, _('Webhook created.'))
    return redirect('manage_webhooks')


@require_POST
@login_required
def edit_webhook(request, webhook_id):
    hook = get_object_or_404(UserWebhook, id=webhook_id, user=request.user)
    data, errors = _WebhookForm.from_post(request.POST)
    if errors:
        for msg in errors.values():
            messages.error(request, msg)
    else:
        for k, v in data.items():
            setattr(hook, k, v)
        hook.save()
        messages.success(request, _('Webhook updated.'))
    return redirect('manage_webhooks')


@require_POST
@login_required
def delete_webhook(request, webhook_id):
    hook = get_object_or_404(UserWebhook, id=webhook_id, user=request.user)
    hook.delete()
    messages.success(request, _('Webhook deleted.'))
    return redirect('manage_webhooks')


@require_POST
@login_required
def test_webhook(request, webhook_id):
    hook = get_object_or_404(UserWebhook, id=webhook_id, user=request.user)
    import hmac as _hmac, hashlib as _hashlib, requests as _rq
    payload = json.dumps({
        'event': 'test',
        'message': 'VoucherVault test webhook delivery',
        'webhook_id': hook.id,
    }).encode()
    headers = {'Content-Type': 'application/json', 'X-VoucherVault-Event': 'test'}
    if hook.secret:
        sig = _hmac.new(hook.secret.encode(), payload, _hashlib.sha256).hexdigest()
        headers['X-VoucherVault-Signature'] = f"sha256={sig}"
    try:
        resp = _rq.post(hook.url, data=payload, headers=headers, timeout=10)
        if resp.ok:
            messages.success(request, _('Test delivery succeeded (HTTP %(status)s).') % {'status': resp.status_code})
        else:
            messages.warning(request, _('Test delivered but got HTTP %(status)s.') % {'status': resp.status_code})
    except Exception as exc:
        messages.error(request, _('Test delivery failed: %(err)s') % {'err': str(exc)})
    return redirect('manage_webhooks')


# ── Phase F: Security & Audit ─────────────────────────────────────────────────

import pyotp
import qrcode as _qrcode
import io as _io
import base64 as _base64
from django.contrib.auth import authenticate as _authenticate, login as _login
from django.contrib.sessions.models import Session as _Session
from django.utils.http import url_has_allowed_host_and_scheme as _safe_redirect


def _client_ip_view(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or None


def custom_login(request):
    """TOTP-aware login view. Replaces django.contrib.auth.views.LoginView."""
    from django.contrib.auth import REDIRECT_FIELD_NAME
    if request.user.is_authenticated:
        return redirect('show_items')

    next_url = request.POST.get(REDIRECT_FIELD_NAME) or request.GET.get(REDIRECT_FIELD_NAME, '')
    error = None

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = _authenticate(request, username=username, password=password)
        if user is not None:
            try:
                device = user.totp_device
                if device.confirmed:
                    request.session['_totp_user_id'] = user.pk
                    if next_url:
                        request.session['_totp_next'] = next_url
                    return redirect('totp_verify')
            except TOTPDevice.DoesNotExist:
                pass
            _login(request, user)
            if next_url and _safe_redirect(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect('show_items')
        error = _('Invalid username or password.')

    return render(request, 'registration/login.html', {'error': error, 'next': next_url})


def _generate_totp_backup_codes(device):
    """Generate 8 single-use backup codes, store as hashed values, return plaintext."""
    from django.contrib.auth.hashers import make_password
    import secrets
    device.backup_codes.all().delete()
    plaintext = []
    for _ in range(8):
        code = secrets.token_hex(4).upper()  # e.g. "A3B70C9D"
        formatted = f"{code[:4]}-{code[4:]}"
        device.backup_codes.create(code_hash=make_password(code))
        plaintext.append(formatted)
    return plaintext


def _consume_totp_backup_code(device, raw_token):
    """Return True and mark code used if raw_token matches any unused backup code."""
    from django.contrib.auth.hashers import check_password
    token = raw_token.upper().replace('-', '').replace(' ', '')
    for bc in device.backup_codes.filter(used=False):
        if check_password(token, bc.code_hash):
            bc.used = True
            bc.save(update_fields=['used'])
            return True
    return False


@login_required
def totp_setup(request):
    """Generate a new TOTP secret, display QR code, wait for confirmation."""
    try:
        device = request.user.totp_device
        if device.confirmed:
            messages.info(request, _('Two-factor authentication is already enabled.'))
            return redirect('session_management')
    except TOTPDevice.DoesNotExist:
        device = None

    if request.method == 'POST':
        token = request.POST.get('token', '').strip().replace(' ', '')
        secret = request.POST.get('secret', '').strip()
        totp = pyotp.TOTP(secret)
        if totp.verify(token, valid_window=1):
            if device is None:
                device = TOTPDevice(user=request.user)
            device.secret = secret
            device.confirmed = True
            device.save()
            plaintext_codes = _generate_totp_backup_codes(device)
            messages.success(request, _('Two-factor authentication enabled.'))
            return render(request, 'totp_setup.html', {
                'setup_complete': True,
                'backup_codes': plaintext_codes,
            })
        else:
            messages.error(request, _('Invalid code. Please try again.'))

    secret = pyotp.random_base32()
    if device and not device.confirmed:
        secret = device.secret
    else:
        device = TOTPDevice.objects.create(user=request.user, secret=secret, confirmed=False) if device is None else device
        device.secret = secret
        device.save()

    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=request.user.email or request.user.username,
        issuer_name='VoucherVault Plus+',
    )
    qr_img = _qrcode.make(totp_uri)
    buf = _io.BytesIO()
    qr_img.save(buf, format='PNG')
    qr_b64 = _base64.b64encode(buf.getvalue()).decode()

    return render(request, 'totp_setup.html', {'secret': secret, 'qr_b64': qr_b64})


def totp_verify(request):
    """Second-factor gate after password auth; used in the login flow."""
    user_id = request.session.get('_totp_user_id')
    if not user_id:
        return redirect('login')
    try:
        from django.contrib.auth.models import User as _User
        user = _User.objects.get(pk=user_id)
    except _User.DoesNotExist:
        return redirect('login')

    error = None
    if request.method == 'POST':
        token = request.POST.get('token', '').strip().replace(' ', '')
        try:
            device = user.totp_device
            verified = pyotp.TOTP(device.secret).verify(token, valid_window=1)
            if not verified:
                verified = _consume_totp_backup_code(device, token)
            if verified:
                _login(request, user)
                del request.session['_totp_user_id']
                next_url = request.session.pop('_totp_next', '')
                if next_url and _safe_redirect(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                return redirect('show_items')
        except TOTPDevice.DoesNotExist:
            return redirect('login')
        error = _('Invalid code. Please try again.')

    return render(request, 'totp_verify.html', {'error': error})


@require_POST
@login_required
def totp_disable(request):
    try:
        request.user.totp_device.delete()
        messages.success(request, _('Two-factor authentication disabled.'))
    except TOTPDevice.DoesNotExist:
        pass
    return redirect('session_management')


@login_required
def session_management(request):
    """List the user's active sessions; allow revoking individual ones."""
    current_key = request.session.session_key
    user_sessions = []
    for s in _Session.objects.filter(expire_date__gt=timezone.now()):
        data = s.get_decoded()
        if str(data.get('_auth_user_id')) == str(request.user.pk):
            user_sessions.append({
                'key': s.session_key,
                'expire_date': s.expire_date,
                'is_current': s.session_key == current_key,
                'created': data.get('_session_created'),
            })
    user_sessions.sort(key=lambda x: x['expire_date'], reverse=True)

    try:
        totp_device = request.user.totp_device
        totp_enabled = totp_device.confirmed
        backup_codes_remaining = totp_device.backup_codes.filter(used=False).count() if totp_enabled else 0
    except TOTPDevice.DoesNotExist:
        totp_enabled = False
        backup_codes_remaining = 0

    recent_logins = LoginAuditLog.objects.filter(user=request.user).order_by('-timestamp')[:20]

    return render(request, 'session_management.html', {
        'user_sessions': user_sessions,
        'totp_enabled': totp_enabled,
        'backup_codes_remaining': backup_codes_remaining,
        'recent_logins': recent_logins,
    })


@require_POST
@login_required
def revoke_session(request):
    key = request.POST.get('session_key', '')
    if key and key != request.session.session_key:
        _Session.objects.filter(session_key=key).delete()
        messages.success(request, _('Session revoked.'))
    return redirect('session_management')


@require_GET
@login_required
def gdpr_data_export(request):
    """GDPR-compliant machine-readable export of everything stored for the user."""
    user = request.user
    items_qs = Item.objects.filter(user=user).prefetch_related(
        'tags', 'transactions', 'documents', 'shares',
    ).select_related('wallet')

    items_data = []
    for item in items_qs:
        items_data.append({
            'id': str(item.id),
            'name': item.name,
            'type': item.type,
            'redeem_code': item.redeem_code,
            'card_number': item.card_number,
            'code_type': item.code_type,
            'pin': item.pin,
            'issuer': item.issuer,
            'issue_date': str(item.issue_date) if item.issue_date else None,
            'expiry_date': str(item.expiry_date) if item.expiry_date else None,
            'description': item.description,
            'notes': item.notes,
            'value': str(item.value) if item.value is not None else None,
            'value_type': item.value_type,
            'currency': item.currency,
            'is_used': item.is_used,
            'is_pinned': item.is_pinned,
            'is_archived': item.is_archived,
            'is_recurring': item.is_recurring,
            'renewal_period': item.renewal_period,
            'renewal_date': str(item.renewal_date) if item.renewal_date else None,
            'notifications_muted': item.notifications_muted,
            'share_message': item.share_message,
            'wallet': item.wallet.name if item.wallet else None,
            'tags': [t.name for t in item.tags.all()],
            'transactions': [
                {'date': str(t.date), 'description': t.description, 'value': str(t.value)}
                for t in item.transactions.all()
            ],
            'documents': [
                {'file': str(d.file), 'label': d.label, 'uploaded_at': str(d.uploaded_at)}
                for d in item.documents.all()
            ],
            'shared_with': [
                {'username': s.shared_with_user.username, 'shared_at': str(s.shared_at)}
                for s in item.shares.all()
            ],
            'created_at': str(item.issue_date),
            'last_used_at': str(item.last_used_at) if item.last_used_at else None,
        })

    from notify.models import NotificationRule
    rules_data = list(
        NotificationRule.objects.filter(user=user).values(
            'name', 'backend', 'enabled', 'event_types', 'digest_frequency', 'created_at',
        )
    )
    for r in rules_data:
        r['created_at'] = str(r['created_at'])

    wallets_data = list(
        Wallet.objects.filter(user=user).values('name', 'description', 'color', 'created_at', 'updated_at')
    )
    for w in wallets_data:
        w['created_at'] = str(w['created_at'])
        w['updated_at'] = str(w['updated_at'])

    tags_data = list(Tag.objects.filter(user=user).values('name', 'color'))

    payload = {
        'export_date': str(timezone.now()),
        'username': user.username,
        'email': user.email,
        'date_joined': str(user.date_joined),
        'items': items_data,
        'wallets': wallets_data,
        'tags': tags_data,
        'notification_rules': rules_data,
    }
    response = HttpResponse(
        json.dumps(payload, indent=2, default=str),
        content_type='application/json',
    )
    response['Content-Disposition'] = 'attachment; filename="vouchervault-my-data.json"'
    return response


# ── Phase C: PWA Install prompt ───────────────────────────────────────────────
# (No server-side view needed — fully client-side in pwa-install.js)
