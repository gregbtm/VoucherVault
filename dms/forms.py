from django import forms
from .models import DMSProvider


class DMSProviderForm(forms.ModelForm):
    class Meta:
        model = DMSProvider
        fields = [
            'name', 'provider', 'base_url', 'enabled',
            'api_token', 'username', 'password',
            'docspell_collective', 'docspell_source_id',
            'auto_push', 'auto_pull', 'pull_tag', 'pull_correspondent',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'My Paperless Server'}),
            'provider': forms.Select(attrs={'class': 'form-select', 'id': 'id_provider'}),
            'base_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://paperless.example.com'}),
            'api_token': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password', 'render_value': True}, render_value=True),
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'password': forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password', 'render_value': True}, render_value=True),
            'docspell_collective': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'mycollective'}),
            'docspell_source_id': forms.TextInput(attrs={'class': 'form-control'}),
            'pull_tag': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'vouchervault'}),
            'pull_correspondent': forms.TextInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'api_token': 'API Token',
            'docspell_collective': 'Collective Name',
            'docspell_source_id': 'Upload Source ID',
        }
        help_texts = {
            'base_url': 'Base URL of your DMS, e.g. https://paperless.example.com (no trailing slash).',
            'api_token': 'Paperless-ngx / PaperMerge: your API token from the admin UI.',
            'username': 'Docspell: your login username.',
            'password': 'Docspell: your login password. A dedicated service account is recommended.',
            'docspell_collective': 'Docspell: the collective (tenant) name your account belongs to.',
            'docspell_source_id': 'Docspell: the "source" ID to use for uploads (create one in Docspell → Upload Sources).',
            'auto_push': 'Automatically upload new VoucherVault attachments to this DMS when they are saved.',
            'auto_pull': 'Periodically check this DMS for new documents tagged for VoucherVault and import them.',
            'pull_tag': 'Only pull documents with this tag. Leave blank to pull all readable documents.',
            'pull_correspondent': 'Only pull documents from this correspondent/source (optional).',
        }
