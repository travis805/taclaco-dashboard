"""
Stripe Import Module
Pulls payment data from Stripe API and imports to order_fulfillment table
Supports both Payment Intents (from Payment Links) and paid Invoices
"""

import requests
from datetime import datetime
import database as db

# Stripe API base URL
STRIPE_API_BASE = "https://api.stripe.com/v1"


def get_stripe_api_key():
    """
    Get Stripe API key from settings table
    Returns: API key string or None if not set
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'stripe_api_key'")
    result = cursor.fetchone()
    conn.close()
    return result['value'] if result else None


def save_stripe_api_key(api_key):
    """Save Stripe API key to settings table"""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO settings (key, value, updated_at)
        VALUES ('stripe_api_key', ?, CURRENT_TIMESTAMP)
    """, (api_key,))
    conn.commit()
    conn.close()


def fetch_product_details(api_key, product_id):
    """
    Fetch product details from Stripe Products API
    Returns product name or None if not found
    """
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.get(
            f"{STRIPE_API_BASE}/products/{product_id}",
            headers=headers
        )
        
        if response.status_code == 200:
            product = response.json()
            return product.get('name', None)
        else:
            return None
    except Exception:
        return None


def fetch_payment_intents(api_key, limit=100, starting_after=None):
    """
    Fetch Payment Intents from Stripe API
    These come from Payment Links
    Expands latest_charge.balance_transaction to get actual fees
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "limit": limit,
    }
    
    # Need to pass expand as multiple params for Stripe API
    # expand[]: data.latest_charge.balance_transaction gets the actual fee
    # expand[]: data.latest_charge.invoice.lines gets line items
    
    if starting_after:
        params["starting_after"] = starting_after
    
    response = requests.get(
        f"{STRIPE_API_BASE}/payment_intents",
        headers=headers,
        params={
            **params,
            "expand[]": ["data.latest_charge.balance_transaction", "data.latest_charge.invoice.lines"]
        }
    )
    
    if response.status_code != 200:
        raise Exception(f"Stripe API error: {response.status_code} - {response.text}")
    
    return response.json()


def fetch_checkout_session(api_key, payment_intent_id):
    """
    Fetch checkout session for a payment intent
    Returns session data with line items or None if not found
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        response = requests.get(
            f"{STRIPE_API_BASE}/checkout/sessions",
            headers=headers,
            params={"payment_intent": payment_intent_id}
        )
        
        if response.status_code == 200:
            sessions = response.json().get('data', [])
            if sessions:
                return sessions[0]  # Return first session
        return None
    except Exception:
        return None


def fetch_checkout_line_items(api_key, session_id):
    """
    Fetch line items for a checkout session
    Returns list of line items with full product details
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        response = requests.get(
            f"{STRIPE_API_BASE}/checkout/sessions/{session_id}/line_items",
            headers=headers,
            params={"expand[]": ["data.price.product"]}
        )
        
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception:
        return []


def fetch_and_store_stripe_line_items(api_key, payment_intent_id):
    """
    Fetch checkout session and line items for a payment, store in database
    
    Returns:
        Dict with session totals and list of line items, or None if not found
    """
    # Get checkout session
    session = fetch_checkout_session(api_key, payment_intent_id)
    if not session:
        return None
    
    session_id = session.get('id')
    
    # Get line items
    line_items = fetch_checkout_line_items(api_key, session_id)
    if not line_items:
        return None
    
    # Extract totals from session
    total_details = session.get('total_details', {})
    
    order_totals = {
        'payment_intent_id': payment_intent_id,
        'checkout_session_id': session_id,
        'amount_subtotal': session.get('amount_subtotal', 0) / 100,
        'amount_total': session.get('amount_total', 0) / 100,
        'amount_discount': total_details.get('amount_discount', 0) / 100,
        'amount_shipping': total_details.get('amount_shipping', 0) / 100,
        'amount_tax': total_details.get('amount_tax', 0) / 100,
        'currency': session.get('currency', 'usd'),
        'customer_name': session.get('customer_details', {}).get('name'),
        'customer_email': session.get('customer_details', {}).get('email'),
    }
    
    # Store order totals
    db.add_stripe_order_totals(order_totals)
    
    # Process and store line items
    parsed_items = []
    for item in line_items:
        price = item.get('price', {})
        product = price.get('product', {}) if isinstance(price.get('product'), dict) else {}
        
        line_item_data = {
            'line_item_id': item.get('id'),
            'payment_intent_id': payment_intent_id,
            'checkout_session_id': session_id,
            'stripe_product_id': product.get('id') if product else price.get('product'),
            'product_name': product.get('name') if product else item.get('description'),
            'description': item.get('description'),
            'quantity': item.get('quantity', 1),
            'unit_amount': price.get('unit_amount', 0) / 100,
            'amount_subtotal': item.get('amount_subtotal', 0) / 100,
            'amount_total': item.get('amount_total', 0) / 100,
            'amount_discount': item.get('amount_discount', 0) / 100,
            'amount_tax': item.get('amount_tax', 0) / 100,
            'currency': price.get('currency', 'usd')
        }
        
        db.add_stripe_line_item(line_item_data)
        parsed_items.append(line_item_data)
        
        # Auto-add to product mapping if not already there (with UNKNOWN purchase_id)
        stripe_product_id = line_item_data.get('stripe_product_id')
        if stripe_product_id:
            existing_mapping = db.get_stripe_product_mapping(stripe_product_id)
            if not existing_mapping:
                db.add_stripe_product_mapping(
                    stripe_product_id,
                    'UNKNOWN',
                    line_item_data.get('product_name')
                )
    
    return {
        'order_totals': order_totals,
        'line_items': parsed_items
    }


def fetch_invoices(api_key, limit=100, starting_after=None):
    """
    Fetch paid Invoices from Stripe API
    Expands payment_intent.latest_charge.balance_transaction for actual fees
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "limit": limit,
        "status": "paid",
    }
    
    if starting_after:
        params["starting_after"] = starting_after
    
    response = requests.get(
        f"{STRIPE_API_BASE}/invoices",
        headers=headers,
        params={
            **params,
            "expand[]": ["data.payment_intent.latest_charge.balance_transaction", "data.charge.balance_transaction"]
        }
    )
    
    if response.status_code != 200:
        raise Exception(f"Stripe API error: {response.status_code} - {response.text}")
    
    return response.json()


