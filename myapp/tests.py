import json
import os
import urllib.error
import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask
from PIL import Image
from rest_framework.authtoken.models import Token

from django.core.files.uploadedfile import SimpleUploadedFile

from .avatar import generate_initial_avatar, normalize_logo_image
from .forms import ItemForm, SiteConfigurationForm, TagForm, TransactionForm, WalletForm
from .merchant_logos import (
    _logo_sources,
    fetch_merchant_logo,
    get_cached_balance_check_url,
    get_cached_logo,
    get_cached_logos_for_issuers,
    guess_domain,
    remember_balance_check_url,
)
from .ics_calendar import _escape_text, _fold_line, build_ics_calendar
from .nearby_places import _names_match, _normalize, find_nearby_issuer_matches
from .imagehash import compute_dhash, hamming_distance
from .models import AppSettings, BalanceHistory, Document, Item, ItemPublicShare, LoginAuditLog, MerchantProfile, ScanFieldCorrection, SiteConfiguration, Tag, TOTPDevice, Transaction, UpdateCheckStatus, UpstreamSyncStatus, UserPreference, UserProfile, UserWebhook, Wallet, WalletActivity, WalletMembership
from .scan_learning import apply_learned_corrections, record_scan_corrections
from .portainer import PortainerRedeployError, trigger_redeploy
from .test_utils import set_site_config
from .tasks import check_for_update_task, check_upstream_version_task, fetch_merchant_logo_task
from .update_check import _is_newer, _parse_version, check_for_update, check_upstream_version
from .utils import fetch_oidc_discovery, generate_code_image_base64, levenshtein_distance
from .views import _integration_status


def make_item(user, **kwargs):
    defaults = {
        'type': 'voucher',
        'name': 'Test Voucher',
        'redeem_code': 'ABC123',
        'issuer': 'Acme',
        'expiry_date': date.today() + timedelta(days=30),
        'value': '10.00',
        'user': user,
    }
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


class LedgerBalanceTests(TestCase):
    """
    Item.get_current_balance() / Item.objects.with_current_balance() are the
    single source of truth for "starting value plus every transaction",
    replacing several independent copies of this formula that used to live
    in views.py, analytics.py, forms.py, and the API serializer.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_balance_with_no_transactions_equals_item_value(self):
        item = make_item(self.user, value='25.00')
        item.refresh_from_db()
        self.assertEqual(item.get_current_balance(), item.value)

    def test_balance_subtracts_transactions(self):
        item = make_item(self.user, value='25.00')
        item.refresh_from_db()
        Transaction.objects.create(item=item, description='Spend 1', value='-5.00')
        Transaction.objects.create(item=item, description='Spend 2', value='-3.50')
        self.assertEqual(item.get_current_balance(), item.value - Decimal('5') - Decimal('3.50'))

    def test_balance_accepts_prefetched_transactions_without_extra_query(self):
        item = make_item(self.user, value='25.00')
        item.refresh_from_db()
        Transaction.objects.create(item=item, description='Spend', value='-5.00')
        transactions = item.transactions.all()
        list(transactions)  # evaluate once, populating the queryset's result cache

        with self.assertNumQueries(0):
            balance = item.get_current_balance(transactions)
        self.assertEqual(balance, item.value - Decimal('5'))

    def test_with_current_balance_annotation_matches_instance_method(self):
        item = make_item(self.user, value='25.00')
        item.refresh_from_db()
        Transaction.objects.create(item=item, description='Spend', value='-5.00')

        annotated = Item.objects.with_current_balance().get(pk=item.pk)
        self.assertEqual(annotated.current_balance, item.get_current_balance())

    def test_with_current_balance_annotation_with_no_transactions(self):
        item = make_item(self.user, value='25.00')
        item.refresh_from_db()
        annotated = Item.objects.with_current_balance().get(pk=item.pk)
        self.assertEqual(annotated.current_balance, item.value)


class TransactionFormDateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user, value='50.00')
        self.item.refresh_from_db()

    def test_form_valid_without_date(self):
        form = TransactionForm({'description': 'Spend', 'value': '-10.00'}, item=self.item)
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_valid_with_datetime_local_format(self):
        form = TransactionForm(
            {'description': 'Spend', 'value': '-5.00', 'date': '2026-03-15T14:30'},
            item=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        txn = form.save(commit=False)
        self.assertEqual(txn.date.year, 2026)
        self.assertEqual(txn.date.month, 3)

    def test_form_valid_with_date_only_format(self):
        form = TransactionForm(
            {'description': 'Spend', 'value': '-5.00', 'date': '2026-01-20'},
            item=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_invalid_with_garbage_date(self):
        form = TransactionForm(
            {'description': 'Spend', 'value': '-5.00', 'date': 'not-a-date'},
            item=self.item,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('date', form.errors)


class BalanceHistoryOnTransactionSaveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user, value='30.00', type='giftcard')
        self.item.refresh_from_db()
        self.client.force_login(self.user)

    def _post_transaction(self, value, description='Spend'):
        url = reverse('view_item', args=[self.item.id])
        return self.client.post(url, {'description': description, 'value': value})

    def test_transaction_post_creates_balance_history_entry(self):
        response = self._post_transaction('-10.00')
        self.assertRedirects(response, reverse('view_item', args=[self.item.id]))
        self.assertEqual(BalanceHistory.objects.filter(item=self.item).count(), 1)
        entry = BalanceHistory.objects.get(item=self.item)
        self.assertEqual(entry.balance, Decimal('20.00'))

    def test_multiple_transactions_each_create_history_entry(self):
        self._post_transaction('-5.00', 'First')
        self._post_transaction('-3.00', 'Second')
        self.assertEqual(BalanceHistory.objects.filter(item=self.item).count(), 2)
        balances = list(BalanceHistory.objects.filter(item=self.item).order_by('recorded_at').values_list('balance', flat=True))
        self.assertEqual(balances[0], Decimal('25.00'))
        self.assertEqual(balances[1], Decimal('22.00'))

    def test_balance_history_note_contains_description(self):
        self._post_transaction('-7.00', 'Weekly shop')
        entry = BalanceHistory.objects.get(item=self.item)
        self.assertEqual(entry.note, 'Weekly shop')

    def test_item_marked_used_when_balance_reaches_zero(self):
        self._post_transaction('-30.00')
        self.item.refresh_from_db()
        self.assertTrue(self.item.is_used)


class SpendStatsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='100.00')

    def test_no_transactions_returns_zeros(self):
        from .analytics import get_spend_stats
        stats = get_spend_stats(self.user)
        self.assertEqual(stats['total_spent'], '0.00')
        self.assertEqual(stats['redeemed_value'], '0.00')
        self.assertEqual(len(stats['monthly_spend']), 12)

    def test_total_spent_sums_negative_transactions_only(self):
        from .analytics import get_spend_stats
        Transaction.objects.create(item=self.item, description='Spend', value='-10.00')
        Transaction.objects.create(item=self.item, description='Top-up', value='5.00')
        stats = get_spend_stats(self.user)
        self.assertEqual(stats['total_spent'], '10.00')

    def test_redeemed_value_excludes_loyalty_cards(self):
        from .analytics import get_spend_stats
        loyalty = make_item(self.user, type='loyaltycard', value='500.00', is_used=True, redeem_code='LC')
        regular = make_item(self.user, type='giftcard', value='20.00', is_used=True, redeem_code='GC')
        stats = get_spend_stats(self.user)
        self.assertEqual(stats['redeemed_value'], '20.00')

    def test_monthly_spend_always_has_12_entries(self):
        from .analytics import get_spend_stats
        stats = get_spend_stats(self.user)
        months = stats['monthly_spend']
        self.assertEqual(len(months), 12)
        for entry in months:
            self.assertRegex(entry['month'], r'^\d{4}-\d{2}$')

    def test_monthly_spend_reflects_current_month_transactions(self):
        from .analytics import get_spend_stats
        from django.utils import timezone as tz
        Transaction.objects.create(item=self.item, description='S', value='-15.00', date=tz.now())
        stats = get_spend_stats(self.user)
        current_month = tz.now().strftime('%Y-%m')
        current_entry = next(e for e in stats['monthly_spend'] if e['month'] == current_month)
        self.assertEqual(current_entry['amount'], '15.00')

    def test_other_users_transactions_excluded(self):
        from .analytics import get_spend_stats
        bob = User.objects.create_user(username='bob', password='pw12345!')
        bob_item = make_item(bob, type='giftcard', value='100.00', redeem_code='BOB1')
        Transaction.objects.create(item=bob_item, description='Bob spend', value='-99.00')
        stats = get_spend_stats(self.user)
        self.assertEqual(stats['total_spent'], '0.00')


class DefaultCurrencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_new_item_defaults_to_gbp(self):
        item = Item.objects.create(
            type='voucher', name='Test', redeem_code='ABC', issuer='Acme',
            expiry_date=date.today() + timedelta(days=30), value='10.00', user=self.user,
        )
        self.assertEqual(item.currency, 'GBP')

    def test_new_user_preference_defaults_to_gbp(self):
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        self.assertEqual(preferences.default_currency, 'GBP')

    def test_create_item_view_prefills_gbp_when_no_preference_set(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('create_item'))
        self.assertEqual(response.context['form'].initial['currency'], 'GBP')


class WalletModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_wallet_name_unique_per_user(self):
        Wallet.objects.create(user=self.user, name='Travel')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.create(user=self.user, name='Travel')

    def test_same_wallet_name_allowed_for_different_users(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        Wallet.objects.create(user=self.user, name='Travel')
        Wallet.objects.create(user=bob, name='Travel')  # should not raise
        self.assertEqual(Wallet.objects.count(), 2)

    def test_deleting_wallet_unassigns_items_instead_of_deleting_them(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        item = make_item(self.user, wallet=wallet)
        wallet.delete()
        item.refresh_from_db()
        self.assertIsNone(item.wallet)
        self.assertTrue(Item.objects.filter(pk=item.pk).exists())


class TagModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_tag_name_unique_per_user(self):
        Tag.objects.create(user=self.user, name='discount')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Tag.objects.create(user=self.user, name='discount')

    def test_deleting_tag_keeps_item(self):
        tag = Tag.objects.create(user=self.user, name='discount')
        item = make_item(self.user)
        item.tags.add(tag)
        tag.delete()
        item.refresh_from_db()
        self.assertTrue(Item.objects.filter(pk=item.pk).exists())
        self.assertEqual(item.tags.count(), 0)


class ItemFormScopingTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.alice_wallet = Wallet.objects.create(user=self.alice, name='Travel')
        self.bob_wallet = Wallet.objects.create(user=self.bob, name='Groceries')
        self.alice_tag = Tag.objects.create(user=self.alice, name='discount')

    def test_wallet_field_only_offers_own_wallets(self):
        form = ItemForm(user=self.alice)
        self.assertIn(self.alice_wallet, form.fields['wallet'].queryset)
        self.assertNotIn(self.bob_wallet, form.fields['wallet'].queryset)

    def test_cannot_submit_another_users_wallet(self):
        form = ItemForm(data={
            'type': 'voucher', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'Z',
            'value': '5.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today(), 'wallet': self.bob_wallet.id,
        }, user=self.alice)
        self.assertFalse(form.is_valid())
        self.assertIn('wallet', form.errors)

    def test_new_tags_parsed_from_comma_separated_string(self):
        form = ItemForm(data={
            'type': 'voucher', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'Z',
            'value': '5.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today(), 'new_tags': ' groceries ,, discount ',
        }, user=self.alice)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['new_tags'], ['groceries', 'discount'])


class WalletViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_wallet(self):
        response = self.client.post(reverse('manage_wallets'), {
            'name': 'Travel', 'description': '', 'icon': 'bi-airplane', 'color': '#123456',
        })
        self.assertRedirects(response, reverse('manage_wallets'))
        self.assertTrue(Wallet.objects.filter(user=self.alice, name='Travel').exists())

    def test_duplicate_wallet_name_rejected(self):
        Wallet.objects.create(user=self.alice, name='Travel')
        response = self.client.post(reverse('manage_wallets'), {
            'name': 'Travel', 'description': '', 'icon': '', 'color': '#123456',
        })
        self.assertEqual(response.status_code, 200)  # re-renders form with error
        self.assertEqual(Wallet.objects.filter(user=self.alice, name='Travel').count(), 1)

    def test_cannot_edit_another_users_wallet(self):
        bob_wallet = Wallet.objects.create(user=self.bob, name='Groceries')
        response = self.client.get(reverse('edit_wallet', args=[bob_wallet.id]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_another_users_wallet(self):
        bob_wallet = Wallet.objects.create(user=self.bob, name='Groceries')
        response = self.client.post(reverse('delete_wallet', args=[bob_wallet.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Wallet.objects.filter(pk=bob_wallet.pk).exists())


class TagViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_tag(self):
        response = self.client.post(reverse('manage_tags'), {'name': 'discount', 'color': '#654321'})
        self.assertRedirects(response, reverse('manage_tags'))
        self.assertTrue(Tag.objects.filter(user=self.alice, name='discount').exists())

    def test_cannot_delete_another_users_tag(self):
        bob_tag = Tag.objects.create(user=self.bob, name='discount')
        response = self.client.post(reverse('delete_tag', args=[bob_tag.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Tag.objects.filter(pk=bob_tag.pk).exists())


class CreateItemWithOrganizationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Travel')
        self.tag = Tag.objects.create(user=self.user, name='discount')

    def test_create_item_with_wallet_tags_and_notes(self):
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Flight Voucher', 'issuer': 'Airline', 'redeem_code': 'FLY100',
            'value': '100.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
            'wallet': self.wallet.id, 'tags': [self.tag.id], 'new_tags': 'summer',
            'notes': 'Show at gate.',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='Flight Voucher')
        self.assertEqual(item.wallet, self.wallet)
        self.assertEqual(item.notes, 'Show at gate.')
        tag_names = set(item.tags.values_list('name', flat=True))
        self.assertEqual(tag_names, {'discount', 'summer'})

    def test_create_item_auto_assigns_wallet_from_issuer_match(self):
        train_wallet = Wallet.objects.create(
            user=self.user, name='Train Tickets', auto_assign_issuer_match='National Rail',
        )
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'HAP to LON', 'issuer': 'National Rail', 'redeem_code': 'RAIL1',
            'value': '0.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.wallet, train_wallet)

    def test_create_item_auto_assign_does_not_override_explicit_wallet_choice(self):
        Wallet.objects.create(user=self.user, name='Train Tickets', auto_assign_issuer_match='National Rail')
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'HAP to LON', 'issuer': 'National Rail', 'redeem_code': 'RAIL2',
            'value': '0.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(), 'wallet': self.wallet.id,
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.wallet, self.wallet)

    def test_create_item_no_matching_rule_leaves_wallet_blank(self):
        Wallet.objects.create(user=self.user, name='Train Tickets', auto_assign_issuer_match='National Rail')
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Unrelated Item', 'issuer': 'Acme', 'redeem_code': 'ACME1',
            'value': '0.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='Unrelated Item')
        self.assertIsNone(item.wallet)


class WalletAutoAssignMatchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_match_for_issuer_is_case_insensitive_substring(self):
        wallet = Wallet.objects.create(user=self.user, name='Train Tickets', auto_assign_issuer_match='national rail')
        self.assertEqual(Wallet.match_for_issuer(self.user, 'National Rail'), wallet)

    def test_match_for_issuer_returns_none_without_match(self):
        Wallet.objects.create(user=self.user, name='Train Tickets', auto_assign_issuer_match='National Rail')
        self.assertIsNone(Wallet.match_for_issuer(self.user, 'Acme'))

    def test_match_for_issuer_returns_none_for_wallets_without_a_rule(self):
        Wallet.objects.create(user=self.user, name='Groceries')
        self.assertIsNone(Wallet.match_for_issuer(self.user, 'Anything'))

    def test_match_for_issuer_ignores_other_users_wallets(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        Wallet.objects.create(user=bob, name='Train Tickets', auto_assign_issuer_match='National Rail')
        self.assertIsNone(Wallet.match_for_issuer(self.user, 'National Rail'))

    def test_match_for_issuer_returns_none_for_blank_issuer(self):
        Wallet.objects.create(user=self.user, name='Train Tickets', auto_assign_issuer_match='National Rail')
        self.assertIsNone(Wallet.match_for_issuer(self.user, ''))


class TravelPassTypeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def _post_travelpass(self, **overrides):
        data = {
            'type': 'travelpass', 'name': 'HAP to LON', 'issuer': 'National Rail',
            'redeem_code': 'RAIL1', 'code_type': 'qrcode', 'value_type': 'money',
            'currency': 'GBP', 'expiry_date': date.today().isoformat(),
            'journey_origin': 'Hatfield Peverel', 'journey_destination': 'London Terminals',
        }
        data.update(overrides)
        return self.client.post(reverse('create_item'), data)

    def test_create_item_travelpass_auto_assigns_travel_pass_wallet(self):
        response = self._post_travelpass()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.wallet.name, 'Travel Pass')
        self.assertEqual(item.wallet.user, self.user)

    def test_create_item_travelpass_overrides_explicit_wallet_choice(self):
        other_wallet = Wallet.objects.create(user=self.user, name='Everything Else')
        response = self._post_travelpass(wallet=other_wallet.id)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.wallet.name, 'Travel Pass')

    def test_create_item_travelpass_reuses_existing_travel_pass_wallet(self):
        self._post_travelpass(name='Outward')
        self._post_travelpass(name='Return', redeem_code='RAIL2')
        wallets = Wallet.objects.filter(user=self.user, name='Travel Pass')
        self.assertEqual(wallets.count(), 1)
        self.assertEqual(
            Item.objects.get(name='Outward').wallet_id,
            Item.objects.get(name='Return').wallet_id,
        )

    def test_create_item_travelpass_issue_date_defaults_to_expiry_when_blank(self):
        response = self._post_travelpass(expiry_date='2027-03-15')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.issue_date.isoformat(), '2027-03-15')
        self.assertEqual(item.expiry_date.isoformat(), '2027-03-15')

    def test_create_item_travelpass_respects_explicit_issue_date(self):
        response = self._post_travelpass(issue_date='2027-01-01', expiry_date='2027-03-15')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.issue_date.isoformat(), '2027-01-01')

    def test_create_item_travelpass_value_defaults_to_zero(self):
        response = self._post_travelpass()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.value, 0)

    def test_create_item_travelpass_stores_travel_time(self):
        response = self._post_travelpass(travel_time='09:14')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='HAP to LON')
        self.assertEqual(item.travel_time, time(9, 14))

    def test_get_or_create_travel_pass_wallet_is_idempotent(self):
        first = Wallet.get_or_create_travel_pass_wallet(self.user)
        second = Wallet.get_or_create_travel_pass_wallet(self.user)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Wallet.objects.filter(user=self.user, name='Travel Pass').count(), 1)

    def test_item_save_forces_travel_pass_wallet_regardless_of_caller(self):
        other_wallet = Wallet.objects.create(user=self.user, name='Somewhere Else')
        item = Item.objects.create(
            type='travelpass', name='Direct Save', issuer='National Rail', redeem_code='RAIL3',
            expiry_date=date.today(), value=Decimal('0.00'), currency='GBP', user=self.user,
            wallet=other_wallet,
        )
        item.refresh_from_db()
        self.assertEqual(item.wallet.name, 'Travel Pass')


class SuggestFieldOptionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_ranks_by_frequency_then_recency(self):
        # Two older "National Rail" items vs one newer "Greater Anglia"
        # one-off: the habit (higher frequency) outranks the more recent
        # one-off, and both still show up since the list holds up to 5.
        for index, name in enumerate(['A', 'B']):
            item = Item.objects.create(
                type='giftcard', name=name, issuer='National Rail', redeem_code=f'NR{index}',
                expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
            )
            Item.objects.filter(pk=item.pk).update(created_at=timezone.now() - timedelta(days=2 - index))
        Item.objects.create(
            type='giftcard', name='One-off', issuer='Greater Anglia', redeem_code='GA1',
            expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
        )

        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'issuer'})
        self.assertEqual(response.status_code, 200)
        options = response.json()['options']
        self.assertEqual([opt['value'] for opt in options], ['National Rail', 'Greater Anglia'])

    def test_caps_at_five_options(self):
        for i in range(7):
            Item.objects.create(
                type='giftcard', name=f'Item {i}', issuer=f'Issuer {i}', redeem_code=f'C{i}',
                expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
            )

        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'issuer'})
        self.assertEqual(len(response.json()['options']), 5)

    def test_wallet_field_returns_id_as_value_and_name_as_label(self):
        wallet = Wallet.objects.create(user=self.user, name='Groceries')
        Item.objects.create(
            type='giftcard', name='With Wallet', issuer='Some Issuer', redeem_code='W1',
            expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
            wallet=wallet,
        )

        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'wallet'})
        options = response.json()['options']
        self.assertEqual(options, [{'value': str(wallet.id), 'label': 'Groceries'}])

    def test_rejects_field_not_in_the_allowlist(self):
        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'notes'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'options': []})

    def test_returns_empty_when_no_items_of_that_type_exist(self):
        response = self.client.get(reverse('suggest_field_options'), {'type': 'coupon', 'field': 'issuer'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'options': []})

    def test_does_not_leak_another_users_items(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        Item.objects.create(
            type='giftcard', name='Bobs Card', issuer='Bob Issuer', redeem_code='BOB1',
            expiry_date=date.today(), value=Decimal('5.00'), currency='GBP', user=bob,
        )

        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'issuer'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'options': []})

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'issuer'})
        self.assertNotEqual(response.status_code, 200)

    def test_currency_and_code_type_in_allowlist(self):
        for field in ('currency', 'code_type'):
            Item.objects.create(
                type='giftcard', name=f'Item {field}', issuer='Tesco',
                redeem_code=f'CODE{field}', expiry_date=date.today(),
                value=Decimal('1.00'), currency='GBP', code_type='qrcode', user=self.user,
            )
            response = self.client.get(
                reverse('suggest_field_options'), {'type': 'giftcard', 'field': field},
            )
            self.assertEqual(response.status_code, 200)
            options = response.json()['options']
            self.assertGreater(len(options), 0)

    def test_context_boosts_matching_issuer(self):
        # Two items with different issuers; context_issuer matches one.
        import json as _json
        Item.objects.create(
            type='giftcard', name='Match', issuer='Tesco', redeem_code='T1',
            expiry_date=date.today(), value=Decimal('1.00'), currency='GBP',
            logo_slug='tesco', user=self.user,
        )
        Item.objects.create(
            type='giftcard', name='Other', issuer='Amazon', redeem_code='A1',
            expiry_date=date.today(), value=Decimal('1.00'), currency='GBP',
            logo_slug='amazon', user=self.user,
        )
        ctx = _json.dumps({'issuer': 'Tesco'})
        response = self.client.get(
            reverse('suggest_field_options'),
            {'type': 'giftcard', 'field': 'logo_slug', 'context': ctx},
        )
        options = response.json()['options']
        # The Tesco logo slug should rank first because the context issuer matches.
        self.assertEqual(options[0]['value'], 'tesco')

    def test_cross_type_fallback_when_type_pool_is_thin(self):
        # Only 2 distinct issuer values for giftcard; fallback from other types fills the gap.
        Item.objects.create(
            type='giftcard', name='GC1', issuer='Issuer A', redeem_code='GCA',
            expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
        )
        for i in range(3):
            Item.objects.create(
                type='coupon', name=f'CP{i}', issuer=f'Coupon Issuer {i}', redeem_code=f'CP{i}',
                expiry_date=date.today(), value=Decimal('1.00'), currency='GBP', user=self.user,
            )
        response = self.client.get(
            reverse('suggest_field_options'), {'type': 'giftcard', 'field': 'issuer'},
        )
        options = response.json()['options']
        # Should include both the type-specific result and fallback results.
        self.assertGreater(len(options), 1)

    def _post_item(self, extra):
        data = {
            'type': 'giftcard', 'name': 'Test Card', 'issuer': 'Issuer',
            'redeem_code': 'CODE99', 'value': '10.00', 'currency': 'GBP',
            'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        }
        data.update(extra)
        return self.client.post(reverse('create_item'), data)

    def test_suggestion_feedback_recorded_on_item_save(self):
        # User accepts "Amazon" suggestion then changes issuer to "Tesco" before saving.
        from myapp.models import ScanFieldCorrection
        response = self._post_item({
            'issuer': 'Tesco', '_sg_suggested_issuer': 'Amazon', 'redeem_code': 'FEEDBACK1',
        })
        self.assertEqual(response.status_code, 302)
        correction = ScanFieldCorrection.objects.filter(
            user=self.user, field='issuer', ai_value='Amazon', corrected_value='Tesco',
        ).first()
        self.assertIsNotNone(correction)

    def test_suggestion_accepted_and_kept_clears_stale_correction(self):
        # Old correction maps "Tesco" away; user keeps the Tesco suggestion → retire it.
        from myapp.models import ScanFieldCorrection
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='issuer',
            ai_value='Tesco', corrected_value='Sainsburys',
        )
        response = self._post_item({
            'issuer': 'Tesco', '_sg_suggested_issuer': 'Tesco', 'redeem_code': 'KEPT1',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            ScanFieldCorrection.objects.filter(user=self.user, field='issuer', ai_value='Tesco').exists()
        )


class AnimationAssetsTests(TestCase):
    """
    The site-wide animation layer (Phase 83) is progressive enhancement:
    these tests guard the wiring, since a 404'd vendor file would
    silently disable every entrance animation without breaking anything
    else.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_base_template_references_animation_assets(self):
        response = self.client.get(reverse('show_items'))
        content = response.content.decode()
        self.assertIn('assets/css/animations.css', content)
        self.assertIn('assets/vendor/motion/motion.min.js', content)
        self.assertIn('assets/js/animations.js', content)

    def test_animation_static_files_exist(self):
        from django.contrib.staticfiles import finders
        for path in (
            'assets/css/animations.css',
            'assets/js/animations.js',
            'assets/vendor/motion/motion.min.js',
        ):
            self.assertIsNotNone(finders.find(path), f'{path} missing from static files')

    def test_serviceworker_precaches_animation_assets(self):
        import pathlib
        sw = pathlib.Path('myapp/serviceworker.js').read_text()
        self.assertIn('/static/assets/css/animations.css', sw)
        self.assertIn('/static/assets/js/animations.js', sw)
        self.assertIn('/static/assets/vendor/motion/motion.min.js', sw)


