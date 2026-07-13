from django import forms
from .models import *
import os
import re
import qrcode
from io import BytesIO
from django.http import HttpResponse
import apprise
from django import forms
from django.core.validators import URLValidator
from django.db.models import Q
from .models import *
from django.utils.translation import gettext_lazy as _
from datetime import timedelta
from django.utils import timezone

def validate_uploaded_file(file):
    allowed_content_types = ['image/jpeg', 'image/png', 'image/jpg', 'application/pdf']
    allowed_extensions = ['.jpeg', '.jpg', '.png', '.pdf']

    if hasattr(file, 'content_type'):
        if file.content_type not in allowed_content_types:
            raise forms.ValidationError(_('File type is not supported.'))

    ext = os.path.splitext(file.name)[1].lower()
    if ext not in allowed_extensions:
        raise forms.ValidationError(_('File extension is not supported.'))

    if file.size > 5 * 1024 * 1024:  # 5MB
        raise forms.ValidationError(_('File size is too large.'))

    return file


class ItemForm(forms.ModelForm):
    file = forms.FileField(required=False)
    value_type = forms.CharField(widget=forms.HiddenInput(), initial='money')
    tile_color = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
        label=_('Tile Color')
    )
    new_tags = forms.CharField(
        required=False,
        label=_('New Tags'),
        help_text=_('Comma-separated names of new tags to create and attach.'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('e.g. groceries, discount')}),
    )

    class Meta:
        model = Item
        fields = ['name', 'issuer', 'redeem_code', 'card_number', 'pin', 'issue_date', 'expiry_date', 'description', 'logo_slug', 'type', 'value', 'value_type', 'currency', 'file', 'code_type', 'tile_color', 'wallet', 'tags', 'notes', 'notify_days_before', 'balance_check_url']
        widgets = {
            'issue_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'expiry_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'tags': forms.CheckboxSelectMultiple(),
            'notes': forms.Textarea(attrs={'rows': 3}),
            'notify_days_before': forms.NumberInput(attrs={'min': 0}),
            'card_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': _('Leave blank to display the redeem code instead'),
            }),
            'balance_check_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': _("The merchant's balance/validity check page"),
            }),
        }

    def __init__(self, *args, user=None, **kwargs):
        super(ItemForm, self).__init__(*args, **kwargs)
        if 'data' in kwargs:
            item_type = kwargs['data'].get('type')
            if item_type == 'loyaltycard':
                self.fields['value'].required = False
            else:
                self.fields['value'].required = True

        # Make expiry_date optional
        self.fields['expiry_date'].required = False

        # Scope wallet/tag choices to the owning user so items can't be
        # organised into another user's wallet or tags.
        self.fields['wallet'].required = False
        if user is not None:
            self.fields['wallet'].queryset = Wallet.objects.filter(
                Q(user=user) | Q(shared_with=user)
            ).distinct()
            self.fields['tags'].queryset = Tag.objects.filter(user=user)
        else:
            self.fields['wallet'].queryset = Wallet.objects.none()
            self.fields['tags'].queryset = Tag.objects.none()

        # Default a brand-new item to "No Barcode" rather than the model's
        # own "qrcode" default - most items start out with nothing scanned
        # yet, and a scan/AI-photo/.pkpass import updates this field for you
        # once it actually detects a symbology (see scanner.js). Editing an
        # existing item, or duplicating one (which passes code_type via its
        # own explicit `initial=`, taking precedence over this), is unaffected.
        # Must go through self.initial (not just field.initial) - ModelForm's
        # own __init__ already seeded self.initial['code_type'] from the
        # unsaved instance's model-default value ("qrcode"), and a BoundField
        # always prefers self.initial over field.initial when both are set.
        # Item.id is a UUIDField with default=uuid.uuid4, so even a
        # brand-new unsaved Item() already has a non-empty pk - _state.adding
        # is the actual "hasn't been saved to the DB yet" signal here.
        is_new_item = self.instance._state.adding
        caller_set_code_type = 'code_type' in (kwargs.get('initial') or {})
        if is_new_item and not caller_set_code_type:
            self.initial['code_type'] = 'none'

    def clean_new_tags(self):
        raw = self.cleaned_data.get('new_tags', '')
        return [name.strip() for name in raw.split(',') if name.strip()]

    def clean_tile_color(self):
        color = self.cleaned_data.get('tile_color')

        # Treat UI default placeholder values as unset
        if color in ['#1e1e1e', '#f3f3f3', '']:  # Add more fallback defaults if needed
            return None

        return color

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            validate_uploaded_file(file)
        return file

    def clean(self):
        cleaned_data = super().clean()
        item_type = cleaned_data.get('type')
        value_type = cleaned_data.get('value_type')
        value = cleaned_data.get('value')
        expiry_date = cleaned_data.get('expiry_date')

        # Set expiry_date to 50 years in the future if not provided
        if not expiry_date:
            cleaned_data['expiry_date'] = timezone.now() + timedelta(days=50*365)  # 50 years in the future

        if item_type == 'loyaltycard' and value != 0:
            error_msg_value = _('Value must be zero for loyalty cards.')
            raise forms.ValidationError(error_msg_value)
        if item_type == 'coupon':
            if value_type == 'money':
                if value is None or value < 0:
                    raise forms.ValidationError(_('Value must be a positive monetary amount.'))
            elif value_type == 'percentage':
                if value is None or value < 0 or value > 100:
                    raise forms.ValidationError(_('Percentage value must be between 0 and 100.'))
            elif value_type == 'multiplier':
                if value is None or value < 1:
                    raise forms.ValidationError(_('Multiplier must be 1 or higher.'))
        elif item_type != 'loyaltycard' and (value is None or value < 0):
            error_message_positive = _('Value must be positive.')
            raise forms.ValidationError(error_message_positive)
        
        return cleaned_data

