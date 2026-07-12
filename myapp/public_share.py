"""
Helpers for the public, no-login-required item share link
(myapp.models.ItemPublicShare) - link-preview/crawler detection and a
lightweight rate limiter, for the view itself and for PIN-verification
attempts. Kept out of views.py since it's enough self-contained logic to
warrant its own module, matching the existing pattern (portainer.py,
update_check.py, merchant_logos.py).
"""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Case-insensitive substrings identifying known link-preview/unfurl bots -
# messaging and social apps fetch a shared URL purely to build a preview
# card (title/image) before a human ever opens it. These should get the
# og:* metadata and nothing else - never the redeem code, PIN, or balance
# - and shouldn't count as a real "view" of the link.
_LINK_PREVIEW_BOT_MARKERS = (
    'whatsapp', 'facebookexternalhit', 'twitterbot', 'slackbot',
    'telegrambot', 'discordbot', 'linkedinbot', 'skypeuripreview',
    'googlebot', 'bingbot', 'applebot', 'redditbot', 'pinterest',
    'vkshare', 'embedly', 'quora link preview', 'outbrain', 'iframely',
    'mail.ru', 'yandex',
)


def is_link_preview_bot(user_agent):
    if not user_agent:
        return False
    ua = user_agent.lower()
    return any(marker in ua for marker in _LINK_PREVIEW_BOT_MARKERS)


def _client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _rate_limited(key, limit, window_seconds):
    """
    True if `key` has been hit more than `limit` times within the last
    `window_seconds`. Fails open (never blocks) if the cache backend is
    unreachable - a Redis hiccup shouldn't take down a public page over a
    rate-limit check that's defense-in-depth, not the primary control.
    """
    try:
        added = cache.add(key, 1, timeout=window_seconds)
        count = 1 if added else cache.incr(key)
    except Exception:
        logger.warning('Rate limit check failed for key %s; failing open', key, exc_info=True)
        return False
    return count > limit


def view_rate_limited(request, share_id):
    """General throttle on viewing a given share link - generous, just basic hygiene."""
    return _rate_limited(f'share-view:{share_id}:{_client_ip(request)}', limit=60, window_seconds=60)


def pin_attempt_rate_limited(request, share_id):
    """
    Tighter throttle specifically on PIN-verification attempts, since a
    short numeric PIN is only meaningful protection if guessing it is
    slow. 10 attempts / 10 minutes caps a brute force at several days
    minimum for a full 4-digit keyspace sweep, while still tolerating a
    legitimate recipient mistyping a few times.
    """
    return _rate_limited(f'share-pin:{share_id}:{_client_ip(request)}', limit=10, window_seconds=600)
