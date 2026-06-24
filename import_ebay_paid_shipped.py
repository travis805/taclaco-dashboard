"""
Taclaco Dashboard - eBay Paid and Shipped Report Importer

PURPOSE:
This importer is specifically for capturing TRACKING NUMBERS from historical eBay orders.
The eBay Fulfillment API only returns orders from the last 90 days, so for older orders,
we need this CSV import to get tracking numbers for matching Pirate Ship shipping costs.

HOW TO GET THE REPORT:
1. Go to eBay Seller Hub -> Reports tab
2. Click "Download report"
3. Select "Orders" -> "Paid and Shipped"
4. Set your date range (can go back to beginning of year or further)
5. Download and upload here

WHAT THIS IMPORTER DOES:
- Extracts tracking numbers and links them to order numbers
- Stores in ebay_fulfillments table (same as API sync)
- Enables Pirate Ship shipping cost matching for historical orders
- Does NOT import financial data (use Transaction Report or API for fees)

NOTE: This report has tracking numbers but limited fee details. For complete
profit calculations, the eBay API sync (last 90 days) or Transaction Report
is needed for fee data.
"""

import pandas as pd
from datetime import datetime
from pathlib import Path
import database as db


def parse_currency(value):
    """Convert currency string to float. Handles '$1,234.56' format."""
    if pd.isna(value) or value in ("--", ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
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
        # eBay format: "Dec-28-25"
        dt = datetime.strptime(str(value).strip(), "%b-%d-%y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # Alt format: "Dec 28, 2025"
            dt = datetime.strptime(str(value).strip(), "%b %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return str(value)


def import_paid_shipped_report(filepath):
    """
    Import eBay Paid and Shipped Report CSV.
    
    This importer focuses on capturing tracking numbers for orders,
    which enables matching Pirate Ship shipping costs to orders.
    
    Args:
        filepath: Path to the CSV file
    
    Returns:
        dict with import statistics
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    
    # Read CSV - skip the first empty row, header is row 1
    df = pd.read_csv(filepath, skiprows=1, encoding='utf-8-sig')
    
    # Normalize column names
    df.columns = df.columns.str.strip()
    
    stats = {
        "total_rows": len(df),
        "orders_processed": 0,
        "tracking_numbers_found": 0,
        "fulfillments_added": 0,
        "fulfillments_updated": 0,
        "skipped_no_tracking": 0,
        "skipped_empty": 0,
        "errors": []
    }
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Track unique order/tracking combinations we've processed
    processed_fulfillments = set()
    
    for idx, row in df.iterrows():
        try:
            order_number = str(row.get("Order Number", "")).strip()
            tracking_number = str(row.get("Tracking Number", "")).strip()
            
            # Skip empty rows
            if not order_number or order_number == "":
                stats["skipped_empty"] += 1
                continue
            
            stats["orders_processed"] += 1
            
            # Skip if no tracking number
            if not tracking_number or tracking_number == "":
                stats["skipped_no_tracking"] += 1
                continue
            
            stats["tracking_numbers_found"] += 1
            
            # Skip if we've already processed this order/tracking combo
            combo_key = f"{order_number}|{tracking_number}"
            if combo_key in processed_fulfillments:
                continue
            processed_fulfillments.add(combo_key)
            
            # Extract other useful fields
            shipped_date = parse_date(row.get("Shipped On Date"))
            shipping_service = str(row.get("Shipping Service", "")).strip()
            
            # Determine carrier from shipping service
            carrier = "USPS"  # Default
            if "UPS" in shipping_service.upper():
                carrier = "UPS"
            elif "FEDEX" in shipping_service.upper():
                carrier = "FedEx"
            elif "DHL" in shipping_service.upper():
                carrier = "DHL"
            elif "STANDARD ENVELOPE" in shipping_service.upper():
                carrier = "USPS"  # eBay Standard Envelope is USPS
            
            # Check if this fulfillment already exists
            cursor.execute("""
                SELECT fulfillment_id FROM ebay_fulfillments
                WHERE order_id = ? AND tracking_number = ?
            """, (order_number, tracking_number))
            
            existing = cursor.fetchone()
            
            if existing:
                # Update existing record
                cursor.execute("""
                    UPDATE ebay_fulfillments
                    SET carrier = COALESCE(?, carrier),
                        ship_date = COALESCE(?, ship_date)
                    WHERE order_id = ? AND tracking_number = ?
                """, (carrier, shipped_date, order_number, tracking_number))
                stats["fulfillments_updated"] += 1
            else:
                # Insert new fulfillment record
                # Generate a fulfillment_id from order_number + tracking
                fulfillment_id = f"{order_number}-{tracking_number[:10]}"
                
                cursor.execute("""
                    INSERT INTO ebay_fulfillments (
                        fulfillment_id, order_id, tracking_number, carrier, ship_date
                    ) VALUES (?, ?, ?, ?, ?)
                """, (fulfillment_id, order_number, tracking_number, carrier, shipped_date))
                stats["fulfillments_added"] += 1
                
        except Exception as e:
            stats["errors"].append(f"Row {idx}: {str(e)}")
    
    conn.commit()
    conn.close()
    
    return stats


def get_orders_missing_tracking():
    """
    Find eBay orders that don't have tracking in ebay_fulfillments.
    Useful for identifying gaps after import.
    
    Returns:
        DataFrame of orders without tracking
    """
    conn = db.get_connection()
    df = db._read_sql(conn, """
        SELECT DISTINCT s.order_number, s.sale_date, s.item_title
        FROM sales s
        WHERE s.platform = 'eBay'
        AND NOT EXISTS (
            SELECT 1 FROM ebay_fulfillments ef
            WHERE ef.order_id = s.order_number
        )
        ORDER BY s.sale_date DESC
    """)
    conn.close()
    return df


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
        print(f"Importing: {filepath}")
        
        stats = import_paid_shipped_report(filepath)
        print(f"\nImport Statistics:")
        print(f"  Total rows: {stats['total_rows']}")
        print(f"  Orders processed: {stats['orders_processed']}")
        print(f"  Tracking numbers found: {stats['tracking_numbers_found']}")
        print(f"  Fulfillments added: {stats['fulfillments_added']}")
        print(f"  Fulfillments updated: {stats['fulfillments_updated']}")
        print(f"  Skipped (no tracking): {stats['skipped_no_tracking']}")
        print(f"  Skipped (empty): {stats['skipped_empty']}")
        
        if stats['errors']:
            print(f"\nErrors ({len(stats['errors'])}):")
            for err in stats['errors'][:10]:
                print(f"  {err}")
    else:
        print("Usage: python import_ebay_paid_shipped.py <path_to_csv>")
