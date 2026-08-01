import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.export_formats import OFXExporter, QIFExporter
from myapp.models import Transaction
from myapp.tests import make_item


class QIFExporterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user, name='Coffee Card', value='20.00')

    def test_export_emits_one_record_per_transaction(self):
        tx = Transaction.objects.create(item=self.item, description='Latte', value=Decimal('-3.50'))
        output = QIFExporter.export(Transaction.objects.filter(pk=tx.pk))
        self.assertTrue(output.startswith('!Type:Cash\n'))
        self.assertIn(f"D{tx.date.strftime('%m/%d/%Y')}", output)
        self.assertIn('T-3.50', output)
        self.assertIn('PCoffee Card', output)
        self.assertIn('MLatte', output)
        self.assertEqual(output.count('^'), 1)

    def test_export_with_no_transactions_is_just_the_header(self):
        output = QIFExporter.export(Transaction.objects.none())
        self.assertEqual(output, '!Type:Cash\n')


class OFXExporterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user, name='Coffee Card', value='20.00', currency='GBP')

    def test_export_marks_a_spend_as_debit(self):
        tx = Transaction.objects.create(item=self.item, description='Latte', value=Decimal('-3.50'))
        output = OFXExporter.export(Transaction.objects.filter(pk=tx.pk))
        self.assertIn('<TRNTYPE>DEBIT', output)
        self.assertIn('<TRNAMT>-3.50', output)
        self.assertIn(f'<FITID>{tx.id}', output)
        self.assertIn('<CURDEF>GBP', output)

    def test_export_marks_a_topup_as_credit(self):
        tx = Transaction.objects.create(item=self.item, description='Top up', value=Decimal('10.00'))
        output = OFXExporter.export(Transaction.objects.filter(pk=tx.pk))
        self.assertIn('<TRNTYPE>CREDIT', output)

    def test_export_with_no_transactions_still_produces_a_valid_envelope(self):
        output = OFXExporter.export(Transaction.objects.none())
        self.assertIn('<OFX>', output)
        self.assertIn('</OFX>', output)
        self.assertIn('<CURDEF>USD', output)


class ExportSelectedQifOfxViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')
        self.item = make_item(self.user, name='Coffee Card', value='20.00')
        self.other_item = make_item(self.other_user, name='Bob Card', value='20.00')
        self.tx = Transaction.objects.create(item=self.item, description='Latte', value=Decimal('-3.50'))
        self.other_tx = Transaction.objects.create(item=self.other_item, description='Snack', value=Decimal('-1.00'))
        self.client.force_login(self.user)

    def _post(self, url_name, item_ids):
        return self.client.post(
            reverse(url_name),
            data=json.dumps({'item_ids': item_ids}),
            content_type='application/json',
        )

    def test_qif_export_returns_qif_content_type_and_attachment(self):
        response = self._post('export_selected_qif', [str(self.item.id)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/qif')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('Latte', response.content.decode())

    def test_ofx_export_returns_ofx_content_type_and_attachment(self):
        response = self._post('export_selected_ofx', [str(self.item.id)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/x-ofx')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('Latte', response.content.decode())

    def test_qif_export_excludes_another_users_transactions(self):
        response = self._post('export_selected_qif', [str(self.item.id), str(self.other_item.id)])
        self.assertNotIn('Snack', response.content.decode())

    def test_ofx_export_excludes_another_users_transactions(self):
        response = self._post('export_selected_ofx', [str(self.item.id), str(self.other_item.id)])
        self.assertNotIn('Snack', response.content.decode())

    def test_qif_export_requires_login(self):
        self.client.logout()
        response = self._post('export_selected_qif', [str(self.item.id)])
        self.assertEqual(response.status_code, 302)

    def test_qif_export_requires_post(self):
        response = self.client.get(reverse('export_selected_qif'))
        self.assertEqual(response.status_code, 405)
