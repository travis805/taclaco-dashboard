"""
Taclaco Dashboard - eBay Transaction Report Importer
Parses eBay Transaction Report CSV and imports to database
"""

import pandas as pd
from datetime import datetime
from pathlib import Path
from database import get_connection, get_purchase_id_from_sku


def parse_currency(value):
    """Convert currency string to float. Handles '$1,234.56' format."""
    if pd.isna(value) or value in ("--", ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    # Remove $ and commas, handle negatives
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_date(value):
    """Parse eBay date format to ISO format."""
    if pd.isna(value) or value in ("--", ""):
        return None
    try:
        # eBay format: "Nov 19, 2025"
        dt = datetime.strptime(str(value).strip(), "%b %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return str(value)


def find_header_row(filepath):
    """Find the row number containing the column headers."""
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        for i, line in enumerate(f):
            if "Transaction creation date" in line:
                return i
    return None


def import_ebay_transaction_report(filepath, batch_name=None):
    """
    Import eBay Transaction Report CSV into database.
    
    Args:
        filepath: Path to the CSV file
        batch_name: Optional name for this import batch (defaults to timestamp)
    
    Returns:
        dict with import statistics
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    
    # Find the header row (eBay CSVs have metadata rows at top)
    header_row = find_header_row(filepath)
    if header_row is None:
        raise ValueError("Could not find header row in CSV")
    
    # Read CSV starting from header row
    df = pd.read_csv(filepath, skiprows=header_row, encoding='utf-8-sig')
    
    # Normalize column names (remove extra spaces, lowercase)
    df.columns = df.columns.str.strip()
    
    # Generate batch name if not provided
    if batch_name is None:
        batch_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Connect to database
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {
        "total_rows": len(df),
        "orders": 0,
        "shipping_labels": 0,
        "other_fees": 0,
        "payouts": 0,
        "claims": 0,
        "subscription_fees": 0,
        "skipped": 0,
        "errors": []
    }
    
    for idx, row in df.iterrows():
        try:
            tx_type = str(row.get("Type", "")).strip()
            
            # Skip empty rows
            if not tx_type or tx_type == "--":
                stats["skipped"] += 1
                continue
            
            # Parse common fields
            tx_date = parse_date(row.get("Transaction creation date"))
            order_number = str(row.get("Order number", "")).strip()
            if order_number == "--":
                order_number = None
            
            # Insert into staging table
            cursor.execute("""
                INSERT INTO ebay_transactions (
                    transaction_date, type, order_number, legacy_order_id,
                    buyer_username, buyer_name, ship_to_city, ship_to_state,
                    ship_to_zip, ship_to_country, net_amount, payout_currency,
                    payout_date, payout_id, payout_method, payout_status,
                    reason_for_hold, item_id, transaction_id, item_title,
                    custom_label, quantity, item_subtotal, shipping_and_handling,
                    seller_collected_tax, ebay_collected_tax, final_value_fee_fixed,
                    final_value_fee_variable, regulatory_operating_fee,
                    very_high_inad_fee, below_standard_fee, international_fee,
                    charity_donation, deposit_processing_fee, gross_transaction_amount,
                    transaction_currency, exchange_rate, reference_id, description,
                    import_batch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tx_date,
                tx_type,
                order_number,
                str(row.get("Legacy order ID", "")).strip() or None,
                str(row.get("Buyer username", "")).strip() or None,
                str(row.get("Buyer name", "")).strip() or None,
                str(row.get("Ship to city", "")).strip() or None,
                str(row.get("Ship to province/region/state", "")).strip() or None,
                str(row.get("Ship to zip", "")).strip() or None,
                str(row.get("Ship to country", "")).strip() or None,
                parse_currency(row.get("Net amount")),
                str(row.get("Payout currency", "")).strip() or None,
                parse_date(row.get("Payout date")),
                str(row.get("Payout ID", "")).strip() or None,
                str(row.get("Payout method", "")).strip() or None,
                str(row.get("Payout status", "")).strip() or None,
                str(row.get("Reason for hold", "")).strip() or None,
                str(row.get("Item ID", "")).strip() or None,
                str(row.get("Transaction ID", "")).strip() or None,
                str(row.get("Item title", "")).strip() or None,
                str(row.get("Custom label", "")).strip() or None,
                int(row.get("Quantity", 0)) if pd.notna(row.get("Quantity")) and str(row.get("Quantity", "")).strip() not in ("--", "") else None,
                parse_currency(row.get("Item subtotal")),
                parse_currency(row.get("Shipping and handling")),
                parse_currency(row.get("Seller collected tax")),
                parse_currency(row.get("eBay collected tax")),
                parse_currency(row.get("Final Value Fee - fixed")),
                parse_currency(row.get("Final Value Fee - variable")),
                parse_currency(row.get("Regulatory operating fee")),
                parse_currency(row.get("Very high \"item not as described\" fee")),
                parse_currency(row.get("Below standard performance fee")),
                parse_currency(row.get("International fee")),
                parse_currency(row.get("Charity donation")),
                parse_currency(row.get("Deposit processing fee")),
                parse_currency(row.get("Gross transaction amount")),
                str(row.get("Transaction currency", "")).strip() or None,
                str(row.get("Exchange rate", "")).strip() or None,
                str(row.get("Reference ID", "")).strip() or None,
                str(row.get("Description", "")).strip() or None,
                batch_name
            ))
            
            # Track stats by type
            if tx_type == "Order":
                stats["orders"] += 1
            elif tx_type == "Shipping label":
                stats["shipping_labels"] += 1
            elif tx_type == "Other fee":
                stats["other_fees"] += 1
            elif tx_type == "Payout":
                stats["payouts"] += 1
            elif tx_type == "Claim":
                stats["claims"] += 1
                # Route buyer dispute claims to transactions table as an expense
                net_amount = parse_currency(row.get("Net amount"))
                item_title = str(row.get("Item title", "")).strip() or None
                reference_id = str(row.get("Reference ID", "")).strip() or None
                desc_parts = []
                if item_title and item_title not in ("--", ""):
                    desc_parts.append(item_title)
                if reference_id and reference_id not in ("--", ""):
                    desc_parts.append(f"Ref: {reference_id}")
                description = " | ".join(desc_parts) if desc_parts else "eBay Buyer Claim"
                cursor.execute("""
                    INSERT INTO transactions (
                        source, transaction_date, merchant_name, description,
                        amount, category, status, import_method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "eBay-Claim",
                    tx_date,
                    "eBay Buyer Claim",
                    description,
                    net_amount,  # already negative
                    "Platform Fees - eBay",
                    "Categorized",
                    f"ebay_csv:{batch_name}"
                ))

            # Check for eBay Store Subscription Fee (Other fee with no order number,
            # not a Promoted Listings fee)
            if (tx_type == "Other fee"
                    and (order_number is None)
                    and "Promoted Listings" not in str(row.get("Description", ""))):
                stats["subscription_fees"] += 1
                net_amount = parse_currency(row.get("Net amount"))
                description = str(row.get("Description", "")).strip() or "eBay Store Subscription Fee"
                cursor.execute("""
                    INSERT INTO transactions (
                        source, transaction_date, merchant_name, description,
                        amount, category, status, import_method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "eBay-Fee",
                    tx_date,
                    "eBay",
                    description,
                    net_amount,  # already negative
                    "Software & Subscriptions",
                    "Categorized",
                    f"ebay_csv:{batch_name}"
                ))

        except Exception as e:
            stats["errors"].append(f"Row {idx}: {str(e)}")
    
    conn.commit()
    conn.close()
    
    stats["batch_name"] = batch_name
    return stats


def process_orders_to_sales(batch_name=None):
    """
    Process staged eBay transactions into the sales table.
    Groups multi-item orders and calculates profit.
    
    Args:
        batch_name: If provided, only process this batch
    
    Returns:
        dict with processing statistics
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get Order transactions (these are the actual sales)
    # SKIP rows with no item title - those are order summary rows, not actual items
    where_clause = "WHERE type = 'Order' AND item_title IS NOT NULL AND item_title != '' AND item_title != '--'"
    if batch_name:
        where_clause += f" AND import_batch = '{batch_name}'"
    
    cursor.execute(f"""
        SELECT * FROM ebay_transactions 
        {where_clause}
        ORDER BY order_number, transaction_date
    """)
    
    transactions = cursor.fetchall()
    
    stats = {
        "processed": 0,
        "skipped_duplicates": 0,
        "errors": []
    }
    
    for tx in transactions:
        try:
            # Check if this transaction already exists in sales
            cursor.execute("""
                SELECT sale_id FROM sales 
                WHERE platform = 'eBay' 
                AND order_number = ? 
                AND transaction_id = ?
            """, (tx["order_number"], tx["transaction_id"]))
            
            if cursor.fetchone():
                stats["skipped_duplicates"] += 1
                continue
            
            # Get purchase ID from SKU
            purchase_id = get_purchase_id_from_sku(
                tx["custom_label"], 
                tx["item_title"]
            )
            
            # Calculate net profit for this line item
            # Revenue
            revenue = (tx["item_subtotal"] or 0) + (tx["shipping_and_handling"] or 0)
            
            # Fees (stored as negative in eBay report, we want positive)
            fees_fixed = abs(tx["final_value_fee_fixed"] or 0)
            fees_variable = abs(tx["final_value_fee_variable"] or 0)
            regulatory_fee = abs(tx["regulatory_operating_fee"] or 0)
            international_fee = abs(tx["international_fee"] or 0)
            
            # Net = Revenue - Fees (shipping cost and supplies added later)
            net_profit = revenue - fees_fixed - fees_variable - regulatory_fee - international_fee
            
            # Insert into sales table
            cursor.execute("""
                INSERT INTO sales (
                    purchase_id, platform, order_number, transaction_id,
                    item_title, custom_label, sale_date, quantity,
                    sale_price, shipping_charged, shipping_cost,
                    platform_fees_fixed, platform_fees_variable,
                    regulatory_fee, promoted_listing_fee, international_fee,
                    supplies_estimate, net_profit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                purchase_id,
                "eBay",
                tx["order_number"],
                tx["transaction_id"],
                tx["item_title"],
                tx["custom_label"],
                tx["transaction_date"],
                tx["quantity"],
                tx["item_subtotal"],
                tx["shipping_and_handling"],
                0,  # shipping_cost - needs to be linked from shipping label transactions
                fees_fixed,
                fees_variable,
                regulatory_fee,
                0,  # promoted_listing_fee - linked separately
                international_fee,
                0,  # supplies_estimate - calculated from tiers
                net_profit
            ))
            
            stats["processed"] += 1
            
        except Exception as e:
            stats["errors"].append(f"Order {tx['order_number']}: {str(e)}")
    
    conn.commit()
    conn.close()
    
    return stats


