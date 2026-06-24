"""
Bank (WF Checking) Transaction Import Module
Extends import_novo.py to handle the WF checking account CSV export.
Format is identical to the old Novo CSV (Date, Description, Amount, Note, ...).

New patterns vs. original Novo importer:
  - "Travis Campbell" (no TRANSFER suffix) → Owner Distribution
  - "Taclaco STRIPE... SHOPIFY" → Shopify Payout
  - AMEX EPAYMENT → CC Payment (AmEx)
"""

import sqlite3
import sys
from pathlib import Path

# Re-use all parsing/import machinery from import_novo
from import_novo import (
    parse_novo_csv,
    import_novo_transactions,
    match_cc_payments_from_chase_data,
    classify_novo_transaction as _base_classify,
)


def classify_bank_transaction(date_str, description, amount):
    """
    Classify a WF checking account transaction.
    Calls the base Novo classifier first; overrides/extends for new patterns.
    """
    desc_upper = description.upper()

    # ── Owner Distribution (no TRANSFER keyword in description) ──────────
    # Bank exports show "Travis Campbell" for personal → business transfers
    if 'TRAVIS CAMPBELL' in desc_upper:
        return {
            'category': 'Owner Distribution',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Owner distribution / personal transfer',
        }

    # ── Shopify payout via Stripe (description contains SHOPIFY) ─────────
    if 'SHOPIFY' in desc_upper:
        return {
            'category': 'Shopify Payout',
            'subcategory': None,
            'cc_account': None,
            'skip_reason': None,
            'mca_split': None,
            'notes': 'Shopify/Stripe payout deposit',
        }

    # ── AmEx CC payment ───────────────────────────────────────────────────
    if 'AMEX EPAYMENT' in desc_upper or 'AMEX' in desc_upper:
        return {
            'category': 'CC Payment',
            'subcategory': 'AmEx',
            'cc_account': 'AmEx',
            'skip_reason': None,
            'mca_split': None,
            'notes': 'American Express autopay from bank',
        }

    # Delegate everything else to the base Novo classifier
    return _base_classify(date_str, description, amount)


def parse_bank_csv(file_path):
    """
    Parse WF checking account CSV using Novo parser, then re-classify
    with the extended classifier for patterns not in the original importer.
    """
    # parse_novo_csv already handles amount parsing, date parsing, and
    # calls classify_novo_transaction. We re-run classification here so
    # the extended rules apply without duplicating parsing logic.
    import pandas as pd

    df = pd.read_csv(file_path)
    df.columns = [c.strip() for c in df.columns]

    def parse_amount(val):
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace('$', '').replace(',', '').strip()
        return float(s)

    df['parsed_amount'] = df['Amount'].apply(parse_amount)
    df['parsed_date'] = pd.to_datetime(df['Date'], format='%m-%d-%Y')
    df['date_str'] = df['parsed_date'].dt.strftime('%Y-%m-%d')

    results = []
    for _, row in df.iterrows():
        cls = classify_bank_transaction(
            row['date_str'],
            row['Description'],
            row['parsed_amount'],
        )
        results.append({
            'transaction_date': row['date_str'],
            'description': row['Description'],
            'amount': row['parsed_amount'],
            'running_balance': None,
            'category': cls['category'],
            'subcategory': cls['subcategory'],
            'cc_account': cls['cc_account'],
            'skip_reason': cls['skip_reason'],
            'mca_principal': cls['mca_split']['principal'] if cls['mca_split'] else None,
            'mca_interest': cls['mca_split']['interest'] if cls['mca_split'] else None,
            'notes': cls['notes'],
        })

    return results


def import_bank_transactions(file_path, db_path, import_batch_id=None):
    """
    Full pipeline: parse → classify → import → match CC payments.
    Returns result dict.
    """
    transactions = parse_bank_csv(file_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    result = import_novo_transactions(conn, transactions, import_batch_id=import_batch_id)

    # Cross-reference CC payments against Chase transaction data
    cc_match = match_cc_payments_from_chase_data(conn)
    result['cc_matched'] = cc_match.get('matched', 0)
    result['cc_unmatched'] = cc_match.get('unmatched', 0)

    # Show any unclassified
    cursor = conn.cursor()
    cursor.execute("""
        SELECT transaction_date, description, amount
        FROM novo_transactions
        WHERE category = 'UNCLASSIFIED'
        AND import_batch_id = ?
        ORDER BY transaction_date
    """, (import_batch_id,))
    result['unclassified_detail'] = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return result


if __name__ == '__main__':
    from collections import Counter

    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'Activities_06-11-2026_05-35-53.csv'
    db_path = sys.argv[2] if len(sys.argv) > 2 else 'taclaco.db'

    print(f"Parsing bank CSV: {csv_path}")
    transactions = parse_bank_csv(csv_path)

    from collections import Counter
    cats = Counter(t['category'] for t in transactions)
    print("\nClassification Summary:")
    for cat, count in sorted(cats.items()):
        amounts = [t['amount'] for t in transactions if t['category'] == cat]
        print(f"  {cat:25s}: {count:3d} txns, total ${sum(amounts):>12,.2f}")

    unclassified = [t for t in transactions if t['category'] == 'UNCLASSIFIED']
    if unclassified:
        print(f"\n⚠ UNCLASSIFIED:")
        for t in unclassified:
            print(f"  {t['transaction_date']}  ${t['amount']:>10,.2f}  {t['description']}")

    print(f"\nImporting to {db_path}...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = import_novo_transactions(conn, transactions, import_batch_id='bank_manual')
    print(f"  Imported:          {result['imported']}")
    print(f"  Skipped (dupes):   {result['skipped_duplicates']}")
    print(f"  Unclassified:      {result['unclassified']}")
    cc = match_cc_payments_from_chase_data(conn)
    print(f"  CC matched:        {cc.get('matched', 0)}")
    conn.close()
