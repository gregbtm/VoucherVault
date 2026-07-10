from datetime import date, timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .forms import ItemForm, TagForm, WalletForm
from .models import Item, Tag, Wallet


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
        self.assertEqual(response.context['expiring_soon_list'], [])
