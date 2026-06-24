"""
PayPal / Venmo Transaction Import Module
Imports net PayPal activity into the transactions table (source='PayPal').

Rules:
  - SKIP: internal PayPal mechanics (Currency Conversion, Withdrawals, Holds, etc.)
  - SKIP: credit-card-funded clusters — any timestamp where a "General Card Deposit"
      exists. All rows at that second are CC-funded; skip the entire cluster.
  - SKIP: bank-funded purchases — any timestamp where a "Bank Deposit to PP Account"
      exists. The actual expense is already captured in the bank account feed.
  - IMPORT: all remaining transactions with real net economic impact:
      incoming payments (sales), balance-funded purchases, refunds given.
  - Foreign currency transactions: use the USD amount from the matched
      General Currency Conversion row (the USD outflow row).
  - Duplicate detection: by PayPal transaction_id stored in source_data JSON,
      and by date + merchant + amount as fallback.
"""

import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ── Row types that are pure internal PayPal mechanics ────────────────────────
# Matched with .strip() so trailing spaces in the CSV don't cause misses.
SKIP_TYPES = {
    'General Currency Conversion',
    'Bank Deposit to PP Account',
    'User Initiated Withdrawal',
    'General Account Hold',
    'Account Hold for Open Authorization',
    'Reversal of General Account Hold',
}

# ── Category classification ───────────────────────────────────────────────────
PURCHASE_TYPES = {
    'Mobile Payment',
    'General Payment',
    'PreApproved Payment Bill User Payment',
}


def _parse_amount(val):
    """Parse PayPal amount string like '1,234.56' or '-200.00' to float."""
    if isinstance(val, (int, float)):
        return float(val)
    return float(str(val).replace(',', '').strip())


def _parse_date(date_str, time_str):
    """Parse PayPal date/time to YYYY-MM-DD."""
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", '%m/%d/%Y %H:%M:%S')
        return dt.strftime('%Y-%m-%d'), dt
    except ValueError:
        return date_str, None


def _classify(name, tx_type, amount, item_title):
    """Return (category, merchant_name, notes) for a PayPal transaction."""
    if amount > 0:
        category = 'PayPal Sale'
        notes = item_title or f'PayPal payment received from {name}'
    else:
        # Negative — outgoing payment or refund given
        name_lower = (name or '').lower()
        item_lower = (item_title or '').lower()
        if 'whatnot' in name_lower:
            category = 'Whatnot Fee'
        elif 'unavailable' in item_lower or tx_type == 'Refund':
            category = 'Sales Refund'
        else:
            category = 'Trading cards - collections'
        notes = item_title or f'PayPal payment to {name}'

    return category, (name or 'Unknown').strip(), notes


