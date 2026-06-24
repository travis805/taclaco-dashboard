"""
TAG Grading CSV Import Module
Parses TAG grading report CSVs and imports graded cards to database
"""

import pandas as pd
from datetime import datetime
import database as db

def parse_tag_csv(file_path):
    """
    Parse TAG grading CSV export
    Returns: DataFrame with cleaned and standardized card data
    
    Expected columns from TAG (case-insensitive):
    - Item # / ITEM #
    - Card name / CARD NAME
    - Grade / GRADE
    - Grading report / GRADING REPORT (ignored)
    - Cert # / CERT #
    - Year / YEAR
    - Manufacturer / MANUFACTURER
    - Brand / BRAND
    - Card Set / CARD SET
    - Card no / CARD NO
    - Variation / VARIATION
    - Download media (ignored)
    
    Optional columns:
    - Purchase_ID (to auto-assign purchase IDs during import)
    """
    
    # Read CSV
    df = pd.read_csv(file_path)
    
    # Create a mapping of lowercase column names to actual column names
    col_mapping = {col.lower(): col for col in df.columns}
    
    # Helper function to get column name case-insensitively
    def get_col(name):
        """Get actual column name from case-insensitive match"""
        return col_mapping.get(name.lower())
    
    # Validate required columns (case-insensitive)
    required_cols = ['cert #', 'card name', 'grade']
    missing_cols = []
    for col in required_cols:
        if col not in col_mapping:
            missing_cols.append(col)
    
    if missing_cols:
        raise ValueError(f"CSV missing required columns: {missing_cols}. Found columns: {list(df.columns)}")
    
    # Clean and standardize columns (using actual column names from CSV)
    df['cert_number'] = df[get_col('cert #')].astype(str).str.strip()
    df['card_name'] = df[get_col('card name')].astype(str).str.strip()
    df['grade'] = df[get_col('grade')].astype(str).str.strip()
    
    # Optional columns with defaults
    year_col = get_col('year')
    if year_col:
        df['year'] = pd.to_numeric(df[year_col], errors='coerce').fillna(0).astype(int)
    else:
        df['year'] = 0
    
    manufacturer_col = get_col('manufacturer')
    df['manufacturer'] = df[manufacturer_col].astype(str).str.strip() if manufacturer_col else ''
    
    brand_col = get_col('brand')
    df['brand'] = df[brand_col].astype(str).str.strip() if brand_col else ''
    
    card_set_col = get_col('card set')
    df['card_set'] = df[card_set_col].astype(str).str.strip() if card_set_col else ''
    
    card_no_col = get_col('card no')
    df['card_number'] = df[card_no_col].astype(str).str.strip() if card_no_col else ''
    
    variation_col = get_col('variation')
    df['variation'] = df[variation_col].astype(str).str.strip() if variation_col else ''
    
    # Check for Purchase_ID column (case-sensitive for this one since user adds it)
    if 'Purchase_ID' in df.columns:
        df['purchase_id'] = df['Purchase_ID'].astype(str).str.strip()
        # Replace empty strings and 'nan' with None
        df['purchase_id'] = df['purchase_id'].replace(['', 'nan', 'NaN'], None)
    else:
        df['purchase_id'] = None
    
    # Remove any rows with empty cert numbers
    df = df[df['cert_number'] != '']
    df = df[df['cert_number'] != 'nan']
    
    return df

def import_tag_cards_to_batch(df, batch_id, purchase_id=None):
    """
    Import TAG cards into a grading batch
    
    Args:
        df: DataFrame from parse_tag_csv()
        batch_id: Grading batch ID to link cards to
        purchase_id: Optional purchase ID to assign to all cards (overridden by per-row purchase_id if present)
    
    Returns:
        dict with import statistics
    """
    
    stats = {
        'total_rows': len(df),
        'imported': 0,
        'duplicates': 0,
        'with_purchase_id': 0,
        'errors': []
    }
    
    for idx, row in df.iterrows():
        try:
            # Use row-level purchase_id if available, otherwise fall back to parameter
            card_purchase_id = None
            if 'purchase_id' in row and pd.notna(row['purchase_id']) and row['purchase_id']:
                card_purchase_id = row['purchase_id']
                stats['with_purchase_id'] += 1
            elif purchase_id:
                card_purchase_id = purchase_id
            
            card_id = db.add_graded_card(
                batch_id=batch_id,
                cert_number=row['cert_number'],
                card_name=row['card_name'],
                grade=row['grade'],
                year=row['year'] if row['year'] > 0 else None,
                manufacturer=row['manufacturer'] if row['manufacturer'] else None,
                brand=row['brand'] if row['brand'] else None,
                card_set=row['card_set'] if row['card_set'] else None,
                card_number=row['card_number'] if row['card_number'] else None,
                variation=row['variation'] if row['variation'] else None,
                purchase_id=card_purchase_id
            )
            
            if card_id:
                stats['imported'] += 1
            else:
                stats['duplicates'] += 1
                stats['errors'].append(f"Duplicate cert: {row['cert_number']}")
                
        except Exception as e:
            stats['errors'].append(f"Row {idx}: {str(e)}")
    
    # After import, recalculate batch costs and allocate to cards
    if stats['imported'] > 0:
        db.update_grading_batch_costs(batch_id)
    
    return stats

