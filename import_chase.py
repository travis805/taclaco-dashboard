"""
Chase Bank Transaction Import Module
Processes Chase credit card CSV exports and categorizes as purchases or expenses
"""

import pandas as pd
import re
from datetime import datetime
import database as db

def clean_merchant_name(raw_name):
    """
    Clean up merchant names for better readability and matching
    Examples:
    - "SP INDIPRO GAMES" Ã¢â€ â€™ "IndiPro Games"
    - "AMAZON MKTPL*NK6ND79K2" Ã¢â€ â€™ "Amazon Marketplace"
    - "CLAUDE.AI SUBSCRIPTION" Ã¢â€ â€™ "Claude AI"
    """
    
    # Check for existing mapping first
    mapping = db.get_merchant_mapping(raw_name)
    if mapping:
        return mapping['clean_merchant_name']
    
    # Clean up common patterns
    name = raw_name.strip()
    
    # Remove "SP " prefix (Stripe/Square/etc payment processor codes)
    if name.startswith("SP "):
        name = name[3:]
    
    # Common merchant patterns
    replacements = {
        "INDIPRO GAMES": "IndiPro Games",
        "LIFETCG.COM": "LIFE TCG",
        "ELEMENTAL CARDS": "Elemental Cards",
        "REFORMED POKE LAB": "Reformed Poke Lab",
        "ALLPOKETCG": "AllPoke TCG",
        "STOMPING GROUNDS": "Stomping Grounds",
        "GATORDEALSTCG": "Gator Deals TCG",
        "TCGVAULT22": "TCG Vault",
        "TAG GRADING": "TAG Grading",
        "BPMTRADINGSUPPLIES": "BPM Trading Supplies",
        "CLAUDE.AI": "Claude AI",
        "LINK MY BOOKS": "Link My Books",
        "SELLERISE INC": "Sellerise",
        "XERO US": "Xero",
        "TCG AUTOMATE": "TCG Automate",
        "DESCRIPT": "Descript",
        "ETSY HUNT": "Etsy Hunt",
        "PIRATE SHIP": "Pirate Ship",
        "MERCARI": "Mercari",
        "PAYPAL *EBAYINCSHIP": "eBay Shipping Labels",
        "PAYPAL *POKEMSTCNTR": "Pokemon Store Center",
        "PAYPAL *KICKZNKARDZ": "Kickz N Kardz",
        "CERTIFIED COLLECTIBLES": "Certified Collectibles",
        "CERTIFIED TRADING CARD": "Certified Trading Card",
    }
    
    for pattern, replacement in replacements.items():
        if pattern in name.upper():
            return replacement
    
    # Amazon patterns
    if name.startswith("AMAZON MKTPL"):
        return "Amazon Marketplace"
    if name.startswith("AMZ*Amazon"):
        return "Amazon Payments"
    if name.startswith("AMAZON.COM"):
        return "Amazon Direct"
    if name.startswith("AMZN Mktp"):
        return "Amazon Marketplace"
    
    # eBay patterns
    if name.startswith("eBay O*"):
        return "eBay Purchase"
    
    # Square patterns
    if "SQ *" in name:
        # Extract merchant name after SQ *
        parts = name.split("SQ *")
        if len(parts) > 1:
            return parts[1].strip().title()
    
    # PayPal patterns (keep generic for manual review)
    if name.startswith("PAYPAL *"):
        parts = name.split("PAYPAL *")
        if len(parts) > 1:
            return f"PayPal - {parts[1].strip().title()}"
    
    # Plan fees
    if name.startswith("PLAN FEE"):
        return "Plan Fee (Financing Charge)"
    
    # Interest charges
    if "INTEREST CHARGE" in name:
        return "Interest Charge"
    
    # If no pattern matched, return title case
    return name.title()

