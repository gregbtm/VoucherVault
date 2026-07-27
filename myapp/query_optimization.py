"""Database query optimization utilities and middleware."""

import logging
from django.db import connection, reset_queries
from django.conf import settings

logger = logging.getLogger(__name__)


class QueryCounterMiddleware:
    """
    Middleware to log query counts per request (for development/debugging).
    Enable with settings.DEBUG = True and set ENABLE_QUERY_LOGGING env var.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.should_log = settings.DEBUG and settings.DEBUG == True

    def __call__(self, request):
        if self.should_log:
            reset_queries()

        response = self.get_response(request)

        if self.should_log:
            query_count = len(connection.queries)
            if query_count > 20:
                logger.warning(
                    f'{request.method} {request.path}: {query_count} queries',
                    extra={'request': request}
                )

        return response


class OptimizedQueryHints:
    """Reference for common query optimizations."""

    @staticmethod
    def items_with_transactions(user):
        """Efficiently fetch items with precomputed transaction totals."""
        from django.db.models import Sum, F
        from myapp.models import Item
        return Item.objects.filter(user=user).annotate(
            transaction_total=Sum('transactions__value') + F('value')
        )

    @staticmethod
    def wallets_with_items(user):
        """Efficiently fetch wallets with item counts and prefetched collaborators."""
        from django.db.models import Count
        from myapp.models import Wallet
        return Wallet.objects.filter(user=user).annotate(
            item_count=Count('items', distinct=True)
        ).prefetch_related('shared_with')

    @staticmethod
    def items_with_categories(user):
        """Efficiently fetch items with categories and recommendations."""
        from myapp.models import Item
        return Item.objects.filter(user=user).select_related(
            'category', 'wallet'
        ).prefetch_related('recommendations')

    @staticmethod
    def user_analytics(user):
        """Efficiently compute user-wide analytics."""
        from django.db.models import Count, Sum
        from myapp.models import Item, Transaction

        active_items = Item.objects.filter(user=user, is_used=False)
        transactions = Transaction.objects.filter(item__user=user)

        return {
            'total_items': active_items.count(),
            'used_items': Item.objects.filter(user=user, is_used=True).count(),
            'expired_items': active_items.filter(expiry_date__lt=timezone.now()).count(),
            'total_spent': abs(
                transactions.filter(value__lt=0).aggregate(Sum('value'))['value__sum'] or 0
            ),
        }