def detect_duplicate_certs(df):
    """
    Check for cert numbers that already exist in database
    Returns: DataFrame with duplicate info
    """
    
    duplicates = []
    
    for _, row in df.iterrows():
        existing = db.find_graded_card_by_cert(row['cert_number'])
        
        if existing:
            duplicates.append({
                'cert_number': row['cert_number'],
                'card_name': row['card_name'],
                'grade': row['grade'],
                'existing_batch': existing['batch_name'],
                'existing_status': existing.get('status', 'Unknown')
            })
    
    return pd.DataFrame(duplicates)

def extract_cert_from_title(title):
    """
    Extract TAG cert number from eBay listing title
    
    Common patterns:
    - "Pikachu TAG #12345678"
    - "Charizard [TAG 12345678]"
    - "Mew TAG Cert 12345678"
    - "TAG #A1234567"
    
    Returns: cert_number or None if not found
    """
    
    import re
    
    # Try various patterns
    patterns = [
        r'TAG\s*#?\s*([A-Z0-9]{8})',  # TAG #12345678 or TAG 12345678
        r'\[TAG\s+([A-Z0-9]{8})\]',    # [TAG 12345678]
        r'TAG\s+Cert\s+([A-Z0-9]{8})', # TAG Cert 12345678
        r'Cert\s*#?\s*([A-Z0-9]{8})',  # Cert #12345678
        r'#([A-Z0-9]{8})\b',           # #12345678 (fallback)
    ]
    
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    
    return None

def link_sales_to_graded_cards():
    """
    Automatically link sales to graded cards by extracting cert numbers from titles
    Returns: dict with linking statistics
    """
    
    # Get all sales that don't have grading_fee set yet
    sales_df = db.get_all_sales()
    unlinked_sales = sales_df[sales_df['grading_fee'] == 0]
    
    stats = {
        'total_sales': len(unlinked_sales),
        'linked': 0,
        'no_cert_found': 0,
        'cert_not_in_db': 0,
        'already_sold': 0
    }
    
    for _, sale in unlinked_sales.iterrows():
        # Try to extract cert from title
        cert = extract_cert_from_title(sale['item_title'])
        
        if not cert:
            stats['no_cert_found'] += 1
            continue
        
        # Check if this cert exists in graded_cards
        graded_card = db.find_graded_card_by_cert(cert)
        
        if not graded_card:
            stats['cert_not_in_db'] += 1
            continue
        
        # Check if card is already linked to another sale
        if graded_card['status'] == 'Sold':
            stats['already_sold'] += 1
            continue
        
        # Link the card to this sale
        success = db.link_graded_card_to_sale(cert, sale['sale_id'])
        
        if success:
            stats['linked'] += 1
    
    return stats

if __name__ == "__main__":
    # Test import with a sample file
    import sys
    
    if len(sys.argv) > 1:
        filepath = sys.argv[1]
        print(f"Parsing TAG CSV: {filepath}")
        
        df = parse_tag_csv(filepath)
        print(f"âœ… Parsed {len(df)} cards")
        print("\nPreview:")
        print(df[['cert_number', 'card_name', 'grade', 'year', 'card_set']].head())
        
        # Check for duplicates
        duplicates = detect_duplicate_certs(df)
        if not duplicates.empty:
            print(f"\nâš ï¸  Found {len(duplicates)} duplicate cert numbers")
            print(duplicates)
    else:
        print("Usage: python import_tag.py <path_to_tag_csv>")
