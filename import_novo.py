"""
Novo Bank Transaction Import Module
Processes Novo checking account CSV exports, auto-classifies transactions,
and stores them for journal entry generation.

Categories:
  - eBay Payout: eBay settlement deposits
  - Stripe Payout: Stripe transfer deposits
  - Amazon Payout: Amazon marketplace deposits
  - CC Payment: Credit card payments (matched to specific card)
  - Loan Repayment: Novo Funding MCA payments (split principal/interest)
  - Owner Contribution: Personal transfers into business account
  - PayPal Transfer: PayPal balance transfers (in or out)
  - Venmo Transfer: Venmo cashout deposits
  - Mercari Payout: Mercari settlement deposits
  - SKIP: Micro-deposits, verification transactions
"""

import pandas as pd
import re
from datetime import datetime

# ── MCA Split Lookup (hardcoded per handoff doc) ─────────────────────────────
# Three payments total, fully paid off by March 2025
MCA_SPLITS = {
    '2025-01-13': {
        'total': 532.50,
        'mca_1051_principal': 178.84,
        'mca_1051_interest': 132.00,
        'mca_5685_principal': 166.66,
        'mca_5685_interest': 55.00,
    },
    '2025-02-13': {
        'total': 753.66,
        'mca_1051_principal': 400.00,
        'mca_1051_interest': 132.00,
        'mca_5685_principal': 166.66,
        'mca_5685_interest': 55.00,
    },
    '2025-03-13': {
        'total': 221.70,
        'mca_1051_principal': 0.00,
        'mca_1051_interest': 0.00,
        'mca_5685_principal': 166.70,
        'mca_5685_interest': 55.00,
    },
}

# ── CC Payment Card Matching ─────────────────────────────────────────────────
# Rules to match Novo CC payment descriptions to specific Chase/WF cards
CC_MATCH_RULES = [
    # Wells Fargo - always "WF Credit Card AUTO PAY"
    {
        'pattern': r'WF Credit Card',
        'account': 'Wells Fargo CC',
        'card_label': 'Wells Fargo',
    },
    # Chase autopay of exactly $40 on 14th-15th = Chase 4051 minimum
    # Chase autopay of $126 on 14th = Chase 4051
    # Larger EPAY amounts = other cards (need amount-based logic)
    # For now, we classify all Chase payments and let the JE generator
    # handle card matching via the novo_transactions.cc_account field
]


