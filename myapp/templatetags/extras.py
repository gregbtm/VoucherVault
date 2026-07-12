from django import template
import os
import re
from django.conf import settings

from myapp.models import SiteConfiguration

register = template.Library()

@register.filter
def env(key):
    if key == "OIDC_ENABLED":
        return settings.OIDC_ENABLED
    if key == "OIDC_AUTOLOGIN":
        return settings.OIDC_AUTOLOGIN
    if key == "VERSION":
        return settings.VERSION
    if key == "EXPIRY_THRESHOLD":
        return SiteConfiguration.load().expiry_threshold_days

@register.filter()
def comma_to_dot(value):
    return str(value).replace(',', '.')

@register.filter
def heat_level(count):
    """Buckets a day's expiring-item count into 0-4 for the calendar's
    sequential single-hue (amber) magnitude encoding."""
    if not count:
        return 0
    if count <= 2:
        return 1
    if count <= 4:
        return 2
    if count <= 7:
        return 3
    return 4

@register.filter
def in_list(value, the_list):
    """Membership check that tolerates str/int mismatches (e.g. form field
    values submitted as strings vs. model PKs as ints)."""
    try:
        return str(value) in [str(item) for item in the_list]
    except TypeError:
        return False

@register.filter
def is_image_file(filename):
    if not filename:
        return False
    return bool(re.search(r'\.(jpg|jpeg|png)$', filename.lower()))

@register.filter
def basename(path):
    if not path:
        return ''
    return os.path.basename(str(path))


def clamp(value, min_value=0, max_value=255):
    return max(min_value, min(value, max_value))

@register.filter
@register.filter
def darken(hex_color, amount=20):
    try:
        if not hex_color:
            return '#placeholder'

        hex_color = hex_color.strip().lstrip('#')

        # Propagate placeholder for frontend JS
        if hex_color.lower() in ['placeholder']:
            return '#placeholder'

        # Special default fallbacks
        if hex_color.lower() == '1e1e1e':
            return '#2a2a2a'
        elif hex_color.lower() == 'f3f3f3':
            return '#e0e0e0'

        # Shorthand hex like #abc
        if len(hex_color) == 3:
            hex_color = ''.join([c * 2 for c in hex_color])

        if isinstance(amount, str):
            amount = re.sub(r'[^0-9]', '', amount)

        amount = int(amount)

        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)

        r = clamp(r - amount)
        g = clamp(g - amount)
        b = clamp(b - amount)

        return f'rgb({r}, {g}, {b})'
    except Exception:
        return '#placeholder'  # Fallback to placeholder if error
