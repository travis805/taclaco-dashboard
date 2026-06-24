# -*- coding: utf-8 -*-
"""
Taclaco TCG Business Dashboard
Multi-platform sales tracking with eBay, Stripe, Chase bank import
Phase 2: Added Order Fulfillment, Data Source Status, Pirate Ship Import
"""

import streamlit as st
import pandas as pd
import os
from datetime import datetime, timedelta
import database as db
import import_ebay
import import_chase
import import_tag

# Try to import Phase 2 modules (may not exist yet)
try:
    import import_stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False

try:
    import import_pirateship
    PIRATESHIP_AVAILABLE = True
except ImportError:
    PIRATESHIP_AVAILABLE = False

try:
    import import_novo
    NOVO_AVAILABLE = True
except ImportError:
    NOVO_AVAILABLE = False

try:
    import generate_journal_entry as je_gen
    JE_AVAILABLE = True
except ImportError:
    JE_AVAILABLE = False

try:
    import import_ebay_api
    EBAY_API_AVAILABLE = True
except ImportError:
    EBAY_API_AVAILABLE = False

try:
    import amazon_processor
    AMAZON_AVAILABLE = True
except ImportError:
    AMAZON_AVAILABLE = False

# Page config
st.set_page_config(
    page_title="Taclaco Dashboard",
    page_icon="",
    layout="wide"
)

# ── Access gate ──────────────────────────────────────────────────────────────