def link_shipping_costs(batch_name=None):
    """
    Link shipping label costs to their orders.
    Updates sales records with actual shipping costs.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get shipping label transactions
    where_clause = "WHERE type = 'Shipping label'"
    if batch_name:
        where_clause += f" AND import_batch = '{batch_name}'"
    
    cursor.execute(f"""
        SELECT order_number, net_amount, reference_id 
        FROM ebay_transactions 
        {where_clause}
    """)
    
    shipping_labels = cursor.fetchall()
    
    stats = {"linked": 0, "not_found": 0}
    
    for label in shipping_labels:
        # Shipping costs are negative in the report
        shipping_cost = abs(label["net_amount"])
        
        # Update all sales for this order (split evenly if multiple items)
        cursor.execute("""
            SELECT COUNT(*) as count FROM sales 
            WHERE order_number = ? AND platform = 'eBay'
        """, (label["order_number"],))
        
        count_result = cursor.fetchone()
        item_count = count_result["count"] if count_result else 0
        
        if item_count > 0:
            # Split shipping cost across items in the order
            cost_per_item = shipping_cost / item_count
            
            cursor.execute("""
                UPDATE sales 
                SET shipping_cost = shipping_cost + ?,
                    net_profit = net_profit - ?
                WHERE order_number = ? AND platform = 'eBay'
            """, (cost_per_item, cost_per_item, label["order_number"]))
            
            stats["linked"] += 1
        else:
            stats["not_found"] += 1
    
    conn.commit()
    conn.close()
    
    return stats


def link_promoted_listing_fees(batch_name=None):
    """
    Link promoted listing fees to their orders.
    Updates sales records with ad costs.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get promoted listing fee transactions
    where_clause = "WHERE type = 'Other fee' AND description LIKE '%Promoted Listings%'"
    if batch_name:
        where_clause += f" AND import_batch = '{batch_name}'"
    
    cursor.execute(f"""
        SELECT order_number, item_id, net_amount 
        FROM ebay_transactions 
        {where_clause}
    """)
    
    promo_fees = cursor.fetchall()
    
    stats = {"linked": 0, "not_found": 0}
    
    for fee in promo_fees:
        # Fees are negative in the report
        promo_cost = abs(fee["net_amount"])
        
        # Try to match by order number first
        cursor.execute("""
            UPDATE sales 
            SET promoted_listing_fee = promoted_listing_fee + ?,
                net_profit = net_profit - ?
            WHERE order_number = ? AND platform = 'eBay'
        """, (promo_cost, promo_cost, fee["order_number"]))
        
        if cursor.rowcount > 0:
            stats["linked"] += 1
        else:
            stats["not_found"] += 1
    
    conn.commit()
    conn.close()
    
    return stats


if __name__ == "__main__":
    # Test import with a sample file
    import sys
    
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
        print(f"Importing: {filepath}")
        
        # Import raw transactions
        import_stats = import_ebay_transaction_report(filepath)
        print(f"Import stats: {import_stats}")
        
        # Process to sales
        process_stats = process_orders_to_sales(import_stats["batch_name"])
        print(f"Process stats: {process_stats}")
        
        # Link shipping costs
        shipping_stats = link_shipping_costs(import_stats["batch_name"])
        print(f"Shipping link stats: {shipping_stats}")
        
        # Link promoted listing fees
        promo_stats = link_promoted_listing_fees(import_stats["batch_name"])
        print(f"Promo fee link stats: {promo_stats}")
    else:
        print("Usage: python import_ebay.py <path_to_csv>")
