import io

import fitz
import treepoem
from django.test import SimpleTestCase
from PIL import Image

from .pdf_ticket import (
    decode_barcode_from_image,
    decode_barcode_from_pdf,
    pdf_page_to_png_bytes,
    rasterize_pdf,
)


def _barcode_image(barcode_type, data):
    barcode = treepoem.generate_barcode(barcode_type=barcode_type, data=data, scale=2)
    buffer = io.BytesIO()
    barcode.save(buffer, 'PNG')
    buffer.seek(0)
    return Image.open(buffer).convert('RGB')


def _pdf_with_image(image, page_size=(400, 400), image_rect=(50, 50, 350, 350)):
    """Builds a single-page PDF with the given PIL image placed on it."""
    doc = fitz.open()
    page = doc.new_page(width=page_size[0], height=page_size[1])
    image_bytes = io.BytesIO()
    image.save(image_bytes, 'PNG')
    page.insert_image(fitz.Rect(*image_rect), stream=image_bytes.getvalue())
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _blank_pdf(page_size=(400, 400)):
    doc = fitz.open()
    doc.new_page(width=page_size[0], height=page_size[1])
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class DecodeBarcodeFromImageTests(SimpleTestCase):
    def test_decodes_aztec_code(self):
        image = _barcode_image('azteccode', 'AABXC5V4LVT')
        redeem_code, code_type = decode_barcode_from_image(image)
        self.assertEqual(redeem_code, 'AABXC5V4LVT')
        self.assertEqual(code_type, 'azteccode')

    def test_decodes_qr_code(self):
        image = _barcode_image('qrcode', 'https://example.com/voucher/123')
        redeem_code, code_type = decode_barcode_from_image(image)
        self.assertEqual(redeem_code, 'https://example.com/voucher/123')
        self.assertEqual(code_type, 'qrcode')

    def test_decodes_code128(self):
        image = _barcode_image('code128', 'GC998877')
        redeem_code, code_type = decode_barcode_from_image(image)
        self.assertEqual(redeem_code, 'GC998877')
        self.assertEqual(code_type, 'code128')

    def test_blank_image_returns_none(self):
        blank = Image.new('RGB', (200, 200), color='white')
        redeem_code, code_type = decode_barcode_from_image(blank)
        self.assertIsNone(redeem_code)
        self.assertIsNone(code_type)


class DecodeBarcodeFromPdfTests(SimpleTestCase):
    def test_decodes_barcode_embedded_in_pdf(self):
        image = _barcode_image('azteccode', 'TICKET99')
        pdf_bytes = _pdf_with_image(image)
        redeem_code, code_type = decode_barcode_from_pdf(pdf_bytes)
        self.assertEqual(redeem_code, 'TICKET99')
        self.assertEqual(code_type, 'azteccode')

    def test_blank_pdf_returns_none(self):
        pdf_bytes = _blank_pdf()
        redeem_code, code_type = decode_barcode_from_pdf(pdf_bytes)
        self.assertIsNone(redeem_code)
        self.assertIsNone(code_type)

    def test_finds_barcode_on_second_page(self):
        # Multi-page PDF built manually (not via _pdf_with_image, which is
        # single-page) - first page blank, second page has the barcode, to
        # confirm every page is checked rather than assuming page one.
        image = _barcode_image('azteccode', 'SECONDPAGE')
        doc = fitz.open()
        doc.new_page(width=400, height=400)
        page2 = doc.new_page(width=400, height=400)
        image_bytes = io.BytesIO()
        image.save(image_bytes, 'PNG')
        page2.insert_image(fitz.Rect(50, 50, 350, 350), stream=image_bytes.getvalue())
        pdf_bytes = doc.tobytes()
        doc.close()

        redeem_code, code_type = decode_barcode_from_pdf(pdf_bytes)
        self.assertEqual(redeem_code, 'SECONDPAGE')
        self.assertEqual(code_type, 'azteccode')


class RasterizePdfTests(SimpleTestCase):
    def test_rasterizes_every_page(self):
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.new_page(width=200, height=200)
        doc.new_page(width=200, height=200)
        pdf_bytes = doc.tobytes()
        doc.close()

        images = rasterize_pdf(pdf_bytes)
        self.assertEqual(len(images), 3)
        for image in images:
            self.assertIsInstance(image, Image.Image)


class PdfPageToPngBytesTests(SimpleTestCase):
    def test_returns_valid_png_bytes(self):
        pdf_bytes = _blank_pdf()
        png_bytes = pdf_page_to_png_bytes(pdf_bytes)
        # PNG magic number
        self.assertTrue(png_bytes.startswith(b'\x89PNG\r\n\x1a\n'))
        # Round-trips through PIL without raising
        Image.open(io.BytesIO(png_bytes)).verify()

    def test_out_of_range_page_falls_back_to_first(self):
        pdf_bytes = _blank_pdf()
        png_bytes = pdf_page_to_png_bytes(pdf_bytes, page_number=5)
        self.assertTrue(png_bytes.startswith(b'\x89PNG\r\n\x1a\n'))
