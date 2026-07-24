"""
Light client for the PocketID admin REST API.

Covers the subset of endpoints needed for OIDC invite provisioning:
  - User creation (POST /api/users)
  - One-time access token generation (POST /api/users/:id/one-time-access-token)
  - Connectivity ping (GET /api/users?limit=1)

All calls require an admin API key generated in the PocketID interface.
"""
import requests


class PocketIDError(Exception):
    pass


class PocketIDClient:
    def __init__(self, base_url: str, api_key: str):
        self._base = base_url.rstrip('/')
        self._headers = {
            'X-API-KEY': api_key,
            'Accept': 'application/json',
        }

    def _url(self, path: str) -> str:
        return f"{self._base}/{path.lstrip('/')}"

    def ping(self) -> tuple[bool, str]:
        """Return (ok, message). Safe to call without catching exceptions."""
        try:
            r = requests.get(
                self._url('/api/users'),
                headers=self._headers,
                params={'limit': 1},
                timeout=5,
            )
            if r.status_code == 200:
                return True, 'Connected'
            if r.status_code == 401:
                return False, 'Invalid API key (401)'
            return False, f'HTTP {r.status_code}'
        except requests.Timeout:
            return False, 'Connection timed out'
        except requests.ConnectionError as exc:
            return False, f'Cannot reach PocketID: {exc}'

    def create_user(
        self,
        username: str,
        email: str = '',
        first_name: str = '',
        last_name: str = '',
    ) -> dict:
        """Create a PocketID user. Returns the created user dict (includes 'id')."""
        payload: dict = {
            'username': username,
            'firstName': first_name,
            'lastName': last_name,
            'emailVerified': bool(email),
        }
        if email:
            payload['email'] = email
        try:
            r = requests.post(
                self._url('/api/users'),
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            body = ''
            try:
                body = exc.response.json().get('error') or exc.response.text[:200]
            except Exception:
                pass
            raise PocketIDError(f'create_user failed ({exc.response.status_code}): {body}') from exc
        except requests.RequestException as exc:
            raise PocketIDError(f'create_user network error: {exc}') from exc

    def find_user_by_email(self, email: str) -> dict | None:
        """Search PocketID for a user with exactly this email. Returns user dict or None."""
        try:
            r = requests.get(
                self._url('/api/users'),
                headers=self._headers,
                params={'search': email, 'limit': 10},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                users = data
            else:
                users = data.get('users') or data.get('data') or []
            for user in users:
                if (user.get('email') or '').lower() == email.lower():
                    return user
            return None
        except requests.HTTPError as exc:
            raise PocketIDError(f'find_user_by_email failed ({exc.response.status_code})') from exc
        except requests.RequestException as exc:
            raise PocketIDError(f'find_user_by_email network error: {exc}') from exc

    def probe_ota(self) -> tuple[bool, str]:
        """Check whether the one-time-access-token endpoint exists in this PocketID version."""
        dummy_id = '00000000-0000-0000-0000-000000000000'
        try:
            r = requests.post(
                self._url(f'/api/users/{dummy_id}/one-time-access-token'),
                json={'ttl': '1h'},
                headers=self._headers,
                timeout=5,
            )
            if 'application/json' in r.headers.get('content-type', ''):
                return True, 'OTA endpoint available'
            return False, f'OTA endpoint not found (HTTP {r.status_code})'
        except requests.Timeout:
            return False, 'Connection timed out'
        except requests.ConnectionError as exc:
            return False, f'Cannot reach PocketID: {exc}'

    def get_ota_token(self, user_id: str, ttl: str = '72h') -> str:
        """
        Request a one-time access token for the given user and return the
        raw token string.  Handles several possible response shapes since
        PocketID's API shape evolves between releases.

        ttl must be a Go duration string (e.g. '72h', '24h').  PocketID's
        handler uses ShouldBindJSON, so a JSON body is required even though
        the ttl field itself is optional — sending no body causes a 500.
        """
        try:
            r = requests.post(
                self._url(f'/api/users/{user_id}/one-time-access-token'),
                json={'ttl': ttl},
                headers=self._headers,
                timeout=10,
            )
            r.raise_for_status()
        except requests.HTTPError as exc:
            raise PocketIDError(
                f'get_ota_token failed ({exc.response.status_code})'
            ) from exc
        except requests.RequestException as exc:
            raise PocketIDError(f'get_ota_token network error: {exc}') from exc

        data = r.json()
        # Handle multiple possible response shapes
        for key in ('token', 'oneTimeAccessToken', 'accessToken', 'access_token'):
            val = data.get(key)
            if val is None:
                continue
            if isinstance(val, dict):
                return str(val.get('token', ''))
            return str(val)
        raise PocketIDError(f'Unexpected OTA response shape: {list(data.keys())}')
