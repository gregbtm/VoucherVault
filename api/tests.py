from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from myapp.models import Item, ItemShare, Tag, Transaction, Wallet
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
