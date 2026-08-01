"""Export formatters for third-party integrations and standard formats."""

import csv
import json
from datetime import datetime
from decimal import Decimal
from io import StringIO, BytesIO

from django.utils import timezone


class YNABExporter:
    """Export items as YNAB (You Need A Budget) transactions."""

    CATEGORY_MAP = {
        'gift_card': 'Gifts',
        'voucher': 'Gifts',
        'coupon': 'Shopping',
        'loyalty_card': 'Hobbies',
        'travel_pass': 'Travel',
    }

    @staticmethod
    def export_csv(items):
        """Generate YNAB-compatible CSV: Date, Payee, Category, Amount."""
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Date', 'Payee', 'Category', 'Amount'])

        for item in items:
            date = (item.issue_date or item.created_at).strftime('%m/%d/%Y')
            payee = item.name or 'Unknown'
            category = YNABExporter.CATEGORY_MAP.get(item.type, 'Other')
            amount = f"-{item.value}" if item.value else "0"
            writer.writerow([date, payee, category, amount])

        return output.getvalue()


class FireflyExporter:
    """Export items as Firefly III transactions JSON."""

    @staticmethod
    def export_json(items, account_id: int):
        """Generate Firefly III compatible transaction array."""
        transactions = []

        for item in items:
            transactions.append({
                'type': 'withdrawal',
                'date': (item.issue_date or item.created_at).isoformat(),
                'amount': str(item.value or '0'),
                'description': item.name or 'Voucher/Gift Card',
                'tags': [item.type],
                'source_id': account_id,
                'destination_name': item.name or 'Merchant',
                'notes': item.description or '',
            })

        return json.dumps({'transactions': transactions}, indent=2)


class SpreadsheetExporter:
    """Export items as structured spreadsheet CSV with detail columns."""

    @staticmethod
    def export_csv(items):
        """Generate detailed spreadsheet CSV for Excel/Google Sheets."""
        output = StringIO()
        writer = csv.writer(output)

        headers = [
            'Name', 'Type', 'Value', 'Currency', 'Issuer', 'Redeem Code',
            'Issue Date', 'Expiry Date', 'Last Used', 'Status',
            'Description', 'Wallet', 'Tags', 'Category'
        ]
        writer.writerow(headers)

        for item in items:
            tags = ', '.join(t.name for t in item.tags.all()) if hasattr(item, 'tags') else ''
            category = getattr(item, 'category', None)
            category_name = category.category if category else ''

            writer.writerow([
                item.name,
                item.type,
                item.value,
                item.currency,
                item.issuer or '',
                item.redeem_code or '',
                item.issue_date.strftime('%Y-%m-%d') if item.issue_date else '',
                item.expiry_date.strftime('%Y-%m-%d') if item.expiry_date else '',
                item.last_used_at.strftime('%Y-%m-%d %H:%M') if item.last_used_at else '',
                'Used' if item.is_used else 'Active',
                item.description or '',
                item.wallet.name if item.wallet else '',
                tags,
                category_name,
            ])

        return output.getvalue()


class QIFExporter:
    """Export item transaction history as QIF (Quicken Interchange Format).

    Unlike YNABExporter/FireflyExporter above (both dead code - never
    imported outside this module), this one backs a live, wired export
    button (myapp/views.py::export_selected_qif). It exports the actual
    Transaction ledger for the selected items, not item metadata - the CSV/
    JSON bulk export already covers that, so a QIF/OFX import into desktop
    finance software (GnuCash, Quicken) is only useful if it carries the
    real spend/top-up history.
    """

    @staticmethod
    def export(transactions):
        lines = ['!Type:Cash']
        for tx in transactions:
            lines.append(f"D{tx.date.strftime('%m/%d/%Y')}")
            lines.append(f"T{tx.value}")
            lines.append(f"P{tx.item.name}")
            if tx.description:
                lines.append(f"M{tx.description}")
            lines.append('^')
        return '\n'.join(lines) + '\n'


