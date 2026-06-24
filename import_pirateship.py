"""
Pirate Ship Import Module
Parses Pirate Ship shipment exports (XLSX) and imports shipping costs
Matches shipping costs to orders by tracking number
"""

import pandas as pd
from datetime import datetime
import database as db


def parse_pirate_ship_date(date_str):
    """
    Parse Pirate Ship date format to ISO date
    Format: "11/29/25 9:23 AM PST" or similar
    Returns: YYYY-MM-DD string
    """
    if pd.isna(date_str):
        return None
    
    try:
        # Remove timezone suffix (PST, EST, etc.)
        date_str = str(date_str).strip()
        for tz in [' PST', ' EST', ' CST', ' MST', ' PDT', ' EDT', ' CDT', ' MDT']:
            date_str = date_str.replace(tz, '')
        
        # Parse the date
        dt = pd.to_datetime(date_str)
        return dt.strftime('%Y-%m-%d')
    except:
        return None


def detect_carrier(tracking_number):
    """
    Detect carrier from tracking number format
    
    USPS: 20-22 digits or starts with 94
    UPS: Starts with 1Z
    FedEx: 12-15 digits
    """
    if not tracking_number:
        return 'Unknown'
    
    tracking = str(tracking_number).strip().upper()
    
    if tracking.startswith('1Z'):
        return 'UPS'
    elif tracking.startswith('94') or len(tracking) >= 20:
        return 'USPS'
    elif len(tracking) in [12, 15]:
        return 'FedEx'
    else:
        return 'USPS'  # Default to USPS for Pirate Ship


def parse_pirate_ship_xlsx(file_path):
    """
    Parse Pirate Ship shipments export (XLSX)
    
    Expected columns:
    - Created Date
    - Recipient
    - Email
    - Tracking Number
    - Cost
    - Status
    - Source (eBay or blank for direct sales)
    - Batch
    - Label Size
    - Saved Package
    - Ship From
    - Tracking Status
    
    Returns: DataFrame with cleaned data
    """
    df = pd.read_excel(file_path)
    
    # Validate required columns
    required_cols = ['Tracking Number', 'Cost', 'Status']
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    
    # Clean and standardize
    df['tracking_number'] = df['Tracking Number'].astype(str).str.strip()
    df['cost'] = pd.to_numeric(df['Cost'], errors='coerce').fillna(0)
    df['status'] = df['Status'].astype(str).str.strip()
    df['recipient'] = df['Recipient'].astype(str).str.strip() if 'Recipient' in df.columns else ''
    df['email'] = df['Email'].astype(str) if 'Email' in df.columns else ''
    df['source'] = df['Source'].fillna('Direct') if 'Source' in df.columns else 'Unknown'
    
    # Parse ship date
    if 'Created Date' in df.columns:
        df['ship_date'] = df['Created Date'].apply(parse_pirate_ship_date)
    else:
        df['ship_date'] = None
    
    # Detect carrier
    df['carrier'] = df['tracking_number'].apply(detect_carrier)
    
    # Service type from tracking pattern (basic detection)
    def detect_service(tracking, cost):
        if cost > 10:
            return 'Priority' if cost < 20 else 'Priority Express'
        return 'First Class'
    
    df['service_type'] = df.apply(lambda r: detect_service(r['tracking_number'], r['cost']), axis=1)
    
    return df


