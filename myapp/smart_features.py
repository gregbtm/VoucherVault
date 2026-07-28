"""Smart features: auto-categorization, budget tracking, recommendations."""
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q
from .models import Item, ItemCategory, ItemRecommendation

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


# Reasons generate_item_recommendations owns end-to-end: it both creates
# these and retires them once their condition no longer holds. Excludes
# 'budget_overspend' - that condition is wallet-level, not item-level, and
# doesn't fit this model's per-item FK.
MANAGED_REASONS = ('expires_soon', 'expires_very_soon', 'low_balance', 'unused')


def generate_item_recommendations(item):
    """
    Generate (or refresh) action recommendations for a single item, and
    retire any previously-generated recommendation whose condition no
    longer holds - e.g. the balance was topped back up, the item got used,
    or an "expires in 3 days" item is now simply expired. Idempotent and
    cheap enough to call on every save (see myapp/signals.py) as well as
    from the daily sweep (myapp/tasks.py) that catches time-only changes
    no save would ever trigger.
    """
    if item.is_used or item.is_archived:
        ItemRecommendation.objects.filter(item=item, reason__in=MANAGED_REASONS).delete()
        return []

    today = timezone.localtime().date()
    applicable = set()

    # Expiry-based recommendations
    if item.expiry_date:
        days_left = (item.expiry_date - today).days
        if 0 <= days_left <= 1:
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='expires_very_soon',
                defaults={
                    'action': 'Expires today—use now!' if days_left == 0 else 'Expires tomorrow—use now!',
                    'priority': 3
                }
            )
            applicable.add('expires_very_soon')
        elif 2 <= days_left <= 7:
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='expires_soon',
                defaults={
                    'action': f'Expires in {days_left} days',
                    'priority': 2
                }
            )
            applicable.add('expires_soon')

    # Balance-based recommendations - excludes a fully-drained (£0 or
    # negative) balance, which isn't "low", it's just spent.
    if item.value_type == 'money' and item.value:
        current_balance = item.get_current_balance()
        if current_balance and 0 < current_balance < Decimal('5.00'):
            ItemRecommendation.objects.update_or_create(
                item=item,
                reason='low_balance',
                defaults={
                    'action': f'Balance: £{current_balance}—use before it expires',
                    'priority': 2
                }
            )
            applicable.add('low_balance')

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
            applicable.add('unused')

    stale = set(MANAGED_REASONS) - applicable
    if stale:
        ItemRecommendation.objects.filter(item=item, reason__in=stale).delete()

    return list(applicable)


def generate_all_recommendations(user):
    """Generate recommendations for all of a user's active items."""
    items = Item.objects.filter(user=user, is_used=False, is_archived=False)
    all_recommendations = []

    for item in items:
        recs = generate_item_recommendations(item)
        all_recommendations.extend(recs)

    return all_recommendations


def cleanup_recommendations_for_inactive_items():
    """
    Belt-and-braces sweep: deletes any managed recommendation left behind
    on an item that's since become used/archived. generate_item_recommendations
    already clears these the moment it runs against such an item (from the
    post_save signal), but this catches the rare case where that best-effort
    signal failed silently - see myapp/signals.py.
    """
    ItemRecommendation.objects.filter(
        Q(item__is_used=True) | Q(item__is_archived=True),
        reason__in=MANAGED_REASONS,
    ).delete()


def get_user_recommendations(user, dismissed=False):
    """Fetch active or dismissed recommendations for a user."""
    items = Item.objects.filter(user=user).values_list('id', flat=True)
    query = ItemRecommendation.objects.filter(item_id__in=items)

    if dismissed:
        return query.filter(dismissed_at__isnull=False).order_by('-dismissed_at')
    else:
        return query.filter(dismissed_at__isnull=True).order_by('-priority', '-created_at')