def get_smart_categorization(row):
    """
    Smart auto-categorization based on merchant patterns and analysis.
    Returns: dict with category info and confidence level.
    
    UPDATED 2026-03-09:
    - Added new merchants from Jan/Feb 2026 data (Whatnot, Anthropic, Shopify, PriceCharting)
    - Added 3 new SP distributors (1st Capital Gaming, Boardtopia, Envy Card Store)
    - Fixed TAG GRADING: Professional Fees -> Grading Fees (COGS)
    - Fixed NEXT INSUR: Insurance - Vehicles -> Business Insurance
    - Fixed PPCFARM: Advertising & Promotion -> Contractor Costs
    - Fixed CITY OF SLO/CAFRNCHISTXBRD: Professional Fees -> LLC Tax
    - Fixed AMZ*Amazon Payments: was Amazon Fees -> now Skip (Amazon payout, handled by JE generator)
    - Fixed SP INDIPRO GAMES: Trading cards - new product -> Trading cards - IndiPro
    - Account names now match Wave COA exactly (synced 2026-03-08)
    
    Confidence levels and import behavior:
      'high'   + Expense  = auto-import as status='Categorized'
      'high'   + Purchase = import as status='Pending' (needs purchase_id)
      'medium' = suggest category, status='Pending', flag for review
      'low'    = needs manual review, status='Pending'
    """
    description = row['Description']
    chase_category = row['Category']
    transaction_type = row['Type']
    amount = abs(float(row['Amount']))
    
    # Skip payments (not expenses or purchases)
    if transaction_type == 'Payment':
        return {
            'category_type': 'Skip',
            'suggested_category': 'Payment/Transfer',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Returns/refunds - skip
    if transaction_type == 'Return':
        return {
            'category_type': 'Skip',
            'suggested_category': 'Return/Refund',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Amazon Payouts - Skip (handled by JE generator via Amazon Reserved Balances)
    if description.startswith('AMZ*Amazon Payments'):
        return {
            'category_type': 'Skip',
            'suggested_category': 'Amazon Reserved Balances',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # =========================================================================
    # TIER 1: Exact/Substring Match Rules (High Confidence -> Auto-Categorize)
    # Expenses here import as status='Categorized' automatically.
    # =========================================================================
    
    # Software & Subscriptions -> Computer - Software
    if any(x in description for x in [
        'CLAUDE.AI', 'LINK MY BOOKS', 'SELLERISE', 'DESCRIPT', 'TCG AUTOMATE',
        'ETSY HUNT', 'PADDLE.NET* ERANK', 'ANTHROPIC', 'PRICECHARTING',
    ]):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Computer \u2013 Software',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Shopify subscription -> Computer - Software
    if description.startswith('SHOPIFY*'):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Computer \u2013 Software',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Accounting Software
    if 'XERO US' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Accounting Fees',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Shipping Labels - COGS (not operating expense)
    if description == 'PAYPAL *EBAYINCSHIP':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Shipping Charges',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Pirate Ship & USPS - COGS Shipping
    if 'PIRATE SHIP' in description or description.startswith('USPS PO'):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Shipping Charges',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # LLC Taxes & Government Fees (FIXED: was Professional Fees)
    if any(x in description for x in ['CITY OF SLO', 'CAFRNCHISTXBRD']):
        return {
            'category_type': 'Expense',
            'suggested_category': 'LLC Tax',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Business Insurance (FIXED: was Insurance - Vehicles)
    if 'NEXT INSUR' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Business Insurance',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Entertainment
    if 'Patreon*' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Meals and Entertainment',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Office Supplies (specific merchants)
    if description.startswith('STAPLES') or 'POKEMSTCNTR' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Office Supplies',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Contractors (FIXED: PPCFARM moved here from Advertising)
    if 'UPWORK' in description or 'PPCFARM' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Contractor Costs',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Plan Fees - Financing Charges
    if description.startswith('PLAN FEE'):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Novo Funding - Monthly Rate',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Interest Charges
    if 'INTEREST CHARGE' in description:
        return {
            'category_type': 'Expense',
            'suggested_category': 'Interest Expense',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Annual Membership Fee (Chase card fee)
    if description == 'ANNUAL MEMBERSHIP FEE':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Bank Service Charges',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # =========================================================================
    # TIER 2: Pattern Match Rules (High Confidence)
    # Expenses auto-Categorize; Purchases stay Pending (need purchase_id).
    # =========================================================================
    
    # Grading Companies -> Grading Fees (COGS) (FIXED: was Professional Fees)
    if any(x in description for x in ['TAG GRADING', 'CERTIFIED COLLECTIBLES', 'CERTIFIED TRADING CARD']):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Grading Fees',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # IndiPro Games -> dedicated COGS account (FIXED: was generic new product)
    if 'SP INDIPRO GAMES' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - IndiPro',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # "SP" Distributors - Purchases (New Product)
    # These are all known TCG distributors using Square payments
    sp_distributors = [
        'SP LIFETCG.COM',
        'SP ELEMENTAL CARDS',
        'SP REFORMED POKE LAB',
        'SP ALLPOKETCG',
        'SP STOMPING GROUNDS',
        'SP GATORDEALSTCG',
        'SP TCGVAULT22',
        'SP BPMTRADINGSUPPLIES',
        'SP 1ST CAPITAL GAMING',    # NEW: added from Jan/Feb 2026
        'SP BOARDTOPIA',            # NEW: added from Jan/Feb 2026
        'SP ENVY CARD STORE',       # NEW: added from Jan/Feb 2026
    ]
    
    for distributor in sp_distributors:
        if distributor in description:
            return {
                'category_type': 'Purchase',
                'suggested_category': 'Trading cards - new product',
                'needs_review': False,
                'confidence': 'high'
            }
    
    # Whatnot purchases -> Trading cards - collections (NEW)
    # Whatnot is a live auction platform; purchases are always card collections
    if 'PAYPAL *WHATNOT' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - collections',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Mercari - Collections
    if description.startswith('MERCARI*'):
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - collections',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Alibaba - Other Merchandise
    if 'Alibaba.com' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Other merchandise',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Square Purchases (known vendors)
    if 'SQ *SHUFFLE AND CUT' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': False,
            'confidence': 'high'
        }
    
    if 'SQ *MONEYLOVE' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - collections',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Specific PayPal merchants - New Product
    if 'PAYPAL *KICKZNKARDZ' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Best Buy - New Product (sealed product retail)
    if 'BESTBUYCOM' in description or 'BESTBUY.COM' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # GameStop - Other Merchandise
    if 'GAMESTOP' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Other merchandise',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # Cosmic Heroes - New Product
    if 'COSMIC HEROES' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': False,
            'confidence': 'high'
        }
    
    # =========================================================================
    # TIER 3: Needs Review (Medium Confidence)
    # Category is suggested but flagged for manual confirmation.
    # =========================================================================
    
    # PayPal - Manual (various item types, except known merchants above)
    if description.startswith('PAYPAL *') and description != 'PAYPAL *EBAYINCSHIP':
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - collections',
            'needs_review': True,
            'confidence': 'medium'
        }
    
    # Amazon.com purchases (4433 card = typically inventory/new product)
    if description.startswith('AMAZON.COM*'):
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': True,
            'confidence': 'medium'
        }
    
    # Amazon Marketplace (5742 card = typically supplies)
    if description.startswith('AMAZON MKTPL') or description.startswith('AMZN Mktp'):
        return {
            'category_type': 'Expense',
            'suggested_category': 'Shipping Supplies',
            'needs_review': True,
            'confidence': 'medium'
        }
    
    # TCGPlayer - could be various card types
    if 'TCGPLAYER.COM' in description:
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': True,
            'confidence': 'medium'
        }
    
    # eBay Direct Purchases - Manual (could be collections or new product)
    if description.startswith('eBay O*'):
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - collections',
            'needs_review': True,
            'confidence': 'medium'
        }
    
    # =========================================================================
    # TIER 4: Chase Category Fallback (Low Confidence -> Manual Review)
    # =========================================================================
    
    if chase_category == 'Merchandise & Inventory':
        return {
            'category_type': 'Purchase',
            'suggested_category': 'Trading cards - new product',
            'needs_review': True,
            'confidence': 'low'
        }
    
    if chase_category == 'Office & Shipping':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Office Supplies',
            'needs_review': True,
            'confidence': 'low'
        }
    
    if chase_category == 'Professional Services':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Professional Fees',
            'needs_review': True,
            'confidence': 'low'
        }
    
    if chase_category == 'Bills & Utilities':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Utilities',
            'needs_review': True,
            'confidence': 'low'
        }
    
    if chase_category == 'Fees & Adjustments':
        return {
            'category_type': 'Expense',
            'suggested_category': 'Bank Service Charges',
            'needs_review': True,
            'confidence': 'low'
        }
    
    # Default: Needs review
    return {
        'category_type': 'Expense',
        'suggested_category': 'Uncategorized Expense',
        'needs_review': True,
        'confidence': 'low'
    }

