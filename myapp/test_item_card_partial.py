import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.models import ItemShare, UserPreference
from myapp.tests import make_item


def _visible_text(html):
    """Strips tags (and therefore attribute values/CSS/JS) so a substring
    check only matches text a user would actually see, not an href, a CSS
    rule, or an inline <script>."""
    return re.sub(r'<[^>]+>', ' ', html)


class ItemCardTypeLabelTests(TestCase):
    """
    Regression test for a real bug found while consolidating inventory.html
    and sharing_center.html onto a shared partials/item_card.html: inventory
    rendered {% translate entry.item.type %} - since the raw internal type
    values ('giftcard', 'travelpass', etc.) were never registered gettext
    msgids, this rendered the literal untranslated slug for every item, not
    a human-readable label. sharing_center's chain (translated labels for
    the 4 common types, get_type_display() fallback otherwise) was already
    correct and is what the shared partial now uses for both pages.

    travelpass is the type that exposes this most clearly: it has no icon
    branch in the surrounding if/elif chain, and - unlike voucher/giftcard/
    coupon/loyaltycard, whose plural forms already appear in the page's
    type-filter chips/dropdown regardless of any item's data - "Travel
    Pass" never appears anywhere else on the inventory page, so this
    assertion can only pass if the item card itself renders the humanized
    label.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_inventory_renders_the_humanized_travelpass_label_not_the_raw_slug(self):
        make_item(self.user, type='travelpass', name='My Travel Pass', redeem_code='TP1')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'Travel Pass')
        visible = _visible_text(response.content.decode())
        self.assertNotIn('travelpass', visible)


class ItemCardValueVisibilityTests(TestCase):
    """
    inventory.html and sharing_center.html hide the value meta-item for
    loyaltycard items on both pages, but only inventory additionally hides
    it for travelpass - a genuine, pre-existing per-page difference (not a
    bug) preserved through the shared-partial consolidation via the
    hide_value_for_travelpass flag.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')

    def test_inventory_hides_value_for_travelpass(self):
        self.client.login(username='alice', password='pw12345!')
        make_item(self.user, type='travelpass', value=Decimal('42.00'), redeem_code='TP1')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertNotContains(response, '42.00')

    def test_inventory_shows_value_for_giftcard(self):
        self.client.login(username='alice', password='pw12345!')
        make_item(self.user, type='giftcard', value=Decimal('42.00'), redeem_code='GC1')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, '42.00')

    def test_sharing_center_shows_value_for_travelpass(self):
        item = make_item(self.user, type='travelpass', value=Decimal('42.00'), redeem_code='TP2')
        ItemShare.objects.create(item=item, shared_with_user=self.other_user, shared_by=self.user)
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('sharing_center'))
        self.assertContains(response, '42.00')


class ItemCardPerPageElementTests(TestCase):
    """Confirms the page-specific bits of the item card (pin/share/select
    controls, wallet+tags row, and the shared-with/by-me badge + footer)
    only render on the page they belong to. Assertions target markup-only
    signals (a form action, a data-* attribute, an exact class+attribute
    pairing) rather than a bare class name, since several of those class
    names also appear in each page's own <style> block regardless of
    whether the corresponding element actually rendered."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')

    def test_inventory_renders_pin_share_and_select_controls(self):
        self.client.login(username='alice', password='pw12345!')
        make_item(self.user, redeem_code='ITEM1')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'items/toggle_pin/')
        self.assertContains(response, 'data-public-share-url')
        self.assertContains(response, 'type="checkbox" class="select-checkbox"')

    def test_inventory_does_not_render_sharing_badge_or_footer(self):
        self.client.login(username='alice', password='pw12345!')
        make_item(self.user, redeem_code='ITEM2')
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertNotContains(response, 'class="shared-info"')
        self.assertNotContains(response, 'status-badge shared-with-me')
        self.assertNotContains(response, 'status-badge shared-by-me')

    def test_sharing_center_does_not_render_pin_share_or_select_controls(self):
        item = make_item(self.user, redeem_code='ITEM3')
        ItemShare.objects.create(item=item, shared_with_user=self.other_user, shared_by=self.user)
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('sharing_center'))
        self.assertNotContains(response, 'items/toggle_pin/')
        self.assertNotContains(response, 'data-public-share-url')
        self.assertNotContains(response, 'type="checkbox" class="select-checkbox"')

    def test_sharing_center_renders_sharing_badge_and_footer(self):
        item = make_item(self.user, redeem_code='ITEM4')
        ItemShare.objects.create(item=item, shared_with_user=self.other_user, shared_by=self.user)
        self.client.login(username='bob', password='pw12345!')
        response = self.client.get(reverse('sharing_center'))
        self.assertContains(response, 'class="shared-info"')
        self.assertContains(response, 'status-badge shared-with-me')


class ItemCardDescriptionTruncationTests(TestCase):
    """inventory truncates the description to 10 (compact) or 25 (standard)
    words depending on preferences.view_mode; sharing_center always
    truncates to 15 words regardless of view_mode, matching its original
    hardcoded behavior."""

    LONG_DESCRIPTION = ' '.join(f'word{i}' for i in range(1, 31))  # 30 words

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')

    def test_inventory_compact_view_truncates_to_10_words(self):
        self.client.login(username='alice', password='pw12345!')
        UserPreference.objects.update_or_create(user=self.user, defaults={'view_mode': 'compact', 'show_description': True})
        make_item(self.user, redeem_code='ITEM5', description=self.LONG_DESCRIPTION)
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'word10')
        self.assertNotContains(response, 'word11')

    def test_inventory_standard_view_truncates_to_25_words(self):
        self.client.login(username='alice', password='pw12345!')
        UserPreference.objects.update_or_create(user=self.user, defaults={'view_mode': 'standard', 'show_description': True})
        make_item(self.user, redeem_code='ITEM6', description=self.LONG_DESCRIPTION)
        response = self.client.get(reverse('show_items'), {'status': 'all'})
        self.assertContains(response, 'word25')
        self.assertNotContains(response, 'word26')

    def test_sharing_center_always_truncates_to_15_words_regardless_of_view_mode(self):
        item = make_item(self.user, redeem_code='ITEM7', description=self.LONG_DESCRIPTION)
        ItemShare.objects.create(item=item, shared_with_user=self.other_user, shared_by=self.user)
        self.client.login(username='bob', password='pw12345!')
        UserPreference.objects.update_or_create(user=self.other_user, defaults={'view_mode': 'standard'})
        response = self.client.get(reverse('sharing_center'))
        self.assertContains(response, 'word15')
        self.assertNotContains(response, 'word16')