class ScanLearningTests(TestCase):
    """
    myapp/scan_learning.py - the self-healing loop between an AI scan's
    raw extraction (snapshot) and what the user actually saved.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _item(self, **overrides):
        defaults = dict(
            type='travelpass', name='HAP to LON', issuer='National Rail',
            redeem_code='RAIL1', expiry_date=date.today(), value=Decimal('0.00'),
            currency='GBP', user=self.user,
        )
        defaults.update(overrides)
        return Item.objects.create(**defaults)

    def test_correction_recorded_when_user_changes_a_scanned_value(self):
        item = self._item(issuer='National Rail')
        record_scan_corrections(self.user, {'issuer': 'Nationl Rail'}, item)
        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer')
        self.assertEqual(correction.ai_value, 'Nationl Rail')
        self.assertEqual(correction.corrected_value, 'National Rail')
        self.assertEqual(correction.times_seen, 1)

    def test_repeat_of_same_correction_increments_times_seen(self):
        item = self._item()
        record_scan_corrections(self.user, {'issuer': 'Nationl Rail'}, item)
        record_scan_corrections(self.user, {'issuer': 'Nationl Rail'}, item)
        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer')
        self.assertEqual(correction.times_seen, 2)

    def test_keeping_the_scanned_value_retires_a_stale_correction(self):
        item = self._item(issuer='Nationl Rail')
        # Previously corrected "Nationl Rail" -> "National Rail"...
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        # ...but this time the user saved the scanned value untouched.
        record_scan_corrections(self.user, {'issuer': 'Nationl Rail'}, item)
        self.assertFalse(ScanFieldCorrection.objects.filter(user=self.user, field='issuer').exists())

    def test_unchanged_matching_values_record_nothing(self):
        item = self._item(issuer='National Rail')
        record_scan_corrections(self.user, {'issuer': 'National Rail'}, item)
        self.assertFalse(ScanFieldCorrection.objects.exists())

    def test_apply_heals_a_previously_corrected_value(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        result = {'issuer': 'Nationl Rail', 'type': 'travelpass'}
        healed = apply_learned_corrections(self.user, result)
        self.assertEqual(result['issuer'], 'National Rail')
        self.assertEqual(healed, ['issuer'])

    def test_apply_matches_ai_value_case_insensitively(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='nationl rail', corrected_value='National Rail',
        )
        result = {'issuer': 'NATIONL RAIL', 'type': 'travelpass'}
        self.assertEqual(apply_learned_corrections(self.user, result), ['issuer'])
        self.assertEqual(result['issuer'], 'National Rail')

    def test_blank_fill_needs_two_sightings_before_it_replays(self):
        item = self._item(issuer='National Rail')
        record_scan_corrections(self.user, {'issuer': None}, item)

        result = {'issuer': None, 'type': 'travelpass'}
        self.assertEqual(apply_learned_corrections(self.user, result), [])
        self.assertIsNone(result['issuer'])

        record_scan_corrections(self.user, {'issuer': None}, item)
        result = {'issuer': None, 'type': 'travelpass'}
        self.assertEqual(apply_learned_corrections(self.user, result), ['issuer'])
        self.assertEqual(result['issuer'], 'National Rail')

    def test_blank_fill_requires_matching_item_type(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='', corrected_value='National Rail', times_seen=5,
        )
        result = {'issuer': None, 'type': 'giftcard'}
        self.assertEqual(apply_learned_corrections(self.user, result), [])
        self.assertIsNone(result['issuer'])

    def test_apply_rejects_invalid_choice_values(self):
        # A correction whose stored value is no longer a legal choice for
        # the field (here: a type that doesn't exist) must never replay.
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='type',
            ai_value='giftcard', corrected_value='not-a-real-type',
        )
        result = {'type': 'giftcard'}
        self.assertEqual(apply_learned_corrections(self.user, result), [])
        self.assertEqual(result['type'], 'giftcard')

    def test_apply_heals_type_first_so_blank_fills_use_corrected_type(self):
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='giftcard', field='type',
            ai_value='giftcard', corrected_value='travelpass',
        )
        ScanFieldCorrection.objects.create(
            user=self.user, item_type='travelpass', field='issuer',
            ai_value='', corrected_value='National Rail', times_seen=2,
        )
        result = {'type': 'giftcard', 'issuer': None}
        healed = apply_learned_corrections(self.user, result)
        self.assertEqual(result['type'], 'travelpass')
        self.assertEqual(result['issuer'], 'National Rail')
        self.assertEqual(set(healed), {'type', 'issuer'})

    def test_corrections_are_scoped_per_user(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        ScanFieldCorrection.objects.create(
            user=bob, item_type='travelpass', field='issuer',
            ai_value='Nationl Rail', corrected_value='National Rail',
        )
        result = {'issuer': 'Nationl Rail', 'type': 'travelpass'}
        self.assertEqual(apply_learned_corrections(self.user, result), [])
        self.assertEqual(result['issuer'], 'Nationl Rail')

    def test_travel_time_correction_round_trip(self):
        item = self._item(travel_time=time(9, 14))
        record_scan_corrections(self.user, {'travel_time': '09:41'}, item)
        result = {'travel_time': '09:41', 'type': 'travelpass'}
        self.assertEqual(apply_learned_corrections(self.user, result), ['travel_time'])
        self.assertEqual(result['travel_time'], '09:14')

    def test_new_correction_for_same_ai_value_replaces_old_and_resets_count(self):
        item = self._item(issuer='National Rail')
        record_scan_corrections(self.user, {'issuer': 'Ntnl Rail'}, item)
        record_scan_corrections(self.user, {'issuer': 'Ntnl Rail'}, item)
        item.issuer = 'Greater Anglia'
        item.save()
        record_scan_corrections(self.user, {'issuer': 'Ntnl Rail'}, item)
        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer')
        self.assertEqual(correction.corrected_value, 'Greater Anglia')
        self.assertEqual(correction.times_seen, 1)

    def test_create_item_view_records_snapshot_corrections(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('create_item'), {
            'type': 'travelpass', 'name': 'HAP to LON', 'issuer': 'National Rail',
            'redeem_code': 'RAIL9', 'code_type': 'qrcode', 'value_type': 'money',
            'currency': 'GBP', 'expiry_date': date.today().isoformat(),
            'journey_origin': 'Hatfield Peverel', 'journey_destination': 'London Terminals',
            'ai_scan_snapshot': json.dumps({'issuer': 'Nationl Rail', 'type': 'travelpass'}),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        correction = ScanFieldCorrection.objects.get(user=self.user, field='issuer')
        self.assertEqual(correction.corrected_value, 'National Rail')

    def test_create_item_view_tolerates_garbled_snapshot(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Fine', 'issuer': 'Acme', 'redeem_code': 'OK1',
            'value': '5.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
            'ai_scan_snapshot': 'not-json-at-all',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        self.assertFalse(ScanFieldCorrection.objects.exists())


class NoBarcodeCodeTypeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_item_with_no_barcode_stores_no_image(self):
        response = self.client.post(reverse('create_item'), {
            'type': 'giftcard', 'name': 'Numbers Only Card', 'issuer': 'Shop', 'redeem_code': '4111222233334444',
            'value': '25.00', 'currency': 'GBP', 'code_type': 'none', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        item = Item.objects.get(name='Numbers Only Card')
        self.assertEqual(item.code_type, 'none')
        self.assertFalse(item.qr_code_base64)

    def test_edit_item_switch_to_no_barcode_clears_image(self):
        item = make_item(self.user, redeem_code='ABC123', code_type='qrcode', qr_code_base64='dummybase64')
        response = self.client.post(reverse('edit_item', args=[item.id]), {
            'type': item.type, 'name': item.name, 'issuer': item.issuer, 'redeem_code': item.redeem_code,
            'value': item.value, 'currency': item.currency, 'code_type': 'none', 'value_type': item.value_type,
            'issue_date': date.today().isoformat(), 'expiry_date': item.expiry_date.isoformat(),
        })
        self.assertRedirects(response, reverse('view_item', args=[item.id]))
        item.refresh_from_db()
        self.assertEqual(item.code_type, 'none')
        self.assertFalse(item.qr_code_base64)

    def test_view_item_no_barcode_omits_qr_image(self):
        item = make_item(self.user, redeem_code='4111222233334444', code_type='none')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="qr-code"')
        self.assertNotContains(response, 'id="fullscreen-btn"')
        self.assertContains(response, 'id="redeem-code"')
        self.assertContains(response, '4111222233334444')


class TileColorPreservationTests(TestCase):
    """
    tile_color has been the root cause of three separate historical bugs
    (upstream #86, #107, #126) - lost on edit, lost on duplicate. No
    regression test existed for either despite the field being touched
    repeatedly. clean_tile_color() treats the UI's default placeholder
    swatches as "unset" (returns None) - '#ff5733' here is deliberately
    not one of those placeholders, so it must round-trip unchanged.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_tile_color_survives_editing_an_unrelated_field(self):
        item = make_item(self.user, tile_color='#ff5733')
        response = self.client.post(reverse('edit_item', args=[item.id]), {
            'type': item.type, 'name': 'Renamed Voucher', 'issuer': item.issuer,
            'redeem_code': item.redeem_code, 'value': item.value, 'currency': item.currency,
            'code_type': item.code_type, 'value_type': item.value_type,
            'issue_date': date.today().isoformat(), 'expiry_date': item.expiry_date.isoformat(),
            'tile_color': '#ff5733',
        })
        self.assertRedirects(response, reverse('view_item', args=[item.id]))
        item.refresh_from_db()
        self.assertEqual(item.name, 'Renamed Voucher')
        self.assertEqual(item.tile_color, '#ff5733')

    def test_tile_color_preserved_in_duplicate_form(self):
        item = make_item(self.user, tile_color='#ff5733')
        response = self.client.get(reverse('duplicate_item', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="#ff5733"')


class ExtraBarcodeTypeTests(TestCase):
    """
    codabar/code93 were previously mis-detected by the camera scanner as
    code39, and isbn13/isbn10 were both completely broken (treepoem has no
    'isbn13'/'isbn10' barcode type - selecting either always raised
    NotImplementedError). This covers the fix: codabar/code93 render as
    their own real symbologies, and isbn13 renders as the EAN-13 it
    actually is on a physical product barcode.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_codabar_renders_without_error(self):
        item = make_item(self.user, redeem_code='A123456A', code_type='codabar')
        image_b64, resolved_type = generate_code_image_base64(item)
        self.assertIsNotNone(image_b64)
        self.assertEqual(resolved_type, 'codabar')

    def test_code93_renders_without_error(self):
        item = make_item(self.user, redeem_code='ABC-123', code_type='code93')
        image_b64, resolved_type = generate_code_image_base64(item)
        self.assertIsNotNone(image_b64)
        self.assertEqual(resolved_type, 'code93')

    def test_isbn13_renders_as_ean13(self):
        item = make_item(self.user, redeem_code='9780134685991', code_type='isbn13')
        image_b64, resolved_type = generate_code_image_base64(item)
        self.assertIsNotNone(image_b64)
        self.assertEqual(resolved_type, 'ean13')


class IssuerAutocompleteTests(TestCase):
    """
    Manually typing an issuer name is a common source of typos ("Amazom"
    vs "Amazon") that silently split what should be one merchant across
    two spellings, breaking logo matching, the balance-check URL
    suggestion, and analytics grouping. A <datalist> of the user's own
    past issuer names fixes that without forcing a fixed merchant list.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_item_page_lists_own_past_issuers(self):
        make_item(self.user, redeem_code='A1', issuer='Amazon')
        make_item(self.user, redeem_code='A2', issuer='Tesco')
        make_item(self.other_user, redeem_code='A3', issuer='SomeoneElsesShop')

        response = self.client.get(reverse('create_item'))
        self.assertContains(response, '<option value="Amazon">')
        self.assertContains(response, '<option value="Tesco">')
        self.assertNotContains(response, 'SomeoneElsesShop')

    def test_create_item_page_deduplicates_issuers(self):
        make_item(self.user, redeem_code='A1', issuer='Amazon')
        make_item(self.user, redeem_code='A2', issuer='Amazon')

        response = self.client.get(reverse('create_item'))
        self.assertEqual(response.content.decode().count('<option value="Amazon">'), 1)

    def test_edit_item_page_lists_own_past_issuers(self):
        item = make_item(self.user, redeem_code='A1', issuer='Amazon')
        make_item(self.user, redeem_code='A2', issuer='Tesco')

        response = self.client.get(reverse('edit_item', args=[item.id]))
        self.assertContains(response, '<option value="Tesco">')


class ShowItemsWalletFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Travel')
        self.in_wallet = make_item(self.user, name='In Wallet', wallet=self.wallet)
        self.no_wallet = make_item(self.user, name='No Wallet', redeem_code='OTHER')

    def test_filter_by_wallet(self):
        response = self.client.get(reverse('show_items'), {'wallet': self.wallet.id, 'status': 'all'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertEqual(names, ['In Wallet'])

    def test_hidden_type_field_renders_empty_not_literal_none(self):
        """
        Regression test: the "All Wallets" <select>'s onchange does a native
        form submit of #filterForm, which includes a hidden `type` input
        whose value comes from {{ item_type }} - preserving the current type
        filter across a wallet-only change. When no type filter was active,
        item_type is Python None, and an unguarded {{ item_type }} rendered
        the literal string "None" into the hidden input's value attribute.
        The browser then submitted type=None as a real query param, and
        `if item_type:` in show_items() treated that non-empty string as a
        genuine (nonexistent) type filter, silently zeroing every result -
        this is exactly what a user hit switching a wallet filter back to
        "All Wallets": the wallet filter reset correctly, but the item list
        stayed stuck at 0 because of this stowaway `type=None`.
        """
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'name="type" id="hiddenType" value=""')
        self.assertNotContains(response, 'value="None"')

    def test_switching_wallet_filter_back_to_all_shows_every_item_again(self):
        """Behavioral companion to the test above: confirms show_items()
        itself correctly returns every item once the hidden `type` input
        actually renders empty (type='') rather than the literal "None"
        string, i.e. the query shape the fixed template now produces."""
        self.client.get(reverse('show_items'), {'wallet': self.wallet.id})

        response = self.client.get(reverse('show_items'), {'wallet': '', 'type': '', 'status': 'available'})
        names = {entry['item'].name for entry in response.context['items_with_qr']}
        self.assertEqual(names, {'In Wallet', 'No Wallet'})


class ShowItemsTagFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.groceries = Tag.objects.create(user=self.user, name='Groceries')
        self.travel = Tag.objects.create(user=self.user, name='Travel')
        self.tagged_groceries = make_item(self.user, name='Tagged Groceries')
        self.tagged_groceries.tags.add(self.groceries)
        self.tagged_travel = make_item(self.user, name='Tagged Travel', redeem_code='OTHER1')
        self.tagged_travel.tags.add(self.travel)
        self.tagged_both = make_item(self.user, name='Tagged Both', redeem_code='OTHER2')
        self.tagged_both.tags.add(self.groceries, self.travel)
        self.untagged = make_item(self.user, name='Untagged', redeem_code='OTHER3')

    def test_filter_by_single_tag(self):
        response = self.client.get(reverse('show_items'), {'tag': self.groceries.id, 'status': 'all'})
        names = {entry['item'].name for entry in response.context['items_with_qr']}
        self.assertEqual(names, {'Tagged Groceries', 'Tagged Both'})

    def test_filter_by_multiple_tags_is_or(self):
        response = self.client.get(reverse('show_items'), {'tag': [self.groceries.id, self.travel.id], 'status': 'all'})
        names = {entry['item'].name for entry in response.context['items_with_qr']}
        self.assertEqual(names, {'Tagged Groceries', 'Tagged Travel', 'Tagged Both'})

    def test_no_tag_filter_shows_everything(self):
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        names = {entry['item'].name for entry in response.context['items_with_qr']}
        self.assertIn('Untagged', names)

    def test_all_tags_context_includes_item_counts(self):
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        tags_by_name = {tag.name: tag for tag in response.context['all_tags']}
        self.assertEqual(tags_by_name['Groceries'].item_count, 2)
        self.assertEqual(tags_by_name['Travel'].item_count, 2)

    def test_selected_tag_ids_reflected_in_context(self):
        response = self.client.get(reverse('show_items'), {'tag': self.groceries.id, 'status': 'all'})
        self.assertEqual(response.context['selected_tag_ids'], [self.groceries.id])

    def test_other_users_tags_not_shown(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        Tag.objects.create(user=bob, name='Bob Tag')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        tag_names = {tag.name for tag in response.context['all_tags']}
        self.assertNotIn('Bob Tag', tag_names)


class ShowItemsNextUpWidgetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Train Tickets')
        self.other_wallet = Wallet.objects.create(user=self.user, name='Flights')

    def test_no_widget_when_no_wallets_configured(self):
        make_item(self.user, wallet=self.wallet, expiry_date=date.today() + timedelta(days=1))
        response = self.client.get(reverse('show_items'))
        self.assertEqual(response.context['next_up_items'], [])

    def test_widget_shows_soonest_item_from_configured_wallet(self):
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)
        soonest = make_item(self.user, name='Soonest', redeem_code='S1', wallet=self.wallet,
                             expiry_date=date.today() + timedelta(days=1))
        make_item(self.user, name='Later', redeem_code='L1', wallet=self.wallet,
                  expiry_date=date.today() + timedelta(days=5))

        response = self.client.get(reverse('show_items'))
        items = [e['item'] for e in response.context['next_up_items']]
        self.assertEqual([i.id for i in items], [soonest.id])

    def test_widget_interleaves_multiple_wallets_by_date(self):
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet, self.other_wallet)
        preferences.next_up_max_items = 3
        preferences.save()
        flight = make_item(self.user, name='Flight', redeem_code='F1', wallet=self.other_wallet,
                            expiry_date=date.today() + timedelta(days=1))
        train = make_item(self.user, name='Train', redeem_code='T1', wallet=self.wallet,
                           expiry_date=date.today() + timedelta(days=2))

        response = self.client.get(reverse('show_items'))
        items = [e['item'] for e in response.context['next_up_items']]
        self.assertEqual([i.id for i in items], [flight.id, train.id])

    def test_widget_capped_at_configured_max_items(self):
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)
        preferences.next_up_max_items = 2
        preferences.save()
        for i in range(4):
            make_item(self.user, name=f'Item{i}', redeem_code=f'C{i}', wallet=self.wallet,
                      expiry_date=date.today() + timedelta(days=i + 1))

        response = self.client.get(reverse('show_items'))
        self.assertEqual(len(response.context['next_up_items']), 2)

    def test_other_users_wallet_not_leaked_as_next_up(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        bob_wallet = Wallet.objects.create(user=bob, name='Bob Wallet')
        make_item(bob, wallet=bob_wallet, expiry_date=date.today() + timedelta(days=1))

        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)

        response = self.client.get(reverse('show_items'))
        self.assertEqual(response.context['next_up_items'], [])

    def test_preference_form_scopes_wallet_choices_to_owner(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        Wallet.objects.create(user=bob, name='Bob Wallet')
        response = self.client.get(reverse('update_user_preferences'))
        wallet_names = {w.name for w in response.context['form'].fields['next_up_wallets'].queryset}
        self.assertEqual(wallet_names, {'Train Tickets', 'Flights'})

    def test_mark_used_from_widget_redirects_to_next_param(self):
        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today() + timedelta(days=1))
        response = self.client.post(
            reverse('toggle_item_status', args=[item.id]),
            {'next': reverse('show_items')},
        )
        self.assertRedirects(response, reverse('show_items'))
        item.refresh_from_db()
        self.assertTrue(item.is_used)


class ActiveTodayWidgetTests(TestCase):
    """
    myapp.analytics.get_active_today_item() - the daily-commute "Active
    Today" widget. home="Hatfield Peverel" throughout, mirroring the real
    use case: a round-trip rail ticket pair valid for exactly today, with
    reciprocal journey_origin/journey_destination.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.home = 'Hatfield Peverel'

    def _outward(self, **kwargs):
        defaults = {
            'name': 'HAP to LON', 'redeem_code': 'OUT1',
            'journey_origin': 'Hatfield Peverel', 'journey_destination': 'London Terminals',
            'expiry_date': date.today(),
        }
        defaults.update(kwargs)
        return make_item(self.user, **defaults)

    def _return_leg(self, **kwargs):
        defaults = {
            'name': 'LON to HAP', 'redeem_code': 'RET1',
            'journey_origin': 'London Terminals', 'journey_destination': 'Hatfield Peverel',
            'expiry_date': date.today(),
        }
        defaults.update(kwargs)
        return make_item(self.user, **defaults)

    def test_none_when_disabled(self):
        from .analytics import get_active_today_item
        self._outward()
        self.assertIsNone(get_active_today_item(self.user, False, self.home, time(12, 0)))

    def test_none_when_no_home_station_configured(self):
        from .analytics import get_active_today_item
        self._outward()
        self.assertIsNone(get_active_today_item(self.user, True, '', time(12, 0)))

    def test_none_when_no_ticket_valid_today(self):
        from .analytics import get_active_today_item
        self._outward(expiry_date=date.today() + timedelta(days=1))
        self.assertIsNone(get_active_today_item(self.user, True, self.home, time(12, 0)))

    def test_ignores_items_without_both_journey_fields(self):
        from .analytics import get_active_today_item
        make_item(self.user, name='Not A Ticket', redeem_code='GC1', expiry_date=date.today())
        self.assertIsNone(get_active_today_item(self.user, True, self.home, time(12, 0)))

    def test_pre_cutoff_shows_outward_leg(self):
        from .analytics import get_active_today_item
        outward = self._outward()
        self._return_leg()
        result = get_active_today_item(self.user, True, self.home, time(23, 59, 59))
        self.assertEqual(result.id, outward.id)

    def test_post_cutoff_shows_return_leg(self):
        from .analytics import get_active_today_item
        self._outward()
        return_leg = self._return_leg()
        result = get_active_today_item(self.user, True, self.home, time(0, 0))
        self.assertEqual(result.id, return_leg.id)

    def test_pre_cutoff_falls_back_to_return_leg_when_only_return_bought(self):
        from .analytics import get_active_today_item
        return_leg = self._return_leg()
        result = get_active_today_item(self.user, True, self.home, time(23, 59, 59))
        self.assertEqual(result.id, return_leg.id)

    def test_post_cutoff_with_no_return_leg_shows_nothing(self):
        from .analytics import get_active_today_item
        self._outward()
        result = get_active_today_item(self.user, True, self.home, time(0, 0))
        self.assertIsNone(result)

    def test_used_outward_leg_is_never_shown_even_pre_cutoff(self):
        from .analytics import get_active_today_item
        self._outward(is_used=True)
        return_leg = self._return_leg()
        result = get_active_today_item(self.user, True, self.home, time(23, 59, 59))
        self.assertEqual(result.id, return_leg.id)

    def test_archived_ticket_is_never_shown(self):
        from .analytics import get_active_today_item
        self._outward(is_archived=True)
        self.assertIsNone(get_active_today_item(self.user, True, self.home, time(23, 59, 59)))

    def test_home_station_match_is_case_insensitive(self):
        from .analytics import get_active_today_item
        outward = self._outward()
        result = get_active_today_item(self.user, True, 'HATFIELD PEVEREL', time(23, 59, 59))
        self.assertEqual(result.id, outward.id)

    def test_show_items_view_wires_widget_into_context(self):
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.active_today_enabled = True
        preferences.commute_home_station = self.home
        preferences.active_today_cutoff_time = time(23, 59, 59)
        preferences.save()
        outward = self._outward()

        response = self.client.get(reverse('show_items'))
        self.assertEqual(response.context['active_today_item']['item'].id, outward.id)

    def test_mark_expired_commute_outward_tickets_task_flips_outward_used(self):
        from .tasks import mark_expired_commute_outward_tickets

        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.active_today_enabled = True
        preferences.commute_home_station = self.home
        preferences.active_today_cutoff_time = time(0, 0)
        preferences.save()
        outward = self._outward()
        return_leg = self._return_leg()

        mark_expired_commute_outward_tickets()

        outward.refresh_from_db()
        return_leg.refresh_from_db()
        self.assertTrue(outward.is_used)
        self.assertFalse(return_leg.is_used)

    def test_mark_expired_commute_outward_tickets_task_no_op_before_cutoff(self):
        from .tasks import mark_expired_commute_outward_tickets

        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.active_today_enabled = True
        preferences.commute_home_station = self.home
        preferences.active_today_cutoff_time = time(23, 59, 59)
        preferences.save()
        outward = self._outward()

        mark_expired_commute_outward_tickets()

        outward.refresh_from_db()
        self.assertFalse(outward.is_used)


class AnalyticsHelperTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Travel', color='#4154f1')

    def test_get_items_by_wallet_groups_and_labels_no_wallet(self):
        from .analytics import get_items_by_wallet

        make_item(self.user, name='In Wallet', wallet=self.wallet)
        make_item(self.user, name='No Wallet Item', redeem_code='NW1')

        breakdown = {row['name']: row for row in get_items_by_wallet(self.user)}
        self.assertEqual(breakdown['Travel']['count'], 1)
        self.assertEqual(breakdown['Travel']['color'], '#4154f1')
        self.assertEqual(breakdown['No Wallet']['count'], 1)

    def test_get_items_by_wallet_folds_extras_into_other(self):
        from .analytics import get_items_by_wallet

        for i in range(10):
            w = Wallet.objects.create(user=self.user, name=f'Wallet{i}')
            make_item(self.user, name=f'Item{i}', redeem_code=f'C{i}', wallet=w)

        breakdown = get_items_by_wallet(self.user, limit=8)
        self.assertEqual(len(breakdown), 9)  # 8 real wallets + 1 "Other"
        other = next(row for row in breakdown if row['name'] == 'Other')
        self.assertEqual(other['count'], 2)

    def test_get_items_by_wallet_excludes_used_items(self):
        from .analytics import get_items_by_wallet

        make_item(self.user, wallet=self.wallet, is_used=True)
        self.assertEqual(get_items_by_wallet(self.user), [])

    def test_get_expiring_soon_items_respects_window_and_attaches_days_left(self):
        from .analytics import get_expiring_soon_items

        within = make_item(self.user, name='Within', redeem_code='W1', expiry_date=date.today() + timedelta(days=3))
        make_item(self.user, name='Outside', redeem_code='O1', expiry_date=date.today() + timedelta(days=30))
        make_item(self.user, name='AlreadyExpired', redeem_code='E1', expiry_date=date.today() - timedelta(days=1))

        results = get_expiring_soon_items(self.user, days=7)
        self.assertEqual([i.name for i in results], ['Within'])
        self.assertEqual(within.id, results[0].id)
        self.assertEqual(results[0].days_left, 3)

    def test_get_expiring_soon_items_excludes_used(self):
        from .analytics import get_expiring_soon_items

        make_item(self.user, is_used=True, expiry_date=date.today() + timedelta(days=1))
        self.assertEqual(get_expiring_soon_items(self.user), [])

    def test_get_expiring_soon_items_default_follows_site_configured_threshold(self):
        from .analytics import get_expiring_soon_items

        set_site_config(expiry_threshold_days=5)
        within = make_item(self.user, name='Within', redeem_code='W1', expiry_date=date.today() + timedelta(days=3))
        outside = make_item(self.user, name='Outside', redeem_code='O1', expiry_date=date.today() + timedelta(days=10))

        results = get_expiring_soon_items(self.user)
        self.assertEqual([i.id for i in results], [within.id])

        # Raising the threshold should surface the previously-excluded item too,
        # without passing an explicit `days=` - this is what keeps the Dashboard's
        # "Expiring Soon" list in agreement with the Inventory filter chip and the
        # notification default threshold, all three of which read this same setting.
        set_site_config(expiry_threshold_days=15)
        results = get_expiring_soon_items(self.user)
        self.assertEqual({i.id for i in results}, {within.id, outside.id})

    def test_build_expiry_calendar_shape_and_counts(self):
        from .analytics import build_expiry_calendar

        target_date = date.today() + timedelta(days=2)
        make_item(self.user, expiry_date=target_date)

        months = build_expiry_calendar(self.user, months_ahead=2)
        self.assertEqual(len(months), 2)
        self.assertTrue(all('label' in m and 'weeks' in m for m in months))

        found_count = None
        for month in months:
            for week in month['weeks']:
                for day in week:
                    if day and day['date'] == target_date:
                        found_count = day['count']
        self.assertEqual(found_count, 1)

    def test_get_summary_stats_shape(self):
        from .analytics import get_summary_stats

        make_item(self.user, type='giftcard', wallet=self.wallet, value='15.00', currency='EUR',
                  expiry_date=date.today() + timedelta(days=5))
        make_item(self.user, type='loyaltycard', redeem_code='LOY1', value='0', expiry_date=date.today() + timedelta(days=400))

        stats = get_summary_stats(self.user)
        self.assertEqual(stats['total_items'], 2)
        self.assertEqual(stats['expiring_7_days'], 1)
        self.assertEqual(stats['value_by_currency'], {'EUR': '15.00'})
        self.assertEqual(stats['at_risk_value_by_currency'], {'EUR': '15.00'})
        type_counts = {row['type']: row['count'] for row in stats['by_type']}
        self.assertEqual(type_counts, {'giftcard': 1, 'loyaltycard': 1})

    def test_get_expiry_timeline_groups_by_date(self):
        from .analytics import get_expiry_timeline

        target_date = date.today() + timedelta(days=10)
        item = make_item(self.user, name='Grouped', expiry_date=target_date)

        timeline = get_expiry_timeline(self.user)
        key = target_date.isoformat()
        self.assertIn(key, timeline)
        self.assertEqual(timeline[key][0]['id'], str(item.id))
        self.assertEqual(timeline[key][0]['name'], 'Grouped')

    def test_get_next_up_items_returns_empty_when_no_wallets(self):
        from .analytics import get_next_up_items

        make_item(self.user, wallet=self.wallet, expiry_date=date.today() + timedelta(days=1))
        self.assertEqual(get_next_up_items([]), [])

    def test_get_next_up_items_picks_soonest_and_attaches_days_left(self):
        from .analytics import get_next_up_items

        soonest = make_item(self.user, name='Soonest', redeem_code='S1', wallet=self.wallet,
                             expiry_date=date.today() + timedelta(days=2))
        make_item(self.user, name='Later', redeem_code='L1', wallet=self.wallet,
                  expiry_date=date.today() + timedelta(days=10))

        results = get_next_up_items([self.wallet])
        self.assertEqual([r.id for r in results], [soonest.id])
        self.assertEqual(results[0].days_left, 2)

    def test_get_next_up_items_excludes_other_wallet_used_archived_and_past(self):
        from .analytics import get_next_up_items

        other_wallet = Wallet.objects.create(user=self.user, name='Other')
        make_item(self.user, name='OtherWallet', redeem_code='OW1', wallet=other_wallet,
                  expiry_date=date.today() + timedelta(days=1))
        make_item(self.user, name='Used', redeem_code='U1', wallet=self.wallet, is_used=True,
                  expiry_date=date.today() + timedelta(days=1))
        make_item(self.user, name='Archived', redeem_code='A1', wallet=self.wallet, is_archived=True,
                  expiry_date=date.today() + timedelta(days=1))
        make_item(self.user, name='Past', redeem_code='P1', wallet=self.wallet,
                  expiry_date=date.today() - timedelta(days=1))

        self.assertEqual(get_next_up_items([self.wallet]), [])

    def test_get_next_up_items_today_has_zero_days_left(self):
        from .analytics import get_next_up_items

        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today())
        results = get_next_up_items([self.wallet])
        self.assertEqual(results[0].id, item.id)
        self.assertEqual(results[0].days_left, 0)

    def test_get_next_up_items_interleaves_multiple_wallets_by_date(self):
        from .analytics import get_next_up_items

        other_wallet = Wallet.objects.create(user=self.user, name='Other')
        farther = make_item(self.user, name='Farther', redeem_code='F1', wallet=self.wallet,
                             expiry_date=date.today() + timedelta(days=5))
        nearer = make_item(self.user, name='Nearer', redeem_code='N1', wallet=other_wallet,
                            expiry_date=date.today() + timedelta(days=1))

        results = get_next_up_items([self.wallet, other_wallet], limit=3)
        self.assertEqual([r.id for r in results], [nearer.id, farther.id])

    def test_get_next_up_items_respects_limit(self):
        from .analytics import get_next_up_items

        for i in range(5):
            make_item(self.user, name=f'Item{i}', redeem_code=f'L{i}', wallet=self.wallet,
                      expiry_date=date.today() + timedelta(days=i + 1))

        results = get_next_up_items([self.wallet], limit=3)
        self.assertEqual(len(results), 3)


class DashboardAnalyticsContextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Travel', color='#4154f1')

    def test_dashboard_includes_analytics_context(self):
        make_item(self.user, name='Soon', wallet=self.wallet, expiry_date=date.today() + timedelta(days=3), value='25.00', currency='EUR')

        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['at_risk_value'], 25.0)
        self.assertEqual(len(response.context['expiry_calendar']), 3)
        self.assertEqual(response.context['items_by_wallet'][0]['name'], 'Travel')
        self.assertEqual(len(response.context['expiring_soon_list']), 1)
        self.assertGreaterEqual(response.context['wallet_chart_height'], 200)

    def test_dashboard_handles_no_items(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['at_risk_value'])
        self.assertEqual(response.context['items_by_wallet'], [])

    def test_dashboard_expiring_soon_list_agrees_with_inventory_count(self):
        """
        Regression coverage: the Dashboard's "Expiring Soon" list and the
        Inventory page's "Expiring Soon" filter chip must count the same
        items, since both are meant to reflect the same configured
        threshold - previously the Dashboard list used its own hardcoded
        7-day window regardless of what the threshold was set to.
        """
        set_site_config(expiry_threshold_days=12)
        make_item(self.user, name='In Window', wallet=self.wallet, expiry_date=date.today() + timedelta(days=10))
        make_item(self.user, name='Out of Window', redeem_code='OOW1', expiry_date=date.today() + timedelta(days=20))

        dashboard_response = self.client.get(reverse('dashboard'))
        inventory_response = self.client.get(reverse('show_items'), {'status': 'soon_expiring'})

        self.assertEqual(dashboard_response.context['expiry_threshold_days'], 12)
        self.assertEqual(len(dashboard_response.context['expiring_soon_list']), 1)
        self.assertEqual(len(inventory_response.context['items_with_qr']), 1)


class GenerateInitialAvatarTests(TestCase):
    """
    myapp.avatar.generate_initial_avatar - the fallback share/link-preview
    image when no merchant logo is cached, so a share never falls back to
    VoucherVault's own app icon.
    """
    def test_returns_valid_png_bytes(self):
        data = generate_initial_avatar('Uber Eats')
        self.assertTrue(data.startswith(b'\x89PNG'))
        image = Image.open(BytesIO(data))
        self.assertEqual(image.format, 'PNG')

    def test_uses_first_letter_uppercased(self):
        with patch('myapp.avatar.ImageDraw.ImageDraw.text') as mock_text:
            generate_initial_avatar('every wish')
            drawn_text = mock_text.call_args.args[1]
            self.assertEqual(drawn_text, 'E')

    def test_same_name_always_gets_the_same_color(self):
        first = generate_initial_avatar('Amazon')
        second = generate_initial_avatar('Amazon')
        self.assertEqual(first, second)

    def test_blank_name_does_not_crash(self):
        data = generate_initial_avatar('')
        self.assertTrue(data.startswith(b'\x89PNG'))

    def test_custom_size_respected(self):
        data = generate_initial_avatar('Amazon', size=128)
        image = Image.open(BytesIO(data))
        self.assertEqual(image.size, (128, 128))


class NormalizeLogoImageTests(TestCase):
    """
    myapp.avatar.normalize_logo_image - smooths a fetched merchant logo/
    favicon up (or down) to a consistent size, since some sources (Google's
    favicon service especially) return whatever native resolution a
    domain's favicon actually has - often just 32-48px for anything but
    the biggest brands - which looks blockily pixelated once stretched to
    fill a chat bubble or share preview otherwise.
    """
    def _make_png(self, size, color=(10, 20, 30, 255)):
        image = Image.new('RGBA', size, color)
        buffer = BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue()

    def test_upscales_a_small_source_image_to_the_target_size(self):
        small = self._make_png((32, 32))
        normalized = normalize_logo_image(small, size=256)
        image = Image.open(BytesIO(normalized))
        self.assertEqual(image.size, (256, 256))
        self.assertEqual(image.format, 'PNG')

    def test_downscales_a_larger_source_image_too(self):
        large = self._make_png((512, 512))
        normalized = normalize_logo_image(large, size=256)
        image = Image.open(BytesIO(normalized))
        self.assertEqual(image.size, (256, 256))

    def test_preserves_aspect_ratio_for_a_non_square_source(self):
        wide = self._make_png((100, 50))
        normalized = normalize_logo_image(wide, size=256)
        image = Image.open(BytesIO(normalized))
        # Scaled to fill the longer dimension (width, 256), then centered
        # on a 256x256 transparent canvas - never stretched/distorted.
        self.assertEqual(image.size, (256, 256))

    def test_returns_original_bytes_unchanged_when_not_a_parseable_image(self):
        garbage = b'not an image at all'
        self.assertEqual(normalize_logo_image(garbage), garbage)

    def test_already_correct_size_still_returns_valid_image(self):
        exact = self._make_png((256, 256))
        normalized = normalize_logo_image(exact, size=256)
        image = Image.open(BytesIO(normalized))
        self.assertEqual(image.size, (256, 256))


class MerchantLogoServiceTests(TestCase):
    def test_guess_domain_strips_non_alnum_and_lowercases(self):
        self.assertEqual(guess_domain('Amazon'), 'amazon.com')
        self.assertEqual(guess_domain("Trader Joe's"), 'traderjoes.com')

    def test_logo_sources_without_a_key_skips_logo_dev(self):
        sources = _logo_sources()
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0].startswith('https://logo.clearbit.com/'))
        self.assertTrue(sources[1].startswith('https://www.google.com/s2/favicons'))

    def test_logo_sources_with_a_key_puts_logo_dev_first(self):
        set_site_config(logo_dev_api_key='pk_test_123')
        sources = _logo_sources()
        self.assertEqual(len(sources), 3)
        self.assertTrue(sources[0].startswith('https://img.logo.dev/'))
        self.assertIn('token=pk_test_123', sources[0])
        self.assertIn('size=800', sources[0])
        self.assertIn('format=webp', sources[0])

    def test_all_logo_sources_request_800px(self):
        set_site_config(logo_dev_api_key='pk_test_123')
        for source in _logo_sources():
            self.assertTrue('800' in source, source)

    def test_get_cached_logo_never_hits_network(self):
        with patch('myapp.merchant_logos.requests.get') as mock_get:
            self.assertIsNone(get_cached_logo('Amazon'))
            mock_get.assert_not_called()

    def test_get_cached_logo_is_case_insensitive(self):
        MerchantProfile.objects.create(name='Amazon', logo_url='https://logo.clearbit.com/amazon.com')
        self.assertEqual(get_cached_logo('amazon').name, 'Amazon')

    def test_get_cached_logos_for_issuers_batches_and_skips_unfetched(self):
        MerchantProfile.objects.create(name='Amazon', logo_url='https://logo.clearbit.com/amazon.com')
        MerchantProfile.objects.create(name='Unknown Co', logo_url='')  # fetched but no logo found
        result = get_cached_logos_for_issuers(['Amazon', 'Unknown Co', 'Never Fetched', None, ''])
        self.assertEqual(result, {'amazon': 'https://logo.clearbit.com/amazon.com'})

    def test_get_cached_logos_for_issuers_empty_input(self):
        self.assertEqual(get_cached_logos_for_issuers([]), {})

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_uses_first_successful_source(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Amazon')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/amazon.com?size=800')
        self.assertEqual(profile.domain, 'amazon.com')
        self.assertIsNotNone(profile.fetched_at)
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_prefers_logo_dev_when_key_configured(self, mock_get):
        set_site_config(logo_dev_api_key='pk_test_123')
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Amazon')
        self.assertTrue(profile.logo_url.startswith('https://img.logo.dev/amazon.com'))
        self.assertIn('token=pk_test_123', profile.logo_url)
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_falls_back_from_logo_dev_when_it_fails(self, mock_get):
        set_site_config(logo_dev_api_key='pk_test_123')
        mock_get.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]
        profile = fetch_merchant_logo('Amazon')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/amazon.com?size=800')
        self.assertEqual(mock_get.call_count, 2)

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_refetches_when_a_logo_dev_key_is_newly_added(self, mock_get):
        # Regression test for the exact reported bug: a merchant that was
        # already successfully cached via Clearbit/Google (before a
        # logo.dev key existed) must pick up logo.dev on the very next
        # fetch once a key is added, not sit on the old lower-quality
        # result for the rest of the normal 30-day freshness window.
        MerchantProfile.objects.create(
            name='Amazon', domain='amazon.com',
            logo_url='https://logo.clearbit.com/amazon.com?size=800', fetched_at=timezone.now(),
        )
        set_site_config(logo_dev_api_key='pk_test_123')
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Amazon')
        self.assertTrue(profile.logo_url.startswith('https://img.logo.dev/amazon.com'))
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_skips_refetch_when_already_using_logo_dev(self, mock_get):
        set_site_config(logo_dev_api_key='pk_test_123')
        MerchantProfile.objects.create(
            name='Amazon', domain='amazon.com',
            logo_url='https://img.logo.dev/amazon.com?token=pk_test_123&size=800&format=webp',
            fetched_at=timezone.now(),
        )
        fetch_merchant_logo('Amazon')
        mock_get.assert_not_called()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_falls_back_to_second_source(self, mock_get):
        mock_get.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]
        profile = fetch_merchant_logo('Amazon')
        self.assertEqual(profile.logo_url, 'https://www.google.com/s2/favicons?sz=800&domain=amazon.com')
        self.assertEqual(mock_get.call_count, 2)

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_all_sources_fail_still_stamps_fetched_at(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException('boom')
        profile = fetch_merchant_logo('Unknown Merchant')
        self.assertEqual(profile.logo_url, '')
        self.assertIsNotNone(profile.fetched_at)

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_is_case_insensitive_get_or_create(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        fetch_merchant_logo('Amazon')
        fetch_merchant_logo('amazon')
        self.assertEqual(MerchantProfile.objects.count(), 1)

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_skips_network_when_cache_fresh(self, mock_get):
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com', fetched_at=timezone.now()
        )
        fetch_merchant_logo('Amazon')
        mock_get.assert_not_called()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_prefers_domain_hint_over_guessed_domain(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Every Wish', domain_hint='uber.com')
        self.assertEqual(profile.domain, 'uber.com')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/uber.com?size=800')

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_refetches_when_domain_hint_changes_despite_fresh_cache(self, mock_get):
        MerchantProfile.objects.create(
            name='Every Wish', domain='everywish.com', logo_url='', fetched_at=timezone.now()
        )
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Every Wish', domain_hint='uber.com')
        self.assertEqual(profile.domain, 'uber.com')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/uber.com?size=800')
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_skips_refetch_when_domain_hint_matches_cached_domain(self, mock_get):
        MerchantProfile.objects.create(
            name='Every Wish', domain='uber.com', logo_url='https://logo.clearbit.com/uber.com?size=800',
            fetched_at=timezone.now(),
        )
        fetch_merchant_logo('Every Wish', domain_hint='uber.com')
        mock_get.assert_not_called()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_retries_failed_fetch_after_a_day_not_a_month(self, mock_get):
        MerchantProfile.objects.create(
            name='Amazon', logo_url='', fetched_at=timezone.now() - timedelta(days=2)
        )
        mock_get.return_value = MagicMock(status_code=200)
        profile = fetch_merchant_logo('Amazon')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/amazon.com?size=800')
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_does_not_retry_successful_fetch_within_cache_period(self, mock_get):
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com',
            fetched_at=timezone.now() - timedelta(days=2),
        )
        fetch_merchant_logo('Amazon')
        mock_get.assert_not_called()


class BalanceCheckUrlServiceTests(TestCase):
    def test_get_cached_balance_check_url_when_unknown(self):
        self.assertEqual(get_cached_balance_check_url('Never Seen Co'), '')

    def test_remember_creates_new_merchant_profile(self):
        remember_balance_check_url('Tesco', 'https://www.tesco.com/gift-cards/balance')
        self.assertEqual(get_cached_balance_check_url('tesco'), 'https://www.tesco.com/gift-cards/balance')

    def test_remember_updates_existing_profile_without_overwriting_logo(self):
        MerchantProfile.objects.create(name='Tesco', logo_url='https://logo.clearbit.com/tesco.com')
        remember_balance_check_url('Tesco', 'https://www.tesco.com/gift-cards/balance')
        profile = MerchantProfile.objects.get(name='Tesco')
        self.assertEqual(profile.balance_check_url, 'https://www.tesco.com/gift-cards/balance')
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/tesco.com')

    def test_remember_last_write_wins(self):
        remember_balance_check_url('Tesco', 'https://old.example.com/balance')
        remember_balance_check_url('Tesco', 'https://new.example.com/balance')
        self.assertEqual(get_cached_balance_check_url('Tesco'), 'https://new.example.com/balance')
        self.assertEqual(MerchantProfile.objects.filter(name__iexact='tesco').count(), 1)

    def test_remember_noop_when_issuer_or_url_blank(self):
        remember_balance_check_url('', 'https://example.com')
        remember_balance_check_url('Tesco', '')
        self.assertFalse(MerchantProfile.objects.exists())


class MerchantLogoTaskTests(TestCase):
    @patch('myapp.tasks.fetch_merchant_logo')
    def test_task_calls_service_when_enabled(self, mock_fetch):
        set_site_config(merchant_logos_enabled=True)
        fetch_merchant_logo_task('Amazon')
        mock_fetch.assert_called_once_with('Amazon', domain_hint=None)

    @patch('myapp.tasks.fetch_merchant_logo')
    def test_task_passes_domain_hint_through(self, mock_fetch):
        set_site_config(merchant_logos_enabled=True)
        fetch_merchant_logo_task('Every Wish', 'uber.com')
        mock_fetch.assert_called_once_with('Every Wish', domain_hint='uber.com')

    @patch('myapp.tasks.fetch_merchant_logo')
    def test_task_noop_when_disabled(self, mock_fetch):
        set_site_config(merchant_logos_enabled=False)
        fetch_merchant_logo_task('Amazon')
        mock_fetch.assert_not_called()

    @patch('myapp.tasks.fetch_merchant_logo')
    def test_task_noop_for_empty_name(self, mock_fetch):
        fetch_merchant_logo_task('')
        mock_fetch.assert_not_called()


class MerchantLogoViewIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    @patch('myapp.views.fetch_merchant_logo_task.delay')
    def test_create_item_dispatches_logo_fetch(self, mock_delay):
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Flight Voucher', 'issuer': 'Airline', 'redeem_code': 'FLY100',
            'value': '100.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        mock_delay.assert_called_once_with('Airline', None)

    @patch('myapp.views.fetch_merchant_logo_task.delay', side_effect=RuntimeError('broker down'))
    def test_create_item_survives_broker_outage(self, mock_delay):
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Flight Voucher', 'issuer': 'Airline', 'redeem_code': 'FLY100',
            'value': '100.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        self.assertTrue(Item.objects.filter(name='Flight Voucher').exists())

    @patch('myapp.views.fetch_merchant_logo_task.delay')
    def test_edit_item_dispatches_logo_fetch(self, mock_delay):
        item = make_item(self.user, name='Old Name', issuer='Old Issuer')
        response = self.client.post(reverse('edit_item', args=[item.id]), {
            'type': 'voucher', 'name': 'New Name', 'issuer': 'New Issuer', 'redeem_code': item.redeem_code,
            'value': '10.00', 'currency': 'USD', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(), 'expiry_date': (date.today() + timedelta(days=30)).isoformat(),
        })
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': item.id}))
        mock_delay.assert_called_once_with('New Issuer', None)

    def test_show_items_includes_cached_merchant_logo(self):
        make_item(self.user, name='Amazon Voucher', issuer='Amazon')
        MerchantProfile.objects.create(name='Amazon', logo_url='https://logo.clearbit.com/amazon.com')

        response = self.client.get(reverse('show_items'))
        entries = {e['item'].name: e['merchant_logo_url'] for e in response.context['items_with_qr']}
        self.assertEqual(entries['Amazon Voucher'], 'https://logo.clearbit.com/amazon.com')

    def test_show_items_no_logo_yields_none(self):
        make_item(self.user, name='Unknown Voucher', issuer='Nowhere Co')
        response = self.client.get(reverse('show_items'))
        entries = {e['item'].name: e['merchant_logo_url'] for e in response.context['items_with_qr']}
        self.assertIsNone(entries['Unknown Voucher'])

    def test_view_item_includes_cached_merchant_logo(self):
        item = make_item(self.user, name='Amazon Voucher', issuer='Amazon')
        MerchantProfile.objects.create(name='Amazon', logo_url='https://logo.clearbit.com/amazon.com')

        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertEqual(response.context['merchant_logo_url'], 'https://logo.clearbit.com/amazon.com')


class OCRScanUIWiringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_item_ocr_disabled_by_default(self):
        set_site_config(ocr_backend='none')
        response = self.client.get(reverse('create_item'))
        self.assertFalse(response.context['ocr_enabled'])
        self.assertNotContains(response, 'aiScanSection')
        # "Dumb mode": with AI off, only the barcode-only scanner ships -
        # no merged AI-scan wiring at all.
        self.assertNotContains(response, 'decodeBarcodeFromImageFile(file).catch')
        self.assertContains(response, 'id="scanFromFile"')
        self.assertContains(response, 'id="startScanner"')

    def test_create_item_ocr_enabled_shows_scan_section(self):
        set_site_config(ocr_backend='tesseract')
        response = self.client.get(reverse('create_item'))
        self.assertTrue(response.context['ocr_enabled'])
        self.assertContains(response, 'aiScanSection')
        # The merged upload runs the client-side barcode decode against the
        # same photo instead of asking for it twice, and the barcode result
        # always takes priority over the AI's guess.
        self.assertContains(response, 'decodeBarcodeFromImageFile(file).catch')
        self.assertContains(response, "applyDetectedFormat(decoded.formatValue, \"barcode in photo\")")

    def test_edit_item_reflects_ocr_setting(self):
        item = make_item(self.user)
        set_site_config(ocr_backend='claude')
        response = self.client.get(reverse('edit_item', args=[item.id]))
        self.assertTrue(response.context['ocr_enabled'])
        self.assertContains(response, 'aiScanSection')
        self.assertContains(response, 'decodeBarcodeFromImageFile(file).catch')
        # A photo uploaded to refresh name/issuer/expiry on an existing item
        # must never overwrite its already-correct redeem_code/code_type -
        # the merged AI-scan handler must gate every field write on the
        # code having started empty.
        self.assertContains(response, 'const codeWasEmpty = redeemCodeField && !redeemCodeField.value;')
        self.assertContains(response, 'codeWasEmpty && decoded.formatValue')
        self.assertContains(response, 'codeWasEmpty && data.code_type')

    def test_duplicate_item_reflects_ocr_setting(self):
        item = make_item(self.user)
        set_site_config(ocr_backend='tesseract')
        response = self.client.get(reverse('duplicate_item', args=[item.id]))
        self.assertTrue(response.context['ocr_enabled'])


class PkpassUIWiringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_view_item_pkpass_disabled_by_default(self):
        item = make_item(self.user)
        set_site_config(pkpass_cert_path='')
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertFalse(response.context['pkpass_enabled'])
        self.assertNotContains(response, 'Add to Apple Wallet')

    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_view_item_shows_pkpass_link_when_enabled(self, mock_enabled):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertTrue(response.context['pkpass_enabled'])
        self.assertContains(response, 'Add to Apple Wallet')

    def test_view_item_google_wallet_disabled_by_default(self):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertIsNone(response.context['google_wallet_save_url'])
        self.assertNotContains(response, 'Add to Google Wallet')

    @patch('myapp.views.generate_google_wallet_save_url', return_value='https://pay.google.com/gp/v/save/fake-jwt')
    @patch('myapp.views.google_wallet_enabled', return_value=True)
    def test_view_item_shows_google_wallet_link_when_enabled(self, mock_enabled, mock_generate):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertEqual(response.context['google_wallet_save_url'], 'https://pay.google.com/gp/v/save/fake-jwt')
        self.assertContains(response, 'Add to Google Wallet')
        self.assertContains(response, 'https://pay.google.com/gp/v/save/fake-jwt')

    @patch('myapp.views.generate_google_wallet_save_url', side_effect=RuntimeError('boom'))
    @patch('myapp.views.google_wallet_enabled', return_value=True)
    def test_view_item_hides_google_wallet_link_on_generation_failure(self, mock_enabled, mock_generate):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertIsNone(response.context['google_wallet_save_url'])
        self.assertNotContains(response, 'Add to Google Wallet')


def make_upload(name='receipt.pdf', content=b'%PDF-1.4 test', content_type='application/pdf'):
    return SimpleUploadedFile(name, content, content_type=content_type)


class DocumentUploadTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.item = make_item(self.alice)
        self.client.login(username='alice', password='pw12345!')

    def test_owner_can_upload_document(self):
        response = self.client.post(
            reverse('upload_document', args=[self.item.id]),
            {'file': make_upload()},
        )
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': self.item.id}))
        self.assertEqual(self.item.documents.count(), 1)

    def test_rejects_unsupported_file_type(self):
        bad_file = SimpleUploadedFile('malware.exe', b'MZ', content_type='application/octet-stream')
        self.client.post(reverse('upload_document', args=[self.item.id]), {'file': bad_file})
        self.assertEqual(self.item.documents.count(), 0)

    def test_owner_can_delete_document(self):
        document = Document.objects.create(item=self.item, file=make_upload())
        response = self.client.post(reverse('delete_document', args=[document.id]))
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': self.item.id}))
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())

    def test_owner_can_download_document(self):
        document = Document.objects.create(item=self.item, file=make_upload())
        response = self.client.get(reverse('download_document', args=[document.id]))
        self.assertEqual(response.status_code, 200)

    def test_owner_can_view_previewable_document_inline(self):
        document = Document.objects.create(item=self.item, file=make_upload('receipt.pdf'))
        response = self.client.get(reverse('view_document_file', args=[document.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertNotIn('Content-Disposition', response)

    def test_view_rejects_non_previewable_document(self):
        document = Document.objects.create(
            item=self.item,
            file=SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain'),
        )
        response = self.client.get(reverse('view_document_file', args=[document.id]))
        self.assertEqual(response.status_code, 400)

    def test_non_collaborator_cannot_upload_view_or_delete(self):
        document = Document.objects.create(item=self.item, file=make_upload())
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')

        upload_response = self.client.post(reverse('upload_document', args=[self.item.id]), {'file': make_upload()})
        self.assertEqual(upload_response.status_code, 403)

        download_response = self.client.get(reverse('download_document', args=[document.id]))
        self.assertEqual(download_response.status_code, 403)

        view_response = self.client.get(reverse('view_document_file', args=[document.id]))
        self.assertEqual(view_response.status_code, 403)

        delete_response = self.client.post(reverse('delete_document', args=[document.id]))
        self.assertEqual(delete_response.status_code, 403)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())


class SharedWalletTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.carol = User.objects.create_user(username='carol', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.alice, name='Family')
        self.wallet.shared_with.add(self.bob)
        self.item = make_item(self.alice, wallet=self.wallet)

    def test_owner_can_share_wallet(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('share_wallet', args=[self.wallet.id]), {'username': 'carol'})
        self.assertRedirects(response, reverse('edit_wallet', kwargs={'wallet_id': self.wallet.id}))
        self.assertIn(self.carol, self.wallet.shared_with.all())

    def test_non_owner_cannot_share_wallet(self):
        self.client.login(username='bob', password='pw12345!')
        response = self.client.post(reverse('share_wallet', args=[self.wallet.id]), {'username': 'carol'})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(self.carol, self.wallet.shared_with.all())

    def test_owner_can_unshare_wallet(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('unshare_wallet', args=[self.wallet.id, self.bob.id]))
        self.assertRedirects(response, reverse('edit_wallet', kwargs={'wallet_id': self.wallet.id}))
        self.assertNotIn(self.bob, self.wallet.shared_with.all())

    def test_collaborator_can_leave_shared_wallet(self):
        self.client.login(username='bob', password='pw12345!')
        response = self.client.post(reverse('leave_shared_wallet', args=[self.wallet.id]))
        self.assertRedirects(response, reverse('manage_wallets'))
        self.assertNotIn(self.bob, self.wallet.shared_with.all())

    def test_collaborator_sees_shared_wallet_items_in_inventory(self):
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertIn(self.item.name, names)

    def test_outsider_does_not_see_shared_wallet_items(self):
        self.client.login(username='carol', password='pw12345!')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertNotIn(self.item.name, names)

    def test_collaborator_can_view_and_edit_item_in_shared_wallet(self):
        self.client.login(username='bob', password='pw12345!')
        view_response = self.client.get(reverse('view_item', kwargs={'item_uuid': self.item.id}))
        self.assertEqual(view_response.status_code, 200)
        self.assertTrue(view_response.context['can_edit'])
        self.assertFalse(view_response.context['is_owner'])

        edit_response = self.client.post(reverse('edit_item', args=[self.item.id]), {
            'type': 'voucher', 'name': 'Renamed', 'issuer': 'Acme', 'redeem_code': 'ABC123',
            'value': '10.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(), 'wallet': self.wallet.id,
        })
        self.assertRedirects(edit_response, reverse('view_item', kwargs={'item_uuid': self.item.id}))
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Renamed')

    def test_collaborator_can_delete_item_in_shared_wallet(self):
        self.client.login(username='bob', password='pw12345!')
        response = self.client.post(reverse('delete_item', args=[self.item.id]))
        self.assertRedirects(response, reverse('show_items'))
        self.assertFalse(Item.objects.filter(pk=self.item.pk).exists())

    def test_outsider_cannot_view_item_in_shared_wallet(self):
        self.client.login(username='carol', password='pw12345!')
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': self.item.id}))
        self.assertEqual(response.status_code, 403)

    def test_wallet_dropdown_offers_shared_wallets_to_collaborator(self):
        form = ItemForm(user=self.bob)
        self.assertIn(self.wallet, form.fields['wallet'].queryset)


class WebShareButtonWiringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_view_item_renders_share_button(self):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'share-voucher-btn')

    def test_view_item_share_button_carries_correct_public_share_url(self):
        """
        Regression test: voucher-share.js used to hand-build this URL as
        `/items/<id>/public-share/`, which 404s against myapp.urls' i18n
        prefix (see myproject/urls.py's i18n_patterns), gets 301/302-redirected
        by LocaleMiddleware to the prefixed URL, and browsers silently
        downgrade a POST to a GET when following that redirect - which
        @require_POST then rejected with 405. The button must instead carry
        the server-rendered (already-prefixed) URL so no redirect is ever
        involved.
        """
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        expected_url = reverse('get_public_share_link', args=[item.id])
        self.assertIn('/public-share/', expected_url)
        self.assertContains(response, f'data-public-share-url="{expected_url}"')

    def test_view_item_renders_action_toolbar_and_more_sheet(self):
        # Class names also appear unconditionally in the page's own <style>
        # block, so assert on the actual class="..." attribute usage rather
        # than a bare substring match against the whole response body.
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'id="more-actions-toggle"')
        self.assertContains(response, 'id="more-actions-sheet"')
        self.assertContains(response, 'class="toolbar-btn toolbar-btn-primary"')
        # Secondary/destructive actions moved into the "More actions" sheet
        self.assertContains(response, 'class="more-action-row more-action-row-warning"')
        self.assertContains(response, 'class="more-action-row more-action-row-danger"')

    def test_view_item_mark_used_toggle_uses_success_row_when_already_used(self):
        item = make_item(self.user, is_used=True)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'class="more-action-row more-action-row-success"')
        self.assertNotContains(response, 'class="more-action-row more-action-row-warning"')

    def test_inventory_cards_render_share_button(self):
        make_item(self.user)
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'share-voucher-btn')

    def test_inventory_card_share_button_carries_correct_public_share_url(self):
        item = make_item(self.user)
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        expected_url = reverse('get_public_share_link', args=[item.id])
        self.assertContains(response, f'data-public-share-url="{expected_url}"')

    def test_smart_share_flag_reflects_site_setting(self):
        set_site_config(share_via_smart_enabled=True)
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'VV_SHARE_SMART_ENABLED = true')

        set_site_config(share_via_smart_enabled=False)
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'VV_SHARE_SMART_ENABLED = false')


class PublicShareLinkTests(TestCase):
    """
    The "Share via... -> Share details" flow: a per-item, tokenized,
    no-login-required link (ItemPublicShare) that carries merchant, code,
    PIN, and remaining balance - distinct from ItemShare, which grants
    another *VoucherVault user* full access and therefore requires an
    account.
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_owner_can_create_link(self):
        item = make_item(self.alice, pin='4321')
        response = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                     HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('/s/', data['url'])
        self.assertEqual(data['merchant'], item.issuer)
        self.assertEqual(data['code'], item.redeem_code)
        self.assertEqual(data['pin'], '4321')
        self.assertIn(reverse('item_share_logo', args=[item.id]), data['logo_image_url'])
        self.assertTrue(ItemPublicShare.objects.filter(item=item).exists())

    def test_redeem_code_shared_even_when_card_number_also_set(self):
        # card_number is a secondary "if different from the barcode" field
        # (see Item.card_number's help_text) - the redeem code is what's
        # actually needed to redeem the voucher, so it must never be
        # dropped from the share payload in favour of card_number.
        item = make_item(self.alice, card_number='MEMBER-9', redeem_code='REDEEM-1')
        response = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                     HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.json()['code'], 'REDEEM-1')

    def test_balance_included_for_giftcard_only(self):
        giftcard = make_item(self.alice, type='giftcard', redeem_code='GC1', value='25.00')
        voucher = make_item(self.alice, type='voucher', redeem_code='V1')

        gc_response = self.client.post(reverse('get_public_share_link', args=[giftcard.id]),
                                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(gc_response.json()['balance'], '25.00')

        v_response = self.client.post(reverse('get_public_share_link', args=[voucher.id]),
                                       HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertIsNone(v_response.json()['balance'])

    def test_repeated_calls_reuse_same_link(self):
        item = make_item(self.alice)
        first = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                  HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        second = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertEqual(first['url'], second['url'])
        self.assertEqual(ItemPublicShare.objects.filter(item=item).count(), 1)

    def test_non_collaborator_cannot_create_link(self):
        item = make_item(self.alice)
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')
        response = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                     HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)

    def test_regenerate_invalidates_old_link(self):
        item = make_item(self.alice)
        first = self.client.post(reverse('get_public_share_link', args=[item.id]),
                                  HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        old_share_id = ItemPublicShare.objects.get(item=item).id

        second = self.client.post(reverse('regenerate_public_share_link', args=[item.id]),
                                   HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()
        self.assertNotEqual(first['url'], second['url'])

        self.client.logout()
        old_response = self.client.get(reverse('public_item_share', args=[old_share_id]))
        self.assertEqual(old_response.status_code, 410)

    def test_revoke_deletes_link(self):
        item = make_item(self.alice)
        self.client.post(reverse('get_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.client.post(reverse('revoke_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertFalse(ItemPublicShare.objects.filter(item=item).exists())

    def test_plain_form_post_redirects_with_message_instead_of_json(self):
        item = make_item(self.alice)
        response = self.client.post(reverse('get_public_share_link', args=[item.id]), follow=True)
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'Public share link created')

    def test_public_page_requires_no_login_and_tracks_views(self):
        item = make_item(self.alice, pin='1111', card_number='CARD-1')
        share_id = ItemPublicShare.objects.create(item=item, created_by=self.alice).id
        self.client.logout()

        response = self.client.get(reverse('public_item_share', args=[share_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, item.issuer)
        self.assertContains(response, 'CARD-1')
        self.assertContains(response, '1111')

        share = ItemPublicShare.objects.get(id=share_id)
        self.assertEqual(share.view_count, 1)
        self.assertIsNotNone(share.first_viewed_at)
        self.assertIsNotNone(share.last_viewed_at)

        self.client.get(reverse('public_item_share', args=[share_id]))
        share.refresh_from_db()
        self.assertEqual(share.view_count, 2)

    def test_public_page_unknown_token_returns_revoked_page(self):
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 410)

    def test_public_page_excludes_notes(self):
        item = make_item(self.alice, notes='Secret redemption instructions nobody else should see')
        share_id = ItemPublicShare.objects.create(item=item, created_by=self.alice).id
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share_id]))
        self.assertNotContains(response, 'Secret redemption instructions')

    def test_public_page_always_shows_a_merchant_logo_image_and_og_image(self):
        # Regression test: this used to render nothing at all (an empty
        # gap) when no merchant logo happened to be cached yet - the page
        # must always show *something* merchant-relevant (real logo or the
        # generated initial-avatar fallback - see public_item_share_logo),
        # never leave the slot blank, and og:image must point at the same
        # endpoint so link-preview crawlers see the identical image.
        item = make_item(self.alice, issuer='Totally Unknown Merchant')
        share_id = ItemPublicShare.objects.create(item=item, created_by=self.alice).id
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share_id]))
        logo_url = reverse('public_item_share_logo', args=[share_id])
        self.assertContains(response, f'src="{logo_url}"')
        self.assertContains(response, f'og:image" content="http://testserver{logo_url}"')

    def test_item_detail_shows_link_management_card_to_owner(self):
        item = make_item(self.alice)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'Public Share Link')
        self.assertContains(response, 'Create link now')

    def test_item_detail_shows_existing_link_and_view_count(self):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        share.record_view()
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'Opened 1 time')
        self.assertContains(response, f'/s/{share.id}/')


