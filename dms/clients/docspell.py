"""
Docspell client.

API docs: https://docspell.org/docs/api/
Auth: JWT from POST /api/v1/open/auth/login, header X-Docspell-Auth: <token>.
Upload: POST /api/v1/open/upload/item/{sourceId} (multipart).
"""
import logging
from io import BytesIO

import requests

from .base import BaseDMSClient, BrowseResult, DMSDocument

logger = logging.getLogger(__name__)

TIMEOUT = 15


class DocspellClient(BaseDMSClient):

    def __init__(self, provider):
        super().__init__(provider)
        self._token = None

    def _login(self):
        """Obtain a JWT session token."""
        resp = requests.post(
            f'{self.base_url}/api/v1/open/auth/login',
            json={
                'account': f'{self.provider.docspell_collective}/{self.provider.username}',
                'password': self.provider.password,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get('success'):
            self._token = resp.json().get('token', '')
            return True
        return False

    def _session(self):
        s = requests.Session()
        if not self._token:
            self._login()
        s.headers['X-Docspell-Auth'] = self._token or ''
        return s

    def _api(self, path):
        return f'{self.base_url}/api/v1/{path.lstrip("/")}'

    def test_connection(self):
        try:
            if not self._login():
                return {'ok': False, 'message': 'Authentication failed — check collective, username, and password.'}

            resp = self._session().get(self._api('sec/item/search'), params={'limit': 1}, timeout=TIMEOUT)
            if not resp.ok:
                return {'ok': False, 'message': f'Auth OK but search failed: {resp.status_code}'}

            count = resp.json().get('groups', [{}])[0].get('count', 0) if resp.json().get('groups') else 0
            # Version
            ver_resp = requests.get(f'{self.base_url}/api/v1/open/info', timeout=TIMEOUT)
            version = ver_resp.json().get('version', '') if ver_resp.ok else ''
            return {'ok': True, 'message': f'Connected to collective {self.provider.docspell_collective}', 'version': version}
        except requests.exceptions.ConnectionError:
            return {'ok': False, 'message': 'Could not reach the server. Check the URL and that Docspell is running.'}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def get_server_info(self):
        try:
            resp = requests.get(f'{self.base_url}/api/v1/open/info', timeout=TIMEOUT)
            return resp.json() if resp.ok else {}
        except Exception:
            return {}

    def browse(self, query='', page=1, page_size=20, tag='', correspondent=''):
        params = {
            'limit': page_size,
            'offset': (page - 1) * page_size,
        }
        if query:
            params['query'] = query
        if tag:
            params['query'] = (params.get('query', '') + f' tag:{tag}').strip()
        if correspondent:
            params['query'] = (params.get('query', '') + f' source:{correspondent}').strip()

        try:
            resp = self._session().get(self._api('sec/item/search'), params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            items = []
            for group in data.get('groups', []):
                items.extend(group.get('items', []))
            docs = [self._to_dms_doc(d) for d in items]
            total = data.get('total', len(docs))
            return BrowseResult(
                documents=docs,
                total_count=total,
                page=page,
                page_size=page_size,
                has_next=(page * page_size) < total,
                has_prev=page > 1,
            )
        except Exception as exc:
            logger.error('Docspell browse error: %s', exc)
            return BrowseResult(documents=[], total_count=0, page=1, page_size=page_size, has_next=False, has_prev=False)

    def get_document(self, doc_id):
        resp = self._session().get(self._api(f'sec/item/{doc_id}'), timeout=TIMEOUT)
        resp.raise_for_status()
        return self._to_dms_doc(resp.json())

    def download_document(self, doc_id):
        # First get the item to find an attachment ID
        resp = self._session().get(self._api(f'sec/item/{doc_id}'), timeout=TIMEOUT)
        resp.raise_for_status()
        attachments = resp.json().get('attachments', [])
        if not attachments:
            raise ValueError(f'No attachments found for Docspell item {doc_id}')
        att_id = attachments[0]['id']
        dl_resp = self._session().get(self._api(f'sec/attachment/{att_id}/file'), timeout=60)
        dl_resp.raise_for_status()
        return dl_resp.content

    def upload_document(self, filename, content, title='', tags=None, correspondent=''):
        source_id = self.provider.docspell_source_id
        if not source_id:
            raise ValueError('docspell_source_id is required for uploading to Docspell')

        metadata = {}
        if tags:
            metadata['tags'] = {'items': [{'id': t} for t in tags]}

        files = {
            'file': (filename, BytesIO(content)),
        }
        if metadata:
            import json
            files['meta'] = ('meta.json', BytesIO(json.dumps(metadata).encode()), 'application/json')

        resp = requests.post(
            f'{self.base_url}/api/v1/open/upload/item/{source_id}',
            files=files,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get('id', source_id)

    def list_tags(self):
        try:
            resp = self._session().get(self._api('sec/tag'), timeout=TIMEOUT)
            if resp.ok:
                return [{'id': t.get('id'), 'name': t.get('name')} for t in resp.json().get('items', [])]
        except Exception:
            pass
        return []

    def list_correspondents(self):
        # Docspell uses "correspondents" (persons/orgs)
        try:
            resp = self._session().get(self._api('sec/person/all'), timeout=TIMEOUT)
            if resp.ok:
                return [{'id': p.get('id'), 'name': p.get('name')} for p in resp.json().get('items', [])]
        except Exception:
            pass
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_dms_doc(self, d):
        attachments = d.get('attachments', [])
        first_att = attachments[0] if attachments else {}
        return DMSDocument(
            id=str(d.get('id', '')),
            title=d.get('name', d.get('id', '')),
            created=d.get('date', d.get('created', '')),
            modified=d.get('updated', ''),
            content=d.get('notes', ''),
            tags=[t.get('name', '') for t in d.get('tags', [])],
            correspondent=d.get('correspondent', {}).get('name', '') if d.get('correspondent') else '',
            download_url=f'{self.base_url}/api/v1/sec/attachment/{first_att.get("id")}/file' if first_att else '',
            thumbnail_url=f'{self.base_url}/api/v1/sec/item/{d.get("id")}/preview' if d.get('id') else '',
            mime_type=first_att.get('fileType', ''),
            original_filename=first_att.get('name', ''),
            file_size=first_att.get('fileSize', 0) or 0,
        )