def parse_paypal_csv(file_path):
    """
    Parse PayPal activity CSV and return a list of transaction dicts to import.

    Algorithm:
    1. Read all rows; normalize Type by stripping whitespace.
    2. Index rows by (Date, Time) timestamp.
    3. CC-funded detection: any timestamp that contains a 'General Card Deposit'
       row — skip ALL rows at that timestamp.
    4. Bank-funded detection: any timestamp that contains a 'Bank Deposit to PP
       Account' row — skip ALL rows at that timestamp (the bank feed captures it).
    5. Build USD amount map for foreign-currency transactions using matching
       'General Currency Conversion' USD rows.
    6. Emit importable records for all remaining non-skipped rows.
    """
    rows = []
    with open(file_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize Type — PayPal sometimes adds trailing spaces
            row['Type'] = row['Type'].strip()
            rows.append(row)

    # ── Step 1: index rows by timestamp ──────────────────────────────────
    by_timestamp = defaultdict(list)  # (Date, Time) → [row indices]
    for i, row in enumerate(rows):
        by_timestamp[(row['Date'], row['Time'])].append(i)

    # ── Step 2: find timestamps to skip entirely ──────────────────────────
    skip_timestamps = set()
    for row in rows:
        ts = (row['Date'], row['Time'])
        if row['Type'] in ('General Card Deposit', 'Bank Deposit to PP Account'):
            skip_timestamps.add(ts)

    # ── Step 3: build USD amount map for FX transactions ─────────────────
    # 'General Currency Conversion' USD row with negative amount = USD cost
    # Key: (Date, Time) → USD cost (absolute value)
    fx_usd_by_timestamp = {}
    for row in rows:
        if row['Type'] == 'General Currency Conversion' and row['Currency'] == 'USD':
            amt = _parse_amount(row['Amount'])
            if amt < 0:
                fx_usd_by_timestamp[(row['Date'], row['Time'])] = abs(amt)

    # ── Step 4: emit importable records ──────────────────────────────────
    results = []
    for row in rows:
        ts = (row['Date'], row['Time'])
        tx_type = row['Type']
        tx_id = row['Transaction ID']
        status = row['Status']
        currency = row['Currency']

        # Skip entire CC-funded or bank-funded timestamp clusters
        if ts in skip_timestamps:
            continue

        # Skip remaining internal mechanics rows (shouldn't be any left, but guard)
        if tx_type in SKIP_TYPES:
            continue

        amt = _parse_amount(row['Amount'])

        # For foreign-currency outgoing payments, substitute USD equivalent
        if currency != 'USD' and amt < 0:
            usd_amount = fx_usd_by_timestamp.get(ts)
            if usd_amount:
                amt = -usd_amount

        # Skip zero-amount rows
        if amt == 0:
            continue

        date_str, _ = _parse_date(row['Date'], row['Time'])
        name = row.get('Name', '').strip()
        item_title = row.get('Item Title', '').strip()
        category, merchant_name, notes = _classify(name, tx_type, amt, item_title)

        results.append({
            'source': 'PayPal',
            'transaction_date': date_str,
            'merchant_name': merchant_name,
            'description': item_title or f"{tx_type} — {name}",
            'amount': amt,
            'category': category,
            'notes': notes,
            'transaction_id': tx_id,
            'source_data': json.dumps({
                'transaction_id': tx_id,
                'type': tx_type,
                'status': status,
                'currency': currency,
                'name': name,
                'item_title': item_title,
            }),
        })

    return results


def import_paypal_transactions(conn, transactions, import_batch_id=None):
    """
    Insert PayPal transactions into the transactions table.
    Duplicate detection:
      1. By transaction_id stored in source_data JSON field.
      2. Fallback: date + merchant + amount (for manually-entered records).

    Returns result dict.
    """
    cursor = conn.cursor()

    # Build set of already-imported PayPal transaction IDs
    cursor.execute("""
        SELECT source_data FROM transactions
        WHERE source = 'PayPal' AND source_data IS NOT NULL
    """)
    existing_ids = set()
    for (sd,) in cursor.fetchall():
        try:
            d = json.loads(sd)
            tid = d.get('transaction_id')
            if tid:
                existing_ids.add(tid)
        except (json.JSONDecodeError, TypeError):
            pass

    imported = 0
    skipped_dupes = 0

    for txn in transactions:
        tx_id = txn['transaction_id']

        # Primary dupe check: transaction ID
        if tx_id and tx_id in existing_ids:
            skipped_dupes += 1
            continue

        # Fallback dupe check: date + amount + merchant (case-insensitive)
        cursor.execute("""
            SELECT transaction_id FROM transactions
            WHERE source = 'PayPal'
            AND transaction_date = ?
            AND ABS(amount - ?) < 0.02
            AND LOWER(merchant_name) = LOWER(?)
        """, (txn['transaction_date'], txn['amount'], txn['merchant_name']))
        if cursor.fetchone():
            skipped_dupes += 1
            continue

        cursor.execute("""
            INSERT INTO transactions
            (source, transaction_date, merchant_name, description, amount,
             category, notes, source_data, import_batch_id, import_method,
             import_date, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'import_paypal', CURRENT_TIMESTAMP, 'Pending')
        """, (
            txn['source'],
            txn['transaction_date'],
            txn['merchant_name'],
            txn['description'],
            txn['amount'],
            txn['category'],
            txn['notes'],
            txn['source_data'],
            import_batch_id,
        ))

        existing_ids.add(tx_id)
        imported += 1

    conn.commit()

    return {
        'imported': imported,
        'skipped_duplicates': skipped_dupes,
        'total_processed': imported + skipped_dupes,
    }


# ── Convenience function called from run_monthly_import.py ───────────────────

def import_paypal_activity(file_path, db_path=None, import_batch_id=None):
    """Full pipeline: parse -> import. Returns result dict."""
    transactions = parse_paypal_csv(file_path)
    import database as _db
    conn = _db.get_connection()
    result = import_paypal_transactions(conn, transactions, import_batch_id=import_batch_id)
    conn.close()
    result['sales'] = sum(1 for t in transactions if t['amount'] > 0)
    result['purchases'] = sum(1 for t in transactions if t['amount'] < 0)
    return result


# ── Standalone execution ──────────────────────────────────────────────────────

if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'Download (1).CSV'
    db_path = sys.argv[2] if len(sys.argv) > 2 else 'taclaco.db'

    print(f"Parsing PayPal CSV: {csv_path}")
    transactions = parse_paypal_csv(csv_path)

    sales = [t for t in transactions if t['amount'] > 0]
    purchases = [t for t in transactions if t['amount'] < 0]

    print(f"\nSales ({len(sales)}):")
    for t in sorted(sales, key=lambda x: x['transaction_date']):
        print(f"  {t['transaction_date']}  +${t['amount']:>8,.2f}  {t['merchant_name']:25s}  {t['description'][:40]}")

    print(f"\nPurchases ({len(purchases)}):")
    for t in sorted(purchases, key=lambda x: x['transaction_date']):
        print(f"  {t['transaction_date']}  -${abs(t['amount']):>8,.2f}  {t['merchant_name']:25s}  {t['description'][:40]}")

    print(f"\nImporting to {db_path}...")
    import database as _db
    conn = _db.get_connection()
    result = import_paypal_transactions(conn, transactions, import_batch_id='paypal_manual')
    print(f"  Imported:        {result['imported']}")
    print(f"  Skipped (dupes): {result['skipped_duplicates']}")
    conn.close()
