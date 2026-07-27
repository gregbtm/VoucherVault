"""Smart features: auto-categorization, budget tracking, recommendations."""
from decimal import Decimal
from django.utils import timezone
from django.db.models import Sum, Q, F
from datetime import timedelta
from .models import Item, ItemCategory, WalletBudget, ItemRecommendation, Transaction

# Category patterns: (regex_patterns, category, confidence)
CATEGORY_PATTERNS = [
    (['tesco', 'sainsbury', 'asda', 'waitrose', 'morrisons', 'aldi', 'lidl'], 'Groceries', Decimal('0.95')),
    (['netflix', 'disney', 'prime video', 'spotify', 'hulu', 'bbc'], 'Entertainment', Decimal('0.90')),
    (['starbucks', 'costa', 'caffe nero', 'greggs', 'mcdonald'], 'Dining', Decimal('0.85')),
    (['shell', 'bp', 'tesco fuel', 'asda fuel', 'fuel'], 'Fuel', Decimal('0.90')),
    (['amazon', 'ebay', 'argos'], 'Shopping', Decimal('0.80')),
    (['hotel', 'airbnb', 'booking'], 'Travel', Decimal('0.85')),
    (['boots', 'superdrug', 'lloyds'], 'Health & Beauty', Decimal('0.80')),
]


def categorize_item(item):
    """
    Auto-categorize an item based on issuer/name patterns.
    Returns (category, confidence) or None if no match.
    """
    text = f"{item.issuer or ''} {item.name}".lower()

    for patterns, category, confidence in CATEGORY_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return category, confidence

    return 'Other', Decimal('0.5')


def apply_category_to_item(item):
    """Apply auto-categorization to an item and save it."""
    category, confidence = categorize_item(item)
    ItemCategory.objects.update_or_create(
        item=item,
        defaults={'category': category, 'confidence': confidence}
    )
    return ItemCategory.objects.get(item=item)


def get_or_create_wallet_budget(wallet, user, monthly_limit=None):
    """Create or retrieve a budget for a wallet."""
    budget, created = WalletBudget.objects.get_or_create(
        wallet=wallet,
        user=user,
        defaults={'monthly_limit': monthly_limit or Decimal('100.00')}
    )
    return budget, created


def update_wallet_spent_this_month(wallet):
    """Recalculate current month spending for a wallet's budget."""
    today = timezone.localtime().date()
    month_start = today.replace(day=1)

    spent = Transaction.objects.filter(
        item__wallet=wallet,
        value__lt=0,
        date__gte=month_start
    ).aggregate(total=Sum('value'))['total'] or Decimal('0')

    budget = wallet.budget
    budget.current_month_spent = abs(spent).quantize(Decimal('0.01'))
    budget.save(update_fields=['current_month_spent'])

    return budget


def generate_item_recommendations(item):
    """Generate action recommendations for a single item."""
    today = timezone.localtime().date()
    recommendations = []

    # Expiry-based recommendations
    if item.expiry_date:
        days_left = (item.expiry_date - today).days
        if days_left < 0:
            pass  # Don't recommend on expired items
        elif days_left <= 1:
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='expires_very_soon',
                defaults={
                    'action': f'Expires tomorrow—use now!',
                    'priority': 3
                }
            )
            recommendations.append('expires_very_soon')
        elif days_left <= 7:
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='expires_soon',
                defaults={
                    'action': f'Expires in {days_left} days',
                    'priority': 2
                }
            )
            recommendations.append('expires_soon')

    # Balance-based recommendations
    if item.value_type == 'money' and item.value and not item.is_used:
        current_balance = item.get_current_balance()
        if current_balance and current_balance < Decimal('5.00'):
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='low_balance',
                defaults={
                    'action': f'Balance: £{current_balance}—use before it expires',
                    'priority': 2
                }
            )
            recommendations.append('low_balance')

    # Unused items
    if item.last_used_at:
        months_unused = (today - item.last_used_at.date()).days // 30
        if months_unused >= 6:
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='unused',
                defaults={
                    'action': f'Not used in {months_unused} months—consider archiving',
                    'priority': 1
                }
            )
            recommendations.append('unused')

    return recommendations


def generate_all_recommendations(user):
    """Generate recommendations for all of a user's active items."""
    items = Item.objects.filter(user=user, is_used=False, is_archived=False)
    all_recommendations = []

    for item in items:
        recs = generate_item_recommendations(item)
        all_recommendations.extend(recs)

    return all_recommendations


def get_user_recommendations(user, dismissed=False):
    """Fetch active or dismissed recommendations for a user."""
    items = Item.objects.filter(user=user).values_list('id', flat=True)
    query = ItemRecommendation.objects.filter(item_id__in=items)

    if dismissed:
        return query.filter(dismissed_at__isnull=False).order_by('-dismissed_at')
    else:
        return query.filter(dismissed_at__isnull=True).order_by('-priority', '-created_at')


def get_budget_alerts(user):
    """Get wallets where spending has reached alert threshold."""
    budgets = WalletBudget.objects.filter(
        user=user,
        alert_threshold__lte=F('current_month_spent')  # Simplified; ideally calculate percentage
    ).select_related('wallet')

    alerts = []
    for budget in budgets:
        if budget.is_alert_threshold_reached:
            alerts.append({
                'wallet': budget.wallet.name,
                'spent': str(budget.current_month_spent),
                'limit': str(budget.monthly_limit),
                'percentage': budget.spent_percentage
            })
    return alerts
