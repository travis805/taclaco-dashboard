"""
Monthly Journal Entry Generator for Wave Accounting
Generates summary journal entries from dashboard data for import via Wave Connect.

Sections:
  A: eBay Revenue (sale-date basis)
  B: eBay Fees & Costs (sale-date basis)
  C: eBay Refunds (informational — refunds flow through A+B via reversal lines)
  D: eBay Payouts to Novo (payout-date basis)
  E: Chase Ink Unlimited 5742 Expenses
  F: Chase Ink Preferred 4433 Expenses
  G: Chase Ink Other 4051 Expenses
  H: Novo → CC Payments
  I: Owner Contributions
  J: Novo Funding MCA (Jan–Mar 2025 only)
  K: Amazon Payouts (Link My Books offset)

Future sections (auto-included when data exists):
  L: Stripe Revenue (clearing account model, starts Nov 2025)
  M: Stripe Payouts to Novo
  N: PayPal/Venmo/Direct/Mercari Sales (no clearing — cash received)
  O: eBay Gift Card Spending (DR expense, CR Owner Investment)
  P: PayPal Transfers (Novo ↔ PayPal)
  Q: Venmo Transfers (Novo ↔ Venmo)
"""

import sqlite3
from datetime import datetime
from collections import defaultdict


def _fone(cursor):
    """
    Fetch one row and return as a dict, regardless of whether the connection
    is libSQL (returns plain tuples) or sqlite3 with row_factory (sqlite3.Row).
    """
    row = cursor.fetchone()
    if row is None:
        return None
    try:
        return dict(row)
    except TypeError:
        if cursor.description:
            return dict(zip([d[0] for d in cursor.description], row))
        return {}


def _fall(cursor):
    """
    Fetch all rows and return as a list of dicts.
    """
    rows = cursor.fetchall()
    if not rows:
        return []
    try:
        return [dict(r) for r in rows]
    except TypeError:
        if cursor.description:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]
        return [tuple(r) for r in rows]

# ── Wave Account Name Constants ───────────────────────────────────────────────
# These must match Wave COA exactly

# Assets
NOVO_BANK = 'Novo Bank'
EBAY_RESERVED = 'EBay Reserved Balances'  # Wave uses "EBay" not "eBay"
AMAZON_RESERVED = 'Amazon Reserved Balances'
STRIPE_RESERVED = 'Stripe Reserved Balances'  # Must be created in Wave

# Income — Channel-specific revenue accounts (UPDATED 2026-03-09)
EBAY_SALES = 'eBay Sales'                    # eBay product revenue (primary TCG channel)
AMAZON_SALES = 'Amazon Sales - Kitcoff'      # Amazon product revenue (Kitcoff inventory)
DIRECT_SALES = 'Direct Sales'                # Stripe/PayPal/Venmo direct sales
SHOPIFY_SALES = 'Shopify Sales'              # Shopify product revenue
SHIPPING_INCOME = 'Shipping Income'
CUSTOMER_REFUNDS = 'Customer refunds'

# Equity
OWNER_EQUITY = 'Owner Investment / Drawings'

# Liabilities
CC_ACCOUNTS = {
    'Chase-5742': 'Ink Unlimited - 5742',
    'Chase-4433': 'Ink Preferred - 4433',
    'Chase-4051': 'Ink other - 4051',
    'Wells Fargo CC': 'Wells Fargo CC',
    'Ink Unlimited - 5742': 'Ink Unlimited - 5742',
    'Ink Preferred - 4433': 'Ink Preferred - 4433',
    'Ink other - 4051': 'Ink other - 4051',
}
NOVO_FUNDING_LOC = 'Novo Funding LOC'
NOVO_FUNDING_RATE = 'Novo Funding - Monthly Rate'

# PayPal/Venmo asset account — Wave COA has "Paypal" (not "Paypal and Venmo")
PAYPAL_ASSET = 'Paypal'

# Expense: category names in transactions table already match Wave account names
# EXCEPT these which need to be created in Wave if they don't exist:
#   - Shipping Charges (COGS)
#   - Shipping Supplies (Operating)
#   - Trading cards - new product (COGS)
#   - Trading cards - collections (COGS)
#   - Grading Fees (COGS)
#   - Business Insurance (Operating)
#   - LLC Tax (Operating)
#   - Other merchandise (COGS)

# ── Wave ID Lookup ────────────────────────────────────────────────────────────
# Populated from Wave COA export. Used to generate Wave Connect CSV.
# Accounts not in this dict will get empty Wave Id (Wave Connect still works,
# it matches by Account Name).

WAVE_IDS = {
    # Assets
    'Novo Bank': '2365187973424731238',
    'EBay Reserved Balances': '2464786127198999783',
    'Amazon Reserved Balances': '2366200856971433802',
    'Stripe Reserved Balances': '2465212029930165743',
    'Paypal': '2365188423154783349',
    'Cash on Hand': '2365186195467655118',
    'Inventory - Kitcoff': '2365884162600195987',
    'Accounts Receivable': '2365186195559929808',
    # Liabilities
    'Ink Unlimited - 5742': '2365190021771810212',
    'Ink Preferred - 4433': '2365190120690275750',
    'Ink other - 4051': '2365195755863528901',
    'Wells Fargo CC': '2365190215263442353',
    'Novo Funding LOC': '2365912759155152274',
    'Accounts Payable': '2365186195618650066',
    # Income — Channel-specific (UPDATED 2026-03-09)
    'eBay Sales': '2365186195929028570',
    'Amazon Sales - Kitcoff': '2365186195996137436',
    'Direct Sales': '2484056675506574855',
    'Shopify Sales': '2484035755207415667',
    'Shipping Income': '2366201181878998883',
    'Customer refunds': '2366209877568771896',
    'Clearing - Amazon Settlement': '2366178555815122260',
    'Discounts & Promos': '2366201338427201391',
    # Equity
    'Owner Investment / Drawings': '2365186195761256406',
    # Expenses — Advertising (channel-specific, UPDATED 2026-03-09)
    'Advertising - Amazon': '2365186198965703690',
    'Advertising - Ebay sponsored': '2466987844489565391',
    'Advertising - Google': '2484038964697224200',
    # Expenses — Operating
    'Accounting Fees': '2365186198906983432',
    'Amazon Fees': '2365186199225750546',
    'Bank Service Charges': '2365186198168786930',
    'Business Insurance': '2465211663809369534',
    'Computer – Hardware': '2365186196331681766',
    'Computer – Hosting': '2365186196541396972',
    'Computer – Internet': '2365186196474288106',
    'Computer – Software': '2365186196407179240',
    'Contractor Costs': '2365186199150253072',
    'Depreciation Expense': '2365186198579828734',
    'Interest Expense': '2365186198244284404',
    'LLC Tax': '2465953321698055743',
    'Meals and Entertainment': '2365186198646936576',
    'Merchant Account Fees': '2365186196130355168',
    'Novo Funding - Monthly Rate': '2365913102853199264',
    'Office Supplies': '2365186198445611002',
    'Postage & Shipping': '2365186196600117230',
    'Professional Fees': '2365186199032812556',
    'Rent Expense': '2365186196205852642',
    'Repairs & Maintenance': '2365186196264572900',
    'Shipping Supplies': '2465211044981757297',
    'Shopify Payment Processing Fees': '2484037614894700526',
    'Virtual Mailbox': '2365186198378502136',
    'Duplicate transactions': '2365186199091532814',
    # Expenses — COGS
    'Amazon shipping costs': '2366202991494682588',
    'COGS - Kitcoff': '2366213701960324130',
    'Grading Fees': '2465211531604907434',
    'Other merchandise': '2465211869388985830',
    'Shipping Charges': '2465210774432372051',
    'Trading cards - collections': '2465211284526847390',
    'Trading cards - IndiPro': '2484036656487847799',
    'Trading cards - new product': '2465211184987624842',
}