def check_password() -> bool:
    """Gate the app behind a passphrase stored as a server-side secret.

    APP_PASSWORD comes from Streamlit secrets or the environment. If it is not
    set (local/offline dev), the app is open. On the public Cloud deployment the
    secret is set, so nothing renders and no query runs until it matches.
    """
    import hmac

    expected = None
    try:
        expected = st.secrets.get("APP_PASSWORD")
    except Exception:
        pass
    expected = expected or os.environ.get("APP_PASSWORD")

    if not expected:
        return True  # no passphrase configured -> open (dev convenience)
    if st.session_state.get("authed"):
        return True

    st.title("Taclaco Dashboard")
    with st.form("login"):
        pw = st.text_input("Passphrase", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(pw, expected):
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("Incorrect passphrase")
    return False


if not check_password():
    st.stop()


# Initialize database
db.init_database()


# Sidebar navigation
st.sidebar.title("Taclaco Dashboard")

page = st.sidebar.radio(
    "Navigation",
    ["Home", "Financials", "Sales", "Inventory", "Settings"]
)

# ==========================================================================
# PAGE: HOME
# ==========================================================================

if page == "Home":
    st.title("Taclaco Dashboard")

    _conn = db.get_connection()
    _unk = _conn.execute("SELECT COUNT(*) FROM sales WHERE purchase_id = 'UNKNOWN'").fetchone()[0]
    _pend = _conn.execute("SELECT COUNT(*) FROM transactions WHERE status = 'Pending'").fetchone()[0]
    _conn.close()
    if _unk == 0 and _pend == 0:
        st.success('All clear — no UNKNOWN sales or uncategorized transactions.')
    else:
        if _unk > 0:
            st.warning(f'{_unk} sales need a Purchase ID — go to Sales → Review UNKNOWN.')
        if _pend > 0:
            st.warning(f'{_pend} uncategorized transactions — go to Financials → Wave Export.')
    st.divider()

    st.title("Business Overview")

    # Refresh button
    col_title, col_refresh = st.columns([6, 1])
    with col_refresh:
        if st.button("", key="refresh_overview", help="Refresh data"):
            st.rerun()

    # Get dashboard data
    data = db.get_dashboard_summary()
    summary = data['summary']

    # Metrics row
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "Total Orders",
            f"{summary['total_orders']:,}" if summary['total_orders'] else "0"
        )

    with col2:
        st.metric(
            "Total Revenue",
            f"${summary['total_revenue']:,.2f}" if summary['total_revenue'] else "$0.00"
        )

    with col3:
        st.metric(
            "Total Fees",
            f"${summary['total_fees']:,.2f}" if summary['total_fees'] else "$0.00"
        )

    with col4:
        st.metric(
            "Net Profit",
            f"${summary['total_net_profit']:,.2f}" if summary['total_net_profit'] else "$0.00"
        )

    st.divider()

    # Data Source Status
    st.subheader("Data Source Status")

    try:
        status_df = db.get_all_data_source_status()

        if not status_df.empty:
            # Create status display
            cols = st.columns(6)

            for idx, (_, row) in enumerate(status_df.iterrows()):
                with cols[idx % 6]:
                    source = row['source']
                    total = row['total_transactions'] or 0
                    last_import = row['last_import_date']

                    # Determine status color
                    if last_import is None:
                        status_icon = ""
                        status_text = "Never imported"
                    else:
                        try:
                            last_dt = pd.to_datetime(last_import)
                            days_ago = (datetime.now() - last_dt).days
                            if days_ago <= 7:
                                status_icon = ""
                                status_text = f"{days_ago}d ago"
                            elif days_ago <= 14:
                                status_icon = ""
                                status_text = f"{days_ago}d ago"
                            else:
                                status_icon = ""
                                status_text = f"{days_ago}d ago"
                        except:
                            status_icon = ""
                            status_text = "Unknown"

                    st.markdown(f"**{status_icon} {source}**")
                    st.caption(f"{total:,} records")
                    st.caption(status_text)
    except Exception as e:
        st.info("Data source status not available. Run schema migration first.")

    st.divider()

    # Sales by Purchase
    st.subheader("Sales by Purchase")

    if data['sales_by_purchase']:
        sales_df = pd.DataFrame(data['sales_by_purchase'])
        sales_df['Total Profit'] = sales_df['total_profit'].apply(lambda x: f"${x:,.2f}")
        sales_df['Display Name'] = sales_df['display_name'].fillna(sales_df['purchase_id'])

        st.dataframe(
            sales_df[['Display Name', 'item_count', 'Total Profit']].rename(columns={
                'Display Name': 'Purchase',
                'item_count': 'Items Sold'
            }),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No sales data yet. Import eBay transactions to get started.")

# ==========================================================================
# PAGE: FINANCIALS
# ==========================================================================

elif page == "Financials":
    fin_tab1, fin_tab2, fin_tab3 = st.tabs(["📈 P&L Report", "📒 Wave Export", "📦 Amazon"])

    with fin_tab1:
            st.title("Profit & Loss Report")

            # Filters
            st.subheader("Filters")

            col1, col2, col3 = st.columns(3)

            with col1:
                # Date range filter
                date_filter = st.selectbox(
                    "Date Range",
                    ["All Time", "This Year", "Last Year", "Last Month", "Last Quarter", "Custom"],
                    key="pnl_date_filter"
                )

                if date_filter == "Custom":
                    start_date = st.date_input("Start Date", value=datetime.now() - timedelta(days=365))
                    end_date = st.date_input("End Date", value=datetime.now())
                elif date_filter == "This Year":
                    start_date = datetime(datetime.now().year, 1, 1).date()
                    end_date = datetime.now().date()
                elif date_filter == "Last Year":
                    start_date = datetime(datetime.now().year - 1, 1, 1).date()
                    end_date = datetime(datetime.now().year - 1, 12, 31).date()
                elif date_filter == "Last Month":
                    # Get first day of last month
                    today = datetime.now()
                    first_of_this_month = today.replace(day=1)
                    last_month_end = first_of_this_month - timedelta(days=1)
                    last_month_start = last_month_end.replace(day=1)
                    start_date = last_month_start.date()
                    end_date = last_month_end.date()
                elif date_filter == "Last Quarter":
                    # Get the last complete quarter
                    today = datetime.now()
                    current_quarter = (today.month - 1) // 3 + 1
                    if current_quarter == 1:
                        # Last quarter was Q4 of previous year
                        start_date = datetime(today.year - 1, 10, 1).date()
                        end_date = datetime(today.year - 1, 12, 31).date()
                    else:
                        # Last quarter was in current year
                        last_quarter = current_quarter - 1
                        start_month = (last_quarter - 1) * 3 + 1
                        end_month = last_quarter * 3
                        start_date = datetime(today.year, start_month, 1).date()
                        # Get last day of end_month
                        if end_month == 12:
                            end_date = datetime(today.year, 12, 31).date()
                        else:
                            end_date = (datetime(today.year, end_month + 1, 1) - timedelta(days=1)).date()
                else:
                    start_date = None
                    end_date = None

            with col2:
                # Purchase filter - show ALL purchases, not just those with sales
                conn = db.get_connection()
                purchases_df = db._read_sql(conn, """
                    SELECT purchase_id, display_name
                    FROM purchases
                    ORDER BY purchase_id
                """)
                conn.close()

                purchase_options = ["All Purchases"] + [
                    f"{row['purchase_id']} - {row['display_name'] or 'No name'}" 
                    for _, row in purchases_df.iterrows()
                ]
                purchase_id_map = {"All Purchases": None}
                for _, row in purchases_df.iterrows():
                    key = f"{row['purchase_id']} - {row['display_name'] or 'No name'}"
                    purchase_id_map[key] = row['purchase_id']

                selected_purchase = st.selectbox("Purchase", purchase_options, key="pnl_purchase_filter")
                filter_purchase_id = purchase_id_map[selected_purchase]

            with col3:
                # Platform filter
                platform_options = ["All Platforms", "eBay", "Stripe", "PayPal", "Venmo", "Mercari", "Trade"]
                selected_platform = st.selectbox("Platform", platform_options, key="pnl_platform_filter")
                filter_platform = None if selected_platform == "All Platforms" else selected_platform

            st.divider()

            # Build the P&L query with filters
            conn = db.get_connection()

            # Build WHERE clause
            where_clauses = []
            params = []

            if start_date and end_date:
                where_clauses.append("s.sale_date BETWEEN ? AND ?")
                params.extend([start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')])

            if filter_purchase_id:
                where_clauses.append("s.purchase_id = ?")
                params.append(filter_purchase_id)

            if filter_platform:
                where_clauses.append("s.platform = ?")
                params.append(filter_platform)

            where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            # Get sales data
            sales_query = f"""
                SELECT 
                    COALESCE(SUM(s.sale_price), 0) as gross_sales,
                    COALESCE(SUM(s.shipping_charged), 0) as shipping_charged,
                    COALESCE(SUM(s.platform_fees_fixed), 0) as fees_fixed,
                    COALESCE(SUM(s.platform_fees_variable), 0) as fees_variable,
                    COALESCE(SUM(s.regulatory_fee), 0) as regulatory_fees,
                    COALESCE(SUM(s.promoted_listing_fee), 0) as promoted_fees,
                    COALESCE(SUM(s.international_fee), 0) as international_fees,
                    COALESCE(SUM(s.shipping_cost), 0) as shipping_costs,
                    COALESCE(SUM(s.supplies_estimate), 0) as supplies,
                    COALESCE(SUM(s.grading_fee), 0) as grading_fees,
                    COALESCE(SUM(s.net_profit), 0) as net_profit,
                    COUNT(*) as sale_count
                FROM sales s
                {where_sql}
            """

            cursor = conn.cursor()
            cursor.execute(sales_query, params)
            result = cursor.fetchone()

            # Get revenue breakdown by type
            # Trade revenue
            trade_params = params.copy() if params else []
            trade_where = where_sql + (" AND " if where_sql else "WHERE ") + "s.platform = 'Trade'"
            cursor.execute(f"""
                SELECT COALESCE(SUM(s.sale_price), 0) as trade_revenue
                FROM sales s
                {trade_where}
            """, trade_params)
            trade_result = cursor.fetchone()
            trade_revenue = trade_result['trade_revenue'] if trade_result else 0

            # Get revenue by category from linked transactions (collections vs new product)
            # This requires joining sales to transactions via purchase_id and looking at categories
            collections_revenue = 0
            new_product_revenue = 0
            other_revenue = 0

            if filter_purchase_id:
                # Get the category for this purchase from transactions
                cursor.execute("""
                    SELECT category FROM transactions 
                    WHERE purchase_id = ? AND amount < 0
                    LIMIT 1
                """, (filter_purchase_id,))
                cat_result = cursor.fetchone()
                purchase_category = cat_result['category'] if cat_result else ''

                # Non-trade sales for this purchase
                non_trade_sales = result['gross_sales'] - trade_revenue

                if 'collection' in purchase_category.lower():
                    collections_revenue = non_trade_sales
                elif 'new product' in purchase_category.lower():
                    new_product_revenue = non_trade_sales
                else:
                    other_revenue = non_trade_sales
            else:
                # For "All Purchases" view, break down by purchase category
                cursor.execute(f"""
                    SELECT 
                        CASE 
                            WHEN t.category LIKE '%collection%' THEN 'collections'
                            WHEN t.category LIKE '%new product%' THEN 'new_product'
                            ELSE 'other'
                        END as rev_type,
                        COALESCE(SUM(s.sale_price), 0) as revenue
                    FROM sales s
                    LEFT JOIN (
                        SELECT DISTINCT purchase_id, category 
                        FROM transactions 
                        WHERE amount < 0
                    ) t ON s.purchase_id = t.purchase_id
                    {where_sql + " AND " if where_sql else "WHERE "} s.platform != 'Trade'
                    GROUP BY rev_type
                """, params if params else [])

                for row in cursor.fetchall():
                    # row is (rev_type, revenue) positionally
                    _rev_type = row[0] if not hasattr(row, 'keys') else row['rev_type']
                    _revenue = row[1] if not hasattr(row, 'keys') else row['revenue']
                    if _rev_type == 'collections':
                        collections_revenue = _revenue
                    elif _rev_type == 'new_product':
                        new_product_revenue = _revenue
                    else:
                        other_revenue = _revenue

            # Get expense data (COGS) for filtered purchases
            # Sum all negative transactions linked to this purchase (expenses are negative amounts)
            # Also get trade cost basis from RECEIVE trade lines
            cogs_cash = 0
            cogs_trade = 0

            if filter_purchase_id:
                # Cash COGS - from transactions (actual cash spent)
                cursor.execute("""
                    SELECT COALESCE(SUM(amount), 0) as total_cogs
                    FROM transactions
                    WHERE purchase_id = ?
                    AND amount < 0
                """, (filter_purchase_id,))
                cogs_result = cursor.fetchone()
                cogs_cash = abs(cogs_result[0]) if cogs_result else 0

                # Trade COGS - from trade_lines RECEIVE (value received via trades)
                cursor.execute("""
                    SELECT COALESCE(SUM(tl.value), 0) as trade_cogs
                    FROM trade_lines tl
                    WHERE tl.purchase_id = ?
                    AND tl.direction = 'RECEIVE'
                    AND tl.line_type = 'inventory'
                """, (filter_purchase_id,))
                trade_cogs_result = cursor.fetchone()
                cogs_trade = trade_cogs_result[0] if trade_cogs_result else 0
            else:
                # Get all COGS for date range - sum negative transactions with a purchase_id
                cogs_where = []
                cogs_params = []
                if start_date and end_date:
                    cogs_where.append("transaction_date BETWEEN ? AND ?")
                    cogs_params.extend([start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')])

                cogs_where_sql = "AND " + " AND ".join(cogs_where) if cogs_where else ""

                # Cash COGS
                cursor.execute(f"""
                    SELECT COALESCE(SUM(amount), 0) as total_cogs
                    FROM transactions
                    WHERE purchase_id IS NOT NULL
                    AND purchase_id != ''
                    AND amount < 0
                    {cogs_where_sql}
                """, cogs_params)
                cogs_result = cursor.fetchone()
                cogs_cash = abs(cogs_result[0]) if cogs_result else 0

                # Trade COGS - all RECEIVE trade lines (with date filter if applicable)
                trade_where = ""
                trade_params = []
                if start_date and end_date:
                    trade_where = """
                        AND tl.trade_id IN (
                            SELECT trade_id FROM trades 
                            WHERE trade_date BETWEEN ? AND ?
                        )
                    """
                    trade_params = [start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')]

                cursor.execute(f"""
                    SELECT COALESCE(SUM(tl.value), 0) as trade_cogs
                    FROM trade_lines tl
                    WHERE tl.direction = 'RECEIVE'
                    AND tl.line_type = 'inventory'
                    {trade_where}
                """, trade_params)
                trade_cogs_result = cursor.fetchone()
                cogs_trade = trade_cogs_result[0] if trade_cogs_result else 0

            cogs = cogs_cash + cogs_trade

            # Get allocation amounts for this purchase (if filtered)
            allocated_out = 0  # Amount allocated FROM this purchase to others
            allocated_in = 0   # Amount allocated TO this purchase from others
            if filter_purchase_id:
                # Allocated OUT (from this purchase)
                cursor.execute("""
                    SELECT COALESCE(SUM(amount), 0) as total_out
                    FROM profit_allocations
                    WHERE source_purchase_id = ?
                """, (filter_purchase_id,))
                out_result = cursor.fetchone()
                allocated_out = out_result[0] or 0

                # Allocated IN (to this purchase)
                cursor.execute("""
                    SELECT COALESCE(SUM(amount), 0) as total_in
                    FROM profit_allocations
                    WHERE target_purchase_id = ?
                """, (filter_purchase_id,))
                in_result = cursor.fetchone()
                allocated_in = in_result[0] or 0

            conn.close()

            # Calculate totals
            gross_sales = result['gross_sales']
            shipping_charged = result['shipping_charged']
            total_revenue = gross_sales + shipping_charged

            fees_fixed = result['fees_fixed']
            fees_variable = result['fees_variable']
            regulatory_fees = result['regulatory_fees']
            promoted_fees = result['promoted_fees']
            international_fees = result['international_fees']
            total_platform_fees = fees_fixed + fees_variable + regulatory_fees + promoted_fees + international_fees

            shipping_costs = result['shipping_costs']
            supplies = result['supplies']
            grading_fees = result['grading_fees']
            total_operating_costs = shipping_costs + supplies + grading_fees

            # Corporate overhead (5% of revenue)
            corporate_overhead = total_revenue * 0.05

            gross_profit = total_revenue - cogs
            net_operating_profit = gross_profit - total_platform_fees - total_operating_costs - corporate_overhead

            # Net profit after allocations (out - in)
            net_after_allocation = net_operating_profit - allocated_out + allocated_in

            # Display P&L Statement
            st.subheader("P&L Statement")

            col1, col2 = st.columns([2, 1])

            with col1:
                # Revenue Section with breakdown
                st.markdown("**REVENUE**")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Collections: ${collections_revenue:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;New Products: ${new_product_revenue:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Other Merchandise: ${other_revenue:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Trades: ${trade_revenue:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;*Gross Sales: ${gross_sales:,.2f}*")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Shipping Charged: ${shipping_charged:,.2f}")
                st.markdown(f"**Total Revenue: ${total_revenue:,.2f}**")

                st.divider()

                # COGS Section with cash/trade breakdown
                st.markdown("**COST OF GOODS SOLD**")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Cash Purchases: ${cogs_cash:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Trade Acquisitions: ${cogs_trade:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;*Total COGS: ${cogs:,.2f}*")
                st.markdown(f"**Gross Profit: ${gross_profit:,.2f}**")

                st.divider()

                # Platform Fees Section
                st.markdown("**PLATFORM FEES**")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Fixed Fees: ${fees_fixed:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Variable Fees: ${fees_variable:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Regulatory Fees: ${regulatory_fees:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Promoted Listing Fees: ${promoted_fees:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;International Fees: ${international_fees:,.2f}")
                st.markdown(f"**Total Platform Fees: ${total_platform_fees:,.2f}**")

                st.divider()

                # Operating Costs Section
                st.markdown("**OPERATING COSTS**")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Shipping Costs: ${shipping_costs:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Supplies: ${supplies:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Grading Fees: ${grading_fees:,.2f}")
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Corporate Overhead (5%): ${corporate_overhead:,.2f}")
                st.markdown(f"**Total Operating Costs: ${total_operating_costs + corporate_overhead:,.2f}**")

                st.divider()

                # Net Operating Profit
                st.markdown(f"### Net Operating Profit: ${net_operating_profit:,.2f}")

                # Allocation section (only show when filtered by purchase)
                if filter_purchase_id:
                    st.divider()
                    st.markdown("**PURCHASE ALLOCATION**")
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Allocated out to other purchases: ${allocated_out:,.2f}")
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;Allocated in from other purchases: ${allocated_in:,.2f}")
                    st.markdown(f"**Available for Allocation: ${net_after_allocation:,.2f}**")

                    # Allocation UI - Add new allocation
                    st.divider()
                    with st.expander("➕ Record New Allocation", expanded=False):
                        # Get list of purchases for target dropdown
                        conn_alloc = db.get_connection()
                        target_purchases = db._read_sql(conn_alloc, """
                            SELECT purchase_id, display_name 
                            FROM purchases 
                            WHERE purchase_id != ?
                            ORDER BY display_name
                        """,  params=(filter_purchase_id,))
                        conn_alloc.close()

                        # Build target options - default LTH at top
                        target_options = {"LTH - Long Term Hold": "LTH", "PC - Personal Collection": "PC"}
                        for _, p in target_purchases.iterrows():
                            if p['purchase_id'] not in ['LTH', 'PC']:
                                label = f"{p['purchase_id']} - {p['display_name'] or 'No name'}"
                                target_options[label] = p['purchase_id']

                        col1, col2 = st.columns(2)
                        with col1:
                            alloc_amount = st.number_input(
                                "Amount to Allocate ($)", 
                                min_value=0.0, 
                                max_value=float(max(net_after_allocation, 0)),
                                step=100.0,
                                key="alloc_amount"
                            )
                        with col2:
                            alloc_target_label = st.selectbox(
                                "Allocate To",
                                list(target_options.keys()),
                                key="alloc_target"
                            )
                            alloc_target = target_options[alloc_target_label]

                        alloc_date = st.date_input("Allocation Date", value=datetime.now(), key="alloc_date")
                        alloc_notes = st.text_input("Notes (optional)", key="alloc_notes", placeholder="e.g., January 2026 allocation")

                        if st.button("✅ Record Allocation", type="primary", key="submit_allocation"):
                            if alloc_amount > 0:
                                db.add_profit_allocation(
                                    source_purchase_id=filter_purchase_id,
                                    amount=alloc_amount,
                                    target_purchase_id=alloc_target,
                                    allocation_date=alloc_date.strftime('%Y-%m-%d'),
                                    notes=alloc_notes if alloc_notes else None
                                )
                                st.success(f"✅ Allocated ${alloc_amount:,.2f} from {filter_purchase_id} to {alloc_target}")
                                st.rerun()
                            else:
                                st.error("Please enter an amount greater than $0")

                    # Show allocation history for this purchase
                    alloc_history = db.get_allocations_for_purchase(filter_purchase_id)
                    if not alloc_history.empty:
                        with st.expander(f"📋 Allocation History ({len(alloc_history)} records)", expanded=False):
                            for _, alloc in alloc_history.iterrows():
                                col1, col2, col3 = st.columns([3, 1, 0.5])
                                with col1:
                                    target_display = alloc['target_name'] or alloc['target_purchase_id']
                                    st.markdown(f"**${alloc['amount']:,.2f}** → {target_display}")
                                    if alloc['notes']:
                                        st.caption(alloc['notes'])
                                with col2:
                                    st.caption(str(alloc['allocation_date']))
                                with col3:
                                    if st.button("🗑️", key=f"del_alloc_{alloc['allocation_id']}", help="Delete allocation"):
                                        db.delete_profit_allocation(alloc['allocation_id'])
                                        st.rerun()
                                st.divider()

            with col2:
                st.metric("Sales Count", result['sale_count'])
                st.metric("Avg Sale", f"${gross_sales / result['sale_count']:,.2f}" if result['sale_count'] > 0 else "$0.00")

                # Margin percentages
                if total_revenue > 0:
                    gross_margin = (gross_profit / total_revenue) * 100
                    net_margin = (net_operating_profit / total_revenue) * 100
                    st.metric("Gross Margin", f"{gross_margin:.1f}%")
                    st.metric("Net Margin", f"{net_margin:.1f}%")

            # Reconciliation section
            st.divider()
            with st.expander("Reconciliation Details"):
                st.markdown("**Compare calculated vs stored values:**")
                stored_net_profit = result['net_profit']
                # Stored net_profit doesn't include COGS (it's revenue - fees - shipping)
                # So we compare stored to: revenue - fees - operating costs (no COGS, no overhead)
                comparable_calc = total_revenue - total_platform_fees - total_operating_costs

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("Stored net_profit (sum)", f"${stored_net_profit:,.2f}")
                with col2:
                    st.metric("Revenue - Fees - OpCosts", f"${comparable_calc:,.2f}")
                with col3:
                    diff = stored_net_profit - comparable_calc
                    st.metric("Difference", f"${diff:,.2f}", delta=f"${diff:,.2f}" if abs(diff) > 0.01 else None)

                st.caption("Note: Stored net_profit = sale_price - fees - shipping. COGS and overhead are calculated separately in P&L view.")

                # Show full breakdown
                st.markdown("---")
                st.markdown("**Full P&L Breakdown:**")
                st.markdown(f"- Total Revenue: ${total_revenue:,.2f}")
                st.markdown(f"- Less COGS: -${cogs:,.2f}")
                st.markdown(f"- Less Platform Fees: -${total_platform_fees:,.2f}")
                st.markdown(f"- Less Operating Costs: -${total_operating_costs:,.2f}")
                st.markdown(f"- Less Corporate Overhead (5%): -${corporate_overhead:,.2f}")
                st.markdown(f"- **= Net Operating Profit: ${net_operating_profit:,.2f}**")

            # Sales detail
            if filter_purchase_id:
                st.divider()
                with st.expander("View Sales Details"):
                    conn = db.get_connection()
                    sales_detail = db._read_sql(conn, f"""
                        SELECT 
                            sale_date,
                            platform,
                            item_title,
                            sale_price,
                            shipping_cost,
                            platform_fees_fixed + platform_fees_variable as platform_fees,
                            net_profit
                        FROM sales s
                        {where_sql}
                        ORDER BY sale_date DESC
                    """,  params=params)
                    conn.close()

                    if not sales_detail.empty:
                        st.dataframe(sales_detail, use_container_width=True, hide_index=True)
                    else:
                        st.info("No sales found for this filter.")

                # Expense transactions for this purchase
                with st.expander("View Expense Transactions"):
                    conn = db.get_connection()
                    expenses = db._read_sql(conn, """
                        SELECT 
                            transaction_date,
                            source,
                            description,
                            category,
                            amount
                        FROM transactions
                        WHERE purchase_id = ?
                        ORDER BY transaction_date DESC
                    """,  params=(filter_purchase_id,))
                    conn.close()

                    if not expenses.empty:
                        st.dataframe(expenses, use_container_width=True, hide_index=True)
                    else:
                        st.info("No expense transactions linked to this purchase.")

        # ============================================================================
        # PAGE: MONTHLY CHECKLIST
        # ============================================================================


    with fin_tab2:
            st.title("Wave Export — Journal Entry Generator")
            st.caption("Generate monthly journal entries for Wave Accounting via Wave Connect")

            if not JE_AVAILABLE:
                st.error("Journal entry module not found. Ensure `generate_journal_entry.py` is in your project folder.")
            elif not NOVO_AVAILABLE:
                st.error("Novo import module not found. Ensure `import_novo.py` is in your project folder.")
            else:
                conn = db.get_connection()

                # ── Month Selector ────────────────────────────────────────────
                st.markdown("### Select Month")

                col1, col2, col3 = st.columns([1, 1, 2])
                with col1:
                    je_year = st.selectbox("Year", [2025, 2026], index=0, key="je_year")
                with col2:
                    month_names = ["January", "February", "March", "April", "May", "June",
                                  "July", "August", "September", "October", "November", "December"]
                    je_month_name = st.selectbox("Month", month_names, index=0, key="je_month")
                    je_month = month_names.index(je_month_name) + 1
                with col3:
                    st.markdown("")  # spacer
                    st.markdown("")
                    generate_btn = st.button("Generate Journal Entry", type="primary", key="je_generate")

                st.divider()

                if generate_btn or st.session_state.get('je_last_result'):
                    # Generate or use cached result
                    if generate_btn:
                        with st.spinner(f"Generating {je_month_name} {je_year} journal entry..."):
                            result = je_gen.generate_journal_entry(conn, je_year, je_month)
                            st.session_state['je_last_result'] = result
                            st.session_state['je_last_month'] = f"{je_month_name} {je_year}"
                    else:
                        result = st.session_state['je_last_result']

                    month_label = result['month_label']

                    # ── Pre-Export Reconciliation Checks ──────────────────────
                    st.markdown(f"### Pre-Export Checks — {month_label}")

                    has_blockers = False
                    for check in result['recon_checks']:
                        if check['status'] == 'PASS':
                            st.success(f"**{check['name']}**: {check['detail']}")
                        elif check['status'] == 'INFO':
                            st.info(f"**{check['name']}**: {check['detail']}")
                        elif check['status'] == 'WARN':
                            st.warning(f"**{check['name']}**: {check['detail']}")
                        else:
                            st.error(f"**{check['name']}**: {check['detail']}")
                            has_blockers = True

                    # Missing Wave accounts check
                    missing_accounts = je_gen.check_wave_accounts(result)
                    if missing_accounts:
                        st.error(f"**Missing Wave Accounts**: {', '.join(sorted(missing_accounts))} — create these in Wave before importing")
                        has_blockers = True

                    if has_blockers:
                        st.warning("⚠ Resolve blocking issues above before exporting to Wave.")

                    st.divider()

                    # ── Balance Summary ───────────────────────────────────────
                    st.markdown(f"### {month_label} — Summary")

                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total Debits", f"${result['totals']['debits']:,.2f}")
                    col2.metric("Total Credits", f"${result['totals']['credits']:,.2f}")
                    balanced = result['totals']['balanced']
                    col3.metric("Balanced", "✅ Yes" if balanced else "❌ No", 
                               delta="OK" if balanced else "INVESTIGATE",
                               delta_color="normal" if balanced else "inverse")

                    # Novo reconciliation
                    novo_dr = sum(l['debit'] for l in result['all_lines'] if l['account'] == je_gen.NOVO_BANK)
                    novo_cr = sum(l['credit'] for l in result['all_lines'] if l['account'] == je_gen.NOVO_BANK)
                    novo_net = round(novo_dr - novo_cr, 2)

                    st.info(f"**Novo Bank net change: ${novo_net:+,.2f}** (deposits: ${novo_dr:,.2f}, withdrawals: ${novo_cr:,.2f})")

                    st.divider()

                    # ── Section-by-Section Detail ─────────────────────────────
                    st.markdown(f"### Journal Entry Detail")

                    for code, label, section_lines, metadata in result['sections']:
                        with st.expander(f"Section {code}: {label}" + 
                                       (f" — ${sum(l['debit'] for l in section_lines):,.2f}" if section_lines else " — no activity"),
                                       expanded=bool(section_lines)):

                            if not section_lines:
                                note = metadata.get('note', 'No activity this month')
                                if metadata.get('count', 0) > 0:
                                    st.caption(f"{metadata['count']} refund(s) — revenue impact ${metadata.get('refund_revenue', 0):.2f} "
                                             f"(included in Sections A/B via reversal lines)")
                                else:
                                    st.caption(note)
                                continue

                            # Build display table
                            display_data = []
                            for sl in section_lines:
                                display_data.append({
                                    'Account': sl['account'],
                                    'Debit': f"${sl['debit']:,.2f}" if sl['debit'] > 0 else "",
                                    'Credit': f"${sl['credit']:,.2f}" if sl['credit'] > 0 else "",
                                    'Description': sl['description'],
                                })

                            st.dataframe(
                                pd.DataFrame(display_data),
                                use_container_width=True,
                                hide_index=True,
                            )

                            section_dr = sum(sl['debit'] for sl in section_lines)
                            section_cr = sum(sl['credit'] for sl in section_lines)
                            st.caption(f"Section {code} total: DR ${section_dr:,.2f} / CR ${section_cr:,.2f}")

                    st.divider()

                    # ── Export to Wave Connect ────────────────────────────────
                    st.markdown("### Export for Wave Connect")

                    if not balanced:
                        st.error("Journal entry is NOT balanced. Do not export until resolved.")
                    else:
                        csv_content = je_gen.generate_wave_csv(result)

                        st.markdown("""
                        **Instructions:**
                        1. Download the CSV below
                        2. Open your Wave Connect Google Sheet
                        3. Clear the existing data rows (keep headers)
                        4. Paste the CSV data starting at row 2
                        5. Use Wave Connect → Upload Journal to sync
                        """)

                        col1, col2 = st.columns(2)
                        with col1:
                            safe_month = month_label.replace(' ', '_')
                            st.download_button(
                                label=f"Download Wave CSV — {month_label}",
                                data=csv_content,
                                file_name=f"wave_je_{je_year}_{je_month:02d}.csv",
                                mime="text/csv",
                                type="primary",
                                key="je_download_csv"
                            )
                        with col2:
                            md_content = je_gen.generate_markdown_report(result)
                            st.download_button(
                                label=f"Download Report — {month_label}",
                                data=md_content,
                                file_name=f"je_report_{je_year}_{je_month:02d}.md",
                                mime="text/markdown",
                                key="je_download_md"
                            )

                        # Preview the CSV
                        with st.expander("Preview Wave CSV", expanded=False):
                            preview_data = []
                            for line in result['wave_csv_lines']:
                                wave_id = je_gen.WAVE_IDS.get(line['Account Name'], '')
                                preview_data.append({
                                    'Wave Id': wave_id[:8] + '...' if wave_id else '⚠ MISSING',
                                    'Account Name': line['Account Name'],
                                    'Debit': line['Debit'],
                                    'Credit': line['Credit'],
                                    'Description': line['Description'],
                                })
                            st.dataframe(pd.DataFrame(preview_data), use_container_width=True, hide_index=True)

                conn.close()


        # ============================================================================
        # PAGE: SALES
        # ============================================================================


    with fin_tab3:
        st.info('Amazon settlement import is handled via Claude. View-only summaries coming soon.')

# ==========================================================================
# PAGE: SALES
# ==========================================================================

elif page == "Sales":
    sales_tab1, sales_tab2, sales_tab3 = st.tabs(["🧾 Sales List", "❓ Review UNKNOWN", "📬 Orders"])

    with sales_tab1:
            st.title("Sales")

            # Refresh button
            col_title, col_refresh = st.columns([6, 1])
            with col_refresh:
                if st.button("", key="refresh_sales", help="Refresh data"):
                    st.rerun()

            sales_df = db.get_all_sales()

            if sales_df.empty:
                st.info("No sales yet. Import eBay transactions or add manual entries below.")
            else:
                # Summary metrics
                col1, col2, col3, col4 = st.columns(4)

                with col1:
                    st.metric("Total Sales", len(sales_df))
                with col2:
                    st.metric("Revenue", f"${sales_df['sale_price'].sum():,.2f}")
                with col3:
                    total_fees = (
                        sales_df['platform_fees_fixed'].sum() +
                        sales_df['platform_fees_variable'].sum() +
                        sales_df['regulatory_fee'].sum() +
                        sales_df['promoted_listing_fee'].sum()
                    )
                    st.metric("Total Fees", f"${total_fees:,.2f}")
                with col4:
                    st.metric("Net Profit", f"${sales_df['net_profit'].sum():,.2f}")

                st.divider()

                # Filter options
                col1, col2 = st.columns(2)
                with col1:
                    # Ensure platform values are strings and filter out None/NaN
                    platform_values = [str(p) for p in sales_df['platform'].dropna().unique() if p]
                    platforms = ['All'] + sorted(platform_values)
                    selected_platform = st.selectbox("Filter by Platform", platforms)
                with col2:
                    # Get purchases with display names for better dropdown
                    purchases_df = db.get_all_purchases()
                    purchase_options = ['All']
                    purchase_display_map = {'All': 'All'}

                    for _, p in purchases_df.iterrows():
                        pid = str(p['purchase_id']) if p['purchase_id'] else None
                        if pid:
                            purchase_options.append(pid)
                            display = f"{pid} - {p['display_name']}" if p['display_name'] else pid
                            purchase_display_map[pid] = display

                    # Add any purchase_ids from sales that aren't in purchases table
                    for pid in sales_df['purchase_id'].dropna().unique():
                        pid_str = str(pid)
                        if pid_str and pid_str not in purchase_display_map:
                            purchase_options.append(pid_str)
                            purchase_display_map[pid_str] = pid_str

                    selected_purchase = st.selectbox(
                        "Filter by Purchase", 
                        options=purchase_options,
                        format_func=lambda x: purchase_display_map.get(x, str(x))
                    )

                # Apply filters
                filtered_df = sales_df.copy()
                if selected_platform != 'All':
                    filtered_df = filtered_df[filtered_df['platform'] == selected_platform]
                if selected_purchase != 'All':
                    # Convert purchase_id to string for comparison since selectbox returns string
                    filtered_df = filtered_df[filtered_df['purchase_id'].astype(str) == selected_purchase]

                # Calculate total platform fees for display
                filtered_df['platform_fees'] = (
                    filtered_df['platform_fees_fixed'].fillna(0) +
                    filtered_df['platform_fees_variable'].fillna(0) +
                    filtered_df['regulatory_fee'].fillna(0) +
                    filtered_df['promoted_listing_fee'].fillna(0) +
                    filtered_df['international_fee'].fillna(0)
                )

                # Display
                st.dataframe(
                    filtered_df[[
                        'sale_date', 'platform', 'item_title', 'sale_price', 
                        'platform_fees', 'shipping_cost', 'net_profit', 'purchase_id'
                    ]].rename(columns={
                        'sale_date': 'Date',
                        'platform': 'Platform',
                        'item_title': 'Item',
                        'sale_price': 'Price',
                        'platform_fees': 'Fees',
                        'shipping_cost': 'Shipping',
                        'net_profit': 'Profit',
                        'purchase_id': 'Purchase'
                    }),
                    use_container_width=True,
                    hide_index=True
                )


        # ============================================================================
        # PAGE: PURCHASES
        # ============================================================================


    with sales_tab2:
            st.title("Review UNKNOWN Sales")

            # Refresh button
            col_title, col_refresh = st.columns([6, 1])
            with col_refresh:
                if st.button("Refresh", key="refresh_unknown", help="Refresh data"):
                    st.rerun()

            unknown_df = db.get_unknown_sales()

            if unknown_df.empty:
                st.success("No unknown sales! All items are properly categorized.")
            else:
                st.warning(f"{len(unknown_df)} sales need Purchase ID assignment")

                # Get purchases for dropdown with display names
                purchases_df = db.get_all_purchases()

                # Create dropdown options with display name AND purchase_id
                purchase_options = ['UNKNOWN']
                purchase_id_map = {'UNKNOWN': 'UNKNOWN'}

                for _, p in purchases_df.iterrows():
                    display_name = p.get('display_name') or p.get('item_description') or f"Purchase #{p['purchase_id']}"
                    # Format: "Display Name (P#123)"
                    option_label = f"{display_name} (P#{p['purchase_id']})"
                    purchase_options.append(option_label)
                    purchase_id_map[option_label] = p['purchase_id']

                # Process unknown sales - split Stripe orders by product line
                processed_items = []

                for _, row in unknown_df.iterrows():
                    platform = row.get('platform', '')
                    item_title = row.get('item_title', '')

                    # For Stripe sales, check if item_title contains multiple products (comma-separated)
                    if platform == 'Stripe' and ',' in item_title:
                        # Split by comma to get individual products
                        products = [p.strip() for p in item_title.split(',')]

                        for i, product in enumerate(products):
                            # Check for quantity indicator like "(x2)"
                            qty = 1
                            if '(x' in product:
                                import re
                                qty_match = re.search(r'\(x(\d+)\)', product)
                                if qty_match:
                                    qty = int(qty_match.group(1))
                                    product = re.sub(r'\s*\(x\d+\)', '', product)

                            processed_items.append({
                                'sale_id': row['sale_id'],
                                'order_number': row['order_number'],
                                'sale_date': row.get('sale_date', ''),
                                'item_title': product,
                                'full_item_title': item_title,
                                'custom_label': row.get('custom_label', ''),
                                'sale_price': row['sale_price'],
                                'platform': platform,
                                'quantity': qty,
                                'is_split': True,
                                'split_index': i
                            })
                    else:
                        # Non-Stripe or single-item Stripe order
                        processed_items.append({
                            'sale_id': row['sale_id'],
                            'order_number': row['order_number'],
                            'sale_date': row.get('sale_date', ''),
                            'item_title': item_title,
                            'full_item_title': item_title,
                            'custom_label': row.get('custom_label', ''),
                            'sale_price': row['sale_price'],
                            'platform': row.get('platform', ''),
                            'quantity': row.get('quantity', 1),
                            'is_split': False,
                            'split_index': 0
                        })

                st.info(f"Showing {len(processed_items)} items to review ({len(unknown_df)} original records)")
                st.caption("Note: Stripe orders with multiple products are shown as separate line items for easier categorization.")

                st.divider()

                for item in processed_items:
                    col1, col2, col3, col4 = st.columns([3, 2, 1, 1])

                    with col1:
                        # Show platform badge
                        platform_badge = "🟢 eBay" if item['platform'] == 'eBay' else "🟣 Stripe" if item['platform'] == 'Stripe' else item['platform']
                        st.markdown(f"**{item['item_title']}**")

                        # Format sale date
                        sale_date_str = str(item['sale_date'])[:10] if item['sale_date'] else 'N/A'

                        if item['is_split']:
                            st.caption(f"{platform_badge} | {sale_date_str} | Order: {item['order_number'][:20]}... | Qty: {item['quantity']} | (Part of multi-item order)")
                        else:
                            st.caption(f"{platform_badge} | {sale_date_str} | Order: {item['order_number'][:20]}... | SKU: {item['custom_label']} | ${item['sale_price']:.2f}")

                    with col2:
                        # Use unique key for each item
                        key_suffix = f"{item['sale_id']}_{item['split_index']}" if item['is_split'] else str(item['sale_id'])
                        selected = st.selectbox(
                            "Assign to",
                            purchase_options,
                            key=f"assign_{key_suffix}",
                            label_visibility="collapsed"
                        )

                    with col3:
                        if st.button("Save", key=f"save_{key_suffix}"):
                            # Get the actual purchase_id from the map
                            actual_purchase_id = purchase_id_map.get(selected, 'UNKNOWN')

                            if item['is_split']:
                                # For split items, we need to update the whole sale but note this is partial
                                # In the future, you might want to actually split the sale record
                                st.warning("Multi-product orders need manual split in future version")

                            db.update_sale_purchase_id(item['sale_id'], actual_purchase_id)
                            st.success(f"Updated to {actual_purchase_id}")
                            st.rerun()

                    with col4:
                        # Delete button for cancelled/unpaid orders
                        if st.button("🗑️", key=f"delete_{key_suffix}", help="Delete sale (for cancelled/unpaid orders)"):
                            result = db.delete_sale(item['sale_id'])
                            if result['deleted']:
                                st.success(f"Deleted sale: {result['order_number']}")
                                st.rerun()
                            else:
                                st.error(result.get('error', 'Could not delete'))

                    st.divider()

        # ============================================================================
        # PAGE: SETTINGS
        # ============================================================================


    with sales_tab3:
        st.subheader('Orders')
        tab1, tab2 = st.tabs(['Pending Orders', 'Shipped Orders'])


        # ========================================================================
        # TAB 1: PENDING ORDERS
        # ========================================================================

        with tab1:
            st.subheader("Orders Awaiting Shipment")

            try:
                pending_orders = db.get_pending_orders()

                if pending_orders.empty:
                    st.info("No pending orders! All caught up.")
                else:
                    st.success(f" {len(pending_orders)} orders need shipping")

                    # Initialize session state for selections
                    if 'selected_order_ids' not in st.session_state:
                        st.session_state.selected_order_ids = []

                    # Action buttons row
                    col1, col2, col3, col4 = st.columns(4)

                    with col1:
                        if st.button("Select All"):
                            st.session_state.selected_order_ids = pending_orders['fulfillment_id'].tolist()
                            st.rerun()
                    with col2:
                        if st.button("Clear All"):
                            st.session_state.selected_order_ids = []
                            st.rerun()

                    # Show selection count
                    selected_count = len(st.session_state.selected_order_ids)
                    if selected_count > 0:
                        st.info(f" {selected_count} order(s) selected")

                    # Print and Export buttons
                    col3, col4 = st.columns(2)

                    with col3:
                        if st.button("Print Packing Slips", type="secondary"):
                            orders_to_print = st.session_state.selected_order_ids if st.session_state.selected_order_ids else pending_orders['fulfillment_id'].tolist()
                            orders_df = pending_orders[pending_orders['fulfillment_id'].isin(orders_to_print)]

                            if orders_df.empty:
                                st.warning("No orders to print")
                            else:
                                # Generate packing slip HTML
                                packing_slip_html = """<!DOCTYPE html>
    <html>
    <head>
        <title>Packing Slips - Taclaco TCG</title>
        <style>
            @page { size: 4in 6in; margin: 0; }
            body { margin: 0; padding: 0; font-family: Arial, sans-serif; }
            .packing-slip {
                width: 4in; height: 6in; padding: 0.25in;
                box-sizing: border-box; page-break-after: always;
                display: flex; flex-direction: column;
            }
            .packing-slip:last-child { page-break-after: auto; }
            .header { border-bottom: 2px solid #000; padding-bottom: 8px; margin-bottom: 12px; text-align: center; }
            .company-name { font-size: 18px; font-weight: bold; }
            .order-date { font-size: 10px; margin-top: 4px; }
            .section { margin-bottom: 12px; border-bottom: 1px solid #ccc; padding-bottom: 8px; }
            .section-title { font-size: 11px; font-weight: bold; margin-bottom: 6px; }
            .ship-to { font-size: 11px; line-height: 1.4; }
            .order-id { font-size: 9px; color: #666; margin-bottom: 8px; word-wrap: break-word; }
            .order-table { width: 100%; font-size: 10px; border-collapse: collapse; }
            .order-table th { text-align: left; padding: 4px 0; border-bottom: 1px solid #000; }
            .order-table td { padding: 6px 0; }
            .customer-notes { font-size: 10px; font-style: italic; line-height: 1.4; }
            .handwritten-space { flex-grow: 1; display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 60px; }
            .handwritten-line { width: 90%; border-bottom: 1px solid #ccc; margin: 8px 0; }
            .footer { font-size: 9px; text-align: center; line-height: 1.5; margin-top: auto; }
            .footer-title { font-weight: bold; margin-bottom: 4px; }
        </style>
    </head>
    <body>
    """

                                for _, order in orders_df.iterrows():
                                    order_date = order['order_date'] if order['order_date'] else ''
                                    customer_name = order['customer_name'] if order['customer_name'] else 'Customer'
                                    address_line1 = order['shipping_address_line1'] or ''
                                    address_line2 = order['shipping_address_line2'] or ''
                                    city_state_zip = f"{order['shipping_city'] or ''}, {order['shipping_state'] or ''} {order['shipping_zip'] or ''}"
                                    item_desc = order['item_description'] or 'TCG Product'
                                    amount = f"${order['order_total']:.2f}" if order['order_total'] else ''
                                    notes_text = order.get('notes', '') or ''

                                    notes_section = f'<div class="section"><div class="section-title">CUSTOMER NOTES:</div><div class="customer-notes">{notes_text}</div></div>' if notes_text else ''

                                    packing_slip_html += f"""
    <div class="packing-slip">
        <div class="header">
            <div class="company-name">TACLACO TCG</div>
            <div class="order-date">Date: {order_date}</div>
        </div>
        <div class="section">
            <div class="section-title">SHIP TO:</div>
            <div class="ship-to">
                {customer_name}<br>
                {address_line1}{', ' + address_line2 if address_line2 else ''}<br>
                {city_state_zip}
            </div>
        </div>
        <div class="section">
            <div class="section-title">ORDER DETAILS:</div>
            <div class="order-id">Order ID: {order['source_order_id']}</div>
            <table class="order-table">
                <thead><tr><th>Item</th><th style="text-align: center; width: 40px;">Qty</th><th style="text-align: right; width: 70px;">Price</th></tr></thead>
                <tbody><tr><td>{item_desc}</td><td style="text-align: center;">{order['quantity'] or 1}</td><td style="text-align: right;">{amount}</td></tr></tbody>
            </table>
        </div>
        {notes_section}
        <div class="handwritten-space"><div class="handwritten-line"></div></div>
        <div class="footer">
            <div class="footer-title">Questions or concerns?</div>
            <div>Email: travis@tacla.co</div>
            <div>Website: www.tacla.co</div>
        </div>
    </div>
    """

                                packing_slip_html += "</body></html>"

                                st.download_button(
                                    label=f" Download {len(orders_df)} Packing Slip(s)",
                                    data=packing_slip_html,
                                    file_name=f"packing_slips_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
                                    mime="text/html",
                                    key="download_packing_slips"
                                )
                                st.caption("Open the HTML file in browser and print (Ctrl+P)")

                    with col4:
                        if st.button("Export to CSV", type="primary"):
                            orders_to_export = st.session_state.selected_order_ids if st.session_state.selected_order_ids else pending_orders['fulfillment_id'].tolist()

                            if not orders_to_export:
                                st.warning("No orders to export")
                            else:
                                csv_data = db.generate_pirate_ship_csv(orders_to_export)
                                df_export = pd.DataFrame(csv_data)
                                csv_string = df_export.to_csv(index=False)

                                st.download_button(
                                    label=f" Download CSV ({len(orders_to_export)} orders)",
                                    data=csv_string,
                                    file_name=f"pirateship_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                                    mime="text/csv",
                                    key="download_pirateship_csv"
                                )

                    st.divider()

                    # Display orders with checkboxes
                    for idx, order in pending_orders.iterrows():
                        col1, col2, col3 = st.columns([0.5, 3, 1.5])

                        with col1:
                            is_selected = order['fulfillment_id'] in st.session_state.selected_order_ids
                            if st.checkbox("", key=f"chk_{order['fulfillment_id']}", value=is_selected, label_visibility="collapsed"):
                                if order['fulfillment_id'] not in st.session_state.selected_order_ids:
                                    st.session_state.selected_order_ids.append(order['fulfillment_id'])
                            else:
                                if order['fulfillment_id'] in st.session_state.selected_order_ids:
                                    st.session_state.selected_order_ids.remove(order['fulfillment_id'])

                        with col2:
                            name = order['customer_name'] or 'Unknown'
                            email = order['customer_email'] or ''
                            st.markdown(f"**{name}** - {email}")
                            # Show full product description (no truncation)
                            desc = order['item_description'] if order['item_description'] else 'TCG Product'
                            st.caption(f"{order['order_date']} |  ${order['order_total']:.2f} |  {desc}")
                            address = f"{order['shipping_address_line1'] or ''}, {order['shipping_city'] or ''}, {order['shipping_state'] or ''} {order['shipping_zip'] or ''}"
                            st.caption(f"{address}")
                            if order.get('notes'):
                                st.caption(f"{order['notes']}")

                        with col3:
                            carrier = st.selectbox("Carrier", ["USPS", "UPS", "FedEx", "DHL", "Other"], key=f"carrier_{order['fulfillment_id']}", label_visibility="collapsed")
                            tracking = st.text_input("Tracking", key=f"track_{order['fulfillment_id']}", label_visibility="collapsed", placeholder="Enter tracking...")
                            if tracking:
                                if st.button("Ship", key=f"ship_{order['fulfillment_id']}"):
                                    # Use Stripe sync if available, otherwise just local update
                                    if STRIPE_AVAILABLE and order.get('source') == 'Stripe':
                                        success, message = import_stripe.mark_order_shipped_with_stripe_sync(
                                            order['fulfillment_id'], tracking, carrier
                                        )
                                        if 'Stripe' in message:
                                            st.success(message)
                                        else:
                                            st.success("Shipped!")
                                    else:
                                        db.update_order_tracking(order['fulfillment_id'], tracking, carrier)
                                        st.success("Shipped!")
                                    st.rerun()

                        st.divider()

            except Exception as e:
                st.error(f"Error loading orders: {str(e)}")
                st.info("Make sure you've run the Phase 2 schema migration and have the updated database.py")

        # ========================================================================
        # TAB 2: SHIPPED ORDERS
        # ========================================================================
        with tab2:
            st.subheader("Shipped Orders")

            # Helper function to generate tracking URL
            def get_tracking_url(carrier, tracking_number):
                """Generate tracking URL based on carrier"""
                if not tracking_number:
                    return None
                carrier_upper = (carrier or '').upper()
                if carrier_upper == 'USPS':
                    return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}"
                elif carrier_upper == 'UPS':
                    return f"https://www.ups.com/track?tracknum={tracking_number}"
                elif carrier_upper == 'FEDEX':
                    return f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}"
                elif carrier_upper == 'DHL':
                    return f"https://www.dhl.com/us-en/home/tracking/tracking-express.html?submit=1&tracking-id={tracking_number}"
                else:
                    return None

            try:
                all_orders = db.get_all_orders()
                shipped = all_orders[all_orders['fulfillment_status'] == 'Shipped']

                if shipped.empty:
                    st.info("No shipped orders yet.")
                else:
                    st.success(f" {len(shipped)} orders shipped")

                    # Display shipped orders
                    for idx, order in shipped.iterrows():
                        with st.container():
                            col1, col2, col3 = st.columns([3, 2, 1])

                            with col1:
                                st.markdown(f"**{order['customer_name']}**")
                                desc = order['item_description'][:60] if order['item_description'] else 'N/A'
                                st.caption(f"{desc}...")

                            with col2:
                                tracking = order['tracking_number']
                                carrier = order.get('carrier', 'USPS')
                                tracking_url = get_tracking_url(carrier, tracking)

                                if tracking_url:
                                    st.markdown(f"[{tracking}]({tracking_url})")
                                else:
                                    st.markdown(f"{tracking}")
                                st.caption(f"{carrier or 'Unknown'} | Shipped: {order['ship_date']}")

                            with col3:
                                st.markdown(f"${order['order_total']:.2f}")

                            st.divider()

            except Exception as e:
                st.error(f"Error loading shipped orders: {str(e)}")

        # ========================================================================
        # TAB 3: PROCESS TO SALES
        # ========================================================================

# ==========================================================================
# PAGE: INVENTORY
# ==========================================================================

elif page == "Inventory":
    inv_tab1, inv_tab2, inv_tab3 = st.tabs(["🛍️ Purchases", "🔄 Trades", "🏅 Grading"])

    with inv_tab1:
            st.title("Purchases")

            # Refresh button
            col_title, col_refresh = st.columns([6, 1])
            with col_refresh:
                if st.button("", key="refresh_purchases", help="Refresh data"):
                    st.rerun()

            purchases_df = db.get_all_purchases()

            if purchases_df.empty:
                st.info("No purchases yet.")
            else:
                # Summary
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Total Purchases", len(purchases_df))
                with col2:
                    total_cost = purchases_df['total_cost'].sum() if purchases_df['total_cost'].notna().any() else 0
                    st.metric("Total Cost", f"${total_cost:,.2f}")
                with col3:
                    total_profit = purchases_df['total_profit'].sum() if 'total_profit' in purchases_df.columns else 0
                    st.metric("Total Profit", f"${total_profit:,.2f}")
                with col4:
                    open_count = len(purchases_df[purchases_df['status'] == 'Open']) if 'status' in purchases_df.columns else len(purchases_df)
                    st.metric("Open Purchases", open_count)

                st.divider()

                # Filter by status
                status_filter = st.radio("Filter by Status", ["All", "Open", "Closed"], horizontal=True)

                filtered_purchases = purchases_df.copy()
                if status_filter != "All":
                    filtered_purchases = filtered_purchases[filtered_purchases['status'] == status_filter]

                # Display purchases with inline editing
                st.subheader("Purchase List")

                for _, purchase in filtered_purchases.iterrows():
                    with st.container():
                        col1, col2, col3, col4 = st.columns([2, 1.5, 1, 0.5])

                        with col1:
                            display_name = purchase['display_name'] or purchase['purchase_id']
                            st.markdown(f"**{purchase['purchase_id']}** - {display_name}")
                            st.caption(f"{purchase['date']} |  Cost: ${purchase['total_cost']:.2f}" if purchase['total_cost'] else f" {purchase['date']}")

                        with col2:
                            items_sold = purchase.get('items_sold', 0) or 0
                            total_profit = purchase.get('total_profit', 0) or 0
                            st.caption(f"{int(items_sold)} items sold")
                            st.caption(f"Profit: ${total_profit:.2f}")

                        with col3:
                            current_status = purchase['status'] if purchase['status'] else 'Open'
                            new_status = st.selectbox(
                                "Status",
                                ["Open", "Closed"],
                                index=0 if current_status == 'Open' else 1,
                                key=f"status_{purchase['purchase_id']}",
                                label_visibility="collapsed"
                            )

                            if new_status != current_status:
                                # Update status in database
                                conn = db.get_connection()
                                cursor = conn.cursor()
                                cursor.execute("UPDATE purchases SET status = ? WHERE purchase_id = ?", (new_status, purchase['purchase_id']))
                                conn.commit()
                                conn.close()
                                st.rerun()

                        with col4:
                            status_icon = "" if current_status == "Open" else ""
                            st.markdown(f"<div style='font-size: 24px; text-align: center;'>{status_icon}</div>", unsafe_allow_html=True)

                        st.divider()

            # Add new purchase
            st.subheader("Add New Purchase")

            with st.form("add_purchase"):
                col1, col2 = st.columns(2)

                with col1:
                    # Auto-generate purchase ID
                    today = datetime.now()
                    suggested_id = db.get_next_purchase_id(today.strftime('%y%m'))

                    purchase_id = st.text_input("Purchase ID", value=suggested_id)
                    purchase_date = st.date_input("Date", value=today)
                    display_name = st.text_input("Display Name")

                with col2:
                    location = st.text_input("Location/Vendor")
                    total_cost = st.number_input("Total Cost", min_value=0.0, step=0.01)

                    # GL Account dropdown
                    try:
                        cogs_accounts = db.get_cogs_accounts()
                        gl_options = [''] + cogs_accounts['account_name'].tolist()
                        gl_account = st.selectbox("GL Account (COGS)", gl_options)
                    except:
                        gl_account = st.text_input("GL Account")

                if st.form_submit_button("Add Purchase", type="primary"):
                    db.add_purchase(
                        purchase_id=purchase_id,
                        date=purchase_date.strftime('%Y-%m-%d'),
                        description=display_name,
                        location=location,
                        order_number=None,
                        total_cost=total_cost,
                        display_name=display_name,
                        gl_account=gl_account if gl_account else None
                    )
                    st.success(f" Added purchase {purchase_id}")
                    st.rerun()

            # Consolidate Purchases section
            st.divider()
            st.subheader("Consolidate Purchases")
            st.caption("Merge multiple purchase IDs into one. Useful when cards from the same transaction were given separate IDs.")

            with st.expander("Consolidate Purchases", expanded=False):
                # Get all purchases for selection
                all_purchases = db.get_all_purchases()

                if all_purchases.empty:
                    st.info("No purchases to consolidate.")
                else:
                    # Build options list
                    purchase_options = {}
                    for _, p in all_purchases.iterrows():
                        cost_str = f"${p['total_cost']:.2f}" if p['total_cost'] else "$0"
                        label = f"{p['purchase_id']} - {p['display_name'] or 'No name'} ({cost_str})"
                        purchase_options[label] = p['purchase_id']

                    st.markdown("**Step 1: Select purchases to consolidate**")
                    selected_labels = st.multiselect(
                        "Select 2 or more purchases",
                        options=list(purchase_options.keys()),
                        help="Select all purchase IDs that should be merged into one"
                    )

                    if len(selected_labels) >= 2:
                        selected_ids = [purchase_options[label] for label in selected_labels]

                        # Get preview
                        preview = db.get_consolidation_preview(selected_ids)

                        st.markdown("**Step 2: Select master purchase**")
                        st.caption("This purchase will remain. Others will be merged into it and deleted.")

                        master_label = st.selectbox(
                            "Master Purchase (keep this one)",
                            options=selected_labels,
                            help="All other selected purchases will be merged into this one"
                        )
                        master_id = purchase_options[master_label]

                        # Show preview
                        st.markdown("**Preview:**")

                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.metric("Purchases to Merge", preview['purchase_count'])
                        with col2:
                            st.metric("Combined Cost", f"${preview['combined_cost']:.2f}")
                        with col3:
                            st.metric("Sales to Update", preview['total_sales'])

                        # Show details
                        st.markdown("**Details:**")
                        for p in preview['purchases']:
                            sales_count = preview['sales_by_purchase'].get(p['purchase_id'], 0)
                            is_master = " (MASTER - will keep)" if p['purchase_id'] == master_id else " (will merge & delete)"
                            cost = p['total_cost'] or 0
                            st.caption(f"• {p['purchase_id']}: ${cost:.2f}, {sales_count} sales{is_master}")

                        st.warning(f"This will delete {preview['purchase_count'] - 1} purchase record(s) and update {preview['total_sales']} sale(s) to point to {master_id}.")

                        # Confirm and execute
                        confirm = st.checkbox(f"I understand this cannot be undone", key="confirm_consolidate")

                        if confirm:
                            if st.button("Consolidate Purchases", type="primary"):
                                ids_to_merge = [pid for pid in selected_ids if pid != master_id]
                                result = db.consolidate_purchases(master_id, ids_to_merge)

                                if result['success']:
                                    st.success(f"✓ {result['message']}")
                                    st.info(f"New total cost for {master_id}: ${result['new_total_cost']:.2f}")
                                    st.info(f"Sales updated: {result['sales_updated']}")
                                    st.rerun()
                                else:
                                    st.error(f"Error: {result['message']}")
                    elif len(selected_labels) == 1:
                        st.info("Select at least 2 purchases to consolidate.")

        # ============================================================================
        # PAGE: TRADES
        # ============================================================================


    with inv_tab2:
            st.title("Trades")

            # Refresh button
            col_title, col_refresh = st.columns([6, 1])
            with col_refresh:
                if st.button("🔄", key="refresh_trades", help="Refresh data"):
                    st.rerun()

            st.caption("Record inventory trades with other collectors. Trade value counts as revenue on the GIVE side and cost basis on the RECEIVE side.")

            # Get all trades
            trades_df = db.get_all_trades()

            # Summary metrics
            if not trades_df.empty:
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Total Trades", len(trades_df))
                with col2:
                    total_give = trades_df['give_value'].sum()
                    st.metric("Total Given", f"${total_give:,.2f}")
                with col3:
                    total_receive = trades_df['receive_value'].sum()
                    st.metric("Total Received", f"${total_receive:,.2f}")
                with col4:
                    total_shipping = trades_df['shipping_cost'].sum()
                    st.metric("Shipping Costs", f"${total_shipping:,.2f}")

                st.divider()

            # Tabs for viewing vs creating
            tab_list, tab_new = st.tabs(["📋 Trade History", "➕ New Trade"])

            # ========================================================================
            # TAB: TRADE HISTORY
            # ========================================================================
            with tab_list:
                if trades_df.empty:
                    st.info("No trades recorded yet. Use the 'New Trade' tab to record your first trade.")
                else:
                    for _, trade in trades_df.iterrows():
                        with st.container():
                            col1, col2, col3, col4 = st.columns([2.5, 2, 2, 1])

                            with col1:
                                st.markdown(f"**Trade #{trade['trade_id']}** - {trade['trade_date']}")
                                if trade['notes']:
                                    st.caption(trade['notes'][:50] + ('...' if len(str(trade['notes'])) > 50 else ''))

                            with col2:
                                st.markdown(f"**GIVE:** ${trade['give_value']:,.2f}")
                                st.caption(f"{int(trade['give_lines'])} line(s)")

                            with col3:
                                st.markdown(f"**RECEIVE:** ${trade['receive_value']:,.2f}")
                                st.caption(f"{int(trade['receive_lines'])} line(s)")

                            with col4:
                                if trade['shipping_cost'] and trade['shipping_cost'] > 0:
                                    st.caption(f"Ship: ${trade['shipping_cost']:.2f}")

                                # View details button
                                if st.button("View", key=f"view_trade_{trade['trade_id']}"):
                                    st.session_state['view_trade_id'] = trade['trade_id']

                            st.divider()

                    # Show trade details if selected
                    if 'view_trade_id' in st.session_state:
                        trade_id = st.session_state['view_trade_id']
                        details = db.get_trade_details(trade_id)

                        if details:
                            st.subheader(f"Trade #{trade_id} Details")

                            trade_info = details['trade']
                            lines_df = details['lines']

                            # Trade header info
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.markdown(f"**Date:** {trade_info['trade_date']}")
                            with col2:
                                st.markdown(f"**Tracking:** {trade_info['tracking_number'] or 'None'}")
                            with col3:
                                st.markdown(f"**Shipping:** ${trade_info['shipping_cost'] or 0:.2f}")

                            if trade_info['notes']:
                                st.markdown(f"**Notes:** {trade_info['notes']}")

                            # GIVE lines
                            give_lines = lines_df[lines_df['direction'] == 'GIVE']
                            if not give_lines.empty:
                                st.markdown("**GIVE (Traded Away):**")
                                for _, line in give_lines.iterrows():
                                    if line['line_type'] == 'inventory':
                                        purchase_display = line['purchase_name'] or line['purchase_id']
                                        if line['graded_card_id']:
                                            st.markdown(f"- ${line['value']:,.2f} - {line['graded_card_name']} [{line['grade']}] (Cert: {line['cert_number']}) from {purchase_display}")
                                        else:
                                            st.markdown(f"- ${line['value']:,.2f} from {purchase_display}")
                                    else:
                                        st.markdown(f"- ${line['value']:,.2f} Cash ({line['payment_source']})")

                            # RECEIVE lines
                            receive_lines = lines_df[lines_df['direction'] == 'RECEIVE']
                            if not receive_lines.empty:
                                st.markdown("**RECEIVE (Acquired):**")
                                for _, line in receive_lines.iterrows():
                                    if line['line_type'] == 'inventory':
                                        purchase_display = line['purchase_name'] or line['purchase_id']
                                        st.markdown(f"- ${line['value']:,.2f} into {purchase_display}")
                                    else:
                                        st.markdown(f"- ${line['value']:,.2f} Cash ({line['payment_source']})")

                            # Delete button
                            st.divider()
                            col1, col2 = st.columns([4, 1])
                            with col2:
                                if st.button("🗑️ Delete Trade", key=f"delete_trade_{trade_id}", type="secondary"):
                                    st.session_state['confirm_delete_trade'] = trade_id

                            if st.session_state.get('confirm_delete_trade') == trade_id:
                                st.warning("⚠️ This will delete the trade, remove associated sales, reverse purchase cost changes, and reset any graded card statuses.")
                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("✅ Yes, Delete", key="confirm_delete_yes", type="primary"):
                                        result = db.delete_trade(trade_id)
                                        if result['success']:
                                            st.success(f"Trade deleted. Sales removed: {result['sales_deleted']}, Purchases reversed: {result['purchases_reversed']}")
                                            del st.session_state['view_trade_id']
                                            del st.session_state['confirm_delete_trade']
                                            st.rerun()
                                        else:
                                            st.error(result['message'])
                                with col2:
                                    if st.button("❌ Cancel", key="confirm_delete_no"):
                                        del st.session_state['confirm_delete_trade']
                                        st.rerun()

                            # Close details button
                            if st.button("Close Details"):
                                del st.session_state['view_trade_id']
                                if 'confirm_delete_trade' in st.session_state:
                                    del st.session_state['confirm_delete_trade']
                                st.rerun()

            # ========================================================================
            # TAB: NEW TRADE
            # ========================================================================
            with tab_new:
                st.subheader("Record New Trade")

                # Initialize session state for trade lines
                if 'trade_give_lines' not in st.session_state:
                    st.session_state['trade_give_lines'] = []
                if 'trade_receive_lines' not in st.session_state:
                    st.session_state['trade_receive_lines'] = []

                # Trade header
                col1, col2 = st.columns(2)
                with col1:
                    trade_date = st.date_input("Trade Date", value=datetime.now(), key="new_trade_date")
                with col2:
                    tracking_number = st.text_input("Tracking Number (Pirate Ship)", key="new_trade_tracking",
                                                   help="Enter the tracking number for auto-matching when you import Pirate Ship data")

                trade_notes = st.text_area("Notes", key="new_trade_notes", height=68,
                                          help="Optional notes about this trade (e.g., who you traded with)")

                st.divider()

                # Get purchases for dropdowns
                all_purchases = db.get_all_purchases()
                purchase_options = {"-- Select Purchase --": None}
                for _, p in all_purchases.iterrows():
                    label = f"{p['purchase_id']} - {p['display_name'] or 'No name'}"
                    purchase_options[label] = p['purchase_id']

                # Get graded cards for dropdown
                graded_cards_df = db.get_inventory_graded_cards()
                graded_card_options = {"-- No Graded Card --": None}
                for _, gc in graded_cards_df.iterrows():
                    label = f"{gc['cert_number']} - {gc['card_name']} [{gc['grade']}] ({gc['purchase_id']})"
                    graded_card_options[label] = gc['card_id']

                # ====================================================================
                # GIVE SECTION
                # ====================================================================
                st.markdown("### 📤 GIVE (What You're Trading Away)")

                # Display existing GIVE lines
                for i, line in enumerate(st.session_state['trade_give_lines']):
                    col1, col2, col3, col4 = st.columns([2, 1.5, 1.5, 0.5])
                    with col1:
                        if line['type'] == 'inventory':
                            st.markdown(f"**{line['purchase_id']}**")
                            if line.get('graded_card_name'):
                                st.caption(f"Graded: {line['graded_card_name']}")
                        else:
                            st.markdown(f"**Cash** ({line.get('payment_source', 'N/A')})")
                    with col2:
                        st.markdown(f"${line['value']:,.2f}")
                    with col3:
                        st.caption(line['type'])
                    with col4:
                        if st.button("❌", key=f"remove_give_{i}"):
                            st.session_state['trade_give_lines'].pop(i)
                            st.rerun()

                # Add new GIVE line
                with st.expander("➕ Add GIVE Line", expanded=len(st.session_state['trade_give_lines']) == 0):
                    give_type = st.radio("Type", ["Inventory", "Cash"], key="give_line_type", horizontal=True)

                    if give_type == "Inventory":
                        col1, col2 = st.columns(2)
                        with col1:
                            give_purchase_label = st.selectbox("Purchase", list(purchase_options.keys()), key="give_purchase")
                            give_purchase_id = purchase_options[give_purchase_label]
                        with col2:
                            give_value = st.number_input("Trade Value ($)", min_value=0.0, step=1.0, key="give_value")

                        # Graded card selector (filtered by selected purchase)
                        give_card_id = None
                        give_card_name = None
                        if give_purchase_id:
                            filtered_cards = graded_cards_df[graded_cards_df['purchase_id'] == give_purchase_id]
                            card_options = {"-- No Graded Card --": None}
                            for _, gc in filtered_cards.iterrows():
                                label = f"{gc['cert_number']} - {gc['card_name']} [{gc['grade']}]"
                                card_options[label] = gc['card_id']

                            give_card_label = st.selectbox("Link Graded Card (Optional)", list(card_options.keys()), key="give_graded_card")
                            give_card_id = card_options[give_card_label]

                            # Get card name if selected
                            if give_card_id:
                                card_row = graded_cards_df[graded_cards_df['card_id'] == give_card_id].iloc[0]
                                give_card_name = f"{card_row['card_name']} [{card_row['grade']}]"

                        if st.button("Add GIVE Inventory Line", type="primary", key="add_give_inv"):
                            if give_purchase_id and give_value > 0:
                                st.session_state['trade_give_lines'].append({
                                    'type': 'inventory',
                                    'purchase_id': give_purchase_id,
                                    'value': give_value,
                                    'graded_card_id': give_card_id,
                                    'graded_card_name': give_card_name
                                })
                                st.rerun()
                            else:
                                st.error("Please select a purchase and enter a value")

                    else:  # Cash
                        col1, col2 = st.columns(2)
                        with col1:
                            give_cash_source = st.selectbox("Payment Source", ["PayPal", "Venmo", "Other"], key="give_cash_source")
                        with col2:
                            give_cash_value = st.number_input("Amount ($)", min_value=0.0, step=1.0, key="give_cash_value")

                        if st.button("Add GIVE Cash Line", type="primary", key="add_give_cash"):
                            if give_cash_value > 0:
                                st.session_state['trade_give_lines'].append({
                                    'type': 'cash',
                                    'payment_source': give_cash_source,
                                    'value': give_cash_value
                                })
                                st.rerun()
                            else:
                                st.error("Please enter an amount")

                # GIVE total
                give_total = sum(line['value'] for line in st.session_state['trade_give_lines'])
                st.markdown(f"**GIVE Total: ${give_total:,.2f}**")

                st.divider()

                # ====================================================================
                # RECEIVE SECTION
                # ====================================================================
                st.markdown("### 📥 RECEIVE (What You're Getting)")

                # Display existing RECEIVE lines
                for i, line in enumerate(st.session_state['trade_receive_lines']):
                    col1, col2, col3, col4 = st.columns([2, 1.5, 1.5, 0.5])
                    with col1:
                        if line['type'] == 'inventory':
                            st.markdown(f"**{line['purchase_id']}**")
                        else:
                            st.markdown(f"**Cash** ({line.get('payment_source', 'N/A')})")
                    with col2:
                        st.markdown(f"${line['value']:,.2f}")
                    with col3:
                        st.caption(line['type'])
                    with col4:
                        if st.button("❌", key=f"remove_receive_{i}"):
                            st.session_state['trade_receive_lines'].pop(i)
                            st.rerun()

                # Add new RECEIVE line
                with st.expander("➕ Add RECEIVE Line", expanded=len(st.session_state['trade_receive_lines']) == 0):
                    receive_type = st.radio("Type", ["Inventory", "Cash"], key="receive_line_type", horizontal=True)

                    if receive_type == "Inventory":
                        col1, col2 = st.columns(2)
                        with col1:
                            receive_purchase_label = st.selectbox("Purchase (cost basis added here)", list(purchase_options.keys()), key="receive_purchase")
                            receive_purchase_id = purchase_options[receive_purchase_label]
                        with col2:
                            receive_value = st.number_input("Trade Value ($)", min_value=0.0, step=1.0, key="receive_value")

                        if st.button("Add RECEIVE Inventory Line", type="primary", key="add_receive_inv"):
                            if receive_purchase_id and receive_value > 0:
                                st.session_state['trade_receive_lines'].append({
                                    'type': 'inventory',
                                    'purchase_id': receive_purchase_id,
                                    'value': receive_value
                                })
                                st.rerun()
                            else:
                                st.error("Please select a purchase and enter a value")

                    else:  # Cash
                        col1, col2 = st.columns(2)
                        with col1:
                            receive_cash_source = st.selectbox("Payment Source", ["PayPal", "Venmo", "Other"], key="receive_cash_source")
                        with col2:
                            receive_cash_value = st.number_input("Amount ($)", min_value=0.0, step=1.0, key="receive_cash_value")

                        if st.button("Add RECEIVE Cash Line", type="primary", key="add_receive_cash"):
                            if receive_cash_value > 0:
                                st.session_state['trade_receive_lines'].append({
                                    'type': 'cash',
                                    'payment_source': receive_cash_source,
                                    'value': receive_cash_value
                                })
                                st.rerun()
                            else:
                                st.error("Please enter an amount")

                # RECEIVE total
                receive_total = sum(line['value'] for line in st.session_state['trade_receive_lines'])
                st.markdown(f"**RECEIVE Total: ${receive_total:,.2f}**")

                st.divider()

                # ====================================================================
                # TRADE SUMMARY & SUBMIT
                # ====================================================================
                st.markdown("### 📊 Trade Summary")

                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("GIVE Total", f"${give_total:,.2f}")
                with col2:
                    st.metric("RECEIVE Total", f"${receive_total:,.2f}")
                with col3:
                    diff = receive_total - give_total
                    st.metric("Difference", f"${diff:,.2f}", delta=f"${diff:,.2f}" if diff != 0 else None)

                # Validation
                can_submit = True
                warnings = []

                if len(st.session_state['trade_give_lines']) == 0:
                    can_submit = False
                    warnings.append("Add at least one GIVE line")

                if len(st.session_state['trade_receive_lines']) == 0:
                    can_submit = False
                    warnings.append("Add at least one RECEIVE line")

                if abs(give_total - receive_total) > 0.01:
                    # This is just a warning, not blocking
                    st.warning(f"⚠️ Trade values don't balance (difference: ${abs(diff):,.2f}). This is OK if cash is involved.")

                if warnings:
                    for w in warnings:
                        st.error(w)

                # Submit button
                col1, col2 = st.columns([3, 1])
                with col2:
                    submit_disabled = not can_submit
                    if st.button("✅ Record Trade", type="primary", disabled=submit_disabled, key="submit_trade"):
                        try:
                            # Create trade
                            trade_id = db.create_trade(
                                trade_date=trade_date.strftime('%Y-%m-%d'),
                                tracking_number=tracking_number if tracking_number else None,
                                notes=trade_notes if trade_notes else None
                            )

                            # Add GIVE lines
                            for line in st.session_state['trade_give_lines']:
                                db.add_trade_line(
                                    trade_id=trade_id,
                                    direction='GIVE',
                                    line_type=line['type'],
                                    value=line['value'],
                                    purchase_id=line.get('purchase_id'),
                                    graded_card_id=line.get('graded_card_id'),
                                    payment_source=line.get('payment_source')
                                )

                            # Add RECEIVE lines
                            for line in st.session_state['trade_receive_lines']:
                                db.add_trade_line(
                                    trade_id=trade_id,
                                    direction='RECEIVE',
                                    line_type=line['type'],
                                    value=line['value'],
                                    purchase_id=line.get('purchase_id'),
                                    payment_source=line.get('payment_source')
                                )

                            # Process the trade (create sales, update purchase costs)
                            result = db.process_trade(trade_id)

                            if result['success']:
                                st.success(f"✅ Trade #{trade_id} recorded successfully!")
                                st.info(f"Sales created: {result['sales_created']} | Purchases updated: {result['purchases_updated']} | Graded cards linked: {result['graded_cards_linked']}")

                                # Clear session state
                                st.session_state['trade_give_lines'] = []
                                st.session_state['trade_receive_lines'] = []
                                st.rerun()
                            else:
                                st.error(f"Error processing trade: {result['message']}")

                        except Exception as e:
                            st.error(f"Error creating trade: {str(e)}")

                with col1:
                    if st.button("🗑️ Clear Form", key="clear_trade_form"):
                        st.session_state['trade_give_lines'] = []
                        st.session_state['trade_receive_lines'] = []
                        st.rerun()

        # ============================================================================
        # PAGE: GRADING BATCHES
        # ============================================================================



    with inv_tab3:
            st.title("Grading Batches")

            # Refresh button
            col_title, col_refresh = st.columns([6, 1])
            with col_refresh:
                if st.button("", key="refresh_grading", help="Refresh data"):
                    st.rerun()

            # Workflow explanation
            with st.expander(" How Grading Batches Work", expanded=False):
                st.markdown("""
                **Grading Batch Workflow:**

                1. **Record the Cost** - When you pay for grading (via Chase), categorize the transaction to "Grading Fees" GL account
                2. **Create a Batch** - Create a new batch here and link it to that Chase transaction
                3. **Send Cards** - Update status to "Sent" when you ship cards to the grader
                4. **Import Results** - When cards return, import the TAG grading CSV in the "Import TAG CSV" tab here
                5. **Auto-Allocation** - The system divides total cost by # of cards to get cost-per-card
                6. **Sell Cards** - When graded cards sell on eBay, the cert# in the title links to this batch, and grading cost is added to that sale's expenses

                **Status Flow:** Pending -> Sent -> Received -> Complete
                """)

            tab1, tab2, tab3 = st.tabs(["View Batches", "Create New Batch", "Assign Cards"])

            with tab1:
                batches_df = db.get_all_grading_batches()
                if batches_df.empty:
                    st.info("No grading batches yet. Create your first batch in the 'Create New Batch' tab.")
                else:
                    # Summary metrics
                    col1, col2, col3, col4 = st.columns(4)

                    with col1:
                        st.metric("Total Batches", len(batches_df))
                    with col2:
                        total_cards = batches_df['total_cards'].sum() if 'total_cards' in batches_df.columns else 0
                        st.metric("Total Cards", f"{int(total_cards):,}")
                    with col3:
                        cards_sold = batches_df['cards_sold'].sum() if 'cards_sold' in batches_df.columns else 0
                        st.metric("Cards Sold", f"{int(cards_sold):,}")
                    with col4:
                        total_profit = batches_df['total_profit'].sum() if 'total_profit' in batches_df.columns else 0
                        st.metric("Total Profit", f"${total_profit:,.2f}")

                    st.divider()

                    # Display batches
                    for _, batch in batches_df.iterrows():
                        with st.expander(f" {batch['batch_name']} - {batch['status']}", expanded=False):
                            col1, col2 = st.columns(2)

                            with col1:
                                st.markdown(f"**Grader:** {batch['grader']}")
                                st.markdown(f"**Submitted:** {batch['submission_date']}")
                                st.markdown(f"**Grading Fee:** ${batch['grading_fee']:.2f}")
                                st.markdown(f"**Shipping:** ${batch['shipping_cost']:.2f}")
                                st.markdown(f"**Total Cost:** ${batch['total_cost']:.2f}")

                            with col2:
                                st.markdown(f"**Cards:** {int(batch.get('total_cards', 0))}")
                                st.markdown(f"**Sold:** {int(batch.get('cards_sold', 0))}")
                                st.markdown(f"**Cost/Card:** ${batch['cost_per_card']:.2f}")
                                if batch.get('total_profit'):
                                    st.markdown(f"**Profit:** ${batch['total_profit']:.2f}")

                            # Status update
                            st.divider()
                            col1, col2, col3 = st.columns([2, 1, 1])

                            with col1:
                                new_status = st.selectbox(
                                    "Update Status",
                                    ["Pending", "Sent", "Received", "Complete"],
                                    index=["Pending", "Sent", "Received", "Complete"].index(batch['status']) if batch['status'] in ["Pending", "Sent", "Received", "Complete"] else 0,
                                    key=f"batch_status_{batch['batch_id']}"
                                )

                            with col2:
                                if new_status != batch['status']:
                                    if st.button("Update Status", key=f"update_status_{batch['batch_id']}"):
                                        db.update_grading_batch_status(batch['batch_id'], new_status)
                                        st.success(f"Updated to {new_status}")
                                        st.rerun()

                            with col3:
                                if st.button("Recalc Costs", key=f"recalc_{batch['batch_id']}"):
                                    db.update_grading_batch_costs(batch['batch_id'])
                                    st.success("Costs recalculated!")
                                    st.rerun()

                            # Link Shipping Cost section (only show if shipping is $0)
                            if batch['shipping_cost'] == 0:
                                st.divider()
                                st.markdown("**Link Shipping Cost**")

                                # Get unlinked TAG shipments from shipping_costs table
                                try:
                                    conn = db.get_connection()
                                    grader_name = batch['grader']
                                    # Look for shipments to TAG/PSA/CGC etc that aren't linked to a batch
                                    unlinked_shipments = db._read_sql(conn, """
                                        SELECT sc.shipping_id, sc.tracking_number, sc.ship_date, sc.cost, sc.carrier
                                        FROM shipping_costs sc
                                        WHERE sc.recipient LIKE '%TAG%' OR sc.recipient LIKE '%PSA%' 
                                           OR sc.recipient LIKE '%CGC%' OR sc.recipient LIKE '%BGS%'
                                           OR sc.recipient LIKE '%Grading%'
                                        ORDER BY sc.ship_date DESC
                                        LIMIT 20
                                    """)
                                    conn.close()

                                    if not unlinked_shipments.empty:
                                        ship_col1, ship_col2 = st.columns([3, 1])

                                        with ship_col1:
                                            shipment_options = {0: '-- Select Shipment --'}
                                            for _, ship in unlinked_shipments.iterrows():
                                                shipment_options[ship['shipping_id']] = f"{ship['ship_date']} - ${ship['cost']:.2f} - {ship['carrier']} ({ship['tracking_number'][:15]}...)"

                                            selected_shipment = st.selectbox(
                                                "Select TAG Shipment",
                                                options=list(shipment_options.keys()),
                                                format_func=lambda x: shipment_options[x],
                                                key=f"ship_select_{batch['batch_id']}"
                                            )

                                        with ship_col2:
                                            st.write("")  # Spacing
                                            if selected_shipment and selected_shipment != 0:
                                                if st.button("Link", key=f"link_ship_{batch['batch_id']}"):
                                                    # Get the shipping cost
                                                    ship_cost = unlinked_shipments[unlinked_shipments['shipping_id'] == selected_shipment]['cost'].values[0]
                                                    # Update batch shipping cost
                                                    conn = db.get_connection()
                                                    cursor = conn.cursor()
                                                    cursor.execute("""
                                                        UPDATE grading_batches 
                                                        SET shipping_cost = ?, total_cost = grading_fee + ?
                                                        WHERE batch_id = ?
                                                    """, (ship_cost, ship_cost, batch['batch_id']))
                                                    conn.commit()
                                                    conn.close()
                                                    db.update_grading_batch_costs(batch['batch_id'])
                                                    st.success(f"Linked ${ship_cost:.2f} shipping!")
                                                    st.rerun()

                                        # Prepaid label option
                                        if st.button("Prepaid Label (No Cost)", key=f"prepaid_{batch['batch_id']}", help="Use when grading company sent you a prepaid label"):
                                            conn = db.get_connection()
                                            cursor = conn.cursor()
                                            cursor.execute("""
                                                UPDATE grading_batches 
                                                SET shipping_cost = 0, notes = COALESCE(notes, '') || ' [Prepaid Label]'
                                                WHERE batch_id = ?
                                            """, (batch['batch_id'],))
                                            conn.commit()
                                            conn.close()
                                            st.success("Marked as prepaid label!")
                                            st.rerun()
                                    else:
                                        st.caption("No unlinked grading shipments found in Pirate Ship imports")
                                        if st.button("Prepaid Label (No Cost)", key=f"prepaid_{batch['batch_id']}", help="Use when grading company sent you a prepaid label"):
                                            conn = db.get_connection()
                                            cursor = conn.cursor()
                                            cursor.execute("""
                                                UPDATE grading_batches 
                                                SET shipping_cost = 0, notes = COALESCE(notes, '') || ' [Prepaid Label]'
                                                WHERE batch_id = ?
                                            """, (batch['batch_id'],))
                                            conn.commit()
                                            conn.close()
                                            st.success("Marked as prepaid label!")
                                            st.rerun()
                                except Exception as e:
                                    st.caption(f"Could not load shipments: {e}")

                            # Delete batch (with confirmation)
                            st.divider()
                            with st.expander("Danger Zone", expanded=False):
                                st.warning("Deleting a batch will also delete all cards in the batch.")
                                cards_count = int(batch.get('total_cards', 0))
                                if cards_count > 0:
                                    st.error(f"This batch has {cards_count} cards that will be deleted!")

                                confirm_delete = st.checkbox(f"I understand, delete batch '{batch['batch_name']}'", key=f"confirm_del_{batch['batch_id']}")
                                if confirm_delete:
                                    if st.button("Delete Batch", key=f"delete_{batch['batch_id']}", type="secondary"):
                                        db.delete_grading_batch(batch['batch_id'])
                                        st.success(f"Deleted batch: {batch['batch_name']}")
                                        st.rerun()

                            # View cards in this batch
                            st.divider()
                            st.markdown("**Cards in this Batch:**")

                            cards_df = db.get_graded_cards_by_batch(batch['batch_id'])

                            if cards_df.empty:
                                st.info("No cards imported yet. Use the Import TAG CSV tab to import cards.")
                            else:
                                # Display cards table
                                display_cols = ['cert_number', 'card_name', 'grade', 'status', 'allocated_cost']
                                cards_display = cards_df[display_cols].copy()
                                cards_display['allocated_cost'] = cards_display['allocated_cost'].apply(lambda x: f"${x:.2f}" if x else "$0.00")
                                cards_display.columns = ['Cert #', 'Card Name', 'Grade', 'Status', 'Allocated Cost']

                                st.dataframe(cards_display, use_container_width=True, hide_index=True)
            with tab2:
                st.subheader("Create New Grading Batch")

                # Get transactions categorized as Grading Fees (excluding already-linked ones)
                st.markdown("**Link to Transaction (optional)**")
                st.caption("Select a transaction categorized to Grading Fees to auto-populate costs")

                grading_transactions = []
                try:
                    conn = db.get_connection()
                    # Exclude transactions already linked to a batch
                    grading_df = db._read_sql(conn, """
                        SELECT 
                            t.transaction_id,
                            t.transaction_date,
                            t.merchant_name,
                            t.description,
                            t.amount,
                            t.category
                        FROM transactions t
                        WHERE t.status = 'Categorized'
                          AND (t.category = 'Grading Fees' 
                               OR t.category LIKE '%Grading%'
                               OR t.merchant_name LIKE '%TAG%'
                               OR t.description LIKE '%TAG%')
                          AND NOT EXISTS (
                              SELECT 1 FROM grading_batches gb 
                              WHERE gb.chase_transaction_id = t.transaction_id
                          )
                        ORDER BY t.transaction_date DESC
                    """)
                    conn.close()

                    if not grading_df.empty:
                        grading_transactions = [{'id': None, 'display': '-- None --', 'amount': 0}]
                        for _, tx in grading_df.iterrows():
                            grading_transactions.append({
                                'id': tx['transaction_id'],
                                'display': f"{tx['transaction_date']} - {tx['merchant_name'] or tx['description'][:30]} - ${abs(tx['amount']):.2f}",
                                'amount': abs(tx['amount'])
                            })
                except Exception as e:
                    st.caption(f"Could not load transactions: {e}")

                with st.form("create_batch"):
                    col1, col2 = st.columns(2)

                    with col1:
                        batch_name = st.text_input("Batch Name *", placeholder="e.g., TAG Nov 2025")
                        grader = st.selectbox("Grader", ["TAG Grading", "PSA", "CGC", "BGS", "Other"])
                        submission_date = st.date_input("Submission Date", value=datetime.now())

                        # Transaction dropdown
                        if grading_transactions:
                            selected_tx_idx = st.selectbox(
                                "Link to Transaction",
                                range(len(grading_transactions)),
                                format_func=lambda i: grading_transactions[i]['display']
                            )
                            selected_tx = grading_transactions[selected_tx_idx]
                        else:
                            selected_tx = None
                            st.caption("No unlinked grading transactions found")

                    with col2:
                        # Pre-populate from Chase transaction if selected
                        default_fee = selected_tx['amount'] if selected_tx and selected_tx['id'] else 0.0

                        grading_fee = st.number_input("Grading Fee ($)", min_value=0.0, step=0.01, value=default_fee)
                        shipping_cost = st.number_input("Shipping Cost ($)", min_value=0.0, step=0.01)
                        status = st.selectbox("Status", ["Pending", "Sent", "Received", "Complete"])

                    notes = st.text_area("Notes (optional)")

                    if st.form_submit_button("Create Batch", type="primary"):
                        if not batch_name:
                            st.error("Batch name is required")
                        elif db.check_grading_batch_name_exists(batch_name):
                            st.error(f"A batch named '{batch_name}' already exists. Please use a different name.")
                        else:
                            chase_tx_id = selected_tx['id'] if selected_tx and selected_tx['id'] else None

                            batch_id = db.add_grading_batch(
                                batch_name=batch_name,
                                grader=grader,
                                submission_date=submission_date.strftime('%Y-%m-%d'),
                                chase_transaction_id=chase_tx_id,
                                grading_fee=grading_fee,
                                shipping_cost=0,  # Will be linked via tracking
                                notes=notes
                            )

                            # Update status if not Pending
                            if status != "Pending":
                                db.update_grading_batch_status(batch_id, status)

                            st.success(f"Created batch: {batch_name}")
                            st.rerun()

        # ============================================================================
        # PAGE: REVIEW UNKNOWN
        # ============================================================================

            with tab3:
                st.subheader("Assign Cards")
                st.caption("Assign Purchase IDs to graded cards and link cards to sales")

                # Get all batches for filter
                batches_df = db.get_all_grading_batches()

                if batches_df.empty:
                    st.info("No grading batches yet. Create one first.")
                else:
                    # Filters
                    col1, col2 = st.columns(2)

                    with col1:
                        batch_filter_options = {'all': 'All Batches'}
                        for _, b in batches_df.iterrows():
                            batch_filter_options[b['batch_id']] = b['batch_name']

                        selected_batch_filter = st.selectbox(
                            "Filter by Batch",
                            options=list(batch_filter_options.keys()),
                            format_func=lambda x: batch_filter_options[x],
                            key="assign_batch_filter"
                        )

                    with col2:
                        status_filter = st.selectbox(
                            "Filter by Status",
                            ["All", "Needs Purchase ID", "Inventory (Not Sold)", "Sold"],
                            key="assign_status_filter"
                        )

                    # Get graded cards based on filter
                    conn = db.get_connection()

                    query = """
                        SELECT gc.card_id, gc.cert_number, gc.card_name, gc.card_number, gc.card_set, gc.grade, gc.status,
                               gc.purchase_id, gc.allocated_cost, gc.sale_id,
                               gb.batch_name, gb.grader
                        FROM graded_cards gc
                        JOIN grading_batches gb ON gc.batch_id = gb.batch_id
                        WHERE 1=1
                    """
                    params = []

                    if selected_batch_filter != 'all':
                        query += " AND gc.batch_id = ?"
                        params.append(selected_batch_filter)

                    if status_filter == "Needs Purchase ID":
                        query += " AND (gc.purchase_id IS NULL OR gc.purchase_id = '')"
                    elif status_filter == "Inventory (Not Sold)":
                        query += " AND gc.status = 'Inventory'"
                    elif status_filter == "Sold":
                        query += " AND gc.status = 'Sold'"

                    query += " ORDER BY gb.batch_name, gc.card_name LIMIT 30"

                    cards_df = db._read_sql(conn, query,  params=params)
                    conn.close()

                    if cards_df.empty:
                        st.info("No cards match the current filters.")
                    else:
                        st.markdown(f"**Showing {len(cards_df)} cards**")

                        # Get purchases for dropdown
                        purchases_df = db.get_all_purchases()
                        purchase_options = {'': '-- No Purchase ID --'}
                        for _, p in purchases_df.iterrows():
                            display = p['display_name'] if pd.notna(p['display_name']) else p['description'][:30]
                            purchase_options[p['purchase_id']] = f"{p['purchase_id']} - {display}"

                        # Get sales for linking (only show graded card sales by grading company)
                        conn = db.get_connection()
                        graded_sales_df = db._read_sql(conn, """
                            SELECT sale_id, sale_date, item_title, sale_price
                            FROM sales
                            WHERE (grading_fee IS NULL OR grading_fee = 0)
                              AND (item_title LIKE '%TAG%' OR item_title LIKE '%PSA%' 
                                   OR item_title LIKE '%CGC%' OR item_title LIKE '%BGS%'
                                   OR item_title LIKE '%Beckett%')
                            ORDER BY sale_date DESC
                            LIMIT 100
                        """)
                        conn.close()

                        sale_options = {0: '-- Link to Sale --'}
                        for _, s in graded_sales_df.iterrows():
                            title_short = s['item_title'][:40] + '...' if len(s['item_title']) > 40 else s['item_title']
                            sale_options[s['sale_id']] = f"{s['sale_date']} - ${s['sale_price']:.2f} - {title_short}"

                        st.divider()

                        # Display each card with assignment options
                        for idx, card in cards_df.iterrows():
                            status_icon = "Sold" if card['status'] == 'Sold' else "Inventory"

                            with st.container():
                                # Card info row
                                col1, col2, col3 = st.columns([3, 2, 2])

                                with col1:
                                    # Build card number display like "25/102"
                                    card_num = card['card_number'] if pd.notna(card['card_number']) and card['card_number'] else ""
                                    card_set = card['card_set'] if pd.notna(card['card_set']) and card['card_set'] else ""
                                    if card_num and card_set:
                                        card_num_display = f" {card_num}/{card_set}"
                                    elif card_num:
                                        card_num_display = f" #{card_num}"
                                    else:
                                        card_num_display = ""
                                    st.markdown(f"**{card['card_name']}{card_num_display}** - Grade: {card['grade']}")
                                    st.caption(f"Cert: {card['cert_number']} | Batch: {card['batch_name']} | {status_icon}")
                                    st.caption(f"Current PID: {card['purchase_id'] or 'None'} | Cost: ${card['allocated_cost']:.2f}")

                                with col2:
                                    # Purchase ID dropdown
                                    current_pid = card['purchase_id'] if pd.notna(card['purchase_id']) else ''
                                    new_pid = st.selectbox(
                                        "Purchase ID",
                                        options=list(purchase_options.keys()),
                                        format_func=lambda x: purchase_options[x],
                                        index=list(purchase_options.keys()).index(current_pid) if current_pid in purchase_options else 0,
                                        key=f"pid_{card['card_id']}",
                                        label_visibility="collapsed"
                                    )

                                    if new_pid != current_pid:
                                        if st.button("Save PID", key=f"save_pid_{card['card_id']}", type="secondary"):
                                            db.update_card_purchase_id(card['card_id'], new_pid if new_pid else None)
                                            st.success("Updated!")
                                            st.rerun()

                                with col3:
                                    # Sale linking (only for inventory cards)
                                    if card['status'] == 'Inventory':
                                        selected_sale = st.selectbox(
                                            "Link to Sale",
                                            options=list(sale_options.keys()),
                                            format_func=lambda x: sale_options[x],
                                            key=f"sale_{card['card_id']}",
                                            label_visibility="collapsed"
                                        )

                                        if selected_sale and selected_sale != 0:
                                            if st.button("Link", key=f"link_{card['card_id']}", type="primary"):
                                                success = db.link_graded_card_to_sale(card['cert_number'], selected_sale)
                                                if success:
                                                    st.success("Linked!")
                                                    st.rerun()
                                                else:
                                                    st.error("Failed to link")
                                    else:
                                        st.caption("Already sold")

                                st.divider()

                        st.caption(f"Found {len(graded_sales_df)} unlinked graded card sales (TAG/PSA/CGC/BGS in title)")



# ==========================================================================
# PAGE: SETTINGS
# ==========================================================================

elif page == "Settings":
    st.title("Settings")
    s1, s2, s3, s4 = st.tabs(["Shipping Supplies", "Product Mappings", "Database Info", "Data Management"])

    with s1:
        st.subheader("Shipping Supplies Estimate")
        st.caption("Set per-shipment supply costs based on order value tiers. This is for dashboard profit reporting only and does not affect accounting exports.")

        st.markdown("**Cost Tiers (based on total order value in shipment)**")

        # Get current settings
        tier1_cost = float(db.get_setting('supplies_tier1_cost') or '0.25')  # Under $20
        tier2_cost = float(db.get_setting('supplies_tier2_cost') or '0.75')  # $20.01 - $75
        tier3_cost = float(db.get_setting('supplies_tier3_cost') or '1.50')  # Over $75

        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Tier 1: Under $20**")
            st.caption("Standard envelope + top loader")
            new_tier1 = st.number_input(
                "Cost ($)",
                min_value=0.0,
                max_value=10.0,
                value=tier1_cost,
                step=0.05,
                key="tier1_cost"
            )

        with col2:
            st.markdown("**Tier 2: $20.01 - $75**")
            st.caption("Bubble mailer")
            new_tier2 = st.number_input(
                "Cost ($)",
                min_value=0.0,
                max_value=10.0,
                value=tier2_cost,
                step=0.05,
                key="tier2_cost"
            )

        with col3:
            st.markdown("**Tier 3: Over $75**")
            st.caption("Cardboard box + padding")
            new_tier3 = st.number_input(
                "Cost ($)",
                min_value=0.0,
                max_value=10.0,
                value=tier3_cost,
                step=0.05,
                key="tier3_cost"
            )

        if st.button("Save Supplies Costs"):
            db.save_setting('supplies_tier1_cost', str(new_tier1))
            db.save_setting('supplies_tier2_cost', str(new_tier2))
            db.save_setting('supplies_tier3_cost', str(new_tier3))
            st.success(f" Saved! Tier 1: ${new_tier1:.2f} | Tier 2: ${new_tier2:.2f} | Tier 3: ${new_tier3:.2f}")
            st.rerun()

        st.divider()
        st.markdown("**How it works:**")
        st.markdown("""
        - The supplies cost is determined by the **total value of orders in a shipment**
        - For eBay sales, each order is treated as a separate shipment
        - For direct sales (Stripe), if a customer places multiple orders shipped together, 
          the combined value determines the tier
        - This estimate is subtracted from profit in the dashboard but **not** sent to Wave accounting
        """)
    with s2:
        st.subheader("Product Mappings")
        st.caption("Map product identifiers to Purchase IDs for automatic categorization")

        # Create subtabs for different mapping types
        map_tab1, map_tab2 = st.tabs(["Stripe Products", "LIFE TCG Title Patterns"])

        with map_tab1:
            st.markdown("**Stripe Product → Purchase ID**")
            st.caption("Map Stripe product IDs to your Purchase IDs. Used when processing Stripe orders to sales.")

            # Get all Stripe product mappings
            stripe_mappings = db.get_all_stripe_product_mappings()

            # Get purchases for dropdown - show meaningful info
            purchases_df = db.get_all_purchases()
            purchase_options = ['UNKNOWN']
            purchase_display_map = {'UNKNOWN': 'UNKNOWN'}

            for _, p in purchases_df.iterrows():
                # Build a descriptive label: Display Name or Description, Date, Total
                display_name = p.get('display_name') or p.get('description') or f"Purchase #{p['purchase_id']}"
                purchase_date = p.get('date', '')
                total_cost = p.get('total_cost', 0) or 0

                # Format: "Display Name - $XX.XX (2024-01-15) [P#123]"
                option_label = f"{display_name}"
                if total_cost:
                    option_label += f" - ${total_cost:.2f}"
                if purchase_date:
                    option_label += f" ({purchase_date})"
                option_label += f" [P#{p['purchase_id']}]"

                purchase_options.append(option_label)
                purchase_display_map[option_label] = p['purchase_id']

            # Show helpful info about available purchases
            if len(purchases_df) > 0:
                with st.expander(f"📦 View All Purchases ({len(purchases_df)} available)"):
                    display_cols = ['purchase_id', 'display_name', 'description', 'date', 'total_cost', 'status']
                    available_cols = [c for c in display_cols if c in purchases_df.columns]
                    st.dataframe(purchases_df[available_cols], use_container_width=True, hide_index=True)

            # Discover products button
            col_discover, col_info = st.columns([2, 3])
            with col_discover:
                if st.button("🔍 Discover Stripe Products", key="discover_stripe_products"):
                    stats = db.discover_stripe_products_from_line_items()
                    if stats.get('error'):
                        st.error(f"Error: {stats['error']}")
                    else:
                        st.success(f"Found {stats['products_found']} products, added {stats['products_added']} new mappings")
                    st.rerun()

            with col_info:
                st.caption("Click to scan imported Stripe orders for products to map")

            st.divider()

            # Show existing mappings with edit capability
            if not stripe_mappings.empty:
                st.markdown(f"**Current Mappings ({len(stripe_mappings)}):**")

                for _, mapping in stripe_mappings.iterrows():
                    col1, col2, col3 = st.columns([3, 3, 1])

                    with col1:
                        st.markdown(f"**{mapping['product_name'] or 'Unknown Product'}**")
                        st.caption(f"`{mapping['stripe_product_id']}`")

                    with col2:
                        # Find current selection
                        current_purchase = mapping['purchase_id'] or 'UNKNOWN'

                        # Find the matching option label for current purchase
                        current_option = 'UNKNOWN'
                        for opt_label, opt_id in purchase_display_map.items():
                            if opt_id == current_purchase:
                                current_option = opt_label
                                break

                        # Find index of current option
                        try:
                            current_idx = purchase_options.index(current_option)
                        except ValueError:
                            current_idx = 0

                        new_selection = st.selectbox(
                            "Assign to Purchase",
                            purchase_options,
                            index=current_idx,
                            key=f"stripe_map_{mapping['stripe_product_id']}",
                            label_visibility="collapsed"
                        )

                    with col3:
                        if st.button("Save", key=f"save_stripe_{mapping['stripe_product_id']}"):
                            new_purchase_id = purchase_display_map.get(new_selection, 'UNKNOWN')
                            db.add_stripe_product_mapping(
                                mapping['stripe_product_id'],
                                new_purchase_id,
                                mapping['product_name']
                            )
                            st.success("Saved!")
                            st.rerun()

                st.divider()
            else:
                st.info("No Stripe products found yet. Click **Discover Stripe Products** above after importing Stripe orders.")

            # Manual add form
            with st.expander("• Add Manual Mapping"):
                with st.form("add_stripe_mapping"):
                    col1, col2 = st.columns(2)

                    with col1:
                        new_product_id = st.text_input("Stripe Product ID (prod_xxx)")
                        new_product_name = st.text_input("Product Name (optional)")

                    with col2:
                        new_purchase_selection = st.selectbox("Assign to Purchase", purchase_options, key="new_stripe_purchase")

                    if st.form_submit_button("Add Mapping"):
                        if new_product_id:
                            new_purchase_id = purchase_display_map.get(new_purchase_selection, 'UNKNOWN')
                            db.add_stripe_product_mapping(new_product_id, new_purchase_id, new_product_name)
                            st.success("Added mapping!")
                            st.rerun()
                        else:
                            st.error("Product ID is required")

        with map_tab2:
            st.markdown("**LIFE TCG Title Pattern → Purchase ID**")
            st.caption("Map eBay item title patterns to Purchase IDs. If an item title contains the pattern, it will be assigned to that Purchase ID.")

            mappings_df = db.get_all_life_tcg_mappings()

            if not mappings_df.empty:
                st.dataframe(mappings_df, use_container_width=True)

                # Delete mapping section
                st.markdown("**Delete Mapping**")
                col1, col2 = st.columns([3, 1])
                with col1:
                    mapping_options = [f"ID {row['id']}: '{row['title_contains']}' → {row['purchase_id']}" 
                                      for _, row in mappings_df.iterrows()]
                    selected_mapping = st.selectbox("Select mapping to delete", mapping_options, key="delete_mapping_select")
                with col2:
                    if st.button("🗑️ Delete", key="delete_mapping_btn"):
                        # Extract ID from selection
                        mapping_id = int(selected_mapping.split(":")[0].replace("ID ", ""))
                        db.delete_life_tcg_mapping(mapping_id)
                        st.success(f"Deleted mapping ID {mapping_id}")
                        st.rerun()
            else:
                st.info("No mappings yet.")

            # Add new mapping
            with st.form("add_life_mapping"):
                col1, col2, col3 = st.columns(3)

                with col1:
                    title_contains = st.text_input("Title Contains")
                with col2:
                    purchase_id = st.text_input("Purchase ID")
                with col3:
                    notes = st.text_input("Notes (optional)")

                if st.form_submit_button("Add Mapping"):
                    if title_contains and purchase_id:
                        success = db.add_life_tcg_mapping(title_contains, purchase_id, notes)
                        if success:
                            st.success("Added mapping")
                            st.rerun()
                        else:
                            st.error("Mapping already exists")
                    else:
                        st.error("Title pattern and Purchase ID are required")
    with s3:
        st.subheader("Database Information")

        col1, col2, col3 = st.columns(3)

        with col1:
            st.metric("Total Purchases", len(db.get_all_purchases()))
            st.metric("Total Sales", len(db.get_all_sales()))

        with col2:
            st.metric("Unknown Sales", len(db.get_unknown_sales()))
            st.metric("LIFE TCG Mappings", len(mappings_df) if not mappings_df.empty else 0)

        with col3:
            try:
                orders = db.get_all_orders()
                shipping = db.get_all_shipping_costs()
                st.metric("Stripe Orders", len(orders))
                st.metric("Shipping Records", len(shipping))
            except:
                st.metric("Stripe Orders", "N/A")
                st.metric("Shipping Records", "N/A")

        st.divider()

        # Data source status
        st.subheader("Data Source Status")
        try:
            status_df = db.get_all_data_source_status()
            st.dataframe(status_df, use_container_width=True, hide_index=True)

            if st.button("Recalculate Status"):
                db.recalculate_data_source_status()
                st.success("Recalculated!")
                st.rerun()
        except Exception as e:
            st.info(f"Data source status not available: {e}")
    with s4:
        st.subheader("Data Management")


        # Database Backup Section
        st.markdown("**Database Backup**")
        st.caption("Create a timestamped backup of your entire database.")

        col_backup1, col_backup2 = st.columns([2, 3])

        with col_backup1:
            if st.button("Create Backup Now", type="secondary", key="create_backup_btn"):
                backup_path = db.create_backup()
                if backup_path:
                    st.success("Backup created!")
                    st.caption(f"Location: {backup_path}")
                else:
                    st.error("Backup failed. Check console for details.")

        with col_backup2:
            backups = db.list_backups()
            if backups:
                st.caption(f"**{len(backups)} existing backup(s):**")
                for filename, size_kb, modified in backups[:5]:
                    st.caption(f"- {filename} ({size_kb:.1f} KB) - {modified.strftime('%Y-%m-%d %H:%M')}")
                if len(backups) > 5:
                    st.caption(f"... and {len(backups) - 5} more")
            else:
                st.caption("No backups found. Create your first backup!")

        st.divider()

        # Run Migrations
        st.markdown("**Database Maintenance**")
        st.caption("Run migrations to add new columns (safe to run multiple times).")

        if st.button("Run Database Migrations", key="run_migrations_btn"):
            try:
                db.migrate_database()
                st.success("Migrations complete!")
            except Exception as e:
                st.error(f"Migration error: {e}")

        st.divider()

        st.markdown("**Export Data**")
        st.caption("Export data for backup or to preserve before resetting the database.")

        col1, col2 = st.columns(2)

        with col1:
            # Export Purchases
            purchases_df = db.get_all_purchases()
            if not purchases_df.empty:
                csv_purchases = purchases_df.to_csv(index=False)
                st.download_button(
                    label=" Export Purchases (CSV)",
                    data=csv_purchases,
                    file_name=f"taclaco_purchases_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )
                st.caption(f"{len(purchases_df)} purchases")
            else:
                st.info("No purchases to export")

        with col2:
            # Export LIFE TCG Mappings
            mappings_df = db.get_all_life_tcg_mappings()
            if not mappings_df.empty:
                csv_mappings = mappings_df.to_csv(index=False)
                st.download_button(
                    label=" Export LIFE TCG Mappings (CSV)",
                    data=csv_mappings,
                    file_name=f"taclaco_mappings_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )
                st.caption(f"{len(mappings_df)} mappings")
            else:
                st.info("No mappings to export")

        st.divider()
        st.markdown("**Import Data**")
        st.caption("Re-import purchases or mappings from a previously exported CSV.")

        col1, col2 = st.columns(2)

        with col1:
            uploaded_purchases = st.file_uploader("Import Purchases CSV", type=['csv'], key='import_purchases')
            if uploaded_purchases:
                import_df = pd.read_csv(uploaded_purchases)
                st.caption(f"Found {len(import_df)} purchases in file")

                if st.button("Import Purchases"):
                    imported = 0
                    skipped = 0
                    for _, row in import_df.iterrows():
                        try:
                            db.add_purchase(
                                purchase_id=row['purchase_id'],
                                date=row.get('date', ''),
                                description=row.get('description', ''),
                                location=row.get('location', ''),
                                order_number=row.get('order_number', ''),
                                total_cost=row.get('total_cost', 0) if pd.notna(row.get('total_cost')) else 0,
                                display_name=row.get('display_name', ''),
                                gl_account=row.get('gl_account', '')
                            )
                            imported += 1
                        except:
                            skipped += 1
                    st.success(f" Imported {imported} purchases, skipped {skipped} duplicates")

        with col2:
            uploaded_mappings = st.file_uploader("Import Mappings CSV", type=['csv'], key='import_mappings')
            if uploaded_mappings:
                import_df = pd.read_csv(uploaded_mappings)
                st.caption(f"Found {len(import_df)} mappings in file")

                if st.button("Import Mappings"):
                    imported = 0
                    for _, row in import_df.iterrows():
                        success = db.add_life_tcg_mapping(
                            row['title_contains'],
                            row['purchase_id'],
                            row.get('notes', '')
                        )
                        if success:
                            imported += 1
                    st.success(f" Imported {imported} mappings")

        st.divider()
        st.markdown("**Reset Database**")
        st.caption("Delete all data and start fresh. Make sure to export your purchases first!")

        with st.expander(" Danger Zone", expanded=False):
            st.warning("This will delete ALL data except your Stripe API key setting. This cannot be undone!")

            confirm = st.text_input("Type 'RESET' to confirm")
            if st.button("Reset Database", type="primary"):
                if confirm == "RESET":
                    # Preserve ALL critical settings
                    api_key = db.get_setting('stripe_api_key')
                    supplies_tier1 = db.get_setting('supplies_tier1_cost')
                    supplies_tier2 = db.get_setting('supplies_tier2_cost')
                    supplies_tier3 = db.get_setting('supplies_tier3_cost')
                    ebay_app_id = db.get_setting('ebay_app_id')
                    ebay_dev_id = db.get_setting('ebay_dev_id')
                    ebay_cert_id = db.get_setting('ebay_cert_id')
                    ebay_user_token = db.get_setting('ebay_user_token')
                    ebay_environment = db.get_setting('ebay_environment')

                    # Delete and reinitialize
                    import os
                    if os.path.exists(db.DB_PATH):
                        os.remove(db.DB_PATH)
                    db.init_database()

                    # Restore all settings
                    if api_key:
                        db.save_setting('stripe_api_key', api_key)
                    if supplies_tier1:
                        db.save_setting('supplies_tier1_cost', supplies_tier1)
                    if supplies_tier2:
                        db.save_setting('supplies_tier2_cost', supplies_tier2)
                    if supplies_tier3:
                        db.save_setting('supplies_tier3_cost', supplies_tier3)
                    if ebay_app_id:
                        db.save_setting('ebay_app_id', ebay_app_id)
                    if ebay_dev_id:
                        db.save_setting('ebay_dev_id', ebay_dev_id)
                    if ebay_cert_id:
                        db.save_setting('ebay_cert_id', ebay_cert_id)
                    if ebay_user_token:
                        db.save_setting('ebay_user_token', ebay_user_token)
                    if ebay_environment:
                        db.save_setting('ebay_environment', ebay_environment)

                    st.success("Database reset! Please re-import your data.")
                    st.rerun()
                else:
                    st.error("Please type 'RESET' to confirm")

