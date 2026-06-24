"""
American Express Transaction Import Module
Processes AmEx CSV exports and imports into the universal transactions table.

AmEx CSV columns:
    Date, Receipt, Description, Amount, Extended Details, Appears On Your Statement As,
    Address, City/State, Zip Code, Country, Reference, Category

Amount convention: positive = charge (expense), negative = credit/refund.
The transactions table uses: negative = expense, positive = income.
So we negate AmEx amounts on import.
"""

import json
import pandas as pd
from datetime import datetime
import database as db

# Source label used in the transactions table
SOURCE = 'AmEx'


# ---------------------------------------------------------------------------
# Merchant name cleanup
# ---------------------------------------------------------------------------

def clean_amex_merchant(raw_description):
    """Clean up raw AmEx description into a readable merchant name."""

    # Check for an existing merchant mapping first
    mapping = db.get_merchant_mapping(raw_description)
    if mapping:
        return mapping['clean_merchant_name']

    name = raw_description.strip()

    # Plastiq payments — extract payee name
    if name.upper().startswith('PLASTIQ'):
        # Format: "Plastiq = <Payee>  <City>  <State>"
        parts = name.split('=')
        if len(parts) > 1:
            payee = parts[1].strip().split()[0].title()
            return f"Plastiq → {payee}"

    # CA Franchise Tax Board
    if 'CAFRNCHISTXBRD' in name.upper():
        return 'CA Franchise Tax Board'

    # PayPal patterns
    if name.upper().startswith('PAYPAL'):
        tail = name.split('*')
        if len(tail) > 1:
            merchant = tail[1].strip().split()[0].title()
            return f'PayPal — {merchant}'

    # Credits / fee adjustments
    if 'CREDIT' in name.upper() or 'ADJUSTMENT' in name.upper():
        return 'AmEx Credit / Adjustment'

    # Fallback: title-case trimmed description
    return name.title()


# ---------------------------------------------------------------------------
# Smart auto-categorization
# ---------------------------------------------------------------------------

def get_amex_categorization(description: str, amex_category: str, amount: float) -> dict:
    """
    Return categorization dict matching the shape used by import_chase.py:
        category_type  : 'Expense' | 'Purchase' | 'Skip'
        suggested_category : Wave account name (or reason for skip)
        confidence     : 'high' | 'medium' | 'low'
        needs_review   : bool
    """
    desc_upper = description.upper()
    cat_upper  = (amex_category or '').upper()

    # ---- Credits / refunds ----
    if amount < 0:
        return dict(category_type='Skip', suggested_category='credit_or_refund',
                    confidence='high', needs_review=False)

    # ---- Plastiq → IndiPro (inventory purchase) ----
    if 'PLASTIQ' in desc_upper and 'INDIPRO' in desc_upper:
        return dict(category_type='Purchase', suggested_category='Trading cards - IndiPro',
                    confidence='high', needs_review=True)

    # ---- CA Franchise Tax Board ----
    if 'CAFRNCHISTXBRD' in desc_upper:
        return dict(category_type='Expense', suggested_category='LLC Tax',
                    confidence='high', needs_review=False)

    # ---- PayPal professional services ----
    if 'PAYPAL' in desc_upper and 'PROFESSIONAL' in cat_upper:
        return dict(category_type='Expense', suggested_category='Contractor Costs',
                    confidence='medium', needs_review=True)

    if 'PAYPAL' in desc_upper:
        return dict(category_type='Purchase', suggested_category='Trading cards - Unknown',
                    confidence='low', needs_review=True)

    # ---- Generic government services ----
    if 'GOVERNMENT' in cat_upper:
        return dict(category_type='Expense', suggested_category='LLC Tax',
                    confidence='medium', needs_review=True)

    # ---- Business services ----
    if 'BUSINESS SERVICES' in cat_upper:
        return dict(category_type='Expense', suggested_category='Contractor Costs',
                    confidence='medium', needs_review=True)

    # ---- Fees & adjustments ----
    if 'FEES' in cat_upper or 'ADJUSTMENT' in cat_upper:
        return dict(category_type='Skip', suggested_category='credit_or_refund',
                    confidence='medium', needs_review=False)

    # ---- Fallback ----
    return dict(category_type='Expense', suggested_category='Other Business Expenses',
                confidence='low', needs_review=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_amex_csv(file_path: str) -> pd.DataFrame:
    """
    Parse an AmEx CSV export.
    Returns a DataFrame with cleaned + categorized columns added.
    """
    df = pd.read_csv(file_path)

    required = ['Date', 'Description', 'Amount', 'Category', 'Reference']
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"AmEx CSV missing columns: {missing}")

    df['Date'] = pd.to_datetime(df['Date'], format='%m/%d/%Y')
    df['Clean Merchant'] = df['Description'].apply(clean_amex_merchant)

    # Apply categorization row-by-row
    cats = df.apply(
        lambda r: get_amex_categorization(r['Description'], r.get('Category', ''), r['Amount']),
        axis=1
    )
    df['Category Type']        = cats.apply(lambda x: x['category_type'])
    df['Suggested Category']   = cats.apply(lambda x: x['suggested_category'])
    df['Confidence']           = cats.apply(lambda x: x['confidence'])
    df['Needs Review']         = cats.apply(lambda x: x['needs_review'])

    return df


