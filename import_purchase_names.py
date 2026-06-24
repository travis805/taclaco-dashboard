#!/usr/bin/env python3
"""
Import purchase IDs and display names from CSV into taclaco.db

Usage:
    python import_purchase_names.py <csv_file>

Example:
    python import_purchase_names.py taclaco_purchase_id_mappings.csv
"""

import sqlite3
import pandas as pd
import sys
from pathlib import Path


def get_db_path():
    """Find taclaco.db in current or parent directories"""
    current = Path.cwd()
    
    # Check current directory
    if (current / 'taclaco.db').exists():
        return current / 'taclaco.db'
    
    # Check parent directory (common if running from scripts folder)
    if (current.parent / 'taclaco.db').exists():
        return current.parent / 'taclaco.db'
    
    # Check common iCloud Drive location
    icloud_path = Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs/Taclaco Dashboard/taclaco.db'
    if icloud_path.exists():
        return icloud_path
    
    raise FileNotFoundError("Could not find taclaco.db in current directory, parent directory, or iCloud Drive")


def import_purchase_names(csv_file):
    """Import purchase IDs and display names from CSV"""
    
    # Read CSV
    csv_path = Path(csv_file)
    if not csv_path.exists():
        print(f"❌ Error: {csv_file} not found")
        return False
    
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"❌ Error reading CSV: {e}")
        return False
    
    # Validate columns
    required_cols = {'purchase_id', 'display_name'}
    if not required_cols.issubset(df.columns):
        print(f"❌ Error: CSV must contain columns: {required_cols}")
        print(f"   Found: {set(df.columns)}")
        return False
    
    # Find database
    try:
        db_path = get_db_path()
        print(f"📁 Using database: {db_path}")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return False
    
    # Connect to database
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
    except Exception as e:
        print(f"❌ Error connecting to database: {e}")
        return False
    
    # Check if purchases table exists
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='purchases'")
        if not cursor.fetchone():
            print("❌ Error: 'purchases' table not found in database")
            conn.close()
            return False
    except Exception as e:
        print(f"❌ Error checking table: {e}")
        conn.close()
        return False
    
    # Process each row
    inserted = 0
    updated = 0
    skipped = 0
    errors = 0
    
    print(f"\n📊 Processing {len(df)} rows from CSV...\n")
    
    for idx, row in df.iterrows():
        purchase_id = str(row['purchase_id']).strip()
        display_name = str(row['display_name']).strip()
        location = str(row['location']).strip() if 'location' in df.columns else None
        
        # Skip empty rows
        if not purchase_id or purchase_id.lower() == 'nan':
            skipped += 1
            continue
        
        try:
            # Check if purchase_id already exists
            cursor.execute("SELECT purchase_id FROM purchases WHERE purchase_id = ?", (purchase_id,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing record
                cursor.execute(
                    "UPDATE purchases SET display_name = ? WHERE purchase_id = ?",
                    (display_name, purchase_id)
                )
                updated += 1
                print(f"  ✏️  {purchase_id}: Updated display name")
            else:
                # Insert new record
                cursor.execute(
                    "INSERT INTO purchases (purchase_id, display_name, location) VALUES (?, ?, ?)",
                    (purchase_id, display_name, location)
                )
                inserted += 1
                print(f"  ✅ {purchase_id}: Created with display name '{display_name}'")
        
        except Exception as e:
            errors += 1
            print(f"  ❌ {purchase_id}: Error - {e}")
    
    # Commit changes
    try:
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error saving changes: {e}")
        return False
    
    # Print summary
    print(f"\n{'='*60}")
    print(f"Import Summary:")
    print(f"{'='*60}")
    print(f"✅ Inserted:  {inserted} new purchases")
    print(f"✏️  Updated:   {updated} existing purchases")
    print(f"⏭️  Skipped:   {skipped} empty rows")
    print(f"❌ Errors:    {errors}")
    print(f"{'='*60}")
    print(f"\n✨ Import complete! Your dashboard is ready to use.\n")
    
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_purchase_names.py <csv_file>")
        print("Example: python import_purchase_names.py taclaco_purchase_id_mappings.csv")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    success = import_purchase_names(csv_file)
    sys.exit(0 if success else 1)
