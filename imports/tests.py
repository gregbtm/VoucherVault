import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from myapp.models import Item, Tag, Wallet

from .exporters.csv_export import export_items_csv
from .exporters.json_export import export_items_json
from .models import ImportJob
from .parsers.catima import parse as parse_catima
from .parsers.native_csv import parse as parse_native_csv
from .parsers.native_json import parse as parse_native_json
from .tasks import create_item_from_row, process_import_job

CATIMA_SAMPLE = (
    'Group,Description,Note,Card Number,EAN Barcode ID,Card Type,Expiry,Balance,Balance Type,Colour,Star\n'
    'Supermarkets,Tesco Clubcard,My loyalty card,1234567890,,QR_CODE,,0,,,1\n'
    'Restaurants,Nandos Gift Card,Birthday gift,GC998877,,CODE_128,2026-12-31,25.50,GBP,#ff5733,0\n'
    ',Bad Row,,,,,,,,,,\n'
)


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


class CatimaParserTests(TestCase):
    def test_parses_valid_rows_and_flags_bad_row(self):
        rows, errors = parse_catima(io.StringIO(CATIMA_SAMPLE))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(errors), 1)

        loyalty = rows[0]
        self.assertEqual(loyalty['type'], 'loyaltycard')
        self.assertEqual(loyalty['value'], Decimal('0'))
        self.assertEqual(loyalty['wallet_name'], 'Supermarkets')
        self.assertEqual(loyalty['code_type'], 'qrcode')

        giftcard = rows[1]
        self.assertEqual(giftcard['type'], 'giftcard')
        self.assertEqual(giftcard['value'], Decimal('25.50'))
        self.assertEqual(giftcard['currency'], 'GBP')
        self.assertEqual(giftcard['code_type'], 'code128')
        self.assertEqual(giftcard['expiry_date'], date(2026, 12, 31))
        self.assertEqual(giftcard['tile_color'], '#ff5733')

    def test_handles_bytes_and_bom(self):
        content = ('﻿' + CATIMA_SAMPLE).encode('utf-8')
        rows, errors = parse_catima(io.BytesIO(content))
        self.assertEqual(len(rows), 2)

    def test_invalid_balance_reported_as_error(self):
        text = (
            'Group,Description,Note,Card Number,EAN Barcode ID,Card Type,Expiry,Balance,Balance Type,Colour,Star\n'
            ',Weird Card,,X1,,,,notanumber,,,0\n'
        )
        rows, errors = parse_catima(io.StringIO(text))
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('balance', errors[0]['message'].lower())


class NativeCsvRoundTripTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_export_then_import_round_trip(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        tag = Tag.objects.create(user=self.user, name='discount')
        item = make_item(self.user, name='Flight Voucher', wallet=wallet, notes='Show at gate', notify_days_before=14)
        item.tags.add(tag)

        csv_text = export_items_csv(Item.objects.filter(user=self.user))
        rows, errors = parse_native_csv(io.StringIO(csv_text))

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['name'], 'Flight Voucher')
        self.assertEqual(row['wallet_name'], 'Travel')
        self.assertEqual(row['tag_names'], ['discount'])
        self.assertEqual(row['notes'], 'Show at gate')
        self.assertEqual(row['notify_days_before'], 14)
        self.assertEqual(row['value'], Decimal('10.00'))

    def test_missing_required_field_reported(self):
        text = 'type,name,issuer,redeem_code,pin,code_type,issue_date,expiry_date,value,value_type,currency,description,notes,wallet,tags,is_used,is_pinned,tile_color,notify_days_before,logo_slug\nvoucher,,Issuer,,,,,,,,,,,,,,,,,\n'
        rows, errors = parse_native_csv(io.StringIO(text))
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)

    def test_invalid_type_reported(self):
        text = 'type,name,issuer,redeem_code,pin,code_type,issue_date,expiry_date,value,value_type,currency,description,notes,wallet,tags,is_used,is_pinned,tile_color,notify_days_before,logo_slug\nnotatype,X,Issuer,CODE1,,,,,,,,,,,,,,,,\n'
        rows, errors = parse_native_csv(io.StringIO(text))
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)


class NativeJsonRoundTripTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_export_then_import_round_trip(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        item = make_item(self.user, name='Flight Voucher', wallet=wallet)

        payload = export_items_json(Item.objects.filter(user=self.user))
        rows, errors = parse_native_json(io.StringIO(json.dumps(payload)))

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], 'Flight Voucher')
        self.assertEqual(rows[0]['wallet_name'], 'Travel')

    def test_accepts_items_wrapped_dict(self):
        rows, errors = parse_native_json(io.StringIO(json.dumps({'items': [
            {'type': 'voucher', 'name': 'X', 'redeem_code': 'C1', 'issuer': 'Y', 'value': '5.00'},
        ]})))
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)

    def test_invalid_json_reported(self):
        rows, errors = parse_native_json(io.StringIO('not json'))
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn('Invalid JSON', errors[0]['message'])


class CreateItemFromRowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_creates_item_with_qr_code_and_source(self):
        row = {
            'type': 'voucher', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'C1',
            'value': Decimal('5.00'), 'value_type': 'money', 'currency': 'EUR',
        }
        item = create_item_from_row(self.user, row)
        self.assertEqual(item.source, 'csv_import')
        self.assertTrue(item.qr_code_base64)
        self.assertGreater(item.expiry_date, date.today() + timedelta(days=365 * 40))

    def test_resolves_wallet_and_tags_by_name(self):
        row = {
            'type': 'voucher', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'C1',
            'value': Decimal('5.00'), 'wallet_name': 'Travel', 'tag_names': ['discount', 'summer'],
        }
        item = create_item_from_row(self.user, row)
        self.assertEqual(item.wallet.name, 'Travel')
        self.assertEqual(set(item.tags.values_list('name', flat=True)), {'discount', 'summer'})

    def test_reuses_existing_wallet_for_same_user(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        row = {'type': 'voucher', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'C1', 'value': Decimal('5.00'), 'wallet_name': 'Travel'}
        item = create_item_from_row(self.user, row)
        self.assertEqual(item.wallet_id, wallet.id)
        self.assertEqual(Wallet.objects.filter(user=self.user, name='Travel').count(), 1)

    def test_loyaltycard_value_forced_to_zero(self):
        row = {'type': 'loyaltycard', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'C1', 'value': Decimal('5.00')}
        item = create_item_from_row(self.user, row)
        self.assertEqual(item.value, Decimal('0'))

    def test_giftcard_negative_value_raises(self):
        row = {'type': 'giftcard', 'name': 'X', 'issuer': 'Y', 'redeem_code': 'C1', 'value': Decimal('-5.00')}
        with self.assertRaises(ValueError):
            create_item_from_row(self.user, row)


class ProcessImportJobTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _make_job(self, source_type, content, filename):
        upload = SimpleUploadedFile(filename, content.encode('utf-8'))
        return ImportJob.objects.create(user=self.user, source_type=source_type, file=upload)

    def test_successful_catima_import(self):
        job = self._make_job('catima_csv', CATIMA_SAMPLE, 'catima.csv')
        process_import_job(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, 'complete')
        self.assertEqual(job.imported_count, 2)
        self.assertEqual(job.error_count, 1)
        self.assertEqual(Item.objects.filter(user=self.user).count(), 2)
        self.assertIsNotNone(job.completed_at)

    def test_row_validation_errors_are_recorded_without_failing_job(self):
        text = (
            'type,name,issuer,redeem_code,pin,code_type,issue_date,expiry_date,value,value_type,currency,'
            'description,notes,wallet,tags,is_used,is_pinned,tile_color,notify_days_before,logo_slug\n'
            'giftcard,Bad Value,Issuer,CODE1,,,,,-5,,,,,,,,,,,\n'
            'voucher,Good One,Issuer,CODE2,,,,,5,,,,,,,,,,,\n'
        )
        job = self._make_job('native_csv', text, 'backup.csv')
        process_import_job(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, 'complete')
        self.assertEqual(job.imported_count, 1)
        self.assertEqual(job.error_count, 1)

    def test_unparseable_file_marks_job_failed(self):
        job = self._make_job('native_json', 'not json at all', 'backup.json')
        process_import_job(str(job.id))
        job.refresh_from_db()
        # native_json.parse() itself catches JSON errors and returns them as
        # row errors rather than raising, so this should complete with 0 imported.
        self.assertEqual(job.status, 'complete')
        self.assertEqual(job.imported_count, 0)
        self.assertEqual(job.error_count, 1)


class UploadViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    @patch('imports.views.process_import_job.delay')
    def test_upload_creates_job_and_dispatches_task(self, mock_delay):
        upload = SimpleUploadedFile('catima.csv', CATIMA_SAMPLE.encode('utf-8'))
        response = self.client.post(reverse('upload_import'), {'source_type': 'catima_csv', 'file': upload})
        job = ImportJob.objects.get(user=self.alice)
        self.assertRedirects(response, reverse('import_job_status', args=[job.id]))
        mock_delay.assert_called_once_with(str(job.id))

    @patch('imports.views.process_import_job.delay', side_effect=RuntimeError('Retry limit exceeded'))
    def test_broker_unreachable_fails_gracefully(self, mock_delay):
        # Regression test: found via manual verification that a broker/queue
        # outage at dispatch time propagated as an unhandled 500 instead of
        # being caught and surfaced on the job.
        upload = SimpleUploadedFile('catima.csv', CATIMA_SAMPLE.encode('utf-8'))
        response = self.client.post(reverse('upload_import'), {'source_type': 'catima_csv', 'file': upload})
        job = ImportJob.objects.get(user=self.alice)
        self.assertRedirects(response, reverse('import_job_status', args=[job.id]))
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIn('Could not queue', job.errors[0]['message'])

    def test_invalid_source_type_rejected(self):
        upload = SimpleUploadedFile('x.csv', b'a,b\n1,2\n')
        response = self.client.post(reverse('upload_import'), {'source_type': 'not_real', 'file': upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_extension_mismatch_rejected(self):
        upload = SimpleUploadedFile('x.txt', b'a,b\n1,2\n')
        response = self.client.post(reverse('upload_import'), {'source_type': 'catima_csv', 'file': upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_cannot_view_another_users_job(self):
        bob_job = ImportJob.objects.create(user=self.bob, source_type='catima_csv', file=SimpleUploadedFile('x.csv', b'x'))
        response = self.client.get(reverse('import_job_status', args=[bob_job.id]))
        self.assertEqual(response.status_code, 404)


class ExportViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        make_item(self.alice, name='Alice Item')
        make_item(self.bob, name='Bob Item', redeem_code='BOBCODE')

    def test_csv_export_contains_only_own_items(self):
        response = self.client.get(reverse('export_csv'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Alice Item', content)
        self.assertNotIn('Bob Item', content)
        self.assertEqual(response['Content-Type'], 'text/csv')

    def test_json_export_contains_only_own_items(self):
        response = self.client.get(reverse('export_json'))
        data = json.loads(response.content)
        names = [row['name'] for row in data]
        self.assertEqual(names, ['Alice Item'])