def _month_range(year, month):
    """Return (start_date, end_exclusive_date) strings for a month."""
    start = f"{year}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{month + 1:02d}-01"
    return start, end


def _je_line(account, debit, credit, description):
    """Create a single journal entry line dict."""
    return {
        'account': account,
        'debit': round(debit, 2) if debit else 0.0,
        'credit': round(credit, 2) if credit else 0.0,
        'description': description,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def section_a_ebay_revenue(conn, year, month):
    """Section A: eBay Revenue Recognition (sale-date basis)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    
    c.execute("""
        SELECT 
            COUNT(*) as order_count,
            COALESCE(SUM(sale_price), 0) as total_sale_price,
            COALESCE(SUM(shipping_charged), 0) as total_shipping_charged
        FROM sales
        WHERE platform = 'eBay' AND sale_date >= ? AND sale_date < ?
    """, (start, end))
    
    r = _fone(c)
    count = r['order_count']
    sale_price = r['total_sale_price']
    shipping = r['total_shipping_charged']
    gross = round(sale_price + shipping, 2)
    
    if count == 0:
        return [], {'orders': 0, 'gross': 0, 'sale_price': 0, 'shipping': 0}
    
    lines = []
    lines.append(_je_line(EBAY_RESERVED, gross, 0,
        f"Jan eBay gross receivable ({count} orders)" if month == 1 else
        f"{datetime(year, month, 1).strftime('%b')} eBay gross receivable ({count} orders)"))
    
    mon = datetime(year, month, 1).strftime('%b')
    
    if sale_price != 0:
        lines.append(_je_line(EBAY_SALES, 0, round(sale_price, 2),
            f"{mon} eBay product revenue"))
    if shipping != 0:
        lines.append(_je_line(SHIPPING_INCOME, 0, round(shipping, 2),
            f"{mon} eBay shipping charged to buyers"))
    
    return lines, {'orders': count, 'gross': gross, 'sale_price': sale_price, 'shipping': shipping}


def section_b_ebay_fees(conn, year, month):
    """Section B: eBay Fees & Costs (sale-date basis)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT 
            COALESCE(SUM(platform_fees_fixed + platform_fees_variable), 0) as merchant_fees,
            COALESCE(SUM(promoted_listing_fee), 0) as promo_fees,
            COALESCE(SUM(shipping_cost), 0) as shipping_costs,
            COALESCE(SUM(platform_fees_fixed), 0) as fees_fixed,
            COALESCE(SUM(platform_fees_variable), 0) as fees_variable
        FROM sales
        WHERE platform = 'eBay' AND sale_date >= ? AND sale_date < ?
    """, (start, end))
    
    r = _fone(c)
    merchant_fees = round(r['merchant_fees'], 2)
    promo_fees = round(r['promo_fees'], 2)
    shipping_costs = round(r['shipping_costs'], 2)
    total_deductions = round(merchant_fees + promo_fees + shipping_costs, 2)
    
    if total_deductions == 0:
        return [], {'merchant_fees': 0, 'promo_fees': 0, 'shipping_costs': 0, 'total': 0}
    
    lines = []
    if merchant_fees != 0:
        lines.append(_je_line('Merchant Account Fees', abs(merchant_fees), 0,
            f"{mon} eBay fees (fixed ${abs(r['fees_fixed']):.2f} + variable ${abs(r['fees_variable']):.2f})"))
    if promo_fees != 0:
        lines.append(_je_line('Advertising - Ebay sponsored', abs(promo_fees), 0,
            f"{mon} eBay promoted listing fees"))
    if shipping_costs != 0:
        lines.append(_je_line('Shipping Charges', abs(shipping_costs), 0,
            f"{mon} eBay shipping costs (ESUS + carrier)"))
    
    lines.append(_je_line(EBAY_RESERVED, 0, abs(total_deductions),
        f"{mon} eBay fee/cost deductions"))
    
    return lines, {
        'merchant_fees': merchant_fees,
        'promo_fees': promo_fees,
        'shipping_costs': shipping_costs,
        'total': total_deductions,
    }


def section_b2_ebay_other_fees(conn, year, month):
    """
    Section B2: eBay Non-Sale Fees (payout-date basis).
    
    These are fees deducted from eBay payouts that are NOT tied to individual sales:
      - Store Subscription Fee -> Computer - Software
      - Claims/Disputes -> Merchant Account Fees
      - Other (Special Duration Fee, adjustments, etc.) -> Merchant Account Fees
    
    Note: Promoted Listings General fees are EXCLUDED here because they are the
    same charges as sales.promoted_listing_fee (Section B), just on payout-date
    basis. Including them would double-count.
    
    Source: ebay_transactions WHERE type IN ('Other fee', 'Adjustment', 'Claim'),
            keyed by payout_date (when eBay deducts them from payout).
            Uses COALESCE(payout_date, transaction_date) for API-synced rows.
    """
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT type, description, SUM(net_amount) as total, COUNT(*) as cnt
        FROM ebay_transactions
        WHERE type IN ('Other fee', 'Adjustment', 'Claim')
        AND description NOT LIKE '%Promoted Listing%'
        AND COALESCE(payout_date, transaction_date) >= ? 
        AND COALESCE(payout_date, transaction_date) < ?
        GROUP BY type, description
        ORDER BY SUM(net_amount)
    """, (start, end))
    
    rows = _fall(c)
    if not rows:
        return [], {'total': 0}
    
    # Classify each fee type to a Wave expense account
    subscription_total = 0
    claims_total = 0
    other_total = 0
    detail = {}
    
    for row in rows:
        tx_type = row['type'] or ''
        desc = row['description'] or ''
        amount = round(row['total'], 2)  # negative = expense
        cnt = row['cnt']
        
        if 'Subscription' in desc or 'Store' in desc:
            subscription_total += amount
            detail[desc] = {'amount': amount, 'count': cnt, 'account': 'Computer - Software'}
        elif tx_type == 'Claim':
            claims_total += amount
            detail[f"Claim: {desc}"] = {'amount': amount, 'count': cnt, 'account': 'Merchant Account Fees'}
        else:
            other_total += amount
            detail[desc] = {'amount': amount, 'count': cnt, 'account': 'Merchant Account Fees'}
    
    lines = []
    total_expense = 0
    
    if subscription_total != 0:
        amt = abs(round(subscription_total, 2))
        lines.append(_je_line('Computer - Software', amt, 0,
            f"{mon} eBay store subscription"))
        total_expense += amt
    
    if claims_total != 0:
        amt = abs(round(claims_total, 2))
        lines.append(_je_line('Merchant Account Fees', amt, 0,
            f"{mon} eBay claims/disputes"))
        total_expense += amt
    
    if other_total != 0:
        amt = abs(round(other_total, 2))
        lines.append(_je_line('Merchant Account Fees', amt, 0,
            f"{mon} eBay other fees/adjustments"))
        total_expense += amt
    
    total_expense = round(total_expense, 2)
    
    if total_expense > 0:
        lines.append(_je_line(EBAY_RESERVED, 0, total_expense,
            f"{mon} eBay non-sale fee deductions"))
    
    return lines, {
        'total': total_expense,
        'subscription': abs(subscription_total),
        'claims': abs(claims_total),
        'other': abs(other_total),
        'detail': detail,
    }


