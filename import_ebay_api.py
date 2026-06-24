"""
eBay API Integration Module for Taclaco Dashboard
Phase 3A: User Token authentication with Fulfillment and Finances APIs

Supports both Sandbox and Production environments.
"""

import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import database as db


# ============================================================================
# CONFIGURATION
# ============================================================================

# API Endpoints
SANDBOX_BASE_URL = "https://api.sandbox.ebay.com"
PRODUCTION_BASE_URL = "https://api.ebay.com"

# Finances API uses a different subdomain
SANDBOX_FINANCES_BASE_URL = "https://apiz.sandbox.ebay.com"
PRODUCTION_FINANCES_BASE_URL = "https://apiz.ebay.com"

# API Paths
FULFILLMENT_API_PATH = "/sell/fulfillment/v1"
FINANCES_API_PATH = "/sell/finances/v1"


def get_api_base_url(api_type: str = 'fulfillment') -> str:
    """Get the correct API base URL based on environment setting and API type"""
    env = db.get_setting('ebay_environment') or 'sandbox'
    
    if api_type == 'finances':
        return PRODUCTION_FINANCES_BASE_URL if env == 'production' else SANDBOX_FINANCES_BASE_URL
    else:
        return PRODUCTION_BASE_URL if env == 'production' else SANDBOX_BASE_URL


