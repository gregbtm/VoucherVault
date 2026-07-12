import logging

import requests

from .models import SiteConfiguration

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


class PortainerRedeployError(Exception):
    pass


def trigger_redeploy() -> None:
    """
    POSTs to Portainer's per-stack webhook URL (SiteConfiguration's
    portainer_webhook_url), telling Portainer to pull the latest git
    changes and rebuild/redeploy the stack. Called server-side from within
    the same Docker network Portainer and this container already share, so
    it works whether or not Portainer's own UI/API is reachable from
    outside the host.
    """
    webhook_url = SiteConfiguration.load().portainer_webhook_url
    if not webhook_url:
        raise PortainerRedeployError('PORTAINER_WEBHOOK_URL is not configured.')

    try:
        response = requests.post(webhook_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error('Portainer redeploy webhook call failed: %s', exc)
        raise PortainerRedeployError(str(exc)) from exc
