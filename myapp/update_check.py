import logging

import requests
from django.conf import settings
from django.utils import timezone

from .models import UpdateCheckStatus

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


def _parse_version(value: str) -> tuple:
    """
    Best-effort numeric parse of a version string for ordering comparison,
    e.g. 'v1.2.0' -> (1, 2, 0). Non-numeric segments become 0 rather than
    raising, since GitHub tag names aren't guaranteed to be strict semver.
    """
    value = value.strip().lstrip('vV')
    parts = []
    for segment in value.split('.'):
        digits = ''.join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    if not latest or not current or current == 'unknown':
        return False
    return _parse_version(latest) > _parse_version(current)


def check_for_update() -> None:
    """
    Hits the public GitHub Releases API for settings.UPDATE_CHECK_REPO and
    persists the result to UpdateCheckStatus so the web process(es) can
    display it without making their own network call per request. No-ops
    (and leaves the previous result in place) if disabled or the request
    fails - a transient GitHub outage shouldn't flip the banner off.
    """
    if not settings.UPDATE_CHECK_ENABLED:
        return

    url = f'https://api.github.com/repos/{settings.UPDATE_CHECK_REPO}/releases/latest'
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('Update check request failed: %s', exc)
        return

    try:
        data = response.json()
        latest_version = data.get('tag_name', '') or ''
        release_url = data.get('html_url', '') or ''
    except ValueError:
        logger.warning('Update check response was not valid JSON')
        return

    status = UpdateCheckStatus.load()
    status.latest_version = latest_version
    status.latest_release_url = release_url
    status.checked_at = timezone.now()
    status.update_available = _is_newer(latest_version, settings.VERSION)
    status.save()
