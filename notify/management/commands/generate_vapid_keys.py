import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.management.base import BaseCommand
from py_vapid import Vapid02


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class Command(BaseCommand):
    help = 'Generate a VAPID keypair for Web Push notifications and print them as env vars.'

    def handle(self, *args, **options):
        vapid = Vapid02()
        vapid.generate_keys()

        private_value = vapid.private_key.private_numbers().private_value
        private_key = _b64url(private_value.to_bytes(32, 'big'))
        public_key = _b64url(vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))

        self.stdout.write(self.style.SUCCESS('Add these to your .env, then restart the container:\n'))
        self.stdout.write(f'WEBPUSH_VAPID_PUBLIC_KEY={public_key}')
        self.stdout.write(f'WEBPUSH_VAPID_PRIVATE_KEY={private_key}')
        self.stdout.write('\nOptionally also set WEBPUSH_VAPID_CLAIMS_EMAIL (defaults to mailto:admin@example.com).')
