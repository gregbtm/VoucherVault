import os

from django import forms
from django.utils.translation import gettext_lazy as _

from .backends.webpush import webpush_enabled
from .models import NotificationRule

PRIORITY_CHOICES = [
    ('min', _('Min')),
    ('low', _('Low')),
    ('default', _('Default')),
    ('high', _('High')),
    ('urgent', _('Urgent')),
]


class NotificationRuleForm(forms.ModelForm):
    """
    A single form covering every backend's config fields; only the fields
    relevant to the selected `backend` are shown (via JS) and persisted
    into the model's `config` JSONField.
    """
    event_types = forms.MultipleChoiceField(
        choices=NotificationRule.EVENT_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=True,
        label=_('Notify me when'),
    )

    ntfy_server = forms.CharField(
        required=False, label=_('ntfy Server'),
        widget=forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://ntfy.sh'}),
    )
    ntfy_topic = forms.CharField(
        required=False, label=_('ntfy Topic'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'vouchervault'}),
    )
    ntfy_priority = forms.ChoiceField(
        required=False, choices=PRIORITY_CHOICES, initial='default', label=_('Priority'),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    ntfy_token = forms.CharField(
        required=False, label=_('ntfy Access Token (optional)'),
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    webhook_url = forms.URLField(
        required=False, label=_('Webhook URL'),
        widget=forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://n8n.example.com/webhook/vouchervault'}),
    )
    webhook_header_name = forms.CharField(
        required=False, label=_('Custom Header Name (optional)'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'X-Secret'}),
    )
    webhook_header_value = forms.CharField(
        required=False, label=_('Custom Header Value (optional)'),
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    apprise_urls = forms.CharField(
        required=False, label=_('Apprise URLs'),
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'tgram://bottoken/ChatID,mailto://user:pass@example.com'}),
    )

    class Meta:
        model = NotificationRule
        fields = ['name', 'backend', 'enabled', 'event_types']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'backend': forms.Select(attrs={'class': 'form-select'}),
            'enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not webpush_enabled() and (not self.instance.pk or self.instance.backend != 'webpush'):
            self.fields['backend'].choices = [c for c in self.fields['backend'].choices if c[0] != 'webpush']
        if not self.instance.pk:
            self.fields['ntfy_server'].initial = os.environ.get('NTFY_DEFAULT_SERVER', 'https://ntfy.sh')
        if self.instance.pk:
            self.fields['event_types'].initial = self.instance.event_types
            config = self.instance.config or {}
            if self.instance.backend == 'ntfy':
                self.fields['ntfy_server'].initial = config.get('server', '')
                self.fields['ntfy_topic'].initial = config.get('topic', '')
                self.fields['ntfy_priority'].initial = config.get('priority', 'default')
                self.fields['ntfy_token'].initial = config.get('token', '')
            elif self.instance.backend == 'webhook':
                self.fields['webhook_url'].initial = config.get('url', '')
                headers = config.get('headers') or {}
                if headers:
                    name, value = next(iter(headers.items()))
                    self.fields['webhook_header_name'].initial = name
                    self.fields['webhook_header_value'].initial = value
            elif self.instance.backend == 'apprise':
                urls = config.get('urls', '')
                self.fields['apprise_urls'].initial = urls if isinstance(urls, str) else ','.join(urls)

    def clean_name(self):
        name = self.cleaned_data['name']
        qs = NotificationRule.objects.filter(user=self.user, name=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if self.user is not None and qs.exists():
            raise forms.ValidationError(_('You already have a notification rule with this name.'))
        return name

    def clean(self):
        cleaned_data = super().clean()
        backend = cleaned_data.get('backend')

        if backend == 'ntfy':
            if not cleaned_data.get('ntfy_server') or not cleaned_data.get('ntfy_topic'):
                raise forms.ValidationError(_('ntfy server and topic are required.'))
        elif backend == 'webhook':
            if not cleaned_data.get('webhook_url'):
                raise forms.ValidationError(_('Webhook URL is required.'))
        elif backend == 'apprise':
            if not cleaned_data.get('apprise_urls'):
                raise forms.ValidationError(_('At least one Apprise URL is required.'))

        return cleaned_data

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.event_types = self.cleaned_data['event_types']

        backend = self.cleaned_data['backend']
        if backend == 'ntfy':
            config = {
                'server': self.cleaned_data['ntfy_server'].rstrip('/'),
                'topic': self.cleaned_data['ntfy_topic'],
                'priority': self.cleaned_data.get('ntfy_priority') or 'default',
            }
            if self.cleaned_data.get('ntfy_token'):
                config['token'] = self.cleaned_data['ntfy_token']
        elif backend == 'webhook':
            config = {'url': self.cleaned_data['webhook_url']}
            header_name = self.cleaned_data.get('webhook_header_name')
            header_value = self.cleaned_data.get('webhook_header_value')
            if header_name and header_value:
                config['headers'] = {header_name: header_value}
        elif backend == 'apprise':
            config = {'urls': self.cleaned_data['apprise_urls']}
        else:
            config = {}

        rule.config = config
        if commit:
            rule.save()
        return rule
