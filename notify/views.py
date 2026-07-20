import json

import requests as req_lib
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from .backends.webpush import get_vapid_public_key, webpush_enabled
from .forms import NotificationRuleForm
from .models import NotificationLog, NotificationRule, WebPushSubscription
from .tasks import send_test_notification


@login_required
def manage_rules(request):
    """List, create notification rules. Editing/deleting happens inline via the same page."""
    if request.method == 'POST':
        form = NotificationRuleForm(request.POST, user=request.user)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.user = request.user
            rule.save()
            messages.success(request, _('Notification rule created successfully!'))
            return redirect('manage_notification_rules')
    else:
        form = NotificationRuleForm(user=request.user)

    rules = NotificationRule.objects.filter(user=request.user)
    return render(request, 'notify/rules.html', {
        'form': form, 'rules': rules,
        'webpush_enabled': webpush_enabled(),
        'vapid_public_key': get_vapid_public_key(),
    })


@login_required
def edit_rule(request, rule_id):
    rule = get_object_or_404(NotificationRule, id=rule_id, user=request.user)
    if request.method == 'POST':
        form = NotificationRuleForm(request.POST, instance=rule, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Notification rule updated successfully!'))
            return redirect('manage_notification_rules')
    else:
        form = NotificationRuleForm(instance=rule, user=request.user)

    rules = NotificationRule.objects.filter(user=request.user)
    return render(request, 'notify/rules.html', {
        'form': form, 'rules': rules, 'editing_rule': rule,
        'webpush_enabled': webpush_enabled(),
        'vapid_public_key': get_vapid_public_key(),
    })


@require_POST
@login_required
def delete_rule(request, rule_id):
    rule = get_object_or_404(NotificationRule, id=rule_id, user=request.user)
    rule.delete()
    messages.success(request, _('Notification rule deleted successfully!'))
    return redirect('manage_notification_rules')


@require_POST
@login_required
def test_rule(request, rule_id):
    rule = get_object_or_404(NotificationRule, id=rule_id, user=request.user)
    success, detail = send_test_notification(rule)
    if success:
        messages.success(request, _('Test notification sent successfully!'))
    else:
        messages.error(request, _('Test notification failed: %(detail)s') % {'detail': detail or _('unknown error')})
    return redirect('manage_notification_rules')


@require_POST
@login_required
def firefly_test_connection(request):
    """AJAX endpoint: test a Firefly III URL + token by calling its /api/v1/about."""
    url = (request.POST.get('url') or '').rstrip('/')
    token = (request.POST.get('token') or '').strip()
    if not url or not token:
        return JsonResponse({'ok': False, 'error': _('URL and token are required.')})
    try:
        import requests as req_lib
        resp = req_lib.get(
            f'{url}/api/v1/about',
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=8,
        )
        if resp.status_code == 200:
            version = resp.json().get('data', {}).get('version', 'unknown')
            return JsonResponse({'ok': True, 'version': version})
        if resp.status_code in (401, 403):
            return JsonResponse({'ok': False, 'error': _('Authentication failed — check your token.')})
        return JsonResponse({'ok': False, 'error': _('Server returned HTTP %(code)s.') % {'code': resp.status_code}})
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': _('Cannot reach server: %(detail)s') % {'detail': str(exc)}})


@login_required
def notification_log(request):
    logs = NotificationLog.objects.filter(user=request.user).select_related('rule', 'item')[:200]
    return render(request, 'notify/log.html', {'logs': logs})


@require_POST
@login_required
def webpush_subscribe(request):
    """Registers (or refreshes) a browser's Push API subscription for the current user."""
    try:
        data = json.loads(request.body)
        endpoint = data['endpoint']
        keys = data['keys']
        p256dh = keys['p256dh']
        auth = keys['auth']
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': _('Invalid subscription payload.')}, status=400)

    WebPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user': request.user,
            'p256dh': p256dh,
            'auth': auth,
            'user_agent': request.META.get('HTTP_USER_AGENT', '')[:255],
        },
    )
    return JsonResponse({'success': True})


@require_POST
@login_required
def webpush_unsubscribe(request):
    """Removes a browser's Push API subscription for the current user."""
    try:
        data = json.loads(request.body)
        endpoint = data['endpoint']
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': _('Invalid request.')}, status=400)

    WebPushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({'success': True})