def section_c_ebay_refunds(conn, year, month):
    """
    Section C: eBay Refunds (informational).
    
    With reversal-line design, refunds automatically flow through Sections A+B
    as negative revenue and fee credits. This section is informational only —
    it reports refund activity but generates NO journal entry lines.
    """
    start, end = _month_range(year, month)
    c = conn.cursor()
    
    c.execute("""
        SELECT COUNT(*) as cnt, 
               COALESCE(SUM(sale_price), 0) as refund_revenue,
               COALESCE(SUM(shipping_charged), 0) as refund_shipping
        FROM sales
        WHERE platform = 'eBay' AND sale_date >= ? AND sale_date < ?
        AND item_title LIKE 'REFUND:%'
    """, (start, end))
    
    r = _fone(c)
    count = r['cnt']
    refund_revenue = r['refund_revenue']  # Negative
    refund_shipping = r['refund_shipping']  # Negative
    
    return [], {
        'count': count,
        'refund_revenue': refund_revenue,
        'refund_shipping': refund_shipping,
        'note': 'Refunds flow through Sections A+B via reversal lines (no separate JE lines needed)'
            if count > 0 else 'No refunds this month',
    }


def section_d_ebay_payouts(conn, year, month):
    """Section D: eBay Payouts to Novo (payout-date basis)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt,
               GROUP_CONCAT(transaction_date || ' $' || PRINTF('%.2f', amount), ', ') as detail
        FROM novo_transactions
        WHERE category = 'eBay Payout'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    count = r['cnt'] or 0
    
    if total == 0:
        return [], {'total': 0, 'count': 0}
    
    lines = []
    # Build deposit detail for description
    c.execute("""
        SELECT transaction_date, amount FROM novo_transactions
        WHERE category = 'eBay Payout'
        AND transaction_date >= ? AND transaction_date < ?
        ORDER BY transaction_date
    """, (start, end))
    deposits = _fall(c)
    deposit_detail = ' + '.join(f"${r['amount']:.2f} on {r['transaction_date'][5:]}" for r in deposits)
    
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} eBay deposits ({deposit_detail})"))
    lines.append(_je_line(EBAY_RESERVED, 0, total,
        f"{mon} eBay payout clearing"))
    
    return lines, {'total': total, 'count': count}


def _section_chase_expenses(conn, year, month, source, section_label, liability_account):
    """Generic Chase CC expense section (E, F, or G)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    # Get expenses grouped by category, excluding SKIP
    c.execute("""
        SELECT category, SUM(amount) as net_amount, COUNT(*) as cnt
        FROM transactions
        WHERE source = ? AND transaction_date >= ? AND transaction_date < ?
        AND category NOT LIKE 'SKIP%'
        AND category IS NOT NULL
        GROUP BY category
        HAVING SUM(amount) != 0
        ORDER BY ABS(SUM(amount)) DESC
    """, (source, start, end))
    
    rows = _fall(c)
    
    if not rows:
        return [], {'total': 0, 'categories': {}}
    
    lines = []
    total = 0
    categories = {}
    
    for row in rows:
        # Amounts in transactions table: negative = expense, positive = refund
        # Net amount for JE: take absolute value (it's a debit to expense)
        net = round(row['net_amount'], 2)
        if net == 0:
            continue
        
        expense_amount = abs(net)
        categories[row['category']] = {'amount': expense_amount, 'count': row['cnt']}
        total += expense_amount
        
        lines.append(_je_line(row['category'], expense_amount, 0,
            f"{mon} {row['category'].lower()} ({row['cnt']} txn{'s' if row['cnt'] > 1 else ''})"))
    
    total = round(total, 2)
    
    if total > 0:
        lines.append(_je_line(liability_account, 0, total,
            f"{mon} {section_label} total expenses"))
    
    return lines, {'total': total, 'categories': categories}