class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ['description', 'value']
    
    def __init__(self, *args, **kwargs):
        self.item = kwargs.pop('item', None)
        super(TransactionForm, self).__init__(*args, **kwargs)

    def clean_value(self):
        value = self.cleaned_data['value']
        if value >= 0:
            error_msg_transaction = _('Transaction value must be negative.')
            raise forms.ValidationError(error_msg_transaction)
        
        if self.item:
            # Calculate the total value after applying this transaction
            total_value = self.item.get_current_balance() + value
            if total_value < 0:
                error_msg_value_calc = _('Transaction would result in negative item value.')
                raise forms.ValidationError(error_msg_value_calc)
        return value     

class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['apprise_urls']
        widgets = {
            'apprise_urls': forms.Textarea(
                attrs={
                    'rows': 3,
                    'class': 'form-control',
                    'placeholder': 'tgram://bottoken1/ChatID1,tgram://bottoken2/ChatID2'
                }
            ),
        }

class UserPreferenceForm(forms.ModelForm):
    class Meta:
        model = UserPreference
        fields = [
            'show_issue_date', 'show_expiry_date', 'show_value', 'show_description',
            'sort_by', 'sort_order', 'view_mode', 'fixer_api_key', 'default_currency',
            'keep_screen_awake', 'oled_dark_mode', 'offline_cache_enabled',
        ]
        widgets = {
            'show_issue_date': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'show_expiry_date': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'show_value': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'show_description': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sort_by': forms.Select(attrs={'class': 'form-select'}),
            'sort_order': forms.Select(attrs={'class': 'form-select'}),
            'view_mode': forms.Select(attrs={'class': 'form-select'}),
            'fixer_api_key': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('Enter your Fixer.io API key')}),
            'default_currency': forms.Select(attrs={'class': 'form-select'}),
            'keep_screen_awake': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'oled_dark_mode': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'offline_cache_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ['file']

    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            validate_uploaded_file(file)
        return file


class WalletShareForm(forms.Form):
    username = forms.CharField(
        label=_('Username'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('Username to invite')}),
    )

    def __init__(self, *args, wallet=None, **kwargs):
        self.wallet = wallet
        super().__init__(*args, **kwargs)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise forms.ValidationError(_('No user with that username exists.'))
        if self.wallet is not None:
            if user == self.wallet.user:
                raise forms.ValidationError(_('You already own this wallet.'))
            if self.wallet.shared_with.filter(pk=user.pk).exists():
                raise forms.ValidationError(_('This wallet is already shared with that user.'))
        self.cleaned_data['user'] = user
        return username


