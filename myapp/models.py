from datetime import time
from decimal import Decimal

from django.core.cache import cache
from django.db import models
from django.db.models import ExpressionWrapper, F, Sum
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
import os
import uuid
from django.core.validators import RegexValidator, MinValueValidator, MaxValueValidator

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
    tilt_scan_detection_enabled = models.BooleanField(
        default=False,
        help_text="On an item's page, suggest marking it Used when you tilt your phone "
                   "forward - the motion of presenting a barcode to a reader (train "
                   "barriers, a till scanner). Requires enabling motion access once per "
                   "device; never marks an item used on its own, only suggests it.",
    )
    oled_dark_mode = models.BooleanField(
        default=False, help_text="Use true-black surfaces in dark mode (OLED screens)."
    )
    offline_cache_enabled = models.BooleanField(
        default=True, help_text="Show the 'Cache for Offline' option in the sidebar. Turning this off purges any existing offline cache."
    )
    blur_codes_enabled = models.BooleanField(
        default=True,
        help_text="Blur barcodes and redeem codes until tapped. Turn off for faster access at point-of-sale.",
    )
    next_up_wallets = models.ManyToManyField(
        'Wallet', blank=True, related_name='+',
        help_text="Highlight the soonest-expiring items from these wallets at the top of Inventory "
                   "(e.g. a 'Train Tickets' wallet, to always surface the next one to use). Empty means off.",
    )
    next_up_max_items = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(3)],
        help_text="How many upcoming items to show in the Next Up widget, soonest first (1-3).",
    )
    active_today_enabled = models.BooleanField(
        default=False,
        help_text="Show the Active Today widget - surfaces today's outward or return leg of a "
                   "round-trip ticket (e.g. a daily commute), switching over at the cutoff time "
                   "below. Requires a home station to be set.",
    )
    commute_home_station = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Your home station/stop. Matched case-insensitively against a ticket's journey "
                   "origin/destination to tell an outward leg from a return leg, e.g. \"Hatfield "
                   "Peverel\" or \"HAP\" - use whatever your tickets actually print.",
    )
    active_today_cutoff_time = models.TimeField(
        default=time(12, 0),
        help_text="Before this time, Active Today shows today's outward leg (home station as "
                   "origin). From this time on, the outward leg is marked used and the return "
                   "leg (home station as destination) is shown instead.",
    )
    nearby_items_enabled = models.BooleanField(
        default=False,
        help_text="On Inventory, ask for your location once and suggest items whose issuer "
                   "matches a shop near you right now (e.g. \"You're near Tesco - 2 items "
                   "here\"). A one-shot lookup only - never tracks your location in the "
                   "background. Looked up via OpenStreetMap; no account or API key needed.",
    )
    nearby_radius_m = models.PositiveIntegerField(
        default=150,
        validators=[MinValueValidator(25), MaxValueValidator(1000)],
        help_text="How far around your current location to look for a matching shop, in metres.",
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
    auto_assign_issuer_match = models.CharField(
        max_length=100, blank=True, default='',
        help_text="New items whose issuer contains this text (case-insensitive) are automatically "
                   "placed in this wallet, unless a wallet was already chosen. E.g. \"National "
                   "Rail\" to route scanned train tickets straight into a \"Train Tickets\" wallet.",
    )
    budget_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Optional monthly spending budget for this wallet. When set, a progress bar "
                   "shows how much of the budget has been spent in the current calendar month.",
    )
    firefly_rule = models.ForeignKey(
        'notify.NotificationRule',
        null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text="Firefly III notification rule used for items in this wallet. Overrides user default; overridden by per-item setting.",
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']
        unique_together = ('user', 'name')

    def __str__(self):
        return self.name

    @classmethod
    def match_for_issuer(cls, user, issuer):
        """
        Returns the first of `user`'s wallets whose `auto_assign_issuer_match`
        is a case-insensitive substring of `issuer`, or None. Used to
        auto-file a newly created item (e.g. a scanned train ticket) into
        the right wallet without the user having to pick one by hand.
        """
        if not issuer:
            return None
        for wallet in cls.objects.filter(user=user).exclude(auto_assign_issuer_match='').order_by('name'):
            if wallet.auto_assign_issuer_match.lower() in issuer.lower():
                return wallet
        return None

    TRAVEL_PASS_WALLET_NAME = 'Travel Pass'

    @classmethod
    def get_or_create_travel_pass_wallet(cls, user):
        """
        Every Travel Pass item is unconditionally filed here - see
        Item.save(). Created lazily on first use rather than at signup, so
        a user who never scans a travel ticket never gets an empty wallet
        they didn't ask for.
        """
        wallet, _ = cls.objects.get_or_create(user=user, name=cls.TRAVEL_PASS_WALLET_NAME)
        return wallet


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
        ('travelpass', 'Travel Pass'),
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
    share_message = models.TextField(
        blank=True, default='',
        help_text="Optional message shown to anyone viewing the public share link."
    )
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
    image_phash = models.CharField(
        max_length=16, blank=True, default='',
        help_text="Perceptual hash of `file`, for duplicate-photo detection. Computed "
                   "automatically on save - never set this by hand.",
    )
    journey_origin = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Departure station/stop for a train or transport ticket, e.g. \"Hatfield "
                   "Peverel\" or \"HAP\". Powers the Active Today widget - leave blank for "
                   "anything that isn't a point-to-point travel ticket.",
    )
    journey_destination = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Arrival station/stop for a train or transport ticket, e.g. \"London "
                   "Terminals\" or \"LON\".",
    )
    travel_time = models.TimeField(
        null=True, blank=True,
        help_text="Scheduled departure/travel time for a Travel Pass, if known - leave blank "
                   "otherwise (many train tickets are date-only, with no fixed time).",
    )
    order_id = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Merchant/booking order or confirmation reference, if different from the "
                   "redeem code - e.g. a rail ticket's order ID alongside its separate ticket "
                   "number. Optional for any item type.",
    )
    discount_applied = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Discount or railcard applied at purchase, e.g. \"Network Railcard\". "
                   "Informational only.",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    minimum_spend = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Minimum basket value required to redeem this voucher or coupon.",
    )
    points_balance = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Current points or stamps total on a loyalty card. Updated manually.",
    )
    membership_tier = models.CharField(
        max_length=50, blank=True, default='',
        help_text="Loyalty scheme tier, e.g. Silver, Gold, Platinum.",
    )
    initial_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Face/loaded value when purchased — useful for tracking discount or spend on gift cards.",
    )
    seat_number = models.CharField(
        max_length=50, blank=True, default='',
        help_text="Seat or coach reservation on a travel ticket, e.g. Coach C, Seat 42.",
    )
    firefly_account_id = models.CharField(
        max_length=50, blank=True, default='',
        help_text="Firefly III asset account ID linked to this item. Set via 'Link to Firefly III' or enter manually.",
    )
    firefly_rule = models.ForeignKey(
        'notify.NotificationRule',
        null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text="Override which Firefly III notification rule handles this item's balance sync. Falls back to wallet-level rule, then user's first enabled Firefly rule.",
    )
    notifications_muted = models.BooleanField(
        default=False,
        help_text="Suppress all notification rules for this item (expiry warnings, renewal alerts, etc.).",
    )
    is_recurring = models.BooleanField(
        default=False,
        help_text="This item renews periodically (subscription, annual pass, etc.).",
    )
    RENEWAL_PERIOD_CHOICES = (
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
        ('biannual', 'Every 6 months'),
        ('annual', 'Annual'),
    )
    renewal_period = models.CharField(
        max_length=10, blank=True, choices=RENEWAL_PERIOD_CHOICES,
        help_text="How often this item renews.",
    )
    renewal_date = models.DateField(
        null=True, blank=True,
        help_text="Next renewal / billing date for recurring items.",
    )

    objects = ItemQuerySet.as_manager()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cache the value at load time so Item.save() can detect changes.
        self._original_value = self.value

    class Meta:
        indexes = [
            # Covers the most common filter: user's active items with expiry
            models.Index(fields=['user', 'is_used', 'expiry_date'], name='item_user_used_expiry'),
            # Covers per-type count queries on the dashboard and inventory
            models.Index(fields=['user', 'type', 'is_used'], name='item_user_type_used'),
            # Covers archived filter (exclude + filter on is_archived)
            models.Index(fields=['user', 'is_archived', 'is_used'], name='item_user_archived_used'),
        ]

    def save(self, *args, **kwargs):
        # FieldFile._committed is False exactly when a new upload has just
        # been assigned to `file` and not yet written to storage - this is
        # Django's own signal for "the file changed this save call", so a
        # save that only touches other fields doesn't re-read and re-hash
        # a file that hasn't changed. Recomputing is cheap either way (a
        # tiny 9x8 thumbnail), so a missing/unrecognized attribute just
        # falls through to "compute it" rather than skipping silently.
        if self.file and not getattr(self.file, '_committed', True):
            from .imagehash import compute_dhash
            try:
                self.file.seek(0)
                self.image_phash = compute_dhash(self.file.read())
                self.file.seek(0)
            except Exception:
                pass
        # Every Travel Pass is unconditionally filed into the "Travel Pass"
        # wallet - enforced here rather than per-caller (views, API, CSV
        # import) so nothing can bypass it by skipping the web form.
        if self.type == 'travelpass' and self.user_id:
            self.wallet = Wallet.get_or_create_travel_pass_wallet(self.user)
        super().save(*args, **kwargs)

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

