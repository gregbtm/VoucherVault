"""
Standalone unit tests for the MCP server, run directly with:

    cd mcp_server && python -m unittest run_tests -v

Deliberately named run_tests.py, not tests.py: `manage.py test` (no
arguments) walks the whole repo looking for test*.py files regardless of
INSTALLED_APPS, and this package isn't part of the Django project (it has
no Django dependency at all, only `mcp` and `requests`, which aren't in
the main app's requirements.txt) — a name matching that glob would make
Django try to import it and fail wherever `mcp` isn't installed.

Every VoucherVaultClient method is exercised against a mocked
requests.request, so these never need a live VoucherVault instance.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('VOUCHERVAULT_BASE_URL', 'http://localhost:8000')
os.environ.setdefault('VOUCHERVAULT_API_TOKEN', 'fake-token')

from client import VoucherVaultApiError, VoucherVaultClient
import server


def _mock_response(status_code=200, json_data=None):
    response = MagicMock()
    response.status_code = status_code
    response.content = b'{}' if json_data is None else b'x'
    response.json.return_value = json_data if json_data is not None else {}
    return response


class VoucherVaultClientTests(unittest.TestCase):
    def setUp(self):
        self.client = VoucherVaultClient(base_url='http://vv.example.com', api_token='tok123')

    def test_requires_base_url_and_token(self):
        # clear=True so the module-level VOUCHERVAULT_* defaults (set for
        # the rest of this file's import-time convenience) don't mask the
        # missing-value check being tested here.
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                VoucherVaultClient(base_url='', api_token='tok')
            with self.assertRaises(RuntimeError):
                VoucherVaultClient(base_url='http://vv.example.com', api_token='')

    @patch('client.requests.request')
    def test_list_items_sends_auth_header_and_drops_none_params(self, mock_request):
        mock_request.return_value = _mock_response(200, {'results': []})
        self.client.list_items(search='coffee', type=None, is_used=False)

        args, kwargs = mock_request.call_args
        self.assertEqual(args[0], 'GET')
        self.assertEqual(args[1], 'http://vv.example.com/api/v1/items/')
        self.assertEqual(kwargs['headers'], {'Authorization': 'Token tok123'})
        self.assertEqual(kwargs['params'], {'search': 'coffee', 'is_used': False})

    @patch('client.requests.request')
    def test_get_item(self, mock_request):
        mock_request.return_value = _mock_response(200, {'id': 'abc', 'name': 'Test'})
        result = self.client.get_item('abc')
        self.assertEqual(result['name'], 'Test')
        mock_request.assert_called_once_with(
            'GET', 'http://vv.example.com/api/v1/items/abc/', headers={'Authorization': 'Token tok123'}, timeout=15,
        )

    @patch('client.requests.request')
    def test_create_item_posts_json_payload(self, mock_request):
        mock_request.return_value = _mock_response(201, {'id': 'new-id'})
        payload = {'type': 'voucher', 'name': 'X'}
        self.client.create_item(payload)
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['json'], payload)

    @patch('client.requests.request')
    def test_add_transaction_builds_expected_payload(self, mock_request):
        mock_request.return_value = _mock_response(201, {'id': 't1'})
        self.client.add_transaction('item-1', 'Coffee', '-4.50')
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['json'], {'description': 'Coffee', 'value': '-4.50'})

    @patch('client.requests.request')
    def test_error_response_raises_with_status_and_detail(self, mock_request):
        mock_request.return_value = _mock_response(404, {'detail': 'Not found.'})
        with self.assertRaises(VoucherVaultApiError) as ctx:
            self.client.get_item('missing')
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, {'detail': 'Not found.'})

    @patch('client.requests.request')
    def test_no_content_response_returns_empty_dict(self, mock_request):
        response = MagicMock()
        response.status_code = 204
        response.content = b''
        mock_request.return_value = response
        self.assertEqual(self.client.update_item('abc', {'is_archived': True}), {})


class ToolFunctionTests(unittest.TestCase):
    """Each @mcp.tool() function is still a plain callable — exercised
    directly here, with VoucherVaultClient mocked out entirely."""

    @patch('server._client')
    def test_search_items_unwraps_paginated_results(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.list_items.return_value = {'results': [{'id': '1'}], 'count': 1}
        mock_client_factory.return_value = mock_client

        result = server.search_items(query='coffee')
        self.assertEqual(result, [{'id': '1'}])
        mock_client.list_items.assert_called_once_with(
            search='coffee', type=None, is_used=None, is_archived=None,
        )

    @patch('server._client')
    def test_get_expiring_items_computes_cutoff_and_filters(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.list_items.return_value = {'results': []}
        mock_client_factory.return_value = mock_client

        server.get_expiring_items(days=14)
        _args, kwargs = mock_client.list_items.call_args
        self.assertEqual(kwargs['is_used'], False)
        self.assertEqual(kwargs['is_archived'], False)
        self.assertEqual(kwargs['ordering'], 'expiry_date')
        self.assertIn('expires_before', kwargs)

    @patch('server._client')
    def test_create_item_omits_blank_optional_fields(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.create_item.return_value = {'id': 'new'}
        mock_client_factory.return_value = mock_client

        server.create_item(
            type='voucher', name='X', redeem_code='C1', issuer='Shop', expiry_date='2027-01-01',
        )
        payload = mock_client.create_item.call_args[0][0]
        self.assertNotIn('pin', payload)
        self.assertNotIn('notes', payload)
        self.assertEqual(payload['currency'], 'GBP')

    @patch('server._client')
    def test_mark_item_used_calls_redeem(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.redeem_item.return_value = {'id': 'i1', 'is_used': True}
        mock_client_factory.return_value = mock_client

        result = server.mark_item_used('i1')
        mock_client.redeem_item.assert_called_once_with('i1')
        self.assertTrue(result['is_used'])

    @patch('server._client')
    def test_set_item_archived_patches_is_archived(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.update_item.return_value = {'id': 'i1', 'is_archived': True}
        mock_client_factory.return_value = mock_client

        server.set_item_archived('i1', True)
        mock_client.update_item.assert_called_once_with('i1', {'is_archived': True})

    @patch('server._client')
    def test_list_wallets_unwraps_paginated_results(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.list_wallets.return_value = {'results': [{'id': 'w1', 'name': 'Groceries'}], 'count': 1}
        mock_client_factory.return_value = mock_client

        result = server.list_wallets()
        self.assertEqual(result, [{'id': 'w1', 'name': 'Groceries'}])

    @patch('server._client')
    def test_create_wallet_omits_blank_optionals(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.create_wallet.return_value = {'id': 'w2', 'name': 'Travel'}
        mock_client_factory.return_value = mock_client

        server.create_wallet('Travel')
        payload = mock_client.create_wallet.call_args[0][0]
        self.assertEqual(payload, {'name': 'Travel'})
        self.assertNotIn('description', payload)

    @patch('server._client')
    def test_create_wallet_passes_optional_fields(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.create_wallet.return_value = {'id': 'w3'}
        mock_client_factory.return_value = mock_client

        server.create_wallet('Gifts', description='Gift cards', color='#ff0000')
        payload = mock_client.create_wallet.call_args[0][0]
        self.assertEqual(payload['description'], 'Gift cards')
        self.assertEqual(payload['color'], '#ff0000')

    @patch('server._client')
    def test_list_tags_unwraps_results(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.list_tags.return_value = {'results': [{'id': 't1', 'name': 'food'}]}
        mock_client_factory.return_value = mock_client

        result = server.list_tags()
        self.assertEqual(result, [{'id': 't1', 'name': 'food'}])

    @patch('server._client')
    def test_list_wallet_activity_passes_wallet_filter(self, mock_client_factory):
        mock_client = MagicMock()
        mock_client.list_wallet_activity.return_value = {'results': []}
        mock_client_factory.return_value = mock_client

        server.list_wallet_activity(wallet_id='w1')
        mock_client.list_wallet_activity.assert_called_once_with(wallet_id='w1')


class NewClientMethodTests(unittest.TestCase):
    def setUp(self):
        self.client = VoucherVaultClient(base_url='http://vv.example.com', api_token='tok123')

    @patch('client.requests.request')
    def test_list_wallets(self, mock_request):
        mock_request.return_value = _mock_response(200, {'results': []})
        self.client.list_wallets()
        args, _ = mock_request.call_args
        self.assertEqual(args[0], 'GET')
        self.assertIn('wallets/', args[1])

    @patch('client.requests.request')
    def test_list_wallet_activity_with_filter(self, mock_request):
        mock_request.return_value = _mock_response(200, {'results': []})
        self.client.list_wallet_activity(wallet_id='w99')
        _args, kwargs = mock_request.call_args
        self.assertEqual(kwargs['params'], {'wallet': 'w99'})

    @patch('client.requests.request')
    def test_get_expiry_timeline(self, mock_request):
        mock_request.return_value = _mock_response(200, [])
        self.client.get_expiry_timeline()
        args, _ = mock_request.call_args
        self.assertIn('expiry-timeline', args[1])


if __name__ == '__main__':
    unittest.main()
