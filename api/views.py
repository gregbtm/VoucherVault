from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django_filters.rest_framework import DjangoFilterBackend

from myapp.models import Item, ItemShare, Transaction, UserPreference, UserProfile

from .filters import ItemFilter
from .permissions import IsOwner
from .serializers import (
    ItemSerializer,
    ItemShareSerializer,
    TransactionSerializer,
    UserPreferenceSerializer,
    UserProfileSerializer,
)


class ItemViewSet(viewsets.ModelViewSet):
    """
    Full CRUD for the authenticated user's items, plus /redeem/, /transactions/
    and /shares/ sub-resources. Every queryset below is scoped to
    `user=request.user` — no item belonging to another user is ever
    reachable through this API, even by UUID guessing.
    """
    serializer_class = ItemSerializer
    permission_classes = [IsAuthenticated, IsOwner]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = ItemFilter
    search_fields = ['name', 'redeem_code', 'issuer', 'description']
    ordering_fields = ['expiry_date', 'name', 'value', 'issue_date']
    ordering = ['expiry_date']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Item.objects.none()
        return Item.objects.filter(user=self.request.user).prefetch_related('transactions')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'])
    def redeem(self, request, pk=None):
        item = self.get_object()
        item.is_used = True
        item.save(update_fields=['is_used'])
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=['get', 'post'], url_path='transactions')
    def transactions(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = TransactionSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            serializer.save(item=item)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = TransactionSerializer(item.transactions.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='shares')
    def shares(self, request, pk=None):
        item = self.get_object()
        if request.method == 'POST':
            serializer = ItemShareSerializer(data=request.data, context={'item': item, 'request': request})
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        serializer = ItemShareSerializer(item.shared_with.all(), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['delete'], url_path=r'shares/(?P<share_id>\d+)')
    def delete_share(self, request, pk=None, share_id=None):
        item = self.get_object()
        share = get_object_or_404(ItemShare, item=item, pk=share_id)
        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TransactionViewSet(viewsets.ModelViewSet):
    """
    Direct access to individual transactions. Creation happens via
    /items/{id}/transactions/ since a transaction always belongs to an item.
    """
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ['get', 'put', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return Transaction.objects.none()
        return Transaction.objects.filter(item__user=self.request.user).order_by('-date')


class UserPreferenceView(generics.RetrieveUpdateAPIView):
    """Singleton settings object for the authenticated user."""
    serializer_class = UserPreferenceSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        preference, _created = UserPreference.objects.get_or_create(user=self.request.user)
        return preference


class UserProfileView(generics.RetrieveUpdateAPIView):
    """Singleton profile object (Apprise notification URLs) for the authenticated user."""
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        profile, _created = UserProfile.objects.get_or_create(user=self.request.user)
        return profile