def parse_payment_intent(intent):
    """
    Parse a Stripe Payment Intent into order_fulfillment format
    Returns: dict ready for database insert, or None if should be skipped
    """
    # Only include succeeded payments
    if intent.get('status') != 'succeeded':
        return None
    
    # Skip generic "Payment for Invoice" - these are invoice payments
    if intent.get('description') == 'Payment for Invoice':
        return None
    
    # Extract shipping address
    shipping = intent.get('shipping') or {}
    address = shipping.get('address') or {}
    
    # Extract billing details as fallback
    charges = intent.get('charges', {}).get('data', [])
    billing = charges[0].get('billing_details', {}) if charges else {}
    
    # Get customer email - try multiple sources
    customer_email = (
        intent.get('receipt_email') or 
        billing.get('email') or 
        intent.get('metadata', {}).get('email') or
        ''
    )
    
    # Get customer name
    customer_name = (
        shipping.get('name') or 
        billing.get('name') or 
        ''
    )
    
    # Parse creation timestamp
    created_timestamp = intent.get('created', 0)
    order_date = datetime.fromtimestamp(created_timestamp).strftime('%Y-%m-%d')
    
    # Check if already shipped (from React dashboard metadata)
    metadata = intent.get('metadata', {})
    is_shipped = metadata.get('shipped') == 'true'
    tracking_number = metadata.get('tracking_number', '')
    carrier = metadata.get('carrier', '')
    shipped_date = metadata.get('shipped_date', '')
    
    # Determine fulfillment status
    if is_shipped and tracking_number:
        fulfillment_status = 'Shipped'
    else:
        fulfillment_status = 'Pending'
    
    # Extract product description - try multiple methods
    item_description = intent.get('description') or ''
    quantity = 1
    
    # Method 1: Check payment_details for product reference (Payment Links)
    payment_details = intent.get('payment_details', {})
    product_id = payment_details.get('order_reference')
    
    if product_id and product_id.startswith('prod_'):
        # Fetch product name from Stripe Products API
        # Note: This requires the API key to be passed in or retrieved
        # For now, we'll store the product_id and fetch it during import
        item_description = f"PRODUCT_ID:{product_id}"  # Placeholder - will be replaced during import
    
    # Method 2: Try to get line items from invoice (Custom Invoices)
    if not item_description or item_description.startswith('PRODUCT_ID:'):
        if charges:
            latest_charge = charges[0]
            invoice = latest_charge.get('invoice')
            if invoice and isinstance(invoice, dict):
                lines = invoice.get('lines', {}).get('data', [])
                if lines:
                    # Concatenate all product descriptions
                    descriptions = []
                    total_qty = 0
                    for line in lines:
                        desc = line.get('description', '')
                        qty = line.get('quantity', 1)
                        if desc:
                            if qty > 1:
                                descriptions.append(f"{desc} (x{qty})")
                            else:
                                descriptions.append(desc)
                            total_qty += qty
                    
                    if descriptions:
                        item_description = ', '.join(descriptions)
                        quantity = total_qty
    
    # If still a product ID placeholder, we'll resolve it later
    # If completely empty, use fallback
    if not item_description:
        item_description = 'TCG Product'
    
    # Extract actual Stripe fee from balance_transaction
    stripe_fee = 0
    latest_charge = intent.get('latest_charge')
    if latest_charge and isinstance(latest_charge, dict):
        balance_txn = latest_charge.get('balance_transaction')
        if balance_txn and isinstance(balance_txn, dict):
            # Fee is in cents, convert to dollars
            stripe_fee = balance_txn.get('fee', 0) / 100
    
    # Build order record
    return {
        'source': 'Stripe',
        'source_order_id': intent['id'],
        'order_date': order_date,
        'customer_name': customer_name,
        'customer_email': customer_email,
        'shipping_name': shipping.get('name') or customer_name,
        'shipping_address_line1': address.get('line1') or '',
        'shipping_address_line2': address.get('line2') or '',
        'shipping_city': address.get('city') or '',
        'shipping_state': address.get('state') or '',
        'shipping_zip': address.get('postal_code') or '',
        'shipping_country': address.get('country') or 'US',
        'item_description': item_description,
        'quantity': quantity,
        'order_total': intent.get('amount', 0) / 100,  # Stripe uses cents
        'platform_fee': stripe_fee,  # Actual fee from Stripe
        'notes': metadata.get('customer_notes') or '',
        # Shipping info from metadata
        'fulfillment_status': fulfillment_status,
        'tracking_number': tracking_number,
        'carrier': carrier,
        'ship_date': shipped_date if shipped_date else None
    }


