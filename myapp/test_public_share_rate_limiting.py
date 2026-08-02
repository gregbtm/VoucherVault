from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from myapp.public_share import _client_ip, view_rate_limited


class ClientIpTrustTests(TestCase):
    """
    _client_ip must not trust a client-supplied X-Forwarded-For unless the
    deployment has explicitly said its reverse proxy sets it itself
    (settings.TRUST_X_FORWARDED_FOR) - otherwise any visitor can set a
    fresh value on every request and dodge the rate limiters below, which
    key on this IP.
    """

    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(TRUST_X_FORWARDED_FOR=False)
    def test_xff_ignored_by_default(self):
        request = self.factory.get('/s/whatever/', REMOTE_ADDR='10.0.0.5',
                                    HTTP_X_FORWARDED_FOR='1.2.3.4')
        self.assertEqual(_client_ip(request), '10.0.0.5')

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_xff_used_when_trusted(self):
        request = self.factory.get('/s/whatever/', REMOTE_ADDR='10.0.0.5',
                                    HTTP_X_FORWARDED_FOR='1.2.3.4, 10.0.0.1')
        self.assertEqual(_client_ip(request), '1.2.3.4')

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_falls_back_to_remote_addr_when_trusted_but_header_absent(self):
        request = self.factory.get('/s/whatever/', REMOTE_ADDR='10.0.0.5')
        self.assertEqual(_client_ip(request), '10.0.0.5')

    @override_settings(TRUST_X_FORWARDED_FOR=False)
    def test_falls_back_to_unknown_with_no_remote_addr_or_trust(self):
        request = self.factory.get('/s/whatever/')
        del request.META['REMOTE_ADDR']
        self.assertEqual(_client_ip(request), 'unknown')


class RateLimitBypassTests(TestCase):
    """
    With X-Forwarded-For untrusted (the default), spoofing a different
    value on every request must NOT reset the rate-limit bucket - it
    should key on REMOTE_ADDR regardless of what the client claims.
    """

    def setUp(self):
        self.factory = RequestFactory()
        cache.clear()

    @override_settings(TRUST_X_FORWARDED_FOR=False)
    def test_spoofed_xff_does_not_evade_view_rate_limit(self):
        for i in range(60):
            request = self.factory.get('/s/share-1/', REMOTE_ADDR='10.0.0.9',
                                        HTTP_X_FORWARDED_FOR=f'203.0.113.{i}')
            self.assertFalse(view_rate_limited(request, 'share-1'))

        request = self.factory.get('/s/share-1/', REMOTE_ADDR='10.0.0.9',
                                    HTTP_X_FORWARDED_FOR='203.0.113.250')
        self.assertTrue(view_rate_limited(request, 'share-1'))

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_distinct_trusted_xff_values_get_independent_buckets(self):
        for i in range(60):
            request = self.factory.get('/s/share-2/', REMOTE_ADDR='10.0.0.9',
                                        HTTP_X_FORWARDED_FOR='198.51.100.1')
            self.assertFalse(view_rate_limited(request, 'share-2'))

        # A genuinely different (trusted) client IP is unaffected by the
        # other client's usage of the same throttle key namespace.
        request = self.factory.get('/s/share-2/', REMOTE_ADDR='10.0.0.9',
                                    HTTP_X_FORWARDED_FOR='198.51.100.2')
        self.assertFalse(view_rate_limited(request, 'share-2'))
