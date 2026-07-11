import os
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from imports.models import ImportJob
from myapp.models import Item, ItemShare, MerchantProfile, Tag, Transaction, UserPreference, UserProfile, Wallet
from myapp.utils import generate_code_image_base64
from notify.models import NotificationLog, NotificationRule

_UNSET = object()


class WalletSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField(read_only=True)
    is_owner = serializers.SerializerMethodField(read_only=True)
    shared_with_usernames = serializers.SlugRelatedField(
        source='shared_with', slug_field='username', many=True, read_only=True
    )

    class Meta:
        model = Wallet
        fields = [
            'id', 'name', 'description', 'icon', 'color', 'item_count',
            'is_owner', 'shared_with_usernames', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'item_count', 'is_owner', 'shared_with_usernames', 'created_at', 'updated_at']

    def get_item_count(self, wallet) -> int:
        # Uses the queryset's annotation when present (list/retrieve); falls
        # back to a live count otherwise (e.g. right after create/update).
        count = getattr(wallet, 'item_count', None)
        return count if count is not None else wallet.items.count()

    def get_is_owner(self, wallet) -> bool:
        request = self.context.get('request')
        return bool(request) and wallet.user_id == request.user.id

    def validate_name(self, name):
        request = self.context['request']
        qs = Wallet.objects.filter(user=request.user, name=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_('You already have a wallet with this name.'))
        return name


class TagSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'name', 'color', 'item_count']
        read_only_fields = ['id', 'item_count']

    def get_item_count(self, tag) -> int:
        count = getattr(tag, 'item_count', None)
        return count if count is not None else tag.items.count()

    def validate_name(self, name):
        request = self.context['request']
        qs = Tag.objects.filter(user=request.user, name=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_('You already have a tag with this name.'))
        return name


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'item', 'date', 'description', 'value']
        read_only_fields = ['id', 'item']

    def validate_value(self, value):
        if value >= 0:
            raise serializers.ValidationError(_('Transaction value must be negative.'))
        return value

    def validate(self, attrs):
        item = self.context.get('item') or getattr(self.instance, 'item', None)
        value = attrs.get('value', getattr(self.instance, 'value', None))
        if item is not None and value is not None:
            other_transactions = item.transactions.exclude(pk=getattr(self.instance, 'pk', None))
            total_value = item.value + sum(t.value for t in other_transactions) + value
            if total_value < 0:
                raise serializers.ValidationError(_('Transaction would result in negative item value.'))
        return attrs


class ItemShareSerializer(serializers.ModelSerializer):
    username = serializers.CharField(write_only=True)
    shared_with_username = serializers.CharField(source='shared_with_user.username', read_only=True)
    shared_by_username = serializers.CharField(source='shared_by.username', read_only=True)

    class Meta:
        model = ItemShare
        fields = ['id', 'item', 'username', 'shared_with_username', 'shared_by_username', 'shared_at']
        read_only_fields = ['id', 'item', 'shared_at']

    def validate_username(self, username):
        request = self.context['request']
        if username == request.user.username:
            raise serializers.ValidationError(_('You cannot share an item with yourself.'))
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            raise serializers.ValidationError(_("User '%(username)s' not found.") % {'username': username})

    def create(self, validated_data):
        recipient = validated_data.pop('username')
        item = self.context['item']
        share, _created = ItemShare.objects.get_or_create(
            item=item,
            shared_with_user=recipient,
            defaults={'shared_by': self.context['request'].user},
        )
        return share