def import_amex_transactions(df: pd.DataFrame, import_batch_id) -> dict:
    """
    Import parsed AmEx transactions into the universal transactions table.

    Status logic:
      - Credits / fee adjustments             → 'Skipped'
      - High-confidence Expense, no review    → 'Categorized'
      - Everything else (Purchases, low conf) → 'Pending'

    Returns dict with import counts.
    """
    imported_pending   = 0
    auto_categorized   = 0
    skipped            = 0
    duplicate_skipped  = 0

    for _, row in df.iterrows():
        category_type    = row['Category Type']
        confidence       = row['Confidence']
        needs_review     = row['Needs Review']
        suggested_cat    = row['Suggested Category']
        raw_amount       = float(row['Amount'])
        # Negate: AmEx positive = expense → store as negative
        amount           = -raw_amount
        transaction_date = row['Date'].date()
        description      = str(row['Description']).strip()
        reference        = str(row.get('Reference', '')).strip().strip("'")
        amex_category    = str(row.get('Category', '')).strip()

        # ---- Skip credits and adjustments ----
        if category_type == 'Skip':
            db.add_transaction(
                source=SOURCE,
                transaction_date=transaction_date,
                merchant_name=row['Clean Merchant'],
                description=description,
                amount=amount,
                category=suggested_cat,
                notes=f'AmEx ref: {reference}',
                source_data=json.dumps({'amex_category': amex_category, 'reference': reference}),
                import_batch_id=import_batch_id,
                import_method='CSV',
                status='Skipped',
            )
            skipped += 1
            continue

        # ---- Duplicate detection (by Reference number) ----
        if reference:
            if db.check_transaction_exists(SOURCE, str(transaction_date), description, amount):
                duplicate_skipped += 1
                continue

        # ---- Determine status ----
        if confidence == 'high' and category_type == 'Expense' and not needs_review:
            status = 'Categorized'
            auto_categorized += 1
        else:
            status = 'Pending'
            imported_pending += 1

        db.add_transaction(
            source=SOURCE,
            transaction_date=transaction_date,
            merchant_name=row['Clean Merchant'],
            description=description,
            amount=amount,
            category=suggested_cat if status == 'Categorized' else None,
            notes=f'AmEx ref: {reference}',
            source_data=json.dumps({'amex_category': amex_category, 'reference': reference}),
            import_batch_id=import_batch_id,
            import_method='CSV',
            status=status,
        )

    return {
        'imported': imported_pending + auto_categorized,
        'auto_categorized': auto_categorized,
        'pending_review': imported_pending,
        'skipped': skipped,
        'duplicate_skipped': duplicate_skipped,
        'total_processed': imported_pending + auto_categorized + skipped + duplicate_skipped,
    }