def categorize_transaction(row):
    """
    Wrapper for backward compatibility - calls get_smart_categorization
    """
    return get_smart_categorization(row)

def parse_chase_csv(file_path):
    """
    Parse Chase credit card CSV export with smart categorization
    Returns: DataFrame with cleaned and categorized transactions
    """
    
    # Read CSV
    df = pd.read_csv(file_path)
    
    # Expected columns
    expected_cols = ['Card', 'Transaction Date', 'Post Date', 'Description', 
                     'Category', 'Type', 'Amount', 'Memo']
    
    # Validate columns
    if not all(col in df.columns for col in expected_cols):
        raise ValueError(f"CSV missing required columns. Expected: {expected_cols}")
    
    # Convert dates
    df['Transaction Date'] = pd.to_datetime(df['Transaction Date'])
    df['Post Date'] = pd.to_datetime(df['Post Date'])
    
    # Clean merchant names
    df['Clean Merchant'] = df['Description'].apply(clean_merchant_name)
    
    # Smart auto-categorization
    df['Auto Category'] = df.apply(get_smart_categorization, axis=1)
    df['Category Type'] = df['Auto Category'].apply(lambda x: x['category_type'])
    df['Suggested Category'] = df['Auto Category'].apply(lambda x: x['suggested_category'])
    df['Needs Review'] = df['Auto Category'].apply(lambda x: x['needs_review'])
    df['Confidence'] = df['Auto Category'].apply(lambda x: x['confidence'])
    
    return df