def classify_novo_transaction(date_str, description, amount):
    """
    Classify a single Novo transaction based on description patterns.
    
    Returns dict with:
        category: str - the classification category
        subcategory: str|None - additional detail (e.g., card name for CC payments)
        cc_account: str|None - Wave liability account for CC payments
        skip_reason: str|None - reason if SKIP
        mca_split: dict|None - principal/interest split for MCA payments
        notes: str - auto-generated notes
    """
    desc_upper = description.upper()
    
    # ── SKIP: Verification micro-deposits ─────────────────────────────────
    if 'ACCTVERIFY' in desc_upper or 'VERIFYBANK' in desc_upper:
        return {
            'category': 'SKIP',
            'subcategory': 'Verification',
            'cc_account': None,
            'skip_reason': 'eBay/PayPal verification micro-deposit',
            'mca_split': None,
            'notes': 'Verification micro-deposit — excluded from JE',
        }
    
    # ── eBay Payout ───────────────────────────────────────────────────────
    if 'EBAY' in desc_upper and 'PAYMENT' in desc_upper:
        # Extract payout ID from description if present (P followed by digits)
        payout_match = re.search(r'P(\d{10})', description)
        payout_id = f"P{payout_match.group(1)}" if payout_match else None
        return {
            'category': 'eBay Payout',
            'subcategory': payout_id,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': f'eBay settlement deposit{" — " + payout_id if payout_id else ""}',
        }
    
    # ── Stripe Payout ─────────────────────────────────────────────────────
    if 'STRIPE TRANSFER' in desc_upper:
        # Extract transfer ID
        xfer_match = re.search(r'ST-([A-Z0-9]+)', description)
        xfer_id = xfer_match.group(0) if xfer_match else None
        return {
            'category': 'Stripe Payout',
            'subcategory': xfer_id,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': f'Stripe transfer deposit{" — " + xfer_id if xfer_id else ""}',
        }
    
    # ── Amazon Payout ─────────────────────────────────────────────────────
    if 'AMAZON' in desc_upper and ('PAYMENT' in desc_upper or 'MARKETPLACE' in desc_upper):
        return {
            'category': 'Amazon Payout',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Amazon marketplace settlement deposit',
        }
    
    # ── Mercari Payout ────────────────────────────────────────────────────
    if 'MERCARI' in desc_upper:
        return {
            'category': 'Mercari Payout',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Mercari settlement deposit',
        }
    
    # ── Novo Funding MCA ──────────────────────────────────────────────────
    if 'NOVO FUNDING' in desc_upper or 'MCA' in desc_upper:
        mca_split = MCA_SPLITS.get(date_str)
        if mca_split:
            principal = mca_split['mca_1051_principal'] + mca_split['mca_5685_principal']
            interest = mca_split['mca_1051_interest'] + mca_split['mca_5685_interest']
            return {
                'category': 'Loan Repayment',
                'subcategory': 'Novo Funding MCA',
                'cc_account': None,
                'skip_reason': None,
                'mca_split': {
                    'principal': round(principal, 2),
                    'interest': round(interest, 2),
                    'total': mca_split['total'],
                    'detail': mca_split,
                },
                'notes': f'MCA payment — principal ${principal:.2f}, interest ${interest:.2f}',
            }
        else:
            return {
                'category': 'Loan Repayment',
                'subcategory': 'Novo Funding MCA',
                'cc_account': None,
                'skip_reason': None,
                'mca_split': None,
                'notes': f'MCA payment — no split data for {date_str} (check MCA_SPLITS)',
            }
    
    # ── Wells Fargo CC Payment ────────────────────────────────────────────
    if 'WF CREDIT CARD' in desc_upper or 'WELLS FARGO' in desc_upper:
        return {
            'category': 'CC Payment',
            'subcategory': 'Wells Fargo CC',
            'cc_account': 'Wells Fargo CC',
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Wells Fargo CC auto-pay from Novo',
        }
    
    # ── Chase CC Payment ──────────────────────────────────────────────────
    if 'CHASE CREDIT CRD' in desc_upper:
        # Try to identify which card based on patterns
        cc_account = classify_chase_payment(date_str, amount, description)
        return {
            'category': 'CC Payment',
            'subcategory': cc_account,
            'cc_account': cc_account,
            'skip_reason': None,
            'mca_split': None,
            'notes': f'Chase CC payment to {cc_account}',
        }
    
    # ── Owner Contribution ────────────────────────────────────────────────
    # Travis Campbell TRANSFER_OUT or JPMorgan Chase Ext Trnsfr (personal → Novo)
    if ('TRAVIS CAMPBELL' in desc_upper and 'TRANSFER' in desc_upper) or \
       ('JPMORGAN CHASE' in desc_upper and 'TRNSFR' in desc_upper):
        return {
            'category': 'Owner Contribution',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Owner equity contribution to business',
        }
    
    # ── Vendor Payment: InDiPro (ACH from Novo) ──────────────────────────
    # Direct ACH payment to InDiPro Games — inventory purchase paid from bank
    if 'INDIPRO' in desc_upper:
        return {
            'category': 'Vendor Payment',
            'subcategory': 'Trading cards - IndiPro',
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'InDiPro ACH vendor payment — needs purchase_id',
        }
    
    # ── Vendor Payment: PayPal to individual (not a balance transfer) ─────
    # Pattern: "PAYPAL (PERSON NAME) (LOCATION)" — these are direct vendor
    # payments, NOT PayPal balance transfers (which say "PAYPAL TRANSFER" 
    # or "PAYPAL INSTANT TRANSFER").
    if 'PAYPAL (' in description and amount < 0:
        # Extract vendor name from between first set of parens
        name_match = re.search(r'PAYPAL \(([^)]+)\)', description)
        vendor_name = name_match.group(1) if name_match else 'Unknown vendor'
        return {
            'category': 'Vendor Payment',
            'subcategory': 'Trading cards - collections',  # Default; user can change
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': f'PayPal vendor payment to {vendor_name} — needs purchase_id',
        }
    
    # ── PayPal Transfer (inbound — deposit to Novo) ───────────────────────
    if 'PAYPAL TRANSFER' in desc_upper and amount > 0:
        return {
            'category': 'PayPal Transfer',
            'subcategory': 'inbound',
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'PayPal balance transfer to Novo',
        }
    
    # ── PayPal Transfer (outbound — from Novo to PayPal) ──────────────────
    if 'PAYPAL' in desc_upper and ('INSTANT TRANSFER' in desc_upper or 'INST XFER' in desc_upper):
        return {
            'category': 'PayPal Transfer',
            'subcategory': 'outbound',
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'PayPal instant transfer from Novo',
        }
    
    # ── Venmo Transfer ────────────────────────────────────────────────────
    if 'VENMO' in desc_upper:
        return {
            'category': 'Venmo Transfer',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Venmo cashout to Novo',
        }
    
    # ── Unclassified ──────────────────────────────────────────────────────
    return {
        'category': 'UNCLASSIFIED',
        'subcategory': None,
        'cc_account': None,
        'skip_reason': None,
        'mca_split': None,
        'notes': f'Needs manual review: {description}',
    }


