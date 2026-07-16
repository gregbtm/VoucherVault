from io import BytesIO

from PIL import Image

# 8x8 -> 64 bits. Large enough to distinguish genuinely different photos,
# small enough that ordinary re-compression/re-crop/lighting differences
# between two photos of the same physical card still land well within a
# sane duplicate-detection threshold of each other (see
# SiteConfiguration.duplicate_photo_threshold, checked by callers - 0 is
# byte-for-byte identical after resizing, low single digits is the
# conventional dHash threshold for "same picture, different compression/
# crop/lighting").
_HASH_SIZE = 8


def compute_dhash(image_bytes: bytes) -> str:
    """
    Difference hash (dHash): downscale to a tiny 9x8 greyscale thumbnail
    and record, for each pixel, whether it's brighter than its right
    neighbour - one bit per comparison, 64 bits total. Unlike a
    cryptographic hash (sha256 etc.), which differs completely for even a
    single changed byte, two photos of the same physical card - shot
    seconds apart, slightly different crop/angle/compression - collapse to
    a near-identical fingerprint here, which is exactly what "did I already
    upload this photo" needs.

    Returns '' if the bytes can't be parsed as a raster image (corrupt/
    truncated data, or something that isn't an image at all) rather than
    raising - callers should treat that as "no hash available", not a
    hard failure blocking the actual save.
    """
    try:
        image = Image.open(BytesIO(image_bytes)).convert('L').resize(
            (_HASH_SIZE + 1, _HASH_SIZE), Image.LANCZOS,
        )
    except Exception:
        return ''

    pixels = list(image.getdata())
    bits = []
    for row in range(_HASH_SIZE):
        offset = row * (_HASH_SIZE + 1)
        for col in range(_HASH_SIZE):
            bits.append('1' if pixels[offset + col] > pixels[offset + col + 1] else '0')
    return f'{int("".join(bits), 2):016x}'


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """
    Differing bits between two hex-encoded dHash strings. Returns a
    distance larger than any real threshold (rather than raising or
    silently matching) if either hash is missing/blank, so a caller
    comparing against its own threshold never has to special-case "no hash".
    """
    if not hash_a or not hash_b:
        return _HASH_SIZE * _HASH_SIZE
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count('1')
