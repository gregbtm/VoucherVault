from decimal import Decimal

from django.db import models
from django.db.models import ExpressionWrapper, F, Sum
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
import os
import uuid
from django.core.validators import RegexValidator

CURRENCY_CHOICES = (
    ('AED', 'AED - UAE Dirham'),
    ('AUD', 'AUD - Australian Dollar'),
    ('BGN', 'BGN - Bulgarian Lev'),
    ('BRL', 'BRL - Brazilian Real'),
    ('CAD', 'CAD - Canadian Dollar'),
    ('CHF', 'CHF - Swiss Franc'),
    ('CNY', 'CNY - Chinese Yuan'),
    ('CZK', 'CZK - Czech Koruna'),
    ('DKK', 'DKK - Danish Krone'),
    ('EUR', 'EUR - Euro'),
    ('GBP', 'GBP - British Pound'),
    ('HKD', 'HKD - Hong Kong Dollar'),
    ('HRK', 'HRK - Croatian Kuna'),
    ('HUF', 'HUF - Hungarian Forint'),
    ('IDR', 'IDR - Indonesian Rupiah'),
    ('ILS', 'ILS - Israeli Shekel'),
    ('INR', 'INR - Indian Rupee'),
    ('JPY', 'JPY - Japanese Yen'),
    ('KRW', 'KRW - South Korean Won'),
    ('MXN', 'MXN - Mexican Peso'),
    ('MYR', 'MYR - Malaysian Ringgit'),
    ('NOK', 'NOK - Norwegian Krone'),
    ('NZD', 'NZD - New Zealand Dollar'),
    ('PHP', 'PHP - Philippine Peso'),
    ('PLN', 'PLN - Polish Zloty'),
    ('RON', 'RON - Romanian Leu'),
    ('RUB', 'RUB - Russian Ruble'),
    ('SEK', 'SEK - Swedish Krona'),
    ('SGD', 'SGD - Singapore Dollar'),
    ('THB', 'THB - Thai Baht'),
    ('TRY', 'TRY - Turkish Lira'),
    ('USD', 'USD - US Dollar'),
    ('ZAR', 'ZAR - South African Rand'),
)

