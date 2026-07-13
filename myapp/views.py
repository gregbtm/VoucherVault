import os
import json
import logging
import secrets
import unicodedata
import mimetypes
import uuid
import datetime as dt
from django.db import IntegrityError
from django.db.models import Q
from django.utils.safestring import mark_safe
from .forms import *
from .ics_calendar import build_ics_calendar
from .models import *
from .utils import generate_code_image_base64, get_fixer_rates, convert_currency
from django.db.models import Sum
from django.utils import timezone
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django.utils.timezone import now
from django.utils.http import url_has_allowed_host_and_scheme
from .decorators import require_authorization_header_with_api_token
from .analytics import build_expiry_calendar, get_expiring_soon_items, get_items_by_wallet
from .merchant_logos import get_cached_balance_check_url, get_cached_logo, get_cached_logos_for_issuers, remember_balance_check_url
from .portainer import PortainerRedeployError, trigger_redeploy
from .update_check import check_for_update
from .help_docs import render_doc
from .public_share import is_link_preview_bot, pin_attempt_rate_limited, view_rate_limited
from .tasks import fetch_merchant_logo_task
from imports.exporters.google_wallet import generate_google_wallet_save_url, google_wallet_enabled
from imports.exporters.pkpass import generate_pkpass, pkpass_enabled
from notify.tasks import notify_balance_changed, notify_item_archived, notify_item_created, notify_item_shared, notify_item_used
from ocr.backends import ocr_enabled
from django.db.models import Count, Sum, Q
from django.db.models.functions import Coalesce
from django.db.models import Value
from django.utils.text import get_valid_filename

logger = logging.getLogger(__name__)

apprise_txt = _('Apprise URLs were already configured. Will not display them again here to protect secrets. You can freely re-configure the URLs now and hit update though.')

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
def ping(request):
    return HttpResponse('', status=204)

