from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import models
from django.utils.timezone import now
from .models import HelpTopic, HelpAccessLog
from .serializers import HelpTopicSerializer, HelpAccessLogSerializer


class HelpTopicViewSet(viewsets.ModelViewSet):
    """API for help topics - searchable documentation"""
    queryset = HelpTopic.objects.filter(is_published=True)
    serializer_class = HelpTopicSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'slug'
    pagination_class = None

    def get_queryset(self):
        queryset = super().get_queryset()
        category = self.request.query_params.get('category')
        search = self.request.query_params.get('search')

        if category:
            queryset = queryset.filter(category=category)

        if search:
            queryset = queryset.filter(
                models.Q(title__icontains=search) |
                models.Q(content__icontains=search) |
                models.Q(description__icontains=search)
            )

        return queryset.order_by('category', 'title')

    @action(detail=True, methods=['post'])
    def log_access(self, request, slug=None):
        """Log when a user accesses a help topic"""
        topic = self.get_object()
        log = HelpAccessLog.objects.create(
            topic=topic,
            user=request.user if request.user.is_authenticated else None,
            session_key=request.session.session_key or '',
            accessed_at=now()
        )
        return Response(
            HelpAccessLogSerializer(log).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get all available help categories"""
        categories = HelpTopic.CATEGORY_CHOICES
        return Response({
            'categories': [{'value': val, 'label': label} for val, label in categories]
        })


class HelpAccessLogViewSet(viewsets.ModelViewSet):
    """API for help access logs - analytics"""
    queryset = HelpAccessLog.objects.all()
    serializer_class = HelpAccessLogSerializer
    permission_classes = [permissions.IsAdminUser]
    pagination_class = None

    def get_queryset(self):
        if not self.request.user.is_staff:
            return HelpAccessLog.objects.none()
        return super().get_queryset()
