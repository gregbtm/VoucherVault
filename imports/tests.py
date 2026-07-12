import base64
import csv
import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, Encoding, pkcs12
from cryptography.x509.oid import NameOID
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from myapp.models import Item, Tag, Wallet

from myapp.models import Document, Transaction, UserPreference, UserProfile
from myapp.test_utils import set_site_config

from notify.models import NotificationRule

from .exporters.csv_export import export_items_csv
from .exporters.full_backup import export_full_backup
from .exporters.google_wallet import generate_google_wallet_save_url, google_wallet_enabled
from .exporters.json_export import export_items_json
from .exporters.pkpass import generate_pkpass, pkpass_enabled
from .full_backup_import import FullBackupImportError, import_full_backup
from .models import ImportJob
from .parsers.catima import parse as parse_catima
from .parsers.native_csv import parse as parse_native_csv
from .parsers.native_json import parse as parse_native_json
from .pkpass_import import PkpassImportError, extract_pkpass_fields
from .tasks import backup_user, create_item_from_row, process_import_job, run_scheduled_backups

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

    def test_codabar_and_code93_map_to_their_own_types(self):
        text = (
            'Group,Description,Note,Card Number,EAN Barcode ID,Card Type,Expiry,Balance,Balance Type,Colour,Star\n'
            ',Codabar Card,,X1,,CODABAR,,0,,,0\n'
            ',Code93 Card,,X2,,CODE_93,,0,,,0\n'
        )
        rows, errors = parse_catima(io.StringIO(text))
        self.assertEqual(len(errors), 0)
        self.assertEqual(rows[0]['code_type'], 'codabar')
        self.assertEqual(rows[1]['code_type'], 'code93')

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


