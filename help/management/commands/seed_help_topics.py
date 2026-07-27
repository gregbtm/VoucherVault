from django.core.management.base import BaseCommand
from help.models import HelpTopic


class Command(BaseCommand):
    help = 'Seed initial help topics'

    def handle(self, *args, **options):
        help_topics = [
            {
                'title': 'Getting Started with VoucherVault',
                'slug': 'getting-started-overview',
                'category': 'getting-started',
                'description': 'Introduction to VoucherVault and basic features',
                'content': '<h2>Welcome to VoucherVault</h2><p>VoucherVault is your digital wallet for vouchers, gift cards, loyalty cards, and coupons.</p><h3>Key Features:</h3><ul><li>Organize all your digital cards in one place</li><li>Add items via camera or manual entry</li><li>Track expiry dates with notifications</li><li>Share wallets with friends and family</li><li>Access offline with PWA support</li></ul>',
                'tags': ['intro', 'overview', 'first-time'],
            },
            {
                'title': 'Adding Items',
                'slug': 'adding-items',
                'category': 'items',
                'description': 'Learn how to add vouchers, gift cards, and loyalty cards',
                'content': '<h2>Adding Items to Your Inventory</h2><p>There are several ways to add items to VoucherVault:</p><h3>Camera Scan</h3><p>Click the camera icon to scan a barcode. VoucherVault will automatically extract details from the barcode.</p><h3>Manual Entry</h3><p>Click "Add Item" and fill in the details manually. You can add images, descriptions, and other information.</p><h3>Import from File</h3><p>Use the Import feature to upload CSV or JSON files containing multiple items at once.</p>',
                'tags': ['items', 'camera', 'scan', 'manual', 'import'],
            },
            {
                'title': 'Inventory Management',
                'slug': 'inventory-management',
                'category': 'inventory',
                'description': 'Overview of the inventory dashboard and search',
                'content': '<h2>Managing Your Inventory</h2><p>The inventory page is your main dashboard for viewing all items.</p><h3>View Options</h3><ul><li><strong>List View:</strong> See all items in a detailed list</li><li><strong>Grid View:</strong> Visual card layout for quick browsing</li></ul><h3>Filtering</h3><p>Use the filter options to narrow down items by:</p><ul><li>Wallet</li><li>Tag</li><li>Status (Active, Archived, Expired)</li><li>Category (Gift Card, Voucher, etc.)</li></ul><h3>Sorting</h3><p>Sort items by expiry date, name, value, or last used.</p>',
                'tags': ['inventory', 'dashboard', 'filter', 'sort', 'view'],
            },
            {
                'title': 'Creating and Managing Wallets',
                'slug': 'wallets-guide',
                'category': 'wallets',
                'description': 'Organize items into wallets and share with others',
                'content': '<h2>Wallets: Organizing Your Items</h2><p>Wallets help you organize items by purpose or person.</p><h3>Creating a Wallet</h3><ol><li>Go to the Wallets section</li><li>Click "Create Wallet"</li><li>Enter a name (e.g., "Groceries", "Travel")</li><li>Save</li></ol><h3>Adding Items to Wallets</h3><p>When creating or editing an item, select which wallet it belongs to.</p><h3>Sharing Wallets</h3><p>Share entire wallets with family members or friends. They can view and add items to shared wallets.</p>',
                'tags': ['wallets', 'organize', 'share', 'collaboration'],
            },
            {
                'title': 'Using Tags',
                'slug': 'tags-guide',
                'category': 'tags',
                'description': 'Tag items for better organization and filtering',
                'content': '<h2>Tags: Additional Organization</h2><p>Tags provide a flexible way to organize and categorize items across multiple dimensions.</p><h3>Creating Tags</h3><p>Go to Tags and click "Add Tag". Enter a descriptive name.</p><h3>Assigning Tags to Items</h3><p>When creating or editing an item, select one or more tags to apply.</p><h3>Using Tags for Filtering</h3><p>Filter your inventory by tags to quickly find related items. For example, tag all birthday gift cards with "birthday".</p><h3>Tag Ideas</h3><ul><li>By season: "Winter", "Summer"</li><li>By recipient: "Mom", "Kids"</li><li>By purpose: "Restaurant", "Shopping"</li></ul>',
                'tags': ['tags', 'organize', 'filter', 'categorize'],
            },
            {
                'title': 'Setting Up Notifications',
                'slug': 'notifications-setup',
                'category': 'notifications',
                'description': 'Get alerts before your items expire',
                'content': '<h2>Notification Rules</h2><p>Set up rules to receive notifications when items are about to expire.</p><h3>Creating a Notification Rule</h3><ol><li>Go to Notifications → Rules</li><li>Click "Add Rule"</li><li>Choose how to be notified (Email, Push, Webhook, etc.)</li><li>Set when to notify (e.g., 7 days before expiry)</li><li>Configure which items trigger the notification</li></ol><h3>Notification Channels</h3><ul><li><strong>Email:</strong> Receive alerts in your inbox</li><li><strong>Web Push:</strong> Browser notifications</li><li><strong>Webhook:</strong> Integration with external services</li><li><strong>ntfy:</strong> Send to ntfy.sh topic</li></ul>',
                'tags': ['notifications', 'alerts', 'expiry', 'reminders'],
            },
            {
                'title': 'Sharing Items and Wallets',
                'slug': 'sharing-guide',
                'category': 'sharing',
                'description': 'Share items and wallets with others',
                'content': '<h2>Sharing Your Items</h2><p>VoucherVault makes it easy to share items and wallets.</p><h3>Sharing Individual Items</h3><p>On an item detail page, use the Share button to:</p><ul><li>Share via social media</li><li>Copy a public link</li><li>Share with other VoucherVault users</li></ul><h3>Sharing Wallets</h3><p>Invite specific users to view and contribute to a wallet. They\'ll see all items in that wallet.</p><h3>Public Links</h3><p>Create a public link to share an item with anyone, even if they don\'t have a VoucherVault account. You can set an optional PIN code for security.</p>',
                'tags': ['sharing', 'social', 'collaboration', 'links'],
            },
            {
                'title': 'User Settings',
                'slug': 'user-settings',
                'category': 'settings',
                'description': 'Customize your VoucherVault experience',
                'content': '<h2>Settings</h2><p>Personalize your VoucherVault experience in Settings.</p><h3>Display Preferences</h3><ul><li><strong>Theme:</strong> Light or Dark mode</li><li><strong>Language:</strong> Choose your preferred language</li><li><strong>Timezone:</strong> Set your local timezone for notifications</li></ul><h3>Notification Settings</h3><p>Configure how you want to receive notifications and which types you\'d like to see.</p><h3>Data & Privacy</h3><ul><li>Export your data</li><li>Backup and restore</li><li>Manage connected apps</li><li>View activity log</li></ul>',
                'tags': ['settings', 'preferences', 'customization'],
            },
            {
                'title': 'Frequently Asked Questions',
                'slug': 'faq-overview',
                'category': 'faq',
                'description': 'Common questions and quick answers',
                'content': '<h2>Frequently Asked Questions</h2><h3>Is my data secure?</h3><p>Yes! VoucherVault uses industry-standard encryption. Your items are encrypted and only visible to you and people you explicitly share with.</p><h3>Does it work offline?</h3><p>Yes! VoucherVault works offline in supported browsers. When you\'re back online, changes sync automatically.</p><h3>Can I export my data?</h3><p>Absolutely. You can export your data as CSV or JSON at any time from Settings.</p><h3>What file formats are supported?</h3><p>We support CSV and JSON imports. You can import data from other apps like Catima.</p>',
                'tags': ['faq', 'questions', 'answers', 'help'],
            },
            {
                'title': 'Troubleshooting',
                'slug': 'troubleshooting-overview',
                'category': 'troubleshooting',
                'description': 'Solutions to common issues',
                'content': '<h2>Troubleshooting</h2><h3>Camera scan not working</h3><p>Make sure you\'ve granted camera permissions to VoucherVault in your browser settings.</p><h3>Notifications not arriving</h3><p>Check that notifications are enabled in your browser and app settings. Verify your notification rule is set correctly.</p><h3>Data not syncing</h3><p>Ensure you have an internet connection and that you\'re signed in to your account. Try refreshing the page.</p><h3>Items not appearing</h3><p>Check your filters and search terms. Items might be hidden by active filters.</p>',
                'tags': ['troubleshooting', 'issues', 'problems', 'errors'],
            },
            {
                'title': 'Item Types',
                'slug': 'item-types-guide',
                'category': 'items',
                'description': 'Different types of items VoucherVault supports',
                'content': '<h2>Item Types</h2><p>VoucherVault supports several types of items:</p><h3>Gift Card</h3><p>Digital or physical gift cards with remaining balance. Track the amount spent.</p><h3>Voucher</h3><p>One-time use vouchers or discount codes. Mark as used when redeemed.</p><h3>Loyalty Card</h3><p>Member cards for loyalty programs. Store member ID and track points or visits.</p><h3>Coupon</h3><p>Limited-time discount coupons with expiry dates.</p><h3>Travel Pass</h3><p>Transport tickets, boarding passes, and event tickets with travel information.</p>',
                'tags': ['items', 'types', 'categories'],
            },
        ]

        created_count = 0
        for topic_data in help_topics:
            topic, created = HelpTopic.objects.get_or_create(
                slug=topic_data['slug'],
                defaults={
                    'title': topic_data['title'],
                    'category': topic_data['category'],
                    'description': topic_data['description'],
                    'content': topic_data['content'],
                    'tags': topic_data['tags'],
                    'is_published': True,
                }
            )
            if created:
                created_count += 1
                self.stdout.write(f'Created: {topic.title}')

        self.stdout.write(
            self.style.SUCCESS(f'\nSuccessfully created {created_count} help topics')
        )