class ScanFieldCorrection(models.Model):
    """
    One user's remembered correction to an AI photo-scan field: "the scan
    said `ai_value` for `field`, and I changed it to `corrected_value`
    before saving". Recorded automatically on item save (see
    myapp/scan_learning.py) and replayed against future scan results, so
    a correction only ever has to be made once - e.g. an operator name
    the model keeps misreading, or a barcode symbology it keeps calling
    "qrcode" when this user's train tickets are really Aztec codes.

    ai_value == '' means "the scan left this field blank and the user
    filled it in" - those are replayed more cautiously (only after being
    seen twice, and only for the same item type) since a blank can be
    filled with something item-specific once without it being a pattern.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='scan_corrections')
    item_type = models.CharField(max_length=100, blank=True, default='')
    field = models.CharField(max_length=50)
    ai_value = models.CharField(max_length=255, blank=True, default='')
    corrected_value = models.CharField(max_length=255)
    times_seen = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'item_type', 'field', 'ai_value')

    def __str__(self):
        shown = self.ai_value or '(blank)'
        return f'{self.field}: {shown} -> {self.corrected_value}'


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
    extracted_text = models.TextField(
        blank=True, default='',
        help_text="Text extracted from the document by OCR, populated automatically after upload when OCR is enabled.",
    )

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
    firefly_transaction_id = models.CharField(
        max_length=64, blank=True, default='',
        help_text="Firefly III transaction ID after a successful sync. Blank means unsynced or Firefly not configured.",
    )

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
    last_check_error = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Set when the most recent check couldn't reach GitHub or got an unexpected response; "
                   "cleared on the next successful check. Blank with a recent checked_at means GitHub was reachable.",
    )

    class Meta:
        verbose_name = "Update Check Status"
        verbose_name_plural = "Update Check Status"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Update check (latest: {self.latest_version or 'unknown'})"


# ── Phase C/D/E/F additions ─────────────────────────────────────────────────

class WalletMembership(models.Model):
    """Role-aware record of a shared-wallet collaboration (Phase D)."""
    ROLE_VIEWER = 'viewer'
    ROLE_EDITOR = 'editor'
    ROLE_CHOICES = [(ROLE_VIEWER, 'Viewer'), (ROLE_EDITOR, 'Editor')]

    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wallet_memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_EDITOR)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('wallet', 'user')

    def __str__(self):
        return f"{self.user.username} on {self.wallet.name} ({self.role})"


class WalletActivity(models.Model):
    """Immutable audit trail for shared-wallet actions (Phase D)."""
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='activities')
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='wallet_activities')
    action = models.CharField(max_length=50)
    item = models.ForeignKey('Item', on_delete=models.SET_NULL, null=True, blank=True, related_name='wallet_activities')
    item_name = models.CharField(max_length=255, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        actor = self.actor.username if self.actor else '(deleted)'
        return f"{actor} {self.action} in {self.wallet.name}"


class UserWebhook(models.Model):
    """Per-user outbound webhook fired on item lifecycle events (Phase E)."""
    EVENT_CHOICES = [
        ('item_created', 'Item Created'),
        ('item_used', 'Item Marked Used'),
        ('item_archived', 'Item Archived'),
        ('item_balance_changed', 'Balance Updated'),
        ('item_expiry_warning', 'Expiry Warning'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='webhooks')
    name = models.CharField(max_length=100)
    url = models.URLField()
    secret = models.CharField(
        max_length=64, blank=True, default='',
        help_text="Optional HMAC-SHA256 signing secret. When set, each request includes an X-VoucherVault-Signature header.",
    )
    events = models.JSONField(default=list, help_text="List of event types that trigger this webhook.")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.user.username})"


class TOTPDevice(models.Model):
    """TOTP authenticator app binding for a user (Phase F)."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='totp_device')
    secret = models.CharField(max_length=32)
    confirmed = models.BooleanField(default=False)
    name = models.CharField(max_length=100, default='Authenticator App')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"TOTP for {self.user.username} ({'confirmed' if self.confirmed else 'pending'})"


