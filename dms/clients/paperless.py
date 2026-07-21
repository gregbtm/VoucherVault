"""
Paperless-ngx client.

API docs: https://docs.paperless-ngx.com/api/
Auth: Token in Authorization header.
"""
import logging
from io import BytesIO

import requests

from .base import BaseDMSClient, BrowseResult, DMSDocument

logger = logging.getLogger(__name__)

TIMEOUT = 15


class PaperlessNGXClient(BaseDMSClient):

    def _session(self):
        s = requests.Session()
        s.headers['Authorization'] = f'Token {self.provider.api_token}'
        return s

    def _api(self, path):
        return f'{self.base_url}/api/{path.lstrip("/")}'

    def test_connection(self):
        try:
            resp = self._session().get(self._api('documents/'), params={'page_size': 1}, timeout=TIMEOUT)
            if resp.status_code == 401:
                return {'ok': False, 'message': 'Authentication failed — check your API token.'}
            resp.raise_for_status()
            data = resp.json()
            count = data.get('count', 0)
            # Try to get version
            ver_resp = self._session().get(self._api('remote_version/'), timeout=TIMEOUT)
            version = ''
            if ver_resp.ok:
                version = ver_resp.json().get('version', '')
            return {'ok': True, 'message': f'Connected — {count} documents', 'version': version}
        except requests.exceptions.ConnectionError:
            return {'ok': False, 'message': 'Could not reach the server. Check the URL and that Paperless-ngx is running.'}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}

    def get_server_info(self):
        try:
            resp = self._session().get(self._api('remote_version/'), timeout=TIMEOUT)
            return resp.json() if resp.ok else {}
        except Exception:
            return {}

    def browse(self, query='', page=1, page_size=20, tag='', correspondent=''):
        params = {'page': page, 'page_size': page_size}
        if query:
            params['query'] = query
        if tag:
            # look up tag id by name
            tag_id = self._get_tag_id(tag)
            if tag_id:
                params['tags__id__all'] = tag_id
        if correspondent:
            corr_id = self._get_correspondent_id(correspondent)
            if corr_id:
                params['correspondent__id'] = corr_id

        try:
            resp = self._session().get(self._api('documents/'), params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            docs = [self._to_dms_doc(d) for d in data.get('results', [])]
            total = data.get('count', 0)
            return BrowseResult(
                documents=docs,
                total_count=total,
                page=page,
                page_size=page_size,
                has_next=data.get('next') is not None,
                has_prev=data.get('previous') is not None,
            )
        except Exception as exc:
            logger.error('Paperless browse error: %s', exc)
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
        tag_ids = []
        if tags:
            for t in tags:
                tid = self._get_or_create_tag(t)
                if tid:
                    tag_ids.append(str(tid))

        corr_id = None
        if correspondent:
            corr_id = self._get_or_create_correspondent(correspondent)

        data = {}
        if title:
            data['title'] = title
        if tag_ids:
            data['tags'] = tag_ids
        if corr_id:
            data['correspondent'] = corr_id

        files = {'document': (filename, BytesIO(content))}
        resp = self._session().post(
            self._api('documents/post_document/'),
            files=files,
            data=data,
            timeout=120,
        )
        resp.raise_for_status()
        # Paperless returns the task ID immediately; the actual doc ID comes later via task status
        task_id = resp.json() if isinstance(resp.json(), str) else str(resp.json())
        return task_id

    def list_tags(self):
        return self._paginate_list(self._api('tags/'))

    def list_correspondents(self):
        return self._paginate_list(self._api('correspondents/'))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_dms_doc(self, d):
        return DMSDocument(
            id=str(d.get('id', '')),
            title=d.get('title', ''),
            created=d.get('created', ''),
            modified=d.get('modified', ''),
            content=d.get('content', ''),
            tags=[str(t) for t in d.get('tags', [])],
            correspondent=str(d.get('correspondent', '') or ''),
            download_url=f'{self.base_url}/api/documents/{d.get("id")}/download/',
            thumbnail_url=f'{self.base_url}/api/documents/{d.get("id")}/thumb/',
            mime_type=d.get('mime_type', ''),
            original_filename=d.get('original_file_name', ''),
            file_size=d.get('original_size', 0) or 0,
        )

    def _paginate_list(self, url):
        items = []
        while url:
            resp = self._session().get(url, params={'page_size': 100}, timeout=TIMEOUT)
            if not resp.ok:
                break
            data = resp.json()
            items.extend(data.get('results', []))
            url = data.get('next')
        return [{'id': i.get('id'), 'name': i.get('name', i.get('slug', ''))} for i in items]

    def _get_tag_id(self, name):
        resp = self._session().get(self._api('tags/'), params={'name': name}, timeout=TIMEOUT)
        if resp.ok:
            results = resp.json().get('results', [])
            if results:
                return results[0]['id']
        return None

    def _get_correspondent_id(self, name):
        resp = self._session().get(self._api('correspondents/'), params={'name': name}, timeout=TIMEOUT)
        if resp.ok:
            results = resp.json().get('results', [])
            if results:
                return results[0]['id']
        return None

    def _get_or_create_tag(self, name):
        existing = self._get_tag_id(name)
        if existing:
            return existing
        resp = self._session().post(self._api('tags/'), json={'name': name}, timeout=TIMEOUT)
        if resp.ok:
            return resp.json().get('id')
        return None

    def _get_or_create_correspondent(self, name):
        existing = self._get_correspondent_id(name)
        if existing:
            return existing
        resp = self._session().post(self._api('correspondents/'), json={'name': name}, timeout=TIMEOUT)
        if resp.ok:
            return resp.json().get('id')
        return None
