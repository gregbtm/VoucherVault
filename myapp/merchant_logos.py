import logging
import re
from datetime import timedelta

import requests
from django.db.models.functions import Lower
from django.utils import timezone

from .models import MerchantProfile, SiteConfiguration

logger = logging.getLogger(__name__)

CACHE_DAYS = 30
FAILURE_RETRY_DAYS = 1
FETCH_TIMEOUT = 5

# logo.dev first when a key is configured - a dedicated logo API that
# returns real, consistently high-resolution brand marks (not a favicon),
# up to the 800px max its own API supports (see
# https://www.logo.dev/docs/logo-images/get) - webp for the best quality
# per byte. Then Clearbit (real brand logos too, but frequently
# unreachable/rate-limited without an account), then Google favicons as a
# near-100%-hit-rate last resort - it serves whatever native resolution a
# domain's favicon actually has (often just 32-48px for anything but the
# biggest brands, sometimes even less, observed as low as 16px at a high
# requested `sz`) regardless of what's requested, so it's requested large
# but never relied on for quality. All three request the largest size that
# doesn't get upscaled by the source itself; see
# myapp.avatar.normalize_logo_image for what smooths out whatever
# resolution actually comes back below that ceiling.
_MAX_LOGO_SIZE = 800
_LOGO_DEV_PREFIX = 'https://img.logo.dev/'

_NON_ALNUM_RE = re.compile(r'[^a-z0-9]')


def _logo_sources(config=None) -> list:
    """
    Ordered candidate URL templates for a merchant's logo, most-preferred
    first. Takes an optional already-loaded SiteConfiguration to avoid a
    redundant query when the caller already has one.
    """
    config = config or SiteConfiguration.load()
    sources = []
    if config.logo_dev_api_key:
        sources.append(
            f'https://img.logo.dev/{{domain}}?token={config.logo_dev_api_key}'
            f'&size={_MAX_LOGO_SIZE}&format=webp'
        )
    sources.append(f'https://logo.clearbit.com/{{domain}}?size={_MAX_LOGO_SIZE}')
    sources.append(f'https://www.google.com/s2/favicons?sz={_MAX_LOGO_SIZE}&domain={{domain}}')
    return sources


def merchant_logos_enabled() -> bool:
    return SiteConfiguration.load().merchant_logos_enabled


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


def remember_balance_check_url(issuer: str, url: str) -> None:
    """
    Upserts a merchant's gift-card balance-check link so future items from
    the same issuer can suggest it (see get_cached_balance_check_url).
    Deliberately last-write-wins: if a user enters a different link for
    the same merchant later, that becomes the new suggestion.
    """
    if not issuer or not url:
        return
    issuer = issuer.strip()
    profile = MerchantProfile.objects.filter(name__iexact=issuer).first()
    if profile is None:
        profile = MerchantProfile.objects.create(name=issuer, balance_check_url=url)
    elif profile.balance_check_url != url:
        profile.balance_check_url = url
        profile.save(update_fields=['balance_check_url'])


def get_cached_balance_check_url(name: str):
    """Read-only lookup, mirrors get_cached_logo. Returns '' if none is known."""
    profile = get_cached_logo(name)
    return profile.balance_check_url if profile else ''


def fetch_merchant_logo(name: str, domain_hint: str = None) -> MerchantProfile:
    """
    Looks up (or creates) the MerchantProfile for `name` and, on a cache
    miss or expiry, fetches a logo URL from _logo_sources(). Makes real HTTP
    requests — call this from a Celery task (see fetch_merchant_logo_task),
    never directly from a request/template render path.

    domain_hint, when given, is used as the domain instead of guessing one
    from `name` — e.g. an item's OCR-extracted logo_slug ("uber.com") is a
    far more reliable domain than guessing one from its issuer name
    ("Every Wish"), which is often just who resold/issued the card, not
    who it's actually branded for. If a *different* domain_hint arrives
    for an already-cached merchant (e.g. the first item for "Every Wish"
    had no logo_slug and cached a wrong guessed domain, and a later item
    for the same issuer was OCR-scanned and does have one), that overrides
    the normal freshness window and forces a refetch - a better domain
    hint arriving is a stronger signal than "we already looked recently".

    A fetch that found nothing (logo_url left blank) is retried much
    sooner than a successful one (FAILURE_RETRY_DAYS vs CACHE_DAYS) -
    otherwise a transient network hiccup on the very first save gets
    "stuck" showing no logo for a full month.

    Likewise, if a logo.dev key has just been configured (added in Site
    Settings) since a merchant was last cached, and that cached result
    isn't already from logo.dev, that also overrides the freshness
    window - otherwise a merchant that was cached once via the Clearbit/
    Google fallback (before a key existed) keeps showing that lower-
    quality result for the full CACHE_DAYS window even after a better
    source becomes available, which is confusing right after adding a
    key expecting it to take effect immediately.
    """
    name = name.strip()
    # Case-insensitive get-or-create: MerchantProfile.name is exact-unique,
    # but issuer casing varies between items ("Amazon" vs "amazon"), and we
    # don't want a duplicate cache row — and thus a duplicate fetch — per
    # casing variant of the same merchant.
    profile = MerchantProfile.objects.filter(name__iexact=name).first()
    if profile is None:
        profile = MerchantProfile.objects.create(name=name)

    config = SiteConfiguration.load()
    domain = domain_hint.strip().lower() if domain_hint else guess_domain(name)

    if profile.fetched_at:
        domain_hint_changed = bool(domain_hint) and profile.domain != domain
        logo_dev_now_available = bool(config.logo_dev_api_key) and not (profile.logo_url or '').startswith(_LOGO_DEV_PREFIX)
        cache_days = CACHE_DAYS if profile.logo_url else FAILURE_RETRY_DAYS
        cache_is_fresh = (timezone.now() - profile.fetched_at) < timedelta(days=cache_days)
        if cache_is_fresh and not domain_hint_changed and not logo_dev_now_available:
            return profile  # cache hit, still fresh
    for template in _logo_sources(config):
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