def import_chase_transactions(df, import_batch_id):
    """
    Import Chase transactions into database.
    
    UPDATED 2026-03-09: High-confidence expense transactions now import as
    status='Categorized' automatically. Purchase transactions (COGS) always
    import as 'Pending' because they need a purchase_id assigned.
    
    Status logic:
      - Payments/Returns/Amazon Payouts -> 'Skipped'
      - High-confidence Expense (no purchase_id needed) -> 'Categorized'
      - Everything else (Purchases, medium/low confidence) -> 'Pending'
    
    Returns: dict with counts of imported, auto_categorized, skipped
    """
    
    imported_pending = 0
    auto_categorized = 0
    skipped_payments = 0
    skipped_returns = 0
    skipped_amazon_payouts = 0
    
    for _, row in df.iterrows():
        transaction_type = row['Type']
        auto_cat = row['Auto Category']
        category_type = auto_cat['category_type']
        confidence = auto_cat['confidence']
        needs_review = auto_cat['needs_review']
        suggested_category = auto_cat['suggested_category']
        
        # --- SKIPPED transactions ---
        if transaction_type == 'Payment':
            db.add_chase_transaction(
                card_last_four=str(row['Card']),
                transaction_date=row['Transaction Date'].date(),
                post_date=row['Post Date'].date(),
                description=row['Description'],
                clean_merchant_name=row['Clean Merchant'],
                chase_category=row['Category'],
                transaction_type=row['Type'],
                amount=float(row['Amount']),
                memo=row['Memo'] if pd.notna(row['Memo']) else '',
                import_batch_id=import_batch_id,
                status='Skipped',
                skip_reason='credit_card_payment'
            )
            skipped_payments += 1
            continue
        
        if transaction_type == 'Return':
            db.add_chase_transaction(
                card_last_four=str(row['Card']),
                transaction_date=row['Transaction Date'].date(),
                post_date=row['Post Date'].date(),
                description=row['Description'],
                clean_merchant_name=row['Clean Merchant'],
                chase_category=row['Category'],
                transaction_type=row['Type'],
                amount=float(row['Amount']),
                memo=row['Memo'] if pd.notna(row['Memo']) else '',
                import_batch_id=import_batch_id,
                status='Skipped',
                skip_reason='return_refund'
            )
            skipped_returns += 1
            continue
        
        if category_type == 'Skip' and suggested_category == 'Amazon Reserved Balances':
            db.add_chase_transaction(
                card_last_four=str(row['Card']),
                transaction_date=row['Transaction Date'].date(),
                post_date=row['Post Date'].date(),
                description=row['Description'],
                clean_merchant_name=row['Clean Merchant'],
                chase_category=row['Category'],
                transaction_type=row['Type'],
                amount=float(row['Amount']),
                memo=row['Memo'] if pd.notna(row['Memo']) else '',
                import_batch_id=import_batch_id,
                status='Skipped',
                skip_reason='amazon_payout'
            )
            skipped_amazon_payouts += 1
            continue
        
        # --- Determine import status ---
        # Auto-categorize if: high confidence + expense type + no review needed
        # Purchases always stay Pending because they need purchase_id assignment
        if (confidence == 'high' 
                and category_type == 'Expense' 
                and not needs_review):
            import_status = 'Categorized'
            auto_categorized += 1
        else:
            import_status = 'Pending'
            imported_pending += 1
        
        db.add_chase_transaction(
            card_last_four=str(row['Card']),
            transaction_date=row['Transaction Date'].date(),
            post_date=row['Post Date'].date(),
            description=row['Description'],
            clean_merchant_name=row['Clean Merchant'],
            chase_category=row['Category'],
            transaction_type=row['Type'],
            amount=float(row['Amount']),
            memo=row['Memo'] if pd.notna(row['Memo']) else '',
            import_batch_id=import_batch_id,
            status=import_status,
            skip_reason=None
        )
    
    return {
        'imported': imported_pending + auto_categorized,
        'auto_categorized': auto_categorized,
        'pending_review': imported_pending,
        'skipped_payments': skipped_payments,
        'skipped_returns': skipped_returns,
        'skipped_amazon_payouts': skipped_amazon_payouts,
        'total_processed': (imported_pending + auto_categorized + 
                           skipped_payments + skipped_returns + skipped_amazon_payouts)
    }


def suggest_purchase_id_from_date(transaction_date):
    """
    Suggest next available Purchase ID based on transaction date
    Format: YYMMXX (e.g., 251101, 251102, 251103)
    """
    # Extract YYMM from date
    year_month = transaction_date.strftime('%y%m')
    
    # Get next available ID for this month
    return db.get_next_purchase_id(year_month)

def detect_duplicate_transactions(df):
    """
    Check for potential duplicate transactions already in database
    Returns: DataFrame with duplicate info
    """
    
    # Get existing Chase transactions
    existing = db.get_all_chase_transactions()
    
    if existing.empty:
        return pd.DataFrame()
    
    # Check for matches on: date, description, amount
    duplicates = []
    
    for _, new_row in df.iterrows():
        matches = existing[
            (existing['transaction_date'] == new_row['Transaction Date'].date()) &
            (existing['description'] == new_row['Description']) &
            (existing['amount'] == new_row['Amount'])
        ]
        
        if not matches.empty:
            duplicates.append({
                'transaction_date': new_row['Transaction Date'],
                'description': new_row['Description'],
                'amount': new_row['Amount'],
                'existing_id': matches.iloc[0]['id']
            })
    
    return pd.DataFrame(duplicates)