def parse_invoice(invoice):
    """
    Parse a Stripe Invoice into order_fulfillment format
    Returns: dict ready for database insert
    """
    # Extract shipping address
    shipping = invoice.get('customer_shipping') or {}
    address = shipping.get('address') or {}
    
    # Get line items description
    lines = invoice.get('lines', {}).get('data', [])
    description = ', '.join([
        line.get('description', 'Item') 
        for line in lines 
        if line.get('description')
    ]) or 'Invoice'
    
    # Get quantity from line items
    quantity = sum(line.get('quantity', 1) for line in lines)
    
    # Parse creation timestamp
    created_timestamp = invoice.get('created', 0)
    order_date = datetime.fromtimestamp(created_timestamp).strftime('%Y-%m-%d')
    
    # Check if already shipped (from React dashboard metadata)
    metadata = invoice.get('metadata', {})
    is_shipped = metadata.get('shipped') == 'true'
    tracking_number = metadata.get('tracking_number', '')
    carrier = metadata.get('carrier', '')
    shipped_date = metadata.get('shipped_date', '')
    
    # Determine fulfillment status
    if is_shipped and tracking_number:
        fulfillment_status = 'Shipped'
    else:
        fulfillment_status = 'Pending'
    
    # Extract actual Stripe fee from balance_transaction
    stripe_fee = 0
    
    # Try payment_intent.latest_charge.balance_transaction first
    payment_intent = invoice.get('payment_intent')
    if payment_intent and isinstance(payment_intent, dict):
        latest_charge = payment_intent.get('latest_charge')
        if latest_charge and isinstance(latest_charge, dict):
            balance_txn = latest_charge.get('balance_transaction')
            if balance_txn and isinstance(balance_txn, dict):
                stripe_fee = balance_txn.get('fee', 0) / 100
    
    # Fallback: try charge.balance_transaction
    if stripe_fee == 0:
        charge = invoice.get('charge')
        if charge and isinstance(charge, dict):
            balance_txn = charge.get('balance_transaction')
            if balance_txn and isinstance(balance_txn, dict):
                stripe_fee = balance_txn.get('fee', 0) / 100
    
    # Build order record
    return {
        'source': 'Stripe',
        'source_order_id': invoice['id'],
        'order_date': order_date,
        'customer_name': invoice.get('customer_name') or shipping.get('name') or '',
        'customer_email': invoice.get('customer_email') or '',
        'shipping_name': shipping.get('name') or invoice.get('customer_name') or '',
        'shipping_address_line1': address.get('line1') or '',
        'shipping_address_line2': address.get('line2') or '',
        'shipping_city': address.get('city') or '',
        'shipping_state': address.get('state') or '',
        'shipping_zip': address.get('postal_code') or '',
        'shipping_country': address.get('country') or 'US',
        'item_description': description,
        'quantity': quantity,
        'order_total': invoice.get('amount_paid', 0) / 100,  # Stripe uses cents
        'platform_fee': stripe_fee,  # Actual fee from Stripe
        'notes': metadata.get('customer_notes') or '',
        # Shipping info from metadata
        'fulfillment_status': fulfillment_status,
        'tracking_number': tracking_number,
        'carrier': carrier,
        'ship_date': shipped_date if shipped_date else None
    }


