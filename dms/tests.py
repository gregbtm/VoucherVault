"""
Tests for the DMS integration app.
"""
import uuid
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from myapp.models import Item
from .models import DMSProvider, DMSSyncLog


def make_provider(user, provider_type=DMSProvider.PROVIDER_PAPERLESS, **kwargs):
    defaults = {
        'name': 'Test DMS',
        'provider': provider_type,
        'base_url': 'http://paperless.local',
        'api_token': 'testtoken',
        'enabled': True,
    }
    defaults.update(kwargs)
    return DMSProvider.objects.create(user=user, **defaults)


class DMSProviderModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('dmsuser', password='pass123')

    def test_str(self):
        p = make_provider(self.user)
        self.assertIn('Test DMS', str(p))
        self.assertIn('Paperless', str(p))

    def test_status_badge_ok(self):
        p = make_provider(self.user, status=DMSProvider.STATUS_OK)
        self.assertEqual(p.status_badge_class, 'success')

    def test_status_badge_error(self):
        p = make_provider(self.user, status=DMSProvider.STATUS_ERROR)
        self.assertEqual(p.status_badge_class, 'danger')

    def test_status_badge_unchecked(self):
        p = make_provider(self.user)
        self.assertEqual(p.status_badge_class, 'secondary')


class DMSSyncLogModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('loguser', password='pass123')
        self.provider = make_provider(self.user)

    def test_str_contains_direction_and_provider(self):
        log = DMSSyncLog.objects.create(
            provider=self.provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_OK,
        )
        s = str(log)
        self.assertIn('Push', s)
        self.assertIn('Test DMS', s)

    def test_status_badge(self):
        log = DMSSyncLog.objects.create(
            provider=self.provider,
            direction=DMSSyncLog.DIRECTION_PULL,
            status=DMSSyncLog.STATUS_ERROR,
        )
        self.assertEqual(log.status_badge_class, 'danger')


class DMSProvidersViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('viewuser', 'v@example.com', 'pass123')
        self.other = User.objects.create_user('other', 'o@example.com', 'pass123')
        self.client = Client()
        self.client.force_login(self.user)

    def test_providers_list_empty(self):
        resp = self.client.get(reverse('dms:providers'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No DMS providers')

    def test_providers_list_shows_own_providers(self):
        make_provider(self.user, name='My Paperless')
        make_provider(self.other, name='Other Paperless')
        resp = self.client.get(reverse('dms:providers'))
        self.assertContains(resp, 'My Paperless')
        self.assertNotContains(resp, 'Other Paperless')

    def test_add_provider_get(self):
        resp = self.client.get(reverse('dms:add_provider'))
        self.assertEqual(resp.status_code, 200)

    def test_add_provider_post(self):
        resp = self.client.post(reverse('dms:add_provider'), {
            'name': 'New Provider',
            'provider': 'paperless',
            'base_url': 'http://paperless.local',
            'api_token': 'token123',
            'enabled': True,
            'auto_push': False,
            'auto_pull': False,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(DMSProvider.objects.filter(user=self.user, name='New Provider').count(), 1)

    def test_edit_provider(self):
        p = make_provider(self.user)
        resp = self.client.post(reverse('dms:edit_provider', args=[p.id]), {
            'name': 'Renamed',
            'provider': 'paperless',
            'base_url': 'http://paperless.local',
            'api_token': 'tok',
            'enabled': True,
            'auto_push': False,
            'auto_pull': False,
        })
        self.assertEqual(resp.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.name, 'Renamed')

    def test_edit_other_users_provider_returns_404(self):
        p = make_provider(self.other)
        resp = self.client.get(reverse('dms:edit_provider', args=[p.id]))
        self.assertEqual(resp.status_code, 404)

    def test_delete_provider(self):
        p = make_provider(self.user)
        resp = self.client.post(reverse('dms:delete_provider', args=[p.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(DMSProvider.objects.filter(id=p.id).exists())

    def test_delete_other_users_provider_returns_404(self):
        p = make_provider(self.other)
        resp = self.client.post(reverse('dms:delete_provider', args=[p.id]))
        self.assertEqual(resp.status_code, 404)

    def test_requires_login(self):
        c = Client()
        resp = c.get(reverse('dms:providers'))
        self.assertEqual(resp.status_code, 302)


class DMSTestConnectionViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('tcuser', password='pass123')
        self.client = Client()
        self.client.force_login(self.user)

    @patch('dms.views.get_client')
    def test_test_connection_ok(self, mock_get_client):
        p = make_provider(self.user)
        mock_client = MagicMock()
        mock_client.test_connection.return_value = {'ok': True, 'message': 'Connected', 'version': '1.0'}
        mock_get_client.return_value = mock_client

        resp = self.client.get(reverse('dms:test_connection', args=[p.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], DMSProvider.STATUS_OK)

        p.refresh_from_db()
        self.assertEqual(p.status, DMSProvider.STATUS_OK)

    @patch('dms.views.get_client')
    def test_test_connection_fail(self, mock_get_client):
        p = make_provider(self.user)
        mock_client = MagicMock()
        mock_client.test_connection.return_value = {'ok': False, 'message': 'Auth failed'}
        mock_get_client.return_value = mock_client

        resp = self.client.get(reverse('dms:test_connection', args=[p.id]))
        data = resp.json()
        self.assertFalse(data['ok'])
        p.refresh_from_db()
        self.assertEqual(p.status, DMSProvider.STATUS_ERROR)

    def test_other_users_provider_404(self):
        other = User.objects.create_user('other2', password='pass')
        p = make_provider(other)
        resp = self.client.get(reverse('dms:test_connection', args=[p.id]))
        self.assertEqual(resp.status_code, 404)


class DMSBrowseViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('browseuser', password='pass123')
        self.client = Client()
        self.client.force_login(self.user)

    @patch('dms.views.get_client')
    def test_browse_returns_documents(self, mock_get_client):
        from dms.clients.base import BrowseResult, DMSDocument
        p = make_provider(self.user)
        mock_client = MagicMock()
        doc = DMSDocument(id='1', title='Invoice')
        mock_client.browse.return_value = BrowseResult(
            documents=[doc], total_count=1, page=1, page_size=20, has_next=False, has_prev=False
        )
        mock_get_client.return_value = mock_client

        resp = self.client.get(reverse('dms:browse', args=[p.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['documents']), 1)
        self.assertEqual(data['documents'][0]['title'], 'Invoice')

    @patch('dms.views.get_client')
    def test_browse_error_returns_500(self, mock_get_client):
        p = make_provider(self.user)
        mock_client = MagicMock()
        mock_client.browse.side_effect = Exception('connection refused')
        mock_get_client.return_value = mock_client

        resp = self.client.get(reverse('dms:browse', args=[p.id]))
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()['ok'])


class DMSPushDocumentViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('pushuser', password='pass123')
        self.client = Client()
        self.client.force_login(self.user)
        from datetime import date, timedelta
        self.item = Item.objects.create(
            user=self.user, name='Test Item', redeem_code='CODE1',
            type='voucher', issuer='TestCo', value='0.00',
            expiry_date=date.today() + timedelta(days=365),
        )

    @patch('dms.views.get_client')
    def test_push_document_success(self, mock_get_client):
        from myapp.models import Document
        from django.core.files.base import ContentFile

        p = make_provider(self.user)
        doc = Document(item=self.item)
        doc.file.save('test.pdf', ContentFile(b'%PDF dummy'), save=True)

        mock_client = MagicMock()
        mock_client.upload_document.return_value = 'dms-42'
        mock_get_client.return_value = mock_client

        resp = self.client.post(reverse('dms:push_document', args=[p.id, doc.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['dms_id'], 'dms-42')
        self.assertTrue(DMSSyncLog.objects.filter(
            provider=p, direction='push', status='ok', document=doc
        ).exists())

    @patch('dms.views.get_client')
    def test_push_document_error_logs_failure(self, mock_get_client):
        from myapp.models import Document
        from django.core.files.base import ContentFile

        p = make_provider(self.user)
        doc = Document(item=self.item)
        doc.file.save('fail.pdf', ContentFile(b'data'), save=True)

        mock_client = MagicMock()
        mock_client.upload_document.side_effect = Exception('upload error')
        mock_get_client.return_value = mock_client

        resp = self.client.post(reverse('dms:push_document', args=[p.id, doc.id]))
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(resp.json()['ok'])
        self.assertTrue(DMSSyncLog.objects.filter(status='error').exists())


class DMSSyncLogsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('logviewuser', password='pass123')
        self.client = Client()
        self.client.force_login(self.user)

    def test_logs_empty(self):
        resp = self.client.get(reverse('dms:sync_logs'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No sync activity')

    def test_logs_shows_own_logs(self):
        p = make_provider(self.user)
        log = DMSSyncLog.objects.create(
            provider=p,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_OK,
            dms_document_title='My Invoice',
        )
        resp = self.client.get(reverse('dms:sync_logs'))
        self.assertContains(resp, 'My Invoice')

    def test_logs_filter_by_provider(self):
        p = make_provider(self.user)
        other_user = User.objects.create_user('other3', password='pass')
        other_p = make_provider(other_user)

        DMSSyncLog.objects.create(provider=p, direction='push', status='ok', dms_document_title='Mine')
        # The other provider belongs to a different user — won't appear
        resp = self.client.get(reverse('dms:sync_logs') + f'?provider={p.id}')
        self.assertContains(resp, 'Mine')


class DMSClientFactoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('factuser', password='pass123')

    def test_get_client_paperless(self):
        from dms.clients import get_client
        from dms.clients.paperless import PaperlessNGXClient
        p = make_provider(self.user, provider='paperless')
        c = get_client(p)
        self.assertIsInstance(c, PaperlessNGXClient)

    def test_get_client_docspell(self):
        from dms.clients import get_client
        from dms.clients.docspell import DocspellClient
        p = make_provider(self.user, provider='docspell')
        c = get_client(p)
        self.assertIsInstance(c, DocspellClient)

    def test_get_client_papermerge(self):
        from dms.clients import get_client
        from dms.clients.papermerge import PaperMergeClient
        p = make_provider(self.user, provider='papermerge')
        c = get_client(p)
        self.assertIsInstance(c, PaperMergeClient)

    def test_get_client_unknown_raises(self):
        from dms.clients import get_client
        p = make_provider(self.user)
        p.provider = 'unknown'
        with self.assertRaises(ValueError):
            get_client(p)


class DMSPullDocumentViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('pulluser', password='pass123')
        self.client = Client()
        self.client.force_login(self.user)

    @patch('dms.views.get_client')
    def test_pull_creates_item_and_document(self, mock_get_client):
        from dms.clients.base import DMSDocument
        p = make_provider(self.user)

        dms_doc = DMSDocument(
            id='99', title='Receipt', original_filename='receipt.pdf', content='some text'
        )
        mock_client = MagicMock()
        mock_client.get_document.return_value = dms_doc
        mock_client.download_document.return_value = b'%PDF data'
        mock_get_client.return_value = mock_client

        import json
        resp = self.client.post(
            reverse('dms:pull_document', args=[p.id]),
            data=json.dumps({'dms_doc_id': '99'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertIn('item_uuid', data)
        item = Item.objects.get(id=data['item_uuid'])
        self.assertEqual(item.name, 'Receipt')
        self.assertEqual(item.documents.count(), 1)

    def test_pull_missing_dms_doc_id_returns_400(self):
        p = make_provider(self.user)
        resp = self.client.post(reverse('dms:pull_document', args=[p.id]), data='{}', content_type='application/json')
        self.assertEqual(resp.status_code, 400)