def classify_chase_payment(date_str, amount, description):
    """
    Match a Chase CC payment to a specific card liability account.
    
    Uses a simple heuristic for initial classification.
    Call match_cc_payments_from_chase_data() after import for precise matching.
    """
    amt = abs(amount)
    desc_upper = description.upper()
    
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        day = dt.day
    except (ValueError, TypeError):
        day = 0
    
    # Small AUTOPAYBUS on 14th/15th is consistently the 4051 minimum
    if 'AUTOPAYBUS' in desc_upper and amt <= 48 and day in range(13, 16):
        return 'Ink other - 4051'
    
    return 'Chase CC - needs review'


# ── Source-to-Wave account mapping for Chase cards ────────────────────────────
CHASE_SOURCE_TO_WAVE = {
    'Chase-4051': 'Ink other - 4051',
    'Chase-4433': 'Ink Preferred - 4433',
    'Chase-5742': 'Ink Unlimited - 5742',
}


def match_cc_payments_from_chase_data(conn):
    """
    Cross-reference Novo CC payments against the Chase transactions table
    to determine which card each payment went to.
    
    Chase records CC payments with category='SKIP - Credit Card Payment' 
    and positive amounts (credits). Novo records them as negative (debits).
    Dates may differ by a few days (Novo = send date, Chase = post date).
    We match on amount (unique enough) within a +/- 7 day window.
    
    Returns: dict with counts of matched, already_matched, unmatched
    """
    cursor = conn.cursor()
    
    # Get all Novo CC payments that need matching
    cursor.execute("""
        SELECT id, transaction_date, amount, description, cc_account
        FROM novo_transactions
        WHERE category = 'CC Payment'
        AND (cc_account = 'Chase CC - needs review' OR cc_account IS NULL)
    """)
    unmatched = [dict(r) for r in cursor.fetchall()]
    
    if not unmatched:
        return {'matched': 0, 'already_matched': 0, 'unmatched': 0}
    
    # Get all Chase CC payment records
    cursor.execute("""
        SELECT source, transaction_date, amount
        FROM transactions
        WHERE category = 'SKIP - Credit Card Payment'
        AND amount > 0
    """)
    chase_payments = [dict(r) for r in cursor.fetchall()]
    
    matched = 0
    still_unmatched = 0
    
    for novo_txn in unmatched:
        novo_amt = abs(novo_txn['amount'])
        novo_date = datetime.strptime(novo_txn['transaction_date'], '%Y-%m-%d')
        
        # Find matching Chase payment: same amount, within 7 days
        best_match = None
        best_delta = 999
        
        for chase_txn in chase_payments:
            chase_amt = abs(chase_txn['amount'])
            chase_date = datetime.strptime(chase_txn['transaction_date'], '%Y-%m-%d')
            
            if abs(chase_amt - novo_amt) < 0.01:  # Amount matches
                delta = abs((novo_date - chase_date).days)
                if delta <= 7 and delta < best_delta:
                    best_match = chase_txn
                    best_delta = delta
        
        if best_match:
            wave_account = CHASE_SOURCE_TO_WAVE.get(best_match['source'], best_match['source'])
            cursor.execute("""
                UPDATE novo_transactions 
                SET cc_account = ?, subcategory = ?, notes = ?
                WHERE id = ?
            """, (
                wave_account,
                wave_account,
                f"Chase CC payment to {wave_account} (matched via amount ${novo_amt:.2f})",
                novo_txn['id']
            ))
            matched += 1
            # Remove from pool to prevent double-matching
            chase_payments.remove(best_match)
        else:
            still_unmatched += 1
    
    conn.commit()
    
    return {
        'matched': matched,
        'unmatched': still_unmatched,
    }


