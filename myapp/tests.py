import json
import os
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from django.core.files.uploadedfile import SimpleUploadedFile

from .forms import ItemForm, SiteConfigurationForm, TagForm, WalletForm
from .merchant_logos import (
    fetch_merchant_logo,
    get_cached_balance_check_url,
    get_cached_logo,
    get_cached_logos_for_issuers,
    guess_domain,
    remember_balance_check_url,
)
from .ics_calendar import _escape_text, _fold_line, build_ics_calendar
from .models import Document, Item, MerchantProfile, SiteConfiguration, Tag, Transaction, UpdateCheckStatus, UserPreference, UserProfile, Wallet
from .portainer import PortainerRedeployError, trigger_redeploy
from .test_utils import set_site_config
from .tasks import check_for_update_task, fetch_merchant_logo_task
from .update_check import _is_newer, _parse_version, check_for_update
from .utils import generate_code_image_base64


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
        self.assertRedirects(response, reverse('show_items'))
        item = Item.objects.get(name='Flight Voucher')
        self.assertEqual(item.wallet, self.wallet)
        self.assertEqual(item.notes, 'Show at gate.')
        tag_names = set(item.tags.values_list('name', flat=True))
        self.assertEqual(tag_names, {'discount', 'summer'})


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
        self.assertRedirects(response, reverse('show_items'))
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


class MerchantLogoServiceTests(TestCase):
    def test_guess_domain_strips_non_alnum_and_lowercases(self):
        self.assertEqual(guess_domain('Amazon'), 'amazon.com')
        self.assertEqual(guess_domain("Trader Joe's"), 'traderjoes.com')

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
        self.assertEqual(profile.logo_url, 'https://logo.clearbit.com/amazon.com')
        self.assertEqual(profile.domain, 'amazon.com')
        self.assertIsNotNone(profile.fetched_at)
        mock_get.assert_called_once()

    @patch('myapp.merchant_logos.requests.get')
    def test_fetch_merchant_logo_falls_back_to_second_source(self, mock_get):
        mock_get.side_effect = [MagicMock(status_code=404), MagicMock(status_code=200)]
        profile = fetch_merchant_logo('Amazon')
        self.assertEqual(profile.logo_url, 'https://www.google.com/s2/favicons?sz=64&domain=amazon.com')
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
        mock_fetch.assert_called_once_with('Amazon')

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
        self.assertRedirects(response, reverse('show_items'))
        mock_delay.assert_called_once_with('Airline')

    @patch('myapp.views.fetch_merchant_logo_task.delay', side_effect=RuntimeError('broker down'))
    def test_create_item_survives_broker_outage(self, mock_delay):
        response = self.client.post(reverse('create_item'), {
            'type': 'voucher', 'name': 'Flight Voucher', 'issuer': 'Airline', 'redeem_code': 'FLY100',
            'value': '100.00', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
            'issue_date': date.today().isoformat(),
        })
        self.assertRedirects(response, reverse('show_items'))
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
        mock_delay.assert_called_once_with('New Issuer')

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

    def test_create_item_ocr_enabled_shows_scan_section(self):
        set_site_config(ocr_backend='tesseract')
        response = self.client.get(reverse('create_item'))
        self.assertTrue(response.context['ocr_enabled'])
        self.assertContains(response, 'aiScanSection')

    def test_edit_item_reflects_ocr_setting(self):
        item = make_item(self.user)
        set_site_config(ocr_backend='claude')
        response = self.client.get(reverse('edit_item', args=[item.id]))
        self.assertTrue(response.context['ocr_enabled'])
        self.assertContains(response, 'aiScanSection')

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

    def test_non_collaborator_cannot_upload_view_or_delete(self):
        document = Document.objects.create(item=self.item, file=make_upload())
        self.client.logout()
        self.client.login(username='bob', password='pw12345!')

        upload_response = self.client.post(reverse('upload_document', args=[self.item.id]), {'file': make_upload()})
        self.assertEqual(upload_response.status_code, 403)

        download_response = self.client.get(reverse('download_document', args=[document.id]))
        self.assertEqual(download_response.status_code, 403)

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

    def test_inventory_cards_render_share_button(self):
        make_item(self.user)
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'share-voucher-btn')


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
        self.assertRedirects(response, reverse('show_items'))
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

    @patch('myapp.tasks.check_for_update')
    def test_task_delegates_to_service(self, mock_check):
        set_site_config(update_check_enabled=True)
        check_for_update_task()
        mock_check.assert_called_once()


class UpdateCheckContextProcessorTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        self.regular_user = User.objects.create_user(username='alice', password='pw12345!')
        UpdateCheckStatus.objects.create(pk=1, latest_version='v1.1.0', update_available=True)

    def test_banner_shown_to_superuser(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'A newer version')

    def test_banner_hidden_from_regular_user(self):
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'A newer version')

    def test_banner_hidden_when_no_update_available(self):
        UpdateCheckStatus.objects.filter(pk=1).update(update_available=False)
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'A newer version')


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


class PortainerRedeployBannerTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username='admin', password='pw12345!', email='a@example.com')
        UpdateCheckStatus.objects.create(pk=1, latest_version='v1.1.0', update_available=True)
        self.client.login(username='admin', password='pw12345!')

    def test_button_shown_when_configured(self):
        set_site_config(portainer_webhook_url='https://portainer.example.com/api/webhooks/abc123')
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'Redeploy now')

    def test_button_hidden_when_not_configured(self):
        set_site_config(portainer_webhook_url='')
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, 'Redeploy now')


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


def _site_config_form_data(**overrides):
    data = {
        'expiry_threshold_days': 30, 'expiry_last_notification_days': 7,
        'ntfy_default_server': 'https://ntfy.sh',
        'webpush_vapid_public_key': '', 'webpush_vapid_private_key': '',
        'webpush_vapid_claims_email': 'mailto:admin@example.com',
        'merchant_logos_enabled': True,
        'ocr_backend': 'none', 'anthropic_api_key': '', 'anthropic_ocr_model': 'claude-sonnet-5',
        'openai_api_key': '', 'openai_ocr_model': 'gpt-4o-mini',
        'pkpass_cert_path': '', 'pkpass_cert_password': '', 'pkpass_wwdr_cert_path': '',
        'pkpass_team_id': '', 'pkpass_pass_type_id': '', 'pkpass_organization_name': 'VoucherVault Plus+',
        'google_wallet_service_account_key_path': '', 'google_wallet_issuer_id': '', 'google_wallet_class_id': '',
        'update_check_enabled': True, 'update_check_repo': 'gregbtm/VoucherVault',
        'portainer_webhook_url': '',
        'scheduled_backup_enabled': True, 'backup_retention_count': 7,
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
        form = SiteConfigurationForm(data=_site_config_form_data(anthropic_api_key='new-secret'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.anthropic_api_key, 'new-secret')

    def test_non_secret_fields_save_normally(self):
        form = SiteConfigurationForm(data=_site_config_form_data(expiry_threshold_days=45, ocr_backend='tesseract'), instance=self.config)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.expiry_threshold_days, 45)
        self.assertEqual(saved.ocr_backend, 'tesseract')


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
        self.assertContains(response, 'Save Site Settings')

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

    def test_nav_link_shown_only_to_superuser(self):
        self.client.login(username='admin', password='pw12345!')
        response = self.client.get(reverse('show_items'))
        self.assertContains(response, 'Site Settings')

        self.client.logout()
        self.client.login(username='alice', password='pw12345!')
        response = self.client.get(reverse('show_items'))
        self.assertNotContains(response, 'Site Settings')


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
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact',
            'default_currency': 'GBP', 'keep_screen_awake': 'on', 'offline_cache_enabled': 'on',
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')

    def test_disabling_offline_cache_adds_purge_signal(self):
        prefs, _ = UserPreference.objects.get_or_create(user=self.user)
        prefs.offline_cache_enabled = True
        prefs.save()

        response = self.client.post(reverse('update_user_preferences'), data={
            'show_expiry_date': 'on', 'show_value': 'on', 'show_description': 'on',
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact',
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
            'sort_by': 'expiry_date', 'sort_order': 'asc', 'view_mode': 'compact',
            'default_currency': 'GBP', 'keep_screen_awake': 'on',
            # still leaving offline_cache_enabled off - no transition, no purge needed
        })
        self.assertRedirects(response, reverse('show_items') + '?prefs_saved=1')


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
