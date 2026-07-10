import logging
import os
import re
from datetime import timedelta

import requests
from django.db.models.functions import Lower
from django.utils import timezone

from .models import MerchantProfile

logger = logging.getLogger(__name__)

CACHE_DAYS = 30
FETCH_TIMEOUT = 5

# Clearbit first (real brand logos), Google favicons as a fallback (near
# 100% hit rate but often just a generic favicon, sometimes a blank/default
# globe icon for domains that don't exist).
LOGO_SOURCES = [
    'https://logo.clearbit.com/{domain}',
    'https://www.google.com/s2/favicons?sz=64&domain={domain}',
]

_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')


def merchant_logos_enabled() -> bool:
    return os.environ.get('MERCHANT_LOGOS_ENABLED', 'true').lower() in ('true', '1', 'yes')


def guess_domain(name: str) -> str:
    """Best-effort domain guess from a free-text merchant/issuer name."""
    slug = _NON_ALNUM_RE.sub('', name.lower())
    return f'{slug}.com'


def get_cached_logo(name: str):
    """
    Read-only lookup — never makes a network call, safe to use from a
    request/template render path. Returns the MerchantProfile if one has
    been fetched (successfully or not) for this name, else None.
    """
    if not name:
        return None
    return MerchantProfile.objects.filter(name__iexact=name.strip()).first()


def get_cached_logos_for_issuers(issuers) -> dict:
    """
    Batch read-only lookup for template rendering: given an iterable of
    issuer names, returns {lowercased_issuer: logo_url} for whichever have
    a cached, successfully-fetched logo. Never makes a network call — a
    single query regardless of how many issuers are passed in.
    """
    names_lower = {name.strip().lower() for name in issuers if name and name.strip()}
    if not names_lower:
        return {}
    profiles = (
        MerchantProfile.objects
        .annotate(name_lower=Lower('name'))
        .filter(name_lower__in=names_lower)
        .exclude(logo_url='')
    )
    return {p.name_lower: p.logo_url for p in profiles}


def fetch_merchant_logo(name: str) -> MerchantProfile:
    """
    Looks up (or creates) the MerchantProfile for `name` and, on a cache
    miss or expiry, fetches a logo URL from LOGO_SOURCES. Makes real HTTP
    requests — call this from a Celery task (see fetch_merchant_logo_task),
    never directly from a request/template render path.
    """
    name = name.strip()
    # Case-insensitive get-or-create: MerchantProfile.name is exact-unique,
    # but issuer casing varies between items ("Amazon" vs "amazon"), and we
    # don't want a duplicate cache row — and thus a duplicate fetch — per
    # casing variant of the same merchant.
    profile = MerchantProfile.objects.filter(name__iexact=name).first()
    if profile is None:
        profile = MerchantProfile.objects.create(name=name)

    if profile.fetched_at and (timezone.now() - profile.fetched_at) < timedelta(days=CACHE_DAYS):
        return profile  # cache hit, still fresh

    domain = guess_domain(name)
    for template in LOGO_SOURCES:
        url = template.format(domain=domain)
        try:
            response = requests.get(url, timeout=FETCH_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning('Merchant logo fetch failed for %r via %s: %s', name, url, exc)
            continue
        if response.status_code == 200:
            profile.logo_url = url
            profile.domain = domain
            profile.fetched_at = timezone.now()
            profile.save()
            break
    else:
        # Nothing worked — still stamp fetched_at so a broken/unknown
        # merchant is only retried on the normal cache cadence, not every
        # time an item for it is saved.
        profile.fetched_at = timezone.now()
        profile.save(update_fields=['fetched_at'])

    return profile
