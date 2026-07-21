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
    'google-wallet':      ('Google Wallet setup', 'GOOGLE_WALLET_SETUP.md'),
    'apple-wallet':       ('Apple Wallet setup', 'APPLE_WALLET_SETUP.md'),
    'ocr':                ('Scan with AI (OCR) setup', 'OCR_SETUP.md'),
    'auto-deploy':        ('Update check & auto-redeploy', 'AUTO_DEPLOY.md'),
    'backup-restore':     ('Scheduled backups & restore', 'BACKUP_RESTORE.md'),
    'upstream-sync':      ('How upstream sync works', 'UPSTREAM_SYNC.md'),
    'n8n':                ('Connecting n8n', 'N8N_SETUP.md'),
    'mcp-server':         ('Wiring up an AI assistant (MCP)', 'MCP_SERVER_SETUP.md'),
    'DMS_SETUP':          ('Document Archive (DMS) setup', 'DMS_SETUP.md'),
    'rail-ticket':        ('Auto-importing UK rail eTickets', 'RAIL_TICKET_IMPORT_SETUP.md'),
    'synology':           ('Synology NAS integration', 'SYNOLOGY_NAS_SETUP.md'),
    'firefly':            ('Syncing spend to Firefly III', 'FIREFLY_III_SETUP.md'),
    'upgrade':            ('Upgrading from the upstream image', 'UPGRADE.md'),
}

# Categories for the Help Center index page.
# Each entry: (category_title, icon, [(slug, description)])
CATEGORIES = [
    ('Wallet & Sharing', 'bi-wallet2', [
        ('google-wallet',  'Export vouchers to Google Wallet on Android'),
        ('apple-wallet',   'Export vouchers to Apple Wallet on iOS & macOS'),
    ]),
    ('Scanning & OCR', 'bi-camera', [
        ('ocr',            'Enable AI-powered document scanning to extract voucher details automatically'),
    ]),
    ('Document Archive', 'bi-archive', [
        ('DMS_SETUP',      'Connect Paperless-ngx, Docspell, or PaperMerge to store and retrieve documents'),
    ]),
    ('Automation & Integrations', 'bi-gear-wide-connected', [
        ('n8n',            'Build no-code workflows with n8n — email import, Slack alerts, and more'),
        ('rail-ticket',    'Automatically import UK rail eTickets from your inbox via n8n'),
        ('mcp-server',     'Give Claude, Claude Code, or any MCP client direct access to your vault'),
        ('firefly',        'Post voucher spend automatically to your Firefly III ledger'),
    ]),
    ('Storage & Backup', 'bi-hdd', [
        ('backup-restore', 'Nightly automated backups with one-command restore'),
        ('synology',       'Mount NAS storage, run backups, and use Container Manager on Synology DSM 7'),
    ]),
    ('Maintenance & Ops', 'bi-tools', [
        ('auto-deploy',    'One-click redeploy triggered by a Portainer webhook — no manual restarts'),
        ('upgrade',        'Migrate from the upstream VoucherVault image to this fork'),
        ('upstream-sync',  'How changes from the upstream project are merged into this fork'),
    ]),
]


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