def parse_novo_csv(file_path):
    """
    Parse Novo bank CSV export and classify all transactions.
    
    Novo CSV format:
        Date, Description, Amount, Note, Check Number, Category
        
    Returns: list of dicts, one per transaction, with classification data
    """
    # Read CSV
    df = pd.read_csv(file_path)
    
    # Clean column names (Novo has a leading space on ' Category')
    df.columns = [c.strip() for c in df.columns]
    
    # Parse amounts — Novo uses "$1,234.56" format with possible negatives
    def parse_amount(val):
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace('$', '').replace(',', '').strip()
        return float(s)
    
    df['parsed_amount'] = df['Amount'].apply(parse_amount)
    
    # Parse dates — Novo uses MM-DD-YYYY format
    df['parsed_date'] = pd.to_datetime(df['Date'], format='%m-%d-%Y')
    df['date_str'] = df['parsed_date'].dt.strftime('%Y-%m-%d')
    
    # Classify each transaction
    results = []
    for _, row in df.iterrows():
        classification = classify_novo_transaction(
            row['date_str'], 
            row['Description'], 
            row['parsed_amount']
        )
        
        results.append({
            'transaction_date': row['date_str'],
            'description': row['Description'],
            'amount': row['parsed_amount'],
            'running_balance': None,  # Novo CSV doesn't always have this reliably
            'category': classification['category'],
            'subcategory': classification['subcategory'],
            'cc_account': classification['cc_account'],
            'skip_reason': classification['skip_reason'],
            'mca_principal': classification['mca_split']['principal'] if classification['mca_split'] else None,
            'mca_interest': classification['mca_split']['interest'] if classification['mca_split'] else None,
            'notes': classification['notes'],
        })
    
    return results


def create_novo_transactions_table(conn):
    """Create the novo_transactions table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS novo_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_date DATE NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT,
            cc_account TEXT,
            skip_reason TEXT,
            mca_principal REAL,
            mca_interest REAL,
            notes TEXT,
            import_batch_id INTEGER,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed INTEGER DEFAULT 0,
            review_notes TEXT
        )
    """)
    conn.commit()