def section_e_chase_5742(conn, year, month):
    return _section_chase_expenses(conn, year, month, 'Chase-5742', 'Chase 5742', 'Ink Unlimited - 5742')

def section_f_chase_4433(conn, year, month):
    return _section_chase_expenses(conn, year, month, 'Chase-4433', 'Chase 4433', 'Ink Preferred - 4433')

def section_g_chase_4051(conn, year, month):
    return _section_chase_expenses(conn, year, month, 'Chase-4051', 'Chase 4051', 'Ink other - 4051')


def section_h_cc_payments(conn, year, month):
    """Section H: Novo → CC Payments (balance sheet only)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT cc_account, SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'CC Payment'
        AND transaction_date >= ? AND transaction_date < ?
        AND cc_account IS NOT NULL AND cc_account != 'Chase CC - needs review'
        GROUP BY cc_account
        ORDER BY ABS(SUM(amount)) DESC
    """, (start, end))
    
    rows = _fall(c)
    
    # Also check for unresolved payments
    c.execute("""
        SELECT COUNT(*) as cnt FROM novo_transactions
        WHERE category = 'CC Payment' AND cc_account = 'Chase CC - needs review'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    unresolved = _fone(c)['cnt']
    
    if not rows and unresolved == 0:
        return [], {'total': 0, 'by_card': {}, 'unresolved': 0}
    
    lines = []
    total_out = 0
    by_card = {}
    
    for row in rows:
        amount = abs(round(row['total'], 2))
        account = row['cc_account']
        by_card[account] = amount
        total_out += amount
        
        lines.append(_je_line(account, amount, 0,
            f"{mon} CC payment" + (f" ({row['cnt']} payments)" if row['cnt'] > 1 else "")))
    
    total_out = round(total_out, 2)
    
    if total_out > 0:
        lines.append(_je_line(NOVO_BANK, 0, total_out,
            f"{mon} total CC payments from Novo"))
    
    return lines, {'total': total_out, 'by_card': by_card, 'unresolved': unresolved}


def section_i_owner_contributions(conn, year, month):
    """Section I: Owner Contributions"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt,
               GROUP_CONCAT('$' || PRINTF('%.2f', amount), ' + ') as detail
        FROM novo_transactions
        WHERE category = 'Owner Contribution'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    count = r['cnt'] or 0
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} owner contributions ({r['detail']})"))
    lines.append(_je_line(OWNER_EQUITY, 0, total,
        f"{mon} owner equity contribution"))
    
    return lines, {'total': total, 'count': count}


def section_j_novo_funding(conn, year, month):
    """Section J: Novo Funding MCA (Jan–Mar 2025 only)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, SUM(mca_principal) as principal, 
               SUM(mca_interest) as interest, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'Loan Repayment'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = abs(round(r['total'] or 0, 2))
    principal = round(r['principal'] or 0, 2)
    interest = round(r['interest'] or 0, 2)
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    if principal > 0:
        lines.append(_je_line(NOVO_FUNDING_LOC, principal, 0,
            f"{mon} MCA principal"))
    if interest > 0:
        lines.append(_je_line(NOVO_FUNDING_RATE, interest, 0,
            f"{mon} MCA interest"))
    lines.append(_je_line(NOVO_BANK, 0, total,
        f"{mon} Novo Funding payment"))
    
    return lines, {'total': total, 'principal': principal, 'interest': interest}


def section_k_amazon_payouts(conn, year, month):
    """Section K: Amazon Payouts (Link My Books offset — Novo deposit side only)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt,
               GROUP_CONCAT('$' || PRINTF('%.2f', amount), ' + ') as detail
        FROM novo_transactions
        WHERE category = 'Amazon Payout'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    count = r['cnt'] or 0
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} Amazon deposits ({r['detail']})"))
    lines.append(_je_line(AMAZON_RESERVED, 0, total,
        f"{mon} Amazon settlement to Novo"))
    
    return lines, {'total': total, 'count': count}


# ── Future Sections (auto-included when data exists) ──────────────────────────

def section_l_stripe_revenue(conn, year, month):
    """Section L: Stripe Revenue (sale-date basis, clearing account model)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT 
            COUNT(*) as order_count,
            COALESCE(SUM(sale_price), 0) as total_sale_price,
            COALESCE(SUM(shipping_charged), 0) as total_shipping,
            COALESCE(SUM(platform_fees_fixed + platform_fees_variable), 0) as total_fees,
            COALESCE(SUM(shipping_cost), 0) as total_shipping_cost
        FROM sales
        WHERE platform = 'Stripe' AND sale_date >= ? AND sale_date < ?
    """, (start, end))
    
    r = _fone(c)
    count = r['order_count']
    if count == 0:
        return [], {'orders': 0}
    
    sale_price = round(r['total_sale_price'], 2)
    shipping = round(r['total_shipping'], 2)
    fees = round(r['total_fees'], 2)
    ship_cost = round(r['total_shipping_cost'], 2)
    gross = round(sale_price + shipping, 2)
    total_deductions = round(abs(fees) + abs(ship_cost), 2)
    
    lines = []
    
    # Revenue side
    lines.append(_je_line(STRIPE_RESERVED, gross, 0,
        f"{mon} Stripe gross receivable ({count} orders)"))
    if sale_price != 0:
        lines.append(_je_line(DIRECT_SALES, 0, sale_price,
            f"{mon} Stripe product revenue"))
    if shipping != 0:
        lines.append(_je_line(SHIPPING_INCOME, 0, shipping,
            f"{mon} Stripe shipping charged"))
    
    # Fee side
    if fees != 0:
        lines.append(_je_line('Merchant Account Fees', abs(fees), 0,
            f"{mon} Stripe processing fees"))
    if ship_cost != 0:
        lines.append(_je_line('Shipping Charges', abs(ship_cost), 0,
            f"{mon} Stripe shipping costs"))
    if total_deductions > 0:
        lines.append(_je_line(STRIPE_RESERVED, 0, total_deductions,
            f"{mon} Stripe fee deductions"))
    
    return lines, {
        'orders': count, 'gross': gross, 'sale_price': sale_price,
        'shipping': shipping, 'fees': fees, 'ship_cost': ship_cost,
    }


