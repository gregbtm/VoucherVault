from django.db import models
from django.utils.text import slugify


class HelpTopic(models.Model):
    """In-app help documentation topics"""
    CATEGORY_CHOICES = [
        ('getting-started', 'Getting Started'),
        ('inventory', 'Inventory Management'),
        ('items', 'Items'),
        ('wallets', 'Wallets'),
        ('tags', 'Tags'),
        ('notifications', 'Notifications'),
        ('sharing', 'Sharing & Collaboration'),
        ('settings', 'Settings'),
        ('faq', 'FAQ'),
        ('troubleshooting', 'Troubleshooting'),
    ]

    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    content = models.TextField(help_text='HTML content for the help topic')
    description = models.CharField(max_length=255, blank=True, help_text='Short description for search')
    tags = models.JSONField(default=list, blank=True, help_text='Search tags')
    related_topics = models.ManyToManyField('self', blank=True, symmetrical=False, related_name='related_to')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ['category', 'title']
        verbose_name = 'Help Topic'
        verbose_name_plural = 'Help Topics'

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)


class HelpAccessLog(models.Model):
    """Track which help topics users view"""
    topic = models.ForeignKey(HelpTopic, on_delete=models.CASCADE, related_name='access_logs')
    user = models.ForeignKey('auth.User', on_delete=models.CASCADE, null=True, blank=True)
    accessed_at = models.DateTimeField(auto_now_add=True)
    session_key = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-accessed_at']
        verbose_name = 'Help Access Log'
        verbose_name_plural = 'Help Access Logs'

    def __str__(self):
        return f'{self.topic.title} - {self.accessed_at}'
