from datetime import date, timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from myapp.models import Item, ItemShare, Transaction


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
