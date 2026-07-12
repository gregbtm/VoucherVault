import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from imports.models import ImportJob
from myapp.models import Item, ItemShare, MerchantProfile, Tag, Transaction, Wallet
from myapp.test_utils import set_site_config
from notify.models import NotificationLog, NotificationRule


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


class AuthenticationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_items_list_requires_authentication(self):
        response = self.client.get('/api/v1/items/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_obtain_token(self):
        response = self.client.post('/api/v1/auth/token/', {'username': 'alice', 'password': 'pw12345!'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('token', response.data)
        token = Token.objects.get(user=self.user)
        self.assertEqual(response.data['token'], token.key)

    def test_token_authenticates_requests(self):
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = self.client.get('/api/v1/items/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_schema_and_docs_are_reachable(self):
        self.assertEqual(self.client.get('/api/v1/schema/').status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get('/api/v1/docs/').status_code, status.HTTP_200_OK)


class ItemCrudTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_create_item_generates_qr_code(self):
        payload = {
            'type': 'voucher',
            'name': 'Coffee Shop',
            'redeem_code': 'SAVE10',
            'issuer': 'Cafe Corp',
            'value': '10.00',
            'currency': 'EUR',
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = Item.objects.get(pk=response.data['id'])
        self.assertEqual(item.user, self.user)
        self.assertTrue(item.qr_code_base64)
        # expiry_date left unset -> defaults ~50 years out
        self.assertGreater(item.expiry_date, date.today() + timedelta(days=365 * 40))

    def test_loyaltycard_requires_zero_value(self):
        payload = {
            'type': 'loyaltycard',
            'name': 'Rewards Card',
            'redeem_code': 'LOY001',
            'issuer': 'Store',
            'value': '5.00',
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_giftcard_requires_positive_value(self):
        payload = {
            'type': 'giftcard',
            'name': 'Gift Card',
            'redeem_code': 'GC001',
            'issuer': 'Store',
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_retrieve_update_delete(self):
        item = make_item(self.user)

        list_response = self.client.get('/api/v1/items/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data['count'], 1)

        detail_response = self.client.get(f'/api/v1/items/{item.id}/')
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data['name'], 'Test Voucher')

        original_qr = item.qr_code_base64
        update_response = self.client.patch(f'/api/v1/items/{item.id}/', {'redeem_code': 'NEWCODE'})
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.redeem_code, 'NEWCODE')
        self.assertNotEqual(item.qr_code_base64, original_qr)

        delete_response = self.client.delete(f'/api/v1/items/{item.id}/')
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Item.objects.filter(pk=item.id).exists())

    def test_redeem_action(self):
        item = make_item(self.user)
        response = self.client.post(f'/api/v1/items/{item.id}/redeem/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertTrue(item.is_used)

    def test_filter_and_search(self):
        make_item(self.user, name='Alpha Coupon', type='coupon', value_type='percentage', value='10')
        make_item(self.user, name='Beta Voucher', redeem_code='XYZ999')

        filtered = self.client.get('/api/v1/items/?type=coupon')
        self.assertEqual(filtered.data['count'], 1)

        searched = self.client.get('/api/v1/items/?search=Beta')
        self.assertEqual(searched.data['count'], 1)
        self.assertEqual(searched.data['results'][0]['name'], 'Beta Voucher')


class Phase11FieldsApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_card_number_round_trips(self):
        response = self.client.post('/api/v1/items/', {
            'type': 'loyaltycard', 'name': 'Card', 'issuer': 'Shop', 'redeem_code': 'ABC',
            'card_number': 'MEMBER-42', 'value': '0', 'currency': 'EUR', 'code_type': 'qrcode', 'value_type': 'money',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['card_number'], 'MEMBER-42')

    def test_is_archived_filter(self):
        make_item(self.user, name='Archived', is_archived=True)
        make_item(self.user, name='Visible', redeem_code='OTHER')

        response = self.client.get('/api/v1/items/?is_archived=true')
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Archived')

    def test_last_used_at_is_read_only(self):
        item = make_item(self.user)
        response = self.client.patch(f'/api/v1/items/{item.id}/', {'last_used_at': '2020-01-01T00:00:00Z'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertIsNone(item.last_used_at)


class BalanceCheckUrlApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.user)

    def test_create_item_remembers_balance_check_url(self):
        response = self.client.post('/api/v1/items/', {
            'type': 'giftcard', 'name': 'Tesco Card', 'issuer': 'Tesco', 'redeem_code': 'GC1',
            'value': '25.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
            'balance_check_url': 'https://www.tesco.com/gift-cards/balance',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['balance_check_url'], 'https://www.tesco.com/gift-cards/balance')
        self.assertEqual(
            MerchantProfile.objects.get(name__iexact='Tesco').balance_check_url,
            'https://www.tesco.com/gift-cards/balance',
        )

    def test_update_item_remembers_balance_check_url(self):
        item = make_item(self.user, type='giftcard', issuer='Amazon')
        response = self.client.patch(f'/api/v1/items/{item.id}/', {
            'balance_check_url': 'https://www.amazon.co.uk/gc/balance',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            MerchantProfile.objects.get(name__iexact='Amazon').balance_check_url,
            'https://www.amazon.co.uk/gc/balance',
        )

    def test_merchant_profile_serializer_exposes_balance_check_url(self):
        MerchantProfile.objects.create(name='Tesco', balance_check_url='https://www.tesco.com/gift-cards/balance')
        response = self.client.get('/api/v1/merchants/')
        self.assertEqual(response.data['results'][0]['balance_check_url'], 'https://www.tesco.com/gift-cards/balance')


class CrossUserIsolationTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.alice_item = make_item(self.alice)
        self.client.force_authenticate(user=self.bob)

    def test_list_excludes_other_users_items(self):
        response = self.client.get('/api/v1/items/')
        self.assertEqual(response.data['count'], 0)

    def test_detail_of_other_users_item_is_not_found(self):
        response = self.client.get(f'/api/v1/items/{self.alice_item.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_delete_other_users_item(self):
        response = self.client.delete(f'/api/v1/items/{self.alice_item.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Item.objects.filter(pk=self.alice_item.id).exists())

    def test_cannot_add_transaction_to_other_users_item(self):
        response = self.client.post(
            f'/api/v1/items/{self.alice_item.id}/transactions/', {'description': 'hack', 'value': '-1.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_preferences_and_profile_are_per_user(self):
        self.client.patch('/api/v1/preferences/', {'default_currency': 'USD'})
        self.client.force_authenticate(user=self.alice)
        response = self.client.get('/api/v1/preferences/')
        self.assertNotEqual(response.data['default_currency'], 'USD')


class TransactionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.user)
        self.item = make_item(self.user, value='10.00')

    def test_create_transaction_requires_negative_value(self):
        response = self.client.post(f'/api/v1/items/{self.item.id}/transactions/', {'description': 'Spend', 'value': '5.00'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_transaction_rejects_overspend(self):
        response = self.client.post(
            f'/api/v1/items/{self.item.id}/transactions/', {'description': 'Spend', 'value': '-20.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_and_list_transactions(self):
        response = self.client.post(
            f'/api/v1/items/{self.item.id}/transactions/', {'description': 'Spend', 'value': '-4.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        list_response = self.client.get(f'/api/v1/items/{self.item.id}/transactions/')
        self.assertEqual(len(list_response.data), 1)

    def test_update_transaction_via_top_level_route(self):
        txn = Transaction.objects.create(item=self.item, description='Spend', value='-2.00')
        response = self.client.patch(f'/api/v1/transactions/{txn.id}/', {'value': '-3.00'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        txn.refresh_from_db()
        self.assertEqual(str(txn.value), '-3.00')

    def test_other_users_transactions_are_not_listed(self):
        bob = User.objects.create_user(username='bob', password='pw12345!')
        bob_item = make_item(bob)
        Transaction.objects.create(item=bob_item, description='Bob spend', value='-1.00')

        response = self.client.get('/api/v1/transactions/')
        self.assertEqual(len(response.data['results']), 0)


class ItemShareTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        self.item = make_item(self.alice)

    def test_share_item_with_existing_user(self):
        response = self.client.post(f'/api/v1/items/{self.item.id}/shares/', {'username': 'bob'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(ItemShare.objects.filter(item=self.item, shared_with_user=self.bob).exists())

    def test_cannot_share_with_unknown_user(self):
        response = self.client.post(f'/api/v1/items/{self.item.id}/shares/', {'username': 'ghost'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_share_with_self(self):
        response = self.client.post(f'/api/v1/items/{self.item.id}/shares/', {'username': 'alice'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unshare_item(self):
        share = ItemShare.objects.create(item=self.item, shared_with_user=self.bob, shared_by=self.alice)
        response = self.client.delete(f'/api/v1/items/{self.item.id}/shares/{share.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemShare.objects.filter(pk=share.id).exists())


class WebhookEventApiWiringTests(APITestCase):
    """Confirms the DRF API fires the same Phase 12.2 lifecycle events as the web UI."""

    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    @patch('api.views.notify_item_created')
    def test_create_item_fires_item_created(self, mock_notify):
        response = self.client.post('/api/v1/items/', {
            'type': 'voucher', 'name': 'API Voucher', 'issuer': 'Shop', 'redeem_code': 'API100',
            'value': '10.00', 'currency': 'GBP', 'code_type': 'qrcode', 'value_type': 'money',
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        mock_notify.assert_called_once()

    @patch('api.views.notify_item_used')
    def test_redeem_fires_item_used(self, mock_notify):
        item = make_item(self.alice)
        response = self.client.post(f'/api/v1/items/{item.id}/redeem/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_notify.assert_called_once()

    @patch('api.views.notify_balance_changed')
    def test_add_transaction_fires_balance_changed(self, mock_notify):
        item = make_item(self.alice, value='10.00')
        response = self.client.post(
            f'/api/v1/items/{item.id}/transactions/', {'description': 'Spend', 'value': '-4.00'}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args[0][0], item)

    @patch('api.views.notify_item_shared')
    def test_share_fires_item_shared(self, mock_notify):
        item = make_item(self.alice)
        response = self.client.post(f'/api/v1/items/{item.id}/shares/', {'username': 'bob'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        mock_notify.assert_called_once_with(item, 'bob')


class WalletApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_create_and_list_wallet(self):
        response = self.client.post('/api/v1/wallets/', {'name': 'Travel', 'color': '#123456'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['item_count'], 0)

        list_response = self.client.get('/api/v1/wallets/')
        self.assertEqual(list_response.data['count'], 1)

    def test_wallet_item_count_updates(self):
        wallet = Wallet.objects.create(user=self.alice, name='Travel')
        make_item(self.alice, wallet=wallet)
        response = self.client.get(f'/api/v1/wallets/{wallet.id}/')
        self.assertEqual(response.data['item_count'], 1)

    def test_wallet_items_action_lists_only_its_items(self):
        wallet = Wallet.objects.create(user=self.alice, name='Travel')
        in_wallet = make_item(self.alice, wallet=wallet, name='In Wallet')
        make_item(self.alice, name='No Wallet', redeem_code='OTHER')

        response = self.client.get(f'/api/v1/wallets/{wallet.id}/items/')
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], str(in_wallet.id))

    def test_duplicate_wallet_name_rejected(self):
        Wallet.objects.create(user=self.alice, name='Travel')
        response = self.client.post('/api/v1/wallets/', {'name': 'Travel'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_access_another_users_wallet(self):
        bob_wallet = Wallet.objects.create(user=self.bob, name='Groceries')
        self.assertEqual(self.client.get(f'/api/v1/wallets/{bob_wallet.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(f'/api/v1/wallets/{bob_wallet.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Wallet.objects.filter(pk=bob_wallet.pk).exists())

    def test_deleting_wallet_unassigns_items(self):
        wallet = Wallet.objects.create(user=self.alice, name='Travel')
        item = make_item(self.alice, wallet=wallet)
        response = self.client.delete(f'/api/v1/wallets/{wallet.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        item.refresh_from_db()
        self.assertIsNone(item.wallet)


class TagApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_create_and_list_tag(self):
        response = self.client.post('/api/v1/tags/', {'name': 'discount', 'color': '#654321'})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        list_response = self.client.get('/api/v1/tags/')
        self.assertEqual(list_response.data['count'], 1)

    def test_cannot_access_another_users_tag(self):
        bob_tag = Tag.objects.create(user=self.bob, name='discount')
        self.assertEqual(self.client.get(f'/api/v1/tags/{bob_tag.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(f'/api/v1/tags/{bob_tag.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Tag.objects.filter(pk=bob_tag.pk).exists())


class ItemWalletAndTagsTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        self.wallet = Wallet.objects.create(user=self.alice, name='Travel')
        self.tag = Tag.objects.create(user=self.alice, name='discount')

    def test_create_item_with_wallet_and_tags(self):
        payload = {
            'type': 'voucher', 'name': 'Flight Voucher', 'redeem_code': 'FLY100', 'issuer': 'Airline',
            'value': '100.00', 'wallet': self.wallet.id, 'tag_ids': [self.tag.id], 'notes': 'Show at gate.',
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = Item.objects.get(pk=response.data['id'])
        self.assertEqual(item.wallet, self.wallet)
        self.assertEqual(item.notes, 'Show at gate.')
        self.assertEqual(list(item.tags.values_list('id', flat=True)), [self.tag.id])
        self.assertEqual(response.data['wallet_name'], 'Travel')
        self.assertEqual(response.data['tags'][0]['name'], 'discount')

    def test_cannot_assign_another_users_wallet_to_item(self):
        bob_wallet = Wallet.objects.create(user=self.bob, name='Groceries')
        payload = {
            'type': 'voucher', 'name': 'X', 'redeem_code': 'X', 'issuer': 'X',
            'value': '5.00', 'wallet': bob_wallet.id,
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_assign_another_users_tag_to_item(self):
        bob_tag = Tag.objects.create(user=self.bob, name='discount')
        payload = {
            'type': 'voucher', 'name': 'X', 'redeem_code': 'X', 'issuer': 'X',
            'value': '5.00', 'tag_ids': [bob_tag.id],
        }
        response = self.client.post('/api/v1/items/', payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_clear_tags_with_empty_list(self):
        item = make_item(self.alice)
        item.tags.add(self.tag)
        response = self.client.patch(f'/api/v1/items/{item.id}/', {'tag_ids': []}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)

    def test_filter_items_by_wallet_and_tag(self):
        make_item(self.alice, name='In Wallet', wallet=self.wallet)
        tagged = make_item(self.alice, name='Tagged', redeem_code='TAG1')
        tagged.tags.add(self.tag)
        make_item(self.alice, name='Neither', redeem_code='NEITHER')

        by_wallet = self.client.get(f'/api/v1/items/?wallet={self.wallet.id}')
        self.assertEqual(by_wallet.data['count'], 1)

        by_tag = self.client.get(f'/api/v1/items/?tags={self.tag.id}')
        self.assertEqual(by_tag.data['count'], 1)
        self.assertEqual(by_tag.data['results'][0]['name'], 'Tagged')

    def test_item_serializer_exposes_notify_days_before(self):
        response = self.client.post('/api/v1/items/', {
            'type': 'voucher', 'name': 'X', 'redeem_code': 'X', 'issuer': 'X',
            'value': '5.00', 'notify_days_before': 14,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['notify_days_before'], 14)


class NotificationRuleApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_create_ntfy_rule(self):
        payload = {
            'name': 'My ntfy', 'backend': 'ntfy', 'enabled': True,
            'event_types': ['expiry_warning'],
            'config': {'server': 'https://ntfy.example.com', 'topic': 'vv'},
        }
        response = self.client.post('/api/v1/notifications/rules/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(NotificationRule.objects.filter(user=self.alice, name='My ntfy').exists())

    def test_create_rule_rejects_incomplete_config(self):
        payload = {
            'name': 'Bad', 'backend': 'webhook', 'enabled': True,
            'event_types': ['expiry_warning'], 'config': {},
        }
        response = self.client.post('/api/v1/notifications/rules/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_rule_rejects_invalid_event_type(self):
        payload = {
            'name': 'Bad', 'backend': 'webhook', 'enabled': True,
            'event_types': ['not_a_real_event'], 'config': {'url': 'https://example.com'},
        }
        response = self.client.post('/api/v1/notifications/rules/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_name_rejected(self):
        NotificationRule.objects.create(user=self.alice, name='dup', backend='ntfy', config={'server': 'https://ntfy.example.com', 'topic': 'vv'})
        payload = {
            'name': 'dup', 'backend': 'webhook', 'enabled': True,
            'event_types': ['expiry_warning'], 'config': {'url': 'https://example.com'},
        }
        response = self.client.post('/api/v1/notifications/rules/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_access_another_users_rule(self):
        bob_rule = NotificationRule.objects.create(
            user=self.bob, name='bob rule', backend='ntfy',
            config={'server': 'https://ntfy.example.com', 'topic': 'vv'}, event_types=['expiry_warning'],
        )
        self.assertEqual(self.client.get(f'/api/v1/notifications/rules/{bob_rule.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(f'/api/v1/notifications/rules/{bob_rule.id}/').status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(NotificationRule.objects.filter(pk=bob_rule.pk).exists())

    @patch('api.views.send_test_notification', return_value=(True, ''))
    def test_test_action_success(self, mock_send):
        rule = NotificationRule.objects.create(
            user=self.alice, name='r', backend='ntfy',
            config={'server': 'https://ntfy.example.com', 'topic': 'vv'}, event_types=['expiry_warning'],
        )
        response = self.client.post(f'/api/v1/notifications/rules/{rule.id}/test/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])
        mock_send.assert_called_once_with(rule)

    @patch('api.views.send_test_notification', return_value=(False, 'boom'))
    def test_test_action_failure_returns_502(self, mock_send):
        rule = NotificationRule.objects.create(
            user=self.alice, name='r', backend='ntfy',
            config={'server': 'https://ntfy.example.com', 'topic': 'vv'}, event_types=['expiry_warning'],
        )
        response = self.client.post(f'/api/v1/notifications/rules/{rule.id}/test/')
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertFalse(response.data['success'])

    def test_cannot_test_another_users_rule(self):
        bob_rule = NotificationRule.objects.create(
            user=self.bob, name='bob rule', backend='ntfy',
            config={'server': 'https://ntfy.example.com', 'topic': 'vv'}, event_types=['expiry_warning'],
        )
        response = self.client.post(f'/api/v1/notifications/rules/{bob_rule.id}/test/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class NotificationLogApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_log_only_shows_own_entries(self):
        NotificationLog.objects.create(user=self.alice, event_type='test', success=True)
        NotificationLog.objects.create(user=self.bob, event_type='test', success=True)
        response = self.client.get('/api/v1/notifications/log/')
        self.assertEqual(response.data['count'], 1)

    def test_log_is_read_only(self):
        response = self.client.post('/api/v1/notifications/log/', {'event_type': 'test', 'success': True})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


CATIMA_SAMPLE = (
    'Group,Description,Note,Card Number,EAN Barcode ID,Card Type,Expiry,Balance,Balance Type,Colour,Star\n'
    'Supermarkets,Tesco Clubcard,My loyalty card,1234567890,,QR_CODE,,0,,,1\n'
)


class ImportApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    @patch('api.views.process_import_job.delay')
    def test_upload_creates_job_and_dispatches_task(self, mock_delay):
        upload = SimpleUploadedFile('catima.csv', CATIMA_SAMPLE.encode('utf-8'))
        response = self.client.post('/api/v1/imports/upload/', {'source_type': 'catima_csv', 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        job = ImportJob.objects.get(user=self.alice)
        self.assertEqual(response.data['id'], str(job.id))
        mock_delay.assert_called_once_with(str(job.id))

    def test_upload_rejects_invalid_source_type(self):
        upload = SimpleUploadedFile('x.csv', b'a,b\n1,2\n')
        response = self.client.post('/api/v1/imports/upload/', {'source_type': 'nope', 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('api.views.process_import_job.delay', side_effect=RuntimeError('Retry limit exceeded'))
    def test_broker_unreachable_fails_gracefully(self, mock_delay):
        upload = SimpleUploadedFile('catima.csv', CATIMA_SAMPLE.encode('utf-8'))
        response = self.client.post('/api/v1/imports/upload/', {'source_type': 'catima_csv', 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data['status'], 'failed')

    def test_preview_does_not_create_items_or_job(self):
        upload = SimpleUploadedFile('catima.csv', CATIMA_SAMPLE.encode('utf-8'))
        response = self.client.post('/api/v1/imports/preview/', {'source_type': 'catima_csv', 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['row_count'], 1)
        self.assertEqual(response.data['rows'][0]['name'], 'Tesco Clubcard')
        self.assertEqual(ImportJob.objects.count(), 0)
        self.assertEqual(Item.objects.count(), 0)

    def test_job_status_polling(self):
        job = ImportJob.objects.create(user=self.alice, source_type='catima_csv', file=SimpleUploadedFile('x.csv', b'x'), status='complete', imported_count=3)
        response = self.client.get(f'/api/v1/imports/jobs/{job.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['imported_count'], 3)

    def test_cannot_view_another_users_job(self):
        bob_job = ImportJob.objects.create(user=self.bob, source_type='catima_csv', file=SimpleUploadedFile('x.csv', b'x'))
        response = self.client.get(f'/api/v1/imports/jobs/{bob_job.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_job_list_only_shows_own_jobs(self):
        ImportJob.objects.create(user=self.bob, source_type='catima_csv', file=SimpleUploadedFile('x.csv', b'x'))
        response = self.client.get('/api/v1/imports/jobs/')
        self.assertEqual(response.data['count'], 0)


class ExportApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        make_item(self.alice, name='Alice Item')
        make_item(self.bob, name='Bob Item', redeem_code='BOBCODE')

    def test_csv_export_only_own_items(self):
        response = self.client.get('/api/v1/exports/csv/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode()
        self.assertIn('Alice Item', content)
        self.assertNotIn('Bob Item', content)

    def test_json_export_only_own_items(self):
        response = self.client.get('/api/v1/exports/json/')
        data = json.loads(response.content)
        self.assertEqual([row['name'] for row in data], ['Alice Item'])

    def test_exports_require_authentication(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/v1/exports/csv/').status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get('/api/v1/exports/json/').status_code, status.HTTP_401_UNAUTHORIZED)

    def test_full_backup_export_and_restore_round_trip(self):
        download = self.client.get('/api/v1/exports/full-backup/')
        self.assertEqual(download.status_code, status.HTTP_200_OK)
        self.assertEqual(download['Content-Type'], 'application/zip')

        self.client.force_authenticate(user=self.bob)
        upload = SimpleUploadedFile('backup.zip', download.content, content_type='application/zip')
        response = self.client.post('/api/v1/imports/full-backup/', {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['imported_count'], 1)
        self.assertTrue(Item.objects.filter(user=self.bob, name='Alice Item').exists())


class AnalyticsApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        self.wallet = Wallet.objects.create(user=self.alice, name='Travel', color='#4154f1')

    def test_summary_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/v1/analytics/summary/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_summary_only_reflects_own_items(self):
        make_item(self.alice, name='Alice Item', wallet=self.wallet, value='25.00', currency='EUR',
                   expiry_date=date.today() + timedelta(days=3))
        make_item(self.bob, name='Bob Item', redeem_code='BOBCODE')

        response = self.client.get('/api/v1/analytics/summary/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total_items'], 1)
        self.assertEqual(response.data['expiring_7_days'], 1)
        self.assertEqual(response.data['value_by_currency'], {'EUR': '25.00'})
        self.assertEqual(response.data['at_risk_value_by_currency'], {'EUR': '25.00'})
        self.assertEqual(response.data['by_wallet'][0]['name'], 'Travel')

    def test_expiry_timeline_only_reflects_own_items(self):
        target_date = date.today() + timedelta(days=15)
        make_item(self.alice, name='Alice Item', expiry_date=target_date)
        make_item(self.bob, name='Bob Item', redeem_code='BOBCODE', expiry_date=target_date)

        response = self.client.get('/api/v1/analytics/expiry-timeline/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        key = target_date.isoformat()
        self.assertIn(key, response.data)
        self.assertEqual(len(response.data[key]), 1)
        self.assertEqual(response.data[key][0]['name'], 'Alice Item')

    def test_expiry_timeline_months_param_bounds(self):
        response = self.client.get('/api/v1/analytics/expiry-timeline/?months=notanumber')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get('/api/v1/analytics/expiry-timeline/?months=1')
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class MerchantProfileApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/v1/merchants/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_returns_cached_profiles(self):
        MerchantProfile.objects.create(name='Amazon', domain='amazon.com', logo_url='https://logo.clearbit.com/amazon.com')
        response = self.client.get('/api/v1/merchants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Amazon')

    def test_read_only(self):
        response = self.client.post('/api/v1/merchants/', {'name': 'Amazon'})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


def _tiny_image_upload(name='voucher.png', content_type='image/png', size=None):
    content = b'0' * size if size else b'\x89PNG\r\n\x1a\n' + b'0' * 100
    return SimpleUploadedFile(name, content, content_type=content_type)


class OCRExtractApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.client.force_authenticate(user=self.alice)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post('/api/v1/ocr/extract/', {'image': _tiny_image_upload()}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_disabled_by_default(self):
        set_site_config(ocr_backend='none')
        response = self.client.post('/api/v1/ocr/extract/', {'image': _tiny_image_upload()}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_501_NOT_IMPLEMENTED)

    def test_no_image_provided(self):
        set_site_config(ocr_backend='tesseract')
        response = self.client.post('/api/v1/ocr/extract/', {}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversized_image_rejected(self):
        upload = _tiny_image_upload(size=8 * 1024 * 1024 + 1)
        set_site_config(ocr_backend='tesseract')
        response = self.client.post('/api/v1/ocr/extract/', {'image': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unsupported_content_type_rejected(self):
        upload = _tiny_image_upload(name='voucher.txt', content_type='text/plain')
        set_site_config(ocr_backend='tesseract')
        response = self.client.post('/api/v1/ocr/extract/', {'image': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('api.views.get_backend')
    def test_success_returns_backend_result(self, mock_get_backend):
        mock_backend = MagicMock()
        mock_backend.extract.return_value = {
            'code': 'SAVE20', 'name': 'Acme', 'issuer': None, 'expiry_date': '2026-12-31', 'confidence': 0.8,
        }
        mock_get_backend.return_value = mock_backend

        set_site_config(ocr_backend='claude')
        response = self.client.post('/api/v1/ocr/extract/', {'image': _tiny_image_upload()}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['code'], 'SAVE20')
        self.assertEqual(response.data['name'], 'Acme')
        mock_backend.extract.assert_called_once()

    @patch('api.views.get_backend', side_effect=RuntimeError('tesseract binary missing'))
    def test_backend_unavailable_returns_503(self, mock_get_backend):
        set_site_config(ocr_backend='tesseract')
        response = self.client.post('/api/v1/ocr/extract/', {'image': _tiny_image_upload()}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class PkpassApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        self.item = make_item(self.alice)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(f'/api/v1/items/{self.item.id}/pkpass/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('api.views.pkpass_enabled', return_value=False)
    def test_disabled_by_default(self, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/pkpass/')
        self.assertEqual(response.status_code, status.HTTP_501_NOT_IMPLEMENTED)

    @patch('api.views.pkpass_enabled', return_value=True)
    @patch('api.views.generate_pkpass', return_value=b'fake-pkpass-bytes')
    def test_success_returns_binary_pkpass(self, mock_generate, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/pkpass/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.content, b'fake-pkpass-bytes')
        self.assertEqual(response['Content-Type'], 'application/vnd.apple.pkpass')
        self.assertIn(str(self.item.id), response['Content-Disposition'])
        mock_generate.assert_called_once()

    @patch('api.views.pkpass_enabled', return_value=True)
    @patch('api.views.generate_pkpass', side_effect=RuntimeError('PKPASS_TEAM_ID is not set.'))
    def test_generation_failure_returns_503(self, mock_generate, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/pkpass/')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('api.views.pkpass_enabled', return_value=True)
    def test_cannot_download_another_users_item_pkpass(self, mock_enabled):
        bob_item = make_item(self.bob, redeem_code='BOBCODE')
        response = self.client.get(f'/api/v1/items/{bob_item.id}/pkpass/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class GoogleWalletApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.force_authenticate(user=self.alice)
        self.item = make_item(self.alice)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(f'/api/v1/items/{self.item.id}/google-wallet/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('api.views.google_wallet_enabled', return_value=False)
    def test_disabled_by_default(self, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/google-wallet/')
        self.assertEqual(response.status_code, status.HTTP_501_NOT_IMPLEMENTED)

    @patch('api.views.google_wallet_enabled', return_value=True)
    @patch('api.views.generate_google_wallet_save_url', return_value='https://pay.google.com/gp/v/save/fake-jwt')
    def test_success_returns_save_url(self, mock_generate, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/google-wallet/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['save_url'], 'https://pay.google.com/gp/v/save/fake-jwt')
        mock_generate.assert_called_once()

    @patch('api.views.google_wallet_enabled', return_value=True)
    @patch('api.views.generate_google_wallet_save_url', side_effect=RuntimeError('GOOGLE_WALLET_ISSUER_ID is not set.'))
    def test_generation_failure_returns_503(self, mock_generate, mock_enabled):
        response = self.client.get(f'/api/v1/items/{self.item.id}/google-wallet/')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('api.views.google_wallet_enabled', return_value=True)
    def test_cannot_fetch_another_users_item_google_wallet_link(self, mock_enabled):
        bob_item = make_item(self.bob, redeem_code='BOBCODE')
        response = self.client.get(f'/api/v1/items/{bob_item.id}/google-wallet/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SharedWalletApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.carol = User.objects.create_user(username='carol', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.alice, name='Family')
        self.wallet.shared_with.add(self.bob)
        self.item = make_item(self.alice, wallet=self.wallet)

    def test_collaborator_sees_shared_wallet_and_its_items(self):
        self.client.force_authenticate(user=self.bob)
        wallet_response = self.client.get(f'/api/v1/wallets/{self.wallet.id}/')
        self.assertEqual(wallet_response.status_code, status.HTTP_200_OK)
        self.assertFalse(wallet_response.data['is_owner'])

        item_response = self.client.get(f'/api/v1/items/{self.item.id}/')
        self.assertEqual(item_response.status_code, status.HTTP_200_OK)

    def test_outsider_cannot_see_shared_wallet_or_its_items(self):
        self.client.force_authenticate(user=self.carol)
        self.assertEqual(
            self.client.get(f'/api/v1/wallets/{self.wallet.id}/').status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.client.get(f'/api/v1/items/{self.item.id}/').status_code, status.HTTP_404_NOT_FOUND
        )

    def test_collaborator_can_edit_and_delete_item_in_shared_wallet(self):
        self.client.force_authenticate(user=self.bob)
        patch_response = self.client.patch(f'/api/v1/items/{self.item.id}/', {'name': 'Renamed'})
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK, patch_response.data)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Renamed')

        delete_response = self.client.delete(f'/api/v1/items/{self.item.id}/')
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

    def test_collaborator_cannot_modify_wallet_itself(self):
        self.client.force_authenticate(user=self.bob)
        response = self.client.patch(f'/api/v1/wallets/{self.wallet.id}/', {'name': 'Renamed'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_invite_and_revoke_via_share_action(self):
        self.client.force_authenticate(user=self.alice)
        invite_response = self.client.post(f'/api/v1/wallets/{self.wallet.id}/share/', {'username': 'carol'})
        self.assertEqual(invite_response.status_code, status.HTTP_201_CREATED, invite_response.data)
        self.assertIn(self.carol, self.wallet.shared_with.all())

        revoke_response = self.client.delete(f'/api/v1/wallets/{self.wallet.id}/share/{self.carol.id}/')
        self.assertEqual(revoke_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertNotIn(self.carol, self.wallet.shared_with.all())

    def test_collaborator_cannot_invite_others(self):
        self.client.force_authenticate(user=self.bob)
        response = self.client.post(f'/api/v1/wallets/{self.wallet.id}/share/', {'username': 'carol'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(self.carol, self.wallet.shared_with.all())

    def test_wallet_items_action_lists_all_collaborators_items(self):
        self.client.force_authenticate(user=self.bob)
        response = self.client.get(f'/api/v1/wallets/{self.wallet.id}/items/')
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], str(self.item.id))
