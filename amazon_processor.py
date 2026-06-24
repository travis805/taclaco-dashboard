"""
Amazon Transaction Categorization & COGS Calculation Tool
Replaces Link My Books ($20/month) for Amazon settlement processing.

Reads Amazon settlement TSV files, applies categorization rules,
calculates COGS based on SKU costs, and exports Wave-ready CSV files.
"""

import csv
import json
import os
import logging
from datetime import datetime
from collections import defaultdict

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def load_config(config_path="amazon_config.json"):
    """Load categorization rules from JSON config."""
    with open(config_path, 'r') as f:
        return json.load(f)


def load_sku_costs(costs_path):
    """Load SKU-to-cost mapping from CSV file."""
    sku_costs = {}
    with open(costs_path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row['SKU'].strip().upper()
            cost = float(row['Cost'].strip())
            sku_costs[sku] = cost
    logger.info(f"Loaded {len(sku_costs)} SKU cost mappings")
    return sku_costs


def parse_settlement(settlement_path):
    """
    Parse Amazon settlement TSV file into structured rows.
    Skips the summary row (row 2) which has total-amount but no transaction details.
    """
    rows = []
    settlement_info = {}
    
    with open(settlement_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter='\t')
        
        for i, row in enumerate(reader):
            # First data row is the settlement summary
            if i == 0:
                settlement_info = {
                    'settlement_id': row.get('settlement-id', '').strip(),
                    'start_date': row.get('settlement-start-date', '').strip(),
                    'end_date': row.get('settlement-end-date', '').strip(),
                    'deposit_date': row.get('deposit-date', '').strip(),
                    'total_amount': row.get('total-amount', '0').strip(),
                    'currency': row.get('currency', 'USD').strip(),
                }
                logger.info(f"Settlement {settlement_info['settlement_id']}: "
                           f"{settlement_info['start_date']} to {settlement_info['end_date']}, "
                           f"deposit: {settlement_info['deposit_date']}, "
                           f"total: ${settlement_info['total_amount']}")
                continue
            
            rows.append(row)
    
    logger.info(f"Parsed {len(rows)} transaction rows from settlement")
    return settlement_info, rows


def is_tax_row(row, config):
    """Check if a row should be excluded as a tax row."""
    amount_type = (row.get('amount-type') or '').strip()
    amount_desc = (row.get('amount-description') or '').strip()
    
    for rule in config.get('tax_exclusion_rules', []):
        if 'amount_type' in rule and amount_type == rule['amount_type']:
            if 'amount_description' in rule and amount_desc == rule['amount_description']:
                return True
            if 'amount_description_prefix' in rule and amount_desc.startswith(rule['amount_description_prefix']):
                return True
    return False


def categorize_row(row, config):
    """
    Categorize a single transaction row based on config mappings.
    Returns (wave_id, account_name, category, generate_cogs) or None if row should be skipped.
    """
    # Skip tax rows
    if is_tax_row(row, config):
        return None
    
    transaction_type = (row.get('transaction-type') or '').strip()
    amount_type = (row.get('amount-type') or '').strip()
    amount_desc = (row.get('amount-description') or '').strip()
    
    # Skip empty rows
    if not transaction_type and not amount_type:
        return None
    
    # ── REFUND transactions: everything goes to Customer Refunds ──
    # The principal, commission reversal, RefundCommission fee, promo reversal,
    # ReturnShipping — all net into one refund account.
    # COGS reversal is only triggered on the Principal line.
    if transaction_type == 'Refund':
        refund_acct = config.get('refund_account', {
            'wave_id': '2366209877568771896',
            'account_name': 'Customer refunds'
        })
        # Only the Principal line triggers a COGS reversal
        is_principal = (amount_type == 'ItemPrice' and amount_desc == 'Principal')
        return (refund_acct['wave_id'], refund_acct['account_name'], 'Refund', is_principal)
    
    # ── FBA Inventory Reimbursement: credit against Amazon Fees ──
    if amount_type == 'FBA Inventory Reimbursement':
        fees_acct = config['account_mappings'].get('Order|ItemFees|Commission', {})
        wave_id = fees_acct.get('wave_id', '2365186199225750546')
        acct_name = fees_acct.get('account_name', 'Amazon Fees')
        return (wave_id, acct_name, 'FBA Reimbursement', False)
    
    # ── Standard lookup: transaction-type|amount-type|amount-description ──
    lookup_key = f"{transaction_type}|{amount_type}|{amount_desc}"
    mappings = config.get('account_mappings', {})
    
    # Direct match
    if lookup_key in mappings:
        m = mappings[lookup_key]
        return (m['wave_id'], m['account_name'], m['category'], m.get('generate_cogs', False))
    
    # ── Fallback rules ──
    amount_str = (row.get('amount') or '0').strip()
    try:
        amount = float(amount_str)
    except ValueError:
        amount = 0.0
    
    fallback = config.get('fallback_rules', {})
    
    if 'shipping' in amount_desc.lower() or 'shipping' in amount_type.lower():
        fb = fallback.get('shipping_keyword', fallback.get('negative_default'))
        return (fb['wave_id'], fb['account_name'], 'Shipping (fallback)', False)
    
    if 'reserve' in amount_desc.lower():
        fb = fallback.get('reserve_keyword', fallback.get('negative_default'))
        return (fb['wave_id'], fb['account_name'], 'Reserve (fallback)', False)
    
    if amount < 0:
        fb = fallback.get('negative_default')
        return (fb['wave_id'], fb['account_name'], f'Uncategorized Expense ({amount_desc})', False)
    
    fb = fallback.get('positive_default')
    return (fb['wave_id'], fb['account_name'], f'Uncategorized Revenue ({amount_desc})', True)


def generate_description(category, sku, amount_desc, quantity=0):
    """Generate human-readable description for Wave journal entry."""
    if 'Sales Revenue' in category:
        qty_str = f" ({quantity} unit{'s' if quantity != 1 else ''})" if quantity else ""
        return f"Amazon Sales - {sku}{qty_str}" if sku else f"Amazon Sales{qty_str}"
    
    if 'Shipping Income' in category:
        return f"Amazon Shipping Income - {sku}" if sku else "Amazon Shipping Income"
    
    if 'Refund' in category and 'Fee' not in category:
        qty_str = f" ({quantity} unit{'s' if quantity != 1 else ''})" if quantity else ""
        return f"Amazon Refund - {sku}{qty_str}" if sku else f"Amazon Refund{qty_str}"
    
    if 'Fee' in category and 'FBA' not in category and 'Handling' not in category:
        return f"Amazon Seller Fees - {amount_desc}"
    
    if 'Promotional' in category or 'Discount' in category:
        return f"Amazon Promo Discount - {sku}" if sku else "Amazon Promo Discount"
    
    if 'Reserve' in category:
        return f"Amazon Reserved Balances - {amount_desc}"
    
    if 'Shipping' in category or 'FBA' in category or 'Handling' in category:
        return f"Amazon Shipping Cost - {amount_desc}"
    
    if 'Subscription' in category:
        return "Amazon Seller Subscription Fee"
    
    if 'Storage' in category:
        return "Amazon Storage Fee"
    
    if 'COGS' in category:
        return f"COGS - {sku}" if sku else "COGS"
    
    return f"Amazon - {category} - {amount_desc}"


def process_settlement(settlement_path, costs_path, config_path="amazon_config.json"):
    """
    Main processing function. Reads settlement, categorizes, calculates COGS.
    
    Returns:
        dict with keys:
            - 'settlement_info': dict of settlement metadata
            - 'categorized_rows': list of processed row dicts
            - 'wave_rows': list of Wave-ready output dicts
            - 'summary': dict of summary statistics
            - 'warnings': list of warning messages
            - 'skipped_tax_rows': count of tax rows excluded
    """
    config = load_config(config_path)
    sku_costs = load_sku_costs(costs_path)
    settlement_info, raw_rows = parse_settlement(settlement_path)
    
    warnings = []
    skipped_tax_count = 0
    categorized_rows = []
    
    # Phase 1: Categorize each row
    for row in raw_rows:
        result = categorize_row(row, config)
        
        if result is None:
            skipped_tax_count += 1
            continue
        
        wave_id, account_name, category, generate_cogs = result
        
        amount_str = (row.get('amount') or '0').strip()
        try:
            amount = float(amount_str)
        except ValueError:
            warnings.append(f"Invalid amount '{amount_str}' for order {row.get('order-id', 'N/A')}")
            continue
        
        sku = (row.get('sku') or '').strip().upper()
        posted_date = (row.get('posted-date') or '').strip()
        amount_desc = (row.get('amount-description') or '').strip()
        order_id = (row.get('order-id') or '').strip()
        transaction_type = (row.get('transaction-type') or '').strip()
        
        quantity_str = (row.get('quantity-purchased') or '').strip()
        try:
            quantity = int(quantity_str) if quantity_str else 0
        except ValueError:
            quantity = 0
        
        # For refund principal lines with no quantity, default to 1.
        # Log a warning if the refund amount is large enough to suggest multi-unit.
        if generate_cogs and quantity == 0 and transaction_type == 'Refund' and sku:
            quantity = 1
            if sku in sku_costs and sku_costs[sku] > 0:
                # If refund amount is more than 2x the cost, likely multi-unit
                cost_ratio = abs(amount) / sku_costs[sku]
                if cost_ratio > 2.5:
                    warnings.append(
                        f"Refund {order_id}: ${abs(amount):.2f} for {sku} may be multi-unit "
                        f"(cost ${sku_costs[sku]:.2f}, ratio {cost_ratio:.1f}x). "
                        f"COGS defaulted to 1 unit. Review and adjust if needed."
                    )
        
        # Look up COGS
        unit_cost = 0.0
        cogs_amount = 0.0
        if generate_cogs and sku:
            if sku in sku_costs:
                unit_cost = sku_costs[sku]
                cogs_amount = quantity * unit_cost
            else:
                warnings.append(f"SKU '{sku}' not found in cost mapping (order {order_id}). COGS set to $0.")
        
        categorized_rows.append({
            'posted_date': posted_date,
            'order_id': order_id,
            'transaction_type': transaction_type,
            'amount_type': (row.get('amount-type') or '').strip(),
            'amount_description': amount_desc,
            'wave_id': wave_id,
            'account_name': account_name,
            'category': category,
            'amount': amount,
            'sku': sku,
            'quantity': quantity,
            'unit_cost': unit_cost,
            'cogs_amount': cogs_amount,
            'generate_cogs': generate_cogs,
            'description': generate_description(category, sku, amount_desc, quantity),
        })
    
    logger.info(f"Categorized {len(categorized_rows)} rows, skipped {skipped_tax_count} tax rows")
    
    # Phase 2: Build Wave output rows
    # Settlement rows (revenue, fees, shipping, reserves) balance naturally.
    # COGS entries are separate (debit COGS / credit Inventory) and kept in
    # a separate list so Travis can import them as a separate JE or combine.
    wave_rows = []      # Main settlement journal (self-balancing)
    cogs_rows = []      # COGS journal entries (need inventory offset)
    
    for cr in categorized_rows:
        amount = cr['amount']
        
        # Settlement amounts have correct signs:
        #   positive = credit (revenue, reserve release)
        #   negative = debit (fees, costs, reserve holds)
        if amount >= 0:
            debit = ""
            credit = f"{amount:.2f}"
        else:
            debit = f"{abs(amount):.2f}"
            credit = ""
        
        wave_rows.append({
            'Wave Id': cr['wave_id'],
            'Tax Activity': '',
            'Account Name': cr['account_name'],
            'Debit': debit,
            'Credit': credit,
            'Line Item Description (Optional)': cr['description'],
            '_posted_date': cr['posted_date'],
            '_sku': cr['sku'],
            '_quantity': cr['quantity'],
            '_category': cr['category'],
            '_order_id': cr['order_id'],
        })
        
        # Generate COGS entry if applicable (debit COGS / credit Inventory)
        if cr['generate_cogs'] and cr['cogs_amount'] > 0:
            cogs_config = config['cogs_account']
            inv_config = config['inventory_account']
            qty_label = f"{cr['quantity']} unit{'s' if cr['quantity'] != 1 else ''}"
            cost_label = f"@ ${cr['unit_cost']:.2f}"
            
            if cr['transaction_type'] == 'Refund':
                # Refund reversal: credit COGS (reduce expense), debit Inventory (restore asset)
                cogs_rows.append({
                    'Wave Id': cogs_config['wave_id'],
                    'Tax Activity': '',
                    'Account Name': cogs_config['account_name'],
                    'Debit': '',
                    'Credit': f"{cr['cogs_amount']:.2f}",
                    'Line Item Description (Optional)': f"COGS Reversal - {cr['sku']} ({qty_label} {cost_label})",
                    '_posted_date': cr['posted_date'],
                    '_sku': cr['sku'],
                    '_quantity': cr['quantity'],
                    '_category': 'COGS',
                    '_order_id': cr['order_id'],
                })
                cogs_rows.append({
                    'Wave Id': inv_config['wave_id'],
                    'Tax Activity': '',
                    'Account Name': inv_config['account_name'],
                    'Debit': f"{cr['cogs_amount']:.2f}",
                    'Credit': '',
                    'Line Item Description (Optional)': f"Inventory Restored - {cr['sku']} ({qty_label} {cost_label})",
                    '_posted_date': cr['posted_date'],
                    '_sku': cr['sku'],
                    '_quantity': cr['quantity'],
                    '_category': 'Inventory',
                    '_order_id': cr['order_id'],
                })
            else:
                # Sale: debit COGS (increase expense), credit Inventory (reduce asset)
                cogs_rows.append({
                    'Wave Id': cogs_config['wave_id'],
                    'Tax Activity': '',
                    'Account Name': cogs_config['account_name'],
                    'Debit': f"{cr['cogs_amount']:.2f}",
                    'Credit': '',
                    'Line Item Description (Optional)': f"COGS - {cr['sku']} ({qty_label} {cost_label})",
                    '_posted_date': cr['posted_date'],
                    '_sku': cr['sku'],
                    '_quantity': cr['quantity'],
                    '_category': 'COGS',
                    '_order_id': cr['order_id'],
                })
                cogs_rows.append({
                    'Wave Id': inv_config['wave_id'],
                    'Tax Activity': '',
                    'Account Name': inv_config['account_name'],
                    'Debit': '',
                    'Credit': f"{cr['cogs_amount']:.2f}",
                    'Line Item Description (Optional)': f"Inventory Sold - {cr['sku']} ({qty_label} {cost_label})",
                    '_posted_date': cr['posted_date'],
                    '_sku': cr['sku'],
                    '_quantity': cr['quantity'],
                    '_category': 'Inventory',
                    '_order_id': cr['order_id'],
                })
    
    # Phase 3: Add clearing account entry for the net settlement deposit/withdrawal
    # The clearing account represents cash moving to/from the bank.
    # Positive settlement = Amazon deposits cash (debit clearing = receivable)
    # Negative settlement = Amazon withholds/charges (credit clearing = payable)
    settle_debits = sum(float(r['Debit']) for r in wave_rows if r['Debit'])
    settle_credits = sum(float(r['Credit']) for r in wave_rows if r['Credit'])
    net_movement = settle_credits - settle_debits  # positive = net credit, needs debit clearing
    
    clearing_config = config['clearing_account']
    if abs(net_movement) > 0.005:
        if net_movement > 0:
            wave_rows.insert(0, {
                'Wave Id': clearing_config['wave_id'],
                'Tax Activity': '',
                'Account Name': clearing_config['account_name'],
                'Debit': f"{net_movement:.2f}",
                'Credit': '',
                'Line Item Description (Optional)': f"Amazon Settlement {settlement_info['settlement_id']} - Gross Receivable",
                '_posted_date': settlement_info.get('deposit_date', '').split(' ')[0],
                '_sku': '', '_quantity': 0, '_category': 'Clearing', '_order_id': '',
            })
        else:
            wave_rows.insert(0, {
                'Wave Id': clearing_config['wave_id'],
                'Tax Activity': '',
                'Account Name': clearing_config['account_name'],
                'Debit': '',
                'Credit': f"{abs(net_movement):.2f}",
                'Line Item Description (Optional)': f"Amazon Settlement {settlement_info['settlement_id']} - Net Payable",
                '_posted_date': settlement_info.get('deposit_date', '').split(' ')[0],
                '_sku': '', '_quantity': 0, '_category': 'Clearing', '_order_id': '',
            })
    
    # Verify settlement + clearing balances
    final_settle_dr = sum(float(r['Debit']) for r in wave_rows if r['Debit'])
    final_settle_cr = sum(float(r['Credit']) for r in wave_rows if r['Credit'])
    settle_diff = abs(final_settle_dr - final_settle_cr)
    
    if settle_diff > 0.01:
        warnings.append(f"Settlement journal imbalance: DR {final_settle_dr:.2f} vs CR {final_settle_cr:.2f} (diff {settle_diff:.2f})")
        logger.warning(f"Settlement journal does not self-balance! Diff: {settle_diff:.2f}")
    else:
        logger.info(f"Settlement journal balanced: DR = CR = ${final_settle_dr:.2f}")
    
    # Verify COGS entries balance (debit COGS = credit Inventory)
    cogs_debits = sum(float(r['Debit']) for r in cogs_rows if r['Debit'])
    cogs_credits = sum(float(r['Credit']) for r in cogs_rows if r['Credit'])
    cogs_diff = abs(cogs_debits - cogs_credits)
    if cogs_diff > 0.01:
        warnings.append(f"COGS journal imbalance: DR {cogs_debits:.2f} vs CR {cogs_credits:.2f}")
    else:
        logger.info(f"COGS journal balanced: DR = CR = ${cogs_debits:.2f}")
    
    # Combined journal should now be fully balanced
    all_wave_rows = wave_rows + cogs_rows
    
    # Phase 4: Build summary-level Wave export (one line per GL account)
    period_label = _build_period_label(settlement_info)
    summary_wave_rows = _summarize_by_account(all_wave_rows, settlement_info, period_label)
    
    # Phase 5: Calculate summary
    summary = calculate_summary(all_wave_rows, settlement_info, skipped_tax_count)
    
    # Final balance check (settlement portion only)
    logger.info(f"Settlement: DR ${settle_debits:.2f} / CR ${settle_credits:.2f} (balanced: {settle_diff < 0.01})")
    
    cogs_total = sum(float(r['Debit']) for r in cogs_rows if r['Debit'])
    cogs_reversal = sum(float(r['Credit']) for r in cogs_rows if r['Credit'])
    logger.info(f"COGS entries: DR ${cogs_total:.2f} / CR ${cogs_reversal:.2f} (net: ${cogs_total - cogs_reversal:.2f})")
    
    return {
        'settlement_info': settlement_info,
        'categorized_rows': categorized_rows,
        'wave_rows': all_wave_rows,
        'summary_wave_rows': summary_wave_rows,
        'settlement_rows': wave_rows,
        'cogs_rows': cogs_rows,
        'summary': summary,
        'warnings': warnings,
        'skipped_tax_rows': skipped_tax_count,
    }


def _build_period_label(settlement_info):
    """Build a short period label like 'Jan 30 - Feb 13' from settlement dates."""
    try:
        start = settlement_info.get('start_date', '')[:10]  # '2026-01-30'
        end = settlement_info.get('end_date', '')[:10]
        sd = datetime.strptime(start, '%Y-%m-%d')
        ed = datetime.strptime(end, '%Y-%m-%d')
        return f"{sd.strftime('%b %d')} - {ed.strftime('%b %d')}"
    except (ValueError, IndexError):
        return "settlement period"


def _summarize_by_account(detail_rows, settlement_info, period_label):
    """
    Aggregate detail rows into one line per GL account for Wave export.
    
    Produces output like the eBay JE style:
      Product Sales,,Product Sales,,1647.50,Amazon product revenue (93 orders) Jan 30 - Feb 13
      COGS - Kitcoff,,COGS - Kitcoff,839.87,,Amazon COGS (93 units) Jan 30 - Feb 13
    """
    # Accumulate by (wave_id, account_name)
    acct_totals = {}  # key -> {debit, credit, count, quantities}
    
    for row in detail_rows:
        key = (row['Wave Id'], row['Account Name'])
        if key not in acct_totals:
            acct_totals[key] = {'debit': 0.0, 'credit': 0.0, 'count': 0, 'qty': 0}
        
        acct_totals[key]['debit'] += float(row['Debit']) if row['Debit'] else 0.0
        acct_totals[key]['credit'] += float(row['Credit']) if row['Credit'] else 0.0
        acct_totals[key]['count'] += 1
        acct_totals[key]['qty'] += row.get('_quantity', 0) or 0
    
    sid = settlement_info.get('settlement_id', '')
    
    # Description templates per account
    desc_map = {
        'Product Sales': lambda s: f"Amazon product revenue ({s['count']} orders) {period_label}",
        'Shipping Income': lambda s: f"Amazon shipping charged to buyers ({s['count']} orders) {period_label}",
        'Amazon Fees': lambda s: f"Amazon seller fees ({s['count']} txns) {period_label}",
        'Amazon shipping costs': lambda s: f"Amazon shipping costs ({s['count']} txns) {period_label}",
        'COGS - Kitcoff': lambda s: f"Amazon COGS ({s['qty']} units) {period_label}",
        'Inventory - Kitcoff': lambda s: f"Amazon inventory ({s['qty']} units) {period_label}",
        'Customer refunds': lambda s: f"Amazon customer refunds ({s['count']} txns) {period_label}",
        'Discounts & Promos': lambda s: f"Amazon promotional discounts ({s['count']} txns) {period_label}",
        'Amazon Reserved Balances': lambda s: f"Amazon reserve balance {period_label}",
        'Clearing - Amazon Settlement': lambda s: f"Amazon settlement deposit {period_label}",
    }
    
    summary_rows = []
    for (wave_id, acct_name), stats in acct_totals.items():
        debit = stats['debit']
        credit = stats['credit']
        
        # Net the debits and credits for accounts that can have both (e.g., reserves)
        # For most accounts only one side will be non-zero
        net_debit = max(debit - credit, 0.0)
        net_credit = max(credit - debit, 0.0)
        
        # Build description
        desc_fn = desc_map.get(acct_name, lambda s: f"Amazon {acct_name} ({s['count']} txns) {period_label}")
        description = desc_fn(stats)
        
        if net_debit > 0.005:  # avoid rounding dust
            summary_rows.append({
                'Wave Id': wave_id,
                'Tax Activity': '',
                'Account Name': acct_name,
                'Debit': f"{net_debit:.2f}",
                'Credit': '',
                'Line Item Description (Optional)': description,
            })
        elif net_credit > 0.005:
            summary_rows.append({
                'Wave Id': wave_id,
                'Tax Activity': '',
                'Account Name': acct_name,
                'Debit': '',
                'Credit': f"{net_credit:.2f}",
                'Line Item Description (Optional)': description,
            })
    
    # Verify balance
    total_dr = sum(float(r['Debit']) for r in summary_rows if r['Debit'])
    total_cr = sum(float(r['Credit']) for r in summary_rows if r['Credit'])
    diff = abs(total_dr - total_cr)
    if diff > 0.01:
        logger.warning(f"Summary rows imbalance: DR {total_dr:.2f} vs CR {total_cr:.2f} (diff {diff:.2f})")
    else:
        logger.info(f"Summary rows balanced: DR = CR = ${total_dr:.2f} ({len(summary_rows)} lines)")
    
    return summary_rows


def calculate_summary(wave_rows, settlement_info, skipped_tax_count):
    """Calculate summary statistics from wave output rows."""
    summary = {
        'settlement_id': settlement_info.get('settlement_id', ''),
        'period': f"{settlement_info.get('start_date', '')} to {settlement_info.get('end_date', '')}",
        'deposit_date': settlement_info.get('deposit_date', ''),
        'stated_total': settlement_info.get('total_amount', '0'),
        'total_rows': len(wave_rows),
        'skipped_tax_rows': skipped_tax_count,
        'by_account': defaultdict(lambda: {'debit': 0.0, 'credit': 0.0, 'count': 0}),
    }
    
    for row in wave_rows:
        acct = row['Account Name']
        debit = float(row['Debit']) if row['Debit'] else 0.0
        credit = float(row['Credit']) if row['Credit'] else 0.0
        summary['by_account'][acct]['debit'] += debit
        summary['by_account'][acct]['credit'] += credit
        summary['by_account'][acct]['count'] += 1
    
    # Convert defaultdict to regular dict for JSON serialization
    summary['by_account'] = dict(summary['by_account'])
    
    # Key totals
    summary['total_debits'] = sum(float(r['Debit']) for r in wave_rows if r['Debit'])
    summary['total_credits'] = sum(float(r['Credit']) for r in wave_rows if r['Credit'])
    summary['product_sales'] = summary['by_account'].get('Product Sales', {}).get('credit', 0.0)
    summary['shipping_income'] = summary['by_account'].get('Shipping Income', {}).get('credit', 0.0)
    summary['total_cogs'] = summary['by_account'].get('COGS - Kitcoff', {}).get('debit', 0.0)
    summary['amazon_fees'] = summary['by_account'].get('Amazon Fees', {}).get('debit', 0.0)
    summary['shipping_costs'] = summary['by_account'].get('Amazon shipping costs', {}).get('debit', 0.0)
    summary['refunds'] = summary['by_account'].get('Customer refunds', {}).get('debit', 0.0)
    summary['reserves_debit'] = summary['by_account'].get('Amazon Reserved Balances', {}).get('debit', 0.0)
    summary['reserves_credit'] = summary['by_account'].get('Amazon Reserved Balances', {}).get('credit', 0.0)
    summary['discounts'] = summary['by_account'].get('Discounts & Promos', {}).get('debit', 0.0)
    summary['inventory_credit'] = summary['by_account'].get('Inventory - Kitcoff', {}).get('credit', 0.0)
    summary['gross_profit'] = summary['product_sales'] + summary['shipping_income'] - summary['total_cogs']
    
    return summary


def export_wave_csv(wave_rows, output_path):
    """Export Wave-ready CSV file (only the 6 standard columns)."""
    fieldnames = ['Wave Id', 'Tax Activity', 'Account Name', 'Debit', 'Credit', 'Line Item Description (Optional)']
    
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(wave_rows)
    
    logger.info(f"Exported {len(wave_rows)} rows to {output_path}")
    return output_path


def export_summary(summary, warnings, output_path):
    """Export processing summary to text file."""
    lines = []
    lines.append("=" * 60)
    lines.append("AMAZON SETTLEMENT PROCESSING SUMMARY")
    lines.append("=" * 60)
    lines.append(f"Settlement ID: {summary['settlement_id']}")
    lines.append(f"Period: {summary['period']}")
    lines.append(f"Deposit Date: {summary['deposit_date']}")
    lines.append(f"Stated Settlement Total: ${summary['stated_total']}")
    lines.append("")
    lines.append("-" * 40)
    lines.append("FINANCIAL SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Product Sales:       ${summary['product_sales']:>10.2f}")
    lines.append(f"Shipping Income:     ${summary['shipping_income']:>10.2f}")
    lines.append(f"Total Revenue:       ${summary['product_sales'] + summary['shipping_income']:>10.2f}")
    lines.append(f"COGS:                ${summary['total_cogs']:>10.2f}")
    lines.append(f"Gross Profit:        ${summary['gross_profit']:>10.2f}")
    lines.append(f"Amazon Fees:         ${summary['amazon_fees']:>10.2f}")
    lines.append(f"Shipping Costs:      ${summary['shipping_costs']:>10.2f}")
    lines.append(f"Discounts:           ${summary['discounts']:>10.2f}")
    lines.append(f"Refunds:             ${summary['refunds']:>10.2f}")
    lines.append(f"Reserves (Debit):    ${summary['reserves_debit']:>10.2f}")
    lines.append(f"Reserves (Credit):   ${summary['reserves_credit']:>10.2f}")
    lines.append("")
    lines.append("-" * 40)
    lines.append("JOURNAL BALANCE")
    lines.append("-" * 40)
    lines.append(f"Total Debits:        ${summary['total_debits']:>10.2f}")
    lines.append(f"Total Credits:       ${summary['total_credits']:>10.2f}")
    diff = abs(summary['total_debits'] - summary['total_credits'])
    lines.append(f"Difference:          ${diff:>10.2f} {'BALANCED' if diff < 0.01 else 'UNBALANCED!'}")
    lines.append(f"")
    lines.append(f"COGS / Inventory:    ${summary['total_cogs']:>10.2f} (DR COGS / CR Inventory)")
    lines.append("")
    lines.append("-" * 40)
    lines.append("ROWS BY ACCOUNT")
    lines.append("-" * 40)
    for acct_name, stats in sorted(summary['by_account'].items()):
        dr = stats['debit']
        cr = stats['credit']
        lines.append(f"  {acct_name:<35} DR: ${dr:>9.2f}  CR: ${cr:>9.2f}  ({stats['count']} rows)")
    lines.append("")
    lines.append(f"Total output rows: {summary['total_rows']}")
    lines.append(f"Tax rows excluded: {summary['skipped_tax_rows']}")
    
    if warnings:
        lines.append("")
        lines.append("-" * 40)
        lines.append(f"WARNINGS ({len(warnings)})")
        lines.append("-" * 40)
        for w in warnings:
            lines.append(f"  ! {w}")
    
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    with open(output_path, 'w') as f:
        f.write('\n'.join(lines))
    
    logger.info(f"Summary exported to {output_path}")
    return '\n'.join(lines)


def main():
    """CLI entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Amazon Transaction Categorization & COGS Calculator')
    parser.add_argument('--settlement', required=True, help='Path to Amazon settlement TSV file')
    parser.add_argument('--costs', required=True, help='Path to SKU-cost CSV mapping')
    parser.add_argument('--config', default='amazon_config.json', help='Path to config JSON')
    parser.add_argument('--output', default='.', help='Output directory')
    parser.add_argument('--verbose', action='store_true', help='Print detailed logs')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Process
    result = process_settlement(args.settlement, args.costs, args.config)
    
    # Generate output filenames
    sid = result['settlement_info'].get('settlement_id', 'unknown')
    date_str = datetime.now().strftime('%Y%m%d')
    
    csv_path = os.path.join(args.output, f"amazon_transactions_{sid}_{date_str}.csv")
    summary_path = os.path.join(args.output, f"amazon_summary_{sid}_{date_str}.txt")
    
    # Export summary-level Wave CSV (one line per GL account)
    export_wave_csv(result['summary_wave_rows'], csv_path)
    summary_text = export_summary(result['summary'], result['warnings'], summary_path)
    
    print(summary_text)
    print(f"\nFiles exported:")
    print(f"  Wave CSV: {csv_path}")
    print(f"  Summary:  {summary_path}")


if __name__ == '__main__':
    main()