class WalletForm(forms.ModelForm):
    class Meta:
        model = Wallet
        fields = ['name', 'description', 'icon', 'color']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('e.g. Supermarkets')}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'icon': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'bi-cart'}),
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data['name']
        qs = Wallet.objects.filter(user=self.user, name=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if self.user is not None and qs.exists():
            raise forms.ValidationError(_('You already have a wallet with this name.'))
        return name

class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ['name', 'color']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': _('e.g. discount')}),
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-control form-control-color'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data['name']
        qs = Tag.objects.filter(user=self.user, name=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if self.user is not None and qs.exists():
            raise forms.ValidationError(_('You already have a tag with this name.'))
        return name


class SiteConfigurationForm(forms.ModelForm):
    """
    Every field on SiteConfiguration, editable from the in-app Site
    Settings page. Fields in SiteConfiguration.SECRET_FIELDS render as
    password inputs that always display blank (never round-trip the
    actual secret back into the page) and, if left blank on submit, keep
    whatever was already stored instead of blanking it out - see save().
    """
    class Meta:
        model = SiteConfiguration
        fields = [
            'expiry_threshold_days', 'expiry_last_notification_days', 'ntfy_default_server',
            'webpush_vapid_public_key', 'webpush_vapid_private_key', 'webpush_vapid_claims_email',
            'merchant_logos_enabled',
            'share_via_smart_enabled', 'share_link_expiry_days', 'share_link_pin_enabled',
            'ocr_backend', 'anthropic_api_key', 'anthropic_ocr_model', 'openai_api_key', 'openai_ocr_model',
            'pkpass_cert_path', 'pkpass_cert_password', 'pkpass_wwdr_cert_path',
            'pkpass_team_id', 'pkpass_pass_type_id', 'pkpass_organization_name',
            'google_wallet_service_account_key_path', 'google_wallet_issuer_id', 'google_wallet_class_id',
            'update_check_enabled', 'update_check_repo',
            'portainer_webhook_url',
            'scheduled_backup_enabled', 'backup_retention_count',
        ]
        widgets = {
            'expiry_threshold_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'expiry_last_notification_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'ntfy_default_server': forms.TextInput(attrs={'class': 'form-control'}),
            'webpush_vapid_public_key': forms.TextInput(attrs={'class': 'form-control'}),
            'webpush_vapid_claims_email': forms.TextInput(attrs={'class': 'form-control'}),
            'merchant_logos_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'share_via_smart_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'share_link_expiry_days': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'share_link_pin_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'ocr_backend': forms.Select(attrs={'class': 'form-select'}),
            'anthropic_ocr_model': forms.TextInput(attrs={'class': 'form-control'}),
            'openai_ocr_model': forms.TextInput(attrs={'class': 'form-control'}),
            'pkpass_cert_path': forms.TextInput(attrs={'class': 'form-control'}),
            'pkpass_wwdr_cert_path': forms.TextInput(attrs={'class': 'form-control'}),
            'pkpass_team_id': forms.TextInput(attrs={'class': 'form-control'}),
            'pkpass_pass_type_id': forms.TextInput(attrs={'class': 'form-control'}),
            'pkpass_organization_name': forms.TextInput(attrs={'class': 'form-control'}),
            'google_wallet_service_account_key_path': forms.TextInput(attrs={'class': 'form-control'}),
            'google_wallet_issuer_id': forms.TextInput(attrs={'class': 'form-control'}),
            'google_wallet_class_id': forms.TextInput(attrs={'class': 'form-control'}),
            'update_check_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'update_check_repo': forms.TextInput(attrs={'class': 'form-control'}),
            'portainer_webhook_url': forms.TextInput(attrs={'class': 'form-control'}),
            'scheduled_backup_enabled': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'backup_retention_count': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
        }

    REPO_RE = re.compile(r'^[\w.-]+/[\w.-]+$')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_secret_values = {
            field: getattr(self.instance, field, '') for field in SiteConfiguration.SECRET_FIELDS
        }
        for field in SiteConfiguration.SECRET_FIELDS:
            self.fields[field].required = False
            placeholder = _('•••• leave blank to keep current value') if self._original_secret_values[field] else _('Not set')
            self.fields[field].widget = forms.PasswordInput(render_value=False, attrs={'class': 'form-control', 'placeholder': placeholder})

    def _effective_secret(self, field):
        """
        A blank submitted value for a SECRET_FIELDS field means "leave the
        stored value alone" (see save()), not "this is blank" - cross-field
        checks involving a secret need the value that will actually end up
        on the instance, not the always-blank submitted one.
        """
        return self.cleaned_data.get(field) or self._original_secret_values.get(field, '')

    def _clean_url(self, field_name):
        value = (self.cleaned_data.get(field_name) or '').strip()
        if value:
            try:
                URLValidator(schemes=['http', 'https'])(value)
            except forms.ValidationError:
                raise forms.ValidationError(_('Enter a valid http:// or https:// URL.'))
        return value

    def clean_portainer_webhook_url(self):
        return self._clean_url('portainer_webhook_url')

    def clean_ntfy_default_server(self):
        return self._clean_url('ntfy_default_server')

    def clean_update_check_repo(self):
        value = (self.cleaned_data.get('update_check_repo') or '').strip()
        if value and not self.REPO_RE.match(value):
            raise forms.ValidationError(_('Enter a GitHub repository as "owner/repo".'))
        return value

    def clean_expiry_threshold_days(self):
        value = self.cleaned_data.get('expiry_threshold_days')
        if value is not None and value < 1:
            raise forms.ValidationError(_('Must be at least 1 day.'))
        return value

    def clean_expiry_last_notification_days(self):
        value = self.cleaned_data.get('expiry_last_notification_days')
        if value is not None and value < 1:
            raise forms.ValidationError(_('Must be at least 1 day.'))
        return value

    def clean_backup_retention_count(self):
        value = self.cleaned_data.get('backup_retention_count')
        if value is not None and value < 1:
            raise forms.ValidationError(_('Must keep at least 1 backup.'))
        return value

    def clean_anthropic_api_key(self):
        value = self.cleaned_data.get('anthropic_api_key', '')
        if value and not value.startswith('sk-ant-'):
            raise forms.ValidationError(_('Anthropic API keys start with "sk-ant-" - double check you copied the whole key.'))
        return value

    def clean_openai_api_key(self):
        value = self.cleaned_data.get('openai_api_key', '')
        if value and not value.startswith('sk-'):
            raise forms.ValidationError(_('OpenAI API keys start with "sk-" - double check you copied the whole key.'))
        return value

    def _clean_cert_path(self, field_name):
        value = (self.cleaned_data.get(field_name) or '').strip()
        if value and not os.path.isfile(value):
            raise forms.ValidationError(_('No file found at this path inside the container - check the path and that the volume is mounted.'))
        return value

    def clean_pkpass_cert_path(self):
        return self._clean_cert_path('pkpass_cert_path')

    def clean_pkpass_wwdr_cert_path(self):
        return self._clean_cert_path('pkpass_wwdr_cert_path')

    def clean_google_wallet_service_account_key_path(self):
        return self._clean_cert_path('google_wallet_service_account_key_path')

    def clean(self):
        cleaned_data = super().clean()

        threshold = cleaned_data.get('expiry_threshold_days')
        final_warning = cleaned_data.get('expiry_last_notification_days')
        if threshold is not None and final_warning is not None and final_warning > threshold:
            self.add_error(
                'expiry_last_notification_days',
                _('The final warning must be sooner than the initial warning threshold.'),
            )

        public_key = cleaned_data.get('webpush_vapid_public_key')
        private_key = self._effective_secret('webpush_vapid_private_key')
        if bool(public_key) != bool(private_key):
            msg = _('Both the VAPID public and private key are required together to enable Web Push.')
            self.add_error('webpush_vapid_public_key', msg)
            self.add_error('webpush_vapid_private_key', msg)

        key_path = cleaned_data.get('google_wallet_service_account_key_path')
        issuer_id = cleaned_data.get('google_wallet_issuer_id')
        if bool(key_path) != bool(issuer_id):
            msg = _('Both the service account key path and issuer ID are required together to enable Google Wallet export.')
            self.add_error('google_wallet_service_account_key_path', msg)
            self.add_error('google_wallet_issuer_id', msg)

        pkpass_fields = ['pkpass_cert_path', 'pkpass_wwdr_cert_path', 'pkpass_team_id', 'pkpass_pass_type_id']
        filled = [f for f in pkpass_fields if cleaned_data.get(f)]
        if filled and len(filled) < len(pkpass_fields):
            msg = _('Certificate path, WWDR certificate path, Team ID, and Pass Type ID are all required together to enable Apple Wallet export.')
            for f in pkpass_fields:
                self.add_error(f, msg)

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        for field in SiteConfiguration.SECRET_FIELDS:
            if not self.cleaned_data.get(field):
                setattr(instance, field, self._original_secret_values[field])
        if commit:
            instance.save()
        return instance