def get_auth_headers() -> Dict[str, str]:
    """Build authentication headers for eBay API requests"""
    user_token = db.get_setting('ebay_user_token')
    
    if not user_token:
        raise ValueError("eBay User Token not configured. Please add credentials in Settings.")
    
    return {
        'Authorization': f'Bearer {user_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }


# ============================================================================
# LOW-LEVEL API REQUEST FUNCTIONS
# ============================================================================

def ebay_api_request(
    endpoint: str,
    params: Optional[Dict] = None,
    method: str = 'GET',
    debug: bool = False,
    api_type: str = 'fulfillment'
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Make an authenticated request to eBay API
    
    Args:
        endpoint: API endpoint path (e.g., '/sell/fulfillment/v1/order')
        params: Query parameters
        method: HTTP method (GET, POST, etc.)
        debug: If True, return debug info in error message
        api_type: 'fulfillment' or 'finances' - determines base URL
    
    Returns:
        Tuple of (response_data, error_message)
        - On success: (dict, None)
        - On error: (None, error_string)
    """
    debug_info = []
    
    try:
        base_url = get_api_base_url(api_type)
        url = f"{base_url}{endpoint}"
        headers = get_auth_headers()
        
        # Mask token for debug output
        debug_headers = {k: (v[:20] + '...' if k == 'Authorization' and len(v) > 20 else v) for k, v in headers.items()}
        
        debug_info.append(f"URL: {url}")
        debug_info.append(f"Method: {method}")
        debug_info.append(f"Headers: {debug_headers}")
        debug_info.append(f"Params: {params}")
        
        if method == 'GET':
            response = requests.get(url, headers=headers, params=params, timeout=30)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=params, timeout=30)
        else:
            return None, f"Unsupported HTTP method: {method}"
        
        debug_info.append(f"Response Status: {response.status_code}")
        debug_info.append(f"Response Headers: {dict(response.headers)}")
        
        # Check for successful response
        if response.status_code == 200:
            return response.json(), None
        elif response.status_code == 204:
            return {}, None  # No content but success
        else:
            # Try to extract error message from response
            response_text = response.text
            debug_info.append(f"Response Body: {response_text[:1000]}")
            
            try:
                error_data = response.json()
                error_msg = error_data.get('errors', [{}])[0].get('message', response_text)
                error_id = error_data.get('errors', [{}])[0].get('errorId', 'N/A')
                debug_info.append(f"Error ID: {error_id}")
            except:
                error_msg = response_text or f"HTTP {response.status_code}"
            
            if debug:
                return None, f"eBay API Error ({response.status_code}): {error_msg}\n\n--- DEBUG INFO ---\n" + "\n".join(debug_info)
            else:
                return None, f"eBay API Error ({response.status_code}): {error_msg}"
    
    except requests.exceptions.Timeout:
        error = "Request timed out. Please try again."
        if debug:
            return None, f"{error}\n\n--- DEBUG INFO ---\n" + "\n".join(debug_info)
        return None, error
    except requests.exceptions.ConnectionError as e:
        error = f"Connection error: {str(e)}"
        if debug:
            return None, f"{error}\n\n--- DEBUG INFO ---\n" + "\n".join(debug_info)
        return None, error
    except Exception as e:
        error = f"Request failed: {str(e)}"
        if debug:
            return None, f"{error}\n\n--- DEBUG INFO ---\n" + "\n".join(debug_info)
        return None, error


def test_api_connection() -> Tuple[bool, str]:
    """
    Test if eBay API credentials are valid
    
    Returns:
        Tuple of (success, message)
    """
    # Check if credentials are configured
    user_token = db.get_setting('ebay_user_token')
    if not user_token:
        return False, "eBay credentials not configured"
    
    # Show token length for debugging
    token_info = f"Token length: {len(user_token)} chars, starts with: {user_token[:30]}..."
    
    # Try to fetch orders with a small limit to test connection
    endpoint = f"{FULFILLMENT_API_PATH}/order"
    params = {'limit': 1}
    
    data, error = ebay_api_request(endpoint, params, debug=True)
    
    if error:
        return False, f"{error}\n\nToken Info: {token_info}"
    
    env = db.get_setting('ebay_environment') or 'sandbox'
    order_count = len(data.get('orders', []))
    total = data.get('total', 0)
    return True, f"Connected to eBay ({env.title()}) successfully! Found {total} total orders."


def test_finances_api() -> Tuple[bool, str]:
    """
    Test if Finances API is accessible (separate from Fulfillment API)
    
    Returns:
        Tuple of (success, message)
    """
    user_token = db.get_setting('ebay_user_token')
    if not user_token:
        return False, "eBay credentials not configured"
    
    # Try to fetch recent transactions
    endpoint = f"{FINANCES_API_PATH}/transaction"
    
    # Last 30 days
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)
    start_str = start_date.strftime('%Y-%m-%dT00:00:00.000Z')
    end_str = end_date.strftime('%Y-%m-%dT23:59:59.999Z')
    
    params = {
        'filter': f"transactionDate:[{start_str}..{end_str}]",
        'limit': 5
    }
    
    data, error = ebay_api_request(endpoint, params, debug=True, api_type='finances')
    
    if error:
        return False, f"Finances API Error: {error}"
    
    transactions = data.get('transactions', [])
    total = data.get('total', 0)
    
    if total == 0:
        return True, f"Finances API connected but returned 0 transactions for last 30 days. This might be normal if fees are processed differently."
    
    # Show sample transaction types
    trans_types = set()
    for t in transactions[:5]:
        trans_types.add(t.get('transactionType', 'UNKNOWN'))
    
    return True, f"Finances API working! Found {total} transactions. Types: {', '.join(trans_types)}"


# ============================================================================
# FULFILLMENT API FUNCTIONS
# ============================================================================

def fetch_orders(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 200
) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch orders from eBay Fulfillment API
    
    Args:
        start_date: Start of date range (defaults to 90 days ago)
        end_date: End of date range (defaults to now)
        limit: Maximum orders to fetch (max 200 per request)
    
    Returns:
        Tuple of (orders_list, error_message)
    """
    # Default date range: last 90 days
    if not end_date:
        end_date = datetime.utcnow()
    if not start_date:
        start_date = end_date - timedelta(days=90)
    
    # Format dates for eBay API (ISO 8601)
    start_str = start_date.strftime('%Y-%m-%dT00:00:00.000Z')
    end_str = end_date.strftime('%Y-%m-%dT23:59:59.999Z')
    
    # Build filter parameter
    filter_str = f"creationdate:[{start_str}..{end_str}]"
    
    endpoint = f"{FULFILLMENT_API_PATH}/order"
    all_orders = []
    offset = 0
    
    while True:
        params = {
            'filter': filter_str,
            'limit': min(limit, 200),
            'offset': offset
        }
        
        data, error = ebay_api_request(endpoint, params)
        
        if error:
            return all_orders, error
        
        orders = data.get('orders', [])
        all_orders.extend(orders)
        
        # Check if there are more pages
        total = data.get('total', 0)
        if offset + len(orders) >= total or len(orders) == 0:
            break
        
        offset += len(orders)
        
        # Safety limit to prevent infinite loops
        if offset >= 10000:
            break
    
    return all_orders, None


def fetch_order_detail(order_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Fetch detailed information for a single order
    
    Args:
        order_id: eBay order ID
    
    Returns:
        Tuple of (order_data, error_message)
    """
    endpoint = f"{FULFILLMENT_API_PATH}/order/{order_id}"
    return ebay_api_request(endpoint)


def fetch_shipping_fulfillments(order_id: str) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch shipping fulfillment (tracking) info for an order
    
    Args:
        order_id: eBay order ID
    
    Returns:
        Tuple of (fulfillments_list, error_message)
    """
    endpoint = f"{FULFILLMENT_API_PATH}/order/{order_id}/shipping_fulfillment"
    data, error = ebay_api_request(endpoint)
    
    if error:
        return [], error
    
    return data.get('fulfillments', []), None


# ============================================================================
# FINANCES API FUNCTIONS
# ============================================================================

def fetch_transactions(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 200
) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch financial transactions (fees, payouts) from eBay Finances API
    
    Args:
        start_date: Start of date range (defaults to 90 days ago)
        end_date: End of date range (defaults to now)
        limit: Maximum transactions to fetch (max 200 per request)
    
    Returns:
        Tuple of (transactions_list, error_message)
    """
    # Default date range: last 90 days
    if not end_date:
        end_date = datetime.utcnow()
    if not start_date:
        start_date = end_date - timedelta(days=90)
    
    # Format dates for eBay API
    start_str = start_date.strftime('%Y-%m-%dT00:00:00.000Z')
    end_str = end_date.strftime('%Y-%m-%dT23:59:59.999Z')
    
    # Build filter parameter
    filter_str = f"transactionDate:[{start_str}..{end_str}]"
    
    endpoint = f"{FINANCES_API_PATH}/transaction"
    all_transactions = []
    offset = 0
    
    while True:
        params = {
            'filter': filter_str,
            'limit': min(limit, 200),
            'offset': offset
        }
        
        data, error = ebay_api_request(endpoint, params, api_type='finances')
        
        if error:
            return all_transactions, error
        
        transactions = data.get('transactions', [])
        all_transactions.extend(transactions)
        
        # Check if there are more pages
        total = data.get('total', 0)
        if offset + len(transactions) >= total or len(transactions) == 0:
            break
        
        offset += len(transactions)
        
        # Safety limit
        if offset >= 10000:
            break
    
    return all_transactions, None


def fetch_payouts(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Tuple[List[Dict], Optional[str]]:
    """
    Fetch payout summaries from eBay Finances API
    
    Args:
        start_date: Start of date range (defaults to 90 days ago)
        end_date: End of date range (defaults to now)
    
    Returns:
        Tuple of (payouts_list, error_message)
    """
    # Default date range: last 90 days
    if not end_date:
        end_date = datetime.utcnow()
    if not start_date:
        start_date = end_date - timedelta(days=90)
    
    # Format dates for eBay API
    start_str = start_date.strftime('%Y-%m-%dT00:00:00.000Z')
    end_str = end_date.strftime('%Y-%m-%dT23:59:59.999Z')
    
    filter_str = f"payoutDate:[{start_str}..{end_str}]"
    
    endpoint = f"{FINANCES_API_PATH}/payout"
    params = {
        'filter': filter_str,
        'limit': 200
    }
    
    data, error = ebay_api_request(endpoint, params, api_type='finances')
    
    if error:
        return [], error
    
    return data.get('payouts', []), None


# ============================================================================
# DATA TRANSFORMATION FUNCTIONS
# ============================================================================

def parse_order(order_data: Dict) -> Dict:
    """
    Transform eBay API order data into database format
    
    Args:
        order_data: Raw order from eBay API
    
    Returns:
        Dict formatted for ebay_orders table
    """
    # Extract shipping address
    fulfillment_instructions = order_data.get('fulfillmentStartInstructions', [])
    ship_to = {}
    if fulfillment_instructions:
        ship_to = fulfillment_instructions[0].get('shippingStep', {}).get('shipTo', {})
        contact = ship_to.get('contactAddress', {})
        ship_to = {
            'name': ship_to.get('fullName', ''),
            'address1': contact.get('addressLine1', ''),
            'address2': contact.get('addressLine2', ''),
            'city': contact.get('city', ''),
            'state': contact.get('stateOrProvince', ''),
            'zip': contact.get('postalCode', ''),
            'country': contact.get('countryCode', '')
        }
    
    # Extract buyer info
    buyer = order_data.get('buyer', {})
    
    # Extract price summary
    pricing = order_data.get('pricingSummary', {})
    total = pricing.get('total', {})
    order_total = float(total.get('value', 0))
    
    return {
        'order_id': order_data.get('orderId', ''),
        'order_number': order_data.get('orderId', ''),  # eBay uses orderId as display number
        'legacy_order_id': order_data.get('legacyOrderId', ''),
        'created_date': order_data.get('creationDate', ''),
        'buyer_username': buyer.get('username', ''),
        'buyer_name': buyer.get('buyerRegistrationAddress', {}).get('fullName', ''),
        'ship_to_name': ship_to.get('name', ''),
        'ship_to_address1': ship_to.get('address1', ''),
        'ship_to_address2': ship_to.get('address2', ''),
        'ship_to_city': ship_to.get('city', ''),
        'ship_to_state': ship_to.get('state', ''),
        'ship_to_zip': ship_to.get('zip', ''),
        'ship_to_country': ship_to.get('country', ''),
        'order_status': order_data.get('orderFulfillmentStatus', ''),
        'order_total': order_total
    }


def parse_line_items(order_id: str, order_data: Dict) -> List[Dict]:
    """
    Extract line items from order data
    
    Args:
        order_id: Parent order ID
        order_data: Raw order from eBay API
    
    Returns:
        List of dicts formatted for ebay_line_items table
    """
    line_items = []
    
    for item in order_data.get('lineItems', []):
        price_info = item.get('lineItemCost', {})
        # lineItemCost is the TOTAL cost for the line (already includes quantity)
        # NOT the per-unit price
        line_item_cost = float(price_info.get('value', 0))
        quantity = item.get('quantity', 1)
        
        # Calculate actual unit price by dividing total by quantity
        unit_price = line_item_cost / quantity if quantity > 0 else line_item_cost
        
        line_items.append({
            'line_item_id': item.get('lineItemId', ''),
            'order_id': order_id,
            'item_id': item.get('legacyItemId', ''),
            'item_title': item.get('title', ''),
            'sku': item.get('sku', ''),  # Custom label
            'quantity': quantity,
            'unit_price': unit_price,  # Per-unit price
            'line_total': line_item_cost  # Already the total from eBay
        })
    
    return line_items


def parse_fulfillments(order_id: str, fulfillments_data: List[Dict]) -> List[Dict]:
    """
    Transform fulfillment data into database format
    
    Args:
        order_id: Parent order ID
        fulfillments_data: Raw fulfillments from eBay API
    
    Returns:
        List of dicts formatted for ebay_fulfillments table
    """
    fulfillments = []
    
    for ful in fulfillments_data:
        shipment = ful.get('shipmentTrackingNumber', '')
        carrier = ful.get('shippingCarrierCode', '')
        ship_date = ful.get('shippedDate', '')
        
        # Try to extract date only
        if ship_date and 'T' in ship_date:
            ship_date = ship_date.split('T')[0]
        
        fulfillments.append({
            'fulfillment_id': ful.get('fulfillmentId', ''),
            'order_id': order_id,
            'tracking_number': shipment,
            'carrier': carrier,
            'ship_date': ship_date
        })
    
    return fulfillments


def parse_transaction_fees(transaction_data: Dict) -> List[Dict]:
    """
    Extract fee information from a financial transaction
    
    Handles multiple transaction types:
    - SALE: Contains TOTAL_FEE and itemized fees (FINAL_VALUE_FEE, etc.)
    - SHIPPING_LABEL: eBay Standard Envelope postage charges
    - NON_SALE_CHARGE: Promoted Listings and other fees (linked via references)
    - REFUND: Refund transactions with fee adjustments
    
    Args:
        transaction_data: Raw transaction from eBay Finances API
    
    Returns:
        List of dicts formatted for ebay_fees table
    """
    fees = []
    
    # Get transaction details
    transaction_type = transaction_data.get('transactionType', '')
    order_ref = transaction_data.get('orderId', '')
    transaction_id = transaction_data.get('transactionId', '')
    transaction_date = transaction_data.get('transactionDate', '')
    payout_id = transaction_data.get('payoutId', '')
    
    # Extract date only
    if transaction_date and 'T' in transaction_date:
        transaction_date = transaction_date.split('T')[0]
    
    # Handle SHIPPING_LABEL transactions
    if transaction_type == 'SHIPPING_LABEL':
        amount = transaction_data.get('amount', {})
        fee_amount = abs(float(amount.get('value', 0)))
        
        if fee_amount > 0 and order_ref:
            fees.append({
                'order_id': order_ref,
                'transaction_id': transaction_id,
                'fee_type': 'SHIPPING_LABEL',
                'amount': fee_amount,
                'transaction_date': transaction_date,
                'payout_id': payout_id
            })
        return fees
    
    # Handle NON_SALE_CHARGE transactions (Promoted Listings, etc.)
    if transaction_type == 'NON_SALE_CHARGE':
        amount = transaction_data.get('amount', {})
        fee_amount = abs(float(amount.get('value', 0)))
        memo = transaction_data.get('transactionMemo', '')
        
        # Get order_id from references if not in orderId field
        if not order_ref:
            references = transaction_data.get('references', [])
            for ref in references:
                if ref.get('referenceType') == 'ORDER_ID':
                    order_ref = ref.get('referenceId', '')
                    break
        
        if fee_amount > 0 and order_ref:
            # Determine fee type from memo
            if 'Promoted Listings' in memo:
                fee_type = 'PROMOTED_LISTING_FEE'
            else:
                fee_type = 'NON_SALE_CHARGE'
            
            fees.append({
                'order_id': order_ref,
                'transaction_id': transaction_id,
                'fee_type': fee_type,
                'amount': fee_amount,
                'transaction_date': transaction_date,
                'payout_id': payout_id
            })
        return fees
    
    # Handle SALE and REFUND transactions (original logic)
    if transaction_type in ('SALE', 'REFUND'):
        # Get TOTAL_FEE from totalFeeAmount
        total_fee = transaction_data.get('totalFeeAmount', {})
        if total_fee:
            fee_amount = float(total_fee.get('value', 0))
            fee_amount = abs(fee_amount)
            
            if fee_amount > 0:
                fees.append({
                    'order_id': order_ref,
                    'transaction_id': transaction_id,
                    'fee_type': 'TOTAL_FEE',
                    'amount': fee_amount,
                    'transaction_date': transaction_date,
                    'payout_id': payout_id
                })
        
        # Get itemized fees from orderLineItems.marketplaceFees
        marketplace_fees = transaction_data.get('orderLineItems', [])
        for item in marketplace_fees:
            item_fees = item.get('marketplaceFees', [])
            for fee in item_fees:
                fee_type = fee.get('feeType', 'UNKNOWN')
                fee_amount = abs(float(fee.get('amount', {}).get('value', 0)))
                
                if fee_amount > 0:
                    fees.append({
                        'order_id': order_ref,
                        'transaction_id': f"{transaction_id}_{fee_type}",
                        'fee_type': fee_type,
                        'amount': fee_amount,
                        'transaction_date': transaction_date,
                        'payout_id': payout_id
                    })
    
    return fees


# ============================================================================
# MASTER SYNC FUNCTIONS
# ============================================================================


def parse_api_transaction(transaction_data: Dict, payout_date_lookup: Dict = None) -> Optional[Dict]:
    """
    Parse a Finances API transaction into ebay_transactions table format.
    Handles ALL transaction types from the Finances API.
    
    Args:
        transaction_data: Raw transaction from eBay Finances API
        payout_date_lookup: Optional dict mapping payoutId -> payout_date (YYYY-MM-DD)
        
    Returns:
        Dict formatted for ebay_transactions table, or None if not storable
    """
    transaction_type = transaction_data.get('transactionType', '')
    
    # Map API transaction types to CSV-compatible type names
    # These match the types already in ebay_transactions from CSV imports
    TYPE_MAP = {
        'SALE':             'Order',
        'REFUND':           'Refund',
        'SHIPPING_LABEL':   'Shipping label',
        'NON_SALE_CHARGE':  'Other fee',
        'ADJUSTMENT':       'Adjustment',
        'TRANSFER':         'Transfer',
        'DISPUTE':          'Claim',
        'CREDIT':           'Credit',
        'PAYOUT':           'Payout',
        'LOAN_REPAYMENT':   'Loan repayment',
        'PURCHASE':         'Purchase',
    }
    
    mapped_type = TYPE_MAP.get(transaction_type)
    if not mapped_type:
        # Unknown type -- store it with raw API type name for visibility
        mapped_type = transaction_type
    
    # Extract common fields
    order_id = transaction_data.get('orderId', '')
    transaction_id = transaction_data.get('transactionId', '')
    transaction_date = transaction_data.get('transactionDate', '')
    
    # Parse date (format: 2025-12-27T12:34:56.000Z)
    if transaction_date and 'T' in transaction_date:
        transaction_date = transaction_date.split('T')[0]
    
    # Get amount
    amount = transaction_data.get('amount', {})
    gross_amount = float(amount.get('value', 0))
    currency = amount.get('currency', 'USD')
    
    # Use bookingEntry to determine sign (DEBIT = cost to seller = negative)
    booking_entry = transaction_data.get('bookingEntry', '')
    if booking_entry == 'DEBIT' and gross_amount > 0:
        gross_amount = -gross_amount
    elif booking_entry == 'CREDIT' and gross_amount < 0:
        gross_amount = abs(gross_amount)
    
    # Get payout info and resolve payout_date
    payout_id = transaction_data.get('payoutId', '')
    payout_date = None
    if payout_id and payout_date_lookup:
        payout_date = payout_date_lookup.get(payout_id)
    
    # Get references (tracking numbers for shipping labels, order IDs for fees)
    references = transaction_data.get('references', [])
    reference_id = None
    for ref in references:
        ref_type = ref.get('referenceType', '')
        if ref_type == 'TRACKING_NUMBER':
            tracking = ref.get('referenceId', '')
            reference_id = f"Tracking no. {tracking}"
            break
        elif ref_type == 'ORDER_ID' and not order_id:
            # NON_SALE_CHARGE transactions may have orderId in references
            order_id = ref.get('referenceId', '')
    
    # For SHIPPING_LABEL transactions without tracking in references,
    # lookup tracking number from ebay_fulfillments by order_id
    if transaction_type == 'SHIPPING_LABEL' and not reference_id and order_id:
        try:
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tracking_number 
                FROM ebay_fulfillments 
                WHERE order_id = ?
                LIMIT 1
            """, (order_id,))
            result = cursor.fetchone()
            conn.close()
            
            if result and result['tracking_number']:
                tracking = result['tracking_number']
                reference_id = f"Tracking no. {tracking}"
        except Exception:
            pass
    
    # Build description from transactionMemo
    description = transaction_data.get('transactionMemo', '') or ''
    
    if transaction_type == 'SHIPPING_LABEL':
        if 'Standard Envelope' in description or (reference_id and 'ESUS' in str(reference_id)):
            description = 'eBay Standard Envelope'
        elif 'UPS' in description:
            description = 'UPS'
        elif 'FEDEX' in description or 'FedEx' in description:
            description = 'FEDEX'
        elif 'USPS' in description:
            description = 'USPS'
        elif not description:
            description = 'USPS'
    elif not description:
        description = '--'
    
    # Build transaction record
    transaction = {
        'transaction_date': transaction_date,
        'payout_date': payout_date,
        'type': mapped_type,
        'order_number': order_id,
        'transaction_id': transaction_id,
        'gross_transaction_amount': gross_amount,
        'net_amount': gross_amount,
        'transaction_currency': currency,
        'payout_id': payout_id,
        'reference_id': reference_id,
        'description': description,
    }
    
    return transaction


def build_payout_date_lookup(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Dict:
    """
    Fetch payouts from eBay API and build a payoutId -> payout_date mapping.
    Used to resolve payout_date for transactions fetched from the Finances API.
    
    Returns:
        Dict mapping payout_id (str) -> payout_date (str, YYYY-MM-DD)
    """
    payouts, error = fetch_payouts(start_date, end_date)
    if error:
        print(f"Warning: Could not fetch payouts for date lookup: {error}")
        return {}
    
    lookup = {}
    for payout in payouts:
        payout_id = payout.get('payoutId', '')
        payout_date = payout.get('payoutDate', '')
        if payout_id and payout_date:
            if 'T' in payout_date:
                payout_date = payout_date.split('T')[0]
            lookup[payout_id] = payout_date
    
    return lookup



def sync_ebay_data(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    progress_callback=None
) -> Dict:
    """
    Master sync function - fetches and stores all eBay data
    
    Args:
        start_date: Start of date range (defaults to 90 days ago)
        end_date: End of date range (defaults to now)
        progress_callback: Optional callback function(step, message) for progress updates
    
    Returns:
        Dict with sync statistics
    """
    stats = {
        'orders_fetched': 0,
        'orders_stored': 0,
        'line_items_stored': 0,
        'fulfillments_stored': 0,
        'fees_stored': 0,
        'transactions_stored': 0,
        'errors': [],
        'success': False
    }
    
    def update_progress(step: str, msg: str):
        if progress_callback:
            progress_callback(step, msg)
    
    try:
        # ====================================================================
        # STEP 1: Fetch Orders from Fulfillment API
        # ====================================================================
        update_progress('orders', 'Fetching orders from eBay...')
        
        orders, error = fetch_orders(start_date, end_date)
        
        if error:
            stats['errors'].append(f"Orders fetch failed: {error}")
            # Continue anyway to try fees
        
        stats['orders_fetched'] = len(orders)
        
        # ====================================================================
        # STEP 2: Store Orders, Line Items, and Fulfillments
        # ====================================================================
        update_progress('storing', f'Processing {len(orders)} orders...')
        
        for order_data in orders:
            order_id = order_data.get('orderId', '')
            
            # Parse and store order
            order = parse_order(order_data)
            if db.add_ebay_order(order):
                stats['orders_stored'] += 1
            
            # Parse and store line items
            line_items = parse_line_items(order_id, order_data)
            for item in line_items:
                if db.add_ebay_line_item(item):
                    stats['line_items_stored'] += 1
            
            # Fetch and store fulfillments (tracking)
            fulfillments_data, ful_error = fetch_shipping_fulfillments(order_id)
            if not ful_error:
                fulfillments = parse_fulfillments(order_id, fulfillments_data)
                for ful in fulfillments:
                    if db.add_ebay_fulfillment(ful):
                        stats['fulfillments_stored'] += 1
        
        # ====================================================================
        # STEP 3: Fetch Transactions/Fees from Finances API
        # ====================================================================
        update_progress('fees', 'Fetching financial transactions...')
        
        transactions, error = fetch_transactions(start_date, end_date)
        
        if error:
            stats['errors'].append(f"Transactions fetch failed: {error}")
        else:
            stats['transactions_fetched'] = len(transactions)
            if len(transactions) == 0:
                stats['errors'].append(f"Finances API returned 0 transactions for date range {start_date} to {end_date}")
            
            for trans in transactions:
                fees = parse_transaction_fees(trans)
                for fee in fees:
                    if db.add_ebay_fee(fee):
                        stats['fees_stored'] += 1

            # ====================================================================
            # STEP 3A.5: Build Payout Date Lookup
            # ====================================================================
            update_progress('payouts', 'Resolving payout dates...')
            payout_date_lookup = build_payout_date_lookup(start_date, end_date)

            # ====================================================================
            # STEP 3B: Store Transactions in ebay_transactions Table
            # ====================================================================
            update_progress('transactions', 'Storing transaction records...')
            
            for trans in transactions:
                transaction_record = parse_api_transaction(trans, payout_date_lookup)
                if transaction_record:
                    if db.add_ebay_transaction_from_api(transaction_record):
                        stats['transactions_stored'] += 1
        
        # ====================================================================
        # STEP 4: Update Sync Timestamp
        # ====================================================================
        update_progress('complete', 'Sync complete!')
        
        db.save_setting('ebay_last_sync', datetime.now().isoformat())
        stats['success'] = len(stats['errors']) == 0
        
    except Exception as e:
        stats['errors'].append(f"Unexpected error: {str(e)}")
        stats['success'] = False
    
    return stats


def get_sync_status() -> Dict:
    """
    Get current eBay sync status
    
    Returns:
        Dict with status information
    """
    last_sync = db.get_setting('ebay_last_sync')
    environment = db.get_setting('ebay_environment') or 'sandbox'
    has_credentials = bool(db.get_setting('ebay_user_token'))
    
    return {
        'last_sync': last_sync,
        'environment': environment,
        'has_credentials': has_credentials
    }


# ============================================================================
# CREDENTIALS MANAGEMENT
# ============================================================================

def save_ebay_credentials(
    app_id: str,
    dev_id: str,
    cert_id: str,
    user_token: str,
    environment: str = 'sandbox'
) -> bool:
    """
    Save eBay API credentials to settings
    
    Args:
        app_id: eBay App ID (Client ID)
        dev_id: eBay Dev ID
        cert_id: eBay Cert ID (Client Secret)
        user_token: User Token for authentication
        environment: 'sandbox' or 'production'
    
    Returns:
        True if saved successfully
    """
    try:
        db.save_setting('ebay_app_id', app_id)
        db.save_setting('ebay_dev_id', dev_id)
        db.save_setting('ebay_cert_id', cert_id)
        db.save_setting('ebay_user_token', user_token)
        db.save_setting('ebay_environment', environment)
        return True
    except Exception as e:
        print(f"Error saving credentials: {e}")
        return False


def get_ebay_credentials() -> Dict:
    """
    Get eBay API credentials (masked for display)
    
    Returns:
        Dict with credential info (tokens masked)
    """
    app_id = db.get_setting('ebay_app_id') or ''
    dev_id = db.get_setting('ebay_dev_id') or ''
    cert_id = db.get_setting('ebay_cert_id') or ''
    user_token = db.get_setting('ebay_user_token') or ''
    environment = db.get_setting('ebay_environment') or 'sandbox'
    
    # Mask sensitive values
    def mask_value(val: str, visible: int = 8) -> str:
        if not val:
            return '[Not Set]'
        if len(val) <= visible * 2:
            return '*' * len(val)
        return val[:visible] + '...' + val[-visible:]
    
    return {
        'app_id': mask_value(app_id),
        'app_id_full': app_id,
        'dev_id': mask_value(dev_id),
        'dev_id_full': dev_id,
        'cert_id': mask_value(cert_id),
        'cert_id_full': cert_id,
        'user_token': mask_value(user_token, 12),
        'user_token_full': user_token,
        'environment': environment,
        'has_credentials': bool(user_token)
    }


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def format_date_range(start_date: datetime, end_date: datetime) -> str:
    """Format date range for display"""
    return f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"


def process_ebay_api_to_sales() -> Dict:
    """
    Process synced eBay API data into the sales table.
    
    This function:
    1. Reads from ebay_orders, ebay_line_items, ebay_fees tables
    2. Extracts purchase_id from SKU using existing logic
    3. Aggregates fees by order
    4. Creates records in the sales table
    5. Skips orders that already exist in sales (by order_number)
    
    Returns:
        Dict with processing statistics
    """
    stats = {
        'orders_processed': 0,
        'line_items_processed': 0,
        'sales_created': 0,
        'sales_skipped_duplicate': 0,
        'sales_skipped_no_items': 0,
        'errors': []
    }
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    try:
        # Get all synced orders that are FULFILLED (exclude cancelled/unpaid)
        cursor.execute("""
            SELECT * FROM ebay_orders
            WHERE order_status = 'FULFILLED'
            ORDER BY created_date DESC
        """)
        orders = cursor.fetchall()
        
        for order in orders:
            order_id = order['order_id']
            order_number = order['order_number'] or order_id
            created_date = order['created_date']
            
            # Extract just the date portion
            if created_date and 'T' in str(created_date):
                sale_date = str(created_date).split('T')[0]
            else:
                sale_date = str(created_date)[:10] if created_date else None
            
            stats['orders_processed'] += 1
            
            # Get line items for this order
            cursor.execute("""
                SELECT * FROM ebay_line_items
                WHERE order_id = ?
            """, (order_id,))
            line_items = cursor.fetchall()
            
            if not line_items:
                stats['sales_skipped_no_items'] += 1
                continue
            
            # Get fees for this order (aggregate by fee type)
            cursor.execute("""
                SELECT fee_type, SUM(amount) as total_amount
                FROM ebay_fees
                WHERE order_id = ?
                GROUP BY fee_type
            """, (order_id,))
            fees_rows = cursor.fetchall()
            
            # Build fee dictionary
            fees = {}
            for fee_row in fees_rows:
                fees[fee_row['fee_type']] = fee_row['total_amount']
            
            # Extract itemized fees using correct fee type names from eBay
            # FINAL_VALUE_FEE = variable percentage fee
            # FINAL_VALUE_FEE_FIXED_PER_ORDER = fixed fee per order ($0.30 or $0.40)
            # SHIPPING_LABEL = eBay Standard Envelope postage (separate transaction type)
            # PROMOTED_LISTING_FEE = Ad fees from NON_SALE_CHARGE transactions
            final_value_fee_variable = fees.get('FINAL_VALUE_FEE', 0)
            final_value_fee_fixed = fees.get('FINAL_VALUE_FEE_FIXED_PER_ORDER', fees.get('FINAL_VALUE_FEE_FIXED', 0))
            regulatory_fee = fees.get('REGULATORY_OPERATING_FEE', 0)
            international_fee = fees.get('INTERNATIONAL_FEE', 0)
            
            # Get promoted listing fee (from NON_SALE_CHARGE transactions)
            ad_fee = fees.get('PROMOTED_LISTING_FEE', fees.get('AD_FEE', 0))
            
            # Get shipping label cost directly (from SHIPPING_LABEL transactions)
            ebay_shipping_cost = fees.get('SHIPPING_LABEL', 0)
            
            # Process each line item as a separate sale
            num_items = len(line_items)
            
            for item in line_items:
                line_item_id = item['line_item_id']
                item_title = item['item_title']
                sku = item['sku']
                quantity = item['quantity'] or 1
                unit_price = item['unit_price'] or 0
                line_total = item['line_total'] or (unit_price * quantity)
                
                stats['line_items_processed'] += 1
                
                # Check if this line item already exists in sales
                cursor.execute("""
                    SELECT sale_id FROM sales
                    WHERE transaction_id = ? OR (order_number = ? AND item_title = ?)
                """, (line_item_id, order_number, item_title))
                
                existing = cursor.fetchone()
                if existing:
                    stats['sales_skipped_duplicate'] += 1
                    continue
                
                # Extract purchase_id from SKU
                purchase_id = db.get_purchase_id_from_sku(sku, item_title)
                
                # Calculate supplies estimate based on line total
                supplies_estimate = db.get_supplies_cost_for_amount(line_total)
                
                # Distribute fees proportionally across line items
                # (simplified: divide evenly for now)
                item_fees_fixed = final_value_fee_fixed / num_items
                item_fees_variable = final_value_fee_variable / num_items
                item_regulatory_fee = regulatory_fee / num_items
                item_international_fee = international_fee / num_items
                item_ad_fee = ad_fee / num_items
                item_shipping_cost = ebay_shipping_cost / num_items
                
                # Calculate net profit (including shipping cost now)
                net_profit = (
                    line_total
                    - item_fees_fixed
                    - item_fees_variable
                    - item_regulatory_fee
                    - item_ad_fee
                    - item_international_fee
                    - item_shipping_cost
                    - supplies_estimate
                )
                
                # Insert into sales table
                cursor.execute("""
                    INSERT INTO sales (
                        purchase_id, platform, order_number, transaction_id, item_title, custom_label,
                        sale_date, quantity, sale_price, shipping_charged, shipping_cost,
                        platform_fees_fixed, platform_fees_variable, regulatory_fee,
                        promoted_listing_fee, international_fee, supplies_estimate, grading_fee, net_profit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    purchase_id,
                    'eBay',
                    order_number,
                    line_item_id,  # Use line_item_id as transaction_id for uniqueness
                    item_title,
                    sku,
                    sale_date,
                    quantity,
                    line_total,
                    0,  # shipping_charged - not tracked separately in API
                    item_shipping_cost,  # shipping_cost extracted from TOTAL_FEE difference
                    item_fees_fixed,
                    item_fees_variable,
                    item_regulatory_fee,
                    item_ad_fee,
                    item_international_fee,
                    supplies_estimate,
                    0,  # grading_fee - linked later via cert number
                    net_profit
                ))
                
                stats['sales_created'] += 1
        
        conn.commit()
        
    except Exception as e:
        stats['errors'].append(f"Processing error: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
    
    return stats


def get_unprocessed_orders_count() -> int:
    """
    Count how many eBay API orders haven't been processed to sales yet.
    
    Returns:
        Number of unprocessed orders
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Count orders that have line items not yet in sales
    cursor.execute("""
        SELECT COUNT(DISTINCT eo.order_id) 
        FROM ebay_orders eo
        JOIN ebay_line_items eli ON eo.order_id = eli.order_id
        WHERE NOT EXISTS (
            SELECT 1 FROM sales s 
            WHERE s.transaction_id = eli.line_item_id
        )
    """)
    
    result = cursor.fetchone()
    conn.close()
    
    return result[0] if result else 0


def match_ebay_standard_envelope_shipping() -> Dict:
    """
    Repair function: Update shipping costs and fees for existing sales records.
    
    This updates existing eBay sales that are missing:
    - SHIPPING_LABEL costs (eBay Standard Envelope postage)
    - FINAL_VALUE_FEE_FIXED_PER_ORDER (fixed transaction fee)
    - PROMOTED_LISTING_FEE (ad fees)
    
    New sales processed after this fix will automatically have correct fees.
    
    Returns:
        Dict with repair statistics
    """
    stats = {
        'sales_checked': 0,
        'sales_updated': 0,
        'shipping_costs_assigned': 0.0,
        'fixed_fees_assigned': 0.0,
        'ad_fees_assigned': 0.0,
        'errors': []
    }
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    try:
        # Find all eBay sales that might need repair
        cursor.execute("""
            SELECT sale_id, order_number, sale_price, 
                   platform_fees_fixed, platform_fees_variable, 
                   regulatory_fee, promoted_listing_fee, international_fee,
                   shipping_cost, net_profit
            FROM sales
            WHERE platform = 'eBay'
        """)
        
        sales_to_check = cursor.fetchall()
        
        for sale in sales_to_check:
            stats['sales_checked'] += 1
            order_number = sale['order_number']
            needs_update = False
            
            # Get fees from ebay_fees for this order
            cursor.execute("""
                SELECT fee_type, SUM(amount) as total_amount
                FROM ebay_fees
                WHERE order_id = ?
                GROUP BY fee_type
            """, (order_number,))
            fees_rows = cursor.fetchall()
            
            if not fees_rows:
                continue
            
            # Build fee dictionary
            fees = {}
            for fee_row in fees_rows:
                fees[fee_row['fee_type']] = fee_row['total_amount']
            
            # Get correct fees from ebay_fees
            correct_fixed_fee = fees.get('FINAL_VALUE_FEE_FIXED_PER_ORDER', fees.get('FINAL_VALUE_FEE_FIXED', 0))
            correct_shipping = fees.get('SHIPPING_LABEL', 0)
            correct_ad_fee = fees.get('PROMOTED_LISTING_FEE', 0)
            
            # Count how many line items share this order to distribute fees
            cursor.execute("""
                SELECT COUNT(*) as count FROM sales
                WHERE order_number = ? AND platform = 'eBay'
            """, (order_number,))
            count_result = cursor.fetchone()
            num_items = count_result['count'] if count_result else 1
            
            # Calculate per-item fees
            fixed_fee_per_item = correct_fixed_fee / num_items
            shipping_per_item = correct_shipping / num_items
            ad_fee_per_item = correct_ad_fee / num_items
            
            # Check what needs updating
            current_fixed = sale['platform_fees_fixed'] or 0
            current_shipping = sale['shipping_cost'] or 0
            current_ad = sale['promoted_listing_fee'] or 0
            
            # Calculate adjustments needed
            fixed_diff = fixed_fee_per_item - current_fixed
            shipping_diff = shipping_per_item - current_shipping
            ad_diff = ad_fee_per_item - current_ad
            
            total_adjustment = fixed_diff + shipping_diff + ad_diff
            
            # Only update if there's a meaningful difference
            if abs(total_adjustment) > 0.001:
                cursor.execute("""
                    UPDATE sales
                    SET platform_fees_fixed = ?,
                        shipping_cost = ?,
                        promoted_listing_fee = ?,
                        net_profit = net_profit - ?
                    WHERE sale_id = ?
                """, (
                    fixed_fee_per_item,
                    shipping_per_item,
                    ad_fee_per_item,
                    total_adjustment,
                    sale['sale_id']
                ))
                
                stats['sales_updated'] += 1
                if shipping_diff > 0:
                    stats['shipping_costs_assigned'] += shipping_diff
                if fixed_diff > 0:
                    stats['fixed_fees_assigned'] += fixed_diff
                if ad_diff > 0:
                    stats['ad_fees_assigned'] += ad_diff
        
        conn.commit()
        
    except Exception as e:
        stats['errors'].append(f"Error repairing sales: {str(e)}")
        conn.rollback()
    finally:
        conn.close()
    
    return stats


def get_unmatched_esus_shipping_summary() -> Dict:
    """
    Get a summary of eBay sales that have $0 shipping cost but may need repair.
    
    Returns:
        Dict with summary information
    """
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # Find sales with $0 shipping that have TOTAL_FEE > itemized fees
    cursor.execute("""
        SELECT COUNT(*) as count
        FROM sales s
        WHERE s.platform = 'eBay'
        AND (s.shipping_cost = 0 OR s.shipping_cost IS NULL)
    """)
    
    result = cursor.fetchone()
    unmatched_count = result['count'] if result else 0
    
    # Get sample of unmatched sales
    cursor.execute("""
        SELECT s.sale_id, s.order_number, s.item_title, s.sale_date, s.sale_price, s.shipping_cost
        FROM sales s
        WHERE s.platform = 'eBay'
        AND (s.shipping_cost = 0 OR s.shipping_cost IS NULL)
        ORDER BY s.sale_date DESC
        LIMIT 10
    """)
    
    unmatched_sales = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return {
        'unmatched_count': unmatched_count,
        'unmatched_sales': unmatched_sales
    }


if __name__ == "__main__":
    # Test the connection if run directly
    print("Testing eBay API connection...")
    success, message = test_api_connection()
    print(f"Result: {message}")