def import_novo_transactions(conn, transactions, import_batch_id=None):
    """
    Import classified Novo transactions into the database.
    Checks for duplicates by date + description + amount.
    
    UPDATED 2026-03-09: Vendor Payment transactions route to the universal
    `transactions` table (like Chase imports) instead of `novo_transactions`.
    Cross-table duplicate check prevents double-counting if the expense was
    already entered manually via the Manual Entry tab.
    
    Returns: dict with counts
    """
    cursor = conn.cursor()
    
    # Ensure table exists
    create_novo_transactions_table(conn)
    
    imported = 0
    skipped_dupes = 0
    skipped_verify = 0
    unclassified = 0
    vendor_imported = 0
    vendor_skipped_dupes = 0
    
    for txn in transactions:
        # ── Vendor Payments → route to transactions table ─────────────
        if txn['category'] == 'Vendor Payment':
            # Check for duplicate in novo_transactions (re-import of same CSV)
            cursor.execute("""
                SELECT id FROM novo_transactions 
                WHERE transaction_date = ? AND description = ? AND amount = ?
            """, (txn['transaction_date'], txn['description'], txn['amount']))
            if cursor.fetchone():
                skipped_dupes += 1
                continue
            
            # Cross-table check: was this already entered manually in transactions?
            # Match on date + similar amount (manual entries use PayPal/Venmo source)
            cursor.execute("""
                SELECT transaction_id FROM transactions 
                WHERE transaction_date = ? 
                AND ABS(amount - ?) < 0.02
                AND source IN ('PayPal', 'Venmo', 'Cash', 'Zelle', 'Check', 'Other', 'Novo-Vendor')
            """, (txn['transaction_date'], txn['amount']))
            if cursor.fetchone():
                vendor_skipped_dupes += 1
                # Still record in novo_transactions for bank reconciliation,
                # but mark as SKIP so it doesn't generate a JE line
                cursor.execute("""
                    INSERT INTO novo_transactions 
                    (transaction_date, description, amount, category, subcategory, 
                     cc_account, skip_reason, mca_principal, mca_interest, notes, import_batch_id)
                    VALUES (?, ?, ?, 'SKIP', ?, NULL, 'duplicate_manual_entry', NULL, NULL, ?, ?)
                """, (
                    txn['transaction_date'], txn['description'], txn['amount'],
                    txn['subcategory'],
                    f"Skipped — matching manual entry found in transactions table. {txn['notes']}",
                    import_batch_id,
                ))
                continue
            
            # No duplicate found — insert into transactions table as Pending purchase
            cursor.execute("""
                INSERT INTO transactions 
                (source, transaction_date, merchant_name, description, amount, 
                 category, status, import_batch_id, import_method, notes)
                VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, 'Novo-Vendor', ?)
            """, (
                'Novo-Vendor',
                txn['transaction_date'],
                txn['description'].split('(')[0].strip(),  # Clean merchant name
                txn['description'],
                txn['amount'],  # Negative for expense
                txn['subcategory'],  # e.g., 'Trading cards - IndiPro'
                import_batch_id,
                txn['notes'],
            ))
            
            # Also record in novo_transactions for bank reconciliation
            cursor.execute("""
                INSERT INTO novo_transactions 
                (transaction_date, description, amount, category, subcategory, 
                 cc_account, skip_reason, mca_principal, mca_interest, notes, import_batch_id)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
            """, (
                txn['transaction_date'], txn['description'], txn['amount'],
                txn['category'], txn['subcategory'],
                txn['notes'], import_batch_id,
            ))
            vendor_imported += 1
            imported += 1
            continue
        
        # ── All other categories → novo_transactions as before ────────
        # Check for duplicate
        cursor.execute("""
            SELECT id FROM novo_transactions 
            WHERE transaction_date = ? AND description = ? AND amount = ?
        """, (txn['transaction_date'], txn['description'], txn['amount']))
        
        if cursor.fetchone():
            skipped_dupes += 1
            continue
        
        if txn['category'] == 'SKIP':
            skipped_verify += 1
            # Still store it, but flagged
        
        if txn['category'] == 'UNCLASSIFIED':
            unclassified += 1
        
        cursor.execute("""
            INSERT INTO novo_transactions 
            (transaction_date, description, amount, category, subcategory, 
             cc_account, skip_reason, mca_principal, mca_interest, notes, import_batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            txn['transaction_date'],
            txn['description'],
            txn['amount'],
            txn['category'],
            txn['subcategory'],
            txn['cc_account'],
            txn['skip_reason'],
            txn['mca_principal'],
            txn['mca_interest'],
            txn['notes'],
            import_batch_id,
        ))
        imported += 1
    
    conn.commit()
    
    return {
        'imported': imported,
        'skipped_duplicates': skipped_dupes,
        'skipped_verification': skipped_verify,
        'unclassified': unclassified,
        'vendor_imported': vendor_imported,
        'vendor_skipped_manual': vendor_skipped_dupes,
        'total_processed': imported + skipped_dupes + vendor_skipped_dupes,
    }


def get_novo_transactions_by_month(conn, year, month):
    """Get all Novo transactions for a given month, excluding SKIPs."""
    cursor = conn.cursor()
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"
    
    cursor.execute("""
        SELECT * FROM novo_transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        AND category != 'SKIP'
        ORDER BY transaction_date
    """, (start, end))
    
    return [dict(row) for row in cursor.fetchall()]


def get_novo_summary_by_month(conn, year, month):
    """
    Get summary of Novo transactions by category for a given month.
    Returns dict of category → total amount.
    """
    cursor = conn.cursor()
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"
    
    cursor.execute("""
        SELECT category, subcategory, cc_account,
               SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        AND category != 'SKIP'
        GROUP BY category, subcategory, cc_account
        ORDER BY category
    """, (start, end))
    
    return [dict(row) for row in cursor.fetchall()]


# ── Standalone execution for testing ──────────────────────────────────────────

if __name__ == '__main__':
    import sqlite3
    import sys
    
    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'Activities_02-10-2026_05-28-47.csv'
    db_path = sys.argv[2] if len(sys.argv) > 2 else 'taclaco.db'
    
    print(f"Parsing Novo CSV: {csv_path}")
    transactions = parse_novo_csv(csv_path)
    
    print(f"\nClassification Summary:")
    from collections import Counter
    cats = Counter(t['category'] for t in transactions)
    for cat, count in sorted(cats.items()):
        amounts = [t['amount'] for t in transactions if t['category'] == cat]
        print(f"  {cat:25s}: {count:3d} txns, total ${sum(amounts):>12,.2f}")
    
    print(f"\n{'─' * 60}")
    print(f"Total transactions: {len(transactions)}")
    
    # Show CC payment detail
    cc_payments = [t for t in transactions if t['category'] == 'CC Payment']
    if cc_payments:
        print(f"\nCC Payment Details:")
        for t in cc_payments:
            print(f"  {t['transaction_date']}  ${t['amount']:>10,.2f}  → {t['cc_account']}")
    
    # Show MCA detail
    mca_payments = [t for t in transactions if t['category'] == 'Loan Repayment']
    if mca_payments:
        print(f"\nMCA Payment Details:")
        for t in mca_payments:
            print(f"  {t['transaction_date']}  ${t['amount']:>10,.2f}  P=${t['mca_principal']:.2f}  I=${t['mca_interest']:.2f}")
    
    # Show unclassified
    unclassified = [t for t in transactions if t['category'] == 'UNCLASSIFIED']
    if unclassified:
        print(f"\n⚠ UNCLASSIFIED Transactions:")
        for t in unclassified:
            print(f"  {t['transaction_date']}  ${t['amount']:>10,.2f}  {t['description']}")
    
    # Import to DB
    print(f"\nImporting to {db_path}...")
    import database as _db
    conn = _db.get_connection()
    result = import_novo_transactions(conn, transactions, import_batch_id=1)
    print(f"  Imported: {result['imported']}")
    print(f"  Skipped (dupes): {result['skipped_duplicates']}")
    print(f"  Skipped (verify): {result['skipped_verification']}")
    print(f"  Unclassified: {result['unclassified']}")
    
    # Match CC payments against Chase data
    print(f"\nMatching CC payments against Chase transactions...")
    cc_result = match_cc_payments_from_chase_data(conn)
    print(f"  Matched: {cc_result['matched']}")
    print(f"  Unmatched: {cc_result['unmatched']}")
    
    # Validate January 2025
    print(f"\n{'═' * 60}")
    print(f"January 2025 Validation:")
    jan = get_novo_summary_by_month(conn, 2025, 1)
    for row in jan:
        sub = row.get('subcategory') or ''
        print(f"  {row['category']:25s} {sub:25s} ${row['total']:>10,.2f} ({row['cnt']} txns)")
    
    # Detailed Jan transactions
    jan_txns = get_novo_transactions_by_month(conn, 2025, 1)
    print(f"\nJanuary 2025 Detail ({len(jan_txns)} transactions):")
    total_in = 0
    total_out = 0
    for t in jan_txns:
        direction = '+' if t['amount'] > 0 else '-'
        print(f"  {t['transaction_date']}  {direction}${abs(t['amount']):>10,.2f}  {t['category']:25s}  {t['description'][:50]}")
        if t['amount'] > 0:
            total_in += t['amount']
        else:
            total_out += abs(t['amount'])
    
    print(f"\n  Inflows:  ${total_in:>10,.2f}")
    print(f"  Outflows: ${total_out:>10,.2f}")
    print(f"  Net:      ${total_in - total_out:>10,.2f}")
    print(f"  Expected: $415.71")
    
    conn.close()
