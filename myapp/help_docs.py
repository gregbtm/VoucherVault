"""
Renders the setup guides under docs/*.md in-app, for the "?" help buttons
next to the relevant Site Settings sections - kept as an allowlisted slug
-> filename map (never a raw path from the URL) so this can't be turned
into an arbitrary-file-read, and rendered locally rather than linking out
to GitHub so the guide is available even on a fully offline deployment.
"""
import logging

import markdown
from django.conf import settings

logger = logging.getLogger(__name__)

DOCS = {
    'google-wallet': ('Google Wallet setup', 'GOOGLE_WALLET_SETUP.md'),
    'apple-wallet': ('Apple Wallet setup', 'APPLE_WALLET_SETUP.md'),
    'ocr': ('Scan with AI (OCR) setup', 'OCR_SETUP.md'),
    'auto-deploy': ('Update check & Portainer redeploy', 'AUTO_DEPLOY.md'),
    'backup-restore': ('Scheduled backups & restore', 'BACKUP_RESTORE.md'),
}


def render_doc(slug):
    """Returns (title, html) for a known doc slug, or None if the slug/file is missing."""
    entry = DOCS.get(slug)
    if not entry:
        return None
    title, filename = entry
    path = settings.BASE_DIR / 'docs' / filename
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        logger.warning('Help doc %s not found on disk at %s', slug, path)
        return None
    html = markdown.markdown(text, extensions=['fenced_code', 'tables', 'toc'])
    return title, html
