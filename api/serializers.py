import os
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from myapp.models import Item, ItemShare, Transaction, UserPreference, UserProfile
from myapp.utils import generate_code_image_base64


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

    class Meta:
        model = Item
        fields = [
            'id', 'type', 'name', 'redeem_code', 'code_type', 'pin', 'issuer',
            'issue_date', 'expiry_date', 'description', 'logo_slug', 'value',
            'value_type', 'currency', 'is_used', 'is_pinned', 'tile_color',
            'file', 'qr_code_base64', 'default_expiry_notification_sent',
            'final_expiry_notification_sent', 'days_until_expiry', 'transaction_total',
        ]
        read_only_fields = [
            'id', 'qr_code_base64', 'default_expiry_notification_sent',
            'final_expiry_notification_sent',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mirrors ItemForm: expiry_date defaults to +50y, value is optional for loyalty cards.
        self.fields['expiry_date'].required = False
        self.fields['value'].required = False

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
        if not validated_data.get('expiry_date'):
            validated_data['expiry_date'] = timezone.now().date() + timedelta(days=50 * 365)
        # Item.issue_date's model default is timezone.now (a datetime) even though
        # it's a DateField; set it explicitly here to avoid storing a datetime.
        validated_data.setdefault('issue_date', timezone.now().date())

        item = Item(**validated_data)
        item.qr_code_base64, item.code_type = generate_code_image_base64(item)
        item.save()

        if file_obj:
            self._save_uploaded_file(item, file_obj)

        return item

    def update(self, instance, validated_data):
        file_obj = validated_data.pop('file', None)
        original_redeem_code = instance.redeem_code
        original_code_type = instance.code_type

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if instance.redeem_code != original_redeem_code or instance.code_type != original_code_type:
            instance.qr_code_base64, instance.code_type = generate_code_image_base64(instance)

        instance.save()

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
