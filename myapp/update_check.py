import logging

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SiteConfiguration, UpdateCheckStatus, UpstreamSyncStatus

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


def _fetch_latest_release(repo: str) -> tuple[dict | None, str | None]:
    """
    Hits GitHub's public "latest release" endpoint for `repo`. Returns
    (data, None) on success or (None, error_message) on failure - shared
    by check_for_update() and check_upstream_version() below, which only
    differ in which repo they check and which model they persist to.
    """
    url = f'https://api.github.com/repos/{repo}/releases/latest'
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'Accept': 'application/vnd.github+json'})
        response.raise_for_status()
        return response.json(), None
    except requests.RequestException as exc:
        logger.warning('Release check request failed for %s: %s', repo, exc)
        return None, str(exc)
    except ValueError:
        logger.warning('Release check response was not valid JSON for %s', repo)
        return None, 'GitHub returned an unexpected response.'


def check_for_update() -> None:
    """
    Hits the public GitHub Releases API for SiteConfiguration.update_check_repo and
    persists the result to UpdateCheckStatus so the web process(es) can
    display it without making their own network call per request. No-ops
    (and leaves the previous result in place) if disabled or the request
    fails - a transient GitHub outage shouldn't flip the banner off.
    """
    config = SiteConfiguration.load()
    if not config.update_check_enabled:
        return

    status = UpdateCheckStatus.load()
    data, error = _fetch_latest_release(config.update_check_repo)
    if error is not None:
        # checked_at still advances so "last checked" reflects this attempt,
        # but latest_version/update_available are deliberately left as-is -
        # a transient GitHub outage shouldn't flip the banner off.
        status.last_check_error = error
        status.checked_at = timezone.now()
        status.save()
        return

    latest_version = data.get('tag_name', '') or ''
    release_url = data.get('html_url', '') or ''
    status.latest_version = latest_version
    status.latest_release_url = release_url
    status.checked_at = timezone.now()
    status.last_check_error = ''
    status.update_available = _is_newer(latest_version, settings.VERSION)
    status.save()


def check_upstream_version() -> None:
    """
    Checks l4rm4nd/VoucherVault's (upstream) latest release and persists
    the result to UpstreamSyncStatus, so the app can display "based on
    upstream vX.Y.Z" alongside its own version. Independent of
    UPDATE_CHECK_ENABLED (that flag is specifically for this fork's own
    releases) - runs unconditionally, since it's informational only and
    never drives an "update available" banner or any user-facing action.
    """
    status = UpstreamSyncStatus.load()
    data, error = _fetch_latest_release(status.upstream_repo)
    if error is not None:
        status.last_check_error = error
        status.checked_at = timezone.now()
        status.save()
        return

    status.latest_version = data.get('tag_name', '') or ''
    status.latest_release_url = data.get('html_url', '') or ''
    published_at = data.get('published_at')
    status.latest_release_published_at = parse_datetime(published_at) if published_at else None
    status.checked_at = timezone.now()
    status.last_check_error = ''
    status.save()