def import_stripe_orders(api_key=None, days_back=30):
    """
    Import orders from Stripe API
    
    Args:
        api_key: Stripe secret key (if None, reads from settings)
        days_back: How many days of history to fetch (default 30)
    
    Returns:
        dict with import statistics
    """
    if api_key is None:
        api_key = get_stripe_api_key()
    
    if not api_key:
        raise ValueError("No Stripe API key configured. Set it in dashboard settings.")
    
    stats = {
        'payment_intents_fetched': 0,
        'invoices_fetched': 0,
        'orders_imported': 0,
        'orders_updated': 0,
        'orders_skipped_duplicate': 0,
        'orders_skipped_filtered': 0,
        'line_items_stored': 0,
        'errors': []
    }
    
    # Calculate cutoff date
    cutoff_timestamp = int((datetime.now().timestamp()) - (days_back * 24 * 60 * 60))
    
    # Fetch Payment Intents
    try:
        pi_data = fetch_payment_intents(api_key)
        payment_intents = pi_data.get('data', [])
        stats['payment_intents_fetched'] = len(payment_intents)
        
        # Process Payment Intents
        for intent in payment_intents:
            # Skip if too old
            if intent.get('created', 0) < cutoff_timestamp:
                continue
            
            order_data = parse_payment_intent(intent)
            
            if order_data is None:
                stats['orders_skipped_filtered'] += 1
                continue
            
            payment_intent_id = intent['id']
            
            # Check if already exists
            existing = db.get_order_by_source_id('Stripe', payment_intent_id)
            if existing:
                # Check if we need to update shipping status
                if order_data.get('fulfillment_status') == 'Shipped' and existing['fulfillment_status'] != 'Shipped':
                    # Update the existing order with shipping info
                    db.update_order_status(
                        existing['fulfillment_id'],
                        'Shipped',
                        tracking_number=order_data.get('tracking_number'),
                        carrier=order_data.get('carrier'),
                        ship_date=order_data.get('ship_date')
                    )
                    stats['orders_updated'] += 1
                else:
                    stats['orders_skipped_duplicate'] += 1
                
                # ALWAYS try to fetch line items for existing orders (even if duplicate)
                # This populates stripe_line_items for orders imported before this feature existed
                try:
                    line_item_result = fetch_and_store_stripe_line_items(api_key, payment_intent_id)
                    if line_item_result:
                        stats['line_items_stored'] += len(line_item_result.get('line_items', []))
                except Exception as li_error:
                    # Don't fail if line items fail
                    pass
                
                continue
            
            # Insert new order
            try:
                db.add_order(**order_data)
                stats['orders_imported'] += 1
                
                # Fetch and store line items from checkout session
                try:
                    line_item_result = fetch_and_store_stripe_line_items(api_key, payment_intent_id)
                    if line_item_result:
                        stats['line_items_stored'] += len(line_item_result.get('line_items', []))
                except Exception as li_error:
                    # Don't fail the whole import if line items fail
                    stats['errors'].append(f"Line items for {payment_intent_id}: {str(li_error)}")
                    
            except Exception as e:
                stats['errors'].append(f"Payment Intent {payment_intent_id}: {str(e)}")
    
    except Exception as e:
        stats['errors'].append(f"Payment Intents fetch error: {str(e)}")
    
    # Fetch Invoices
    try:
        inv_data = fetch_invoices(api_key)
        invoices = inv_data.get('data', [])
        stats['invoices_fetched'] = len(invoices)
        
        # Process Invoices
        for invoice in invoices:
            # Skip if too old
            if invoice.get('created', 0) < cutoff_timestamp:
                continue
            
            order_data = parse_invoice(invoice)
            
            # Check if already exists
            existing = db.get_order_by_source_id('Stripe', invoice['id'])
            if existing:
                # Check if we need to update shipping status
                if order_data.get('fulfillment_status') == 'Shipped' and existing['fulfillment_status'] != 'Shipped':
                    # Update the existing order with shipping info
                    db.update_order_status(
                        existing['fulfillment_id'],
                        'Shipped',
                        tracking_number=order_data.get('tracking_number'),
                        carrier=order_data.get('carrier'),
                        ship_date=order_data.get('ship_date')
                    )
                    stats['orders_updated'] += 1
                else:
                    stats['orders_skipped_duplicate'] += 1
                continue
            
            # Insert new order
            try:
                db.add_order(**order_data)
                stats['orders_imported'] += 1
            except Exception as e:
                stats['errors'].append(f"Invoice {invoice['id']}: {str(e)}")
    
    except Exception as e:
        stats['errors'].append(f"Invoices fetch error: {str(e)}")
    
    # Resolve product IDs to product names
    # Find all orders with placeholder product IDs
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT fulfillment_id, item_description
        FROM order_fulfillment
        WHERE source = 'Stripe'
        AND item_description LIKE 'PRODUCT_ID:%'
    """)
    product_id_orders = cursor.fetchall()
    
    resolved_count = 0
    for order_row in product_id_orders:
        fulfillment_id = order_row[0]
        item_desc = order_row[1]
        
        # Extract product ID
        product_id = item_desc.replace('PRODUCT_ID:', '')
        
        # Fetch product name
        product_name = fetch_product_details(api_key, product_id)
        
        if product_name:
            # Update the order with the actual product name
            cursor.execute("""
                UPDATE order_fulfillment
                SET item_description = ?
                WHERE fulfillment_id = ?
            """, (product_name, fulfillment_id))
            resolved_count += 1
    
    conn.commit()
    conn.close()
    
    if resolved_count > 0:
        stats['products_resolved'] = resolved_count
    
    # Log the import
    latest_date = None
    orders = db.get_all_orders()
    if not orders.empty:
        stripe_orders = orders[orders['source'] == 'Stripe']
        if not stripe_orders.empty:
            latest_date = stripe_orders['order_date'].max()
    
    db.log_data_import(
        source='Stripe',
        records_imported=stats['orders_imported'],
        records_skipped=stats['orders_skipped_duplicate'] + stats['orders_skipped_filtered'],
        latest_transaction_date=latest_date,
        import_status='Success' if not stats['errors'] else 'Partial',
        error_message='; '.join(stats['errors']) if stats['errors'] else None
    )
    
    # Update data source status
    db.recalculate_data_source_status()
    
    return stats


def get_unfulfilled_orders():
    """
    Get Stripe orders that haven't been shipped yet
    Returns: DataFrame of pending orders
    """
    return db.get_pending_orders()


def mark_order_shipped(fulfillment_id, tracking_number, carrier='USPS'):
    """
    Mark an order as shipped with tracking info
    
    Args:
        fulfillment_id: Database ID of the order
        tracking_number: Shipping tracking number
        carrier: Carrier name (USPS, UPS, FedEx)
    """
    db.update_order_tracking(fulfillment_id, tracking_number, carrier)


def update_stripe_metadata(source_order_id, tracking_number, carrier='USPS'):
    """
    Update Stripe Payment Intent or Invoice metadata with shipping info.
    Pushes tracking number back to Stripe so it shows in the Stripe dashboard.
    
    Args:
        source_order_id: The Stripe Payment Intent ID (pi_xxx) or Invoice ID (in_xxx)
        tracking_number: Shipping tracking number
        carrier: Carrier name (USPS, UPS, FedEx)
    
    Returns:
        tuple (success: bool, message: str)
    """
    api_key = get_stripe_api_key()
    if not api_key:
        return False, "No Stripe API key configured"
    
    try:
        # Determine if this is a Payment Intent or Invoice based on ID prefix
        if source_order_id.startswith('in_'):
            endpoint = f"{STRIPE_API_BASE}/invoices/{source_order_id}"
        else:
            endpoint = f"{STRIPE_API_BASE}/payment_intents/{source_order_id}"
        
        # Update metadata with shipping info
        response = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            data={
                'metadata[tracking_number]': tracking_number,
                'metadata[carrier]': carrier,
                'metadata[shipped]': 'true',
                'metadata[shipped_date]': datetime.now().strftime('%Y-%m-%d')
            }
        )
        
        if response.status_code == 200:
            return True, "Stripe metadata updated"
        else:
            return False, f"Stripe API error: {response.status_code}"
    
    except Exception as e:
        return False, f"Error updating Stripe: {str(e)}"


def mark_order_shipped_with_stripe_sync(fulfillment_id, tracking_number, carrier='USPS'):
    """
    Mark an order as shipped and sync tracking to Stripe.
    
    Args:
        fulfillment_id: Database ID of the order
        tracking_number: Shipping tracking number  
        carrier: Carrier name (USPS, UPS, FedEx)
    
    Returns:
        tuple (success: bool, message: str)
    """
    # First update local database
    db.update_order_tracking(fulfillment_id, tracking_number, carrier)
    
    # Get the source_order_id to update Stripe
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_order_id, source FROM order_fulfillment 
        WHERE fulfillment_id = ?
    """, (fulfillment_id,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        return True, "Local database updated (order not found for Stripe sync)"
    
    source_order_id = result['source_order_id']
    source = result['source']
    
    # Only sync to Stripe for Stripe orders
    if source != 'Stripe':
        return True, "Local database updated (non-Stripe order)"
    
    # Update Stripe metadata
    success, message = update_stripe_metadata(source_order_id, tracking_number, carrier)
    
    if success:
        return True, "Shipped! Tracking synced to Stripe."
    else:
        return True, f"Shipped locally. Stripe sync failed: {message}"


def export_orders_for_pirate_ship(order_ids):
    """
    Export selected orders as CSV for Pirate Ship batch upload
    
    Args:
        order_ids: List of fulfillment_ids to export
    
    Returns:
        List of dicts ready for CSV export
    """
    return db.generate_pirate_ship_csv(order_ids)


def test_stripe_connection(api_key):
    """
    Test if Stripe API key is valid
    Returns: tuple (success: bool, message: str)
    """
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        response = requests.get(
            f"{STRIPE_API_BASE}/balance",
            headers=headers
        )
        
        if response.status_code == 200:
            return True, "Connection successful!"
        elif response.status_code == 401:
            return False, "Invalid API key"
        else:
            return False, f"API error: {response.status_code}"
    
    except Exception as e:
        return False, f"Connection error: {str(e)}"


if __name__ == "__main__":
    # Test the module
    print("Stripe Import Module")
    print("=" * 40)
    
    api_key = get_stripe_api_key()
    if api_key:
        print(f"API Key configured: {api_key[:8]}...")
        success, message = test_stripe_connection(api_key)
        print(f"Connection test: {message}")
        
        if success:
            print("\nImporting orders...")
            stats = import_stripe_orders()
            print(f"Results: {stats}")
    else:
        print("No API key configured.")
        print("Use save_stripe_api_key('sk_...') to configure.")


def process_stripe_to_sales():
    """
    Process Stripe orders from order_fulfillment table into the sales table.
    
    This function:
    1. Reads from order_fulfillment where source = 'Stripe'
    2. Checks stripe_line_items for individual products in the order
    3. Creates SEPARATE sales records for EACH line item (for multi-product orders)
    4. Distributes fees proportionally across line items
    5. Maps Stripe product IDs to purchase IDs using stripe_product_mapping
    
    Returns:
        Dict with processing statistics
    """
    stats = {
        'orders_processed': 0,
        'sales_created': 0,
        'sales_from_line_items': 0,
        'sales_skipped_duplicate': 0,
        'unmapped_products': [],
        'errors': []
    }
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    try:
        # Get all Stripe orders from order_fulfillment
        cursor.execute("""
            SELECT * FROM order_fulfillment
            WHERE source = 'Stripe'
            ORDER BY order_date DESC
        """)
        orders = cursor.fetchall()
        
        for order in orders:
            source_order_id = order['source_order_id']
            order_date = order['order_date']
            order_total = order['order_total'] or 0
            tracking_number = order['tracking_number']
            
            # Get actual Stripe fee from order_fulfillment
            platform_fee = order['platform_fee'] if 'platform_fee' in order.keys() else 0
            
            stats['orders_processed'] += 1
            
            # Check if this order already has ANY sales
            cursor.execute("""
                SELECT COUNT(*) as count FROM sales
                WHERE platform = 'Stripe' AND order_number = ?
            """, (source_order_id,))
            existing_count = cursor.fetchone()['count']
            
            if existing_count > 0:
                stats['sales_skipped_duplicate'] += 1
                continue
            
            # Try to get shipping cost
            shipping_cost = 0
            if tracking_number and tracking_number not in ('', 'N/A', 'NA', 'N/A - test', 'NA - test'):
                cursor.execute("""
                    SELECT cost FROM shipping_costs
                    WHERE tracking_number = ?
                """, (tracking_number,))
                ship_result = cursor.fetchone()
                if ship_result:
                    shipping_cost = ship_result['cost']
            
            # Check for line items in stripe_line_items table
            cursor.execute("""
                SELECT * FROM stripe_line_items
                WHERE payment_intent_id = ?
            """, (source_order_id,))
            line_items = cursor.fetchall()
            
            if line_items and len(line_items) > 0:
                # Create separate sales record for each line item
                total_subtotal = sum(item['amount_subtotal'] or 0 for item in line_items)
                
                for item in line_items:
                    item_subtotal = item['amount_subtotal'] or 0
                    item_quantity = item['quantity'] or 1
                    
                    # Calculate proportional share of fees and shipping
                    proportion = item_subtotal / total_subtotal if total_subtotal > 0 else 1 / len(line_items)
                    
                    item_stripe_fee = platform_fee * proportion
                    item_shipping = shipping_cost * proportion
                    
                    # Try to get purchase_id from product mapping
                    stripe_product_id = item['stripe_product_id']
                    purchase_id = 'UNKNOWN'
                    
                    if stripe_product_id:
                        cursor.execute("""
                            SELECT purchase_id FROM stripe_product_mapping
                            WHERE stripe_product_id = ?
                        """, (stripe_product_id,))
                        mapping = cursor.fetchone()
                        if mapping:
                            purchase_id = mapping['purchase_id']
                        else:
                            # Track unmapped products
                            if stripe_product_id not in [p['id'] for p in stats['unmapped_products']]:
                                stats['unmapped_products'].append({
                                    'id': stripe_product_id,
                                    'name': item['product_name']
                                })
                    
                    # Calculate supplies estimate
                    supplies_estimate = db.get_supplies_cost_for_amount(item_subtotal)
                    
                    # Calculate net profit for this line item
                    net_profit = (
                        item_subtotal 
                        - item_stripe_fee 
                        - supplies_estimate
                        - item_shipping
                    )
                    
                    # Create unique transaction_id for each line item
                    transaction_id = f"{source_order_id}_{item['line_item_id']}"
                    
                    # Get customer name from order
                    customer_name = order['customer_name'] if 'customer_name' in order.keys() else None
                    
                    cursor.execute("""
                        INSERT INTO sales (
                            purchase_id, platform, order_number, transaction_id, item_title, custom_label,
                            sale_date, quantity, sale_price, shipping_charged, shipping_cost,
                            platform_fees_fixed, platform_fees_variable, regulatory_fee,
                            promoted_listing_fee, international_fee, supplies_estimate, grading_fee, net_profit,
                            customer_name
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        purchase_id,
                        'Stripe',
                        source_order_id,
                        transaction_id,
                        item['product_name'] or item['description'],
                        stripe_product_id,  # Store product ID as custom_label for reference
                        order_date,
                        item_quantity,
                        item_subtotal,
                        0,  # shipping_charged
                        item_shipping,
                        0,  # platform_fees_fixed
                        item_stripe_fee,  # Proportional Stripe fee
                        0,  # regulatory_fee
                        0,  # promoted_listing_fee
                        0,  # international_fee
                        supplies_estimate,
                        0,  # grading_fee
                        net_profit,
                        customer_name
                    ))
                    
                    stats['sales_created'] += 1
                    stats['sales_from_line_items'] += 1
            
            else:
                # No line items - create single sale from order_fulfillment data (legacy behavior)
                item_description = order['item_description']
                quantity = order['quantity'] or 1
                
                purchase_id = db.get_purchase_id_from_sku('', item_description)
                supplies_estimate = db.get_supplies_cost_for_amount(order_total)
                
                # Use actual fee or estimate
                if platform_fee and platform_fee > 0:
                    stripe_fee_variable = platform_fee
                    stripe_fee_fixed = 0
                else:
                    stripe_fee_variable = order_total * 0.029
                    stripe_fee_fixed = 0.30
                
                net_profit = (
                    order_total 
                    - stripe_fee_fixed 
                    - stripe_fee_variable 
                    - supplies_estimate
                    - shipping_cost
                )
                
                # Get customer name from order
                customer_name = order['customer_name'] if 'customer_name' in order.keys() else None
                
                cursor.execute("""
                    INSERT INTO sales (
                        purchase_id, platform, order_number, transaction_id, item_title, custom_label,
                        sale_date, quantity, sale_price, shipping_charged, shipping_cost,
                        platform_fees_fixed, platform_fees_variable, regulatory_fee,
                        promoted_listing_fee, international_fee, supplies_estimate, grading_fee, net_profit,
                        customer_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    purchase_id,
                    'Stripe',
                    source_order_id,
                    source_order_id,
                    item_description,
                    None,
                    order_date,
                    quantity,
                    order_total,
                    0,
                    shipping_cost,
                    stripe_fee_fixed,
                    stripe_fee_variable,
                    0, 0, 0,
                    supplies_estimate,
                    0,
                    net_profit,
                    customer_name
                ))
                
                stats['sales_created'] += 1
        
        conn.commit()
        
    except Exception as e:
        stats['errors'].append(f"Processing error: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
    
    return stats


def remap_stripe_sales_purchases():
    """
    Remap purchase_id for existing Stripe sales based on stripe_product_mapping.
    
    This looks at the custom_label field (which stores stripe_product_id) or 
    matches by product name in stripe_line_items, then updates purchase_id
    based on the mappings in stripe_product_mapping.
    
    Returns:
        Dict with remap statistics
    """
    stats = {
        'sales_checked': 0,
        'sales_remapped': 0,
        'already_mapped': 0,
        'no_mapping_found': 0,
        'errors': []
    }
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    try:
        # Get all Stripe sales that have UNKNOWN purchase_id
        cursor.execute("""
            SELECT sale_id, order_number, transaction_id, item_title, custom_label, purchase_id
            FROM sales
            WHERE platform = 'Stripe'
        """)
        
        stripe_sales = cursor.fetchall()
        
        for sale in stripe_sales:
            stats['sales_checked'] += 1
            
            current_purchase_id = sale['purchase_id']
            custom_label = sale['custom_label']  # May contain stripe_product_id
            item_title = sale['item_title']
            order_number = sale['order_number']
            transaction_id = sale['transaction_id']
            
            # Skip if already has a valid (non-UNKNOWN) purchase_id
            if current_purchase_id and current_purchase_id != 'UNKNOWN':
                stats['already_mapped'] += 1
                continue
            
            new_purchase_id = None
            
            # Method 1: Check custom_label for stripe_product_id (prod_xxx)
            if custom_label and custom_label.startswith('prod_'):
                cursor.execute("""
                    SELECT purchase_id FROM stripe_product_mapping
                    WHERE stripe_product_id = ? AND purchase_id != 'UNKNOWN'
                """, (custom_label,))
                mapping = cursor.fetchone()
                if mapping:
                    new_purchase_id = mapping['purchase_id']
            
            # Method 2: Try to match by looking up line items for this order
            if not new_purchase_id:
                # Get line items for this order/transaction
                cursor.execute("""
                    SELECT sli.stripe_product_id, spm.purchase_id
                    FROM stripe_line_items sli
                    JOIN stripe_product_mapping spm ON sli.stripe_product_id = spm.stripe_product_id
                    WHERE sli.payment_intent_id = ?
                    AND spm.purchase_id != 'UNKNOWN'
                """, (order_number,))
                line_mappings = cursor.fetchall()
                
                if line_mappings:
                    # If only one product, use that mapping
                    if len(line_mappings) == 1:
                        new_purchase_id = line_mappings[0]['purchase_id']
                    else:
                        # Multiple products - try to match by item_title
                        for lm in line_mappings:
                            cursor.execute("""
                                SELECT product_name FROM stripe_line_items
                                WHERE stripe_product_id = ?
                            """, (lm['stripe_product_id'],))
                            prod = cursor.fetchone()
                            if prod and prod['product_name'] and item_title:
                                if prod['product_name'].lower() in item_title.lower() or item_title.lower() in prod['product_name'].lower():
                                    new_purchase_id = lm['purchase_id']
                                    break
            
            # Method 3: Try to match by product name in mapping table
            if not new_purchase_id and item_title:
                cursor.execute("""
                    SELECT purchase_id FROM stripe_product_mapping
                    WHERE product_name LIKE ? AND purchase_id != 'UNKNOWN'
                """, (f"%{item_title[:50]}%",))
                mapping = cursor.fetchone()
                if mapping:
                    new_purchase_id = mapping['purchase_id']
            
            # Update if we found a mapping
            if new_purchase_id:
                cursor.execute("""
                    UPDATE sales
                    SET purchase_id = ?
                    WHERE sale_id = ?
                """, (new_purchase_id, sale['sale_id']))
                stats['sales_remapped'] += 1
            else:
                stats['no_mapping_found'] += 1
        
        conn.commit()
        
    except Exception as e:
        stats['errors'].append(f"Remap error: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
    
    return stats


def get_unprocessed_stripe_orders_count():
    """
    Count how many Stripe orders haven't been processed to sales yet.
    
    Returns:
        Number of unprocessed orders
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM order_fulfillment of
        WHERE of.source = 'Stripe'
        AND NOT EXISTS (
            SELECT 1 FROM sales s
            WHERE s.transaction_id = of.source_order_id
            OR (s.platform = 'Stripe' AND s.order_number = of.source_order_id)
        )
    """)
    
    result = cursor.fetchone()
    conn.close()
    
    return result['count'] if result else 0


def repair_stripe_sales_fees():
    """
    Repair existing Stripe sales to add correct fees and shipping costs.
    
    This updates existing Stripe sales records that have:
    - $0 platform fees (fetches actual fee from Stripe API)
    - $0 shipping costs (can match from shipping_costs table)
    
    Returns:
        Dict with repair statistics
    """
    stats = {
        'sales_checked': 0,
        'fees_updated': 0,
        'fees_from_api': 0,
        'fees_estimated': 0,
        'shipping_updated': 0,
        'total_fees_assigned': 0.0,
        'total_shipping_assigned': 0.0,
        'errors': []
    }
    
    # Get API key to fetch actual fees
    api_key = get_stripe_api_key()
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    try:
        # Get all Stripe sales that need fee updates
        cursor.execute("""
            SELECT s.sale_id, s.order_number, s.sale_price, 
                   s.platform_fees_fixed, s.platform_fees_variable, 
                   s.shipping_cost, s.net_profit,
                   of.tracking_number, of.platform_fee as stored_fee
            FROM sales s
            LEFT JOIN order_fulfillment of ON s.order_number = of.source_order_id
            WHERE s.platform = 'Stripe'
        """)
        
        stripe_sales = cursor.fetchall()
        
        for sale in stripe_sales:
            stats['sales_checked'] += 1
            
            sale_price = sale['sale_price'] or 0
            current_fees_fixed = sale['platform_fees_fixed'] or 0
            current_fees_variable = sale['platform_fees_variable'] or 0
            current_shipping = sale['shipping_cost'] or 0
            tracking_number = sale['tracking_number']
            order_number = sale['order_number']
            
            # Check if stored_fee exists in order_fulfillment
            stored_fee = sale['stored_fee'] if 'stored_fee' in sale.keys() and sale['stored_fee'] else 0
            
            # Try to get actual fee from Stripe API if we have API key and no stored fee
            actual_fee = stored_fee
            if api_key and actual_fee == 0 and order_number:
                try:
                    # Fetch the specific payment intent with balance_transaction
                    headers = {"Authorization": f"Bearer {api_key}"}
                    
                    # Try payment intent first
                    if order_number.startswith('pi_'):
                        response = requests.get(
                            f"{STRIPE_API_BASE}/payment_intents/{order_number}",
                            headers=headers,
                            params={"expand[]": "latest_charge.balance_transaction"}
                        )
                        if response.status_code == 200:
                            intent = response.json()
                            latest_charge = intent.get('latest_charge')
                            if latest_charge and isinstance(latest_charge, dict):
                                balance_txn = latest_charge.get('balance_transaction')
                                if balance_txn and isinstance(balance_txn, dict):
                                    actual_fee = balance_txn.get('fee', 0) / 100
                                    stats['fees_from_api'] += 1
                    
                    # Try invoice if it's an invoice ID
                    elif order_number.startswith('in_'):
                        response = requests.get(
                            f"{STRIPE_API_BASE}/invoices/{order_number}",
                            headers=headers,
                            params={"expand[]": "charge.balance_transaction"}
                        )
                        if response.status_code == 200:
                            invoice = response.json()
                            charge = invoice.get('charge')
                            if charge and isinstance(charge, dict):
                                balance_txn = charge.get('balance_transaction')
                                if balance_txn and isinstance(balance_txn, dict):
                                    actual_fee = balance_txn.get('fee', 0) / 100
                                    stats['fees_from_api'] += 1
                except Exception as e:
                    # If API call fails, we'll fall back to estimate
                    pass
            
            # Determine correct fee (actual or estimate)
            if actual_fee > 0:
                correct_fees_variable = actual_fee
                correct_fees_fixed = 0
            else:
                # Fallback to estimate: 2.9% + $0.30
                correct_fees_variable = sale_price * 0.029
                correct_fees_fixed = 0.30
                if current_fees_fixed == 0 and current_fees_variable == 0:
                    stats['fees_estimated'] += 1
            
            # Check if fees need updating
            fees_diff = 0
            current_total_fees = current_fees_fixed + current_fees_variable
            correct_total_fees = correct_fees_fixed + correct_fees_variable
            
            if abs(current_total_fees - correct_total_fees) > 0.001:
                fees_diff = correct_total_fees - current_total_fees
                stats['fees_updated'] += 1
                stats['total_fees_assigned'] += abs(fees_diff)
            
            # Try to match shipping cost
            shipping_diff = 0
            new_shipping = current_shipping
            if tracking_number and tracking_number not in ('', 'N/A', 'NA', 'N/A - test', 'NA - test') and current_shipping == 0:
                cursor.execute("""
                    SELECT cost FROM shipping_costs
                    WHERE tracking_number = ?
                """, (tracking_number,))
                ship_result = cursor.fetchone()
                if ship_result:
                    new_shipping = ship_result['cost']
                    shipping_diff = new_shipping
                    stats['shipping_updated'] += 1
                    stats['total_shipping_assigned'] += new_shipping
            
            # Update if anything changed
            if fees_diff != 0 or shipping_diff != 0:
                total_adjustment = fees_diff + shipping_diff
                
                cursor.execute("""
                    UPDATE sales
                    SET platform_fees_fixed = ?,
                        platform_fees_variable = ?,
                        shipping_cost = ?,
                        net_profit = net_profit - ?
                    WHERE sale_id = ?
                """, (
                    correct_fees_fixed,
                    correct_fees_variable,
                    new_shipping,
                    total_adjustment,
                    sale['sale_id']
                ))
        
        conn.commit()
        
    except Exception as e:
        stats['errors'].append(f"Repair error: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
    
    return stats
