from rest_framework.throttling import UserRateThrottle, AnonRateThrottle


class WriteRateThrottle(UserRateThrottle):
    """
    Rate-limits only unsafe methods (POST/PUT/PATCH/DELETE); GET/HEAD/OPTIONS
    are never throttled by this class. The full CRUD API has no rate
    limiting at all otherwise, so a misbehaving script or leaked token
    could hammer writes without any backpressure - reads stay unlimited
    since they're comparatively cheap and throttling them would just
    break normal browsing/polling use.
    """
    scope = 'write'

    def allow_request(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        return super().allow_request(request, view)


class AnonWriteRateThrottle(AnonRateThrottle):
    """Rate-limit anonymous write requests (form submissions, API access without auth)."""
    scope = 'anon_write'

    def allow_request(self, request, view):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        return super().allow_request(request, view)


class AuthenticatedReadThrottle(UserRateThrottle):
    """Optional: rate-limit high-volume read operations to prevent scraping."""
    scope = 'auth_read'

    def allow_request(self, request, view):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return True
        if not request.user or not request.user.is_authenticated:
            return True
        return super().allow_request(request, view)