def import_pirate_ship_costs(file_path, purchased_only=True, direct_sales_only=False):
    """
    Import shipping costs from Pirate Ship export
    
    Args:
        file_path: Path to the Pirate Ship XLSX export
        purchased_only: Only import labels with Status='Purchased' (default True)
        direct_sales_only: Only import direct sales (Source is blank/NaN)
    
    Returns:
        dict with import statistics
    """
    stats = {
        'total_rows': 0,
        'imported': 0,
        'duplicates': 0,
        'skipped_status': 0,
        'skipped_source': 0,
        'matched_to_orders': 0,
        'unmatched': 0,
        'errors': []
    }
    
    # Parse the file
    try:
        df = parse_pirate_ship_xlsx(file_path)
        stats['total_rows'] = len(df)
    except Exception as e:
        stats['errors'].append(f"Parse error: {str(e)}")
        return stats
    
    # Generate import batch ID
    import_batch_id = int(datetime.now().timestamp())
    
    for idx, row in df.iterrows():
        try:
            # Filter by status
            if purchased_only and row['status'] != 'Purchased':
                stats['skipped_status'] += 1
                continue
            
            # Filter by source
            source = row['source']
            is_direct = source in ['Direct', 'nan', ''] or pd.isna(row.get('Source'))
            
            if direct_sales_only and not is_direct:
                stats['skipped_source'] += 1
                continue
            
            # Skip if tracking number is empty or invalid
            tracking = row['tracking_number']
            if not tracking or tracking == 'nan' or len(tracking) < 10:
                stats['errors'].append(f"Row {idx}: Invalid tracking number")
                continue
            
            # Check for duplicate
            existing = db.get_shipping_cost_by_tracking(tracking)
            if existing:
                stats['duplicates'] += 1
                continue
            
            # Add shipping cost with recipient
            shipping_id = db.add_shipping_cost(
                tracking_number=tracking,
                ship_date=row['ship_date'],
                carrier=row['carrier'],
                service_type=row['service_type'],
                cost=row['cost'],
                weight_oz=None,  # Not in Pirate Ship export
                from_zip=None,
                to_zip=None,
                recipient=row['recipient'],  # Include recipient for troubleshooting
                import_batch_id=import_batch_id
            )
            
            if shipping_id:
                stats['imported'] += 1
                
                # Try to match to an order
                matched = db.match_shipping_to_order(tracking)
                if matched:
                    stats['matched_to_orders'] += 1
                else:
                    stats['unmatched'] += 1
            else:
                stats['duplicates'] += 1
                
        except Exception as e:
            stats['errors'].append(f"Row {idx}: {str(e)}")
    
    # Log the import
    latest_date = None
    shipping_df = db.get_all_shipping_costs()
    if not shipping_df.empty:
        latest_date = shipping_df['ship_date'].max()
    
    db.log_data_import(
        source='Pirate Ship',
        records_imported=stats['imported'],
        records_skipped=stats['duplicates'] + stats['skipped_status'] + stats['skipped_source'],
        latest_transaction_date=latest_date,
        import_status='Success' if not stats['errors'] else 'Partial',
        error_message='; '.join(stats['errors'][:5]) if stats['errors'] else None,
        file_name=str(file_path).split('/')[-1]
    )
    
    # Update data source status
    db.recalculate_data_source_status()
    
    return stats


def match_all_unmatched():
    """
    Attempt to match all unmatched shipping costs to orders
    Useful after importing orders that were shipped before being recorded
    
    Returns: dict with match statistics
    """
    return db.match_all_shipping_costs()


def get_shipping_summary():
    """
    Get summary of shipping costs
    Returns: dict with totals and averages
    """
    return db.get_shipping_summary()


def get_unmatched_shipments():
    """
    Get shipping costs that haven't been matched to orders
    Returns: DataFrame
    """
    return db.get_unmatched_shipping_costs()


def get_direct_sales_shipments():
    """
    Get shipping costs for direct sales (non-eBay)
    These are the ones that should match to Stripe/PayPal orders
    
    Returns: DataFrame of unmatched direct sale shipments
    """
    # Get all unmatched shipping costs
    unmatched = db.get_unmatched_shipping_costs()
    return unmatched