class ItemSerializer(serializers.ModelSerializer):
    days_until_expiry = serializers.SerializerMethodField(read_only=True)
    transaction_total = serializers.SerializerMethodField(read_only=True)
    wallet_name = serializers.CharField(source='wallet.name', read_only=True, default=None)
    tags = TagSerializer(many=True, read_only=True)
    tag_ids = serializers.PrimaryKeyRelatedField(
        many=True, write_only=True, required=False, source='tags', queryset=Tag.objects.none()
    )

    class Meta:
        model = Item
        fields = [
            'id', 'type', 'name', 'redeem_code', 'card_number', 'code_type', 'pin', 'issuer',
            'issue_date', 'expiry_date', 'description', 'logo_slug', 'value',
            'value_type', 'currency', 'is_used', 'is_pinned', 'is_archived', 'tile_color',
            'file', 'qr_code_base64', 'default_expiry_notification_sent',
            'final_expiry_notification_sent', 'days_until_expiry', 'transaction_total',
            'wallet', 'wallet_name', 'tags', 'tag_ids', 'notes', 'notify_days_before', 'source',
            'last_used_at',
        ]
        read_only_fields = [
            'id', 'qr_code_base64', 'default_expiry_notification_sent',
            'final_expiry_notification_sent', 'source', 'last_used_at',
        ]
        extra_kwargs = {
            'wallet': {'required': False, 'allow_null': True, 'queryset': Wallet.objects.none()},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mirrors ItemForm: expiry_date defaults to +50y, value is optional for loyalty cards.
        self.fields['expiry_date'].required = False
        self.fields['value'].required = False

        # Scope wallet/tag choices to the requesting user so an item can never
        # be filed under, or tagged with, another user's wallet/tags.
        request = self.context.get('request')
        if request is not None and request.user.is_authenticated:
            self.fields['wallet'].queryset = Wallet.objects.filter(user=request.user)
            # tag_ids is many=True, so DRF wraps it in a ManyRelatedField whose
            # own .queryset is unused — the child_relation holds the real one.
            self.fields['tag_ids'].child_relation.queryset = Tag.objects.filter(user=request.user)

    def get_days_until_expiry(self, item) -> int | None:
        if not item.expiry_date:
            return None
        return (item.expiry_date - timezone.now().date()).days

    def get_transaction_total(self, item) -> str:
        total = sum((t.value for t in item.transactions.all()), item.value)
        return str(total)

    def validate(self, attrs):
        item_type = attrs.get('type', getattr(self.instance, 'type', None))
        value_type = attrs.get('value_type', getattr(self.instance, 'value_type', 'money'))
        value = attrs.get('value', getattr(self.instance, 'value', None))

        if item_type == 'loyaltycard':
            if value not in (0, None):
                raise serializers.ValidationError({'value': _('Value must be zero for loyalty cards.')})
            attrs['value'] = 0
        elif item_type == 'coupon':
            if value_type == 'money':
                if value is None or value < 0:
                    raise serializers.ValidationError({'value': _('Value must be a positive monetary amount.')})
            elif value_type == 'percentage':
                if value is None or value < 0 or value > 100:
                    raise serializers.ValidationError({'value': _('Percentage value must be between 0 and 100.')})
            elif value_type == 'multiplier':
                if value is None or value < 1:
                    raise serializers.ValidationError({'value': _('Multiplier must be 1 or higher.')})
        elif item_type != 'loyaltycard' and (value is None or value < 0):
            raise serializers.ValidationError({'value': _('Value must be positive.')})

        return attrs

    def _save_uploaded_file(self, item, file_obj):
        username = str(item.user)
        user_folder = os.path.join('uploads', username)
        safe_name = get_valid_filename(os.path.basename(file_obj.name))
        file_name = f"{item.id}_{safe_name}"
        relative_path = os.path.join(user_folder, file_name)
        item.file.save(relative_path, file_obj, save=True)

    def create(self, validated_data):
        file_obj = validated_data.pop('file', None)
        tags = validated_data.pop('tags', _UNSET)
        if not validated_data.get('expiry_date'):
            validated_data['expiry_date'] = timezone.now().date() + timedelta(days=50 * 365)
        # Item.issue_date's model default is timezone.now (a datetime) even though
        # it's a DateField; set it explicitly here to avoid storing a datetime.
        validated_data.setdefault('issue_date', timezone.now().date())

        item = Item(**validated_data)
        item.qr_code_base64, item.code_type = generate_code_image_base64(item)
        item.save()

        if tags is not _UNSET:
            item.tags.set(tags)

        if file_obj:
            self._save_uploaded_file(item, file_obj)

        return item

    def update(self, instance, validated_data):
        file_obj = validated_data.pop('file', None)
        tags = validated_data.pop('tags', _UNSET)
        original_redeem_code = instance.redeem_code
        original_code_type = instance.code_type

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if instance.redeem_code != original_redeem_code or instance.code_type != original_code_type:
            instance.qr_code_base64, instance.code_type = generate_code_image_base64(instance)

        instance.save()

        if tags is not _UNSET:
            instance.tags.set(tags)

        if file_obj:
            old_file_path = instance.file.path if instance.file else None
            self._save_uploaded_file(instance, file_obj)
            if old_file_path and os.path.isfile(old_file_path):
                os.remove(old_file_path)

        return instance


class UserPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPreference
        fields = [
            'show_issue_date', 'show_expiry_date', 'show_value', 'show_description',
            'sort_by', 'sort_order', 'view_mode', 'fixer_api_key', 'default_currency',
        ]


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ['apprise_urls']


class NotificationRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationRule
        fields = ['id', 'name', 'backend', 'config', 'enabled', 'event_types', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate_name(self, name):
        request = self.context['request']
        qs = NotificationRule.objects.filter(user=request.user, name=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(_('You already have a notification rule with this name.'))
        return name

    def validate_event_types(self, event_types):
        valid = {choice for choice, _label in NotificationRule.EVENT_CHOICES}
        invalid = set(event_types) - valid
        if invalid:
            raise serializers.ValidationError(_('Invalid event type(s): %(types)s') % {'types': ', '.join(sorted(invalid))})
        return event_types

    def validate(self, attrs):
        backend = attrs.get('backend', getattr(self.instance, 'backend', None))
        config = attrs.get('config', getattr(self.instance, 'config', None) or {})

        if backend == 'ntfy' and not (config.get('server') and config.get('topic')):
            raise serializers.ValidationError({'config': _('ntfy config requires "server" and "topic".')})
        elif backend == 'webhook' and not config.get('url'):
            raise serializers.ValidationError({'config': _('webhook config requires "url".')})
        elif backend == 'apprise' and not config.get('urls'):
            raise serializers.ValidationError({'config': _('apprise config requires "urls".')})

        return attrs


class NotificationLogSerializer(serializers.ModelSerializer):
    rule_name = serializers.CharField(source='rule.name', read_only=True, default=None)
    item_name = serializers.CharField(source='item.name', read_only=True, default=None)

    class Meta:
        model = NotificationLog
        fields = ['id', 'rule', 'rule_name', 'item', 'item_name', 'event_type', 'sent_at', 'success', 'detail']
        read_only_fields = fields


class ImportJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportJob
        fields = [
            'id', 'source_type', 'status', 'imported_count', 'error_count',
            'errors', 'created_at', 'completed_at',
        ]
        read_only_fields = fields


class MerchantProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerchantProfile
        fields = ['id', 'name', 'domain', 'logo_url', 'brand_color', 'fetched_at']
        read_only_fields = fields
