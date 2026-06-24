"""
Import eBay Purchases from reviewed CSV/Excel file

This script imports eBay purchase history into the Taclaco dashboard database.
It handles:
- Inventory purchases (with purchase_id) → purchases table
- Operating expenses (no purchase_id) → chase_transactions table as expenses
- Multiple rows per purchase_id are consolidated
- PC categories (PC, PC-LIFE, PC-TAR, PC-ZAP) for personal collection tracking

Usage:
    python import_ebay_purchases.py <path_to_file>
"""

import pandas as pd
import sys
import os

# Add parent directory to path for database import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db


def load_ebay_purchases(filepath):
    """Load the reviewed eBay purchases file"""
    if filepath.endswith('.xlsx'):
        df = pd.read_excel(filepath)
    else:
        df = pd.read_csv(filepath)
    
    # Ensure purchase_id is string and handle NaN
    df['purchase_id'] = df['purchase_id'].fillna('').astype(str).str.strip()
    
    # Ensure amount_usd is numeric
    df['amount_usd'] = pd.to_numeric(df['amount_usd'], errors='coerce').fillna(0)
    
    return df


def import_purchases(df):
    """
    Import purchases into the database.
    
    - Rows WITH purchase_id → purchases table (consolidated by purchase_id)
    - Rows WITHOUT purchase_id → chase_transactions as expenses
    """
    
    # Separate inventory purchases from operating expenses
    inventory_df = df[df['purchase_id'] != ''].copy()
    expenses_df = df[df['purchase_id'] == ''].copy()
    
    print(f"\n📦 Inventory purchases: {len(inventory_df)} rows")
    print(f"💰 Operating expenses: {len(expenses_df)} rows")
    
    # --- IMPORT INVENTORY PURCHASES ---
    # Consolidate by purchase_id (sum amounts, combine descriptions)
    if len(inventory_df) > 0:
        consolidated = inventory_df.groupby('purchase_id').agg({
            'order_date': 'min',  # Earliest date
            'seller': lambda x: ', '.join(x.unique()),
            'item_description': lambda x: ' | '.join(x.astype(str).head(3)),  # First 3 items
            'ebay_order_number': lambda x: ', '.join(x.unique()),
            'amount_usd': 'sum',
            'display_name': 'first',
            'gl_account': 'first',
            'payment_method': lambda x: ', '.join(x.unique()),
            'notes': 'first'
        }).reset_index()
        
        print(f"\n📋 Consolidated to {len(consolidated)} unique purchase_ids")
        
        # Import each purchase
        imported = 0
        skipped = 0
        updated = 0
        
        for _, row in consolidated.iterrows():
            purchase_id = row['purchase_id']
            
            # Check if purchase already exists
            existing = db.get_purchase_by_id(purchase_id)
            
            if existing is not None and len(existing) > 0:
                # Update existing purchase with new total
                print(f"  ⚠️  {purchase_id} exists - updating total cost")
                _conn_u = db.get_connection()
                _cur_u = _conn_u.cursor()
                _cur_u.execute("""
                    UPDATE purchases 
                    SET total_cost = total_cost + ?,
                        notes = COALESCE(notes, '') || ' | eBay import: ' || ?
                    WHERE purchase_id = ?
                """, (row['amount_usd'], row['ebay_order_number'], purchase_id))
                _conn_u.commit()
                _conn_u.close()
                updated += 1
            else:
                # Create new purchase
                try:
                    db.add_purchase(
                        purchase_id=purchase_id,
                        date=row['order_date'],
                        description=row['item_description'][:500],  # Truncate long descriptions
                        location=f"eBay: {row['seller'][:100]}",
                        order_number=row['ebay_order_number'],
                        total_cost=row['amount_usd'],
                        display_name=row['display_name'] if pd.notna(row['display_name']) else None,
                        notes=f"Payment: {row['payment_method']}",
                        gl_account=row['gl_account'] if pd.notna(row['gl_account']) else None
                    )
                    print(f"  ✅ {purchase_id}: ${row['amount_usd']:.2f} - {row['display_name']}")
                    imported += 1
                except Exception as e:
                    print(f"  ❌ {purchase_id}: Error - {e}")
                    skipped += 1
        
        print(f"\n📦 Inventory Import Summary:")
        print(f"   Imported: {imported}")
        print(f"   Updated: {updated}")
        print(f"   Skipped: {skipped}")
    
    # --- IMPORT OPERATING EXPENSES ---
    if len(expenses_df) > 0:
        print(f"\n💰 Importing {len(expenses_df)} operating expenses...")
        
        expense_imported = 0
        conn = db.get_connection()
        cursor = conn.cursor()
        for _, row in expenses_df.iterrows():
            try:
                # Insert into chase_transactions as an expense
                cursor.execute("""
                    INSERT INTO chase_transactions (
                        card_last_four,
                        transaction_date,
                        post_date,
                        description,
                        clean_merchant_name,
                        chase_category,
                        transaction_type,
                        amount,
                        expense_category,
                        import_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    'EBAY',  # Card identifier
                    row['order_date'],
                    row['order_date'],
                    row['item_description'][:200],
                    row['seller'],
                    'eBay Purchase',
                    'Sale',
                    -abs(row['amount_usd']),  # Negative for expense
                    row['gl_account'] if pd.notna(row['gl_account']) else 'Office Supplies'
                ))
                print(f"  OK: ${row['amount_usd']:.2f} - {row['seller'][:30]} - {row['item_description'][:40]}")
                expense_imported += 1
                
            except Exception as e:
                print(f"  Error: {e}")
        conn.commit()
        conn.close()
        
        print(f"\n💰 Expense Import Summary: {expense_imported} imported")
    
    return True


def main():
    """Main import function"""
    # Default to the ready-for-import file
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
    else:
        # Try common locations
        possible_paths = [
            '/mnt/user-data/outputs/ebay_purchases_ready_for_import.csv',
            '/mnt/user-data/outputs/ebay_purchases_ready_for_import.xlsx',
            'ebay_purchases_ready_for_import.csv',
            'ebay_purchases_ready_for_import.xlsx'
        ]
        filepath = None
        for p in possible_paths:
            if os.path.exists(p):
                filepath = p
                break
        
        if not filepath:
            print("❌ No input file found. Usage: python import_ebay_purchases.py <filepath>")
            return False
    
    print(f"📂 Loading: {filepath}")
    
    # Initialize database
    db.init_database()
    
    # Load and import
    df = load_ebay_purchases(filepath)
    print(f"📊 Loaded {len(df)} rows")
    
    # Show summary before import
    print("\n=== PRE-IMPORT SUMMARY ===")
    df['purchase_id_clean'] = df['purchase_id'].replace('', '(expenses)')
    summary = df.groupby('purchase_id_clean')['amount_usd'].agg(['count', 'sum']).round(2)
    summary.columns = ['rows', 'total_usd']
    print(summary.to_string())
    print(f"\nGrand Total: ${df['amount_usd'].sum():,.2f}")
    
    # Confirm
    response = input("\n⚠️  Proceed with import? (y/n): ")
    if response.lower() != 'y':
        print("Import cancelled.")
        return False
    
    # Import
    import_purchases(df)
    
    print("\n✅ Import complete!")
    return True


if __name__ == "__main__":
    main()
