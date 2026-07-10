from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from .forms import NotificationRuleForm
from .models import NotificationLog, NotificationRule
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
    return render(request, 'notify/rules.html', {'form': form, 'rules': rules})


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
    return render(request, 'notify/rules.html', {'form': form, 'rules': rules, 'editing_rule': rule})


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


@login_required
def notification_log(request):
    logs = NotificationLog.objects.filter(user=request.user).select_related('rule', 'item')[:200]
    return render(request, 'notify/log.html', {'logs': logs})