class ItemShareLogoViewTests(TestCase):
    """
    myapp.views.item_share_logo - the same-origin proxy the "Share via..."
    chooser's image+details option fetches to attach the merchant's logo as
    a real shared image file (see voucher-share.js).
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_requires_login(self):
        item = make_item(self.alice)
        self.client.logout()
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertNotEqual(response.status_code, 200)

    def test_non_collaborator_denied(self):
        item = make_item(self.alice)
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.status_code, 403)

    @patch('myapp.views.requests.get')
    def test_proxies_cached_merchant_logo(self, mock_get):
        item = make_item(self.alice, issuer='Amazon')
        # fetched_at set - an already-fresh cache, so the synchronous
        # refresh in _resolve_merchant_share_image is a no-op and the
        # only requests.get call is the proxy fetch this test is about.
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com', fetched_at=timezone.now(),
        )
        mock_get.return_value = MagicMock(
            status_code=200, content=b'fake-image-bytes', headers={'Content-Type': 'image/png'}
        )
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'fake-image-bytes')
        self.assertEqual(response['Content-Type'], 'image/png')
        mock_get.assert_called_once_with('https://logo.clearbit.com/amazon.com', timeout=5)

    @patch('myapp.views.requests.get')
    def test_resolves_from_logo_slug_when_nothing_cached_yet(self, mock_get):
        # No prior MerchantProfile row at all - the synchronous resolution
        # in _resolve_merchant_share_image must still find the right
        # domain from the item's own logo_slug, without needing the
        # async fetch_merchant_logo_task to have run first.
        item = make_item(self.alice, issuer='Every Wish', logo_slug='uber.com')
        mock_get.return_value = MagicMock(
            status_code=200, content=b'uber-logo-bytes', headers={'Content-Type': 'image/png'}
        )
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.content, b'uber-logo-bytes')
        profile = MerchantProfile.objects.get(name='Every Wish')
        self.assertEqual(profile.domain, 'uber.com')

    @patch('myapp.views.requests.get')
    def test_resolves_from_logo_slug_even_with_a_stale_wrong_domain_already_cached(self, mock_get):
        # Regression test for the exact reported bug: "Every Wish" (the
        # issuer) already has a stale MerchantProfile cached under a
        # wrong guessed domain - as would happen for an item saved
        # before logo_slug existed, or before this item's own scan
        # populated it. The share image must resolve via this item's own
        # logo_slug right now, not require a prior edit/save to
        # re-trigger the async fetch task.
        item = make_item(self.alice, issuer='Every Wish', logo_slug='uber.com')
        MerchantProfile.objects.create(
            name='Every Wish', domain='everywish.com', logo_url='', fetched_at=timezone.now()
        )
        mock_get.return_value = MagicMock(
            status_code=200, content=b'uber-logo-bytes', headers={'Content-Type': 'image/png'}
        )
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.content, b'uber-logo-bytes')
        profile = MerchantProfile.objects.get(name='Every Wish')
        self.assertEqual(profile.domain, 'uber.com')

    def test_skips_synchronous_refresh_when_merchant_logos_disabled(self):
        set_site_config(merchant_logos_enabled=False)
        item = make_item(self.alice, issuer='Every Wish', logo_slug='uber.com')
        with patch('myapp.views.fetch_merchant_logo') as mock_fetch:
            response = self.client.get(reverse('item_share_logo', args=[item.id]))
        mock_fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)

    @patch('myapp.views.requests.get')
    def test_falls_back_to_generated_avatar_when_no_cached_logo(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException('no network in tests')
        item = make_item(self.alice, issuer='Totally Unknown Merchant')
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertTrue(response.content.startswith(b'\x89PNG'))

    @patch('myapp.views.requests.get')
    def test_falls_back_to_generated_avatar_when_upstream_fetch_fails(self, mock_get):
        import requests
        item = make_item(self.alice, issuer='Amazon')
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com', fetched_at=timezone.now(),
        )
        mock_get.side_effect = requests.RequestException('boom')
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\x89PNG'))

    @patch('myapp.views.requests.get')
    def test_falls_back_to_generated_avatar_when_upstream_returns_error_status(self, mock_get):
        item = make_item(self.alice, issuer='Amazon')
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com', fetched_at=timezone.now(),
        )
        mock_get.return_value = MagicMock(status_code=404, content=b'')
        response = self.client.get(reverse('item_share_logo', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\x89PNG'))


class PublicItemShareLogoViewTests(TestCase):
    """
    myapp.views.public_item_share_logo - the unauthenticated, share_id-
    keyed counterpart to item_share_logo, used by public_item.html's
    on-page <img> and og:image link-preview meta tag.
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')

    @patch('myapp.views.requests.get')
    def test_no_login_required(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=b'x', headers={})
        item = make_item(self.alice, issuer='Amazon')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        response = self.client.get(reverse('public_item_share_logo', args=[share.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_unknown_share_404s(self):
        response = self.client.get(reverse('public_item_share_logo', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)

    @patch('myapp.views.requests.get')
    def test_proxies_cached_merchant_logo(self, mock_get):
        item = make_item(self.alice, issuer='Amazon')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        # fetched_at set - an already-fresh cache, so the synchronous
        # refresh in _resolve_merchant_share_image is a no-op and the
        # only requests.get call is the proxy fetch this test is about.
        MerchantProfile.objects.create(
            name='Amazon', logo_url='https://logo.clearbit.com/amazon.com', fetched_at=timezone.now(),
        )
        mock_get.return_value = MagicMock(
            status_code=200, content=b'fake-image-bytes', headers={'Content-Type': 'image/png'}
        )
        response = self.client.get(reverse('public_item_share_logo', args=[share.id]))
        self.assertEqual(response.content, b'fake-image-bytes')

    @patch('myapp.views.requests.get')
    def test_falls_back_to_generated_avatar_when_no_cached_logo(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException('no network in tests')
        item = make_item(self.alice, issuer='Totally Unknown Merchant')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        response = self.client.get(reverse('public_item_share_logo', args=[share.id]))
        self.assertTrue(response.content.startswith(b'\x89PNG'))

    @patch('myapp.views.requests.get')
    def test_works_even_when_share_is_expired(self, mock_get):
        # The logo image itself carries no sensitive data - exposed the
        # same as the main page already exposes it unconditionally across
        # every one of its states (crawler/expired/PIN-required/unlocked).
        mock_get.return_value = MagicMock(status_code=200, content=b'x', headers={})
        item = make_item(self.alice, issuer='Amazon')
        share = ItemPublicShare.objects.create(
            item=item, created_by=self.alice, expires_at=timezone.now() - timedelta(days=1),
        )
        response = self.client.get(reverse('public_item_share_logo', args=[share.id]))
        self.assertEqual(response.status_code, 200)

    @patch('myapp.views.requests.get')
    def test_works_even_when_pin_locked(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, content=b'x', headers={})
        item = make_item(self.alice, issuer='Amazon')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice, access_pin='1234')
        response = self.client.get(reverse('public_item_share_logo', args=[share.id]))
        self.assertEqual(response.status_code, 200)


class PublicShareSecurityTests(TestCase):
    """
    Covers the expiry/PIN-gate/crawler-detection/rate-limiting overhaul on
    top of the base ItemPublicShare flow tested above.
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_link_sets_expiry_from_site_config(self):
        set_site_config(share_link_expiry_days=30)
        item = make_item(self.alice)
        self.client.post(reverse('get_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        share = ItemPublicShare.objects.get(item=item)
        self.assertIsNotNone(share.expires_at)
        self.assertAlmostEqual(share.expires_at, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))

    def test_create_link_never_expires_when_zero(self):
        set_site_config(share_link_expiry_days=0)
        item = make_item(self.alice)
        self.client.post(reverse('get_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        share = ItemPublicShare.objects.get(item=item)
        self.assertIsNone(share.expires_at)

    def test_create_link_generates_pin_when_enabled(self):
        set_site_config(share_link_pin_enabled=True)
        item = make_item(self.alice)
        self.client.post(reverse('get_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        share = ItemPublicShare.objects.get(item=item)
        self.assertRegex(share.access_pin, r'^\d{4}$')

    def test_create_link_no_pin_when_disabled(self):
        set_site_config(share_link_pin_enabled=False)
        item = make_item(self.alice)
        self.client.post(reverse('get_public_share_link', args=[item.id]),
                          HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        share = ItemPublicShare.objects.get(item=item)
        self.assertEqual(share.access_pin, '')

    def test_expired_link_shows_expired_state_and_not_content(self):
        item = make_item(self.alice, redeem_code='SECRETCODE')
        share = ItemPublicShare.objects.create(
            item=item, created_by=self.alice, expires_at=timezone.now() - timedelta(days=1),
        )
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertContains(response, 'Link expired')
        self.assertNotContains(response, 'SECRETCODE')
        share.refresh_from_db()
        self.assertEqual(share.view_count, 0)

    def test_pin_gate_blocks_content_until_correct_pin(self):
        item = make_item(self.alice, redeem_code='SECRETCODE')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice, access_pin='1234')
        self.client.logout()

        get_response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertNotContains(get_response, 'SECRETCODE')

        wrong = self.client.post(reverse('public_item_share', args=[share.id]), {'access_pin': '0000'})
        self.assertContains(wrong, 'Incorrect code')
        self.assertNotContains(wrong, 'SECRETCODE')
        share.refresh_from_db()
        self.assertEqual(share.failed_pin_attempts, 1)

        correct = self.client.post(reverse('public_item_share', args=[share.id]), {'access_pin': '1234'})
        self.assertContains(correct, 'SECRETCODE')

        # session unlock persists on a subsequent GET without re-entering the PIN
        again = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertContains(again, 'SECRETCODE')

    def test_pin_attempt_rate_limit_blocks_without_incrementing_further(self):
        item = make_item(self.alice, redeem_code='SECRETCODE')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice, access_pin='1234')
        self.client.logout()

        with patch('myapp.views.pin_attempt_rate_limited', return_value=True):
            response = self.client.post(reverse('public_item_share', args=[share.id]), {'access_pin': '0000'})
        self.assertContains(response, 'Too many attempts')
        share.refresh_from_db()
        self.assertEqual(share.failed_pin_attempts, 0)

    def test_link_preview_bot_gets_metadata_only_and_no_view_count(self):
        item = make_item(self.alice, redeem_code='SECRETCODE')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()

        response = self.client.get(reverse('public_item_share', args=[share.id]),
                                    HTTP_USER_AGENT='WhatsApp/2.23.1 A')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, item.issuer)
        self.assertNotContains(response, 'SECRETCODE')
        share.refresh_from_db()
        self.assertEqual(share.view_count, 0)

    def test_view_rate_limit_returns_429(self):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()

        with patch('myapp.views.view_rate_limited', return_value=True):
            response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertEqual(response.status_code, 429)

    def test_og_meta_tags_point_at_the_same_origin_logo_endpoint(self):
        # og:image no longer embeds a third-party logo URL directly - it
        # points at public_item_share_logo, which resolves the cached
        # logo (or a generated avatar fallback) server-side. See
        # PublicItemShareLogoViewTests for coverage of that endpoint
        # itself.
        item = make_item(self.alice, issuer='Ticketmaster')
        MerchantProfile.objects.create(name='Ticketmaster', logo_url='https://example.com/tm-logo.png')
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()

        response = self.client.get(reverse('public_item_share', args=[share.id]))
        logo_url = reverse('public_item_share_logo', args=[share.id])
        self.assertContains(response, f'og:image" content="http://testserver{logo_url}"')
        self.assertContains(response, 'og:title')


class TriggerUpdateCheckViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_requires_login(self):
        response = self.client.post(reverse('trigger_update_check'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_requires_post(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('trigger_update_check'))
        self.assertEqual(response.status_code, 405)

    @patch('myapp.views.check_for_update')
    def test_regular_user_forbidden_and_check_not_called(self, mock_check):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_update_check'), follow=True)
        mock_check.assert_not_called()
        self.assertContains(response, 'Only administrators')

    @patch('myapp.views.check_for_update')
    def test_superuser_triggers_check_and_reports_up_to_date(self, mock_check):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={'update_available': False})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_update_check'), follow=True)
        mock_check.assert_called_once()
        self.assertContains(response, 'on the latest version')

    @patch('myapp.views.check_for_update')
    def test_superuser_reports_update_available(self, mock_check):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={'update_available': True, 'latest_version': 'v1.9.0'})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_update_check'), follow=True)
        mock_check.assert_called_once()
        self.assertContains(response, 'Update available: v1.9.0')

    @override_settings(VERSION='v1.2.3')
    @patch('myapp.views.check_for_update')
    def test_ajax_returns_json_with_version_and_release_link(self, mock_check):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={
            'update_available': True, 'latest_version': 'v1.9.0',
            'latest_release_url': 'https://github.com/gregbtm/VoucherVault/releases/tag/v1.9.0',
        })
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_update_check'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['installed_version'], 'v1.2.3')
        self.assertEqual(payload['latest_version'], 'v1.9.0')
        self.assertEqual(payload['latest_release_url'], 'https://github.com/gregbtm/VoucherVault/releases/tag/v1.9.0')
        self.assertTrue(payload['update_available'])

    @patch('myapp.views.check_for_update')
    def test_ajax_regular_user_forbidden_returns_json(self, mock_check):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_update_check'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)
        mock_check.assert_not_called()


class TriggerUpstreamCheckViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_requires_login(self):
        response = self.client.post(reverse('trigger_upstream_check'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_requires_post(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('trigger_upstream_check'))
        self.assertEqual(response.status_code, 405)

    @patch('myapp.views.check_upstream_version')
    def test_regular_user_forbidden_and_check_not_called(self, mock_check):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_upstream_check'), follow=True)
        mock_check.assert_not_called()
        self.assertContains(response, 'Only administrators')

    @patch('myapp.views.check_upstream_version')
    def test_superuser_triggers_check_and_reports_version(self, mock_check):
        UpstreamSyncStatus.objects.update_or_create(pk=1, defaults={'latest_version': 'v1.29.0'})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_upstream_check'), follow=True)
        mock_check.assert_called_once()
        self.assertContains(response, 'v1.29.0')

    @patch('myapp.views.check_upstream_version')
    def test_superuser_reports_unreachable_error(self, mock_check):
        UpstreamSyncStatus.objects.update_or_create(pk=1, defaults={'last_check_error': 'timed out'})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_upstream_check'), follow=True)
        mock_check.assert_called_once()
        self.assertContains(response, 'timed out')

    @override_settings(UPSTREAM_VERSION='1.28.0')
    @patch('myapp.views.check_upstream_version')
    def test_ajax_returns_json_with_version_and_upstream_behind(self, mock_check):
        UpstreamSyncStatus.objects.update_or_create(pk=1, defaults={
            'latest_version': 'v1.29.0',
            'latest_release_url': 'https://github.com/l4rm4nd/VoucherVault/releases/tag/v1.29.0',
        })
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_upstream_check'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['latest_version'], 'v1.29.0')
        self.assertEqual(payload['latest_release_url'], 'https://github.com/l4rm4nd/VoucherVault/releases/tag/v1.29.0')
        self.assertTrue(payload['upstream_behind'])

    @patch('myapp.views.check_upstream_version')
    def test_ajax_regular_user_forbidden_returns_json(self, mock_check):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_upstream_check'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)
        mock_check.assert_not_called()


class PublicShareWalletTests(TestCase):
    """
    The public share page's "Add to Apple/Google Wallet" buttons - only
    shown when that export method is configured server-side, and (for
    Apple Wallet's binary download) only servable to a visitor who has
    already passed public_item_share's own crawler/expiry/PIN checks.
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_apple_wallet_button_present_when_configured(self, mock_enabled):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertContains(response, 'public-apple-wallet-btn')

    def test_apple_wallet_button_absent_when_not_configured(self):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertNotContains(response, 'public-apple-wallet-btn')

    @patch('myapp.views.generate_google_wallet_save_url', return_value='https://pay.google.com/gp/v/save/xyz')
    @patch('myapp.views.google_wallet_enabled', return_value=True)
    def test_google_wallet_button_present_when_configured(self, mock_enabled, mock_generate):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_share', args=[share.id]))
        self.assertContains(response, 'https://pay.google.com/gp/v/save/xyz')

    @patch('myapp.views.generate_pkpass', return_value=b'PKPASSDATA')
    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_pkpass_download_succeeds_when_unlocked(self, mock_enabled, mock_generate):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_pkpass', args=[share.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'PKPASSDATA')

    def test_pkpass_download_404s_when_not_configured(self):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_pkpass', args=[share.id]))
        self.assertEqual(response.status_code, 404)

    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_pkpass_download_blocked_when_pin_not_unlocked(self, mock_enabled):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice, access_pin='1234')
        self.client.logout()
        response = self.client.get(reverse('public_item_pkpass', args=[share.id]))
        self.assertEqual(response.status_code, 403)

    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_pkpass_download_blocked_for_expired_link(self, mock_enabled):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(
            item=item, created_by=self.alice, expires_at=timezone.now() - timedelta(days=1),
        )
        self.client.logout()
        response = self.client.get(reverse('public_item_pkpass', args=[share.id]))
        self.assertEqual(response.status_code, 403)

    @patch('myapp.views.pkpass_enabled', return_value=True)
    def test_pkpass_download_blocked_for_crawler_ua(self, mock_enabled):
        item = make_item(self.alice)
        share = ItemPublicShare.objects.create(item=item, created_by=self.alice)
        self.client.logout()
        response = self.client.get(reverse('public_item_pkpass', args=[share.id]), HTTP_USER_AGENT='WhatsApp/2.23.1')
        self.assertEqual(response.status_code, 403)


class CodeTypeDefaultTests(TestCase):
    """
    A brand-new item defaults its barcode-type dropdown to "No Barcode"
    rather than the model's own "qrcode" default - most items haven't had
    anything scanned yet, and a real scan/import overwrites this field for
    you (scanner.js::applyDetectedFormat). Editing or duplicating an
    existing item must still show its real code_type, unaffected.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_new_item_form_defaults_code_type_to_none(self):
        response = self.client.get(reverse('create_item'))
        self.assertContains(response, '<option value="none" selected>No Barcode (number only)</option>')

    def test_duplicate_item_preserves_original_code_type(self):
        item = make_item(self.user, code_type='qrcode')
        response = self.client.get(reverse('duplicate_item', args=[item.id]))
        self.assertContains(response, '<option value="qrcode" selected>QR Code</option>')

    def test_edit_item_preserves_existing_code_type(self):
        item = make_item(self.user, code_type='ean13')
        response = self.client.get(reverse('edit_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, '<option value="ean13" selected>EAN-13</option>')


class CardNumberDisplayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_card_number_shown_when_set(self):
        item = make_item(self.user, card_number='MEMBER-123')
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'MEMBER-123')
        self.assertContains(response, 'id="card-number"')

    def test_card_number_block_hidden_when_blank(self):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertNotContains(response, 'id="card-number"')

    def test_duplicate_item_carries_card_number(self):
        item = make_item(self.user, card_number='MEMBER-123')
        response = self.client.get(reverse('duplicate_item', args=[item.id]))
        self.assertEqual(response.context['form'].initial['card_number'], 'MEMBER-123')


class ServeImageFileTests(TestCase):
    """
    Regression test for a latent bug found while adding an unrelated
    Http404 usage elsewhere: this view's "no file attached" branch raised
    a bare Http404 with no import for it anywhere in views.py, which would
    have crashed with NameError instead of returning a 404 the one time
    this branch actually ran.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_returns_404_instead_of_crashing_when_no_file_attached(self):
        item = make_item(self.user)
        response = self.client.get(reverse('serve_image_file', args=[item.id]))
        self.assertEqual(response.status_code, 404)


class ViewOriginalFileTests(TestCase):
    """
    view_original_file backs the "View" overlay button on an item's
    original upload - unlike download_file, it serves inline (no
    attachment header) so an <img> or <iframe> can render it directly, and
    it accepts a PDF as well as an image.
    """
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_serves_image_inline_with_correct_content_type(self):
        item = make_item(self.alice, file=SimpleUploadedFile('scan.png', _make_test_image('blue'), content_type='image/png'))
        response = self.client.get(reverse('view_original_file', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertNotIn('Content-Disposition', response)

    def test_serves_pdf_inline_with_correct_content_type(self):
        item = make_item(self.alice, file=make_upload('ticket.pdf'))
        response = self.client.get(reverse('view_original_file', args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertNotIn('Content-Disposition', response)

    def test_rejects_non_previewable_file_type(self):
        item = make_item(self.alice, file=SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain'))
        response = self.client.get(reverse('view_original_file', args=[item.id]))
        self.assertEqual(response.status_code, 400)

    def test_non_collaborator_forbidden(self):
        item = make_item(self.alice, file=make_upload('ticket.pdf'))
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('view_original_file', args=[item.id]))
        self.assertEqual(response.status_code, 403)


class ArchivedItemTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_toggle_archive(self):
        item = make_item(self.alice)
        response = self.client.post(reverse('toggle_archive_item', args=[item.id]))
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': item.id}))
        item.refresh_from_db()
        self.assertTrue(item.is_archived)

        self.client.post(reverse('toggle_archive_item', args=[item.id]))
        item.refresh_from_db()
        self.assertFalse(item.is_archived)

    def test_non_collaborator_cannot_archive(self):
        item = make_item(self.alice)
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')
        response = self.client.post(reverse('toggle_archive_item', args=[item.id]))
        self.assertEqual(response.status_code, 403)

    def test_archived_items_excluded_from_default_and_available_views(self):
        archived = make_item(self.alice, name='Archived One', is_archived=True)
        visible = make_item(self.alice, name='Visible One', redeem_code='OTHER')

        response = self.client.get(reverse('show_items'), {'status': 'all'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertIn(visible.name, names)
        self.assertNotIn(archived.name, names)

        response = self.client.get(reverse('show_items'), {'status': 'available'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertNotIn(archived.name, names)

    def test_archived_filter_shows_only_archived_items(self):
        archived = make_item(self.alice, name='Archived One', is_archived=True)
        visible = make_item(self.alice, name='Visible One', redeem_code='OTHER')

        response = self.client.get(reverse('show_items'), {'status': 'archived'})
        names = [entry['item'].name for entry in response.context['items_with_qr']]
        self.assertIn(archived.name, names)
        self.assertNotIn(visible.name, names)
        self.assertEqual(response.context['archived_count'], 1)


class WebhookEventWiringTests(TestCase):
    """
    Confirms the web UI actually fires the Phase 12.2 lifecycle events at
    the right transitions — the events themselves are unit-tested in
    notify/tests.py, this just checks each view calls the right one.
    """

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    @patch('myapp.views.notify_item_created')
    def test_create_item_fires_item_created(self, mock_notify):
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'New Voucher', 'issuer': 'Shop', 'redeem_code': 'NEW100',
            'value': '10.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn('/items/view/', response['Location'])
        self.assertIn('?new=1', response['Location'])
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][0].name, 'New Voucher')

    @patch('myapp.views.notify_item_used')
    def test_toggle_item_status_fires_item_used_only_when_marking_used(self, mock_notify):
        item = make_item(self.alice)
        self.client.post(reverse('toggle_item_status', args=[item.id]))
        mock_notify.assert_called_once()

        mock_notify.reset_mock()
        self.client.post(reverse('toggle_item_status', args=[item.id]))  # toggle back to available
        mock_notify.assert_not_called()

    @patch('myapp.views.notify_item_archived')
    def test_toggle_archive_fires_only_when_archiving(self, mock_notify):
        item = make_item(self.alice)
        self.client.post(reverse('toggle_archive_item', args=[item.id]))  # archive
        mock_notify.assert_called_once()

        mock_notify.reset_mock()
        self.client.post(reverse('toggle_archive_item', args=[item.id]))  # unarchive
        mock_notify.assert_not_called()

    @patch('myapp.views.notify_balance_changed')
    def test_adding_transaction_fires_balance_changed(self, mock_notify):
        item = make_item(self.alice, type='giftcard', value='20.00')
        self.client.post(reverse('view_item', kwargs={'item_uuid': item.id}), {
            'description': 'Coffee', 'value': '-5.00',
        })
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][0], item)

    @patch('myapp.views.notify_item_shared')
    def test_sharing_item_fires_item_shared(self, mock_notify):
        item = make_item(self.alice)
        self.client.post(reverse('share_item', args=[item.id]), {'shared_users': [self.bob.id]})
        mock_notify.assert_called_once_with(item, 'bob')

    @patch('myapp.views.notify_item_shared')
    def test_resharing_already_shared_item_does_not_refire(self, mock_notify):
        item = make_item(self.alice)
        self.client.post(reverse('share_item', args=[item.id]), {'shared_users': [self.bob.id]})
        mock_notify.reset_mock()
        self.client.post(reverse('share_item', args=[item.id]), {'shared_users': [self.bob.id]})
        mock_notify.assert_not_called()


class GoogleWalletUpdateWiringTests(TestCase):
    """
    Confirms every place an item's balance/expiry/name/used/archived state
    can change also queues a Google Wallet push (see
    _queue_google_wallet_update) - mirrors WebhookEventWiringTests' pattern
    of checking wiring, not the update logic itself (that's covered in
    imports/tests.py::GoogleWalletExporterTests).
    """

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    @patch('myapp.views._queue_google_wallet_update')
    def test_toggle_item_status_queues_update_both_directions(self, mock_queue):
        item = make_item(self.alice)
        self.client.post(reverse('toggle_item_status', args=[item.id]))
        mock_queue.assert_called_once()

        mock_queue.reset_mock()
        self.client.post(reverse('toggle_item_status', args=[item.id]))
        mock_queue.assert_called_once()

    @patch('myapp.views._queue_google_wallet_update')
    def test_toggle_archive_queues_update_both_directions(self, mock_queue):
        item = make_item(self.alice)
        self.client.post(reverse('toggle_archive_item', args=[item.id]))
        mock_queue.assert_called_once()

        mock_queue.reset_mock()
        self.client.post(reverse('toggle_archive_item', args=[item.id]))
        mock_queue.assert_called_once()

    @patch('myapp.views._queue_google_wallet_update')
    def test_adding_transaction_queues_update(self, mock_queue):
        item = make_item(self.alice, type='giftcard', value='20.00')
        self.client.post(reverse('view_item', kwargs={'item_uuid': item.id}), {
            'description': 'Coffee', 'value': '-5.00',
        })
        mock_queue.assert_called_once_with(item)

    @patch('myapp.views._queue_google_wallet_update')
    def test_editing_item_queues_update(self, mock_queue):
        item = make_item(self.alice, type='giftcard', name='Old Name', value='20.00')
        self.client.post(reverse('edit_item', args=[item.id]), {
            'type': 'giftcard', 'name': 'New Name', 'issuer': item.issuer or '', 'redeem_code': item.redeem_code,
            'value': '20.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        mock_queue.assert_called_once()


class BalanceCheckUrlWiringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def _giftcard_payload(self, **overrides):
        payload = {
            'type': 'giftcard', 'name': 'Tesco Gift Card', 'issuer': 'Tesco', 'redeem_code': 'GC100',
            'value': '25.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_create_item_remembers_balance_check_url(self):
        self.client.post(reverse('create_item'), self._giftcard_payload(
            balance_check_url='https://www.tesco.com/gift-cards/balance'
        ))
        item = Item.objects.get(name='Tesco Gift Card')
        self.assertEqual(item.balance_check_url, 'https://www.tesco.com/gift-cards/balance')
        self.assertEqual(
            MerchantProfile.objects.get(name__iexact='Tesco').balance_check_url,
            'https://www.tesco.com/gift-cards/balance',
        )

    def test_create_item_without_balance_check_url_does_not_touch_merchant_profile(self):
        self.client.post(reverse('create_item'), self._giftcard_payload(redeem_code='GC200'))
        self.assertFalse(MerchantProfile.objects.filter(name__iexact='Tesco').exists())

    def test_lookup_merchant_balance_url_returns_remembered_link(self):
        MerchantProfile.objects.create(name='Tesco', balance_check_url='https://www.tesco.com/gift-cards/balance')
        response = self.client.get(reverse('lookup_merchant_balance_url'), {'issuer': 'tesco'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'balance_check_url': 'https://www.tesco.com/gift-cards/balance'})

    def test_lookup_merchant_balance_url_unknown_issuer(self):
        response = self.client.get(reverse('lookup_merchant_balance_url'), {'issuer': 'Never Seen Co'})
        self.assertEqual(response.json(), {'balance_check_url': ''})

    def test_edit_item_updates_remembered_balance_check_url(self):
        item = make_item(self.user, type='giftcard', name='Amazon Card')
        response = self.client.post(reverse('edit_item', kwargs={'item_uuid': item.id}), {
            'type': 'giftcard', 'name': 'Amazon Card', 'issuer': 'Amazon', 'redeem_code': item.redeem_code,
            'value': '10.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
            'balance_check_url': 'https://www.amazon.co.uk/gc/balance',
        })
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': item.id}))
        item.refresh_from_db()
        self.assertEqual(item.balance_check_url, 'https://www.amazon.co.uk/gc/balance')
        self.assertEqual(
            MerchantProfile.objects.get(name__iexact='Amazon').balance_check_url,
            'https://www.amazon.co.uk/gc/balance',
        )

    def test_view_item_shows_check_balance_button_only_when_set(self):
        with_link = make_item(self.user, type='giftcard', name='Has Link', redeem_code='L1', balance_check_url='https://example.com/balance')
        without_link = make_item(self.user, type='giftcard', name='No Link', redeem_code='L2')

        response = self.client.get(reverse('view_item', kwargs={'item_uuid': with_link.id}))
        self.assertContains(response, 'Check Balance')
        self.assertContains(response, 'https://example.com/balance')

        response = self.client.get(reverse('view_item', kwargs={'item_uuid': without_link.id}))
        self.assertNotContains(response, 'Check Balance')


class CheckDuplicateCodeTests(TestCase):
    """
    myapp.views.check_duplicate_code - the create/edit item forms' warn-
    not-block duplicate-code nudge.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'ABC123'})
        self.assertEqual(response.status_code, 302)

    def test_blank_code_returns_not_duplicate(self):
        response = self.client.get(reverse('check_duplicate_code'), {'code': ''})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_no_match_returns_not_duplicate(self):
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'NOTHING'})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_matches_own_active_item(self):
        item = make_item(self.user, name='Existing Card', redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        payload = response.json()
        self.assertTrue(payload['duplicate'])
        self.assertEqual(payload['item_name'], 'Existing Card')
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_matches_regardless_of_case(self):
        # A code typed by hand and one OCR-scanned off the same physical
        # card can come back with different casing despite being identical.
        make_item(self.user, redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'dup123'})
        self.assertTrue(response.json()['duplicate'])

    def test_matches_despite_stray_whitespace_on_the_stored_code(self):
        make_item(self.user, redeem_code=' DUP123 ')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        self.assertTrue(response.json()['duplicate'])

    def test_ignores_used_items(self):
        make_item(self.user, redeem_code='DUP123', is_used=True)
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_ignores_archived_items(self):
        make_item(self.user, redeem_code='DUP123', is_archived=True)
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_ignores_other_users_items(self):
        make_item(self.other_user, redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_exclude_param_omits_the_item_being_edited(self):
        item = make_item(self.user, redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123', 'exclude': str(item.id)})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_matches_item_in_a_wallet_shared_with_the_user(self):
        wallet = Wallet.objects.create(user=self.other_user, name='Shared Wallet')
        wallet.shared_with.add(self.user)
        item = make_item(self.other_user, name='Shared Card', redeem_code='DUP123', wallet=wallet)
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        payload = response.json()
        self.assertTrue(payload['duplicate'])
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_near_duplicate_flags_a_one_character_difference(self):
        # The exact failure mode this exists for: two scans of the same
        # physical card, one character misread by OCR each time.
        item = make_item(self.user, name='Existing Card', redeem_code='NABWRSZYCP8J8US')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'NABWRSZYCPSJ8US'})
        payload = response.json()
        self.assertFalse(payload['duplicate'])
        self.assertTrue(payload['near_duplicate'])
        self.assertEqual(payload['item_name'], 'Existing Card')
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_near_duplicate_does_not_fire_for_very_different_codes(self):
        make_item(self.user, redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'TOTALLYDIFFERENTCODE'})
        payload = response.json()
        self.assertFalse(payload['duplicate'])
        self.assertNotIn('near_duplicate', payload)

    def test_near_duplicate_prefers_exact_match_when_both_exist(self):
        exact = make_item(self.user, name='Exact', redeem_code='DUP123')
        make_item(self.user, name='Close', redeem_code='DUP124')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP123'})
        payload = response.json()
        self.assertTrue(payload['duplicate'])
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': exact.id}))

    def test_near_duplicate_respects_exclude_param(self):
        item = make_item(self.user, redeem_code='DUP123')
        response = self.client.get(reverse('check_duplicate_code'), {'code': 'DUP124', 'exclude': str(item.id)})
        payload = response.json()
        self.assertFalse(payload['duplicate'])
        self.assertNotIn('near_duplicate', payload)


_TEST_IMAGE_PALETTES = {
    'red': [(200, 30, 30), (30, 30, 200), (30, 200, 30), (200, 200, 30)],
    'blue': [(30, 30, 200), (200, 30, 30), (200, 200, 30), (30, 200, 30)],
    'green': [(30, 200, 30), (200, 200, 30), (30, 30, 200), (200, 30, 30)],
    'purple': [(120, 30, 160), (30, 160, 120), (200, 200, 200), (60, 60, 60)],
    'orange': [(220, 130, 20), (20, 130, 220), (20, 220, 130), (130, 20, 220)],
}


def _make_test_image(variant, size=(64, 64), fmt='PNG'):
    """
    A quadrant-patterned test image, not a flat color - a dHash on a
    perfectly flat-color image is degenerate (every pixel equals its
    neighbour, so the hash is always all-zero regardless of *which*
    color), which would make "different photo" tests pass for the wrong
    reason. Each named variant maps to a distinct 4-quadrant colour
    pattern so genuinely different variants produce genuinely different
    hashes, while re-encoding the same variant (PNG vs JPEG) stays close.
    """
    palette = _TEST_IMAGE_PALETTES[variant]
    width, height = size
    half_w, half_h = width // 2, height // 2
    image = Image.new('RGB', size)
    for i, box in enumerate([
        (0, 0, half_w, half_h), (half_w, 0, width, half_h),
        (0, half_h, half_w, height), (half_w, half_h, width, height),
    ]):
        quadrant = Image.new('RGB', (box[2] - box[0], box[3] - box[1]), color=palette[i])
        image.paste(quadrant, (box[0], box[1]))
    buffer = BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class LevenshteinDistanceTests(TestCase):
    """myapp.utils.levenshtein_distance - the fuzzy-match core of the near-duplicate check."""

    def test_identical_strings_have_zero_distance(self):
        self.assertEqual(levenshtein_distance('ABC123', 'ABC123'), 0)

    def test_single_substitution(self):
        self.assertEqual(levenshtein_distance('ABC123', 'ABC124'), 1)

    def test_single_insertion(self):
        self.assertEqual(levenshtein_distance('ABC123', 'ABC1234'), 1)

    def test_single_deletion(self):
        self.assertEqual(levenshtein_distance('ABC1234', 'ABC123'), 1)

    def test_empty_string_distance_equals_other_length(self):
        self.assertEqual(levenshtein_distance('', 'ABC'), 3)
        self.assertEqual(levenshtein_distance('ABC', ''), 3)

    def test_completely_different_strings(self):
        self.assertEqual(levenshtein_distance('ABC', 'XYZ'), 3)


class ImageHashTests(TestCase):
    """
    myapp.imagehash - the perceptual-hash core of duplicate-photo detection.
    Threshold checks below use the SiteConfiguration.duplicate_photo_threshold
    model default (not a fixed import from imagehash.py, which no longer
    hardcodes one - see SiteConfiguration and check_duplicate_image) - this
    is testing the hash algorithm's own behaviour against a realistic
    threshold, not the settings wiring itself.
    """
    DEFAULT_THRESHOLD = 10

    def test_identical_images_have_zero_distance(self):
        image_bytes = _make_test_image('red')
        self.assertEqual(
            hamming_distance(compute_dhash(image_bytes), compute_dhash(image_bytes)), 0,
        )

    def test_same_image_reencoded_as_jpeg_stays_within_threshold(self):
        # Re-compression/format change is exactly the kind of incidental
        # difference two "same photo" uploads can have.
        png_bytes = _make_test_image('red', fmt='PNG')
        jpeg_bytes = _make_test_image('red', fmt='JPEG')
        distance = hamming_distance(compute_dhash(png_bytes), compute_dhash(jpeg_bytes))
        self.assertLessEqual(distance, self.DEFAULT_THRESHOLD)

    def test_visually_different_images_exceed_threshold(self):
        distance = hamming_distance(compute_dhash(_make_test_image('red')), compute_dhash(_make_test_image('orange')))
        self.assertGreater(distance, self.DEFAULT_THRESHOLD)

    def test_compute_dhash_returns_empty_string_for_invalid_bytes(self):
        self.assertEqual(compute_dhash(b'not an image'), '')

    def test_hamming_distance_treats_missing_hash_as_maximally_distant(self):
        real_hash = compute_dhash(_make_test_image('red'))
        self.assertGreater(hamming_distance(real_hash, ''), self.DEFAULT_THRESHOLD)
        self.assertGreater(hamming_distance('', ''), self.DEFAULT_THRESHOLD)


class ItemImagePhashSaveTests(TestCase):
    """Item.save() computes image_phash automatically for a newly-assigned file."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_new_file_gets_a_hash_on_create(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('blue'), content_type='image/png')
        item = make_item(self.user, file=upload)
        self.assertTrue(item.image_phash)
        self.assertEqual(len(item.image_phash), 16)

    def test_item_without_a_file_has_no_hash(self):
        item = make_item(self.user)
        self.assertEqual(item.image_phash, '')

    def test_resaving_without_touching_the_file_keeps_the_same_hash(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('green'), content_type='image/png')
        item = make_item(self.user, file=upload)
        original_hash = item.image_phash
        item.name = 'Renamed'
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.image_phash, original_hash)


class CheckDuplicateImageTests(TestCase):
    """myapp.views.check_duplicate_image - the photo-based companion to check_duplicate_code."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def _post_image(self, color, **params):
        upload = SimpleUploadedFile('scan.png', _make_test_image(color), content_type='image/png')
        url = reverse('check_duplicate_image')
        if params:
            url += '?' + '&'.join(f'{k}={v}' for k, v in params.items())
        return self.client.post(url, {'image': upload})

    def test_no_image_returns_not_duplicate(self):
        response = self.client.post(reverse('check_duplicate_image'), {})
        self.assertEqual(response.json(), {'duplicate': False})

    def test_matches_the_same_photo_already_attached_to_an_active_item(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        item = make_item(self.user, name='Existing Card', file=upload)
        response = self._post_image('purple')
        payload = response.json()
        self.assertTrue(payload['duplicate'])
        self.assertEqual(payload['item_name'], 'Existing Card')
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_does_not_match_a_visually_different_photo(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        make_item(self.user, file=upload)
        response = self._post_image('orange')
        self.assertEqual(response.json(), {'duplicate': False})

    def test_ignores_used_items(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        make_item(self.user, file=upload, is_used=True)
        response = self._post_image('purple')
        self.assertEqual(response.json(), {'duplicate': False})

    def test_ignores_archived_items(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        make_item(self.user, file=upload, is_archived=True)
        response = self._post_image('purple')
        self.assertEqual(response.json(), {'duplicate': False})

    def test_ignores_other_users_items(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        make_item(self.other_user, file=upload)
        response = self._post_image('purple')
        self.assertEqual(response.json(), {'duplicate': False})

    def test_exclude_param_omits_the_item_being_edited(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        item = make_item(self.user, file=upload)
        response = self._post_image('purple', exclude=str(item.id))
        self.assertEqual(response.json(), {'duplicate': False})

    def test_matches_item_in_a_wallet_shared_with_the_user(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        wallet = Wallet.objects.create(user=self.other_user, name='Shared Wallet')
        wallet.shared_with.add(self.user)
        item = make_item(self.other_user, name='Shared Card', file=upload, wallet=wallet)
        response = self._post_image('purple')
        payload = response.json()
        self.assertTrue(payload['duplicate'])
        self.assertEqual(payload['item_url'], reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_lazily_backfills_a_missing_hash_on_an_existing_item(self):
        upload = SimpleUploadedFile('card.png', _make_test_image('purple'), content_type='image/png')
        item = make_item(self.user, file=upload)
        # Simulate an item saved before this feature shipped: file present,
        # but no hash computed yet.
        Item.objects.filter(pk=item.pk).update(image_phash='')

        response = self._post_image('purple')
        self.assertTrue(response.json()['duplicate'])
        item.refresh_from_db()
        self.assertTrue(item.image_phash)


class BarcodeDecodeViewTests(TestCase):
    """myapp.views.barcode_decode - server-side zxing-cpp fallback endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.url = reverse('barcode_decode')

    @staticmethod
    def _make_barcode_image(code='HELLO123', symbology='code128'):
        import treepoem
        img = treepoem.generate_barcode(symbology, code)
        buf = BytesIO()
        img.convert('RGB').save(buf, 'PNG')
        return buf.getvalue()

    def test_requires_login(self):
        self.client.logout()
        upload = SimpleUploadedFile('bc.png', self._make_barcode_image(), content_type='image/png')
        response = self.client.post(self.url, {'image': upload})
        self.assertNotEqual(response.status_code, 200)

    def test_requires_post(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_no_image_returns_null(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['code'])
        self.assertIsNone(response.json()['code_type'])

    def test_decodes_code128_barcode(self):
        upload = SimpleUploadedFile('bc.png', self._make_barcode_image('HELLO123', 'code128'), content_type='image/png')
        response = self.client.post(self.url, {'image': upload})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['code'], 'HELLO123')
        self.assertEqual(payload['code_type'], 'code128')

    def test_decodes_qr_code(self):
        upload = SimpleUploadedFile('qr.png', self._make_barcode_image('https://example.com', 'qrcode'), content_type='image/png')
        response = self.client.post(self.url, {'image': upload})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['code'], 'https://example.com')
        self.assertEqual(payload['code_type'], 'qrcode')

    def test_non_barcode_image_returns_null(self):
        upload = SimpleUploadedFile('plain.png', _make_test_image('purple'), content_type='image/png')
        response = self.client.post(self.url, {'image': upload})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload['code'])
        self.assertIsNone(payload['code_type'])


class LastUsedTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_viewing_item_updates_last_used_at(self):
        item = make_item(self.user)
        self.assertIsNone(item.last_used_at)
        self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        item.refresh_from_db()
        self.assertIsNotNone(item.last_used_at)

    def test_last_used_at_is_a_valid_sort_option(self):
        self.assertIn('last_used_at', dict(UserPreference.SORT_CHOICES))


class WakeLockPreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_wake_lock_script_present_by_default(self):
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertContains(response, 'navigator.wakeLock')

    def test_wake_lock_script_absent_when_disabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.keep_screen_awake = False
        prefs.save()
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))
        self.assertNotContains(response, 'navigator.wakeLock')


class GbpMigrationDataTests(TestCase):
    """
    Exercises the 0038_convert_existing_items_to_gbp RunPython function
    directly (Django's test DB already has all migrations applied before
    any test data exists, so the migration itself can't be re-triggered
    against pre-existing rows through the normal test runner).
    """
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _run_migration(self):
        import importlib
        module = importlib.import_module('myapp.migrations.0038_convert_existing_items_to_gbp')
        from django.apps import apps as real_apps
        module.relabel_currency_to_gbp(real_apps, None)

    def test_relabels_eur_item_to_gbp_keeping_value(self):
        item = make_item(self.user, currency='EUR', value='5.00')
        self._run_migration()
        item.refresh_from_db()
        self.assertEqual(item.currency, 'GBP')
        self.assertEqual(str(item.value), '5.00')

    def test_relabels_any_currency_to_gbp(self):
        item = make_item(self.user, currency='USD', value='12.34')
        self._run_migration()
        item.refresh_from_db()
        self.assertEqual(item.currency, 'GBP')
        self.assertEqual(str(item.value), '12.34')

    def test_relabels_user_preference_default_currency(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.default_currency = 'EUR'
        prefs.save()
        self._run_migration()
        prefs.refresh_from_db()
        self.assertEqual(prefs.default_currency, 'GBP')

    def test_already_gbp_items_untouched(self):
        item = make_item(self.user, currency='GBP', value='7.50')
        self._run_migration()
        item.refresh_from_db()
        self.assertEqual(item.currency, 'GBP')
        self.assertEqual(str(item.value), '7.50')


class VersionCompareTests(TestCase):
    def test_parse_version_strips_v_prefix(self):
        self.assertEqual(_parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(_parse_version('1.2.3'), (1, 2, 3))

    def test_parse_version_non_numeric_segments_become_zero(self):
        self.assertEqual(_parse_version('1.2.3-beta'), (1, 2, 3))

    def test_is_newer_true_when_latest_greater(self):
        self.assertTrue(_is_newer('v1.1.0', '1.0.0'))

    def test_is_newer_false_when_equal_or_older(self):
        self.assertFalse(_is_newer('v1.0.0', '1.0.0'))
        self.assertFalse(_is_newer('v0.9.0', '1.0.0'))

    def test_is_newer_false_when_current_unknown(self):
        self.assertFalse(_is_newer('v1.0.0', 'unknown'))

    def test_is_newer_false_when_either_empty(self):
        self.assertFalse(_is_newer('', '1.0.0'))
        self.assertFalse(_is_newer('v1.0.0', ''))


class UpdateCheckServiceTests(TestCase):
    @patch('myapp.update_check.requests.get')
    def test_disabled_makes_no_request(self, mock_get):
        set_site_config(update_check_enabled=False)
        check_for_update()
        mock_get.assert_not_called()

    @override_settings(VERSION='1.0.0')
    @patch('myapp.update_check.requests.get')
    def test_records_update_available_when_newer_release_exists(self, mock_get):
        set_site_config(update_check_enabled=True)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'tag_name': 'v1.1.0', 'html_url': 'https://github.com/gregbtm/VoucherVault/releases/tag/v1.1.0'},
        )
        check_for_update()
        status = UpdateCheckStatus.load()
        self.assertTrue(status.update_available)
        self.assertEqual(status.latest_version, 'v1.1.0')
        self.assertIsNotNone(status.checked_at)

    @override_settings(VERSION='1.0.0')
    @patch('myapp.update_check.requests.get')
    def test_records_up_to_date_when_no_newer_release(self, mock_get):
        set_site_config(update_check_enabled=True)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'tag_name': 'v1.0.0', 'html_url': 'https://example.com'},
        )
        check_for_update()
        self.assertFalse(UpdateCheckStatus.load().update_available)

    @override_settings(VERSION='1.0.0')
    @patch('myapp.update_check.requests.get')
    def test_request_failure_leaves_previous_result_untouched(self, mock_get):
        set_site_config(update_check_enabled=True)
        import requests
        UpdateCheckStatus.objects.create(pk=1, latest_version='v1.1.0', update_available=True)
        mock_get.side_effect = requests.RequestException('boom')
        check_for_update()
        self.assertTrue(UpdateCheckStatus.load().update_available)

    @override_settings(VERSION='1.0.0')
    @patch('myapp.update_check.requests.get')
    def test_request_failure_records_error_and_checked_at(self, mock_get):
        set_site_config(update_check_enabled=True)
        import requests
        mock_get.side_effect = requests.RequestException('boom')
        check_for_update()
        status = UpdateCheckStatus.load()
        self.assertEqual(status.last_check_error, 'boom')
        self.assertIsNotNone(status.checked_at)

    @override_settings(VERSION='1.0.0')
    @patch('myapp.update_check.requests.get')
    def test_success_clears_previous_error(self, mock_get):
        set_site_config(update_check_enabled=True)
        UpdateCheckStatus.objects.create(pk=1, last_check_error='old error')
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'tag_name': 'v1.0.0', 'html_url': 'https://example.com'},
        )
        check_for_update()
        self.assertEqual(UpdateCheckStatus.load().last_check_error, '')

    @patch('myapp.tasks.check_for_update')
    def test_task_delegates_to_service(self, mock_check):
        set_site_config(update_check_enabled=True)
        check_for_update_task()
        mock_check.assert_called_once()


class UpstreamVersionCheckTests(TestCase):
    """
    check_upstream_version() checks l4rm4nd/VoucherVault's (upstream)
    latest release, independent of UPDATE_CHECK_ENABLED (that flag is
    specifically for this fork's own releases) and independent of any
    "update available" banner - it's purely informational.
    """

    @patch('myapp.update_check.requests.get')
    def test_records_latest_upstream_release(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'tag_name': 'v1.30.0',
                'html_url': 'https://github.com/l4rm4nd/VoucherVault/releases/tag/v1.30.0',
                'published_at': '2026-07-01T00:00:00Z',
            },
        )
        check_upstream_version()
        status = UpstreamSyncStatus.load()
        self.assertEqual(status.latest_version, 'v1.30.0')
        self.assertEqual(status.upstream_repo, 'l4rm4nd/VoucherVault')
        self.assertIsNotNone(status.latest_release_published_at)
        self.assertIsNotNone(status.checked_at)
        self.assertEqual(status.last_check_error, '')

    @patch('myapp.update_check.requests.get')
    def test_runs_regardless_of_update_check_enabled(self, mock_get):
        # Deliberately does NOT gate on SiteConfiguration.update_check_enabled -
        # that flag controls this fork's own release-banner feature, not
        # this purely informational upstream check.
        set_site_config(update_check_enabled=False)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'tag_name': 'v1.30.0', 'html_url': 'https://example.com', 'published_at': None},
        )
        check_upstream_version()
        mock_get.assert_called_once()

    @patch('myapp.update_check.requests.get')
    def test_request_failure_records_error_and_leaves_previous_version(self, mock_get):
        import requests
        UpstreamSyncStatus.objects.create(pk=1, latest_version='v1.29.0')
        mock_get.side_effect = requests.RequestException('boom')
        check_upstream_version()
        status = UpstreamSyncStatus.load()
        self.assertEqual(status.last_check_error, 'boom')
        self.assertEqual(status.latest_version, 'v1.29.0')
        self.assertIsNotNone(status.checked_at)

    @patch('myapp.tasks.check_upstream_version')
    def test_task_delegates_to_service(self, mock_check):
        check_upstream_version_task()
        mock_check.assert_called_once()