class OFXExporter:
    """Export item transaction history as OFX 1.02 (SGML) bank statement."""

    @staticmethod
    def export(transactions):
        transactions = list(transactions)
        now = timezone.now().strftime('%Y%m%d%H%M%S')
        if transactions:
            dates = [tx.date for tx in transactions]
            dtstart = min(dates).strftime('%Y%m%d')
            dtend = max(dates).strftime('%Y%m%d')
            currency = getattr(transactions[0].item, 'currency', None) or 'USD'
        else:
            dtstart = dtend = timezone.now().strftime('%Y%m%d')
            currency = 'USD'

        stmttrns = []
        for tx in transactions:
            trntype = 'DEBIT' if tx.value < 0 else 'CREDIT'
            name = (tx.item.name or 'VoucherVault')[:32]
            stmttrns.append(
                f"<STMTTRN>\n<TRNTYPE>{trntype}\n"
                f"<DTPOSTED>{tx.date.strftime('%Y%m%d%H%M%S')}\n"
                f"<TRNAMT>{tx.value}\n<FITID>{tx.id}\n<NAME>{name}\n"
                f"<MEMO>{tx.description}\n</STMTTRN>"
            )
        body = '\n'.join(stmttrns)

        return (
            "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
            "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
            "OLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
            "<OFX>\n<SIGNONMSGSRSV1>\n<SONRS>\n<STATUS>\n<CODE>0\n"
            "<SEVERITY>INFO\n</STATUS>\n"
            f"<DTSERVER>{now}\n<LANGUAGE>ENG\n</SONRS>\n</SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1>\n<STMTTRNRS>\n<TRNUID>1\n<STATUS>\n<CODE>0\n"
            "<SEVERITY>INFO\n</STATUS>\n<STMTRS>\n"
            f"<CURDEF>{currency}\n<BANKACCTFROM>\n<BANKID>000000000\n"
            "<ACCTID>VoucherVault\n<ACCTTYPE>CHECKING\n</BANKACCTFROM>\n"
            f"<BANKTRANLIST>\n<DTSTART>{dtstart}\n<DTEND>{dtend}\n"
            f"{body}\n</BANKTRANLIST>\n</STMTRS>\n</STMTTRNRS>\n"
            "</BANKMSGSRSV1>\n</OFX>\n"
        )


class BankStatementParser:
    """Parse bank/credit card statements to extract transaction patterns."""

    MERCHANT_PATTERNS = {
        'amazon': r'AMAZON|AMZN',
        'apple': r'APPLE\.COM|APP STORE|ITUNES',
        'google': r'GOOGLE|PLAY STORE|YOUTUBE',
        'starbucks': r'STARBUCKS|SBUX',
        'uber': r'UBER|LYFT',
        'subscriptions': r'MONTHLY|SUBSCRIPTION|PREMIUM',
    }

    @staticmethod
    def parse_csv(csv_content: str, date_format='%m/%d/%Y'):
        """Parse bank CSV and extract likely gift card/voucher purchases."""
        import re
        lines = csv_content.strip().split('\n')
        reader = csv.DictReader(lines)
        transactions = []

        for row in reader:
            amount = float(row.get('Amount', 0) or 0)
            description = row.get('Description', '') or row.get('Memo', '')
            date_str = row.get('Date', '')

            if not description or amount <= 0:
                continue

            merchant = None
            for key, pattern in BankStatementParser.MERCHANT_PATTERNS.items():
                if re.search(pattern, description, re.IGNORECASE):
                    merchant = key
                    break

            if merchant or any(word in description.lower() for word in
                             ['gift', 'card', 'voucher', 'coupon', 'reload', 'balance']):
                transactions.append({
                    'date': date_str,
                    'description': description,
                    'amount': amount,
                    'merchant': merchant or 'Other',
                    'likely_voucher': True,
                })

        return transactions


class ReceiptParser:
    """Parse receipt images/PDFs to extract purchase details."""

    AMOUNT_PATTERNS = [
        r'(?:Total|Amount|Price)[\s:]*\$?([\d,]+\.?\d*)',
        r'([\d,]+\.?\d*)\s*(?:USD|GBP|EUR)',
    ]

    @staticmethod
    def extract_text_from_pdf(pdf_path):
        """Extract text from PDF (requires pdfplumber or similar)."""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
                return text
        except ImportError:
            return None

    @staticmethod
    def parse_receipt_text(text: str):
        """Parse receipt text for amount, merchant, date, and items."""
        import re
        result = {
            'merchant': None,
            'amount': None,
            'date': None,
            'items': [],
        }

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            for pattern in ReceiptParser.AMOUNT_PATTERNS:
                match = re.search(pattern, line)
                if match and result['amount'] is None:
                    try:
                        result['amount'] = float(match.group(1).replace(',', ''))
                    except (ValueError, IndexError):
                        pass

            if len(line) > 3 and len(line) < 100 and not result['merchant']:
                if any(word in line.lower() for word in ['store', 'shop', 'mall', 'market']):
                    result['merchant'] = line[:50]

        return result