def preview_import(file_path, purchased_only=True, direct_sales_only=False):
    """
    Preview what would be imported without actually importing
    
    Returns: dict with preview data
    """
    df = parse_pirate_ship_xlsx(file_path)
    
    # Apply filters
    if purchased_only:
        df = df[df['status'] == 'Purchased']
    
    if direct_sales_only:
        df = df[df['source'].isin(['Direct', 'nan', '']) | df['Source'].isna()]
    
    # Check for existing tracking numbers
    existing_count = 0
    for tracking in df['tracking_number']:
        if db.get_shipping_cost_by_tracking(tracking):
            existing_count += 1
    
    return {
        'total_rows': len(df),
        'already_imported': existing_count,
        'will_import': len(df) - existing_count,
        'total_cost': df['cost'].sum(),
        'date_range': {
            'earliest': df['ship_date'].min(),
            'latest': df['ship_date'].max()
        },
        'by_carrier': df['carrier'].value_counts().to_dict(),
        'by_source': df['source'].value_counts().to_dict(),
        'sample_records': df[['tracking_number', 'recipient', 'cost', 'ship_date', 'carrier', 'source']].head(10).to_dict('records')
    }


def link_shipment_to_order_manually(tracking_number, fulfillment_id):
    """
    Manually link a shipping cost to an order
    
    Args:
        tracking_number: The tracking number to link
        fulfillment_id: The order's fulfillment_id
    
    Returns: bool success
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE shipping_costs
        SET fulfillment_id = ?, matched = 1
        WHERE tracking_number = ?
    """, (fulfillment_id, tracking_number))
    
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    
    return success


def get_ebay_vs_direct_breakdown(file_path):
    """
    Analyze Pirate Ship export for eBay vs Direct sales breakdown
    Useful for understanding shipping cost distribution
    
    Returns: dict with breakdown stats
    """
    df = parse_pirate_ship_xlsx(file_path)
    purchased = df[df['status'] == 'Purchased']
    
    # Separate eBay from direct
    ebay = purchased[purchased['source'] == 'eBay']
    direct = purchased[purchased['source'].isin(['Direct', 'nan', '']) | purchased['Source'].isna()]
    
    return {
        'total_purchased_labels': len(purchased),
        'total_shipping_cost': purchased['cost'].sum(),
        'ebay': {
            'count': len(ebay),
            'total_cost': ebay['cost'].sum(),
            'avg_cost': ebay['cost'].mean() if len(ebay) > 0 else 0
        },
        'direct': {
            'count': len(direct),
            'total_cost': direct['cost'].sum(),
            'avg_cost': direct['cost'].mean() if len(direct) > 0 else 0
        }
    }


if __name__ == "__main__":
    import sys
    
    print("Pirate Ship Import Module")
    print("=" * 40)
    
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        print(f"\nPreviewing: {file_path}")
        
        # Preview the import
        preview = preview_import(file_path)
        print(f"\nPreview:")
        print(f"  Total rows: {preview['total_rows']}")
        print(f"  Already imported: {preview['already_imported']}")
        print(f"  Will import: {preview['will_import']}")
        print(f"  Total cost: ${preview['total_cost']:.2f}")
        print(f"  Date range: {preview['date_range']}")
        print(f"  By carrier: {preview['by_carrier']}")
        print(f"  By source: {preview['by_source']}")
        
        # Show breakdown
        breakdown = get_ebay_vs_direct_breakdown(file_path)
        print(f"\neBay vs Direct Breakdown:")
        print(f"  eBay: {breakdown['ebay']['count']} shipments, ${breakdown['ebay']['total_cost']:.2f} total")
        print(f"  Direct: {breakdown['direct']['count']} shipments, ${breakdown['direct']['total_cost']:.2f} total")
        
        # Ask to import
        response = input("\nImport all purchased labels? (y/n): ")
        if response.lower() == 'y':
            stats = import_pirate_ship_costs(file_path)
            print(f"\nImport Results:")
            print(f"  Imported: {stats['imported']}")
            print(f"  Duplicates: {stats['duplicates']}")
            print(f"  Matched to orders: {stats['matched_to_orders']}")
            print(f"  Unmatched: {stats['unmatched']}")
            if stats['errors']:
                print(f"  Errors: {len(stats['errors'])}")
    else:
        print("\nUsage: python import_pirateship.py <path_to_xlsx>")
        print("\nFunctions available:")
        print("  - import_pirate_ship_costs(file_path)")
        print("  - preview_import(file_path)")
        print("  - match_all_unmatched()")
        print("  - get_shipping_summary()")
        print("  - get_ebay_vs_direct_breakdown(file_path)")