@override_settings(VERSION='1.0.0')
class UpdateCheckContextProcessorTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')
        UpdateCheckStatus.objects.create(pk=1, latest_version='v1.1.0', update_available=True)

    def test_banner_shown_to_superuser(self):
        # The banner lives on Site Settings, next to the update-checker
        # card it's about - not as a global banner on every page.
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'A newer version')

    def test_banner_hidden_from_regular_user(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'A newer version')

    def test_banner_hidden_when_no_update_available(self):
        UpdateCheckStatus.objects.filter(pk=1).update(update_available=False, latest_version='1.0.0')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        # Not a plain assertNotContains(response, 'A newer version') - that
        # text also appears, inert, inside the "Check for updates now" JS
        # (see PortainerRedeployBannerTests docstring) so the check needs to
        # target the server-rendered banner markup specifically.
        self.assertNotContains(response, 'role="alert" id="update-banner"')

    def test_banner_hidden_once_running_version_catches_up_even_with_stale_flag(self):
        # Regression test for a real bug: update_available is only
        # recomputed when check_for_update() actually runs (daily task or
        # a manual click) - if the container gets redeployed to the fix in
        # between, the stored flag stays stale (True) until the next check,
        # so the banner kept showing "update available" even though the
        # currently running version already *is* that update. The banner
        # must be driven by comparing the stored latest_version against the
        # live settings.VERSION on every request, not by trusting the
        # stored boolean.
        UpdateCheckStatus.objects.filter(pk=1).update(update_available=True, latest_version='v1.0.0')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertNotContains(response, 'role="alert" id="update-banner"')


