# Smart Categories

VoucherVault automatically categorizes your items based on the merchant name, helping you organize and track spending by type.

## Supported Categories

| Category | Examples | Use Case |
|----------|----------|----------|
| **Groceries** | Tesco, Sainsbury's, Asda, Waitrose | Food shopping |
| **Entertainment** | Netflix, Spotify, PlayStation, Xbox | Streaming & gaming |
| **Dining** | Deliveroo, Just Eat, Uber Eats | Food delivery & restaurants |
| **Fuel** | Shell, BP, Texaco | Gas stations |
| **Shopping** | Amazon, eBay, M&S | General retail |
| **Travel** | Airlines, trains, hotels | Transportation & lodging |
| **Health & Beauty** | Boots, Superdrug, gyms | Pharmacy & wellness |

## How Auto-Categorization Works

When you create or edit an item, VoucherVault analyzes the merchant name and automatically assigns a category with a **confidence score** (0-100%).

### Confidence Score

- **90-100%:** Exact merchant match (e.g., "Tesco" → Groceries)
- **70-89%:** Strong pattern match (e.g., "Sainsbury's Nectar Card" → Groceries)
- **Below 70%:** Uncertain match; the category may not be accurate

## Editing Categories

If VoucherVault miscategorizes an item:

1. Open the item
2. Click **Edit**
3. Scroll to **Category**
4. Select the correct category from the dropdown
5. Save

Your manual category override is saved and used for spending analysis.

## Using Categories for Analysis

Categories power your spending analysis:

- **Dashboard:** See total balance by category
- **Budget Tracking:** Set spending limits per wallet and track by category
- **Reports:** Filter items by category to analyze spending patterns
- **Tags:** Create tags for subcategories (e.g., "Premium Streaming" within Entertainment)

## Category Limits

- Each item has only **one category** (most specific match wins)
- Categories are **read-only initially** (set on item creation)
- You can **override the category** anytime by editing the item
- Categories are **not shared** between users (each account has independent categorization)

## Tips

- **Review Miscategorizations:** Occasionally audit auto-categorized items to ensure accuracy
- **Use with Tags:** Combine categories with custom tags for more detailed tracking
- **Export by Category:** When exporting items, group them by category for accounting or reimbursement
- **Budget by Category:** Set category-level budgets in wallet settings to track family or team spending

