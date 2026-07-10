import django_filters

from myapp.models import Item


class ItemFilter(django_filters.FilterSet):
    expires_before = django_filters.DateFilter(field_name='expiry_date', lookup_expr='lte')
    expires_after = django_filters.DateFilter(field_name='expiry_date', lookup_expr='gte')

    class Meta:
        model = Item
        fields = ['type', 'is_used', 'is_pinned', 'currency', 'code_type', 'value_type']
