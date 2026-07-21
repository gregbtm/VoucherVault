"""
WebDAV storage backend for Django.

Tested against Synology WebDAV Server (DSM 7.x), Nextcloud, and plain
Apache/Nginx WebDAV. Uses `requests` (already in requirements.txt) — no
extra dependencies.

Activated by setting USE_WEBDAV_STORAGE=true and the WEBDAV_* env vars.
See docs/SYNOLOGY_NAS_SETUP.md for the full walkthrough.
"""

import os
from io import BytesIO
from urllib.parse import quote, urljoin

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage


class WebDAVStorage(Storage):
    """
    Django storage backend that talks to any WebDAV server.

    Required settings (set via env vars, not directly):
        WEBDAV_URL          - Base URL of the WebDAV collection, e.g.
                              https://nas.example.com:5006/vouchervault
        WEBDAV_USERNAME     - Username for HTTP Basic auth
        WEBDAV_PASSWORD     - Password for HTTP Basic auth

    Optional settings:
        WEBDAV_PUBLIC_URL   - Publicly accessible base URL for file downloads
                              (defaults to WEBDAV_URL). Set this if the NAS
                              is behind a reverse proxy with a friendlier URL.
        WEBDAV_VERIFY_SSL   - Whether to verify the server's TLS certificate
                              (default True). Set to False for self-signed certs
                              on a local NAS — but prefer adding the cert to
                              the trust store instead.
    """

    def __init__(self):
        self._base = settings.WEBDAV_URL.rstrip('/')
        self._public = getattr(settings, 'WEBDAV_PUBLIC_URL', self._base).rstrip('/')
        self._session = requests.Session()
        self._session.auth = (settings.WEBDAV_USERNAME, settings.WEBDAV_PASSWORD)
        self._session.verify = getattr(settings, 'WEBDAV_VERIFY_SSL', True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remote_url(self, name):
        """Build the full WebDAV URL for a storage-relative path."""
        # quote preserves slashes; safe='/' keeps directory separators intact
        return f"{self._base}/{quote(name.lstrip('/'), safe='/')}"

    def _ensure_parent(self, name):
        """MKCOL every segment of the parent path that doesn't exist yet."""
        parent = os.path.dirname(name.lstrip('/'))
        if not parent:
            return
        parts = parent.split('/')
        for i in range(1, len(parts) + 1):
            segment = '/'.join(parts[:i])
            url = f"{self._base}/{quote(segment, safe='/')}"
            resp = self._session.request('MKCOL', url)
            # 201 = created, 405 = already exists (Method Not Allowed on
            # existing collections per RFC 4918 §9.3), 301/409 = also fine
            if resp.status_code not in (201, 301, 405, 409):
                resp.raise_for_status()

    # ------------------------------------------------------------------
    # Storage API
    # ------------------------------------------------------------------

    def _open(self, name, mode='rb'):
        resp = self._session.get(self._remote_url(name))
        resp.raise_for_status()
        return ContentFile(resp.content, name=name)

    def _save(self, name, content):
        self._ensure_parent(name)
        data = content.read() if hasattr(content, 'read') else content
        resp = self._session.put(self._remote_url(name), data=data)
        resp.raise_for_status()
        return name

    def exists(self, name):
        resp = self._session.head(self._remote_url(name))
        return resp.status_code == 200

    def url(self, name):
        return f"{self._public}/{quote(name.lstrip('/'), safe='/')}"

    def delete(self, name):
        resp = self._session.delete(self._remote_url(name))
        # 204 = deleted, 404 = already gone — both are fine
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    def size(self, name):
        resp = self._session.head(self._remote_url(name))
        resp.raise_for_status()
        return int(resp.headers.get('Content-Length', 0))

    def listdir(self, path):
        """
        Return (dirs, files) for the given path via a WebDAV PROPFIND.
        Django's FileSystemStorage contract: dirs is a list of directory
        names, files is a list of file names.  Both are relative to path.
        """
        url = self._remote_url(path) if path else self._base
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<propfind xmlns="DAV:">'
            '<prop><resourcetype/><displayname/></prop>'
            '</propfind>'
        )
        resp = self._session.request(
            'PROPFIND', url,
            data=body,
            headers={'Depth': '1', 'Content-Type': 'application/xml'},
        )
        resp.raise_for_status()

        # Minimal XML parse without lxml/minidom for the common case
        dirs, files = [], []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.content)
        ns = {'d': 'DAV:'}
        responses = root.findall('d:response', ns)
        base_href = url.rstrip('/') + '/'
        for r in responses:
            href = (r.find('d:href', ns) or {}).text or ''
            if href.rstrip('/') == url.rstrip('/'):
                continue  # skip the collection itself
            name_part = href.rstrip('/').rsplit('/', 1)[-1]
            name_part = requests.utils.unquote(name_part)
            col = r.find('.//d:collection', ns)
            if col is not None:
                dirs.append(name_part)
            else:
                files.append(name_part)
        return dirs, files
