"""
PDF eTicket support: rasterizing a PDF page to an image and decoding a
barcode out of it server-side.

This exists because VoucherVault's existing barcode *decoding* (the
camera/file scan on create-item/edit-item) runs entirely client-side in the
browser via ZXing-js - fine for a human with a browser, but useless for an
unattended pipeline with no browser in the loop (an n8n workflow forwarding
an emailed PDF eTicket, for example). zxing-cpp gives the same ZXing decode
engine a server-side, Python-callable form. PyMuPDF (fitz) does the
PDF->image rasterization; it ships a self-contained wheel with its own
bundled MuPDF build, so no system package (poppler, ghostscript, etc.) is
required beyond what's already needed for treepoem's barcode generation.
"""
import io

import fitz
import zxingcpp
from PIL import Image

# zxing-cpp's BarcodeFormat enum names, mapped to the code_type strings this
# app already uses everywhere else (Item.code_type, the create/edit-item
# dropdown, generate_code_image_base64 in utils.py). Deliberately mirrors
# scanner.js's client-side barcodeFormatMap so a ticket decoded server-side
# and one decoded by a camera scan land on the same code_type for the same
# symbology.
_ZXING_FORMAT_MAP = {
    'Aztec': 'azteccode',
    'Codabar': 'codabar',
    'Code39': 'code39',
    'Code93': 'code93',
    'Code128': 'code128',
    'DataMatrix': 'datamatrix',
    'EAN8': 'ean8',
    'EAN13': 'ean13',
    'ITF': 'interleaved2of5',
    'MaxiCode': 'datamatrix',
    'PDF417': 'pdf417',
    'QRCode': 'qrcode',
    'DataBar': 'ean13',
    'DataBarExp': 'ean13',
    'UPCA': 'upca',
    'UPCE': 'upce',
}


def rasterize_pdf(pdf_bytes, dpi=300):
    """
    Renders every page of a PDF to a list of PIL Images, high enough
    resolution (300dpi default) for a dense Aztec/Data Matrix symbol to
    still decode reliably - eTicket barcodes are typically printed small.
    """
    images = []
    with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
        for page in doc:
            pixmap = page.get_pixmap(dpi=dpi)
            mode = 'RGB' if pixmap.n < 4 else 'RGBA'
            image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
            images.append(image.convert('RGB'))
    return images


def decode_barcode_from_image(image):
    """
    Finds and decodes the first barcode in a PIL Image, in the same
    symbology universe the rest of the app already supports. Returns
    (redeem_code, code_type) or (None, None) if nothing decodable is found.
    """
    result = zxingcpp.read_barcode(image)
    if result is None or not result.valid:
        return None, None
    code_type = _ZXING_FORMAT_MAP.get(result.format.name, None)
    if code_type is None:
        return None, None
    return result.text, code_type


def decode_barcode_from_pdf(pdf_bytes, dpi=300):
    """
    Rasterizes every page of a PDF and returns the first barcode found
    (most eTickets are single-page with one barcode) as (redeem_code,
    code_type), or (None, None) if no page contains a decodable barcode.
    """
    for image in rasterize_pdf(pdf_bytes, dpi=dpi):
        redeem_code, code_type = decode_barcode_from_image(image)
        if redeem_code is not None:
            return redeem_code, code_type
    return None, None


def pdf_page_count(pdf_bytes):
    """Returns the number of pages in a PDF."""
    with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
        return len(doc)


def iter_pdf_pages(pdf_bytes, dpi=300):
    """
    Yields (page_index, png_bytes, redeem_code, code_type) for every page.
    The barcode fields are (None, None) if the page contains no decodable barcode.
    """
    images = rasterize_pdf(pdf_bytes, dpi=dpi)
    for idx, image in enumerate(images):
        buf = io.BytesIO()
        image.save(buf, 'PNG')
        png_bytes = buf.getvalue()
        redeem_code, code_type = decode_barcode_from_image(image)
        yield idx, png_bytes, redeem_code, code_type


def pdf_page_to_png_bytes(pdf_bytes, page_number=0, dpi=200):
    """
    Renders a single PDF page to PNG bytes - used to hand a rasterized page
    to the existing OCR vision backends (Claude/OpenAI), which take images,
    not PDFs.
    """
    images = rasterize_pdf(pdf_bytes, dpi=dpi)
    if not images:
        raise ValueError("PDF has no pages")
    if page_number >= len(images):
        page_number = 0
    buffer = io.BytesIO()
    images[page_number].save(buffer, 'PNG')
    return buffer.getvalue()
