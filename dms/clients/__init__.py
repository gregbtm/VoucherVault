from .base import BaseDMSClient, DMSDocument, BrowseResult
from .paperless import PaperlessNGXClient
from .docspell import DocspellClient
from .papermerge import PaperMergeClient


def get_client(provider) -> BaseDMSClient:
    """Factory — return the right client for a DMSProvider instance."""
    from dms.models import DMSProvider
    mapping = {
        DMSProvider.PROVIDER_PAPERLESS: PaperlessNGXClient,
        DMSProvider.PROVIDER_DOCSPELL: DocspellClient,
        DMSProvider.PROVIDER_PAPERMERGE: PaperMergeClient,
    }
    cls = mapping.get(provider.provider)
    if cls is None:
        raise ValueError(f'Unknown DMS provider type: {provider.provider}')
    return cls(provider)
