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
    'notifications':      ('Notification rules & expiry alerts', 'NOTIFICATIONS_SETUP.md'),
    'telegram-discord-email': ('Telegram, Discord & Email notifications', 'TELEGRAM_DISCORD_EMAIL_SETUP.md'),
    'balance-tracking':   ('Balance & redemption tracking', 'BALANCE_TRACKING.md'),
    'pwa-offline':        ('PWA install & offline mode', 'PWA_OFFLINE.md'),
    'webhooks':           ('Outbound webhooks', 'WEBHOOKS_SETUP.md'),
    'api-access':         ('REST API access', 'API_ACCESS.md'),
    'wallets-tags':       ('Wallets & tags', 'WALLETS_AND_TAGS.md'),
    'oidc-setup':         ('OIDC / PocketID SSO setup', 'OIDC_SETUP.md'),
    'security-settings':  ('Security settings & hardening', 'SECURITY_SETTINGS.md'),
    'field-map-doc':      ('Field map & suggestion system', 'FIELD_MAP.md'),
}

# Categories for the Help Center index page.
# Each entry: (category_title, icon, [(slug, description)])
CATEGORIES = [
    ('Getting Started', 'bi-house', [
        ('wallets-tags',   'Organise items into wallets and apply tags for filtering'),
        ('balance-tracking', 'Log transactions, track remaining balance, and view spend analytics'),
        ('notifications',  'Set up expiry alerts via ntfy, webhook, web push, or Apprise'),
        ('telegram-discord-email', 'Send alerts directly to Telegram, a Discord channel, or email'),
        ('api-access',     'Generate an API token and use the REST API or MCP server'),
        ('pwa-offline',    'Install as a PWA, browse offline, and share items directly from Android'),
    ]),
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
        ('webhooks',       'Fire outbound webhooks on item events — wire into n8n, Zapier, or your own endpoint'),
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
    ('Security', 'bi-shield-lock', [
        ('oidc-setup',        'Set up PocketID or any OIDC provider for single sign-on'),
        ('security-settings', 'Login spike alerts, API token expiry, CSP, and system-check warnings'),
    ]),
    ('Reference', 'bi-layout-text-sidebar-reverse', [
        ('field-map-doc',     'Every form field, when it appears, and which ones have context-aware suggestion buttons'),
    ]),
]

# Interactive tools — these open as standalone pages, not as help-doc modals.
# Each entry: (url_name, icon, title, description)
TOOLS = [
    ('field_map', 'bi-layout-text-sidebar-reverse', 'Interactive Field Map',
     'Filter, search, and explore every item form field and the suggestion system — with the type visibility matrix'),
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