def section_m_stripe_payouts(conn, year, month):
    """Section M: Stripe Payouts to Novo"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'Stripe Payout'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    count = r['cnt'] or 0
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} Stripe deposits ({count} transfers)"))
    lines.append(_je_line(STRIPE_RESERVED, 0, total,
        f"{mon} Stripe payout clearing"))
    
    return lines, {'total': total, 'count': count}


def section_n_other_platform_sales(conn, year, month):
    """
    Section N: PayPal/Venmo/Direct/Mercari Sales.
    No clearing account — cash received directly (PayPal/Venmo balance or cash).
    These are revenue-only entries; the cash side is handled by PayPal/Venmo transfers to Novo.
    
    For now, record as: DR PayPal and Venmo (asset), CR Product Sales.
    """
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    platforms = ['PayPal', 'Venmo', 'Direct', 'Mercari', 'Cash']
    
    c.execute(f"""
        SELECT platform, COUNT(*) as cnt,
               COALESCE(SUM(sale_price), 0) as total_sale,
               COALESCE(SUM(shipping_charged), 0) as total_shipping,
               COALESCE(SUM(platform_fees_fixed + platform_fees_variable), 0) as total_fees
        FROM sales
        WHERE platform IN ({','.join('?' * len(platforms))})
        AND sale_date >= ? AND sale_date < ?
        GROUP BY platform
        HAVING COUNT(*) > 0
    """, platforms + [start, end])
    
    rows = _fall(c)
    if not rows:
        return [], {'platforms': {}}
    
    lines = []
    platform_details = {}
    total_revenue = 0
    total_shipping = 0
    total_fees = 0
    
    for row in rows:
        platform = row['platform']
        sale = round(row['total_sale'], 2)
        ship = round(row['total_shipping'], 2)
        fees = round(abs(row['total_fees']), 2)
        gross = round(sale + ship, 2)
        
        platform_details[platform] = {
            'count': row['cnt'], 'sale': sale, 'shipping': ship, 'fees': fees,
        }
        total_revenue += sale
        total_shipping += ship
        total_fees += fees
    
    total_gross = round(total_revenue + total_shipping, 2)
    platform_list = ', '.join(f"{p} ({d['count']})" for p, d in platform_details.items())
    
    # DR: Paypal and Venmo (asset) for the gross amount
    lines.append(_je_line(PAYPAL_ASSET, total_gross, 0,
        f"{mon} other platform sales ({platform_list})"))
    
    if total_revenue != 0:
        lines.append(_je_line(DIRECT_SALES, 0, round(total_revenue, 2),
            f"{mon} other platform product revenue"))
    if total_shipping != 0:
        lines.append(_je_line(SHIPPING_INCOME, 0, round(total_shipping, 2),
            f"{mon} other platform shipping income"))
    
    # Fees as expense
    if total_fees > 0:
        lines.append(_je_line('Merchant Account Fees', total_fees, 0,
            f"{mon} other platform fees"))
        lines.append(_je_line(PAYPAL_ASSET, 0, total_fees,
            f"{mon} other platform fee deduction"))
    
    return lines, {'platforms': platform_details, 'total_gross': total_gross}


def section_o_ebay_giftcard(conn, year, month):
    """Section O: eBay Gift Card Spending (DR expense/COGS, CR Owner Investment)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT category, SUM(amount) as net_amount, COUNT(*) as cnt
        FROM transactions
        WHERE source = 'eBay-GiftCard'
        AND transaction_date >= ? AND transaction_date < ?
        AND category NOT LIKE 'SKIP%'
        AND category IS NOT NULL
        GROUP BY category
        HAVING SUM(amount) != 0
        ORDER BY ABS(SUM(amount)) DESC
    """, (start, end))
    
    rows = _fall(c)
    if not rows:
        return [], {'total': 0}
    
    lines = []
    total = 0
    
    for row in rows:
        expense_amount = abs(round(row['net_amount'], 2))
        total += expense_amount
        lines.append(_je_line(row['category'], expense_amount, 0,
            f"{mon} eBay gift card — {row['category'].lower()} ({row['cnt']} txn{'s' if row['cnt'] > 1 else ''})"))
    
    total = round(total, 2)
    if total > 0:
        lines.append(_je_line(OWNER_EQUITY, 0, total,
            f"{mon} eBay gift card spending (owner-funded)"))
    
    return lines, {'total': total}


def section_p_paypal_transfers(conn, year, month):
    """Section P: PayPal Transfers to/from Novo"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT subcategory, SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'PayPal Transfer'
        AND transaction_date >= ? AND transaction_date < ?
        GROUP BY subcategory
    """, (start, end))
    
    rows = _fall(c)
    if not rows:
        return [], {'total': 0}
    
    lines = []
    info = {}
    
    for row in rows:
        amount = round(row['total'], 2)
        direction = row['subcategory']  # 'inbound' or 'outbound'
        info[direction] = amount
        
        if amount > 0:  # Inbound: PayPal → Novo
            lines.append(_je_line(NOVO_BANK, amount, 0,
                f"{mon} PayPal transfer to Novo ({row['cnt']} transfer{'s' if row['cnt'] > 1 else ''})"))
            lines.append(_je_line(PAYPAL_ASSET, 0, amount,
                f"{mon} PayPal balance transfer"))
        elif amount < 0:  # Outbound: Novo → PayPal
            lines.append(_je_line(PAYPAL_ASSET, abs(amount), 0,
                f"{mon} Novo transfer to PayPal ({row['cnt']} transfer{'s' if row['cnt'] > 1 else ''})"))
            lines.append(_je_line(NOVO_BANK, 0, abs(amount),
                f"{mon} PayPal outbound transfer"))
    
    return lines, info


def section_q_venmo_transfers(conn, year, month):
    """Section Q: Venmo Transfers to Novo"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'Venmo Transfer'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    count = r['cnt'] or 0
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} Venmo cashout to Novo ({count} transfer{'s' if count > 1 else ''})"))
    lines.append(_je_line(PAYPAL_ASSET, 0, total,
        f"{mon} Venmo balance transfer"))
    
    return lines, {'total': total, 'count': count}


