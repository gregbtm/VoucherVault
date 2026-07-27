from rest_framework import serializers
from .models import HelpTopic, HelpAccessLog


class HelpTopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = HelpTopic
        fields = ['id', 'title', 'slug', 'category', 'content', 'description', 'tags', 'related_topics']
        read_only_fields = ['id']


class HelpAccessLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = HelpAccessLog
        fields = ['id', 'topic', 'accessed_at']
        read_only_fields = ['id', 'accessed_at']
