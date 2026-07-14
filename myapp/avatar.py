import hashlib
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# A merchant's own logo is always preferred (see merchant_logos.py) - this
# is only the fallback when nothing is cached, so a share/link-preview
# never falls back to VoucherVault's own app icon, which would misrepresent
# whose voucher is being shared. Same visual pattern as Gmail/Slack contact
# avatars: a deterministic-per-name colour plus the name's first letter.
_PALETTE = [
    '#4F46E5', '#059669', '#DC2626', '#D97706',
    '#7C3AED', '#0891B2', '#DB2777', '#65A30D',
]
_FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def _color_for(name: str) -> str:
    digest = hashlib.md5(name.encode('utf-8')).hexdigest()
    return _PALETTE[int(digest, 16) % len(_PALETTE)]


def generate_initial_avatar(name: str, size: int = 512) -> bytes:
    """
    Renders a circular "initial" avatar (first letter of `name`, on a
    colour deterministic for that name) as PNG bytes. Falls back to PIL's
    built-in bitmap font if the bundled DejaVu TTF isn't present, so this
    degrades to an ugly-but-working avatar rather than raising.
    """
    letter = (name or '?').strip()[:1].upper() or '?'
    color = _color_for(name or '')

    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size, size), fill=color)

    try:
        font = ImageFont.truetype(_FONT_PATH, int(size * 0.5))
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), letter, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text(
        ((size - text_width) / 2 - bbox[0], (size - text_height) / 2 - bbox[1]),
        letter, fill='#FFFFFF', font=font,
    )

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def normalize_logo_image(content: bytes, size: int = 256) -> bytes:
    """
    Re-encodes a fetched merchant logo/favicon to a consistently-sized PNG
    using smooth (Lanczos) resampling. Some sources (Google's favicon
    service especially) return whatever native resolution a domain's
    favicon actually has - often just 32-48px for anything but the
    biggest brands - and without this, that gets blockily stretched by
    whatever ends up displaying it (a WhatsApp chat bubble, a Web Share
    preview), which is what "pixelated logo" share reports turn out to
    be. Preserves aspect ratio, centered on a transparent square canvas,
    so a non-square source doesn't get distorted.

    Returns the original bytes unchanged if they can't be parsed as a
    raster image (e.g. an SVG, or corrupt/truncated data) rather than
    raising - the caller still has *something* to serve.
    """
    try:
        image = Image.open(BytesIO(content)).convert('RGBA')
    except Exception:
        return content

    width, height = image.size
    if not width or not height:
        return content

    scale = size / max(width, height)
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    image = image.resize(new_size, Image.LANCZOS)

    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    canvas.paste(image, ((size - new_size[0]) // 2, (size - new_size[1]) // 2), image)

    buffer = BytesIO()
    canvas.save(buffer, format='PNG')
    return buffer.getvalue()