class UserPreference(models.Model):
    SORT_CHOICES = (
        ('expiry_date', 'Expiry Date'),
        ('name', 'Name'),
        ('issue_date', 'Creation Date'),
        ('value', 'Value'),
        ('last_used_at', 'Last Used'),
    )
    SORT_ORDER_CHOICES = (
        ('asc', 'Ascending'),
        ('desc', 'Descending'),
    )
    VIEW_MODE_CHOICES = (
        ('compact', 'Compact'),
        ('standard', 'Standard'),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    show_issue_date = models.BooleanField(default=False)
    show_expiry_date = models.BooleanField(default=True)
    show_value = models.BooleanField(default=True)
    show_description = models.BooleanField(default=True)
    sort_by = models.CharField(max_length=20, choices=SORT_CHOICES, default='expiry_date')
    sort_order = models.CharField(max_length=4, choices=SORT_ORDER_CHOICES, default='asc')
    view_mode = models.CharField(max_length=10, choices=VIEW_MODE_CHOICES, default='compact')
    fixer_api_key = models.CharField(max_length=64, blank=True, null=True, default=None)
    default_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GBP')
    keep_screen_awake = models.BooleanField(
        default=True, help_text="Keep the screen on while viewing an item's barcode."
    )
    oled_dark_mode = models.BooleanField(
        default=False, help_text="Use true-black surfaces in dark mode (OLED screens)."
    )
    offline_cache_enabled = models.BooleanField(
        default=True, help_text="Show the 'Cache for Offline' option in the sidebar. Turning this off purges any existing offline cache."
    )

class Wallet(models.Model):
    """
    User-defined folder for grouping items (e.g. "Supermarkets", "Travel").
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wallets')
    shared_with = models.ManyToManyField(
        User,
        blank=True,
        related_name='shared_wallets',
        help_text="Other users who can view and manage items in this wallet.",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, default='bi-wallet2')
    color = models.CharField(
        max_length=20,
        blank=True,
        default='#4154f1',
        validators=[RegexValidator(regex=r'^#(?:[0-9a-fA-F]{3}){1,2}$', message='Enter a valid hex color.')],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('user', 'name')

    def __str__(self):
        return self.name


class Tag(models.Model):
    """
    Freeform, per-user label that can be attached to multiple items.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tags')
    name = models.CharField(max_length=50)
    color = models.CharField(
        max_length=20,
        default='#6b7280',
        validators=[RegexValidator(regex=r'^#(?:[0-9a-fA-F]{3}){1,2}$', message='Enter a valid hex color.')],
    )

    class Meta:
        ordering = ['name']
        unique_together = ('user', 'name')

    def __str__(self):
        return self.name


class MerchantProfile(models.Model):
    """
    Cached merchant metadata (logo, domain, brand colour), looked up by
    Item.issuer (case-insensitive). Shared across all users — fetched once
    from an external logo service, reused thereafter — so this is a global
    cache table, not scoped to a user. No FK from Item: `issuer` is
    freeform text the user already types, so the cache is looked up by
    normalized name at display/serialization time instead of requiring a
    relation to stay in sync with it.
    """
    name = models.CharField(max_length=200, unique=True)
    domain = models.CharField(max_length=200, blank=True)
    logo_url = models.URLField(blank=True)
    brand_color = models.CharField(
        max_length=20,
        blank=True,
        validators=[RegexValidator(regex=r'^#(?:[0-9a-fA-F]{3}){1,2}$', message='Enter a valid hex color.')],
    )
    fetched_at = models.DateTimeField(null=True, blank=True)
    balance_check_url = models.URLField(
        blank=True,
        help_text="Remembered gift-card balance/validity check link for this merchant, "
                   "suggested on future gift cards from the same issuer.",
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class ItemQuerySet(models.QuerySet):
    def with_current_balance(self):
        """
        Annotates each item with `current_balance` — its starting `value`
        plus every transaction against it (transactions are negative
        spends). Centralizes the balance calculation for list/bulk views
        as a single annotated query instead of one query per item.
        """
        return self.annotate(
            transaction_total=Sum('transactions__value', default=Decimal('0'))
        ).annotate(
            current_balance=ExpressionWrapper(
                F('value') + F('transaction_total'),
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            )
        )


class Item(models.Model):
    ITEM_TYPES = (
        ('voucher', 'Voucher'),
        ('giftcard', 'Gift Card'),
        ('coupon', 'Coupon'),
        ('loyaltycard', 'Loyalty Card'),
    )
    VALUE_TYPES = (
        ('money', 'Money'),
        ('percentage', 'Percentage'),
        ('multiplier', 'Multiplier'),
    )    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=100, choices=ITEM_TYPES)
    name = models.CharField(max_length=255)
    redeem_code = models.CharField(max_length=255)
    code_type = models.CharField(default="qrcode", max_length=100)
    pin = models.CharField(max_length=25, blank=True, null=True)
    issuer = models.CharField(max_length=255)
    issue_date = models.DateField(default=timezone.now)
    expiry_date = models.DateField()
    description = models.TextField(blank=True, null=True)
    logo_slug = models.CharField(max_length=100, blank=True, null=True, default=None)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    value_type = models.CharField(max_length=20, choices=VALUE_TYPES, default='money')
    is_used = models.BooleanField(default=False)
    qr_code_base64 = models.TextField(blank=True, null=True)
    file = models.FileField(upload_to='database/', blank=True, null=True)
    default_expiry_notification_sent = models.BooleanField(default=False)
    final_expiry_notification_sent = models.BooleanField(default=False)
    tile_color = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        validators=[RegexValidator(regex=r'^#(?:[0-9a-fA-F]{3}){1,2}$', message='Enter a valid hex color.')],
        help_text="Hex code like #FF5733"
    )
    is_pinned = models.BooleanField(default=False)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='GBP')
    wallet = models.ForeignKey(
        Wallet, on_delete=models.SET_NULL, null=True, blank=True, related_name='items'
    )
    tags = models.ManyToManyField(Tag, blank=True, related_name='items')
    notes = models.TextField(blank=True, help_text="Redemption instructions, terms, etc.")
    notify_days_before = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Override the global expiry notification threshold for this item (in days)."
    )
    SOURCE_CHOICES = (
        ('manual', 'Manual'),
        ('csv_import', 'CSV Import'),
        ('api', 'API'),
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    card_number = models.CharField(
        max_length=255, blank=True,
        help_text="Printed member/account number, if different from the barcode's encoded value. Falls back to the redeem code when blank."
    )
    is_archived = models.BooleanField(
        default=False, help_text="Hide from the default inventory view without marking it used or deleting it."
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    balance_check_url = models.URLField(
        blank=True,
        help_text="Link to the merchant's balance/validity check page for this gift card. "
                   "Not a live check — VoucherVault has no way to query balances itself.",
    )

    objects = ItemQuerySet.as_manager()

    def get_current_balance(self, transactions=None):
        """
        Starting value plus every transaction against it (transactions are
        negative spends). Pass an already-fetched `transactions` iterable
        to reuse it instead of triggering another query; for bulk/list
        views, prefer `Item.objects.with_current_balance()` instead, which
        computes this in a single annotated query.
        """
        if transactions is None:
            transactions = self.transactions.all()
        return self.value + sum(t.value for t in transactions)

    def __str__(self):
        return self.name

def document_upload_path(instance, filename):
    safe_name = os.path.basename(filename)
    username = str(instance.item.user)
    return f"database/documents/{username}/{instance.item.id}_{uuid.uuid4().hex}_{safe_name}"


class Document(models.Model):
    """
    A receipt or proof-of-purchase attached to an item. Distinct from
    Item.file (the voucher's own scanned image/PDF): an item can have any
    number of supporting documents.
    """
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='documents')
    file = models.FileField(upload_to=document_upload_path)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return os.path.basename(self.file.name)


class ItemShare(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='shared_with')
    shared_with_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_items')
    shared_at = models.DateTimeField(auto_now_add=True)
    shared_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shared_items')

    class Meta:
        unique_together = ('item', 'shared_with_user')


class ItemPublicShare(models.Model):
    """
    A tokenized, no-login-required link to a read-only redemption summary
    of an item (merchant, code/card number, PIN, remaining balance) - for
    handing a voucher to someone who doesn't have a VoucherVault account,
    as an alternative to ItemShare above (which grants another *VoucherVault
    user* full read/write access and therefore requires them to have one).

    One link per item, created lazily the first time "Share via..." is used
    in its "share details" mode. The id itself is the token in the URL -
    same pattern as Item's own UUID primary key - so there's no separate
    secret field to keep in sync.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.OneToOneField(Item, on_delete=models.CASCADE, related_name='public_share')
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    view_count = models.PositiveIntegerField(default=0)
    first_viewed_at = models.DateTimeField(null=True, blank=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Blank means never expires. Set from SiteConfiguration.share_link_expiry_days at creation/regeneration time.",
    )
    access_pin = models.CharField(
        max_length=10, blank=True, default='',
        help_text="Optional short code required to view this link, separate from the item's own redemption PIN "
                   "(Item.pin, shown on the page itself). Only set when SiteConfiguration.share_link_pin_enabled "
                   "was on at creation/regeneration time. Shown to the owner on the item detail page to relay "
                   "separately from the link itself - not stored as a hash, same plaintext-at-rest posture this "
                   "app already takes with redeem codes and item PINs.",
    )
    failed_pin_attempts = models.PositiveIntegerField(
        default=0,
        help_text="Lifetime count of wrong access-code guesses, surfaced to the owner as an early warning sign "
                   "of probing. Not reset on a successful unlock.",
    )

    def record_view(self):
        now_ts = timezone.now()
        if self.first_viewed_at is None:
            self.first_viewed_at = now_ts
        self.last_viewed_at = now_ts
        self.view_count += 1
        self.save(update_fields=['view_count', 'first_viewed_at', 'last_viewed_at'])

    def is_expired(self):
        return self.expires_at is not None and timezone.now() > self.expires_at

    def __str__(self):
        return f"Public share link for {self.item.name}"


class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='transactions')
    date = models.DateTimeField(default=timezone.now)
    description = models.CharField(max_length=255)
    value = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.description} ({self.value})"      

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    apprise_urls = models.TextField(blank=True, null=True)
    ics_token = models.CharField(
        max_length=64, unique=True, default=uuid.uuid4,
        help_text="Secret token in the subscribe-able .ics calendar feed URL. Regenerating it invalidates the old feed URL.",
    )

    def __str__(self):
        return self.user.username

class AppSettings(models.Model):
    """
    Model for storing API token for authentication.

    This model enforces a singleton pattern to ensure only one set of API settings exists.
    The API token is used for authenticating API requests.

    API Usage:
    - Endpoint: /en/api/get/stats
    - Method: GET
    - Authorization: Requires an API token provided in the `Authorization` header
      in the format: `Authorization: Bearer <API-TOKEN>`
    - Description: Retrieves statistical data about items, users, and issuers.

    Example:
    ```
    curl -H "Authorization: Bearer <API-TOKEN>" http://<your-domain>/api/get/stats
    ```

    Attributes:
    - api_token: A unique token used for API authentication.
    - updated_at: Timestamp of the last update to the API token.

    Methods:
    - regenerate_api_token: Generates a new API token and updates the `updated_at` field.
    """

    api_token = models.CharField(max_length=64, default=uuid.uuid4, unique=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "API Settings"
        verbose_name_plural = "API Settings"

    def regenerate_api_token(self):
        """Generate a new API token."""
        self.api_token = str(uuid.uuid4())  # Ensure it's saved as a string
        self.save()

    def save(self, *args, **kwargs):
        """Override save to enforce singleton behavior and validate the API token."""
        # Ensure only one instance exists
        if not self.pk and AppSettings.objects.exists():
            raise ValueError("Only one AppSettings instance is allowed.")

        # Validate the API token is a valid UUID
        if not isinstance(self.api_token, str):
            self.api_token = str(self.api_token)  # Convert to string if it's a UUID object
        try:
            uuid_obj = uuid.UUID(self.api_token)  # Validate if it's a valid UUID string
            self.api_token = str(uuid_obj)  # Normalize to UUID string format
        except ValueError:
            raise ValueError("The API token must be a valid UUID.")

        super().save(*args, **kwargs)

    def __str__(self):
        return f"API Token (Updated: {self.updated_at})"


class UpdateCheckStatus(models.Model):
    """
    Singleton row (always pk=1) holding the result of the last GitHub
    Releases check, refreshed by the periodic check_for_update_task
    (see myapp/update_check.py). A DB row rather than Django's cache
    framework because the check runs in a Celery worker process and the
    result needs to reach the separate web process(es) serving requests.
    """
    latest_version = models.CharField(max_length=50, blank=True)
    latest_release_url = models.URLField(blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    update_available = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Update Check Status"
        verbose_name_plural = "Update Check Status"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Update check (latest: {self.latest_version or 'unknown'})"


OCR_BACKEND_CHOICES = (
    ('none', 'Disabled'),
    ('claude', 'Claude'),
    ('openai', 'OpenAI'),
    ('tesseract', 'Tesseract (local, free)'),
)


class SiteConfiguration(models.Model):
    """
    Singleton row (always pk=1) holding every operational setting that can
    safely live in the database instead of an env var - i.e. everything
    except the bootstrap/infra settings a process needs before it can even
    reach the database (SECRET_KEY, DOMAIN/ALLOWED_HOSTS, DB_ENGINE and
    friends, REDIS_URL, OIDC_*, SESSION_*). Editable from the in-app Site
    Settings page (superusers only - see myapp/views.py::site_settings).

    Every field here used to be read straight from django.conf.settings
    (see myproject/settings.py's "APP-LEVEL / FEATURE ENV VARS" block,
    which still exists unchanged - it's now purely the seed value used the
    first time this singleton row is created, via a data migration, and
    the documented default for a fresh install). App code now reads
    SiteConfiguration.load() instead, deliberately a fresh DB query every
    time rather than something cached on the settings module: uWSGI runs
    multiple worker processes, and Celery's worker/beat run as separate
    processes again, none of which share Python-level state - only the
    database is common ground, the same reason UpdateCheckStatus above
    works this way.
    """
    # ---- Expiry notifications ----
    expiry_threshold_days = models.PositiveIntegerField(default=30)
    expiry_last_notification_days = models.PositiveIntegerField(default=7)
    ntfy_default_server = models.CharField(max_length=255, blank=True, default='https://ntfy.sh')

    # ---- Web Push notification backend ----
    webpush_vapid_public_key = models.CharField(max_length=255, blank=True, default='')
    webpush_vapid_private_key = models.CharField(max_length=255, blank=True, default='')
    webpush_vapid_claims_email = models.CharField(max_length=255, blank=True, default='mailto:admin@example.com')

    # ---- Merchant logos ----
    merchant_logos_enabled = models.BooleanField(default=True)

    # ---- Sharing ----
    share_via_smart_enabled = models.BooleanField(
        default=True,
        help_text="Let 'Share via...' offer a choice between a bare link and a rich "
                   "share (merchant, code, PIN, remaining balance) via a public, "
                   "no-login-required link. Off reverts to the classic single-tap share.",
    )
    share_link_expiry_days = models.PositiveIntegerField(
        default=30,
        help_text="How long a new/regenerated public share link stays valid. 0 means it never expires.",
    )
    share_link_pin_enabled = models.BooleanField(
        default=False,
        help_text="Require a short access code to view a public share link, on top of the link itself. "
                   "The code is shown to you on the item page to relay separately (call, text, in person) - "
                   "it is never automatically included in the shared text/link itself.",
    )

    # ---- OCR ("Scan with AI") ----
    ocr_backend = models.CharField(max_length=20, choices=OCR_BACKEND_CHOICES, default='none')
    anthropic_api_key = models.CharField(max_length=255, blank=True, default='')
    anthropic_ocr_model = models.CharField(max_length=100, blank=True, default='claude-sonnet-5')
    openai_api_key = models.CharField(max_length=255, blank=True, default='')
    openai_ocr_model = models.CharField(max_length=100, blank=True, default='gpt-4o-mini')

    # ---- Apple Wallet (.pkpass) export ----
    pkpass_cert_path = models.CharField(max_length=500, blank=True, default='')
    pkpass_cert_password = models.CharField(max_length=255, blank=True, default='')
    pkpass_wwdr_cert_path = models.CharField(max_length=500, blank=True, default='')
    pkpass_team_id = models.CharField(max_length=100, blank=True, default='')
    pkpass_pass_type_id = models.CharField(max_length=255, blank=True, default='')
    pkpass_organization_name = models.CharField(max_length=255, blank=True, default='VoucherVault Plus+')

    # ---- Google Wallet export ----
    google_wallet_service_account_key_path = models.CharField(max_length=500, blank=True, default='')
    google_wallet_issuer_id = models.CharField(max_length=100, blank=True, default='')
    google_wallet_class_id = models.CharField(max_length=255, blank=True, default='')

    # ---- Update check (GitHub Releases) ----
    update_check_enabled = models.BooleanField(default=True)
    update_check_repo = models.CharField(max_length=255, blank=True, default='gregbtm/VoucherVault')

    # ---- Portainer redeploy webhook ----
    portainer_webhook_url = models.CharField(max_length=500, blank=True, default='')

    # ---- Scheduled local backups ----
    scheduled_backup_enabled = models.BooleanField(default=True)
    backup_retention_count = models.PositiveIntegerField(default=7)

    # Field names whose values are credentials/secrets rather than plain
    # config - the Site Settings form renders these as password inputs
    # that display blank and leave the stored value untouched when
    # submitted blank, instead of round-tripping the secret through the
    # page on every load/save.
    SECRET_FIELDS = (
        'webpush_vapid_private_key', 'anthropic_api_key', 'openai_api_key',
        'pkpass_cert_password', 'portainer_webhook_url',
    )

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site Configuration"