def section_r_mercari_payouts(conn, year, month):
    """Section R: Mercari Payouts to Novo (Dec 2025 only)"""
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    c.execute("""
        SELECT SUM(amount) as total, COUNT(*) as cnt
        FROM novo_transactions
        WHERE category = 'Mercari Payout'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    
    r = _fone(c)
    total = round(r['total'] or 0, 2)
    
    if total == 0:
        return [], {'total': 0}
    
    lines = []
    lines.append(_je_line(NOVO_BANK, total, 0,
        f"{mon} Mercari deposit"))
    # Mercari sales revenue should already be in section N via sales table
    # This just records the cash movement to Novo
    # Use a Mercari clearing or Paypal and Venmo — keeping it simple
    lines.append(_je_line(PAYPAL_ASSET, 0, total,
        f"{mon} Mercari payout clearing"))
    
    return lines, {'total': total}


def section_s_novo_vendor_payments(conn, year, month):
    """
    Section S: Novo Direct Vendor Payments (UPDATED 2026-03-09).
    
    Handles purchases paid directly from Novo bank account (not via credit card):
      - InDiPro ACH payments → Trading cards - IndiPro
      - PayPal vendor payments (to individuals) → Trading cards - collections (default)
    
    These are in the transactions table with source='Novo-Vendor'.
    JE logic: DR COGS account / CR Novo Bank
    """
    start, end = _month_range(year, month)
    c = conn.cursor()
    mon = datetime(year, month, 1).strftime('%b')
    
    # Get Novo vendor expenses grouped by category, excluding SKIP
    c.execute("""
        SELECT category, SUM(amount) as net_amount, COUNT(*) as cnt
        FROM transactions
        WHERE source = 'Novo-Vendor' AND transaction_date >= ? AND transaction_date < ?
        AND category NOT LIKE 'SKIP%'
        AND category IS NOT NULL
        AND status = 'Categorized'
        GROUP BY category
        HAVING SUM(amount) != 0
        ORDER BY ABS(SUM(amount)) DESC
    """, (start, end))
    
    rows = _fall(c)
    
    if not rows:
        return [], {'total': 0, 'categories': {}}
    
    lines = []
    total = 0
    categories = {}
    
    for row in rows:
        net = round(row['net_amount'], 2)
        if net == 0:
            continue
        
        expense_amount = abs(net)
        categories[row['category']] = {'amount': expense_amount, 'count': row['cnt']}
        total += expense_amount
        
        lines.append(_je_line(row['category'], expense_amount, 0,
            f"{mon} {row['category'].lower()} — Novo direct ({row['cnt']} txn{'s' if row['cnt'] > 1 else ''})"))
    
    total = round(total, 2)
    
    if total > 0:
        lines.append(_je_line(NOVO_BANK, 0, total,
            f"{mon} Novo vendor payments"))
    
    return lines, {'total': total, 'categories': categories}


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-EXPORT RECONCILIATION CHECKS (Deliverable 3)
# ═══════════════════════════════════════════════════════════════════════════════

def run_reconciliation_checks(conn, year, month):
    """
    Run pre-export checks before generating a journal entry.
    Returns list of check results.
    """
    start, end = _month_range(year, month)
    c = conn.cursor()
    checks = []
    
    # 1. Pending (uncategorized) Chase transactions
    c.execute("""
        SELECT COUNT(*) as cnt FROM transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        AND (category IS NULL OR status = 'Pending')
    """, (start, end))
    pending = _fone(c)['cnt']
    checks.append({
        'name': 'Uncategorized Transactions',
        'status': 'FAIL' if pending > 0 else 'PASS',
        'detail': f"{pending} transactions need categorization" if pending > 0 else "All categorized",
        'blocking': pending > 0,
    })
    
    # 2. eBay Clearing Account Reconciliation
    #    The clearing account must balance: all DRs (revenue) minus all CRs (fees + payouts + other fees)
    #    should equal the amount currently in transit (sales made but not yet paid out).
    #    We check CUMULATIVELY through end of this month since timing differences are expected monthly.
    
    # DR side: all eBay revenue ever recognized (sale_price + shipping_charged) through this month
    c.execute("""
        SELECT COALESCE(SUM(sale_price + shipping_charged), 0) as gross
        FROM sales WHERE platform = 'eBay' AND sale_date < ?
    """, (end,))
    cum_gross = round(_fone(c)['gross'], 2)
    
    # CR side 1: per-sale fees from sales table (sale-date basis)
    c.execute("""
        SELECT COALESCE(SUM(platform_fees_fixed + platform_fees_variable 
                           + promoted_listing_fee + shipping_cost), 0) as fees
        FROM sales WHERE platform = 'eBay' AND sale_date < ?
    """, (end,))
    cum_sale_fees = round(abs(_fone(c)['fees']), 2)
    
    # CR side 2: non-sale fees from ebay_transactions (payout-date basis)
    #            Excludes Promoted Listings (already in per-sale fees via Section B).
    #            Includes Claims/disputes which reduce payouts but aren't per-sale fees.
    #            Uses COALESCE(payout_date, transaction_date) so API-synced rows
    #            without payout_date are still counted.
    c.execute("""
        SELECT COALESCE(SUM(net_amount), 0) as other_fees
        FROM ebay_transactions WHERE type IN ('Other fee', 'Adjustment', 'Claim')
        AND description NOT LIKE '%Promoted Listing%'
        AND COALESCE(payout_date, transaction_date) >= '2025-01-01' 
        AND COALESCE(payout_date, transaction_date) < ?
    """, (end,))
    cum_other_fees = round(abs(_fone(c)['other_fees']), 2)
    
    # CR side 3: actual payouts to Novo
    c.execute("""
        SELECT COALESCE(SUM(amount), 0) as payouts
        FROM novo_transactions WHERE category = 'eBay Payout'
        AND transaction_date < ?
    """, (end,))
    cum_payouts = round(_fone(c)['payouts'], 2)
    
    clearing_balance = round(cum_gross - cum_sale_fees - cum_other_fees - cum_payouts, 2)
    
    # The clearing balance should be explainable as "in-transit" revenue:
    # sales recognized but not yet paid out (typically last ~week of the month).
    # 
    # Known gap: per-sale fees in the sales table understate actual eBay deductions
    # by ~$0.50/order (regulatory fees, rounding, etc.). This creates a structural
    # negative drift of ~$50-100/month. A moderately negative balance (down to -$1000)
    # is expected and shown as WARN. Balances below -$1000 indicate a real problem.
    if clearing_balance < -1000:
        status = 'FAIL'
    elif clearing_balance < -10:
        status = 'WARN'
    elif abs(clearing_balance) > 5000:
        status = 'WARN'
    else:
        status = 'PASS'
    
    checks.append({
        'name': 'eBay Clearing Account (cumulative)',
        'status': status,
        'detail': (f"Balance: ${clearing_balance:,.2f} "
                  f"(DR gross: ${cum_gross:,.2f}, "
                  f"CR sale fees: ${cum_sale_fees:,.2f}, "
                  f"CR other fees: ${cum_other_fees:,.2f}, "
                  f"CR payouts: ${cum_payouts:,.2f})"),
        'blocking': clearing_balance < -1000,
    })
    
    # 3. Unmatched CC payments
    c.execute("""
        SELECT COUNT(*) as cnt FROM novo_transactions
        WHERE category = 'CC Payment' AND cc_account = 'Chase CC - needs review'
        AND transaction_date >= ? AND transaction_date < ?
    """, (start, end))
    unmatched_cc = _fone(c)['cnt']
    checks.append({
        'name': 'Unmatched CC Payments',
        'status': 'FAIL' if unmatched_cc > 0 else 'PASS',
        'detail': f"{unmatched_cc} Chase CC payments need card assignment" if unmatched_cc > 0 
                  else "All CC payments matched to cards",
        'blocking': unmatched_cc > 0,
    })
    
    # 4. Missing data sources
    expected_sources = ['Chase-5742', 'Chase-4433']
    for source in expected_sources:
        c.execute("""
            SELECT COUNT(*) as cnt FROM transactions
            WHERE source = ? AND transaction_date >= ? AND transaction_date < ?
            AND category NOT LIKE 'SKIP%'
        """, (source, start, end))
        cnt = _fone(c)['cnt']
        if cnt == 0:
            checks.append({
                'name': f'Missing {source} Data',
                'status': 'WARN',
                'detail': f"No {source} transactions found for this month",
                'blocking': False,
            })
    
    # 5. Novo transactions exist
    c.execute("""
        SELECT COUNT(*) as cnt FROM novo_transactions
        WHERE transaction_date >= ? AND transaction_date < ?
        AND category != 'SKIP'
    """, (start, end))
    novo_cnt = _fone(c)['cnt']
    checks.append({
        'name': 'Novo Bank Data',
        'status': 'PASS' if novo_cnt > 0 else 'FAIL',
        'detail': f"{novo_cnt} Novo transactions found" if novo_cnt > 0 
                  else "No Novo transactions — import CSV first",
        'blocking': novo_cnt == 0,
    })
    
    # 6. Unresolved refunds (refunds in ebay_transactions without matching reversal in sales)
    c.execute("""
        SELECT COUNT(*) as cnt FROM ebay_transactions
        WHERE type = 'REFUND'
        AND payout_date >= ? AND payout_date < ?
        AND order_number NOT IN (
            SELECT DISTINCT order_number FROM sales WHERE item_title LIKE 'REFUND:%'
        )
    """, (start, end))
    unresolved_refunds = _fone(c)['cnt']
    if unresolved_refunds > 0:
        checks.append({
            'name': 'Unresolved Refunds',
            'status': 'WARN',
            'detail': f"{unresolved_refunds} refund(s) without reversal lines in sales",
            'blocking': False,
        })
    
    # 7. Missing Wave accounts — check if all accounts used in JE exist in Wave
    #    (This runs after JE generation, but we can pre-check common ones)
    
    return checks


def check_wave_accounts(je_result):
    """
    Check that all account names used in the JE exist in the Wave COA.
    Returns list of missing account names that need to be created in Wave.
    """
    used_accounts = set(line['Account Name'] for line in je_result['wave_csv_lines'])
    missing = [acc for acc in used_accounts if acc not in WAVE_IDS]
    return missing


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def generate_journal_entry(conn, year, month, skip_recon=False):
    """
    Generate a complete monthly journal entry.
    
    Returns dict with:
        - sections: ordered list of (label, lines, metadata) tuples
        - totals: {debits, credits, balanced}
        - recon_checks: list of check results
        - wave_csv_lines: list of dicts for Wave Connect export
        - month_label: "January 2025" etc.
    """
    month_label = f"{datetime(year, month, 1).strftime('%B %Y')}"
    
    # Run reconciliation checks first
    recon_checks = [] if skip_recon else run_reconciliation_checks(conn, year, month)
    
    # Generate all sections
    sections = []
    
    # Core sections (A–K)
    section_defs = [
        ('A', 'eBay Revenue', section_a_ebay_revenue),
        ('B', 'eBay Fees & Costs (per-sale)', section_b_ebay_fees),
        ('B2', 'eBay Non-Sale Fees', section_b2_ebay_other_fees),
        ('C', 'eBay Refunds (info)', section_c_ebay_refunds),
        ('D', 'eBay Payouts to Novo', section_d_ebay_payouts),
        ('E', 'Chase Ink Unlimited 5742', section_e_chase_5742),
        ('F', 'Chase Ink Preferred 4433', section_f_chase_4433),
        ('G', 'Chase Ink Other 4051', section_g_chase_4051),
        ('H', 'CC Payments from Novo', section_h_cc_payments),
        ('I', 'Owner Contributions', section_i_owner_contributions),
        ('J', 'Novo Funding MCA', section_j_novo_funding),
        ('K', 'Amazon Payouts', section_k_amazon_payouts),
    ]
    
    # Future sections — only include if data exists
    future_defs = [
        ('L', 'Stripe Revenue', section_l_stripe_revenue),
        ('M', 'Stripe Payouts to Novo', section_m_stripe_payouts),
        ('N', 'Other Platform Sales', section_n_other_platform_sales),
        ('O', 'eBay Gift Card Spending', section_o_ebay_giftcard),
        ('P', 'PayPal Transfers', section_p_paypal_transfers),
        ('Q', 'Venmo Transfers', section_q_venmo_transfers),
        ('R', 'Mercari Payouts', section_r_mercari_payouts),
        ('S', 'Novo Vendor Payments', section_s_novo_vendor_payments),
    ]
    
    all_lines = []
    
    for code, label, func in section_defs + future_defs:
        lines, metadata = func(conn, year, month)
        if lines:  # Only include sections with actual JE lines
            sections.append((code, label, lines, metadata))
            all_lines.extend(lines)
        elif code in ('C',):
            # Always include Section C for informational purposes
            sections.append((code, label, lines, metadata))
    
    # Calculate totals
    total_debits = round(sum(l['debit'] for l in all_lines), 2)
    total_credits = round(sum(l['credit'] for l in all_lines), 2)
    balanced = abs(total_debits - total_credits) < 0.01
    
    # Generate Wave Connect CSV format
    wave_csv_lines = []
    for line in all_lines:
        wave_csv_lines.append({
            'Account Name': line['account'],
            'Debit': f"{line['debit']:.2f}" if line['debit'] > 0 else '',
            'Credit': f"{line['credit']:.2f}" if line['credit'] > 0 else '',
            'Description': line['description'],
        })
    
    return {
        'sections': sections,
        'all_lines': all_lines,
        'totals': {
            'debits': total_debits,
            'credits': total_credits,
            'balanced': balanced,
        },
        'recon_checks': recon_checks,
        'wave_csv_lines': wave_csv_lines,
        'month_label': month_label,
    }


def generate_wave_csv(je_result):
    """
    Generate a CSV string for Wave Connect import.
    
    Wave Connect Google Sheet format:
        Column A: Wave Id (account ID — optional, Wave matches by name if blank)
        Column B: Tax Activity (leave blank for JE lines)
        Column C: Account Name (must match Wave COA exactly)
        Column D: Debit amount
        Column E: Credit amount
        Column F: Line Item Description (Optional)
    """
    import csv
    import io
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header row matching Wave Connect columns exactly
    writer.writerow([
        'Wave Id',
        'Tax Activity', 
        'Account Name', 
        'Debit', 
        'Credit', 
        'Line Item Description (Optional)',
    ])
    
    for line in je_result['wave_csv_lines']:
        account_name = line['Account Name']
        wave_id = WAVE_IDS.get(account_name, '')
        
        writer.writerow([
            wave_id,
            '',  # Tax Activity — always blank for JE
            account_name,
            line['Debit'],
            line['Credit'],
            line['Description'],
        ])
    
    return output.getvalue()


def generate_markdown_report(je_result):
    """Generate a readable markdown report of the journal entry."""
    r = je_result
    lines = []
    lines.append(f"# {r['month_label']} — Monthly Journal Entry")
    lines.append("")
    
    # Reconciliation checks
    if r['recon_checks']:
        lines.append("## Pre-Export Checks")
        for check in r['recon_checks']:
            icon = '✅' if check['status'] == 'PASS' else '⚠️' if check['status'] in ('WARN', 'INFO') else '❌'
            lines.append(f"- {icon} **{check['name']}**: {check['detail']}")
        lines.append("")
    
    # Sections
    lines.append("## Journal Entry")
    lines.append("")
    
    for code, label, section_lines, metadata in r['sections']:
        lines.append(f"### Section {code}: {label}")
        lines.append("")
        
        if not section_lines:
            # Informational section (e.g., refunds)
            note = metadata.get('note', 'No activity')
            if metadata.get('count', 0) > 0:
                lines.append(f"*{metadata['count']} refund(s) — revenue impact ${metadata.get('refund_revenue', 0):.2f} "
                           f"(included in Section A/B via reversal lines)*")
            else:
                lines.append(f"*{note}*")
            lines.append("")
            continue
        
        lines.append("| Account Name | Debit | Credit | Description |")
        lines.append("|-------------|------:|-------:|-------------|")
        
        section_dr = 0
        section_cr = 0
        for sl in section_lines:
            dr = f"{sl['debit']:.2f}" if sl['debit'] > 0 else ""
            cr = f"{sl['credit']:.2f}" if sl['credit'] > 0 else ""
            lines.append(f"| {sl['account']} | {dr} | {cr} | {sl['description']} |")
            section_dr += sl['debit']
            section_cr += sl['credit']
        
        lines.append(f"\n*Section {code} total: DR ${section_dr:.2f} / CR ${section_cr:.2f}*")
        lines.append("")
    
    # Totals
    lines.append("## Totals")
    lines.append("")
    lines.append(f"| | Amount |")
    lines.append(f"|---|------:|")
    lines.append(f"| Total Debits | ${r['totals']['debits']:,.2f} |")
    lines.append(f"| Total Credits | ${r['totals']['credits']:,.2f} |")
    lines.append(f"| **Balanced** | **{'✅ YES' if r['totals']['balanced'] else '❌ NO — INVESTIGATE'}** |")
    
    # Novo reconciliation
    lines.append("")
    lines.append("## Novo Bank Reconciliation")
    novo_debits = sum(l['debit'] for l in r['all_lines'] if l['account'] == NOVO_BANK)
    novo_credits = sum(l['credit'] for l in r['all_lines'] if l['account'] == NOVO_BANK)
    novo_net = round(novo_debits - novo_credits, 2)
    lines.append(f"- Novo debits (deposits): ${novo_debits:,.2f}")
    lines.append(f"- Novo credits (withdrawals): ${novo_credits:,.2f}")
    lines.append(f"- **Novo net change: ${novo_net:+,.2f}**")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE TESTING
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'taclaco.db'
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
    month = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    
    conn = sqlite3.connect(db_path)
    
    print(f"Generating journal entry for {datetime(year, month, 1).strftime('%B %Y')}...")
    print()
    
    result = generate_journal_entry(conn, year, month)
    
    # Print markdown report
    print(generate_markdown_report(result))
    
    # Print balance check
    print(f"\n{'═' * 60}")
    print(f"Debits:  ${result['totals']['debits']:>12,.2f}")
    print(f"Credits: ${result['totals']['credits']:>12,.2f}")
    print(f"Balanced: {'✅' if result['totals']['balanced'] else '❌'}")
    
    # Write Wave CSV
    csv_content = generate_wave_csv(result)
    csv_filename = f"wave_je_{year}_{month:02d}.csv"
    with open(csv_filename, 'w') as f:
        f.write(csv_content)
    print(f"\nWave CSV written to: {csv_filename}")
    
    conn.close()