class CreateDefaultPeriodicTasksCommandTests(TestCase):
    """
    myapp.management.commands.create_default_periodic_tasks - runs on every
    container start (docker/entrypoint.sh), so it must be safe to re-run
    against an install that already has these rows from a previous version
    of this command.
    """
    def test_creates_expected_tasks(self):
        call_command('create_default_periodic_tasks')
        names = set(PeriodicTask.objects.values_list('name', flat=True))
        self.assertEqual(names, {
            'Periodic Expiry Check', 'Notification Rules Expiry Check',
            'Next Up Reminder Check',
            'Update Check', 'Upstream Version Check', 'Scheduled Backup',
            'Daily Notification Digest', 'Active Today Outward-Leg Cleanup',
            'Advance Recurring Items', 'Retry Failed Firefly Pushes',
            'DMS Auto Pull',
            'Gift Card Inactivity Check', 'Merchant Health Check',
            'Purge Old Import Jobs',
            'Login Spike Alert',
            'Email Expiry Digest',
        })

    def test_update_check_and_upstream_check_run_hourly_not_daily(self):
        call_command('create_default_periodic_tasks')
        for name in ('Update Check', 'Upstream Version Check'):
            crontab = PeriodicTask.objects.get(name=name).crontab
            self.assertEqual(crontab.hour, '*')

    def test_rerunning_does_not_create_duplicates(self):
        call_command('create_default_periodic_tasks')
        call_command('create_default_periodic_tasks')
        self.assertEqual(PeriodicTask.objects.filter(name='Update Check').count(), 1)

    def test_rerunning_corrects_crontab_on_an_existing_row(self):
        # Simulates an install provisioned by an older version of this
        # command, back when Update Check still shared the daily 9am
        # schedule - re-running the command today should reschedule it to
        # hourly in place, not leave it stuck on the old crontab forever.
        stale_schedule = CrontabSchedule.objects.create(
            minute='0', hour='9', day_of_week='*', day_of_month='*', month_of_year='*'
        )
        PeriodicTask.objects.create(
            name='Update Check', task='myapp.tasks.check_for_update_task',
            crontab=stale_schedule, enabled=True,
        )
        call_command('create_default_periodic_tasks')
        task = PeriodicTask.objects.get(name='Update Check')
        self.assertEqual(task.crontab.hour, '*')
        self.assertEqual(PeriodicTask.objects.filter(name='Update Check').count(), 1)

    def test_rerunning_preserves_an_admin_disabled_task(self):
        call_command('create_default_periodic_tasks')
        task = PeriodicTask.objects.get(name='Update Check')
        task.enabled = False
        task.save(update_fields=['enabled'])

        call_command('create_default_periodic_tasks')
        task.refresh_from_db()
        self.assertFalse(task.enabled)
        self.assertEqual(PeriodicTask.objects.filter(name='Update Check').count(), 1)


@override_settings(VERSION='1.0.0', UPSTREAM_VERSION='1.29.0')
class UpstreamSyncContextProcessorTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_upstream_version_shown_to_superuser(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'based on upstream v1.29.0')

    def test_upstream_version_hidden_from_regular_user(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'based on upstream')

    def test_sync_available_badge_shown_when_upstream_ahead(self):
        # Assert the actual server-rendered badge markup, not just the bare
        # word - "Sync available" also appears unconditionally as a JS
        # string literal in the page's "Check now" script (used to
        # reconstruct the badge client-side after a manual check), so a
        # plain assertContains(response, 'Sync available') would pass
        # regardless of whether the badge is actually rendered.
        UpstreamSyncStatus.objects.create(pk=1, latest_version='v1.30.0')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, '<span class="badge bg-warning text-dark ms-1">Sync available</span>')

    def test_sync_available_badge_hidden_when_up_to_date(self):
        UpstreamSyncStatus.objects.create(pk=1, latest_version='v1.29.0')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertNotContains(response, '<span class="badge bg-warning text-dark ms-1">Sync available</span>')


class PortainerRedeployServiceTests(TestCase):
    def test_raises_when_not_configured(self):
        set_site_config(portainer_webhook_url='')
        with self.assertRaises(PortainerRedeployError):
            trigger_redeploy()

    @patch('myapp.portainer.requests.post')
    def test_posts_to_configured_url(self, mock_post):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
        trigger_redeploy()
        mock_post.assert_called_once_with('https://portainer.example.com/api/webhooks/abc123', timeout=10)

    @patch('myapp.portainer.requests.post')
    def test_request_failure_raises(self, mock_post):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        import requests
        mock_post.side_effect = requests.RequestException('boom')
        with self.assertRaises(PortainerRedeployError):
            trigger_redeploy()


class PortainerRedeployViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_requires_login(self):
        response = self.client.post(reverse('trigger_portainer_redeploy'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_requires_post(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('trigger_portainer_redeploy'))
        self.assertEqual(response.status_code, 405)

    @patch('myapp.views.trigger_redeploy')
    def test_regular_user_forbidden_and_webhook_not_called(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), follow=True)
        mock_trigger.assert_not_called()
        self.assertContains(response, 'Only administrators')

    @patch('myapp.views.trigger_redeploy')
    def test_superuser_success_message(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), follow=True)
        mock_trigger.assert_called_once()
        self.assertContains(response, 'Redeploy triggered')

    @patch('myapp.views.trigger_redeploy')
    def test_superuser_failure_message(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        mock_trigger.side_effect = PortainerRedeployError('boom')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), follow=True)
        self.assertContains(response, 'Redeploy request failed')

    @patch('myapp.views.trigger_redeploy')
    def test_redirects_to_safe_referer(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), HTTP_REFERER='http://testserver/dashboard')
        self.assertRedirects(response, 'http://testserver/dashboard', fetch_redirect_response=False)

    @patch('myapp.views.trigger_redeploy')
    def test_ignores_external_referer(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), HTTP_REFERER='https://evil.example.com/phish')
        self.assertRedirects(response, reverse('show_items'), fetch_redirect_response=False)

    @patch('myapp.views.trigger_redeploy')
    def test_ajax_success_returns_json(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

    @patch('myapp.views.trigger_redeploy')
    def test_ajax_failure_returns_json_error(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        mock_trigger.side_effect = PortainerRedeployError('boom')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 503)
        self.assertIn('boom', response.json()['error'])

    @patch('myapp.views.trigger_redeploy')
    def test_ajax_regular_user_forbidden_returns_json(self, mock_trigger):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='alice', password='pw12345!')
        response = self.client.post(reverse('trigger_portainer_redeploy'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 403)
        mock_trigger.assert_not_called()


@override_settings(VERSION='1.0.0')
class PortainerRedeployBannerTests(TestCase):
    """
    The update-available banner (and its "Redeploy now" button) lives on
    the Site Settings page next to the update-checker card it's about,
    not as a global banner on every page - see the "Update Check" section
    of site_settings.html.

    Since "Check for updates now" can also build this banner client-side
    (no page reload - see check-updates-btn's handler), its HTML template
    lives inline in that JS too, inert until a check actually finds an
    update. That means literal strings like 'id="redeploy-btn"' or 'A
    newer version' can legitimately appear in the page source even when
    nothing is rendered - assertions here target either the actual
    server-rendered wrapper ('role="alert" id="update-banner"', built via
    a Django {% if %}, never via the JS template) or the
    data-redeploy-configured/-url capability flags the JS reads at
    runtime, not the dead template text itself.
    """
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        UpdateCheckStatus.objects.create(pk=1, latest_version='v1.1.0', update_available=True)
        self.client.login(username='admin', password='pw12345!')

    def test_button_shown_when_configured(self):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'role="alert" id="update-banner"')
        self.assertContains(response, 'data-redeploy-configured="1"')

    def test_check_updates_button_always_carries_redeploy_config(self):
        # "Check for updates now" can discover an update mid-session and
        # build the banner + Redeploy button itself via JS, with no page
        # reload - so it needs the webhook config even when no banner was
        # server-rendered (e.g. no update was known about yet).
        # update_check_available is derived by comparing latest_version to
        # the live settings.VERSION (see UpdateCheckContextProcessorTests),
        # not by trusting this stored boolean - both must match to make the
        # banner genuinely absent.
        UpdateCheckStatus.objects.filter(pk=1).update(update_available=False, latest_version='1.0.0')
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        response = self.client.get(reverse('site_settings'))
        content = response.content.decode()
        self.assertNotIn('role="alert" id="update-banner"', content)  # no banner server-rendered
        self.assertIn('data-redeploy-configured="1"', content)
        self.assertIn(f'data-redeploy-url="{reverse("trigger_portainer_redeploy")}"', content)

    def test_button_hidden_when_not_configured(self):
        set_site_config(portainer_webhook_url='')
        response = self.client.get(reverse('site_settings'))
        self.assertNotContains(response, 'data-redeploy-configured="1"')

    def test_banner_not_shown_on_other_pages(self):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'Redeploy now')
        self.assertNotContains(response, 'A newer version')

    def test_redeploy_button_is_not_a_nested_form(self):
        # Same class of bug as test_check_updates_control_is_not_a_nested_form:
        # this banner now lives inside #site-settings-form, so the button
        # must be a plain <button> + fetch, not a literal nested <form>.
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        response = self.client.get(reverse('site_settings'))
        content = response.content.decode()
        self.assertNotIn(f'action="{reverse("trigger_portainer_redeploy")}"', content)
        self.assertIn('id="redeploy-btn"', content)
        self.assertEqual(content.count('<form method="POST" action="" id="site-settings-form">'), 1)


