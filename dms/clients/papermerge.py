"""
PaperMerge 3.x client.

API docs: https://docs.papermerge.io/
Auth: token via POST /api/auth/token/ with username/password.
Documents: /api/documents/ (REST).
"""
import logging
from io import BytesIO

import requests

from .base import BaseDMSClient, BrowseResult, DMSDocument

logger = logging.getLogger(__name__)

TIMEOUT = 15


class PaperMergeClient(BaseDMSClient):

    def __init__(self, provider):
        super().__init__(provider)
        self._token = provider.api_token or ''

    def _get_token(self):
        """Obtain token if not already set."""
        if self._token:
            return self._token
        resp = requests.post(
            f'{self.base_url}/api/auth/token/',
            json={'username': self.provider.username, 'password': self.provider.password},
            timeout=TIMEOUT,
        )
        if resp.ok:
            self._token = resp.json().get('token', resp.json().get('access', ''))
        return self._token

    def _session(self):
        s = requests.Session()
        token = self._get_token()
        if token:
            s.headers['Authorization'] = f'Token {token}'
        return s

    def _api(self, path):
        return f'{self.base_url}/api/{path.lstrip("/")}'

    def test_connection(self):
        try:
            token = self._get_token()
            if not token:
                return {'ok': False, 'message': 'Authentication failed — check API token / username+password.'}

            resp = self._session().get(self._api('documents/'), params={'page_size': 1}, timeout=TIMEOUT)
            if resp.status_code == 401:
                return {'ok': False, 'message': 'Token rejected. Check credentials.'}
            if not resp.ok:
                return {'ok': False, 'message': f'Server returned {resp.status_code}'}

            count = resp.json().get('count', 0)
            return {'ok': True, 'message': f'Connected — {count} documents', 'version': ''}
        except requests.exceptions.ConnectionError:
            return {'ok': False, 'message': 'Could not reach the server. Check the URL and that PaperMerge is running.'}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def get_server_info(self):
        return {}

    def browse(self, query='', page=1, page_size=20, tag='', correspondent=''):
        params = {'page': page, 'page_size': page_size}
        if query:
            params['title'] = query

        try:
            resp = self._session().get(self._api('documents/'), params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            docs = [self._to_dms_doc(d) for d in data.get('results', [])]
            total = data.get('count', len(docs))
            return BrowseResult(
                documents=docs,
                total_count=total,
                page=page,
                page_size=page_size,
                has_next=data.get('next') is not None,
                has_prev=data.get('previous') is not None,
            )
        except Exception as exc:
            logger.error('PaperMerge browse error: %s', exc)
            return BrowseResult(documents=[], total_count=0, page=1, page_size=page_size, has_next=False, has_prev=False)

    def get_document(self, doc_id):
        resp = self._session().get(self._api(f'documents/{doc_id}/'), timeout=TIMEOUT)
        resp.raise_for_status()
        return self._to_dms_doc(resp.json())

    def download_document(self, doc_id):
        resp = self._session().get(self._api(f'documents/{doc_id}/download/'), timeout=60)
        resp.raise_for_status()
        return resp.content

    def upload_document(self, filename, content, title='', tags=None, correspondent=''):
        data = {}
        if title:
            data['title'] = title

        files = {'file': (filename, BytesIO(content))}
        resp = self._session().post(self._api('documents/'), files=files, data=data, timeout=120)
        resp.raise_for_status()
        return str(resp.json().get('id', ''))

    def list_tags(self):
        try:
            resp = self._session().get(self._api('tags/'), timeout=TIMEOUT)
            if resp.ok:
                return [{'id': t.get('id'), 'name': t.get('name')} for t in resp.json().get('results', [])]
        except Exception:
            pass
        return []

    def list_correspondents(self):
        # PaperMerge doesn't have a correspondents concept in v3; return empty
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_dms_doc(self, d):
        return DMSDocument(
            id=str(d.get('id', '')),
            title=d.get('title', d.get('file_name', '')),
            created=d.get('created_at', ''),
            modified=d.get('updated_at', ''),
            content='',
            tags=[t.get('name', '') if isinstance(t, dict) else str(t) for t in d.get('tags', [])],
            correspondent='',
            download_url=f'{self.base_url}/api/documents/{d.get("id")}/download/',
            thumbnail_url='',
            mime_type='',
            original_filename=d.get('file_name', ''),
            file_size=d.get('size', 0) or 0,
        )
