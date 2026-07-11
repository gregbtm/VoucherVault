import os

import requests


class VoucherVaultApiError(Exception):
    """Raised when the VoucherVault REST API returns an error response."""

    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f'VoucherVault API returned {status_code}: {detail}')


class VoucherVaultClient:
    """
    Thin wrapper around the existing VoucherVault REST API
    (myproject/api/v1/). The MCP server never touches the Django ORM or
    duplicates business logic — every tool call becomes one HTTP request
    here, so the same permission checks, validation, and side effects
    (webhook events, QR/barcode generation, etc.) that already gate the
    web UI and the API gate MCP tool calls too.
    """

    def __init__(self, base_url: str | None = None, api_token: str | None = None):
        self.base_url = (base_url or os.environ.get('VOUCHERVAULT_BASE_URL', '')).rstrip('/')
        self.api_token = api_token or os.environ.get('VOUCHERVAULT_API_TOKEN')
        if not self.base_url:
            raise RuntimeError('VOUCHERVAULT_BASE_URL is not set.')
        if not self.api_token:
            raise RuntimeError('VOUCHERVAULT_API_TOKEN is not set.')

    def _headers(self) -> dict:
        return {'Authorization': f'Token {self.api_token}'}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f'{self.base_url}/api/v1/{path.lstrip("/")}'
        response = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise VoucherVaultApiError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def list_items(self, **params) -> dict:
        return self._request('GET', 'items/', params={k: v for k, v in params.items() if v is not None})

    def get_item(self, item_id: str) -> dict:
        return self._request('GET', f'items/{item_id}/')

    def create_item(self, payload: dict) -> dict:
        return self._request('POST', 'items/', json=payload)

    def update_item(self, item_id: str, payload: dict) -> dict:
        return self._request('PATCH', f'items/{item_id}/', json=payload)

    def redeem_item(self, item_id: str) -> dict:
        return self._request('POST', f'items/{item_id}/redeem/')

    def add_transaction(self, item_id: str, description: str, value: str) -> dict:
        return self._request('POST', f'items/{item_id}/transactions/', json={'description': description, 'value': value})

    def get_analytics_summary(self) -> dict:
        return self._request('GET', 'analytics/summary/')