class SiteConfigurationModelTests(TestCase):
    def test_load_creates_singleton_with_defaults(self):
        config = SiteConfiguration.load()
        self.assertEqual(config.pk, 1)
        self.assertEqual(config.expiry_threshold_days, 30)
        self.assertEqual(config.ocr_backend, 'none')

    def test_load_returns_same_row_on_repeat_calls(self):
        first = SiteConfiguration.load()
        first.expiry_threshold_days = 99
        first.save()
        second = SiteConfiguration.load()
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.expiry_threshold_days, 99)


class SiteConfigurationSeedMigrationTests(TestCase):
    """
    The 0046 seed migration must read EXPIRY_THRESHOLD_DAYS_FINAL (what
    notify/tasks.py::final_threshold_days() actually reads pre-migration),
    not just its own EXPIRY_LAST_NOTIFICATION_DAYS fallback - otherwise a
    deployment that set EXPIRY_THRESHOLD_DAYS_FINAL independently silently
    loses that value on upgrade.
    """

    def test_seed_uses_expiry_threshold_days_final_when_set(self):
        import importlib

        from django.apps import apps as django_apps

        seed_migration = importlib.import_module('myapp.migrations.0046_seed_siteconfiguration')

        SiteConfiguration.objects.filter(pk=1).delete()
        with override_settings(EXPIRY_LAST_NOTIFICATION_DAYS=7, EXPIRY_THRESHOLD_DAYS_FINAL=3):
            seed_migration.seed_from_env(django_apps, None)

        config = SiteConfiguration.objects.get(pk=1)
        self.assertEqual(config.expiry_last_notification_days, 3)


def _site_config_form_data(**overrides):
    data = {
        'expiry_threshold_days': 30, 'expiry_last_notification_days': 7,
        'ntfy_default_server': 'https://ntfy.sh',
        'webpush_vapid_public_key': '', 'webpush_vapid_private_key': '',
        'webpush_vapid_claims_email': 'mailto:admin@example.com',
        'merchant_logos_enabled': True,
        'share_via_smart_enabled': True,
        'share_link_expiry_days': 30, 'share_link_pin_enabled': False,
        'ocr_backend': 'none', 'anthropic_api_key': '', 'anthropic_ocr_model': 'claude-sonnet-5',
        'openai_api_key': '', 'openai_ocr_model': 'gpt-4o-mini',
        'pkpass_cert_path': '', 'pkpass_cert_password': '', 'pkpass_wwdr_cert_path': '',
        'pkpass_team_id': '', 'pkpass_pass_type_id': '', 'pkpass_organization_name': 'VoucherVault Plus+',
        'google_wallet_service_account_key_path': '', 'google_wallet_issuer_id': '', 'google_wallet_class_id': '',
        'update_check_enabled': True, 'update_check_repo': 'gregbtm/VoucherVault',
        'portainer_webhook_url': '',
        'scheduled_backup_enabled': True, 'backup_retention_count': 7,
        'expiring_soon_limit': 10, 'calendar_months_ahead': 3,
        'wallet_chart_limit': 8, 'duplicate_photo_threshold': 10,
        'inactivity_threshold_days': 90, 'companies_house_api_key': '',
        'webpush_barcode_key_version': 1,
        'allow_registration': True,
        'invite_expiry_days': 7,
        'email_host': '', 'email_port': 587, 'email_host_user': '',
        'email_host_password': '', 'email_use_tls': True, 'email_use_ssl': False,
        'email_from_address': '',
        'pocket_id_url': '', 'pocket_id_api_key': '',
        'oidc_discovery_url': '',
        'oidc_client_id': '',
        'oidc_client_secret': '',
        'oidc_provider_name': 'SSO',
        'oidc_create_user': True,
        'oidc_autologin': False,
        'oidc_admin_group': '',
        'oidc_require_totp': False,
        'security_alert_ntfy_topic': '',
        'security_alert_threshold': 10,
    }
    data.update(overrides)
    return data