@require_GET
@login_required
def dashboard(request):
    user = request.user

    total_items = Item.objects.filter(user=user, is_used=False).count()
    available_items = Item.objects.filter(user=user, is_used=False, expiry_date__gte=timezone.now()).count()
    used_items = Item.objects.filter(user=user, is_used=True).count()
    expired_items = Item.objects.filter(user=user, expiry_date__lt=timezone.now(), is_used=False).count()

    # Get user preferences for currency settings
    preferences, _ = UserPreference.objects.get_or_create(user=user)
    fixer_api_key = preferences.fixer_api_key
    default_currency = preferences.default_currency or 'GBP'

    # Get threshold days from environment variable or default to 30
    threshold_days = SiteConfiguration.load().expiry_threshold_days
    # Calculate soon-to-expire date (used for both "soon expiring" count and at-risk value)
    soon_expiry_date = now() + timedelta(days=threshold_days)

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
                if item.expiry_date < soon_expiry_date.date():
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
                    if item.expiry_date < soon_expiry_date.date():
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

    coupons_count = Item.objects.filter(user=user, type='coupon', is_used=False, expiry_date__gte=timezone.now()).count()
    vouchers_count = Item.objects.filter(user=user, type='voucher', is_used=False, expiry_date__gte=timezone.now()).count()
    giftcards_count = Item.objects.filter(user=user, type='giftcard', is_used=False, expiry_date__gte=timezone.now()).count()
    loyaltycards_count = Item.objects.filter(user=user, type='loyaltycard', is_used=False, expiry_date__gte=timezone.now()).count()

    # Count the number of items shared by the user
    shared_items_count_by_you = ItemShare.objects.filter(shared_by=user).values('item').distinct().count()
    shared_items_count_with_you = ItemShare.objects.filter(
        shared_with_user=user,
        item__is_used=False,
        item__expiry_date__gte=now().date()
    ).exclude(item__user=user).values('item').distinct().count()

    # Count the number of items soon expiring based on EXPIRY_THRESHOLD_DAYS
    soon_expiring_items = Item.objects.filter(
        user=user,
        is_used=False,
        expiry_date__gte=now(),
        expiry_date__lt=soon_expiry_date
    ).count()

    items_by_wallet = get_items_by_wallet(user)

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
        'loyaltycards_count':loyaltycards_count,
        'expired_items': expired_items,
        "soon_expiring_items": soon_expiring_items,
        'items_by_wallet': items_by_wallet,
        'wallet_chart_height': max(200, len(items_by_wallet) * 40 + 60),
        'expiring_soon_list': get_expiring_soon_items(user),
        'expiry_calendar': build_expiry_calendar(user),
        'shared_items_count_by_you': shared_items_count_by_you,
        'shared_items_count_with_you': shared_items_count_with_you,
    }
    return render(request, 'dashboard.html', context)

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
    
    # Calculate counts for filters (owned items plus items in wallets shared with the
    # user; archived items are hidden from every default view/count, only reachable
    # via the dedicated "Archived" filter)
    all_accessible_items = Item.objects.filter(Q(user=user) | Q(wallet__shared_with=user)).distinct()
    user_items = all_accessible_items.exclude(is_archived=True)
    threshold_days = SiteConfiguration.load().expiry_threshold_days
    soon_expiry_date = now() + timedelta(days=threshold_days)

    available_count = user_items.filter(is_used=False, expiry_date__gte=timezone.now()).count()
    soon_expiring_count = user_items.filter(is_used=False, expiry_date__gte=now(), expiry_date__lt=soon_expiry_date).count()
    used_count = user_items.filter(is_used=True).count()
    expired_count = user_items.filter(expiry_date__lt=timezone.now(), is_used=False).count()
    archived_count = all_accessible_items.filter(is_archived=True).count()

    # Type counts (from available items only)
    available_items_qs = user_items.filter(is_used=False, expiry_date__gte=timezone.now())
    voucher_count = available_items_qs.filter(type='voucher').count()
    giftcard_count = available_items_qs.filter(type='giftcard').count()
    coupon_count = available_items_qs.filter(type='coupon').count()
    loyaltycard_count = available_items_qs.filter(type='loyaltycard').count()

    # Base query
    if filter_value == 'shared_by_me':
        items = Item.objects.filter(shared_with__shared_by=user).exclude(is_archived=True).distinct()
    elif filter_value == 'shared_with_me':
        items = Item.objects.filter(
            shared_with__shared_with_user=user,
            is_used=False,
            expiry_date__gte=now().date()  # Only not expired
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
            Q(issuer__icontains=search_query)
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
    merchant_logos = get_cached_logos_for_issuers(i.issuer for i in items)

    for item in items:
        items_with_qr.append({
            'item': item,
            'qr_code_base64': item.qr_code_base64,
            'current_value': item.current_balance,
            'merchant_logo_url': merchant_logos.get(item.issuer.strip().lower()),
        })

    context = {
        'items_with_qr': items_with_qr,
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
        # Tag filter — counts reflect the user's own non-archived accessible
        # items, same base set "All Items" browses by default
        'all_tags': Tag.objects.filter(user=user).annotate(
            item_count=Count('items', filter=Q(items__in=user_items), distinct=True)
        ).order_by('name'),
        'selected_tag_ids': [int(t) for t in tag_ids],
        # Appended to the static status/type filter chip hrefs so switching
        # status or type doesn't silently drop the active tag filter.
        # tag_ids is already validated as digit-only strings, safe to mark.
        'tag_query_params': mark_safe(''.join(f'&tag={t}' for t in tag_ids)),
    }
    return render(request, 'inventory.html', context)

@login_required
def view_item(request, item_uuid):
    item = get_object_or_404(Item, id=item_uuid)
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

            if total_value <= 0:
                item.is_used = True
                item.save()
                notify_item_used(item)
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

    context = {
        'item': item,
        'transactions': transactions,
        'total_value': total_value,
        'qr_code_base64': item.qr_code_base64,
        'form': form,
        'current_date': timezone.now(),
        'is_owner': is_owner,  # Pass the owner flag to the template
        'can_edit': can_edit,  # Owner or shared-wallet collaborator
        'is_shared': is_shared,  # Pass the shared status to the template
        'merchant_logo_url': cached_merchant.logo_url if cached_merchant else None,
        'pkpass_enabled': pkpass_enabled(),
        'google_wallet_save_url': google_wallet_save_url,
        'document_form': DocumentForm(),
        'preferences': preferences,
        'public_share': ItemPublicShare.objects.filter(item=item).first(),
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

@login_required
def create_item(request):
    if request.method == 'POST':
        form = ItemForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            item = form.save(commit=False)
            item.user = request.user  # Set the user from the session

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
                    fetch_merchant_logo_task.delay(item.issuer)
                except Exception:
                    # Best-effort: a broker outage shouldn't block saving the item.
                    logger.warning('Could not queue merchant logo fetch for %r', item.issuer, exc_info=True)

            remember_balance_check_url(item.issuer, item.balance_check_url)
            notify_item_created(item)

            return redirect('show_items')
        else:
            # If form is not valid, render the form with validation errors
            return render(request, 'create-item.html', {'form': form, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})
    else:
        # If not a POST request, initialize form with user's preferred currency
        preferences, _ = UserPreference.objects.get_or_create(user=request.user)
        form = ItemForm(initial={'currency': preferences.default_currency or 'GBP'}, user=request.user)

    return render(request, 'create-item.html', {'form': form, 'ocr_enabled': ocr_enabled(), 'known_issuers': _known_issuers(request.user)})

@login_required
def edit_item(request, item_uuid):
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)
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
                    fetch_merchant_logo_task.delay(item.issuer)
                except Exception:
                    # Best-effort: a broker outage shouldn't block saving the item.
                    logger.warning('Could not queue merchant logo fetch for %r', item.issuer, exc_info=True)

            remember_balance_check_url(item.issuer, item.balance_check_url)

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

@require_GET
@login_required
def serve_image_file(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    if not item.file:
        raise Http404("No file attached.")

    mime_type, _ = mimetypes.guess_type(item.file.name)
    if not mime_type or not mime_type.startswith('image/'):
        return HttpResponse("File is not an image", status=400)

    return HttpResponse(item.file, content_type=mime_type)

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
    item = get_object_or_404(Item, id=item_id, user=request.user)
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

    item.save()
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
    calendar = build_ics_calendar(request.user)
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
    calendar = build_ics_calendar(profile.user)
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
        messages.error(request, _('Only administrators can trigger a redeploy.'))
        return redirect('show_items')

    try:
        trigger_redeploy()
    except PortainerRedeployError as exc:
        messages.error(request, _('Redeploy request failed: %(error)s') % {'error': exc})
    else:
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

@login_required
def view_doc(request, doc_slug):
    """
    Renders one of docs/*.md in-app for the "?" help buttons next to Site
    Settings sections - superuser-only since it's only linked from there,
    and rendered locally (see help_docs.py) rather than out to GitHub so
    it's available on a fully offline deployment too.
    """
    if not request.user.is_superuser:
        messages.error(request, _('Only administrators can view setup guides.'))
        return redirect('show_items')

    result = render_doc(doc_slug)
    if result is None:
        raise Http404('Unknown help topic.')
    title, html = result
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
    today = now().date()
    
    # Get filter and search parameters
    filter_type = request.GET.get('filter', 'all')
    search_query = request.GET.get('query', '').strip()

    # Retrieve or create user preferences
    preferences, _ = UserPreference.objects.get_or_create(user=current_user)

    shares = ItemShare.objects.filter(
        Q(shared_with_user=current_user) | Q(shared_by=current_user)
    ).select_related('item', 'shared_by', 'shared_with_user') \
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
        'code': item.card_number or item.redeem_code,
        'pin': item.pin or '',
        'balance': str(item.get_current_balance()) if item.type == 'giftcard' else None,
        'currency': item.currency,
    }

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
        return HttpResponse("Unauthorized", status=403)

    try:
        share = item.public_share
    except ItemPublicShare.DoesNotExist:
        try:
            share = _create_public_share(item, request.user)
        except IntegrityError:
            # Lost a race with a concurrent request creating the same
            # OneToOne row - the other request's create() won, use it.
            share = item.public_share
    if _wants_json(request):
        return JsonResponse(_public_share_payload(request, item, share))
    messages.success(request, _('Public share link created.'))
    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def regenerate_public_share_link(request, item_id):
    """Invalidates the old public link (e.g. if it leaked) and issues a fresh one."""
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    ItemPublicShare.objects.filter(item=item).delete()
    share = _create_public_share(item, request.user)
    if _wants_json(request):
        return JsonResponse(_public_share_payload(request, item, share))
    messages.success(request, _('Public share link regenerated. The old link no longer works.'))
    return redirect('view_item', item_uuid=item.id)

@require_POST
@login_required
def revoke_public_share_link(request, item_id):
    """Deletes the public link outright; the next 'Share details' tap creates a new one."""
    item = get_object_or_404(Item, id=item_id)
    if not has_item_access(item, request.user):
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
    share = get_object_or_404(ItemPublicShare.objects.select_related('item'), id=share_id)
    item = share.item
    cached_merchant = get_cached_logo(item.issuer)
    merchant_logo_url = cached_merchant.logo_url if cached_merchant else None

    if is_link_preview_bot(request.META.get('HTTP_USER_AGENT', '')):
        return render(request, 'public_item.html', {
            'item': item, 'crawler_preview': True, 'merchant_logo_url': merchant_logo_url,
        })

    if share.is_expired():
        return render(request, 'public_item.html', {
            'item': item, 'expired': True, 'merchant_logo_url': merchant_logo_url,
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
                'merchant_logo_url': merchant_logo_url,
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
        'merchant_logo_url': merchant_logo_url,
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

        total_value = round((items_with_transaction_values.aggregate(
            total_value=Sum('current_balance'))['total_value'] or 0), 2)

        # Item stats
        item_stats = {
            "total_items": items_query.count(),
            "total_value": total_value,
            "vouchers": items_query.filter(type='voucher').count(),
            "giftcards": items_query.filter(type='giftcard').count(),
            "coupons": items_query.filter(type='coupon').count(),
            "loyaltycards": items_query.filter(type='loyaltycard').count(),
            "used_items": items_query.filter(is_used=True).count(),
            "available_items": items_query.filter(is_used=False).count() - items_query.filter(expiry_date__lt=now()).count(),
            "expired_items": items_query.filter(expiry_date__lt=now()).count(),
            "soon_expiring_items": items_query.filter(expiry_date__gte=now(), expiry_date__lt=soon_expiry_date).count(),
        }

        # Return global user_stats
        user_stats = {
            "total_users": User.objects.count(),
            "active_users": User.objects.filter(is_active=True).count(),
            "disabled_users": User.objects.filter(is_active=False).count(),
            "superusers": User.objects.filter(is_superuser=True).count(),
            "staff_members": User.objects.filter(is_staff=True).count(),
        }

        # Issuer stats
        issuer_transaction_totals = (
            items_query.filter(is_used=False, expiry_date__gte=now())
            .values('issuer')
            .annotate(
                transaction_total=Coalesce(
                    Sum('transactions__value', output_field=models.DecimalField()),
                    Value(0, output_field=models.DecimalField())
                )
            )
        )
        issuer_transaction_map = {item['issuer']: item['transaction_total'] for item in issuer_transaction_totals}

        issuers = (
            items_query.filter(is_used=False, expiry_date__gte=now())
            .values('issuer')
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
                "count": issuer["count"],
                "total_value": round((issuer["base_value"] + issuer_transaction_map.get(issuer["issuer"], 0)), 2),
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
def toggle_archive_item(request, item_uuid):
    """Toggle the archived status of an item: hides it from the default
    inventory views without marking it used or deleting it."""
    item = get_object_or_404(Item, id=item_uuid)
    if not has_item_access(item, request.user):
        return HttpResponse("Unauthorized", status=403)

    item.is_archived = not item.is_archived
    item.save(update_fields=['is_archived'])
    if item.is_archived:
        notify_item_archived(item)

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
    accessible = [item for item in items if has_item_access(item, request.user)]
    skipped = len(item_ids) - len(accessible)
    return accessible, skipped, data

@require_POST
@login_required
def bulk_archive_items(request):
    """Same is_archived + notify_item_archived logic as toggle_archive_item, looped over a selection."""
    items, skipped, _data = _bulk_selected_items(request)
    processed = 0
    for item in items:
        if not item.is_archived:
            item.is_archived = True
            item.save(update_fields=['is_archived'])
            notify_item_archived(item)
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
        wallet.shared_with.add(form.cleaned_data['user'])
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
    messages.success(request, _('Removed %(username)s from this wallet.') % {'username': collaborator.username})
    return redirect('edit_wallet', wallet_id=wallet.id)

@require_POST
@login_required
def leave_shared_wallet(request, wallet_id):
    """A collaborator removes themselves from a wallet shared with them."""
    wallet = get_object_or_404(Wallet, id=wallet_id, shared_with=request.user)
    wallet.shared_with.remove(request.user)
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
