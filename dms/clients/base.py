"""
Base DMS client interface and shared data types.
"""
from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class DMSDocument:
    """Normalised representation of a document from any DMS."""
    id: str
    title: str
    created: Optional[str] = None
    modified: Optional[str] = None
    content: str = ''
    tags: list = field(default_factory=list)
    correspondent: str = ''
    download_url: str = ''
    thumbnail_url: str = ''
    mime_type: str = ''
    original_filename: str = ''
    file_size: int = 0

    def as_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'created': self.created,
            'modified': self.modified,
            'content': self.content,
            'tags': self.tags,
            'correspondent': self.correspondent,
            'download_url': self.download_url,
            'thumbnail_url': self.thumbnail_url,
            'mime_type': self.mime_type,
            'original_filename': self.original_filename,
            'file_size': self.file_size,
        }


@dataclass
class BrowseResult:
    """Paginated document listing result."""
    documents: list
    total_count: int
    page: int
    page_size: int
    has_next: bool
    has_prev: bool


class BaseDMSClient:
    """
    Abstract DMS client. All provider-specific clients must implement these methods.
    """

    def __init__(self, provider):
        self.provider = provider
        self.base_url = provider.base_url.rstrip('/')

    def test_connection(self) -> dict:
        """
        Test connectivity and auth.
        Returns {'ok': True, 'version': '...', 'message': '...'} on success
        or {'ok': False, 'message': '...'} on failure.
        """
        raise NotImplementedError

    def browse(self, query: str = '', page: int = 1, page_size: int = 20, tag: str = '', correspondent: str = '') -> BrowseResult:
        """Return a paginated list of DMSDocument objects."""
        raise NotImplementedError

    def get_document(self, doc_id: str) -> DMSDocument:
        """Fetch a single document by its DMS ID."""
        raise NotImplementedError

    def download_document(self, doc_id: str) -> bytes:
        """Download and return the raw file bytes for a document."""
        raise NotImplementedError

    def upload_document(self, filename: str, content: bytes, title: str = '', tags: list = None, correspondent: str = '') -> str:
        """
        Upload a document to the DMS.
        Returns the DMS-assigned document ID string.
        """
        raise NotImplementedError

    def list_tags(self) -> list:
        """Return a list of tag dicts: [{'id': ..., 'name': ...}]"""
        raise NotImplementedError

    def list_correspondents(self) -> list:
        """Return a list of correspondent dicts: [{'id': ..., 'name': ...}]"""
        raise NotImplementedError

    def get_server_info(self) -> dict:
        """Return server version / capability info as a dict."""
        raise NotImplementedError