class TOTPBackupCode(models.Model):
    """Single-use backup code for TOTP recovery. Stored as a Django password hash."""
    device = models.ForeignKey(TOTPDevice, on_delete=models.CASCADE, related_name='backup_codes')
    code_hash = models.CharField(max_length=128)
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Backup code for {self.device.user.username} ({'used' if self.used else 'available'})"


class LoginAuditLog(models.Model):
    """Immutable record of every login attempt (Phase F)."""
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='login_audit_logs')
    username_attempted = models.CharField(max_length=150, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    success = models.BooleanField()
    failure_reason = models.CharField(max_length=200, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        status = 'OK' if self.success else 'FAIL'
        return f"{self.username_attempted} [{status}] at {self.timestamp}"


class UpstreamSyncStatus(models.Model):
    """
    Singleton row (always pk=1) holding the result of the last check
    against l4rm4nd/VoucherVault's own releases - the upstream project
    this fork is built on top of. Separate from UpdateCheckStatus above,
    which tracks this FORK's own releases. Refreshed by the periodic
    check_upstream_version_task (see myapp/update_check.py). What was
    last actually merged from upstream is tracked in the committed
    UPSTREAM_VERSION file (settings.UPSTREAM_VERSION), not here - this
    model only holds what's currently available upstream.
    """
    upstream_repo = models.CharField(max_length=255, default='l4rm4nd/VoucherVault')
    latest_version = models.CharField(max_length=50, blank=True)
    latest_release_url = models.URLField(blank=True)
    latest_release_published_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    last_check_error = models.CharField(max_length=500, blank=True, default='')

    class Meta:
        verbose_name = "Upstream Sync Status"
        verbose_name_plural = "Upstream Sync Status"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Upstream sync (latest: {self.latest_version or 'unknown'})"


OCR_BACKEND_CHOICES = (
    ('none', 'Disabled'),
    ('claude', 'Claude'),
    ('openai', 'OpenAI'),
    ('tesseract', 'Tesseract (local, free)'),
)


class BalanceHistory(models.Model):
    """One row per balance change, used to draw a spend sparkline on the item detail page."""
    item = models.ForeignKey('Item', on_delete=models.CASCADE, related_name='balance_history')
    balance = models.DecimalField(max_digits=10, decimal_places=2)
    recorded_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['recorded_at']

    def __str__(self):
        return f'{self.item_id} @ {self.recorded_at}: {self.balance}'


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
    webpush_barcode_key_version = models.PositiveIntegerField(
        default=1,
        help_text="Incrementing this value invalidates all previously issued barcode push-image "
                  "tokens, forcing new tokens to be generated on the next notification send. "
                  "Rotate this if you suspect a token has been captured in transit.",
    )

    # ---- Merchant logos ----
    merchant_logos_enabled = models.BooleanField(default=True)
    logo_dev_api_key = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Optional logo.dev publishable key (pk_...). When set, logo.dev is tried "
                   "first for merchant logos - a dedicated logo API with consistently higher-"
                   "resolution real brand marks than the Clearbit/Google-favicon fallbacks "
                   "used otherwise. Get one at https://logo.dev.",
    )

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

    # ---- Gift card health features ----
    inactivity_threshold_days = models.PositiveIntegerField(
        default=90,
        help_text="Days without use before an item triggers an 'Unused Gift Card Reminder' "
                   "notification (for rules subscribed to that event). Applies to all "
                   "non-loyalty money-type items.",
    )
    companies_house_api_key = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Companies House API key for the Merchant Health Alert notification — "
                   "fires if a gift card issuer enters administration or liquidation. "
                   "Get a free key at https://developer.company-information.service.gov.uk/",
    )

    # ---- Notification enrichment ----
    vv_base_url = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Public base URL of this VoucherVault installation (e.g. https://vault.example.com). "
                   "Used to build clickable links in ntfy notifications. Leave blank to omit links.",
    )

    # ---- Nearby items (OpenStreetMap) ----
    nearby_places_enabled = models.BooleanField(
        default=True,
        help_text="Allow the opt-in 'Nearby' widget to query OpenStreetMap's Overpass API "
                   "with a user's one-shot location, matched against their item issuers. "
                   "Off disables the feature site-wide regardless of each user's own preference.",
    )
    overpass_api_url = models.CharField(
        max_length=255, blank=True, default='https://overpass-api.de/api/interpreter',
        help_text="Overpass API endpoint. Point this at your own instance to avoid the public "
                   "one's rate limits - see https://wiki.openstreetmap.org/wiki/Overpass_API/Installation.",
    )

    # ---- Registration control ----
    allow_registration = models.BooleanField(
        default=True,
        help_text="Allow new users to register accounts. Disable to make the instance invite-only "
                   "(existing users and social/OIDC logins are unaffected).",
    )

    # ---- Analytics & duplicate detection display/behaviour limits ----
    # Previously fixed constants in myapp/analytics.py and myapp/imagehash.py -
    # moved here after a settings-gap audit found them to be genuine
    # user-facing behaviour rather than safety/implementation limits.
    expiring_soon_limit = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
        help_text="Max items shown in the Dashboard's 'Expiring Soon' list.",
    )
    calendar_months_ahead = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text="How many months ahead the Dashboard's expiry calendar shows.",
    )
    wallet_chart_limit = models.PositiveIntegerField(
        default=8,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="How many wallets show individually in the 'Items by Wallet' chart before folding the rest into \"Other\".",
    )
    duplicate_photo_threshold = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(0), MaxValueValidator(64)],
        help_text="Hamming-distance sensitivity (out of 64 bits) for flagging two uploaded card "
                   "photos as likely duplicates. Lower is stricter (fewer false-positive matches, "
                   "but might miss a genuine duplicate shot at a different angle/lighting); "
                   "higher is looser.",
    )

    # Field names whose values are credentials/secrets rather than plain
    # config - the Site Settings form renders these as password inputs
    # that display blank and leave the stored value untouched when
    # submitted blank, instead of round-tripping the secret through the
    # page on every load/save.
    SECRET_FIELDS = (
        'webpush_vapid_private_key', 'anthropic_api_key', 'openai_api_key',
        'pkpass_cert_password', 'logo_dev_api_key', 'companies_house_api_key',
    )

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"

    @classmethod
    def load(cls):
        import sys
        if 'test' not in sys.argv:
            cached = cache.get('vv_site_config')
            if cached is not None:
                return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        if 'test' not in sys.argv:
            cache.set('vv_site_config', obj, 60)
        return obj

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        cache.delete('vv_site_config')

    def __str__(self):
        return "Site Configuration"