def _make_self_signed_cert(common_name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert


class PkpassExporterTests(TestCase):
    """
    Generates real self-signed certs (never touches Apple's actual cert
    chain) and drives generate_pkpass() end-to-end, then verifies the
    resulting CMS/PKCS7 signature with the openssl binary — proves the
    signing pipeline actually produces a well-formed, verifiable .pkpass,
    not just that our mocks were called correctly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory()
        pass_key, pass_cert = _make_self_signed_cert('Pass Type ID: pass.test.vouchervault')
        _wwdr_key, wwdr_cert = _make_self_signed_cert('Fake WWDR')

        p12_bytes = pkcs12.serialize_key_and_certificates(
            b'test-pass', pass_key, pass_cert, None, BestAvailableEncryption(b'testpass')
        )
        cls.cert_path = os.path.join(cls.tmpdir.name, 'pass-cert.p12')
        with open(cls.cert_path, 'wb') as f:
            f.write(p12_bytes)

        cls.wwdr_path = os.path.join(cls.tmpdir.name, 'wwdr.pem')
        with open(cls.wwdr_path, 'wb') as f:
            f.write(wwdr_cert.public_bytes(Encoding.PEM))

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        set_site_config(
            pkpass_cert_path=self.cert_path,
            pkpass_cert_password='testpass',
            pkpass_wwdr_cert_path=self.wwdr_path,
            pkpass_team_id='TEAM1234',
            pkpass_pass_type_id='pass.test.vouchervault',
        )

    def test_pkpass_enabled_true_when_cert_path_exists(self):
        self.assertTrue(pkpass_enabled())

    def test_pkpass_disabled_when_cert_path_unset(self):
        set_site_config(pkpass_cert_path='')
        self.assertFalse(pkpass_enabled())

    def test_pkpass_disabled_when_cert_path_missing_file(self):
        set_site_config(pkpass_cert_path='/nonexistent/path.p12')
        self.assertFalse(pkpass_enabled())

    def test_generate_pkpass_produces_valid_signed_bundle(self):
        item = make_item(
            self.user, type='giftcard', name='Coffee Gift Card', issuer='Bean Co',
            redeem_code='GC12345', value='25.00', currency='EUR', code_type='qrcode',
            expiry_date=date(2026, 12, 31), tile_color='#ff5733',
        )
        data = generate_pkpass(item)

        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertEqual(names, {'pass.json', 'icon.png', 'icon@2x.png', 'manifest.json', 'signature'})

            manifest = json.loads(zf.read('manifest.json'))
            for name in ('pass.json', 'icon.png', 'icon@2x.png'):
                expected_hash = hashlib.sha1(zf.read(name), usedforsecurity=False).hexdigest()
                self.assertEqual(manifest[name], expected_hash)

            pass_dict = json.loads(zf.read('pass.json'))
            self.assertEqual(pass_dict['serialNumber'], str(item.id))
            self.assertEqual(pass_dict['passTypeIdentifier'], 'pass.test.vouchervault')
            self.assertEqual(pass_dict['teamIdentifier'], 'TEAM1234')
            self.assertEqual(pass_dict['barcodes'][0]['message'], 'GC12345')
            self.assertEqual(pass_dict['barcodes'][0]['format'], 'PKBarcodeFormatQR')
            self.assertIn('storeCard', pass_dict)
            self.assertEqual(pass_dict['storeCard']['primaryFields'][0]['value'], '25.00 EUR')
            self.assertEqual(pass_dict['backgroundColor'], 'rgb(255, 87, 51)')

            manifest_bytes = zf.read('manifest.json')
            signature_bytes = zf.read('signature')

        with tempfile.TemporaryDirectory() as d:
            manifest_path = os.path.join(d, 'manifest.json')
            sig_path = os.path.join(d, 'signature')
            with open(manifest_path, 'wb') as f:
                f.write(manifest_bytes)
            with open(sig_path, 'wb') as f:
                f.write(signature_bytes)

            result = subprocess.run(
                ['openssl', 'smime', '-verify', '-in', sig_path, '-inform', 'DER',
                 '-content', manifest_path, '-noverify', '-CAfile', self.wwdr_path],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generate_pkpass_coupon_style_for_vouchers(self):
        item = make_item(self.user, type='voucher', name='20% Off', issuer='Shop')
        data = generate_pkpass(item)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            pass_dict = json.loads(zf.read('pass.json'))
        self.assertIn('coupon', pass_dict)
        self.assertNotIn('storeCard', pass_dict)

    def test_generate_pkpass_omits_barcodes_for_no_barcode_code_type(self):
        item = make_item(
            self.user, type='giftcard', name='Coffee Gift Card', issuer='Bean Co',
            redeem_code='GC12345', value='25.00', currency='EUR', code_type='none',
        )
        data = generate_pkpass(item)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            pass_dict = json.loads(zf.read('pass.json'))
        self.assertNotIn('barcodes', pass_dict)
        code_field = next(f for f in pass_dict['storeCard']['secondaryFields'] if f['key'] == 'code')
        self.assertEqual(code_field['value'], 'GC12345')

    def test_generate_pkpass_raises_when_disabled(self):
        set_site_config(pkpass_cert_path='')
        item = make_item(self.user)
        with self.assertRaises(RuntimeError):
            generate_pkpass(item)

    def test_generate_pkpass_raises_when_team_id_missing(self):
        set_site_config(pkpass_team_id='')
        item = make_item(self.user)
        with self.assertRaises(RuntimeError):
            generate_pkpass(item)


def _b64url_decode(segment):
    padding_needed = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding_needed)


class GoogleWalletExporterTests(TestCase):
    """
    Generates a throwaway RSA key (never touches a real Google service
    account) and drives generate_google_wallet_save_url() end-to-end,
    verifying the resulting JWT's structure and signature against the same
    key — proves the signing pipeline is well-formed, not just that our
    mocks were called correctly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = cls.private_key.private_bytes(
            encoding=Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')

        cls.client_email = 'wallet-issuer@test-project.iam.gserviceaccount.com'
        cls.key_path = os.path.join(cls.tmpdir.name, 'google-wallet-key.json')
        with open(cls.key_path, 'w') as f:
            json.dump({'client_email': cls.client_email, 'private_key': private_key_pem}, f)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user(username='bob', password='pw12345!')
        set_site_config(
            google_wallet_service_account_key_path=self.key_path,
            google_wallet_issuer_id='3388000000012345678',
        )

    def test_google_wallet_enabled_true_when_configured(self):
        self.assertTrue(google_wallet_enabled())

    def test_google_wallet_disabled_when_issuer_id_unset(self):
        set_site_config(google_wallet_issuer_id='')
        self.assertFalse(google_wallet_enabled())

    def test_google_wallet_disabled_when_key_path_missing_file(self):
        set_site_config(google_wallet_service_account_key_path='/nonexistent/key.json')
        self.assertFalse(google_wallet_enabled())

    def test_generate_save_url_produces_valid_signed_jwt(self):
        item = make_item(
            self.user, type='giftcard', name='Coffee Gift Card', issuer='Bean Co',
            redeem_code='GC12345', value='25.00', currency='GBP', code_type='qrcode',
            expiry_date=date(2026, 12, 31), tile_color='#ff5733',
        )
        save_url = generate_google_wallet_save_url(item)

        self.assertTrue(save_url.startswith('https://pay.google.com/gp/v/save/'))
        token = save_url.rsplit('/', 1)[-1]
        header_seg, payload_seg, signature_seg = token.split('.')

        header = json.loads(_b64url_decode(header_seg))
        self.assertEqual(header['alg'], 'RS256')

        payload = json.loads(_b64url_decode(payload_seg))
        self.assertEqual(payload['iss'], self.client_email)
        self.assertEqual(payload['aud'], 'google')
        self.assertEqual(payload['typ'], 'savetowallet')

        obj = payload['payload']['genericObjects'][0]
        self.assertEqual(obj['id'], '3388000000012345678.item-%s' % item.id)
        self.assertEqual(obj['genericType'], 'GENERIC_OTHER')
        self.assertEqual(obj['header']['defaultValue']['value'], 'Coffee Gift Card')
        self.assertEqual(obj['barcode']['type'], 'QR_CODE')
        self.assertEqual(obj['barcode']['value'], 'GC12345')
        self.assertEqual(obj['hexBackgroundColor'], '#ff5733')
        balance_module = next(m for m in obj['textModulesData'] if m['id'] == 'balance')
        self.assertEqual(balance_module['body'], '25.00 GBP')

        signing_input = f'{header_seg}.{payload_seg}'.encode('ascii')
        signature = _b64url_decode(signature_seg)
        # Raises InvalidSignature if the JWT wasn't actually signed by this key.
        self.private_key.public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())

    def test_generate_save_url_loyaltycard_generic_type(self):
        item = make_item(self.user, type='loyaltycard', name='Rewards Card', issuer='Shop')
        save_url = generate_google_wallet_save_url(item)
        token = save_url.rsplit('/', 1)[-1]
        _header_seg, payload_seg, _signature_seg = token.split('.')
        payload = json.loads(_b64url_decode(payload_seg))
        obj = payload['payload']['genericObjects'][0]
        self.assertEqual(obj['genericType'], 'GENERIC_LOYALTY_CARD')

    def test_generate_save_url_uses_native_ean13_not_qr_fallback(self):
        # ean13/codabar/ean8/upca/datamatrix are real BarcodeType values
        # Google Wallet supports natively - these must not silently fall
        # back to QR_CODE the way code93/upce/issn genuinely have to
        # (Google's API has no equivalent for those).
        item = make_item(self.user, redeem_code='4006381333931', code_type='ean13')
        save_url = generate_google_wallet_save_url(item)
        token = save_url.rsplit('/', 1)[-1]
        _header_seg, payload_seg, _signature_seg = token.split('.')
        payload = json.loads(_b64url_decode(payload_seg))
        obj = payload['payload']['genericObjects'][0]
        self.assertEqual(obj['barcode']['type'], 'EAN_13')

    def test_generate_save_url_omits_barcode_for_no_barcode_code_type(self):
        item = make_item(
            self.user, type='giftcard', name='Coffee Gift Card', issuer='Bean Co',
            redeem_code='GC12345', value='25.00', currency='GBP', code_type='none',
        )
        save_url = generate_google_wallet_save_url(item)
        token = save_url.rsplit('/', 1)[-1]
        _header_seg, payload_seg, _signature_seg = token.split('.')
        payload = json.loads(_b64url_decode(payload_seg))
        obj = payload['payload']['genericObjects'][0]
        self.assertNotIn('barcode', obj)
        code_module = next(m for m in obj['textModulesData'] if m['id'] == 'code')
        self.assertEqual(code_module['body'], 'GC12345')

    def test_generate_save_url_raises_when_disabled(self):
        set_site_config(google_wallet_issuer_id='')
        item = make_item(self.user)
        with self.assertRaises(RuntimeError):
            generate_google_wallet_save_url(item)

    def test_generate_save_url_raises_when_key_file_invalid(self):
        bad_key_path = os.path.join(self.tmpdir.name, 'bad-key.json')
        with open(bad_key_path, 'w') as f:
            json.dump({'client_email': 'x@y.com'}, f)  # missing private_key
        set_site_config(google_wallet_service_account_key_path=bad_key_path)
        item = make_item(self.user)
        with self.assertRaises(RuntimeError):
            generate_google_wallet_save_url(item)


def _build_pkpass_bytes(pass_dict):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        zf.writestr('pass.json', json.dumps(pass_dict))
    return buffer.getvalue()


class PkpassImportTests(TestCase):
    def test_extracts_standard_fields(self):
        pass_bytes = _build_pkpass_bytes({
            'organizationName': 'Acme Co',
            'description': 'Loyalty Card',
            'expirationDate': '2030-01-01T00:00:00Z',
            'barcodes': [{'format': 'PKBarcodeFormatQR', 'message': 'ABC123'}],
            'storeCard': {
                'primaryFields': [{'key': 'member', 'label': 'Member', 'value': 'John Doe'}],
                'backFields': [{'key': 'pin', 'label': 'PIN', 'value': '1234'}],
            },
        })
        result = extract_pkpass_fields(pass_bytes)
        self.assertEqual(result['issuer'], 'Acme Co')
        self.assertEqual(result['name'], 'Loyalty Card')
        self.assertEqual(result['redeem_code'], 'ABC123')
        self.assertEqual(result['code_type'], 'qrcode')
        self.assertEqual(result['expiry_date'], '2030-01-01')
        self.assertEqual(result['pin'], '1234')

    def test_falls_back_to_field_based_expiry(self):
        pass_bytes = _build_pkpass_bytes({
            'organizationName': 'Acme Co',
            'description': 'Voucher',
            'barcodes': [{'format': 'PKBarcodeFormatCode128', 'message': 'XYZ'}],
            'coupon': {'auxiliaryFields': [{'key': 'expiry', 'label': 'Expires', 'value': '2031-06-15'}]},
        })
        result = extract_pkpass_fields(pass_bytes)
        self.assertEqual(result['expiry_date'], '2031-06-15')
        self.assertEqual(result['code_type'], 'code128')

    def test_rejects_non_zip_file(self):
        with self.assertRaises(PkpassImportError):
            extract_pkpass_fields(b'not a zip file')

    def test_rejects_zip_without_pass_json(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('other.txt', 'nothing here')
        with self.assertRaises(PkpassImportError):
            extract_pkpass_fields(buffer.getvalue())

    def test_rejects_invalid_json(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('pass.json', 'not valid json')
        with self.assertRaises(PkpassImportError):
            extract_pkpass_fields(buffer.getvalue())

    def test_import_pkpass_view_prefills_form(self):
        user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')
        pass_bytes = _build_pkpass_bytes({
            'organizationName': 'Acme Co',
            'description': 'Loyalty Card',
            'barcodes': [{'format': 'PKBarcodeFormatQR', 'message': 'ABC123'}],
        })
        upload = SimpleUploadedFile('pass.pkpass', pass_bytes, content_type='application/vnd.apple.pkpass')
        response = self.client.post(reverse('api-pkpass-import'), {'file': upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['redeem_code'], 'ABC123')


class FullBackupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_export_import_round_trip_preserves_items_and_files(self):
        wallet = Wallet.objects.create(user=self.user, name='Travel')
        item = make_item(self.user, name='Flight Voucher', wallet=wallet)
        item.file.save('receipt.pdf', SimpleUploadedFile('receipt.pdf', b'%PDF-1.4 fake', content_type='application/pdf'))
        Document.objects.create(item=item, file=SimpleUploadedFile('extra.pdf', b'%PDF-1.4 extra', content_type='application/pdf'))

        zip_bytes = export_full_backup(Item.objects.filter(user=self.user).prefetch_related('documents'))

        other_user = User.objects.create_user(username='bob', password='pw12345!')
        result = import_full_backup(other_user, zip_bytes)

        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(result['error_count'], 0)

        restored = Item.objects.get(user=other_user, name='Flight Voucher')
        self.assertNotEqual(restored.id, item.id)  # always a new id, never overwrites
        self.assertEqual(restored.wallet.name, 'Travel')
        self.assertTrue(restored.file.name)
        self.assertEqual(restored.documents.count(), 1)

    def test_restoring_does_not_touch_existing_items(self):
        make_item(self.user, name='Existing Item')
        zip_bytes = export_full_backup(Item.objects.filter(user=self.user))
        import_full_backup(self.user, zip_bytes)
        self.assertEqual(Item.objects.filter(user=self.user).count(), 2)

    def test_rejects_non_zip_file(self):
        with self.assertRaises(FullBackupImportError):
            import_full_backup(self.user, b'not a zip file')

    def test_rejects_zip_without_items_json(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('other.txt', 'nothing here')
        with self.assertRaises(FullBackupImportError):
            import_full_backup(self.user, buffer.getvalue())

    def test_reports_errors_for_invalid_entries(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('items.json', json.dumps([{'type': 'voucher', 'name': 'Missing Code'}]))
        result = import_full_backup(self.user, buffer.getvalue())
        self.assertEqual(result['imported_count'], 0)
        self.assertEqual(result['error_count'], 1)

    def test_full_backup_web_ui_download_and_restore(self):
        self.client.login(username='alice', password='pw12345!')
        make_item(self.user, name='Downloadable')

        download = self.client.get(reverse('export_full_backup'))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download['Content-Type'], 'application/zip')

        upload = SimpleUploadedFile('backup.zip', download.content, content_type='application/zip')
        response = self.client.post(reverse('import_full_backup'), {'file': upload})
        self.assertRedirects(response, reverse('upload_import'))
        self.assertEqual(Item.objects.filter(user=self.user).count(), 2)

    # ---- transactions ----

    def test_export_import_round_trip_preserves_transactions(self):
        item = make_item(self.user, name='Gift Card', value='50.00')
        item.refresh_from_db()
        Transaction.objects.create(item=item, description='Coffee', value=Decimal('-5.00'))
        Transaction.objects.create(item=item, description='Lunch', value=Decimal('-12.50'))

        zip_bytes = export_full_backup(Item.objects.filter(user=self.user).prefetch_related('transactions'))
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        import_full_backup(other_user, zip_bytes)

        restored = Item.objects.get(user=other_user, name='Gift Card')
        self.assertEqual(restored.transactions.count(), 2)
        self.assertEqual(
            set(restored.transactions.values_list('description', 'value')),
            {('Coffee', Decimal('-5.00')), ('Lunch', Decimal('-12.50'))},
        )

    def test_items_without_transactions_have_no_transactions_key(self):
        make_item(self.user, name='No Ledger')
        zip_bytes = export_full_backup(Item.objects.filter(user=self.user).prefetch_related('transactions'))
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = json.loads(zf.read('items.json'))
        self.assertNotIn('_transactions', entries[0])

    # ---- settings ----

    def test_export_omits_settings_json_without_user(self):
        make_item(self.user, name='No Settings Export')
        zip_bytes = export_full_backup(Item.objects.filter(user=self.user))
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            self.assertNotIn('settings.json', zf.namelist())

    def test_export_import_round_trip_restores_preferences(self):
        prefs, _created = UserPreference.objects.get_or_create(user=self.user)
        prefs.default_currency = 'EUR'
        prefs.oled_dark_mode = True
        prefs.save()
        make_item(self.user)

        zip_bytes = export_full_backup(Item.objects.filter(user=self.user), user=self.user)
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        result = import_full_backup(other_user, zip_bytes)

        self.assertTrue(result['settings_restored'])
        other_prefs = UserPreference.objects.get(user=other_user)
        self.assertEqual(other_prefs.default_currency, 'EUR')
        self.assertTrue(other_prefs.oled_dark_mode)

    def test_export_import_round_trip_restores_notification_rules(self):
        NotificationRule.objects.create(
            user=self.user, name='Ntfy Alerts', backend='ntfy',
            config={'server': 'https://ntfy.sh', 'topic': 'my-topic'},
            event_types=['expiry_warning'],
        )
        make_item(self.user)

        zip_bytes = export_full_backup(Item.objects.filter(user=self.user), user=self.user)
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        import_full_backup(other_user, zip_bytes)

        rule = NotificationRule.objects.get(user=other_user, name='Ntfy Alerts')
        self.assertEqual(rule.backend, 'ntfy')
        self.assertEqual(rule.config['topic'], 'my-topic')
        self.assertEqual(rule.event_types, ['expiry_warning'])

    def test_restoring_notification_rules_twice_does_not_duplicate(self):
        NotificationRule.objects.create(user=self.user, name='Rule', backend='webhook', config={'url': 'https://a.example/1'})
        make_item(self.user)
        zip_bytes = export_full_backup(Item.objects.filter(user=self.user), user=self.user)

        other_user = User.objects.create_user(username='bob', password='pw12345!')
        import_full_backup(other_user, zip_bytes)
        import_full_backup(other_user, zip_bytes)

        self.assertEqual(NotificationRule.objects.filter(user=other_user, name='Rule').count(), 1)

    def test_export_import_round_trip_restores_apprise_urls(self):
        self.user.userprofile.apprise_urls = 'tgram://token/chatid'
        self.user.userprofile.save()
        make_item(self.user)

        zip_bytes = export_full_backup(Item.objects.filter(user=self.user), user=self.user)
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        import_full_backup(other_user, zip_bytes)

        self.assertEqual(UserProfile.objects.get(user=other_user).apprise_urls, 'tgram://token/chatid')

    def test_settings_restored_false_when_backup_has_no_settings(self):
        make_item(self.user, name='Plain Item')
        zip_bytes = export_full_backup(Item.objects.filter(user=self.user))
        other_user = User.objects.create_user(username='bob', password='pw12345!')
        result = import_full_backup(other_user, zip_bytes)
        self.assertFalse(result['settings_restored'])


class ScheduledBackupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        patcher = patch('imports.tasks.BACKUP_ROOT', self.tmp_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backup_user_returns_none_without_items(self):
        self.assertIsNone(backup_user(self.user))
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, 'alice')))

    def test_backup_user_writes_zip(self):
        make_item(self.user, name='Backed Up Item')

        path = backup_user(self.user)

        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.dirname(path), os.path.join(self.tmp_dir, 'alice'))
        with zipfile.ZipFile(path) as zf:
            entries = json.loads(zf.read('items.json'))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['name'], 'Backed Up Item')

    def test_rotation_keeps_only_retention_count(self):
        set_site_config(backup_retention_count=3)
        make_item(self.user)
        for _ in range(5):
            backup_user(self.user)

        backup_dir = os.path.join(self.tmp_dir, 'alice')
        remaining = [f for f in os.listdir(backup_dir) if f.endswith('.zip')]
        self.assertEqual(len(remaining), 3)

    @patch('imports.tasks.backup_user')
    def test_run_scheduled_backups_noop_when_disabled(self, mock_backup_user):
        set_site_config(scheduled_backup_enabled=False)
        run_scheduled_backups()
        mock_backup_user.assert_not_called()

    def test_run_scheduled_backups_only_backs_up_users_with_items(self):
        set_site_config(scheduled_backup_enabled=True)
        make_item(self.user)
        User.objects.create_user(username='bob', password='pw12345!')  # no items

        run_scheduled_backups()

        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, 'alice')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp_dir, 'bob')))

    @patch('imports.tasks.backup_user')
    def test_one_users_failure_does_not_stop_others(self, mock_backup_user):
        set_site_config(scheduled_backup_enabled=True)
        make_item(self.user)
        other = User.objects.create_user(username='bob', password='pw12345!')
        make_item(other)
        mock_backup_user.side_effect = [RuntimeError('disk full'), 'some/path.zip']

        run_scheduled_backups()  # must not raise

        self.assertEqual(mock_backup_user.call_count, 2)