class SiteConfigurationFormTests(TestCase):
    def setUp(self):
        self.config = SiteConfiguration.load()
        self.config.anthropic_api_key = 'existing-secret'
        self.config.save()

    def test_blank_secret_field_preserves_existing_value(self):
        form = SiteConfigurationForm(data=_site_config_form_data(anthropic_api_key=''), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.anthropic_api_key, 'existing-secret')

    def test_non_blank_secret_field_updates_value(self):
        form = SiteConfigurationForm(data=_site_config_form_data(anthropic_api_key='sk-ant-new-secret'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.anthropic_api_key, 'sk-ant-new-secret')

    def test_blank_logo_dev_key_preserves_existing_value(self):
        self.config.logo_dev_api_key = 'pk_existing'
        self.config.save()
        form = SiteConfigurationForm(data=_site_config_form_data(logo_dev_api_key=''), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.logo_dev_api_key, 'pk_existing')

    def test_non_blank_logo_dev_key_updates_value(self):
        form = SiteConfigurationForm(data=_site_config_form_data(logo_dev_api_key='pk_new'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.logo_dev_api_key, 'pk_new')

    def test_non_secret_fields_save_normally(self):
        form = SiteConfigurationForm(data=_site_config_form_data(expiry_threshold_days=45, ocr_backend='tesseract'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.expiry_threshold_days, 45)
        self.assertEqual(saved.ocr_backend, 'tesseract')

    def test_portainer_webhook_url_is_not_a_secret_field(self):
        # Deliberately visible/plaintext rather than password-masked - see
        # SiteConfiguration.SECRET_FIELDS - so it round-trips normally like
        # any other plain field instead of requiring a blank-to-preserve dance.
        self.assertNotIn('portainer_webhook_url', SiteConfiguration.SECRET_FIELDS)
        form = SiteConfigurationForm(
            data=_site_config_form_data(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123'),
            instance=self.config,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.portainer_webhook_url, 'https://portainer.example.com/api/webhooks/abc123')

    def test_malformed_portainer_webhook_url_rejected(self):
        form = SiteConfigurationForm(data=_site_config_form_data(portainer_webhook_url='not-a-url'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('portainer_webhook_url', form.errors)

    def test_malformed_ntfy_default_server_rejected(self):
        form = SiteConfigurationForm(data=_site_config_form_data(ntfy_default_server='ntfy.sh'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('ntfy_default_server', form.errors)

    def test_update_check_repo_must_be_owner_slash_repo(self):
        form = SiteConfigurationForm(data=_site_config_form_data(update_check_repo='not a repo'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('update_check_repo', form.errors)

    def test_update_check_repo_valid_shape_accepted(self):
        form = SiteConfigurationForm(data=_site_config_form_data(update_check_repo='gregbtm/VoucherVault'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)

    def test_expiry_threshold_days_must_be_at_least_one(self):
        form = SiteConfigurationForm(data=_site_config_form_data(expiry_threshold_days=0), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('expiry_threshold_days', form.errors)

    def test_backup_retention_count_must_be_at_least_one(self):
        form = SiteConfigurationForm(data=_site_config_form_data(backup_retention_count=0), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('backup_retention_count', form.errors)

    def test_final_warning_cannot_exceed_initial_threshold(self):
        form = SiteConfigurationForm(
            data=_site_config_form_data(expiry_threshold_days=7, expiry_last_notification_days=30),
            instance=self.config,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('expiry_last_notification_days', form.errors)

    def test_anthropic_api_key_format_checked_only_when_set(self):
        form = SiteConfigurationForm(data=_site_config_form_data(anthropic_api_key='wrong-prefix'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('anthropic_api_key', form.errors)

    def test_openai_api_key_format_checked_only_when_set(self):
        form = SiteConfigurationForm(data=_site_config_form_data(openai_api_key='wrong-prefix'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('openai_api_key', form.errors)

    def test_pkpass_cert_path_must_exist_on_disk(self):
        form = SiteConfigurationForm(data=_site_config_form_data(pkpass_cert_path='/nonexistent/cert.p12'), instance=self.config)
        self.assertFalse(form.is_valid())
        self.assertIn('pkpass_cert_path', form.errors)

    def test_google_wallet_key_path_must_exist_on_disk(self):
        form = SiteConfigurationForm(
            data=_site_config_form_data(google_wallet_service_account_key_path='/nonexistent/key.json'),
            instance=self.config,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('google_wallet_service_account_key_path', form.errors)

    def test_webpush_keys_required_together(self):
        form = SiteConfigurationForm(
            data=_site_config_form_data(webpush_vapid_public_key='pub-key-only'),
            instance=self.config,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('webpush_vapid_public_key', form.errors)
        self.assertIn('webpush_vapid_private_key', form.errors)

    def test_webpush_keys_pass_when_private_key_already_stored(self):
        # The private key is a SECRET_FIELDS entry that always submits blank
        # once set - the pairing check must compare against the *stored*
        # value, not the always-blank submitted one, or every unrelated
        # autosave would spuriously flag an already-configured pair as broken.
        self.config.webpush_vapid_private_key = 'existing-private-key'
        self.config.save()
        form = SiteConfigurationForm(
            data=_site_config_form_data(webpush_vapid_public_key='existing-public-key'),
            instance=self.config,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_google_wallet_fields_required_together(self):
        form = SiteConfigurationForm(
            data=_site_config_form_data(google_wallet_issuer_id='issuer-only'),
            instance=self.config,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('google_wallet_service_account_key_path', form.errors)
        self.assertIn('google_wallet_issuer_id', form.errors)

    def test_pkpass_fields_required_together(self):
        form = SiteConfigurationForm(
            data=_site_config_form_data(pkpass_team_id='team-only'),
            instance=self.config,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('pkpass_team_id', form.errors)
        self.assertIn('pkpass_pass_type_id', form.errors)


class IntegrationStatusTests(TestCase):
    """_integration_status() - the readiness badges shown on Site Settings."""

    def test_ocr_status_none_when_backend_disabled(self):
        config = set_site_config(ocr_backend='none')
        self.assertIsNone(_integration_status(config)['ocr'])

    def test_ocr_status_ready_when_claude_key_set(self):
        config = set_site_config(ocr_backend='claude', anthropic_api_key='sk-ant-x')
        self.assertTrue(_integration_status(config)['ocr']['ready'])

    def test_ocr_status_not_ready_when_openai_key_missing(self):
        config = set_site_config(ocr_backend='openai', openai_api_key='')
        self.assertFalse(_integration_status(config)['ocr']['ready'])

    def test_pkpass_status_none_when_path_blank(self):
        config = set_site_config(pkpass_cert_path='')
        self.assertIsNone(_integration_status(config)['pkpass'])

    def test_pkpass_status_not_ready_when_file_missing(self):
        config = set_site_config(pkpass_cert_path='/nonexistent/cert.p12')
        self.assertFalse(_integration_status(config)['pkpass']['ready'])

    def test_google_wallet_status_none_when_path_blank(self):
        config = set_site_config(google_wallet_service_account_key_path='')
        self.assertIsNone(_integration_status(config)['google_wallet'])

    def test_google_wallet_status_not_ready_when_file_missing(self):
        config = set_site_config(google_wallet_service_account_key_path='/nonexistent/key.json')
        self.assertFalse(_integration_status(config)['google_wallet']['ready'])


class SiteSettingsViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_requires_login(self):
        response = self.client.get(reverse('site_settings'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_regular_user_forbidden(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('site_settings'), follow=True)
        self.assertContains(response, 'Only administrators')

    def test_superuser_can_view(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Configure Site Settings')

    def test_secret_value_never_rendered_in_page(self):
        config = SiteConfiguration.load()
        config.anthropic_api_key = 'super-secret-value'
        config.save()
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertNotContains(response, 'super-secret-value')
        self.assertContains(response, 'Currently set')

    def test_superuser_post_saves_and_redirects(self):
        self.client.login(username='admin', password='pw12345!')
        data = _site_config_form_data(expiry_threshold_days=60, ocr_backend='openai')
        response = self.client.post(reverse('site_settings'), data, follow=True)
        self.assertContains(response, 'Site settings saved')
        self.assertEqual(SiteConfiguration.load().expiry_threshold_days, 60)
        self.assertEqual(SiteConfiguration.load().ocr_backend, 'openai')

    def test_autosave_ajax_post_saves_without_redirect(self):
        self.client.login(username='admin', password='pw12345!')
        data = _site_config_form_data(expiry_threshold_days=45)
        response = self.client.post(reverse('site_settings'), data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['success'], True)
        self.assertEqual(SiteConfiguration.load().expiry_threshold_days, 45)

    def test_autosave_ajax_post_invalid_returns_field_errors(self):
        self.client.login(username='admin', password='pw12345!')
        data = _site_config_form_data()
        del data['expiry_threshold_days']
        response = self.client.post(reverse('site_settings'), data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload['success'], False)
        self.assertIn('expiry_threshold_days', payload['errors'])

    def test_no_save_button_present(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertNotContains(response, 'Save Site Settings')
        self.assertContains(response, 'Changes save automatically')

    def test_nav_link_shown_only_to_superuser(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'Site Settings')

        self.client.logout()
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('show_items'))
        self.assertNotContains(response, 'Site Settings')

    def test_check_updates_control_is_not_a_nested_form(self):
        # Regression test for a real bug: a literal <form> nested inside the
        # page's own settings <form> gets auto-closed early by the browser's
        # HTML parser, silently detaching everything rendered after that
        # point in the template from the actual form element (previously
        # this broke the "Save Site Settings" button; now it would break
        # autosave, which submits the same #site-settings-form). The
        # "Check for updates now" control must be a plain <button> that
        # hits its own endpoint via fetch instead of a literal nested <form>.
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        content = response.content.decode()
        self.assertNotIn(f'action="{reverse("trigger_update_check")}"', content)
        self.assertIn('id="check-updates-btn"', content)
        self.assertEqual(content.count('<form method="POST" action="" id="site-settings-form">'), 1)

    def test_portainer_webhook_url_renders_as_plaintext(self):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'https://portainer.example.com/api/webhooks/abc123')

    def test_shows_installed_and_latest_version(self):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={'latest_version': 'v9.9.9', 'checked_at': timezone.now()})
        self.client.login(username='admin', password='pw12345!')
        with override_settings(VERSION='v1.2.3'):
            response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'v1.2.3')
        self.assertContains(response, 'v9.9.9')

    def test_github_connectivity_badge_shows_connected_after_clean_check(self):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={'checked_at': timezone.now(), 'last_check_error': ''})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'Connected')

    def test_github_connectivity_badge_shows_unreachable_on_error(self):
        UpdateCheckStatus.objects.update_or_create(pk=1, defaults={'checked_at': timezone.now(), 'last_check_error': 'boom'})
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'Unreachable')

    def test_ocr_status_badge_shown_when_backend_configured(self):
        set_site_config(ocr_backend='claude', anthropic_api_key='sk-ant-x')
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        self.assertContains(response, 'ocr-fields')

    def test_help_links_present_for_sections_with_setup_docs(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('site_settings'))
        for slug in ('ocr', 'apple-wallet', 'google-wallet', 'auto-deploy', 'backup-restore'):
            self.assertContains(response, reverse('view_doc', args=[slug]))


class HelpDocViewerTests(TestCase):
    """The in-app docs/*.md renderer behind the Site Settings '?' buttons."""

    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')

    def test_requires_login(self):
        response = self.client.get(reverse('view_doc', args=['google-wallet']))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_regular_user_can_view_docs(self):
        """All help docs are accessible to any logged-in user — the Help Center
        exposes every slug, so gating individual docs after that would be confusing."""
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('view_doc', args=['google-wallet']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<h1')

    def test_unknown_doc_slug_404s(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('view_doc', args=['not-a-real-doc']))
        self.assertEqual(response.status_code, 404)

    def test_known_docs_render_successfully(self):
        self.client.login(username='admin', password='pw12345!')
        for slug in ('google-wallet', 'apple-wallet', 'ocr', 'auto-deploy', 'backup-restore'):
            response = self.client.get(reverse('view_doc', args=[slug]))
            self.assertEqual(response.status_code, 200, f'{slug} did not render')
            # Markdown headings should have been converted to real HTML, not left as literal "#" text.
            self.assertContains(response, '<h1', msg_prefix=f'{slug} missing rendered heading')

    def test_ajax_request_returns_json_for_modal(self):
        """The help-doc modal (base.html) fetches this with X-Requested-With - it
        must get {title, body_html} JSON, not the full doc_viewer.html page."""
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('view_doc', args=['google-wallet']), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('Google Wallet', data['title'])
        self.assertIn('<h1', data['body_html'])

    def test_ajax_unknown_doc_slug_returns_json_404(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('view_doc', args=['not-a-real-doc']), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.json())

    def test_regular_user_can_view_integration_docs(self):
        """Integration docs must be reachable by non-superusers — they link from
        the API Access and Notification pages which aren't superuser-gated."""
        self.client.login(username='alice', password='pw12345!')
        for slug in ('n8n', 'mcp-server', 'rail-ticket', 'firefly'):
            response = self.client.get(reverse('view_doc', args=[slug]))
            self.assertEqual(response.status_code, 200, f'{slug} did not render for a regular user')
            self.assertContains(response, '<h1', msg_prefix=f'{slug} missing rendered heading')

    def test_regular_user_can_view_all_known_docs(self):
        """All help docs are now self-service — the Help Center links every slug
        to any logged-in user, so every doc must render with status 200."""
        self.client.login(username='alice', password='pw12345!')
        for slug in ('google-wallet', 'apple-wallet', 'ocr', 'auto-deploy', 'backup-restore'):
            response = self.client.get(reverse('view_doc', args=[slug]))
            self.assertEqual(response.status_code, 200, f'{slug} did not render for a regular user')
            self.assertContains(response, '<h1', msg_prefix=f'{slug} missing rendered heading')


class ApiAccessViewTests(TestCase):
    """The self-service API token page (GUI alternative to
    drf_create_token / POSTing a password to /api/v1/auth/token/)."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('api_access'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_no_token_shows_generate_button(self):
        response = self.client.get(reverse('api_access'))
        self.assertContains(response, 'Generate API Token')
        self.assertNotContains(response, 'Regenerate')

    def test_generate_creates_token_and_reveals_it_once(self):
        response = self.client.post(reverse('api_access'), {'action': 'generate'}, follow=True)
        token = Token.objects.get(user=self.user)
        self.assertContains(response, f'Token {token.key}')

        # A second GET must not still show the raw key - it was a one-shot reveal.
        response = self.client.get(reverse('api_access'))
        self.assertNotContains(response, token.key)
        self.assertContains(response, 'Active token')

    def test_regenerate_replaces_the_key(self):
        old_token = Token.objects.create(user=self.user)
        old_key = old_token.key

        response = self.client.post(reverse('api_access'), {'action': 'regenerate'}, follow=True)
        new_token = Token.objects.get(user=self.user)

        self.assertNotEqual(new_token.key, old_key)
        self.assertContains(response, f'Token {new_token.key}')
        self.assertFalse(Token.objects.filter(key=old_key).exists())

    def test_revoke_deletes_the_token(self):
        Token.objects.create(user=self.user)
        response = self.client.post(reverse('api_access'), {'action': 'revoke'}, follow=True)
        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertContains(response, 'Generate API Token')

    def test_revoke_with_no_token_is_a_harmless_noop(self):
        response = self.client.post(reverse('api_access'), {'action': 'revoke'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Token.objects.filter(user=self.user).exists())

    def test_token_is_scoped_to_the_requesting_user(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        bob_token = Token.objects.create(user=bob)

        response = self.client.get(reverse('api_access'))
        self.assertContains(response, 'Generate API Token')
        self.assertNotContains(response, bob_token.key)


class OfflineCacheTogglePreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_defaults_to_enabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        self.assertTrue(prefs.offline_cache_enabled)

    def test_cache_button_shown_by_default(self):
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'manualCacheManager.cacheData()')

    def test_cache_button_hidden_when_disabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.offline_cache_enabled = False
        prefs.save()
        response = self.client.get(reverse('show_items'))
        self.assertNotContains(response, 'manualCacheManager.cacheData()')

    def test_purge_button_always_shown(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.offline_cache_enabled = False
        prefs.save()
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'manualCacheManager.clearCache()')

    def test_save_redirects_with_prefs_saved_signal(self):
        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact', 'next_up_max_items': '1',
            'default_currency': 'GBP', 'keep_screen_awake': 'on', 'offline_cache_enabled': 'on',
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')

    def test_disabling_offline_cache_adds_purge_signal(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.offline_cache_enabled = True
        prefs.save()

        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact', 'next_up_max_items': '1',
            'default_currency': 'GBP', 'keep_screen_awake': 'on',
            # offline_cache_enabled omitted -> unchecked checkbox -> False
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1&cache_purge=1')
        prefs.refresh_from_db()
        self.assertFalse(prefs.offline_cache_enabled)

    def test_saving_without_toggling_off_has_no_purge_signal(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.offline_cache_enabled = False
        prefs.save()

        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact', 'next_up_max_items': '1',
            'default_currency': 'GBP', 'keep_screen_awake': 'on',
            # still leaving offline_cache_enabled off - no transition, no purge needed
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')


class BlurCodesTogglePreferenceTests(TestCase):
    """
    The tap-to-reveal blur on barcodes/redeem codes used to be hardcoded
    with no way to turn it off, causing real friction at point-of-sale
    (an extra tap before a loyalty card's barcode is even scannable).
    blur_codes_enabled makes it an opt-out per-user preference.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_defaults_to_enabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        self.assertTrue(prefs.blur_codes_enabled)

    def test_codes_blurred_by_default(self):
        # Blur only ever applies to the barcode/QR image - the redeem code
        # text is never blurred, since the whole block is tap-to-copy and
        # needs to stay legible.
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertContains(response, 'qr-image opaque')
        self.assertNotContains(response, 'redeem-code opaque')

    def test_codes_not_blurred_when_disabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.blur_codes_enabled = False
        prefs.save()
        item = make_item(self.user)
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'qr-image opaque')
        self.assertNotContains(response, 'redeem-code opaque')

    def test_toggle_off_saves_via_preferences_form(self):
        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact', 'next_up_max_items': '1',
            'default_currency': 'GBP', 'keep_screen_awake': 'on', 'offline_cache_enabled': 'on',
            # blur_codes_enabled omitted -> unchecked checkbox -> False
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')
        prefs = UserPreference.objects.get(user=self.user)
        self.assertFalse(prefs.blur_codes_enabled)


class RedeemCodeTapToCopyTests(TestCase):
    """
    The redeem code/card number blocks are tap-to-copy in their entirety
    (no separate copy button) and clip to a few lines with a "Show full
    code" toggle once the code gets long, rather than growing the card
    indefinitely.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_short_code_has_no_expand_toggle(self):
        item = make_item(self.user, redeem_code='SHORT123')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="redeem-code-expand-btn"')
        self.assertNotContains(response, 'redeem-code clippable')

    def test_long_code_gets_clippable_and_expand_toggle(self):
        item = make_item(self.user, redeem_code='X' * 120)
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertContains(response, 'id="redeem-code-expand-btn"')
        self.assertContains(response, 'redeem-code clippable')

    def test_long_card_number_gets_clippable_and_expand_toggle(self):
        item = make_item(self.user, card_number='Y' * 120)
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertContains(response, 'id="card-number-expand-btn"')


class TiltScanDetectionTests(TestCase):
    """
    tilt_scan_detection_enabled (opt-in, default off) suggests marking an
    item Used when the phone is tilted forward - see tilt-scan-detect.js
    for the client-side motion heuristic this just gates the markup for.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_defaults_to_disabled(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        self.assertFalse(prefs.tilt_scan_detection_enabled)

    def test_banner_absent_by_default(self):
        item = make_item(self.user, code_type='qrcode')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="tilt-scan-banner"')

    def test_banner_present_when_enabled_and_scannable_and_unused(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.tilt_scan_detection_enabled = True
        prefs.save()
        item = make_item(self.user, code_type='qrcode')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertContains(response, 'id="tilt-scan-banner"')
        self.assertContains(response, 'vvInitTiltScanDetect')

    def test_banner_absent_once_item_already_used(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.tilt_scan_detection_enabled = True
        prefs.save()
        item = make_item(self.user, code_type='qrcode')
        item.is_used = True
        item.save()
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="tilt-scan-banner"')

    def test_banner_absent_for_loyalty_card(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.tilt_scan_detection_enabled = True
        prefs.save()
        item = make_item(self.user, type='loyaltycard', code_type='qrcode')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="tilt-scan-banner"')

    def test_banner_absent_when_no_scannable_code(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.tilt_scan_detection_enabled = True
        prefs.save()
        item = make_item(self.user, code_type='none')
        response = self.client.get(reverse('view_item', args=[item.id]))
        self.assertNotContains(response, 'id="tilt-scan-banner"')

    def test_toggle_on_saves_via_preferences_form(self):
        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact', 'next_up_max_items': '1',
            'default_currency': 'GBP', 'keep_screen_awake': 'on', 'offline_cache_enabled': 'on',
            'blur_codes_enabled': 'on', 'tilt_scan_detection_enabled': 'on',
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')
        prefs = UserPreference.objects.get(user=self.user)
        self.assertTrue(prefs.tilt_scan_detection_enabled)


class ToggleItemStatusAjaxTests(TestCase):
    """
    toggle_item_status supports an AJAX JSON round-trip (mirroring
    toggle_pin_item's existing pattern) so tilt-scan-detect.js's "Mark
    Used" banner button can flip the status without a full page reload.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_ajax_request_returns_json(self):
        item = make_item(self.user)
        response = self.client.post(
            reverse('toggle_item_status', args=[item.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_used'])

        response = self.client.post(
            reverse('toggle_item_status', args=[item.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertFalse(response.json()['is_used'])

    def test_non_ajax_request_still_redirects(self):
        item = make_item(self.user)
        response = self.client.post(reverse('toggle_item_status', args=[item.id]))
        self.assertRedirects(response, reverse('view_item', kwargs={'item_uuid': item.id}))


class PwaCacheClearOnLoginTests(TestCase):
    """
    Regression coverage for a real cross-user data leak: the service
    worker caches authenticated pages by URL only (see
    myapp/serviceworker.js), with no per-session scoping. On a shared or
    kiosk browser, a session that ends without a clean logout (closed tab,
    crash) could leave the next user served the previous user's cached
    pages. Login now flags the very next page render to clear all PWA
    caches client-side, as defense-in-depth on top of logout's own
    proactive clear (see the logout JS in base.html).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_clear_cache_call_present_on_first_page_after_login(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'clearPwaCaches();')

    def test_clear_cache_call_absent_on_subsequent_page_loads(self):
        self.client.login(username='alice', password='pw12345!')
        self.client.get(reverse('show_items'))  # consumes the one-shot flag
        response = self.client.get(reverse('show_items'))
        self.assertNotContains(response, 'clearPwaCaches();')

    def test_clear_pwa_caches_helper_always_defined(self):
        # The shared helper function itself must always be present so the
        # logout flow (which also calls it) works even when this page
        # wasn't reached via a fresh login.
        self.client.login(username='alice', password='pw12345!')
        self.client.get(reverse('show_items'))  # consume the flag
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'function clearPwaCaches()')

    def test_flag_not_set_for_anonymous_requests(self):
        response = self.client.get(reverse('login'))
        self.assertNotContains(response, 'clearPwaCaches();')


class GetStatsCurrencyTests(TestCase):
    """
    Regression coverage for a real data-correctness bug: get_stats() used
    to Sum() item values straight across currencies with no grouping or
    conversion, producing a meaningless total for any multi-currency
    inventory (see upstream l4rm4nd/VoucherVault#135, still open there).
    """

    def setUp(self):
        token = '11111111-1111-1111-1111-111111111111'
        AppSettings.objects.create(api_token=token)
        self.auth_header = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_single_currency_sums_directly(self):
        make_item(self.user, name='A', value='10.00', currency='GBP')
        make_item(self.user, name='B', value='5.00', currency='GBP')
        response = self.client.get(reverse('get_stats'), {'user': 'alice'}, **self.auth_header)
        payload = response.json()
        self.assertEqual(payload['item_stats']['total_value'], 15.0)
        self.assertEqual(payload['item_stats']['total_value_currency'], 'GBP')
        self.assertNotIn('total_value_by_currency', payload['item_stats'])

    def test_mixed_currency_without_fixer_key_reports_breakdown_not_a_wrong_sum(self):
        make_item(self.user, name='A', value='10.00', currency='GBP')
        make_item(self.user, name='B', value='20.00', currency='USD')
        response = self.client.get(reverse('get_stats'), {'user': 'alice'}, **self.auth_header)
        payload = response.json()
        self.assertIsNone(payload['item_stats']['total_value'])
        self.assertEqual(payload['item_stats']['total_value_by_currency'], {'GBP': 10.0, 'USD': 20.0})
        self.assertIn('currency_conversion_note', payload['item_stats'])

    def test_mixed_currency_across_users_with_no_user_filter_reports_breakdown(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        make_item(self.user, name='A', value='10.00', currency='GBP')
        make_item(bob, name='B', value='20.00', currency='EUR')
        response = self.client.get(reverse('get_stats'), **self.auth_header)
        payload = response.json()
        self.assertIsNone(payload['item_stats']['total_value'])
        self.assertEqual(payload['item_stats']['total_value_by_currency'], {'GBP': 10.0, 'EUR': 20.0})

    def test_issuer_stats_grouped_by_currency_not_summed_across_them(self):
        make_item(self.user, name='A', issuer='Acme', value='10.00', currency='GBP')
        make_item(self.user, name='B', issuer='Acme', value='20.00', currency='USD')
        response = self.client.get(reverse('get_stats'), {'user': 'alice'}, **self.auth_header)
        payload = response.json()
        acme_entries = [row for row in payload['issuer_stats'] if row['issuer'] == 'Acme']
        self.assertEqual(len(acme_entries), 2)
        totals_by_currency = {row['currency']: str(row['total_value']) for row in acme_entries}
        self.assertEqual(totals_by_currency, {'GBP': '10.00', 'USD': '20.00'})


class OidcDiscoveryTests(TestCase):
    """
    fetch_oidc_discovery() backs the optional OIDC_DISCOVERY_URL setting,
    which auto-populates OIDC_OP_*_ENDPOINT from a provider's
    .well-known/openid-configuration document instead of requiring each
    endpoint to be configured by hand (upstream closed this as not
    planned - l4rm4nd/VoucherVault#67 - since mozilla-django-oidc itself
    has no discovery support).
    """

    def test_returns_parsed_document_on_success(self):
        document = {
            'authorization_endpoint': 'https://idp.example.com/auth',
            'token_endpoint': 'https://idp.example.com/token',
            'userinfo_endpoint': 'https://idp.example.com/userinfo',
            'jwks_uri': 'https://idp.example.com/jwks',
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(document).encode()
        mock_response.__enter__.return_value = mock_response
        with patch('myapp.utils.urllib.request.urlopen', return_value=mock_response):
            result = fetch_oidc_discovery('https://idp.example.com/.well-known/openid-configuration')
        self.assertEqual(result, document)

    def test_returns_empty_dict_on_network_failure(self):
        with patch('myapp.utils.urllib.request.urlopen', side_effect=urllib.error.URLError('unreachable')):
            result = fetch_oidc_discovery('https://idp.example.com/.well-known/openid-configuration')
        self.assertEqual(result, {})

    def test_returns_empty_dict_on_invalid_json(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b'not json'
        mock_response.__enter__.return_value = mock_response
        with patch('myapp.utils.urllib.request.urlopen', return_value=mock_response):
            result = fetch_oidc_discovery('https://idp.example.com/.well-known/openid-configuration')
        self.assertEqual(result, {})


class IcsCalendarBuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_includes_active_item_with_expiry(self):
        make_item(self.user, name='Coffee Voucher', expiry_date=date(2026, 12, 31))
        calendar = build_ics_calendar(self.user).decode('utf-8')

        self.assertIn('BEGIN:VCALENDAR', calendar)
        self.assertIn('BEGIN:VEVENT', calendar)
        self.assertIn('SUMMARY:Coffee Voucher expires', calendar)
        self.assertIn('DTSTART;VALUE=DATE:20261231', calendar)
        self.assertIn('END:VCALENDAR', calendar)

    def test_excludes_used_items(self):
        make_item(self.user, name='Used Voucher', is_used=True)
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('BEGIN:VEVENT', calendar)

    def test_excludes_archived_items(self):
        make_item(self.user, name='Archived Voucher', is_archived=True)
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('BEGIN:VEVENT', calendar)

    def test_only_includes_own_users_items(self):
        other = User.objects.create_user(username='bob', password='pw12345!')
        make_item(other, name='Someone Elses Voucher')
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('BEGIN:VEVENT', calendar)

    def test_description_never_includes_redeem_code_pin_or_card_number(self):
        """
        Regression guard: this feed is meant to be subscribed to from a
        phone's native calendar app, which typically syncs every field of
        every event to Google/Apple/Outlook's own cloud - an actual
        redeemable code has no business leaving VoucherVault's control
        that way, no matter how "richer" this feed gets in the future.
        """
        make_item(
            self.user, name='Secret Card', redeem_code='SUPERSECRETCODE',
            pin='4471', card_number='4111222233334444',
        )
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('SUPERSECRETCODE', calendar)
        self.assertNotIn('4471', calendar)
        self.assertNotIn('4111222233334444', calendar)

    def test_description_includes_wallet_notes_and_balance_check_url(self):
        wallet = Wallet.objects.create(user=self.user, name='Groceries')
        make_item(
            self.user, name='Tesco Card', wallet=wallet, notes='Show at till',
            balance_check_url='https://tesco.com/balance',
        )
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertIn('Wallet: Groceries', calendar)
        self.assertIn('Notes: Show at till', calendar)
        self.assertIn('Balance check: https://tesco.com/balance', calendar)

    def test_location_is_wallet_name(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        make_item(self.user, name='Train Ticket', wallet=wallet)
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertIn('LOCATION:Travel', calendar)

    def test_no_location_without_a_wallet(self):
        make_item(self.user, name='No Wallet Item')
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('LOCATION:', calendar)

    def test_categories_lists_tag_names(self):
        item = make_item(self.user, name='Tagged Item')
        item.tags.add(Tag.objects.create(user=self.user, name='discount'))
        item.tags.add(Tag.objects.create(user=self.user, name='food'))
        calendar = build_ics_calendar(self.user).decode('utf-8')
        line = next(l for l in calendar.replace('\r\n ', '').split('\r\n') if l.startswith('CATEGORIES:'))
        self.assertEqual(set(line[len('CATEGORIES:'):].split(',')), {'discount', 'food'})

    def test_no_categories_without_tags(self):
        make_item(self.user, name='Untagged Item')
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('CATEGORIES:', calendar)

    def test_url_uses_request_to_build_absolute_link_to_item(self):
        item = make_item(self.user, name='Linked Item')
        factory = RequestFactory()
        request = factory.get('/en/calendar/download/')
        calendar = build_ics_calendar(self.user, request).decode('utf-8')
        self.assertIn(f'URL:http://testserver/en/items/view/{item.id}', calendar.replace('\r\n ', ''))

    def test_no_url_without_a_request(self):
        make_item(self.user, name='No Request Item')
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertNotIn('URL:', calendar)

    def test_valarm_trigger_uses_item_notify_days_before_override(self):
        make_item(self.user, name='Custom Threshold Item', notify_days_before=3)
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertIn('BEGIN:VALARM', calendar)
        self.assertIn('TRIGGER:-P3D', calendar)

    def test_valarm_trigger_falls_back_to_site_configured_threshold(self):
        set_site_config(expiry_threshold_days=10)
        make_item(self.user, name='Default Threshold Item')
        calendar = build_ics_calendar(self.user).decode('utf-8')
        self.assertIn('TRIGGER:-P10D', calendar)

    def test_download_ics_view_includes_url_via_real_request(self):
        self.client.login(username='alice', password='pw12345!')
        item = make_item(self.user, name='Downloaded Item')
        response = self.client.get(reverse('download_ics'))
        calendar = response.content.decode('utf-8').replace('\r\n ', '')
        self.assertIn(f'URL:http://testserver/en/items/view/{item.id}', calendar)

    def test_escape_text_handles_special_characters(self):
        self.assertEqual(_escape_text('A, B; C\\D\nE'), 'A\\, B\; C\\\\D\\nE')

    def test_fold_line_wraps_long_lines(self):
        long_line = 'DESCRIPTION:' + ('x' * 100)
        folded = _fold_line(long_line)
        physical_lines = folded.split('\r\n')
        self.assertGreater(len(physical_lines), 1)
        for line in physical_lines[1:]:
            self.assertTrue(line.startswith(' '))

    def test_fold_line_leaves_short_lines_untouched(self):
        self.assertEqual(_fold_line('SUMMARY:short'), 'SUMMARY:short')


class IcsFeedViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.profile = self.user.userprofile

    def test_new_user_gets_an_ics_token_automatically(self):
        self.assertTrue(self.profile.ics_token)

    def test_feed_accessible_without_login(self):
        make_item(self.user, name='Feed Item')
        response = self.client.get(reverse('ics_feed', kwargs={'token': self.profile.ics_token}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar; charset=utf-8')
        self.assertIn(b'BEGIN:VEVENT', response.content)

    def test_feed_404s_for_unknown_token(self):
        response = self.client.get(reverse('ics_feed', kwargs={'token': '00000000-0000-0000-0000-000000000000'}))
        self.assertEqual(response.status_code, 404)

    def test_download_requires_login(self):
        response = self.client.get(reverse('download_ics'))
        self.assertEqual(response.status_code, 302)

    def test_download_returns_attachment(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('download_ics'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])

    def test_regenerate_token_changes_it_and_invalidates_old_feed_url(self):
        old_token = self.profile.ics_token
        self.client.login(username='alice', password='pw12345!')

        response = self.client.post(reverse('regenerate_ics_token'))

        self.assertRedirects(response, reverse('upload_import'))
        self.profile.refresh_from_db()
        self.assertNotEqual(self.profile.ics_token, old_token)

        old_feed_response = self.client.get(reverse('ics_feed', kwargs={'token': old_token}))
        self.assertEqual(old_feed_response.status_code, 404)

    def test_regenerate_token_requires_login(self):
        response = self.client.post(reverse('regenerate_ics_token'))
        self.assertEqual(response.status_code, 302)


class BulkActionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        self.item1 = make_item(self.user, name='Item One')
        self.item2 = make_item(self.user, name='Item Two')
        self.item3 = make_item(self.user, name='Item Three')

    def _post(self, url_name, payload):
        return self.client.post(
            reverse(url_name), data=json.dumps(payload), content_type='application/json',
        )

    # ---- archive ----

    def test_bulk_archive_archives_selected_items(self):
        response = self._post('bulk_archive_items', {'item_ids': [str(self.item1.id), str(self.item2.id)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['processed'], 2)

        self.item1.refresh_from_db()
        self.item2.refresh_from_db()
        self.item3.refresh_from_db()
        self.assertTrue(self.item1.is_archived)
        self.assertTrue(self.item2.is_archived)
        self.assertFalse(self.item3.is_archived)

    def test_bulk_archive_skips_already_archived_items(self):
        self.item1.is_archived = True
        self.item1.save(update_fields=['is_archived'])

        response = self._post('bulk_archive_items', {'item_ids': [str(self.item1.id), str(self.item2.id)]})

        self.assertEqual(response.json()['processed'], 1)

    @patch('myapp.views.notify_item_archived')
    def test_bulk_archive_notifies_once_per_newly_archived_item(self, mock_notify):
        self._post('bulk_archive_items', {'item_ids': [str(self.item1.id), str(self.item2.id)]})
        self.assertEqual(mock_notify.call_count, 2)

    # ---- delete ----

    def test_bulk_delete_removes_items(self):
        response = self._post('bulk_delete_items', {'item_ids': [str(self.item1.id), str(self.item2.id)]})
        self.assertEqual(response.json()['processed'], 2)
        self.assertFalse(Item.objects.filter(pk=self.item1.pk).exists())
        self.assertFalse(Item.objects.filter(pk=self.item2.pk).exists())
        self.assertTrue(Item.objects.filter(pk=self.item3.pk).exists())

    # ---- tag ----

    def test_bulk_tag_creates_and_assigns_tags(self):
        response = self._post('bulk_tag_items', {
            'item_ids': [str(self.item1.id), str(self.item2.id)], 'tags': 'sale, food',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['processed'], 2)

        self.assertEqual(set(self.item1.tags.values_list('name', flat=True)), {'sale', 'food'})
        self.assertEqual(set(self.item2.tags.values_list('name', flat=True)), {'sale', 'food'})
        self.assertEqual(Tag.objects.filter(user=self.user).count(), 2)

    def test_bulk_tag_reuses_existing_tag(self):
        Tag.objects.create(user=self.user, name='sale')
        self._post('bulk_tag_items', {'item_ids': [str(self.item1.id)], 'tags': 'sale'})
        self.assertEqual(Tag.objects.filter(user=self.user, name='sale').count(), 1)

    def test_bulk_tag_requires_tags_param(self):
        response = self._post('bulk_tag_items', {'item_ids': [str(self.item1.id)], 'tags': ''})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    # ---- move to wallet ----

    def test_bulk_move_assigns_wallet(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        response = self._post('bulk_move_items', {
            'item_ids': [str(self.item1.id), str(self.item2.id)], 'wallet_id': wallet.id,
        })
        self.assertEqual(response.json()['processed'], 2)
        self.item1.refresh_from_db()
        self.item2.refresh_from_db()
        self.assertEqual(self.item1.wallet, wallet)
        self.assertEqual(self.item2.wallet, wallet)

    def test_bulk_move_to_no_wallet_clears_wallet(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        self.item1.wallet = wallet
        self.item1.save(update_fields=['wallet'])

        self._post('bulk_move_items', {'item_ids': [str(self.item1.id)], 'wallet_id': None})

        self.item1.refresh_from_db()
        self.assertIsNone(self.item1.wallet)

    def test_bulk_move_rejects_wallet_not_owned_or_shared(self):
        others_wallet = Wallet.objects.create(user=self.other, name='Bobs Wallet')
        response = self._post('bulk_move_items', {'item_ids': [str(self.item1.id)], 'wallet_id': others_wallet.id})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.item1.refresh_from_db()
        self.assertIsNone(self.item1.wallet)

    def test_bulk_move_allows_wallet_shared_with_user(self):
        shared_wallet = Wallet.objects.create(user=self.other, name='Shared Wallet')
        shared_wallet.shared_with.add(self.user)

        response = self._post('bulk_move_items', {'item_ids': [str(self.item1.id)], 'wallet_id': shared_wallet.id})

        self.assertEqual(response.json()['processed'], 1)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.wallet, shared_wallet)

    # ---- permission scoping ----

    def test_bulk_actions_skip_items_user_cannot_access(self):
        others_item = make_item(self.other, name='Bobs Item')

        response = self._post('bulk_archive_items', {'item_ids': [str(self.item1.id), str(others_item.id)]})

        self.assertEqual(response.json()['processed'], 1)
        self.assertEqual(response.json()['skipped'], 1)
        others_item.refresh_from_db()
        self.assertFalse(others_item.is_archived)

    # ---- auth/method requirements ----

    def test_bulk_actions_require_login(self):
        self.client.logout()
        response = self._post('bulk_archive_items', {'item_ids': [str(self.item1.id)]})
        self.assertEqual(response.status_code, 302)

    def test_bulk_actions_require_post(self):
        response = self.client.get(reverse('bulk_archive_items'))
        self.assertEqual(response.status_code, 405)


@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=3)
class LoginLockoutTests(TestCase):
    """
    django-axes brute-force protection on the login form (myproject/
    settings.py) - locked to a low failure limit here so the tests don't
    need to actually make 5 requests to prove the behaviour. AXES_ENABLED
    is forced back on since settings.py disables it by default under the
    test runner (see the comment there) for compatibility with
    django.test.Client.login(), which every other test in this app uses.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='correct-horse-battery-staple')

    def _attempt(self, password):
        return self.client.post(reverse('login'), {'username': 'alice', 'password': password})

    def test_correct_password_succeeds_under_the_limit(self):
        response = self._attempt('correct-horse-battery-staple')
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_locked_out_after_failure_limit(self):
        for _ in range(3):
            self._attempt('wrong-password')

        # Even the correct password is now rejected - that's the point.
        response = self._attempt('correct-horse-battery-staple')
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_not_locked_out_below_the_limit(self):
        for _ in range(2):
            self._attempt('wrong-password')

        response = self._attempt('correct-horse-battery-staple')
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_successful_login_resets_the_failure_count(self):
        """AXES_RESET_ON_SUCCESS: two failures, then a success, then two
        more failures should NOT reach the limit (3), since the first two
        don't carry over past the successful login in between."""
        self._attempt('wrong-password')
        self._attempt('wrong-password')
        self._attempt('correct-horse-battery-staple')
        self.client.logout()

        self._attempt('wrong-password')
        response = self._attempt('correct-horse-battery-staple')
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_lockout_is_per_username_not_shared_across_users(self):
        User.objects.create_user(username='bob', password='bobs-password')
        for _ in range(3):
            self._attempt('wrong-password')  # locks out alice

        response = self.client.post(reverse('login'), {'username': 'bob', 'password': 'bobs-password'})
        self.assertTrue(response.wsgi_request.user.is_authenticated)


def _overpass_response(names):
    """Builds a fake requests.Response-like object for a successful
    Overpass query returning one named node per name in `names`."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        'elements': [{'type': 'node', 'tags': {'name': name}} for name in names]
        + [{'type': 'node', 'tags': {}}],  # an unnamed POI, which must be skipped
    }
    return mock_response


class NearbyPlacesMatchingTests(TestCase):
    """Unit tests for the fuzzy merchant-name matching in nearby_places.py -
    no network calls, no database."""

    def test_normalize_strips_punctuation_and_case(self):
        self.assertEqual(_normalize("Tesco's Express!"), 'tescos express')

    def test_names_match_exact(self):
        self.assertTrue(_names_match(_normalize('Tesco'), 'Tesco'))

    def test_names_match_chain_branch_substring(self):
        # A chain branch name containing the issuer (or vice versa) is a
        # confident match - the common real-world case for retail chains.
        self.assertTrue(_names_match(_normalize('Tesco'), 'Tesco Express'))
        self.assertTrue(_names_match(_normalize('Tesco Superstore'), 'Tesco'))

    def test_names_match_minor_spelling_difference(self):
        # Same length, single-character difference, neither a substring of
        # the other - only the edit-distance fallback catches this.
        self.assertTrue(_names_match(_normalize('Waitrose'), 'Waltrose'))

    def test_names_match_rejects_unrelated_names(self):
        self.assertFalse(_names_match(_normalize('Tesco'), 'Sainsburys'))

    def test_names_match_empty_strings(self):
        self.assertFalse(_names_match('', 'Tesco'))
        self.assertFalse(_names_match(_normalize('Tesco'), ''))


class FindNearbyIssuerMatchesTests(TestCase):
    """Tests find_nearby_issuer_matches end-to-end against a mocked
    Overpass response - the only network boundary in this module."""

    def setUp(self):
        cache.clear()
        set_site_config(nearby_places_enabled=True)

    def test_returns_matched_issuers(self):
        with patch('myapp.nearby_places.requests.post') as mock_post:
            mock_post.return_value = _overpass_response(['Tesco Express', 'Starbucks'])
            matches = find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco', 'Greggs'])
        self.assertEqual(matches, ['Tesco'])

    def test_empty_when_site_wide_disabled(self):
        set_site_config(nearby_places_enabled=False)
        with patch('myapp.nearby_places.requests.post') as mock_post:
            matches = find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco'])
        mock_post.assert_not_called()
        self.assertEqual(matches, [])

    def test_empty_on_request_failure(self):
        import requests
        with patch('myapp.nearby_places.requests.post', side_effect=requests.ConnectionError('boom')):
            matches = find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco'])
        self.assertEqual(matches, [])

    def test_empty_issuers_list_short_circuits_without_a_request(self):
        with patch('myapp.nearby_places.requests.post') as mock_post:
            matches = find_nearby_issuer_matches(51.5, -0.12, 150, [])
        mock_post.assert_not_called()
        self.assertEqual(matches, [])

    def test_result_is_cached_for_the_same_coordinates(self):
        with patch('myapp.nearby_places.requests.post') as mock_post:
            mock_post.return_value = _overpass_response(['Tesco Express'])
            find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco'])
            find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco'])
        self.assertEqual(mock_post.call_count, 1)

    def test_uses_configured_overpass_url(self):
        set_site_config(overpass_api_url='https://overpass.example.internal/api/interpreter')
        with patch('myapp.nearby_places.requests.post') as mock_post:
            mock_post.return_value = _overpass_response([])
            find_nearby_issuer_matches(51.5, -0.12, 150, ['Tesco'])
        called_url = mock_post.call_args[0][0]
        self.assertEqual(called_url, 'https://overpass.example.internal/api/interpreter')


class NearbyItemsViewTests(TestCase):
    """Tests the nearby_items proxy view: opt-in gating at both the user
    and site level, coordinate validation, and that only the requesting
    user's own (or shared-with-them) items are ever returned."""

    def setUp(self):
        cache.clear()
        set_site_config(nearby_places_enabled=True)
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        prefs, _ = UserPreference.objects.get_or_create(user=self.alice)
        prefs.nearby_items_enabled = True
        prefs.nearby_radius_m = 150
        prefs.save()

    def _post(self, lat='51.5', lon='-0.12'):
        return self.client.post(reverse('nearby_items'), {'lat': lat, 'lon': lon})

    def test_requires_login(self):
        self.client.logout()
        response = self._post()
        self.assertNotEqual(response.status_code, 200)

    def test_empty_when_user_preference_off(self):
        prefs = UserPreference.objects.get(user=self.alice)
        prefs.nearby_items_enabled = False
        prefs.save()
        with patch('myapp.views.find_nearby_issuer_matches') as mock_find:
            response = self._post()
        mock_find.assert_not_called()
        self.assertEqual(response.json(), {'items': []})

    def test_empty_when_site_disabled(self):
        set_site_config(nearby_places_enabled=False)
        with patch('myapp.views.find_nearby_issuer_matches') as mock_find:
            response = self._post()
        mock_find.assert_not_called()
        self.assertEqual(response.json(), {'items': []})

    def test_rejects_invalid_coordinates(self):
        response = self._post(lat='not-a-number', lon='-0.12')
        self.assertEqual(response.status_code, 400)

    def test_rejects_out_of_range_coordinates(self):
        response = self._post(lat='999', lon='-0.12')
        self.assertEqual(response.status_code, 400)

    def test_returns_matched_items_for_the_requesting_user(self):
        make_item(self.alice, name='Clubcard', issuer='Tesco', redeem_code='CODE1')
        make_item(self.alice, name='Unrelated Voucher', issuer='Greggs', redeem_code='CODE2')
        with patch('myapp.views.find_nearby_issuer_matches', return_value=['Tesco']):
            response = self._post()
        data = response.json()
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['items'][0]['name'], 'Clubcard')
        self.assertEqual(data['items'][0]['issuer'], 'Tesco')
        self.assertIn('url', data['items'][0])

    def test_excludes_used_and_archived_items(self):
        make_item(self.alice, name='Used Card', issuer='Tesco', redeem_code='CODE3', is_used=True)
        make_item(self.alice, name='Archived Card', issuer='Tesco', redeem_code='CODE4', is_archived=True)
        with patch('myapp.views.find_nearby_issuer_matches', return_value=['Tesco']):
            response = self._post()
        self.assertEqual(response.json()['items'], [])

    def test_never_returns_another_users_items(self):
        make_item(self.bob, name="Bob's Card", issuer='Tesco', redeem_code='CODE5')
        with patch('myapp.views.find_nearby_issuer_matches', return_value=['Tesco']):
            response = self._post()
        self.assertEqual(response.json()['items'], [])

    def test_passes_users_own_radius_preference(self):
        prefs = UserPreference.objects.get(user=self.alice)
        prefs.nearby_radius_m = 400
        prefs.save()
        with patch('myapp.views.find_nearby_issuer_matches', return_value=[]) as mock_find:
            self._post()
        self.assertEqual(mock_find.call_args[0][2], 400)


class AnalyticsViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice_an', password='pw12345!')
        self.bob = User.objects.create_user(username='bob_an', password='pw12345!')
        self.client.force_login(self.alice)

    def _get(self):
        return self.client.get(reverse('analytics'))

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('analytics'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('accounts/login', response['Location'])

    def test_renders_for_empty_user(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'analytics.html')

    def test_kpis_count_correctly(self):
        today = date.today()
        make_item(self.alice, name='Active', expiry_date=today + timedelta(days=10), is_used=False)
        make_item(self.alice, name='Used', is_used=True, expiry_date=today + timedelta(days=10))
        make_item(self.alice, name='Expired', expiry_date=today - timedelta(days=1), is_used=False)
        response = self._get()
        kpis = response.context['kpis']
        self.assertEqual(kpis['total'], 3)
        self.assertEqual(kpis['active'], 1)
        self.assertEqual(kpis['used'], 1)
        self.assertEqual(kpis['expired'], 1)

    def test_only_own_items_in_context(self):
        make_item(self.alice, name='Alice item')
        make_item(self.bob, name='Bob item')
        response = self._get()
        kpis = response.context['kpis']
        self.assertEqual(kpis['total'], 1)

    def test_post_not_allowed(self):
        response = self.client.post(reverse('analytics'))
        self.assertEqual(response.status_code, 405)

    def test_currency_breakdown_excludes_loyalty_cards(self):
        today = date.today()
        make_item(self.alice, type='giftcard', value='50.00', currency='GBP',
                  value_type='money', expiry_date=today + timedelta(days=30))
        make_item(self.alice, type='loyaltycard', value='100.00', currency='GBP',
                  value_type='money', expiry_date=today + timedelta(days=30))
        response = self._get()
        currencies = [r['currency'] for r in response.context['currency_breakdown']]
        # loyalty card excluded from currency breakdown
        self.assertEqual(len([c for c in currencies if c == 'GBP']), 1)
        total = next(r['total'] for r in response.context['currency_breakdown'] if r['currency'] == 'GBP')
        self.assertAlmostEqual(total, 50.0)

    def test_top_issuers_excludes_blank_issuer(self):
        today = date.today()
        make_item(self.alice, issuer='Tesco', expiry_date=today + timedelta(days=10))
        make_item(self.alice, issuer='', expiry_date=today + timedelta(days=10))
        response = self._get()
        issuers = [r['issuer'] for r in response.context['top_issuers']]
        self.assertIn('Tesco', issuers)
        self.assertNotIn('', issuers)

    def test_json_context_keys_present(self):
        response = self._get()
        for key in ('months_seq_json', 'monthly_added_json', 'monthly_used_json',
                    'value_by_type_json', 'top_issuers_json'):
            self.assertIn(key, response.context)
            json.loads(response.context[key])  # must be valid JSON


# ── Phase C/D/E/F Tests ─────────────────────────────────────────────────────

class WalletMembershipTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user('alice_wm', 'a@ex.com', 'pw')
        self.bob = User.objects.create_user('bob_wm', 'b@ex.com', 'pw')
        self.wallet = Wallet.objects.create(user=self.alice, name='WM Wallet')
        self.client.force_login(self.alice)

    def _share(self, role='editor'):
        return self.client.post(
            reverse('share_wallet', kwargs={'wallet_id': self.wallet.id}),
            {'username': 'bob_wm', 'role': role},
        )

    def test_share_creates_membership(self):
        self._share(role='viewer')
        self.assertTrue(WalletMembership.objects.filter(wallet=self.wallet, user=self.bob, role='viewer').exists())
        self.assertIn(self.bob, self.wallet.shared_with.all())

    def test_share_logs_activity(self):
        self._share()
        self.assertTrue(WalletActivity.objects.filter(wallet=self.wallet, action='member_added').exists())

    def test_unshare_removes_membership(self):
        self._share()
        self.client.post(reverse('unshare_wallet', kwargs={'wallet_id': self.wallet.id, 'user_id': self.bob.id}))
        self.assertFalse(WalletMembership.objects.filter(wallet=self.wallet, user=self.bob).exists())

    def test_leave_removes_membership(self):
        self._share()
        self.client.force_login(self.bob)
        self.client.post(reverse('leave_shared_wallet', kwargs={'wallet_id': self.wallet.id}))
        self.assertFalse(WalletMembership.objects.filter(wallet=self.wallet, user=self.bob).exists())

    def test_activity_feed_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('wallet_activity_feed', kwargs={'wallet_id': self.wallet.id}))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('accounts/login', resp['Location'])

    def test_activity_feed_owner_sees_feed(self):
        WalletActivity.objects.create(wallet=self.wallet, actor=self.alice, action='item_added', item_name='Test')
        resp = self.client.get(reverse('wallet_activity_feed', kwargs={'wallet_id': self.wallet.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Test')

    def test_activity_feed_non_member_rejected(self):
        self.client.force_login(self.bob)
        resp = self.client.get(reverse('wallet_activity_feed', kwargs={'wallet_id': self.wallet.id}))
        self.assertEqual(resp.status_code, 403)


class UserWebhookTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user('alice_wh', 'a@ex.com', 'pw')
        self.bob = User.objects.create_user('bob_wh', 'b@ex.com', 'pw')
        self.client.force_login(self.alice)

    def test_webhook_list_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('manage_webhooks'))
        self.assertEqual(resp.status_code, 302)

    def test_create_webhook(self):
        resp = self.client.post(reverse('create_webhook'), {
            'name': 'My Hook',
            'url': 'https://example.com/hook',
            'secret': '',
            'event_item_created': '1',
            'enabled': '1',
        })
        self.assertRedirects(resp, reverse('manage_webhooks'))
        self.assertTrue(UserWebhook.objects.filter(user=self.alice, name='My Hook').exists())

    def test_create_webhook_requires_event(self):
        self.client.post(reverse('create_webhook'), {
            'name': 'No Events',
            'url': 'https://example.com/hook',
        })
        self.assertFalse(UserWebhook.objects.filter(name='No Events').exists())

    def test_delete_webhook(self):
        hook = UserWebhook.objects.create(
            user=self.alice, name='Del Hook',
            url='https://x.com', events=['item_created'],
        )
        self.client.post(reverse('delete_webhook', kwargs={'webhook_id': hook.id}))
        self.assertFalse(UserWebhook.objects.filter(id=hook.id).exists())

    def test_bob_cannot_delete_alices_webhook(self):
        hook = UserWebhook.objects.create(
            user=self.alice, name='Private',
            url='https://x.com', events=['item_created'],
        )
        self.client.force_login(self.bob)
        resp = self.client.post(reverse('delete_webhook', kwargs={'webhook_id': hook.id}))
        self.assertEqual(resp.status_code, 404)


class TOTPTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user('alice_totp', 'a@ex.com', 'AlicePw123!')
        self.client.force_login(self.alice)

    def test_totp_setup_page_accessible(self):
        resp = self.client.get(reverse('totp_setup'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'data:image/png;base64,')

    def test_totp_setup_creates_unconfirmed_device(self):
        self.client.get(reverse('totp_setup'))
        self.assertTrue(TOTPDevice.objects.filter(user=self.alice, confirmed=False).exists())

    def test_totp_confirm_with_valid_token(self):
        import pyotp
        secret = pyotp.random_base32()
        device = TOTPDevice.objects.create(user=self.alice, secret=secret, confirmed=False)
        token = pyotp.TOTP(secret).now()
        resp = self.client.post(reverse('totp_setup'), {'token': token, 'secret': secret})
        device.refresh_from_db()
        self.assertTrue(device.confirmed)
        # After confirmation the view renders the backup-codes page (200) rather than redirecting.
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['setup_complete'])
        self.assertEqual(len(resp.context['backup_codes']), 8)

    def test_totp_confirm_with_invalid_token_rejected(self):
        import pyotp
        secret = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.alice, secret=secret, confirmed=False)
        resp = self.client.post(reverse('totp_setup'), {'token': '000000', 'secret': secret})
        self.assertFalse(TOTPDevice.objects.get(user=self.alice).confirmed)
        self.assertEqual(resp.status_code, 200)

    def test_totp_disable(self):
        import pyotp
        TOTPDevice.objects.create(user=self.alice, secret=pyotp.random_base32(), confirmed=True)
        self.client.post(reverse('totp_disable'))
        self.assertFalse(TOTPDevice.objects.filter(user=self.alice).exists())

    def test_totp_verify_requires_session_key(self):
        resp = self.client.get(reverse('totp_verify'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])


class SessionManagementTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user('alice_sm', 'a@ex.com', 'pw')
        self.client.force_login(self.alice)

    def test_session_management_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse('session_management'))
        self.assertEqual(resp.status_code, 302)

    def test_session_management_200(self):
        resp = self.client.get(reverse('session_management'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Active Sessions')

    def test_session_management_shows_totp_status(self):
        resp = self.client.get(reverse('session_management'))
        self.assertContains(resp, 'Two-Factor')


class LoginAuditLogTests(TestCase):
    def test_successful_login_creates_log(self):
        User.objects.create_user('audit_user', 'a@ex.com', 'AuditPw123!')
        self.client.post(reverse('login'), {'username': 'audit_user', 'password': 'AuditPw123!'})
        self.assertTrue(LoginAuditLog.objects.filter(username_attempted='audit_user', success=True).exists())

    def test_failed_login_creates_log(self):
        self.client.post(reverse('login'), {'username': 'nobody', 'password': 'wrong'})
        self.assertTrue(LoginAuditLog.objects.filter(username_attempted='nobody', success=False).exists())


class CustomLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('cl_user', 'x@ex.com', 'GoodPw123!')

    def test_login_redirects_authenticated_user(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('login'))
        self.assertEqual(resp.status_code, 302)

    def test_login_with_bad_credentials_returns_error(self):
        resp = self.client.post(reverse('login'), {'username': 'cl_user', 'password': 'wrong'})
        self.assertEqual(resp.status_code, 200)

    def test_login_without_totp_succeeds(self):
        resp = self.client.post(reverse('login'), {'username': 'cl_user', 'password': 'GoodPw123!'})
        self.assertRedirects(resp, reverse('show_items'))

    def test_login_with_totp_redirects_to_verify(self):
        import pyotp
        TOTPDevice.objects.create(user=self.user, secret=pyotp.random_base32(), confirmed=True)
        resp = self.client.post(reverse('login'), {'username': 'cl_user', 'password': 'GoodPw123!'})
        self.assertRedirects(resp, reverse('totp_verify'))
        self.assertIn('_totp_user_id', self.client.session)


class ViewItemPhase116FieldsTests(TestCase):
    """Phase 116 fields (seat_number, initial_value, minimum_spend, points_balance,
    membership_tier) and Phase 115 field (share_message) appear on the item detail page."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def _get(self, item):
        return self.client.get(reverse('view_item', kwargs={'item_uuid': item.id}))

    def test_seat_number_shown(self):
        item = make_item(self.user, type='travelpass', seat_number='12A Coach B')
        resp = self._get(item)
        self.assertContains(resp, '12A Coach B')

    def test_initial_value_shown(self):
        item = make_item(self.user, initial_value='45.00')
        resp = self._get(item)
        self.assertContains(resp, '45.00')
        self.assertContains(resp, 'Face Value')

    def test_minimum_spend_shown(self):
        item = make_item(self.user, minimum_spend='10.00')
        resp = self._get(item)
        self.assertContains(resp, '10.00')
        self.assertContains(resp, 'Minimum Spend')

    def test_points_balance_shown(self):
        item = make_item(self.user, type='loyaltycard', points_balance=3500)
        resp = self._get(item)
        self.assertContains(resp, '3500')
        self.assertContains(resp, 'Points Balance')

    def test_membership_tier_shown(self):
        item = make_item(self.user, type='loyaltycard', membership_tier='Gold')
        resp = self._get(item)
        self.assertContains(resp, 'Gold')

    def test_share_message_shown_to_owner(self):
        item = make_item(self.user, share_message='Use code SAVE20 at checkout.')
        resp = self._get(item)
        self.assertContains(resp, 'Use code SAVE20 at checkout.')

    def test_share_message_hidden_when_empty(self):
        item = make_item(self.user)
        resp = self._get(item)
        self.assertNotContains(resp, 'bi-chat-quote')

    def test_empty_fields_not_rendered(self):
        item = make_item(self.user)
        resp = self._get(item)
        self.assertNotContains(resp, 'Seat / Coach')
        self.assertNotContains(resp, 'Face Value')
        self.assertNotContains(resp, 'Minimum Spend')
        self.assertNotContains(resp, 'Points Balance')


class ItemDocumentAPITests(TestCase):
    """REST API for /api/v1/items/{item_pk}/documents/."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.user = User.objects.create_user(username='alice', password='pw123!')
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}
        self.item = make_item(self.user)

    def _list_url(self):
        return f'/api/v1/items/{self.item.id}/documents/'

    def _detail_url(self, doc_id):
        return f'/api/v1/items/{self.item.id}/documents/{doc_id}/'

    def test_list_returns_empty_by_default(self):
        resp = self.client.get(self._list_url(), **self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        results = data.get('results', data) if isinstance(data, dict) else data
        self.assertEqual(len(results), 0)

    def test_unauthenticated_request_is_rejected(self):
        resp = self.client.get(self._list_url())
        self.assertEqual(resp.status_code, 401)

    def test_other_user_cannot_list_documents(self):
        bob = User.objects.create_user(username='bob', password='pw123!')
        bob_token = Token.objects.create(user=bob)
        resp = self.client.get(self._list_url(), HTTP_AUTHORIZATION=f'Token {bob_token.key}')
        self.assertEqual(resp.status_code, 404)

    def test_delete_document_via_api(self):
        from myapp.models import Document
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write(b'test')
            tmp_path = f.name
        try:
            with open(tmp_path, 'rb') as fh:
                doc = Document.objects.create(item=self.item, file=SimpleUploadedFile('test.txt', b'test'))
            resp = self.client.delete(self._detail_url(doc.id), **self.auth)
            self.assertEqual(resp.status_code, 204)
            self.assertFalse(Document.objects.filter(pk=doc.id).exists())
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class UserPreferenceAPIExpandedTests(TestCase):
    """Verify the expanded UserPreferenceSerializer exposes the new fields."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        self.user = User.objects.create_user(username='alice', password='pw123!')
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_get_returns_new_fields(self):
        resp = self.client.get('/api/v1/preferences/', **self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for field in ('keep_screen_awake', 'blur_codes_enabled', 'offline_cache_enabled',
                      'oled_dark_mode', 'tilt_scan_detection_enabled', 'nearby_items_enabled'):
            self.assertIn(field, data, msg=f'Missing field: {field}')

    def test_patch_keep_screen_awake(self):
        resp = self.client.patch(
            '/api/v1/preferences/',
            data=json.dumps({'keep_screen_awake': False}),
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['keep_screen_awake'])

    def test_patch_nearby_radius(self):
        resp = self.client.patch(
            '/api/v1/preferences/',
            data=json.dumps({'nearby_radius_m': 300}),
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['nearby_radius_m'], 300)


class DMSProviderAPITests(TestCase):
    """REST API for /api/v1/dms/providers/ — scoped to requesting user."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        from dms.models import DMSProvider
        self.user = User.objects.create_user(username='alice', password='pw123!')
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}
        self.provider = DMSProvider.objects.create(
            user=self.user, name='My Paperless', provider='paperless',
            base_url='http://paperless.local', api_token='tok123',
        )

    def test_list_returns_own_providers(self):
        resp = self.client.get('/api/v1/dms/providers/', **self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        results = data.get('results', data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'My Paperless')

    def test_other_user_cannot_see_providers(self):
        from rest_framework.authtoken.models import Token
        bob = User.objects.create_user(username='bob', password='pw123!')
        bob_token = Token.objects.create(user=bob)
        resp = self.client.get('/api/v1/dms/providers/', HTTP_AUTHORIZATION=f'Token {bob_token.key}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        results = data.get('results', data)
        self.assertEqual(len(results), 0)

    def test_unauthenticated_rejected(self):
        resp = self.client.get('/api/v1/dms/providers/')
        self.assertEqual(resp.status_code, 401)

    def test_api_token_write_only(self):
        resp = self.client.get(f'/api/v1/dms/providers/{self.provider.id}/', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('api_token', resp.json())


class DMSSyncLogAPITests(TestCase):
    """REST API for /api/v1/dms/sync-logs/ — read-only, scoped to user's providers."""

    def setUp(self):
        from rest_framework.authtoken.models import Token
        from dms.models import DMSProvider, DMSSyncLog
        self.user = User.objects.create_user(username='alice', password='pw123!')
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}
        self.provider = DMSProvider.objects.create(
            user=self.user, name='P', provider='paperless', base_url='http://p.local',
        )
        self.item = make_item(self.user)
        DMSSyncLog.objects.create(
            provider=self.provider, direction='push', status='ok',
            item=self.item, detail='ok',
        )

    def test_list_returns_logs(self):
        resp = self.client.get('/api/v1/dms/sync-logs/', **self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        results = data.get('results', data)
        self.assertEqual(len(results), 1)

    def test_logs_are_read_only(self):
        resp = self.client.post('/api/v1/dms/sync-logs/', data={}, **self.auth)
        self.assertEqual(resp.status_code, 405)

    def test_other_user_sees_no_logs(self):
        from rest_framework.authtoken.models import Token
        bob = User.objects.create_user(username='bob', password='pw123!')
        bob_token = Token.objects.create(user=bob)
        resp = self.client.get('/api/v1/dms/sync-logs/', HTTP_AUTHORIZATION=f'Token {bob_token.key}')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        results = data.get('results', data)
        self.assertEqual(len(results), 0)
        self.assertNotContains(resp, 'Membership Tier')


# ---------------------------------------------------------------------------
# PocketID / Invite-link / User-management tests
# ---------------------------------------------------------------------------

class InviteLinkModelTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin_inv', 'a@e.com', 'Pw123456!')

    def test_is_valid_fresh_invite(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(created_by=self.admin)
        self.assertTrue(inv.is_valid())

    def test_is_invalid_when_revoked(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(created_by=self.admin, revoked=True)
        self.assertFalse(inv.is_valid())

    def test_is_invalid_when_used(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(created_by=self.admin, used_at=timezone.now())
        self.assertFalse(inv.is_valid())

    def test_is_invalid_when_expired(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(
            created_by=self.admin,
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(inv.is_valid())

    def test_is_valid_before_expiry(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(
            created_by=self.admin,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertTrue(inv.is_valid())


class ManageInvitesViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin_mi', 'b@e.com', 'Pw123456!')
        self.user = User.objects.create_user('regular_mi', 'c@e.com', 'Pw123456!')
        self.client.login(username='admin_mi', password='Pw123456!')

    def test_get_page_200(self):
        resp = self.client.get(reverse('manage_invites'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Invite')

    def test_non_superuser_redirected(self):
        self.client.logout()
        self.client.login(username='regular_mi', password='Pw123456!')
        resp = self.client.get(reverse('manage_invites'))
        self.assertNotEqual(resp.status_code, 200)

    def test_create_invite(self):
        from myapp.models import InviteLink
        resp = self.client.post(reverse('manage_invites'), {
            'action': 'create', 'note': 'Test invite',
        })
        self.assertRedirects(resp, reverse('manage_invites'))
        self.assertTrue(InviteLink.objects.filter(note='Test invite').exists())

    def test_revoke_invite(self):
        from myapp.models import InviteLink
        inv = InviteLink.objects.create(created_by=self.admin, note='to_revoke')
        resp = self.client.post(reverse('manage_invites'), {
            'action': 'revoke', 'token': str(inv.token),
        })
        self.assertRedirects(resp, reverse('manage_invites'))
        inv.refresh_from_db()
        self.assertTrue(inv.revoked)


class AcceptInviteViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin_ai', 'd@e.com', 'Pw123456!')
        from myapp.models import InviteLink
        self.invite = InviteLink.objects.create(created_by=self.admin)

    def test_get_valid_invite_page(self):
        resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Create')

    def test_invalid_token_redirects_to_login(self):
        resp = self.client.get(reverse('accept_invite', args=[str(uuid.uuid4())]))
        self.assertEqual(resp.status_code, 302)

    def test_register_via_invite(self):
        resp = self.client.post(reverse('accept_invite', args=[str(self.invite.token)]), {
            'username': 'newuser_via_invite',
            'email': 'new@example.com',
            'password': 'StrongPass!1',
            'password2': 'StrongPass!1',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(User.objects.filter(username='newuser_via_invite').exists())
        self.invite.refresh_from_db()
        self.assertIsNotNone(self.invite.used_at)

    def test_register_password_mismatch(self):
        resp = self.client.post(reverse('accept_invite', args=[str(self.invite.token)]), {
            'username': 'newuser_mismatch',
            'email': '',
            'password': 'StrongPass!1',
            'password2': 'WrongPass!1',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='newuser_mismatch').exists())

    def test_revoked_invite_redirects_to_login(self):
        self.invite.revoked = True
        self.invite.save()
        resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertEqual(resp.status_code, 302)


class AcceptInviteOIDCTests(TestCase):
    """Tests for the OIDC-redirect path of accept_invite and invite_complete."""

    _OIDC_INIT_URL = '/oidc/authenticate/'

    def setUp(self):
        self.admin = User.objects.create_superuser('admin_oidc', 'o@e.com', 'Pw123456!')
        from myapp.models import InviteLink
        self.invite = InviteLink.objects.create(created_by=self.admin)

    def _stub_reverse(self, name, *args, **kwargs):
        """Intercept reverse() for OIDC URL names not registered in the test URLconf."""
        if name == 'oidc_authentication_init':
            return self._OIDC_INIT_URL
        from django.urls import reverse as real_reverse
        return real_reverse(name, *args, **kwargs)

    @override_settings(OIDC_ENABLED=True)
    def test_oidc_path_stores_token_in_session(self):
        with patch('myapp.views.reverse', side_effect=self._stub_reverse):
            resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('pending_invite_token', self.client.session)
        self.assertEqual(self.client.session['pending_invite_token'], str(self.invite.token))

    @override_settings(OIDC_ENABLED=True)
    def test_oidc_path_redirects_to_oidc_init(self):
        with patch('myapp.views.reverse', side_effect=self._stub_reverse):
            resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertIn(self._OIDC_INIT_URL, resp['Location'])

    @override_settings(OIDC_ENABLED=True)
    def test_oidc_path_includes_next_param(self):
        with patch('myapp.views.reverse', side_effect=self._stub_reverse):
            resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertIn(reverse('invite_complete'), resp['Location'])

    @override_settings(OIDC_ENABLED=False)
    def test_non_oidc_path_shows_password_form(self):
        resp = self.client.get(reverse('accept_invite', args=[str(self.invite.token)]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Create')

    def test_invite_complete_consumes_valid_token(self):
        user = User.objects.create_user('oidc_newuser', 'on@e.com', 'Pw123456!')
        self.client.force_login(user)
        session = self.client.session
        session['pending_invite_token'] = str(self.invite.token)
        session.save()

        resp = self.client.get(reverse('invite_complete'))
        self.assertRedirects(resp, reverse('show_items'), fetch_redirect_response=False)
        self.invite.refresh_from_db()
        self.assertIsNotNone(self.invite.used_at)
        self.assertEqual(self.invite.used_by, user)
        self.assertNotIn('pending_invite_token', self.client.session)

    def test_invite_complete_no_session_token_redirects(self):
        user = User.objects.create_user('oidc_noinvite', 'ni@e.com', 'Pw123456!')
        self.client.force_login(user)
        resp = self.client.get(reverse('invite_complete'))
        self.assertRedirects(resp, reverse('show_items'), fetch_redirect_response=False)

    def test_invite_complete_invalid_token_redirects(self):
        user = User.objects.create_user('oidc_badtoken', 'bt@e.com', 'Pw123456!')
        self.client.force_login(user)
        session = self.client.session
        session['pending_invite_token'] = str(uuid.uuid4())
        session.save()

        resp = self.client.get(reverse('invite_complete'))
        self.assertRedirects(resp, reverse('show_items'), fetch_redirect_response=False)

    def test_invite_complete_requires_login(self):
        resp = self.client.get(reverse('invite_complete'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])


class PocketIDClientTests(TestCase):
    """Unit tests for PocketIDClient — all HTTP calls are mocked."""

    def _make_client(self):
        from myapp.pocket_id import PocketIDClient
        return PocketIDClient('https://id.example.com', 'test-key')

    # ---- ping ----

    def test_ping_ok(self):
        with patch('myapp.pocket_id.requests.get') as mock_get:
            mock_get.return_value.status_code = 200
            ok, msg = self._make_client().ping()
        self.assertTrue(ok)
        self.assertEqual(msg, 'Connected')

    def test_ping_bad_key(self):
        with patch('myapp.pocket_id.requests.get') as mock_get:
            mock_get.return_value.status_code = 401
            ok, msg = self._make_client().ping()
        self.assertFalse(ok)
        self.assertIn('401', msg)

    def test_ping_timeout(self):
        import requests as req_lib
        with patch('myapp.pocket_id.requests.get', side_effect=req_lib.Timeout):
            ok, msg = self._make_client().ping()
        self.assertFalse(ok)
        self.assertIn('timed out', msg)

    # ---- create_user ----

    def test_create_user_success(self):
        with patch('myapp.pocket_id.requests.post') as mock_post:
            mock_post.return_value.status_code = 201
            mock_post.return_value.json.return_value = {'id': 'abc123', 'username': 'alice'}
            mock_post.return_value.raise_for_status = lambda: None
            result = self._make_client().create_user('alice', 'alice@example.com', 'Alice', 'Smith')
        self.assertEqual(result['id'], 'abc123')

    def test_create_user_http_error_raises(self):
        import requests as req_lib
        from myapp.pocket_id import PocketIDError
        resp = MagicMock()
        resp.status_code = 409
        resp.json.return_value = {'error': 'username taken'}
        http_exc = req_lib.HTTPError(response=resp)
        with patch('myapp.pocket_id.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.side_effect = http_exc
            with self.assertRaises(PocketIDError) as ctx:
                self._make_client().create_user('alice')
        self.assertIn('409', str(ctx.exception))

    # ---- get_ota_token ----

    def test_get_ota_token_token_key(self):
        with patch('myapp.pocket_id.requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {'token': 'mytoken'}
            mock_post.return_value.raise_for_status = lambda: None
            token = self._make_client().get_ota_token('user-id-1')
        self.assertEqual(token, 'mytoken')

    def test_get_ota_token_nested_dict(self):
        with patch('myapp.pocket_id.requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {'token': {'token': 'nested-tok'}}
            mock_post.return_value.raise_for_status = lambda: None
            token = self._make_client().get_ota_token('uid')
        self.assertEqual(token, 'nested-tok')

    def test_get_ota_token_unexpected_shape_raises(self):
        from myapp.pocket_id import PocketIDError
        with patch('myapp.pocket_id.requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {'something_else': 'x'}
            mock_post.return_value.raise_for_status = lambda: None
            with self.assertRaises(PocketIDError):
                self._make_client().get_ota_token('uid')


class ProvisionInviteViewTests(TestCase):
    """Tests for the manage_invites 'provision' action and check_pocket_id AJAX view."""

    # Patch paths: PocketIDClient is imported locally inside the view functions,
    # so we patch at the source module (myapp.pocket_id), not at myapp.views.

    def setUp(self):
        self.admin = User.objects.create_superuser('admin_prov', 'a@e.com', 'Pw123456!')
        self.client.login(username='admin_prov', password='Pw123456!')
        from myapp.models import SiteConfiguration
        self.config = SiteConfiguration.load()
        self.config.pocket_id_url = 'https://id.example.com'
        self.config.pocket_id_api_key = 'test-key'
        self.config.save()

    def test_provision_creates_invite_and_stores_chain_url(self):
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.create_user.return_value = {'id': 'uid-1'}
            inst.get_ota_token.return_value = 'ota-tok'
            with patch('django.core.mail.send_mail'):
                resp = self.client.post(reverse('manage_invites'), {
                    'action': 'provision',
                    'email': 'partner@example.com',
                    'first_name': 'Bob',
                    'last_name': 'Jones',
                    'note': 'test partner',
                })
        self.assertRedirects(resp, reverse('manage_invites'), fetch_redirect_response=False)
        from myapp.models import InviteLink
        invite = InviteLink.objects.filter(pocket_id_user_id='uid-1').first()
        self.assertIsNotNone(invite)
        self.assertEqual(invite.note, 'Bob')

    def test_provision_sets_last_provision_session_key(self):
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.create_user.return_value = {'id': 'uid-2'}
            inst.get_ota_token.return_value = 'ota-tok-2'
            with patch('django.core.mail.send_mail'):
                self.client.post(reverse('manage_invites'), {
                    'action': 'provision',
                    'email': 'x@example.com',
                    'first_name': 'Test',
                    'last_name': '',
                    'note': '',
                })
        # Follow the redirect so the session key is consumed on the GET.
        resp = self.client.get(reverse('manage_invites'))
        self.assertContains(resp, 'ota-tok-2')

    def test_provision_no_credentials_shows_error(self):
        from myapp.models import SiteConfiguration
        cfg = SiteConfiguration.load()
        cfg.pocket_id_url = ''
        cfg.pocket_id_api_key = ''
        cfg.save()
        resp = self.client.post(reverse('manage_invites'), {
            'action': 'provision',
            'email': 'y@example.com',
        })
        self.assertRedirects(resp, reverse('manage_invites'), fetch_redirect_response=False)
        resp2 = self.client.get(reverse('manage_invites'))
        self.assertContains(resp2, 'PocketID')

    def test_provision_pocket_id_error_shows_message(self):
        from myapp.pocket_id import PocketIDError
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.create_user.side_effect = PocketIDError('username taken')
            resp = self.client.post(reverse('manage_invites'), {
                'action': 'provision',
                'email': 'z@example.com',
                'first_name': 'Err',
                'last_name': '',
                'note': '',
            })
        self.assertRedirects(resp, reverse('manage_invites'), fetch_redirect_response=False)

    def test_check_pocket_id_ok(self):
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.ping.return_value = (True, 'Connected')
            inst.probe_ota.return_value = (True, 'OTA endpoint available')
            resp = self.client.get(reverse('check_pocket_id'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['message'], 'Connected')
        self.assertTrue(data['ota_ok'])

    def test_check_pocket_id_non_superuser_forbidden(self):
        regular = User.objects.create_user('reg_cp', 'r@e.com', 'Pw123456!')
        self.client.logout()
        self.client.login(username='reg_cp', password='Pw123456!')
        resp = self.client.get(reverse('check_pocket_id'))
        self.assertEqual(resp.status_code, 403)

    def test_check_pocket_id_not_configured(self):
        from myapp.models import SiteConfiguration
        cfg = SiteConfiguration.load()
        cfg.pocket_id_url = ''
        cfg.save()
        resp = self.client.get(reverse('check_pocket_id'))
        data = resp.json()
        self.assertFalse(data['ok'])

    # ── PocketID URL / endpoint contract ──────────────────────────────────────
    # PocketID 2.x serves the one-time-access login at /lc/<code> (which 307s to
    # /login/alternative/code) and lists a user's passkeys at
    # /api/users/<id>/webauthn-credentials.  Guessing either wrong fails silently:
    # an unknown SPA route just renders the login page.

    def test_build_ota_login_url_uses_lc_route_and_account_landing(self):
        from myapp.pocket_id import build_ota_login_url
        url = build_ota_login_url('https://id.example.com/', 'ota-tok')
        self.assertEqual(
            url,
            'https://id.example.com/lc/ota-tok?redirect=%2Fsettings%2Faccount',
        )

    def test_resend_ota_returns_lc_chain_url(self):
        invite = self._make_pocket_id_invite()
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            MockClient.return_value.get_ota_token.return_value = 'fresh-tok'
            resp = self.client.post(reverse('resend_invite_ota'), {'invite_id': invite.pk})
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(
            data['chain_url'],
            'https://id.example.com/lc/fresh-tok?redirect=%2Fsettings%2Faccount',
        )

    def test_get_user_passkeys_calls_webauthn_credentials_endpoint(self):
        from myapp.pocket_id import PocketIDClient
        with patch('myapp.pocket_id.requests.get') as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = [{'id': 'c1', 'name': 'Pixel'}]
            passkeys = PocketIDClient('https://id.example.com', 'k').get_user_passkeys('uid-9')
        self.assertEqual(
            mock_get.call_args[0][0],
            'https://id.example.com/api/users/uid-9/webauthn-credentials',
        )
        self.assertEqual(passkeys, [{'id': 'c1', 'name': 'Pixel'}])

    # ── check_passkey_status ──────────────────────────────────────────────────

    def _make_pocket_id_invite(self):
        from myapp.models import InviteLink
        return InviteLink.objects.create(
            created_by=self.admin,
            pocket_id_user_id='pid-user-1',
        )

    def test_check_passkey_status_has_passkeys(self):
        invite = self._make_pocket_id_invite()
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.get_user_passkeys.return_value = [
                {'name': 'iPhone', 'createdAt': '2024-01-01T00:00:00Z'},
            ]
            resp = self.client.get(reverse('check_passkey_status'), {'invite_id': invite.pk})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['passkeys'][0]['name'], 'iPhone')

    def test_check_passkey_status_no_passkeys(self):
        invite = self._make_pocket_id_invite()
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.get_user_passkeys.return_value = []
            resp = self.client.get(reverse('check_passkey_status'), {'invite_id': invite.pk})
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 0)

    def test_check_passkey_status_non_superuser_forbidden(self):
        regular = User.objects.create_user('reg_ps', 'ps@e.com', 'Pw123456!')
        self.client.logout()
        self.client.login(username='reg_ps', password='Pw123456!')
        resp = self.client.get(reverse('check_passkey_status'), {'invite_id': 1})
        self.assertEqual(resp.status_code, 403)

    def test_check_passkey_status_no_pocket_id_user(self):
        from myapp.models import InviteLink
        invite = InviteLink.objects.create(created_by=self.admin)
        resp = self.client.get(reverse('check_passkey_status'), {'invite_id': invite.pk})
        self.assertEqual(resp.status_code, 400)

    def test_check_passkey_status_pocket_id_error(self):
        from myapp.pocket_id import PocketIDError
        invite = self._make_pocket_id_invite()
        with patch('myapp.pocket_id.PocketIDClient') as MockClient:
            inst = MockClient.return_value
            inst.get_user_passkeys.side_effect = PocketIDError('API down')
            resp = self.client.get(reverse('check_passkey_status'), {'invite_id': invite.pk})
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertIn('API down', data['error'])


class ManageUsersViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('admin_mu', 'e@e.com', 'Pw123456!')
        self.user = User.objects.create_user('regular_mu', 'f@e.com', 'Pw123456!')
        self.client.login(username='admin_mu', password='Pw123456!')

    def test_get_page_200(self):
        resp = self.client.get(reverse('manage_users'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'regular_mu')

    def test_non_superuser_forbidden(self):
        self.client.logout()
        self.client.login(username='regular_mu', password='Pw123456!')
        resp = self.client.get(reverse('manage_users'))
        self.assertNotEqual(resp.status_code, 200)

    def test_promote_user(self):
        resp = self.client.post(reverse('toggle_user_superuser'), {
            'user_id': self.user.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_superuser)

    def test_demote_user(self):
        self.user.is_superuser = True
        self.user.save()
        resp = self.client.post(reverse('toggle_user_superuser'), {
            'user_id': self.user.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)

    def test_cannot_toggle_self(self):
        resp = self.client.post(reverse('toggle_user_superuser'), {
            'user_id': self.admin.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)


class UnlinkOIDCViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('oidc_user', 'g@e.com', 'Pw123456!')
        from myapp.models import UserProfile
        self.profile, _ = UserProfile.objects.get_or_create(user=self.user)
        self.profile.oidc_sub = 'sub|abc123'
        self.profile.oidc_avatar_url = 'https://example.com/avatar.jpg'
        self.profile.oidc_last_login = timezone.now()
        self.profile.save()
        self.client.login(username='oidc_user', password='Pw123456!')

    def test_unlink_clears_oidc_fields(self):
        resp = self.client.post(reverse('unlink_oidc'))
        self.assertEqual(resp.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.oidc_sub, '')
        self.assertEqual(self.profile.oidc_avatar_url, '')
        self.assertIsNone(self.profile.oidc_last_login)

    def test_get_not_allowed(self):
        resp = self.client.get(reverse('unlink_oidc'))
        self.assertEqual(resp.status_code, 405)

    def test_unauthenticated_redirected(self):
        self.client.logout()
        resp = self.client.post(reverse('unlink_oidc'))
        self.assertNotEqual(resp.status_code, 200)
        self.assertNotEqual(resp.status_code, 405)
