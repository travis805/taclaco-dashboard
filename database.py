"""
Database setup and query functions for Taclaco Dashboard
Includes: purchases, sales, supplies, eBay transactions, Chase transactions, grading, and Wave export
Phase 2: Added COA, data source status, order fulfillment, shipping costs
Turso: dual-mode connection layer (libSQL remote or local SQLite fallback).
"""

import sqlite3
import pandas as pd
from datetime import datetime
import os

DB_PATH = "taclaco.db"


def _get_turso_creds():
    """
    Return (url, token) from Streamlit secrets or env vars, or (None, None).
    Wraps st.secrets in a try/except so non-Streamlit scripts (import_*.py) never crash.
    """
    url = None
    token = None

    # Try Streamlit secrets first (only available when running inside Streamlit).
    try:
        import streamlit as st
        url = st.secrets.get("TURSO_DATABASE_URL")
        token = st.secrets.get("TURSO_AUTH_TOKEN")
    except Exception:
        pass

    # Fall back to environment variables (used by import_*.py scripts and CLI tools).
    if not url:
        url = os.environ.get("TURSO_DATABASE_URL")
    if not token:
        token = os.environ.get("TURSO_AUTH_TOKEN")

    if url and token:
        return url, token
    return None, None


def _open_direct(autocommit=False):
    """Direct connection: libSQL over HTTP when Turso creds are set, else local
    SQLite.

    The import path opens this with autocommit=True so every write commits
    immediately. That is what prevents the libSQL single-writer deadlock: several
    importers open a second connection from a helper (e.g. get_purchase_id_from_sku)
    while a first connection is mid-write; with a held remote write transaction the
    nested read blocks forever on the write lock, but autocommitting each statement
    releases the lock right away so the nested read proceeds. Writes persist
    straight to the primary. The dashboard uses the default (non-autocommit) mode;
    it holds a single cached connection per session, so it never nests."""
    url, token = _get_turso_creds()
    if url and token:
        import libsql
        if autocommit:
            return _LibsqlConn(
                libsql.connect(database=url, auth_token=token, autocommit=True)
            )
        return _LibsqlConn(libsql.connect(database=url, auth_token=token))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _open_connection():
    """
    Open a raw (uncached) connection for code running OUTSIDE Streamlit (the
    import_*.py scripts). Uses a direct Turso connection in AUTOCOMMIT mode when
    Turso creds are present, otherwise local SQLite. Autocommit is essential so
    the importers' nested connections do not deadlock on the remote write lock
    (see _open_direct).

    Both backends support name-indexed rows (row["col"]): the local sqlite
    connection sets row_factory=sqlite3.Row; the libSQL connection is wrapped by
    _LibsqlConn (see the compatibility layer below).
    """
    return _open_direct(autocommit=True)


def get_connection():
    """
    Return a database connection. Inside a Streamlit session the cached DIRECT
    connection is returned (opened once, reused across reruns). From import_*.py
    scripts outside Streamlit, a fresh direct autocommit connection is opened.
    """
    try:
        import streamlit as st
        # If we are inside an active Streamlit script run, use the cached path.
        ctx = st.runtime.scriptrunner.get_script_run_ctx()
        if ctx is not None:
            return get_cached_connection()
    except Exception:
        pass
    return _open_connection()


def get_cached_connection():
    """
    Return a cached DIRECT connection for the Streamlit app.
    Uses @st.cache_resource so the connection is opened once per session.
    Falls back to a plain direct connection if Streamlit is not available.
    """
    try:
        import streamlit as st

        @st.cache_resource
        def _cached():
            return _open_direct()

        return _cached()
    except Exception:
        return _open_direct()


# ---------------------------------------------------------------------------
# Row compatibility helpers.
# libSQL returns plain tuples; sqlite3 with row_factory returns sqlite3.Row.
# Use _row_to_dict() instead of dict(row) or row["col"] anywhere cursor rows
# are accessed by column name.
# ---------------------------------------------------------------------------

def _row_to_dict(row, cursor):
    """Convert a single cursor row (sqlite3.Row or libSQL tuple) to a plain dict."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if isinstance(row, sqlite3.Row):
        return dict(row)
    # libSQL returns a plain tuple of column values: rebuild from cursor.description.
    if cursor is not None and cursor.description:
        return dict(zip([d[0] for d in cursor.description], row))
    return {}


def _read_sql(conn, query, params=None):
    """
    Execute a SELECT and return a pandas DataFrame.
    Works with both libSQL connections and standard sqlite3 connections.
    Replaces pd.read_sql_query() throughout the codebase.
    """
    cur = conn.execute(query, params or ())
    cols = [d[0] for d in cur.description] if cur.description else []
    return pd.DataFrame(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------------------
# libSQL compatibility layer.
# The import_*.py modules and parts of the dashboard access cursor rows by
# column name (row["col"]) and bind datetime.date/datetime objects as SQL
# parameters. sqlite3 supports both via row_factory and default adapters; the
# libSQL (Turso) driver returns plain tuples and rejects date objects. The
# local sqlite connection uses row_factory=sqlite3.Row; the libSQL connection
# is wrapped here so its cursors return name-indexable rows and its execute()
# adapts unsupported parameter types. No import_*.py query code needs changing.
# ---------------------------------------------------------------------------

class _Row(tuple):
    """Tuple subclass that also supports row["column"] and row.get("column").

    Intentionally does NOT define keys(): a dict-like row would make
    pd.DataFrame(rows, columns=...) take its dict path and break _read_sql.
    """

    def __new__(cls, values, colmap):
        self = super().__new__(cls, values)
        self._colmap = colmap          # {column_name: index}, shared per cursor
        return self

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._colmap[key])
        return tuple.__getitem__(self, key)

    def get(self, key, default=None):
        idx = self._colmap.get(key) if isinstance(key, str) else key
        if idx is None:
            return default
        try:
            return tuple.__getitem__(self, idx)
        except (IndexError, TypeError):
            return default


def _adapt_param(v):
    """Coerce one bind parameter to a type the libSQL driver accepts.

    libSQL's Hrana protocol serializes params to JSON, so NaN/Infinity floats
    (common from pandas: empty numeric cells become NaN) are rejected with
    "invalid type: null, expected f64". Map all NaN/NaT/NA-like values to SQL
    NULL, and convert dates/Decimals/numpy scalars the driver cannot bind."""
    import math
    import datetime
    from decimal import Decimal
    if v is None:
        return None
    # NaN / NaT / pandas NA -> SQL NULL (guard pd.isna to scalars only).
    try:
        import pandas as pd
        if pd.api.types.is_scalar(v) and pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (str, bytes, int)):
        return v
    if isinstance(v, datetime.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    item = getattr(v, "item", None)    # numpy scalar -> python scalar
    if callable(item):
        try:
            return _adapt_param(item())
        except Exception:
            pass
    return v


def _adapt_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return {k: _adapt_param(v) for k, v in params.items()}
    return tuple(_adapt_param(v) for v in params)


class _LibsqlCursor:
    """Wraps a libSQL cursor so fetched rows are _Row objects (name-indexable)."""

    def __init__(self, conn):
        self._conn = conn      # underlying libsql connection
        self._cur = None

    def _colmap(self):
        desc = self._cur.description if self._cur is not None else None
        if not desc:
            return {}
        return {d[0]: i for i, d in enumerate(desc)}

    def execute(self, sql, params=None):
        if params is None:
            self._cur = self._conn.execute(sql)
        else:
            self._cur = self._conn.execute(sql, _adapt_params(params))
        return self

    def fetchone(self):
        r = self._cur.fetchone()
        return _Row(r, self._colmap()) if r is not None else None

    def fetchall(self):
        cm = self._colmap()
        return [_Row(r, cm) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        cm = self._colmap()
        return [_Row(r, cm) for r in rows]

    def __iter__(self):
        cm = self._colmap()
        for r in self._cur:
            yield _Row(r, cm)

    @property
    def description(self):
        return self._cur.description if self._cur is not None else None

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", -1)

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _LibsqlConn:
    """Wraps a libSQL connection to mirror the sqlite3 connection API used here."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        return _LibsqlCursor(self._conn).execute(sql, params)

    def cursor(self):
        return _LibsqlCursor(self._conn)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        rb = getattr(self._conn, "rollback", None)
        return rb() if callable(rb) else None

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        # Delegate anything not defined here (e.g. row_factory assignment) to libsql.
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Cache decorator helper. When running inside Streamlit, wraps functions with
# @st.cache_data(ttl=300) so repeated reruns do not re-query. When running
# outside Streamlit (import scripts), this is a no-op passthrough.
# ---------------------------------------------------------------------------
try:
    import streamlit as _st_cache
    def _cache(fn):
        return _st_cache.cache_data(ttl=300)(fn)
except Exception:
    def _cache(fn):
        return fn



def init_database():
    """Initialize database with all required tables"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. PURCHASES - Inventory purchases at collection/batch level
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            purchase_id TEXT PRIMARY KEY,
            date DATE,
            description TEXT,
            location TEXT,
            order_number TEXT,
            total_cost REAL,
            status TEXT DEFAULT 'Open',
            display_name TEXT,
            notes TEXT,
            gl_account TEXT
        )
    """)
    
    # 2. SALES - Individual sale transactions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id TEXT,
            platform TEXT,
            order_number TEXT,
            transaction_id TEXT,
            item_title TEXT,
            custom_label TEXT,
            sale_date DATE,
            quantity INTEGER DEFAULT 1,
            sale_price REAL,
            shipping_charged REAL DEFAULT 0,
            shipping_cost REAL DEFAULT 0,
            platform_fees_fixed REAL DEFAULT 0,
            platform_fees_variable REAL DEFAULT 0,
            regulatory_fee REAL DEFAULT 0,
            promoted_listing_fee REAL DEFAULT 0,
            international_fee REAL DEFAULT 0,
            supplies_estimate REAL DEFAULT 0,
            grading_fee REAL DEFAULT 0,
            net_profit REAL,
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id)
        )
    """)
    
    # 3. SUPPLIES - Supply items and costs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS supplies (
            supply_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            cost_per_unit REAL,
            last_updated DATE
        )
    """)
    
    # 4. SUPPLY_TIERS - Price range to supply usage mapping
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS supply_tiers (
            tier_id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_price REAL,
            max_price REAL,
            supplies_used TEXT
        )
    """)
    
    # 5. EBAY_TRANSACTIONS - Raw eBay import staging table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ebay_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            order_number TEXT,
            transaction_id TEXT,
            payment_date DATE,
            payout_date DATE,
            net_amount REAL,
            item_title TEXT,
            custom_label TEXT,
            quantity INTEGER,
            item_subtotal REAL,
            shipping_handling REAL,
            seller_collected_tax REAL,
            ebay_collected_tax REAL,
            electronic_waste_fee REAL,
            final_value_fee_fixed REAL,
            final_value_fee_variable REAL,
            international_fee REAL,
            gross_transaction_amount REAL,
            net_transaction_amount REAL,
            total_fee_amount REAL,
            regulatory_operating_fee REAL,
            description TEXT,
            import_batch_id INTEGER,
            import_date TIMESTAMP,
            processed BOOLEAN DEFAULT 0
        )
    """)
    
    # 6. JOURNAL_ENTRIES - Wave Accounting export
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT,
            account TEXT,
            debit REAL DEFAULT 0,
            credit REAL DEFAULT 0,
            description TEXT
        )
    """)
    
    # 7. LIFE_TCG_MAPPINGS - Title to Purchase ID mappings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS life_tcg_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_contains TEXT UNIQUE,
            purchase_id TEXT,
            notes TEXT,
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id)
        )
    """)
    
    # 8. SETTINGS - Key-value configuration store
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 9. CHASE_TRANSACTIONS - Bank transaction imports (LEGACY - being phased out)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chase_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_last_four TEXT,
            transaction_date DATE,
            post_date DATE,
            description TEXT,
            clean_merchant_name TEXT,
            chase_category TEXT,
            transaction_type TEXT,
            amount REAL,
            memo TEXT,
            purchase_id TEXT,
            expense_category TEXT,
            import_batch_id INTEGER,
            import_date TIMESTAMP,
            status TEXT DEFAULT 'Pending',
            notes TEXT,
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id)
        )
    """)
    
    # 9A. TRANSACTIONS - Universal transaction table (ALL payment sources)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            
            -- Core fields (required for all)
            source TEXT NOT NULL,
            transaction_date DATE NOT NULL,
            merchant_name TEXT,
            description TEXT,
            amount REAL NOT NULL,
            
            -- Categorization
            category TEXT,
            purchase_id TEXT,
            notes TEXT,
            
            -- Source-specific details (JSON for flexibility)
            source_data TEXT,
            
            -- Metadata
            import_batch_id INTEGER,
            import_method TEXT,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'Pending',
            
            -- For Wave export tracking
            exported_to_wave BOOLEAN DEFAULT 0,
            export_date TIMESTAMP,
            
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id)
        )
    """)
    
    # 10. MERCHANT_MAPPINGS - Standardize merchant names
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS merchant_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_merchant_name TEXT UNIQUE,
            clean_merchant_name TEXT,
            default_category TEXT,
            auto_assign_purchase_id TEXT,
            notes TEXT
        )
    """)
    
    # 11. GRADING_BATCHES - Track grading submissions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS grading_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_name TEXT,
            grader TEXT DEFAULT 'TAG Grading',
            submission_date DATE,
            received_date DATE,
            chase_transaction_id INTEGER,
            grading_fee REAL DEFAULT 0,
            shipping_cost REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            card_count INTEGER DEFAULT 0,
            cost_per_card REAL DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chase_transaction_id) REFERENCES chase_transactions(id)
        )
    """)
    
    # 12. GRADED_CARDS - Individual graded card tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS graded_cards (
            card_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            cert_number TEXT UNIQUE,
            card_name TEXT,
            grade TEXT,
            year INTEGER,
            manufacturer TEXT,
            brand TEXT,
            card_set TEXT,
            card_number TEXT,
            variation TEXT,
            purchase_id TEXT,
            allocated_cost REAL DEFAULT 0,
            sale_id INTEGER,
            status TEXT DEFAULT 'Inventory',
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES grading_batches(batch_id),
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id),
            FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
        )
    """)
    
    # =========================================================================
    # PHASE 2 TABLES
    # =========================================================================
    
    # 13. CHART_OF_ACCOUNTS - Wave COA integration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chart_of_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL UNIQUE,
            account_code TEXT,
            account_type TEXT NOT NULL,
            account_sub_type TEXT,
            description TEXT,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 14. DATA_IMPORT_LOG - Track all imports
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_import_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            import_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            latest_transaction_date DATE,
            records_imported INTEGER DEFAULT 0,
            records_updated INTEGER DEFAULT 0,
            records_skipped INTEGER DEFAULT 0,
            import_status TEXT DEFAULT 'Success',
            error_message TEXT,
            file_name TEXT
        )
    """)
    
    # 15. DATA_SOURCE_STATUS - Overview of all data sources
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS data_source_status (
            source TEXT PRIMARY KEY,
            last_import_date TIMESTAMP,
            last_transaction_date DATE,
            total_transactions INTEGER DEFAULT 0,
            pending_sku_assignments INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    
    # 16. ORDER_FULFILLMENT - Stripe/direct order tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_fulfillment (
            fulfillment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_order_id TEXT NOT NULL,
            order_date DATE,
            customer_name TEXT,
            customer_email TEXT,
            shipping_name TEXT,
            shipping_address_line1 TEXT,
            shipping_address_line2 TEXT,
            shipping_city TEXT,
            shipping_state TEXT,
            shipping_zip TEXT,
            shipping_country TEXT DEFAULT 'US',
            item_description TEXT,
            quantity INTEGER DEFAULT 1,
            order_total DECIMAL(10,2),
            fulfillment_status TEXT DEFAULT 'Pending',
            tracking_number TEXT,
            carrier TEXT,
            ship_date DATE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source, source_order_id)
        )
    """)
    
    # 17. SHIPPING_COSTS - Pirate Ship cost tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipping_costs (
            shipping_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_number TEXT UNIQUE NOT NULL,
            fulfillment_id INTEGER,
            ship_date DATE,
            carrier TEXT,
            service_type TEXT,
            cost DECIMAL(10,2) NOT NULL,
            weight_oz DECIMAL(10,2),
            from_zip TEXT,
            to_zip TEXT,
            recipient TEXT,
            import_batch_id INTEGER,
            import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            matched BOOLEAN DEFAULT 0,
            FOREIGN KEY (fulfillment_id) REFERENCES order_fulfillment(fulfillment_id)
        )
    """)
    
    # =========================================================================
    # PHASE 3A: EBAY API TABLES
    # =========================================================================
    
    # 18. EBAY_ORDERS - Orders from Fulfillment API
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ebay_orders (
            order_id TEXT PRIMARY KEY,
            order_number TEXT,
            legacy_order_id TEXT,
            created_date TIMESTAMP,
            buyer_username TEXT,
            buyer_name TEXT,
            ship_to_name TEXT,
            ship_to_address1 TEXT,
            ship_to_address2 TEXT,
            ship_to_city TEXT,
            ship_to_state TEXT,
            ship_to_zip TEXT,
            ship_to_country TEXT,
            order_status TEXT,
            order_total DECIMAL(10,2),
            last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 19. EBAY_LINE_ITEMS - Line items per order
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ebay_line_items (
            line_item_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            item_id TEXT,
            item_title TEXT,
            sku TEXT,
            quantity INTEGER,
            unit_price DECIMAL(10,2),
            line_total DECIMAL(10,2),
            FOREIGN KEY (order_id) REFERENCES ebay_orders(order_id)
        )
    """)
    
    # 20. EBAY_FULFILLMENTS - Tracking info per order
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ebay_fulfillments (
            fulfillment_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            tracking_number TEXT,
            carrier TEXT,
            ship_date DATE,
            FOREIGN KEY (order_id) REFERENCES ebay_orders(order_id)
        )
    """)
    
    # 21. EBAY_FEES - Fee transactions from Finances API
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ebay_fees (
            fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            transaction_id TEXT UNIQUE,
            fee_type TEXT,
            amount DECIMAL(10,2),
            transaction_date DATE,
            payout_id TEXT,
            FOREIGN KEY (order_id) REFERENCES ebay_orders(order_id)
        )
    """)
    
    # =========================================================================
    # PHASE 4: TRADES TABLES
    # =========================================================================
    
    # 22. TRADES - Trade transactions (inventory swaps)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date DATE NOT NULL,
            tracking_number TEXT,
            shipping_cost DECIMAL(10,2) DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 23. TRADE_LINES - Individual lines within a trade (GIVE/RECEIVE)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('GIVE', 'RECEIVE')),
            line_type TEXT NOT NULL CHECK(line_type IN ('inventory', 'cash')),
            purchase_id TEXT,
            value DECIMAL(10,2) NOT NULL,
            graded_card_id INTEGER,
            payment_source TEXT,
            transaction_id INTEGER,
            sale_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id),
            FOREIGN KEY (purchase_id) REFERENCES purchases(purchase_id),
            FOREIGN KEY (graded_card_id) REFERENCES graded_cards(card_id),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (sale_id) REFERENCES sales(sale_id)
        )
    """)
    
    # 24. PROFIT_ALLOCATIONS - Track profit allocated from purchases to long-term hold
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profit_allocations (
            allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_purchase_id TEXT NOT NULL,
            target_purchase_id TEXT DEFAULT 'LTH',
            amount DECIMAL(10,2) NOT NULL,
            allocation_date DATE NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_purchase_id) REFERENCES purchases(purchase_id),
            FOREIGN KEY (target_purchase_id) REFERENCES purchases(purchase_id)
        )
    """)
    
    # Create indexes for trades tables
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_tracking ON trades(tracking_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_lines_trade ON trade_lines(trade_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_lines_purchase ON trade_lines(purchase_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_lines_direction ON trade_lines(direction)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profit_alloc_source ON profit_allocations(source_purchase_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_profit_alloc_target ON profit_allocations(target_purchase_id)")
    
    # Create indexes for Phase 2 tables
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fulfillment_status ON order_fulfillment(fulfillment_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fulfillment_tracking ON order_fulfillment(tracking_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipping_tracking ON shipping_costs(tracking_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipping_matched ON shipping_costs(matched)")
    
    # Migration: Add platform_fee column to order_fulfillment if it doesn't exist
    cursor.execute("PRAGMA table_info(order_fulfillment)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'platform_fee' not in columns:
        cursor.execute("ALTER TABLE order_fulfillment ADD COLUMN platform_fee DECIMAL(10,2) DEFAULT 0")
        print("Added platform_fee column to order_fulfillment table")
    
    # Migration: Add recipient column to shipping_costs if it doesn't exist
    cursor.execute("PRAGMA table_info(shipping_costs)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'recipient' not in columns:
        cursor.execute("ALTER TABLE shipping_costs ADD COLUMN recipient TEXT")
        print("Added recipient column to shipping_costs table")
    
    # Migration: Add customer_name column to sales if it doesn't exist
    cursor.execute("PRAGMA table_info(sales)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'customer_name' not in columns:
        cursor.execute("ALTER TABLE sales ADD COLUMN customer_name TEXT")
        print("Added customer_name column to sales table")
        
        # Migrate existing records: extract customer name from item_title where format is "Item (Customer Name)"
        # Only for non-eBay platforms where customer name was manually entered
        cursor.execute("""
            UPDATE sales
            SET customer_name = TRIM(SUBSTR(item_title, INSTR(item_title, '(') + 1, LENGTH(item_title) - INSTR(item_title, '(') - 1))
            WHERE item_title LIKE '%(%)' 
            AND platform != 'eBay'
            AND customer_name IS NULL
        """)
        migrated = cursor.rowcount
        if migrated > 0:
            print(f"Migrated customer_name for {migrated} existing sales records (from item_title)")
        
        # Migrate Stripe sales: get customer_name from order_fulfillment table
        cursor.execute("""
            UPDATE sales
            SET customer_name = (
                SELECT of.customer_name 
                FROM order_fulfillment of 
                WHERE of.source = 'Stripe' 
                AND of.source_order_id = sales.order_number
            )
            WHERE platform = 'Stripe'
            AND customer_name IS NULL
        """)
        stripe_migrated = cursor.rowcount
        if stripe_migrated > 0:
            print(f"Migrated customer_name for {stripe_migrated} existing Stripe sales records")
    
    # Migration: Backfill customer_name for Stripe sales (runs even if column already exists)
    # This catches any Stripe sales that were created before customer_name was added
    cursor.execute("""
        UPDATE sales
        SET customer_name = (
            SELECT of.customer_name 
            FROM order_fulfillment of 
            WHERE of.source = 'Stripe' 
            AND of.source_order_id = sales.order_number
        )
        WHERE platform = 'Stripe'
        AND customer_name IS NULL
        AND EXISTS (
            SELECT 1 FROM order_fulfillment of 
            WHERE of.source = 'Stripe' 
            AND of.source_order_id = sales.order_number
            AND of.customer_name IS NOT NULL
        )
    """)
    stripe_backfilled = cursor.rowcount
    if stripe_backfilled > 0:
        print(f"Backfilled customer_name for {stripe_backfilled} Stripe sales records")
    
    # 18a. STRIPE_LINE_ITEMS - Individual line items from Stripe checkout sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stripe_line_items (
            line_item_id TEXT PRIMARY KEY,
            payment_intent_id TEXT NOT NULL,
            checkout_session_id TEXT,
            stripe_product_id TEXT,
            product_name TEXT,
            description TEXT,
            quantity INTEGER DEFAULT 1,
            unit_amount DECIMAL(10,2),
            amount_subtotal DECIMAL(10,2),
            amount_total DECIMAL(10,2),
            amount_discount DECIMAL(10,2) DEFAULT 0,
            amount_tax DECIMAL(10,2) DEFAULT 0,
            currency TEXT DEFAULT 'usd',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (payment_intent_id) REFERENCES order_fulfillment(source_order_id)
        )
    """)
    
    # 18b. STRIPE_ORDER_TOTALS - Order-level totals for accounting
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stripe_order_totals (
            payment_intent_id TEXT PRIMARY KEY,
            checkout_session_id TEXT,
            amount_subtotal DECIMAL(10,2),
            amount_total DECIMAL(10,2),
            amount_discount DECIMAL(10,2) DEFAULT 0,
            amount_shipping DECIMAL(10,2) DEFAULT 0,
            amount_tax DECIMAL(10,2) DEFAULT 0,
            stripe_fee DECIMAL(10,2) DEFAULT 0,
            net_amount DECIMAL(10,2),
            currency TEXT DEFAULT 'usd',
            customer_name TEXT,
            customer_email TEXT,
            shipping_name TEXT,
            shipping_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 18c. STRIPE_PRODUCT_MAPPING - Map Stripe product IDs to purchase IDs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stripe_product_mapping (
            stripe_product_id TEXT PRIMARY KEY,
            purchase_id TEXT,
            product_name TEXT,
            default_sku TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create indexes for Phase 3A eBay API tables
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_orders_created ON ebay_orders(created_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_orders_status ON ebay_orders(order_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_line_items_order ON ebay_line_items(order_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_line_items_sku ON ebay_line_items(sku)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_fulfillments_order ON ebay_fulfillments(order_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_fulfillments_tracking ON ebay_fulfillments(tracking_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ebay_fees_order ON ebay_fees(order_id)")
    
    # Create indexes for universal transactions table
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_source ON transactions(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_date ON transactions(transaction_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_category ON transactions(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_purchase ON transactions(purchase_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_status ON transactions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_exported ON transactions(exported_to_wave)")

    # =========================================================================
    # SHOPIFY TABLES
    # =========================================================================

    # Staging table for Shopify orders (one row per line item)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shopify_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_name TEXT,
            email TEXT,
            financial_status TEXT,
            paid_at TEXT,
            fulfillment_status TEXT,
            currency TEXT,
            subtotal REAL DEFAULT 0,
            shipping REAL DEFAULT 0,
            taxes REAL DEFAULT 0,
            total REAL DEFAULT 0,
            discount_code TEXT,
            discount_amount REAL DEFAULT 0,
            lineitem_quantity INTEGER DEFAULT 1,
            lineitem_name TEXT,
            lineitem_price REAL DEFAULT 0,
            lineitem_sku TEXT,
            lineitem_fulfillment_status TEXT,
            payment_method TEXT,
            payment_reference TEXT,
            refunded_amount REAL DEFAULT 0,
            vendor TEXT,
            shopify_order_id TEXT,
            source TEXT,
            notes TEXT,
            created_at_shopify TEXT,
            import_batch TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shopify_orders_unique
        ON shopify_orders(order_name, lineitem_sku, lineitem_name, import_batch)
    """)

    # Staging table for Shopify payouts (one row per settlement)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shopify_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payout_date TEXT,
            status TEXT,
            charges REAL DEFAULT 0,
            refunds REAL DEFAULT 0,
            adjustments REAL DEFAULT 0,
            marketplace_sales_tax REAL DEFAULT 0,
            fees REAL DEFAULT 0,
            total REAL DEFAULT 0,
            currency TEXT,
            bank_reference TEXT,
            import_batch TEXT,
            processed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    print("ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦ Database initialized with all tables")

# ============================================================================
# SETTINGS QUERIES
# ============================================================================

def get_setting(key):
    """Get a setting value by key"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def save_setting(key, value):
    """Save a setting (insert or update)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET 
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
    """, (key, value))
    conn.commit()
    conn.close()

def get_all_settings():
    """Get all settings as a dictionary"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

def get_supplies_cost_for_amount(order_total):
    """
    Get the tiered supplies cost based on order total value.
    
    Tiers:
    - Under $20: Tier 1 (standard envelope)
    - $20.01 - $75: Tier 2 (bubble mailer)
    - Over $75: Tier 3 (cardboard box)
    
    Returns the appropriate supplies cost.
    """
    tier1 = float(get_setting('supplies_tier1_cost') or '0.25')
    tier2 = float(get_setting('supplies_tier2_cost') or '0.75')
    tier3 = float(get_setting('supplies_tier3_cost') or '1.50')
    
    if order_total <= 20:
        return tier1
    elif order_total <= 75:
        return tier2
    else:
        return tier3

# ============================================================================
# PURCHASE QUERIES
# ============================================================================

@_cache
def get_all_purchases():
    """Get all purchases with calculated profit/loss including linked expenses"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            p.*,
            COALESCE(SUM(s.net_profit), 0) as sales_profit,
            COUNT(s.sale_id) as items_sold,
            COALESCE(expense_totals.total_expenses, 0) as linked_expenses,
            COALESCE(SUM(s.net_profit), 0) + COALESCE(expense_totals.total_expenses, 0) - COALESCE(p.total_cost, 0) as total_profit
        FROM purchases p
        LEFT JOIN sales s ON p.purchase_id = s.purchase_id
        LEFT JOIN (
            SELECT purchase_id, SUM(amount) as total_expenses
            FROM transactions
            WHERE purchase_id IS NOT NULL
            AND status = 'Categorized'
            GROUP BY purchase_id
        ) expense_totals ON p.purchase_id = expense_totals.purchase_id
        GROUP BY p.purchase_id
        ORDER BY p.date DESC
    """)
    conn.close()
    return df

def get_purchase_by_id(purchase_id):
    """Get single purchase details"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def add_purchase(purchase_id, date, description, location, order_number, total_cost, display_name=None, notes=None, gl_account=None):
    """Add new purchase"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO purchases (purchase_id, date, description, location, order_number, total_cost, display_name, notes, gl_account)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (purchase_id, date, description, location, order_number, total_cost, display_name, notes, gl_account))
    conn.commit()
    conn.close()

def update_purchase_display_name(purchase_id, display_name):
    """Update display name for a purchase"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE purchases 
        SET display_name = ?
        WHERE purchase_id = ?
    """, (display_name, purchase_id))
    conn.commit()
    conn.close()

def update_purchase_gl_account(purchase_id, gl_account):
    """Update GL account for a purchase"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE purchases 
        SET gl_account = ?
        WHERE purchase_id = ?
    """, (gl_account, purchase_id))
    conn.commit()
    conn.close()

def get_purchases_by_gl_account(gl_account):
    """Get purchases filtered by GL account (e.g., '5300' for Grading Fees)"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM purchases
        WHERE gl_account = ?
        ORDER BY date DESC
    """,  params=(gl_account,))
    conn.close()
    return df

def get_purchase_id_from_sku(sku, item_title=None):
    """
    Extract purchase ID from eBay custom label (SKU)
    
    SKU formats:
    - 6+ characters: Purchase ID in YYMMDD format (e.g., 251115, 250902)
    - "PC" or "N/A": Personal collection items ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ "PC"
    - Short codes (e.g., "25H"): LIFE TCG variation listings ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ lookup by title
    
    Args:
        sku: Custom label from eBay
        item_title: Item title for LIFE TCG lookup (optional)
    
    Returns:
        purchase_id string or "UNKNOWN"
    """
    if not sku or pd.isna(sku):
        return "UNKNOWN"
    
    sku = str(sku).strip()
    
    # Personal collection items
    if sku.upper() in ("PC", "N/A"):
        return "PC"
    
    # 6+ character SKUs contain purchase ID
    if len(sku) >= 6:
        # Extract first 6 characters as purchase ID (YYMMDD or YYMMXX format)
        return sku[:6]
    
    # Short SKUs (e.g., "25H") - LIFE TCG variation listings
    # Look up by title in life_tcg_mappings table
    if item_title and len(sku) <= 4:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Check if title matches any LIFE TCG mapping
        cursor.execute("""
            SELECT purchase_id 
            FROM life_tcg_mappings 
            WHERE ? LIKE '%' || title_contains || '%'
            ORDER BY LENGTH(title_contains) DESC
            LIMIT 1
        """, (item_title,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return result[0]
    
    # Unknown format
    return "UNKNOWN"

def get_next_purchase_id(year_month):
    """
    Get next available purchase ID for a given YYMM
    Format: YYMMXX where XX is 01, 02, 03, etc.
    Example: 251101, 251102, 251103 for November 2025
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Find all purchases starting with this YYMM
    cursor.execute("""
        SELECT purchase_id 
        FROM purchases 
        WHERE purchase_id LIKE ?
        ORDER BY purchase_id DESC
        LIMIT 1
    """, (f"{year_month}%",))
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        # Extract the sequence number (last 2 digits)
        last_id = result[0]
        if len(last_id) >= 6:
            try:
                last_seq = int(last_id[4:6])
                next_seq = last_seq + 1
                return f"{year_month}{next_seq:02d}"
            except ValueError:
                # If last 2 digits aren't numeric, start at 01
                return f"{year_month}01"
    
    # No existing purchases for this month, start at 01
    return f"{year_month}01"

def get_consolidation_preview(purchase_ids):
    """
    Preview what will happen when consolidating purchases.
    
    Args:
        purchase_ids: List of purchase IDs to consolidate
    
    Returns:
        dict with preview info: purchases, sales counts, combined cost, etc.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get purchase details
    placeholders = ','.join(['?' for _ in purchase_ids])
    cursor.execute(f"""
        SELECT purchase_id, display_name, total_cost, date, location
        FROM purchases
        WHERE purchase_id IN ({placeholders})
        ORDER BY purchase_id
    """, purchase_ids)
    purchases = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    
    # Get sales count for each purchase
    sales_by_purchase = {}
    total_sales = 0
    for pid in purchase_ids:
        cursor.execute("SELECT COUNT(*) as count FROM sales WHERE purchase_id = ?", (pid,))
        count = cursor.fetchone()[0]
        sales_by_purchase[pid] = count
        total_sales += count
    
    # Calculate combined cost
    combined_cost = sum(p['total_cost'] or 0 for p in purchases)
    
    conn.close()
    
    return {
        'purchases': purchases,
        'sales_by_purchase': sales_by_purchase,
        'total_sales': total_sales,
        'combined_cost': combined_cost,
        'purchase_count': len(purchases)
    }

def consolidate_purchases(master_id, purchase_ids_to_merge):
    """
    Consolidate multiple purchases into one master purchase.
    
    Args:
        master_id: The purchase ID to keep (will receive combined cost)
        purchase_ids_to_merge: List of purchase IDs to merge INTO master (will be deleted)
    
    Returns:
        dict with success status and details
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get master purchase info
        cursor.execute("SELECT * FROM purchases WHERE purchase_id = ?", (master_id,))
        master = cursor.fetchone()
        if not master:
            conn.close()
            return {'success': False, 'message': f'Master purchase {master_id} not found'}
        
        # Calculate combined cost from all purchases being merged
        placeholders = ','.join(['?' for _ in purchase_ids_to_merge])
        cursor.execute(f"""
            SELECT COALESCE(SUM(total_cost), 0) as merge_cost
            FROM purchases
            WHERE purchase_id IN ({placeholders})
        """, purchase_ids_to_merge)
        merge_cost = cursor.fetchone()[0]
        
        # Update master with combined cost
        new_total_cost = (master.get('total_cost') or 0) + merge_cost
        cursor.execute("""
            UPDATE purchases
            SET total_cost = ?
            WHERE purchase_id = ?
        """, (new_total_cost, master_id))
        
        # Update all sales to point to master
        sales_updated = 0
        for pid in purchase_ids_to_merge:
            cursor.execute("""
                UPDATE sales
                SET purchase_id = ?
                WHERE purchase_id = ?
            """, (master_id, pid))
            sales_updated += cursor.rowcount
        
        # Delete the merged purchases
        cursor.execute(f"""
            DELETE FROM purchases
            WHERE purchase_id IN ({placeholders})
        """, purchase_ids_to_merge)
        purchases_deleted = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        return {
            'success': True,
            'message': f'Consolidated {purchases_deleted + 1} purchases into {master_id}',
            'master_id': master_id,
            'new_total_cost': new_total_cost,
            'sales_updated': sales_updated,
            'purchases_deleted': purchases_deleted
        }
        
    except Exception as e:
        conn.rollback()
        conn.close()
        return {'success': False, 'message': str(e)}

# ============================================================================
# SALES QUERIES
# ============================================================================

@_cache
def get_all_sales():
    """Get all sales with purchase display names"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            s.*,
            p.display_name as purchase_display_name
        FROM sales s
        LEFT JOIN purchases p ON s.purchase_id = p.purchase_id
        ORDER BY s.sale_date DESC
    """)
    conn.close()
    return df

def get_sales_by_purchase(purchase_id):
    """Get all sales for a specific purchase"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM sales
        WHERE purchase_id = ?
        ORDER BY sale_date DESC
    """,  params=(purchase_id,))
    conn.close()
    return df

def add_sale(purchase_id, platform, order_number, transaction_id, item_title, custom_label,
             sale_date, quantity, sale_price, shipping_charged, shipping_cost,
             platform_fees_fixed, platform_fees_variable, regulatory_fee,
             promoted_listing_fee, international_fee, supplies_estimate, grading_fee,
             customer_name=None):
    """Add new sale record
    
    Args:
        customer_name: Optional customer name for the sale (used for non-eBay platforms)
    """
    
    # Calculate net profit
    net_profit = (
        sale_price
        - shipping_cost
        - platform_fees_fixed
        - platform_fees_variable
        - regulatory_fee
        - promoted_listing_fee
        - international_fee
        - supplies_estimate
        - grading_fee
    )
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sales (
            purchase_id, platform, order_number, transaction_id, item_title, custom_label,
            sale_date, quantity, sale_price, shipping_charged, shipping_cost,
            platform_fees_fixed, platform_fees_variable, regulatory_fee,
            promoted_listing_fee, international_fee, supplies_estimate, grading_fee, net_profit,
            customer_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        purchase_id, platform, order_number, transaction_id, item_title, custom_label,
        sale_date, quantity, sale_price, shipping_charged, shipping_cost,
        platform_fees_fixed, platform_fees_variable, regulatory_fee,
        promoted_listing_fee, international_fee, supplies_estimate, grading_fee, net_profit,
        customer_name
    ))
    conn.commit()
    conn.close()

def update_sale_purchase_id(sale_id, purchase_id):
    """Update purchase_id for a sale"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE sales 
        SET purchase_id = ?
        WHERE sale_id = ?
    """, (purchase_id, sale_id))
    conn.commit()
    conn.close()

def delete_sale(sale_id):
    """
    Delete a sale record (e.g., for cancelled/unpaid orders).
    
    Args:
        sale_id: ID of the sale to delete
    
    Returns:
        dict with deletion info
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get sale info before deleting
    cursor.execute("SELECT order_number, item_title, sale_price FROM sales WHERE sale_id = ?", (sale_id,))
    sale = cursor.fetchone()
    
    if not sale:
        conn.close()
        return {'deleted': False, 'error': 'Sale not found'}
    
    # Delete the sale
    cursor.execute("DELETE FROM sales WHERE sale_id = ?", (sale_id,))
    conn.commit()
    conn.close()
    
    return {
        'deleted': True,
        'order_number': sale[0],
        'item_title': sale[1],
        'sale_price': sale[2]
    }

# ============================================================================
# CHASE TRANSACTION QUERIES
# ============================================================================

def get_all_chase_transactions():
    """Get all Chase transactions with merchant mappings"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            c.*,
            p.display_name as purchase_display_name
        FROM chase_transactions c
        LEFT JOIN purchases p ON c.purchase_id = p.purchase_id
        ORDER BY c.transaction_date DESC
    """)
    conn.close()
    return df

def get_pending_chase_transactions():
    """Get Chase transactions that need categorization"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            c.*,
            p.display_name as purchase_display_name
        FROM chase_transactions c
        LEFT JOIN purchases p ON c.purchase_id = p.purchase_id
        WHERE c.status = 'Pending'
        ORDER BY c.transaction_date DESC
    """)
    conn.close()
    return df

def add_chase_transaction(card_last_four, transaction_date, post_date, description, 
                         clean_merchant_name, chase_category, transaction_type, amount, 
                         memo, import_batch_id, status='Pending', skip_reason=None):
    """Add new Chase transaction with optional skip tracking"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO chase_transactions (
            card_last_four, transaction_date, post_date, description, clean_merchant_name,
            chase_category, transaction_type, amount, memo, import_batch_id, import_date,
            status, skip_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        card_last_four, transaction_date, post_date, description, clean_merchant_name,
        chase_category, transaction_type, amount, memo, import_batch_id, datetime.now(),
        status, skip_reason
    ))
    conn.commit()
    conn.close()

def update_chase_transaction_categorization(transaction_id, purchase_id=None, expense_category=None, status='Categorized', notes=None):
    """Update Chase transaction with purchase_id or expense_category"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE chase_transactions 
        SET purchase_id = ?,
            expense_category = ?,
            status = ?,
            notes = ?
        WHERE id = ?
    """, (purchase_id, expense_category, status, notes, transaction_id))
    conn.commit()
    conn.close()

# ============================================================================
# UNIVERSAL TRANSACTIONS TABLE - All Payment Sources
# ============================================================================

def add_transaction(source, transaction_date, merchant_name, description, amount,
                   category=None, purchase_id=None, notes=None, source_data=None,
                   import_batch_id=None, import_method='Manual', status='Pending'):
    """
    Add a transaction to the universal transactions table.
    
    Args:
        source: Payment source (e.g., 'Chase-4433', 'PayPal', 'Venmo', 'Cash')
        transaction_date: Date of transaction
        merchant_name: Merchant/payee name
        description: Transaction description
        amount: Transaction amount (negative for expenses, positive for income)
        category: Wave account category
        purchase_id: Link to purchases table (if inventory)
        notes: User notes
        source_data: JSON string with source-specific details
        import_batch_id: Batch ID for CSV imports
        import_method: 'CSV', 'API', or 'Manual'
        status: 'Pending' or 'Categorized'
    
    Returns:
        transaction_id of inserted record
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO transactions (
            source, transaction_date, merchant_name, description, amount,
            category, purchase_id, notes, source_data,
            import_batch_id, import_method, import_date, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source, transaction_date, merchant_name, description, amount,
        category, purchase_id, notes, source_data,
        import_batch_id, import_method, datetime.now(), status
    ))
    transaction_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return transaction_id

def check_transaction_exists(source, transaction_date, description, amount):
    """
    Check if a transaction already exists based on key fields.
    Used for duplicate detection during CSV imports.
    
    Args:
        source: Payment source (e.g., 'Chase-4433')
        transaction_date: Date of transaction (YYYY-MM-DD)
        description: Original transaction description
        amount: Transaction amount
    
    Returns:
        True if duplicate exists, False otherwise
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM transactions 
        WHERE source = ? 
        AND transaction_date = ? 
        AND description = ? 
        AND amount = ?
    """, (source, transaction_date, description, amount))
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0

def update_transaction_categorization(transaction_id, category, purchase_id=None, notes=None):
    """Update transaction with categorization"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE transactions 
        SET category = ?,
            purchase_id = ?,
            notes = ?,
            status = 'Categorized'
        WHERE transaction_id = ?
    """, (category, purchase_id, notes, transaction_id))
    conn.commit()
    conn.close()

def get_all_transactions(start_date=None, end_date=None, source=None, status=None):
    """Get transactions with optional filters"""
    conn = get_connection()
    
    query = "SELECT * FROM transactions WHERE 1=1"
    params = []
    
    if start_date:
        query += " AND transaction_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND transaction_date <= ?"
        params.append(end_date)
    if source:
        query += " AND source = ?"
        params.append(source)
    if status:
        query += " AND status = ?"
        params.append(status)
    
    query += " ORDER BY transaction_date DESC, transaction_id DESC"
    
    df = _read_sql(conn, query,  params=params)
    conn.close()
    return df

def get_uncategorized_transactions():
    """Get all transactions that need categorization"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM transactions 
        WHERE status = 'Pending' OR category IS NULL
        ORDER BY transaction_date DESC
    """)
    conn.close()
    return df

def get_transactions_by_category(start_date, end_date):
    """Get transaction totals grouped by category for Wave export"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            category,
            SUM(amount) as total_amount,
            COUNT(*) as transaction_count
        FROM transactions
        WHERE transaction_date BETWEEN ? AND ?
        AND status = 'Categorized'
        AND category IS NOT NULL
        GROUP BY category
        ORDER BY category
    """,  params=(start_date, end_date))
    conn.close()
    return df

def get_merchant_mapping(raw_merchant_name):
    """Get clean merchant name from mapping"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT clean_merchant_name, default_category, auto_assign_purchase_id
        FROM merchant_mappings
        WHERE raw_merchant_name = ?
    """, (raw_merchant_name,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def add_merchant_mapping(raw_merchant_name, clean_merchant_name, default_category=None, auto_assign_purchase_id=None, notes=None):
    """Add or update merchant mapping"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO merchant_mappings (
            raw_merchant_name, clean_merchant_name, default_category, auto_assign_purchase_id, notes
        ) VALUES (?, ?, ?, ?, ?)
    """, (raw_merchant_name, clean_merchant_name, default_category, auto_assign_purchase_id, notes))
    conn.commit()
    conn.close()

# ============================================================================
# UNIVERSAL TRANSACTIONS (ALL PAYMENT SOURCES)
# ============================================================================

def get_pending_transactions():
    """Get all uncategorized transactions"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM transactions
        WHERE status = 'Pending'
        ORDER BY transaction_date DESC
    """)
    conn.close()
    return df

def update_transaction_categorization(transaction_id, category, purchase_id=None, notes=None):
    """Update transaction categorization"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE transactions
        SET category = ?, purchase_id = ?, notes = ?, status = 'Categorized'
        WHERE transaction_id = ?
    """, (category, purchase_id, notes, transaction_id))
    conn.commit()
    conn.close()

def get_transactions_for_wave_export(start_date, end_date, exported_only=False):
    """Get transactions ready for Wave export"""
    conn = get_connection()
    
    query = """
        SELECT * FROM transactions
        WHERE transaction_date BETWEEN ? AND ?
        AND status = 'Categorized'
    """
    
    if exported_only:
        query += " AND exported_to_wave = 1"
    else:
        query += " AND (exported_to_wave = 0 OR exported_to_wave IS NULL)"
    
    query += " ORDER BY transaction_date, source, transaction_id"
    
    df = _read_sql(conn, query,  params=(start_date, end_date))
    conn.close()
    return df

def mark_transactions_exported(transaction_ids):
    """Mark transactions as exported to Wave"""
    conn = get_connection()
    cursor = conn.cursor()
    
    placeholders = ','.join('?' * len(transaction_ids))
    cursor.execute(f"""
        UPDATE transactions
        SET exported_to_wave = 1, export_date = CURRENT_TIMESTAMP
        WHERE transaction_id IN ({placeholders})
    """, transaction_ids)
    
    conn.commit()
    conn.close()

# ============================================================================
# LIFE TCG MAPPINGS
# ============================================================================

@_cache
def get_all_life_tcg_mappings():
    """Get all LIFE TCG title mappings"""
    conn = get_connection()
    df = _read_sql(conn, "SELECT * FROM life_tcg_mappings ORDER BY title_contains")
    conn.close()
    return df

def add_life_tcg_mapping(title_contains, purchase_id, notes=None):
    """Add new LIFE TCG mapping"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO life_tcg_mappings (title_contains, purchase_id, notes)
            VALUES (?, ?, ?)
        """, (title_contains, purchase_id, notes))
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    finally:
        conn.close()
    return success

def delete_life_tcg_mapping(mapping_id):
    """Delete LIFE TCG mapping"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM life_tcg_mappings WHERE id = ?", (mapping_id,))
    conn.commit()
    conn.close()

# ============================================================================
# DASHBOARD QUERIES
# ============================================================================

@_cache
def get_dashboard_summary():
    """Get summary statistics for dashboard"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Total sales metrics (sales profit before purchase costs)
    cursor.execute("""
        SELECT 
            COUNT(DISTINCT order_number) as total_orders,
            SUM(sale_price) as total_revenue,
            SUM(platform_fees_fixed + platform_fees_variable + regulatory_fee + promoted_listing_fee + international_fee) as total_fees,
            SUM(shipping_cost) as total_shipping_cost,
            SUM(net_profit) as sales_net_profit
        FROM sales
    """)
    sales_summary = _row_to_dict(cursor.fetchone(), cursor)
    
    # Get total purchase costs from linked transactions
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) as total_purchase_costs
        FROM transactions
        WHERE purchase_id IS NOT NULL
        AND status = 'Categorized'
    """)
    purchase_costs = cursor.fetchone()[0] or 0
    
    # Also get total_cost from purchases table (in case some purchases don't use transactions)
    cursor.execute("""
        SELECT COALESCE(SUM(total_cost), 0) as total_from_purchases
        FROM purchases
    """)
    purchases_total = cursor.fetchone()[0] or 0
    
    # True net profit = sales profit + purchase costs (costs are negative) - purchases.total_cost
    # Note: purchase_costs from transactions are already negative
    total_net_profit = (sales_summary['sales_net_profit'] or 0) + purchase_costs - purchases_total
    
    summary = {
        'total_orders': sales_summary['total_orders'],
        'total_revenue': sales_summary['total_revenue'],
        'total_fees': sales_summary['total_fees'],
        'total_shipping_cost': sales_summary['total_shipping_cost'],
        'total_net_profit': total_net_profit,
        'sales_profit': sales_summary['sales_net_profit'],
        'purchase_costs': purchase_costs,
    }
    
    # Sales by purchase - include linked expenses
    cursor.execute("""
        SELECT 
            p.purchase_id,
            p.display_name,
            COUNT(s.sale_id) as item_count,
            SUM(s.net_profit) as sales_profit,
            COALESCE(expense_totals.total_expenses, 0) as linked_expenses,
            COALESCE(SUM(s.net_profit), 0) + COALESCE(expense_totals.total_expenses, 0) - COALESCE(p.total_cost, 0) as total_profit
        FROM purchases p
        LEFT JOIN sales s ON p.purchase_id = s.purchase_id
        LEFT JOIN (
            SELECT purchase_id, SUM(amount) as total_expenses
            FROM transactions
            WHERE purchase_id IS NOT NULL
            AND status = 'Categorized'
            GROUP BY purchase_id
        ) expense_totals ON p.purchase_id = expense_totals.purchase_id
        GROUP BY p.purchase_id
        HAVING item_count > 0
        ORDER BY total_profit DESC
    """)
    sales_by_purchase = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    
    # Import batches
    cursor.execute("""
        SELECT 
            import_batch_id,
            import_date,
            COUNT(*) as transaction_count
        FROM ebay_transactions
        GROUP BY import_batch_id
        ORDER BY import_date DESC
    """)
    import_history = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    
    conn.close()
    
    return {
        'summary': summary,
        'sales_by_purchase': sales_by_purchase,
        'import_history': import_history
    }

@_cache
def get_unknown_sales():
    """Get sales with UNKNOWN purchase_id"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM sales
        WHERE purchase_id = 'UNKNOWN'
        ORDER BY sale_date DESC
    """)
    conn.close()
    return df

# ============================================================================
# GRADING BATCH QUERIES
# ============================================================================

@_cache
def get_all_grading_batches():
    """Get all grading batches with card counts and profitability"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            gb.*,
            COUNT(DISTINCT gc.card_id) as total_cards,
            COUNT(DISTINCT CASE WHEN gc.status = 'Sold' THEN gc.card_id END) as cards_sold,
            COUNT(DISTINCT CASE WHEN gc.status = 'Inventory' THEN gc.card_id END) as cards_remaining,
            COALESCE(SUM(CASE WHEN gc.status = 'Sold' THEN s.net_profit ELSE 0 END), 0) as total_profit
        FROM grading_batches gb
        LEFT JOIN graded_cards gc ON gb.batch_id = gc.batch_id
        LEFT JOIN sales s ON gc.sale_id = s.sale_id
        GROUP BY gb.batch_id
        ORDER BY gb.submission_date DESC
    """)
    conn.close()
    return df

def get_grading_batch_by_id(batch_id):
    """Get single grading batch details"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM grading_batches WHERE batch_id = ?", (batch_id,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def add_grading_batch(batch_name, grader, submission_date, chase_transaction_id=None, 
                     grading_fee=0, shipping_cost=0, notes=None):
    """Add new grading batch"""
    total_cost = grading_fee + shipping_cost
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO grading_batches (
            batch_name, grader, submission_date, chase_transaction_id,
            grading_fee, shipping_cost, total_cost, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (batch_name, grader, submission_date, chase_transaction_id,
          grading_fee, shipping_cost, total_cost, notes))
    
    batch_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return batch_id

def update_grading_batch_costs(batch_id):
    """Recalculate batch costs and allocate to cards"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get batch info
    cursor.execute("""
        SELECT grading_fee, shipping_cost, 
               (SELECT COUNT(*) FROM graded_cards WHERE batch_id = ?) as card_count
        FROM grading_batches
        WHERE batch_id = ?
    """, (batch_id, batch_id))
    
    result = cursor.fetchone()
    if not result:
        conn.close()
        return False
    
    grading_fee = result['grading_fee'] or 0
    shipping_cost = result['shipping_cost'] or 0
    card_count = result['card_count'] or 0
    
    total_cost = grading_fee + shipping_cost
    cost_per_card = total_cost / card_count if card_count > 0 else 0
    
    # Update batch totals
    cursor.execute("""
        UPDATE grading_batches
        SET total_cost = ?,
            card_count = ?,
            cost_per_card = ?
        WHERE batch_id = ?
    """, (total_cost, card_count, cost_per_card, batch_id))
    
    # Update allocated cost for all cards in batch
    cursor.execute("""
        UPDATE graded_cards
        SET allocated_cost = ?
        WHERE batch_id = ?
    """, (cost_per_card, batch_id))
    
    conn.commit()
    conn.close()
    return True

def update_grading_batch_status(batch_id, status, received_date=None):
    """Update batch status and received date"""
    conn = get_connection()
    cursor = conn.cursor()
    
    if received_date:
        cursor.execute("""
            UPDATE grading_batches
            SET status = ?, received_date = ?
            WHERE batch_id = ?
        """, (status, received_date, batch_id))
    else:
        cursor.execute("""
            UPDATE grading_batches
            SET status = ?
            WHERE batch_id = ?
        """, (status, batch_id))
    
    conn.commit()
    conn.close()

def delete_grading_batch(batch_id):
    """
    Delete a grading batch and all associated cards.
    
    Args:
        batch_id: ID of the batch to delete
    
    Returns:
        dict with deletion statistics
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {'cards_deleted': 0, 'batch_deleted': False}
    
    try:
        # First delete all cards in the batch
        cursor.execute("DELETE FROM graded_cards WHERE batch_id = ?", (batch_id,))
        stats['cards_deleted'] = cursor.rowcount
        
        # Then delete the batch itself
        cursor.execute("DELETE FROM grading_batches WHERE batch_id = ?", (batch_id,))
        stats['batch_deleted'] = cursor.rowcount > 0
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
    
    return stats

def check_grading_batch_name_exists(batch_name):
    """Check if a batch with this name already exists"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT batch_id FROM grading_batches WHERE batch_name = ?", (batch_name,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

# ============================================================================
# GRADED CARD QUERIES
# ============================================================================

def get_graded_cards_by_batch(batch_id):
    """Get all graded cards for a specific batch"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            gc.*,
            p.display_name as purchase_display_name,
            s.order_number as sale_order_number,
            s.sale_price,
            s.net_profit as sale_profit
        FROM graded_cards gc
        LEFT JOIN purchases p ON gc.purchase_id = p.purchase_id
        LEFT JOIN sales s ON gc.sale_id = s.sale_id
        WHERE gc.batch_id = ?
        ORDER BY gc.card_id
    """,  params=(batch_id,))
    conn.close()
    return df

@_cache
def get_graded_inventory():
    """Get all unsold graded cards"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            gc.*,
            gb.batch_name,
            gb.grader,
            p.display_name as purchase_display_name
        FROM graded_cards gc
        JOIN grading_batches gb ON gc.batch_id = gb.batch_id
        LEFT JOIN purchases p ON gc.purchase_id = p.purchase_id
        WHERE gc.status = 'Inventory'
        ORDER BY gc.cert_number
    """)
    conn.close()
    return df

def add_graded_card(batch_id, cert_number, card_name, grade, year=None, 
                   manufacturer=None, brand=None, card_set=None, card_number=None,
                   variation=None, purchase_id=None):
    """Add new graded card"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO graded_cards (
                batch_id, cert_number, card_name, grade, year,
                manufacturer, brand, card_set, card_number, variation, purchase_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (batch_id, cert_number, card_name, grade, year,
              manufacturer, brand, card_set, card_number, variation, purchase_id))
        
        card_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return card_id
    except sqlite3.IntegrityError:
        conn.close()
        return None  # Duplicate cert number

def link_graded_card_to_sale(cert_number, sale_id):
    """Link graded card to a sale when it's sold"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get the allocated cost for this card
    cursor.execute("""
        SELECT allocated_cost FROM graded_cards
        WHERE cert_number = ?
    """, (cert_number,))
    
    result = cursor.fetchone()
    if not result:
        conn.close()
        return False
    
    allocated_cost = result['allocated_cost']
    
    # Update graded card status
    cursor.execute("""
        UPDATE graded_cards
        SET sale_id = ?, status = 'Sold'
        WHERE cert_number = ?
    """, (sale_id, cert_number))
    
    # Update sale with grading fee
    cursor.execute("""
        UPDATE sales
        SET grading_fee = ?,
            net_profit = net_profit - ?
        WHERE sale_id = ?
    """, (allocated_cost, allocated_cost, sale_id))
    
    conn.commit()
    conn.close()
    return True

def find_graded_card_by_cert(cert_number):
    """Find graded card by cert number"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT gc.*, gb.batch_name, gb.grader
        FROM graded_cards gc
        JOIN grading_batches gb ON gc.batch_id = gb.batch_id
        WHERE gc.cert_number = ?
    """, (cert_number,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def update_batch_purchase_id(batch_id, purchase_id):
    """Update purchase_id for all cards in a batch"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE graded_cards
        SET purchase_id = ?
        WHERE batch_id = ?
    """, (purchase_id, batch_id))
    
    rows_updated = cursor.rowcount
    conn.commit()
    conn.close()
    return rows_updated

def update_card_purchase_id(card_id, purchase_id):
    """Update purchase_id for a single graded card"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE graded_cards
        SET purchase_id = ?
        WHERE card_id = ?
    """, (purchase_id, card_id))
    
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

# ============================================================================
# PHASE 2: CHART OF ACCOUNTS QUERIES
# ============================================================================

@_cache
def get_all_accounts():
    """Get all chart of accounts entries"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM chart_of_accounts
        WHERE is_active = 1
        ORDER BY account_type, account_sub_type, account_name
    """)
    conn.close()
    return df

def get_accounts_by_type(account_type):
    """Get accounts filtered by type (Asset, Liability, Income, Expense, Equity)"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM chart_of_accounts
        WHERE account_type = ? AND is_active = 1
        ORDER BY account_sub_type, account_name
    """,  params=(account_type,))
    conn.close()
    return df

def get_accounts_by_sub_type(account_sub_type):
    """Get accounts filtered by sub-type (e.g., 'Cost of Goods Sold', 'Operating Expense')"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM chart_of_accounts
        WHERE account_sub_type = ? AND is_active = 1
        ORDER BY account_name
    """,  params=(account_sub_type,))
    conn.close()
    return df

def get_account_by_code(account_code):
    """Get account by code (e.g., '5300' for Grading Fees)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM chart_of_accounts
        WHERE account_code = ?
    """, (account_code,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def get_account_by_name(account_name):
    """Get account by name"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM chart_of_accounts
        WHERE account_name = ?
    """, (account_name,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def seed_chart_of_accounts():
    """
    Populate chart of accounts with Taclaco's Wave accounting categories.
    
    UPDATED 2026-03-08: Full sync with Wave COA export (Wave_COA_03_08_26.csv).
    - Added channel-specific income accounts (eBay Sales, Amazon Sales - Kitcoff, Shopify Sales)
    - Added channel-specific advertising accounts (Amazon, eBay, Google)
    - Added reserved balance accounts per platform (eBay, Stripe, Amazon)
    - Added Inventory - Kitcoff asset account
    - Added equity accounts (Owner Investment / Drawings, Owner's Equity)
    - Removed generic 'Advertising & Promotion' (replaced by channel-specific)
    - Renamed 'Paypal and Venmo' -> 'Paypal' to match Wave exactly
    - Added Trading cards - IndiPro COGS account
    - Added all missing Wave system accounts (AP, AR, Payroll, FX, etc.)
    
    Account names MUST match Wave exactly for journal entry export compatibility.
    Wave IDs are stored as account_code for future API integration.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Check if already seeded
    cursor.execute("SELECT COUNT(*) as count FROM chart_of_accounts")
    if cursor.fetchone()[0] > 0:
        conn.close()
        return  # Already has data
    
    # Taclaco's Wave Chart of Accounts -- synced from Wave_COA_03_08_26.csv
    # Format: (wave_id, account_name, account_type, account_sub_type, description)
    # Wave IDs stored in account_code field for future API lookups
    accounts = [
        # =====================================================================
        # ASSETS
        # =====================================================================
        # Bank / Cash accounts
        (None, 'Cash on Hand', 'Asset', 'Cash and Bank', 'Cash you haven\'t deposited in the bank'),
        ('2365187973424731238', 'Novo Bank', 'Asset', 'Cash and Bank', None),
        ('2365188423154783349', 'Paypal', 'Asset', 'Cash and Bank', None),
        
        # Inventory
        ('2365884162600195987', 'Inventory - Kitcoff', 'Asset', 'Inventory', None),
        
        # Reserved balances (platform clearing accounts)
        # These hold funds between sale date and payout date for bank reconciliation
        ('2366200856971433802', 'Amazon Reserved Balances', 'Asset', 'Other Short-Term Asset', None),
        ('2464786127198999783', 'EBay Reserved Balances', 'Asset', 'Other Short-Term Asset', None),
        ('2465212029930165743', 'Stripe Reserved Balances', 'Asset', 'Other Short-Term Asset', None),
        
        # Other standard assets
        ('2365186195559929808', 'Accounts Receivable', 'Asset', 'Accounts Receivable', 'Money owed by customers'),
        ('2365187061692093540', 'Wave Payroll Clearing', 'Asset', 'Money in Transit', 'Payroll direct deposit holding'),
        
        # =====================================================================
        # LIABILITIES
        # =====================================================================
        # Credit Cards (5 cards)
        ('2365926470167156383', 'AC Ink Unlimited - 6285', 'Liability', 'Credit Card', None),
        ('2365190120690275750', 'Ink Preferred - 4433', 'Liability', 'Credit Card', None),
        ('2365190021771810212', 'Ink Unlimited - 5742', 'Liability', 'Credit Card', None),
        ('2365195755863528901', 'Ink other - 4051', 'Liability', 'Credit Card', None),
        ('2365190215263442353', 'Wells Fargo CC', 'Liability', 'Credit Card', None),
        # Line of credit
        ('2365912759155152274', 'Novo Funding LOC', 'Liability', 'Loan and Line of Credit', None),
        # Other liabilities
        ('2365186195618650066', 'Accounts Payable', 'Liability', 'Accounts Payable', 'Money owed to suppliers'),
        ('2365187059628495968', 'Payroll Liabilities', 'Liability', 'Due For Payroll', 'Payroll taxes and wages owed'),
        
        # =====================================================================
        # INCOME -- Channel-specific revenue accounts
        # =====================================================================
        # Sales revenue by channel (for profitability analysis)
        ('2365186195996137436', 'Amazon Sales - Kitcoff', 'Income', 'Income', 'Amazon product sales (Kitcoff inventory)'),
        ('2365186195929028570', 'eBay Sales', 'Income', 'Income', 'eBay product sales (primary TCG channel)'),
        ('2484035755207415667', 'Shopify Sales', 'Income', 'Income', 'Shopify product sales'),
        ('2484056675506574855', 'Direct Sales', 'Income', 'Income', 'Stripe/PayPal/Venmo direct sales'),
        # Shared income accounts
        ('2366201181878998883', 'Shipping Income', 'Income', 'Income', 'Shipping charged to customers (all channels)'),
        ('2366209877568771896', 'Customer refunds', 'Income', 'Income', 'Refunds issued to customers'),
        # Discounts & clearing
        ('2366201338427201391', 'Discounts & Promos', 'Income', 'Discount', None),
        ('2366178555815122260', 'Clearing - Amazon Settlement', 'Income', 'Other Income', 'Amazon settlement clearing account'),
        
        # =====================================================================
        # COST OF GOODS SOLD
        # =====================================================================
        # Trading card inventory costs (eBay-focused)
        ('2465211284526847390', 'Trading cards - collections', 'Expense', 'Cost of Goods Sold', 'Singles and collection purchases'),
        ('2465211184987624842', 'Trading cards - new product', 'Expense', 'Cost of Goods Sold', 'Sealed product purchases'),
        ('2484036656487847799', 'Trading cards - IndiPro', 'Expense', 'Cost of Goods Sold', 'IndiPro card purchases'),
        # Amazon-specific COGS
        ('2366202991494682588', 'Amazon shipping costs', 'Expense', 'Cost of Goods Sold', 'Amazon fulfillment shipping'),
        ('2366213701960324130', 'COGS - Kitcoff', 'Expense', 'Cost of Goods Sold', 'Kitcoff inventory cost (Amazon clearance)'),
        # Shared COGS
        ('2465210774432372051', 'Shipping Charges', 'Expense', 'Cost of Goods Sold', 'Shipping costs for sold items'),
        ('2465211531604907434', 'Grading Fees', 'Expense', 'Cost of Goods Sold', 'TAG and other grading services'),
        ('2465211869388985830', 'Other merchandise', 'Expense', 'Cost of Goods Sold', 'Other inventory costs'),
        
        # =====================================================================
        # OPERATING EXPENSES
        # =====================================================================
        # Advertising -- channel-specific (replaces old generic 'Advertising & Promotion')
        ('2365186198965703690', 'Advertising - Amazon', 'Expense', 'Operating Expense', 'Amazon ad spend'),
        ('2466987844489565391', 'Advertising - Ebay sponsored', 'Expense', 'Operating Expense', 'eBay promoted listings'),
        ('2484038964697224200', 'Advertising - Google', 'Expense', 'Operating Expense', 'Google ad spend'),
        # Platform fees
        ('2365186199225750546', 'Amazon Fees', 'Expense', 'Operating Expense', 'All fees charged by Amazon'),
        # General operating expenses
        ('2365186198906983432', 'Accounting Fees', 'Expense', 'Operating Expense', 'Accounting or bookkeeping services'),
        ('2365186198168786930', 'Bank Service Charges', 'Expense', 'Operating Expense', 'Bank fees and charges'),
        (None, 'Business Insurance', 'Expense', 'Operating Expense', 'Business insurance premiums'),
        ('2365186196331681766', 'Computer \u2013 Hardware', 'Expense', 'Operating Expense', 'Computer equipment'),
        ('2365186196541396972', 'Computer \u2013 Hosting', 'Expense', 'Operating Expense', 'Web hosting fees'),
        ('2365186196474288106', 'Computer \u2013 Internet', 'Expense', 'Operating Expense', 'Internet services'),
        ('2365186196407179240', 'Computer \u2013 Software', 'Expense', 'Operating Expense', 'Software and subscriptions'),
        ('2365186199150253072', 'Contractor Costs', 'Expense', 'Operating Expense', 'Contractor and augmentation costs'),
        ('2365186198579828734', 'Depreciation Expense', 'Expense', 'Operating Expense', 'Depreciation of fixed assets'),
        ('2365186199091532814', 'Duplicate transactions', 'Expense', 'Operating Expense', 'Failed payments and duplicates'),
        ('2365186198311393270', 'Insurance \u2013 Vehicles', 'Expense', 'Operating Expense', 'Vehicle insurance'),
        ('2365186198244284404', 'Interest Expense', 'Expense', 'Operating Expense', 'Interest on loans and debt'),
        ('2465953321698055743', 'LLC Tax', 'Expense', 'Operating Expense', 'LLC franchise tax'),
        ('2365186198646936576', 'Meals and Entertainment', 'Expense', 'Operating Expense', 'Business meals and entertainment'),
        ('2365913102853199264', 'Novo Funding - Monthly Rate', 'Expense', 'Operating Expense', 'Interest on Novo Funding draws'),
        ('2365186198445611002', 'Office Supplies', 'Expense', 'Operating Expense', 'Office supplies'),
        ('2365186196600117230', 'Postage & Shipping', 'Expense', 'Operating Expense', 'Shipping costs (eBay/general fulfillment)'),
        ('2365186199032812556', 'Professional Fees', 'Expense', 'Operating Expense', 'Consultant and professional fees'),
        ('2365186196205852642', 'Rent Expense', 'Expense', 'Operating Expense', 'Rent or lease costs'),
        ('2365186196264572900', 'Repairs & Maintenance', 'Expense', 'Operating Expense', 'Repair and upkeep'),
        ('2465211044981757297', 'Shipping Supplies', 'Expense', 'Operating Expense', 'Envelopes, mailers, boxes, etc'),
        ('2365186196667226096', 'Telephone \u2013 Wireless', 'Expense', 'Operating Expense', 'Mobile phone services'),
        ('2365186198512719868', 'Utilities', 'Expense', 'Operating Expense', 'Utilities for business'),
        ('2365186198378502136', 'Virtual Mailbox', 'Expense', 'Operating Expense', 'Virtual mailbox costs'),
        
        # Payment Processing Fees
        ('2365186196130355168', 'Merchant Account Fees', 'Expense', 'Payment Processing Fee', 'Credit card processing fees'),
        ('2484037614894700526', 'Shopify Payment Processing Fees', 'Expense', 'Payment Processing Fee', 'Shopify payment processing'),
        
        # Payroll (Wave system accounts -- included for completeness)
        ('2365187059913708642', 'Payroll Employer Taxes', 'Expense', 'Payroll Expense', None),
        ('2365187059334894686', 'Payroll Gross Pay', 'Expense', 'Payroll Expense', None),
        ('2365186198705656834', 'Payroll \u2013 Employee Benefits', 'Expense', 'Payroll Expense', None),
        ('2365186198772765700', 'Payroll \u2013 Employer\'s Share of Benefits', 'Expense', 'Payroll Expense', None),
        ('2365186198839874566', 'Payroll \u2013 Salary & Wages', 'Expense', 'Payroll Expense', None),
        
        # =====================================================================
        # EQUITY
        # =====================================================================
        ('2365186195761256406', 'Owner Investment / Drawings', 'Equity', 'Business Owner Contribution and Drawing',
         'Owner investment and personal draws'),
        ('2365186195685758932', 'Owner\'s Equity', 'Equity', 'Retained Earnings: Profit',
         'Retained earnings / owner equity'),
        
        # =====================================================================
        # DASHBOARD-ONLY (not in Wave -- used for internal skip/exclude logic)
        # =====================================================================
        (None, 'SKIP - Credit Card Payment', 'Expense', 'Exclude from P&L', 'Credit card payments (not an expense, just a transfer)'),
        (None, 'SKIP - Transfer Between Accounts', 'Expense', 'Exclude from P&L', 'Transfers between accounts (not an expense)'),
        (None, 'SKIP - Personal', 'Expense', 'Exclude from P&L', 'Personal transactions to exclude'),
    ]
    
    for account_code, account_name, account_type, account_sub_type, description in accounts:
        cursor.execute("""
            INSERT INTO chart_of_accounts (account_code, account_name, account_type, account_sub_type, description, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (account_code, account_name, account_type, account_sub_type, description))
    
    conn.commit()
    conn.close()
    print(f"\u2705 Seeded chart of accounts with {len(accounts)} categories from Wave COA (synced 2026-03-08)")


def sync_chart_of_accounts():
    """
    Upsert chart of accounts -- adds any missing accounts without destroying existing data.
    
    Use this instead of seed_chart_of_accounts() when the COA has already been seeded
    but new accounts were added in Wave. Safe to run multiple times.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # New accounts added in the 2026-03-08 Wave sync
    new_accounts = [
        # Assets added
        ('2365884162600195987', 'Inventory - Kitcoff', 'Asset', 'Inventory', None),
        ('2464786127198999783', 'EBay Reserved Balances', 'Asset', 'Other Short-Term Asset', None),
        ('2465212029930165743', 'Stripe Reserved Balances', 'Asset', 'Other Short-Term Asset', None),
        ('2365186195559929808', 'Accounts Receivable', 'Asset', 'Accounts Receivable', 'Money owed by customers'),
        ('2365187061692093540', 'Wave Payroll Clearing', 'Asset', 'Money in Transit', None),
        
        # Liabilities added
        ('2365186195618650066', 'Accounts Payable', 'Liability', 'Accounts Payable', 'Money owed to suppliers'),
        ('2365187059628495968', 'Payroll Liabilities', 'Liability', 'Due For Payroll', None),
        
        # Income accounts (ALL new -- these were completely missing)
        ('2365186195996137436', 'Amazon Sales - Kitcoff', 'Income', 'Income', 'Amazon product sales (Kitcoff inventory)'),
        ('2365186195929028570', 'eBay Sales', 'Income', 'Income', 'eBay product sales (primary TCG channel)'),
        ('2484035755207415667', 'Shopify Sales', 'Income', 'Income', 'Shopify product sales'),
        ('2484056675506574855', 'Direct Sales', 'Income', 'Income', 'Stripe/PayPal/Venmo direct sales'),
        ('2366201181878998883', 'Shipping Income', 'Income', 'Income', 'Shipping charged to customers'),
        ('2366209877568771896', 'Customer refunds', 'Income', 'Income', 'Refunds issued to customers'),
        ('2366201338427201391', 'Discounts & Promos', 'Income', 'Discount', None),
        ('2366178555815122260', 'Clearing - Amazon Settlement', 'Income', 'Other Income', 'Amazon settlement clearing'),
        
        # COGS added
        ('2484036656487847799', 'Trading cards - IndiPro', 'Expense', 'Cost of Goods Sold', 'IndiPro card purchases'),
        
        # Operating expenses added (channel-specific advertising)
        ('2365186198965703690', 'Advertising - Amazon', 'Expense', 'Operating Expense', 'Amazon ad spend'),
        ('2466987844489565391', 'Advertising - Ebay sponsored', 'Expense', 'Operating Expense', 'eBay promoted listings'),
        ('2484038964697224200', 'Advertising - Google', 'Expense', 'Operating Expense', 'Google ad spend'),
        ('2365186198579828734', 'Depreciation Expense', 'Expense', 'Operating Expense', 'Depreciation of fixed assets'),
        ('2365186198311393270', 'Insurance \u2013 Vehicles', 'Expense', 'Operating Expense', 'Vehicle insurance'),
        ('2465953321698055743', 'LLC Tax', 'Expense', 'Operating Expense', 'LLC franchise tax'),
        
        # Payment processing added
        ('2484037614894700526', 'Shopify Payment Processing Fees', 'Expense', 'Payment Processing Fee', 'Shopify fees'),
        
        # Payroll (Wave system)
        ('2365187059913708642', 'Payroll Employer Taxes', 'Expense', 'Payroll Expense', None),
        ('2365187059334894686', 'Payroll Gross Pay', 'Expense', 'Payroll Expense', None),
        ('2365186198705656834', 'Payroll \u2013 Employee Benefits', 'Expense', 'Payroll Expense', None),
        ('2365186198772765700', 'Payroll \u2013 Employer\'s Share of Benefits', 'Expense', 'Payroll Expense', None),
        ('2365186198839874566', 'Payroll \u2013 Salary & Wages', 'Expense', 'Payroll Expense', None),
        
        # Equity
        ('2365186195761256406', 'Owner Investment / Drawings', 'Equity', 'Business Owner Contribution and Drawing',
         'Owner investment and personal draws'),
        ('2365186195685758932', 'Owner\'s Equity', 'Equity', 'Retained Earnings: Profit', 'Retained earnings'),
    ]
    
    added = 0
    for account_code, account_name, account_type, account_sub_type, description in new_accounts:
        cursor.execute("SELECT COUNT(*) as cnt FROM chart_of_accounts WHERE account_name = ?", (account_name,))
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO chart_of_accounts (account_code, account_name, account_type, account_sub_type, description, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (account_code, account_name, account_type, account_sub_type, description))
            added += 1
    
    # Also rename 'Paypal and Venmo' -> 'Paypal' if the old name still exists
    cursor.execute("UPDATE chart_of_accounts SET account_name = 'Paypal' WHERE account_name = 'Paypal and Venmo'")
    renamed_paypal = cursor.rowcount
    
    # Update Wave IDs (account_code) for existing accounts that were missing them
    wave_id_updates = [
        ('2366200856971433802', 'Amazon Reserved Balances'),
        ('2365926470167156383', 'AC Ink Unlimited - 6285'),
        ('2365190120690275750', 'Ink Preferred - 4433'),
        ('2365190021771810212', 'Ink Unlimited - 5742'),
        ('2365195755863528901', 'Ink other - 4051'),
        ('2365190215263442353', 'Wells Fargo CC'),
        ('2365912759155152274', 'Novo Funding LOC'),
        ('2365187973424731238', 'Novo Bank'),
        ('2465211284526847390', 'Trading cards - collections'),
        ('2465211184987624842', 'Trading cards - new product'),
        ('2366202991494682588', 'Amazon shipping costs'),
        ('2366213701960324130', 'COGS - Kitcoff'),
        ('2465210774432372051', 'Shipping Charges'),
        ('2465211531604907434', 'Grading Fees'),
        ('2465211869388985830', 'Other merchandise'),
        ('2365186199225750546', 'Amazon Fees'),
        ('2365186198906983432', 'Accounting Fees'),
        ('2365186198168786930', 'Bank Service Charges'),
        ('2365186199150253072', 'Contractor Costs'),
        ('2365186199091532814', 'Duplicate transactions'),
        ('2365186198244284404', 'Interest Expense'),
        ('2365186198646936576', 'Meals and Entertainment'),
        ('2365913102853199264', 'Novo Funding - Monthly Rate'),
        ('2365186198445611002', 'Office Supplies'),
        ('2365186196600117230', 'Postage & Shipping'),
        ('2365186199032812556', 'Professional Fees'),
        ('2365186196205852642', 'Rent Expense'),
        ('2365186196264572900', 'Repairs & Maintenance'),
        ('2465211044981757297', 'Shipping Supplies'),
        ('2365186198512719868', 'Utilities'),
        ('2365186198378502136', 'Virtual Mailbox'),
        ('2365186196130355168', 'Merchant Account Fees'),
    ]
    
    updated_ids = 0
    for wave_id, name in wave_id_updates:
        cursor.execute("""
            UPDATE chart_of_accounts SET account_code = ? 
            WHERE account_name = ? AND (account_code IS NULL OR account_code = '')
        """, (wave_id, name))
        updated_ids += cursor.rowcount
    
    conn.commit()
    conn.close()
    
    print(f"\u2705 COA sync complete: {added} accounts added, {renamed_paypal} renamed, {updated_ids} Wave IDs updated")
    return {'added': added, 'renamed': renamed_paypal, 'wave_ids_updated': updated_ids}


# ============================================================================
# PLATFORM -> GL ACCOUNT ROUTING
# ============================================================================

# Master mapping: platform + account_type -> Wave GL account name
# This is the single source of truth for all journal entry routing.
# Account names MUST match Wave COA exactly (case-sensitive).
PLATFORM_GL_ROUTING = {
    # -------------------------------------------------------------------------
    # REVENUE accounts -- where to credit product sales by channel
    # -------------------------------------------------------------------------
    'revenue': {
        'Amazon':   'Amazon Sales - Kitcoff',     # Amazon = Kitcoff inventory only
        'eBay':     'eBay Sales',                  # eBay = primary TCG channel
        'Shopify':  'Shopify Sales',               # Shopify sales
        'Stripe':   'Direct Sales',                # Stripe direct sales
        'PayPal':   'Direct Sales',                # PayPal direct sales
        'Venmo':    'Direct Sales',                # Venmo direct sales
        'Trade':    'eBay Sales',                  # Trade value -> eBay Sales
        '_default': 'Direct Sales',
    },
    
    # -------------------------------------------------------------------------
    # SHIPPING INCOME -- shared across all channels (single Wave account)
    # -------------------------------------------------------------------------
    'shipping_income': {
        '_default': 'Shipping Income',
    },
    
    # -------------------------------------------------------------------------
    # PLATFORM FEES -- where to debit marketplace/processing fees
    # -------------------------------------------------------------------------
    'fees': {
        'Amazon':   'Amazon Fees',                 # Amazon seller fees
        'eBay':     'Merchant Account Fees',       # eBay final value fees -> payment processing
        'Shopify':  'Shopify Payment Processing Fees',
        'Stripe':   'Merchant Account Fees',       # Stripe processing fees
        'PayPal':   'Merchant Account Fees',       # PayPal fees
        '_default': 'Merchant Account Fees',
    },
    
    # -------------------------------------------------------------------------
    # ADVERTISING -- channel-specific ad spend
    # -------------------------------------------------------------------------
    'advertising': {
        'Amazon':   'Advertising - Amazon',
        'eBay':     'Advertising - Ebay sponsored',
        'Google':   'Advertising - Google',
        '_default': 'Advertising - Amazon',        # Fallback (rare)
    },
    
    # -------------------------------------------------------------------------
    # SHIPPING EXPENSE -- fulfillment costs by channel
    # -------------------------------------------------------------------------
    'shipping_expense': {
        'Amazon':   'Amazon shipping costs',       # Amazon fulfillment -> COGS
        'eBay':     'Postage & Shipping',          # eBay/general -> Operating Expense
        'Stripe':   'Postage & Shipping',          # Direct orders
        'Shopify':  'Postage & Shipping',
        'PayPal':   'Postage & Shipping',
        'Venmo':    'Postage & Shipping',
        'Trade':    'Postage & Shipping',
        '_default': 'Postage & Shipping',
    },
    
    # -------------------------------------------------------------------------
    # COGS -- cost of goods sold (default by channel; purchase gl_account overrides)
    # -------------------------------------------------------------------------
    'cogs': {
        'Amazon':   'COGS - Kitcoff',             # Amazon = Kitcoff clearance
        'eBay':     'Trading cards - collections', # eBay default = collections
        'Stripe':   'Trading cards - collections',
        'Shopify':  'Other merchandise',
        'Trade':    'Trading cards - collections',
        '_default': 'Trading cards - collections',
    },
    
    # -------------------------------------------------------------------------
    # RESERVED BALANCES -- platform clearing accounts (debit on sale, credit on payout)
    # -------------------------------------------------------------------------
    'reserved_balance': {
        'Amazon':   'Amazon Reserved Balances',
        'eBay':     'EBay Reserved Balances',
        'Stripe':   'Stripe Reserved Balances',
        'Shopify':  'Stripe Reserved Balances',    # Shopify payouts via Stripe
        '_default': 'Accounts Receivable',         # Fallback for direct sales
    },
    
    # -------------------------------------------------------------------------
    # REFUNDS -- customer refund income offset
    # -------------------------------------------------------------------------
    'refunds': {
        '_default': 'Customer refunds',
    },
    
    # -------------------------------------------------------------------------
    # GRADING FEES -- shared across all channels
    # -------------------------------------------------------------------------
    'grading': {
        '_default': 'Grading Fees',
    },
}


def get_gl_account_for_platform(platform, account_type):
    """
    Route transactions to channel-specific GL accounts for Wave journal entries.
    
    This is the single routing function used by all journal entry generation.
    Account names returned match Wave COA exactly (case-sensitive).
    
    Args:
        platform (str): Sales channel -- 'eBay', 'Amazon', 'Stripe', 'Shopify',
                        'PayPal', 'Venmo', 'Trade', etc.
        account_type (str): Type of GL account needed:
            'revenue'          -- Product sales (credit)
            'shipping_income'  -- Shipping charged to customer (credit)
            'fees'             -- Platform/processing fees (debit)
            'advertising'      -- Ad spend (debit)
            'shipping_expense' -- Fulfillment cost (debit)
            'cogs'             -- Cost of goods sold (debit)
            'reserved_balance' -- Platform clearing account (debit on sale)
            'refunds'          -- Customer refunds (debit)
            'grading'          -- Grading fees (debit)
    
    Returns:
        str: GL account name matching Wave COA exactly
    
    Examples:
        >>> get_gl_account_for_platform('Amazon', 'revenue')
        'Amazon Sales - Kitcoff'
        >>> get_gl_account_for_platform('eBay', 'revenue')
        'eBay Sales'
        >>> get_gl_account_for_platform('Amazon', 'fees')
        'Amazon Fees'
        >>> get_gl_account_for_platform('eBay', 'advertising')
        'Advertising - Ebay sponsored'
        >>> get_gl_account_for_platform('eBay', 'reserved_balance')
        'EBay Reserved Balances'
        >>> get_gl_account_for_platform('Stripe', 'shipping_expense')
        'Postage & Shipping'
    """
    if account_type not in PLATFORM_GL_ROUTING:
        raise ValueError(f"Unknown account_type '{account_type}'. "
                        f"Valid types: {list(PLATFORM_GL_ROUTING.keys())}")
    
    routing = PLATFORM_GL_ROUTING[account_type]
    
    # Normalize platform name for lookup (handle case variations)
    platform_normalized = (platform or '').strip()
    # Try exact match first, then title-case, then default
    if platform_normalized in routing:
        return routing[platform_normalized]
    elif platform_normalized.title() in routing:
        return routing[platform_normalized.title()]
    elif platform_normalized.lower() == 'ebay':
        return routing.get('eBay', routing['_default'])
    else:
        return routing['_default']


def get_gl_account_for_sale(sale_row):
    """
    Convenience function: determine GL accounts for a complete sale record.
    
    Args:
        sale_row (dict): A row from the sales table with at least 'platform' key.
                        Optionally includes 'purchase_id' for COGS lookup.
    
    Returns:
        dict: Mapping of account_type -> GL account name for this sale.
    
    Example return:
        {
            'revenue': 'eBay Sales',
            'shipping_income': 'Shipping Income',
            'fees': 'Merchant Account Fees',
            'shipping_expense': 'Postage & Shipping',
            'cogs': 'Trading cards - collections',
            'reserved_balance': 'EBay Reserved Balances',
            'grading': 'Grading Fees',
        }
    """
    platform = sale_row.get('platform', 'eBay')
    
    accounts = {}
    for account_type in ['revenue', 'shipping_income', 'fees', 'shipping_expense',
                         'cogs', 'reserved_balance', 'grading', 'refunds']:
        accounts[account_type] = get_gl_account_for_platform(platform, account_type)
    
    # Override COGS if the sale's purchase has a specific gl_account set
    purchase_id = sale_row.get('purchase_id')
    if purchase_id:
        purchase = get_purchase_by_id(purchase_id)
        if purchase and purchase.get('gl_account'):
            accounts['cogs'] = purchase['gl_account']
    
    return accounts


def get_expense_categories():
    """Get all expense account names for dropdown menus"""
    # Ensure chart is seeded
    seed_chart_of_accounts()
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT account_name, account_code, account_sub_type
        FROM chart_of_accounts
        WHERE account_type = 'Expense' AND is_active = 1
        ORDER BY account_sub_type, account_name
    """)
    results = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    conn.close()
    return results

def get_cogs_accounts():
    """Get Cost of Goods Sold accounts (for purchase categorization)"""
    # Ensure chart is seeded
    seed_chart_of_accounts()
    
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM chart_of_accounts
        WHERE account_sub_type = 'Cost of Goods Sold' AND is_active = 1
        ORDER BY account_name
    """)
    conn.close()
    return df

def get_bank_accounts():
    """Get bank asset accounts for transfers"""
    # Ensure chart is seeded
    seed_chart_of_accounts()
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT account_name, account_code
        FROM chart_of_accounts
        WHERE account_sub_type = 'Bank' AND is_active = 1
        ORDER BY account_name
    """)
    results = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    conn.close()
    return results

def get_credit_card_accounts():
    """Get credit card liability accounts for payments"""
    # Ensure chart is seeded
    seed_chart_of_accounts()
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT account_name, account_code
        FROM chart_of_accounts
        WHERE account_sub_type = 'Credit Card' AND is_active = 1
        ORDER BY account_name
    """)
    results = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    conn.close()
    return results

@_cache
def get_all_accounts_for_categorization():
    """Get all accounts that can be used for transaction categorization"""
    # Ensure chart is seeded
    seed_chart_of_accounts()
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT account_name, account_code, account_type, account_sub_type
        FROM chart_of_accounts
        WHERE is_active = 1
        ORDER BY 
            CASE 
                WHEN account_sub_type = 'Cost of Goods Sold' THEN 1
                WHEN account_type = 'Expense' THEN 2
                WHEN account_sub_type = 'Credit Card' THEN 3
                WHEN account_sub_type = 'Loan and Line of Credit' THEN 4
                WHEN account_sub_type = 'Cash and Bank' THEN 5
                WHEN account_type = 'Asset' THEN 6
                ELSE 7
            END,
            account_name
    """)
    results = [_row_to_dict(r, cursor) for r in cursor.fetchall()]
    conn.close()
    return results

# ============================================================================
# PHASE 2: DATA SOURCE STATUS QUERIES
# ============================================================================

@_cache
def get_all_data_source_status():
    """Get status of all data sources"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM data_source_status
        ORDER BY 
            CASE source 
                WHEN 'eBay' THEN 1
                WHEN 'Stripe' THEN 2
                WHEN 'PayPal' THEN 3
                WHEN 'Venmo' THEN 4
                WHEN 'Amazon' THEN 5
                WHEN 'Pirate Ship' THEN 6
                ELSE 7
            END
    """)
    conn.close()
    return df

def update_data_source_status(source, last_import_date=None, last_transaction_date=None,
                              total_transactions=None, pending_sku_assignments=None):
    """Update status for a data source"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Build dynamic update
    updates = []
    params = []
    
    if last_import_date is not None:
        updates.append("last_import_date = ?")
        params.append(last_import_date)
    if last_transaction_date is not None:
        updates.append("last_transaction_date = ?")
        params.append(last_transaction_date)
    if total_transactions is not None:
        updates.append("total_transactions = ?")
        params.append(total_transactions)
    if pending_sku_assignments is not None:
        updates.append("pending_sku_assignments = ?")
        params.append(pending_sku_assignments)
    
    if updates:
        params.append(source)
        cursor.execute(f"""
            UPDATE data_source_status
            SET {', '.join(updates)}
            WHERE source = ?
        """, params)
        conn.commit()
    
    conn.close()

def recalculate_data_source_status():
    """Recalculate all data source statistics from actual data"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # eBay - from sales table
    cursor.execute("""
        UPDATE data_source_status
        SET total_transactions = (SELECT COUNT(*) FROM sales WHERE platform = 'eBay'),
            last_transaction_date = (SELECT MAX(sale_date) FROM sales WHERE platform = 'eBay'),
            pending_sku_assignments = (SELECT COUNT(*) FROM sales WHERE platform = 'eBay' AND purchase_id = 'UNKNOWN')
        WHERE source = 'eBay'
    """)
    
    # Stripe - from order_fulfillment table
    cursor.execute("""
        UPDATE data_source_status
        SET total_transactions = (SELECT COUNT(*) FROM order_fulfillment WHERE source = 'Stripe'),
            last_transaction_date = (SELECT MAX(order_date) FROM order_fulfillment WHERE source = 'Stripe')
        WHERE source = 'Stripe'
    """)
    
    # Pirate Ship - from shipping_costs table
    cursor.execute("""
        UPDATE data_source_status
        SET total_transactions = (SELECT COUNT(*) FROM shipping_costs),
            last_transaction_date = (SELECT MAX(ship_date) FROM shipping_costs),
            pending_sku_assignments = (SELECT COUNT(*) FROM shipping_costs WHERE matched = 0)
        WHERE source = 'Pirate Ship'
    """)
    
    conn.commit()
    conn.close()

def log_data_import(source, records_imported, records_updated=0, records_skipped=0,
                    latest_transaction_date=None, import_status='Success', 
                    error_message=None, file_name=None):
    """Log a data import event"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO data_import_log (
            source, latest_transaction_date, records_imported, records_updated,
            records_skipped, import_status, error_message, file_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (source, latest_transaction_date, records_imported, records_updated,
          records_skipped, import_status, error_message, file_name))
    
    log_id = cursor.lastrowid
    
    # Also update data_source_status
    cursor.execute("""
        UPDATE data_source_status
        SET last_import_date = CURRENT_TIMESTAMP
        WHERE source = ?
    """, (source,))
    
    conn.commit()
    conn.close()
    return log_id

def get_import_history(source=None, limit=20):
    """Get recent import history, optionally filtered by source"""
    conn = get_connection()
    
    if source:
        df = _read_sql(conn, """
            SELECT * FROM data_import_log
            WHERE source = ?
            ORDER BY import_timestamp DESC
            LIMIT ?
        """,  params=(source, limit))
    else:
        df = _read_sql(conn, """
            SELECT * FROM data_import_log
            ORDER BY import_timestamp DESC
            LIMIT ?
        """,  params=(limit,))
    
    conn.close()
    return df

# ============================================================================
# PHASE 2: ORDER FULFILLMENT QUERIES
# ============================================================================

def get_all_orders(status=None):
    """Get all orders, optionally filtered by status"""
    conn = get_connection()
    
    if status:
        df = _read_sql(conn, """
            SELECT * FROM order_fulfillment
            WHERE fulfillment_status = ?
            ORDER BY order_date DESC
        """,  params=(status,))
    else:
        df = _read_sql(conn, """
            SELECT * FROM order_fulfillment
            ORDER BY order_date DESC
        """)
    
    conn.close()
    return df

def get_pending_orders():
    """Get orders that need to be shipped"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM order_fulfillment
        WHERE fulfillment_status IN ('Pending', 'Printed')
        ORDER BY order_date ASC
    """)
    conn.close()
    return df

def get_order_by_id(fulfillment_id):
    """Get single order by ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM order_fulfillment WHERE fulfillment_id = ?", (fulfillment_id,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def get_order_by_source_id(source, source_order_id):
    """Get order by source and source order ID"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM order_fulfillment 
        WHERE source = ? AND source_order_id = ?
    """, (source, source_order_id))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def add_order(source, source_order_id, order_date, customer_name, customer_email,
              shipping_name, shipping_address_line1, shipping_address_line2,
              shipping_city, shipping_state, shipping_zip, shipping_country,
              item_description, quantity, order_total, notes=None,
              fulfillment_status='Pending', tracking_number=None, carrier=None, ship_date=None,
              platform_fee=0):
    """Add new order for fulfillment"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO order_fulfillment (
                source, source_order_id, order_date, customer_name, customer_email,
                shipping_name, shipping_address_line1, shipping_address_line2,
                shipping_city, shipping_state, shipping_zip, shipping_country,
                item_description, quantity, order_total, notes,
                fulfillment_status, tracking_number, carrier, ship_date, platform_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, source_order_id, order_date, customer_name, customer_email,
              shipping_name, shipping_address_line1, shipping_address_line2,
              shipping_city, shipping_state, shipping_zip, shipping_country,
              item_description, quantity, order_total, notes,
              fulfillment_status, tracking_number, carrier, ship_date, platform_fee))
        
        fulfillment_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return fulfillment_id
    except sqlite3.IntegrityError:
        conn.close()
        return None  # Duplicate order

def update_order_status(fulfillment_id, status, tracking_number=None, carrier=None, ship_date=None):
    """Update order fulfillment status"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE order_fulfillment
        SET fulfillment_status = ?,
            tracking_number = COALESCE(?, tracking_number),
            carrier = COALESCE(?, carrier),
            ship_date = COALESCE(?, ship_date),
            updated_at = CURRENT_TIMESTAMP
        WHERE fulfillment_id = ?
    """, (status, tracking_number, carrier, ship_date, fulfillment_id))
    
    conn.commit()
    conn.close()

def update_order_tracking(fulfillment_id, tracking_number, carrier=None):
    """Update tracking number for an order"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE order_fulfillment
        SET tracking_number = ?,
            carrier = COALESCE(?, carrier),
            fulfillment_status = 'Shipped',
            ship_date = COALESCE(ship_date, DATE('now')),
            updated_at = CURRENT_TIMESTAMP
        WHERE fulfillment_id = ?
    """, (tracking_number, carrier, fulfillment_id))
    
    conn.commit()
    conn.close()

def generate_pirate_ship_csv(order_ids):
    """
    Generate CSV data for Pirate Ship batch upload
    Returns: list of dicts ready for CSV export
    """
    conn = get_connection()
    
    placeholders = ','.join(['?' for _ in order_ids])
    df = _read_sql(conn, f"""
        SELECT * FROM order_fulfillment
        WHERE fulfillment_id IN ({placeholders})
    """,  params=order_ids)
    conn.close()
    
    # Map to Pirate Ship format
    pirate_ship_rows = []
    for _, order in df.iterrows():
        pirate_ship_rows.append({
            'Name': order['shipping_name'] or order['customer_name'],
            'Company': '',
            'Address 1': order['shipping_address_line1'] or '',
            'Address 2': order['shipping_address_line2'] or '',
            'City': order['shipping_city'] or '',
            'State': order['shipping_state'] or '',
            'Zip': order['shipping_zip'] or '',
            'Country': order['shipping_country'] or 'US',
            'Phone': '',
            'Email': order['customer_email'] or '',
            'Order ID': order['source_order_id'],
            'Item Description': order['item_description'] or '',
            'Quantity': order['quantity'] or 1,
        })
    
    return pirate_ship_rows

# ============================================================================
# PHASE 2: SHIPPING COSTS QUERIES
# ============================================================================

def get_all_shipping_costs(matched_only=False):
    """Get all shipping costs, optionally filtered to matched only"""
    conn = get_connection()
    
    if matched_only:
        df = _read_sql(conn, """
            SELECT sc.*, of.source_order_id, of.customer_name
            FROM shipping_costs sc
            LEFT JOIN order_fulfillment of ON sc.fulfillment_id = of.fulfillment_id
            WHERE sc.matched = 1
            ORDER BY sc.ship_date DESC
        """)
    else:
        df = _read_sql(conn, """
            SELECT sc.*, of.source_order_id, of.customer_name
            FROM shipping_costs sc
            LEFT JOIN order_fulfillment of ON sc.fulfillment_id = of.fulfillment_id
            ORDER BY sc.ship_date DESC
        """)
    
    conn.close()
    return df

def get_unmatched_shipping_costs():
    """Get shipping costs that haven't been matched to orders"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM shipping_costs
        WHERE matched = 0
        ORDER BY ship_date DESC
    """)
    conn.close()
    return df


def delete_shipping_cost(shipping_id):
    """Delete a shipping cost record (for removing personal shipments)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM shipping_costs WHERE shipping_id = ?", (shipping_id,))
    conn.commit()
    conn.close()
    return True


def add_shipping_cost(tracking_number, ship_date, carrier, service_type, cost,
                      weight_oz=None, from_zip=None, to_zip=None, recipient=None, import_batch_id=None):
    """Add shipping cost record"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO shipping_costs (
                tracking_number, ship_date, carrier, service_type, cost,
                weight_oz, from_zip, to_zip, recipient, import_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tracking_number, ship_date, carrier, service_type, cost,
              weight_oz, from_zip, to_zip, recipient, import_batch_id))
        
        shipping_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return shipping_id
    except sqlite3.IntegrityError:
        conn.close()
        return None  # Duplicate tracking number

def match_shipping_to_order(tracking_number):
    """
    Match a shipping cost to an order by tracking number
    Returns: True if matched, False if no matching order found
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Find order with this tracking number
    cursor.execute("""
        SELECT fulfillment_id FROM order_fulfillment
        WHERE tracking_number = ?
    """, (tracking_number,))
    
    result = cursor.fetchone()
    
    if result:
        fulfillment_id = result['fulfillment_id']
        cursor.execute("""
            UPDATE shipping_costs
            SET fulfillment_id = ?, matched = 1
            WHERE tracking_number = ?
        """, (fulfillment_id, tracking_number))
        conn.commit()
        conn.close()
        return True
    
    conn.close()
    return False

def match_all_shipping_costs():
    """
    Attempt to match all unmatched shipping costs to orders.
    
    This function:
    1. Checks order_fulfillment table (Stripe/direct orders)
    2. Checks ebay_fulfillments table (eBay orders)
    3. Checks trades table (Trade shipments)
    4. Updates sales.shipping_cost when a match is found
    
    Returns: dict with match statistics
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all unmatched shipping costs
    cursor.execute("SELECT shipping_id, tracking_number, cost FROM shipping_costs WHERE matched = 0")
    unmatched = cursor.fetchall()
    
    stats = {
        'total': len(unmatched), 
        'matched': 0, 
        'matched_stripe': 0,
        'matched_ebay': 0,
        'matched_trades': 0,
        'sales_updated': 0,
        'unmatched': 0
    }
    
    for row in unmatched:
        tracking = row[1]
        cost = row[2]
        matched = False
        
        # 1. Try to match to order_fulfillment (Stripe/direct orders)
        cursor.execute("""
            SELECT fulfillment_id, source_order_id FROM order_fulfillment
            WHERE tracking_number = ?
        """, (tracking,))
        
        stripe_result = cursor.fetchone()
        
        if stripe_result:
            # Mark shipping cost as matched
            cursor.execute("""
                UPDATE shipping_costs
                SET fulfillment_id = ?, matched = 1
                WHERE tracking_number = ?
            """, (stripe_result['fulfillment_id'], tracking))
            
            # Update sales.shipping_cost for this order
            cursor.execute("""
                UPDATE sales
                SET shipping_cost = ?,
                    net_profit = net_profit - ?
                WHERE platform = 'Stripe' 
                AND order_number = ?
                AND (shipping_cost = 0 OR shipping_cost IS NULL)
            """, (cost, cost, stripe_result['source_order_id']))
            
            if cursor.rowcount > 0:
                stats['sales_updated'] += 1
            
            stats['matched'] += 1
            stats['matched_stripe'] += 1
            matched = True
        
        # 2. Try to match to ebay_fulfillments (eBay orders)
        if not matched:
            cursor.execute("""
                SELECT order_id FROM ebay_fulfillments
                WHERE tracking_number = ?
            """, (tracking,))
            
            ebay_result = cursor.fetchone()
            
            if ebay_result:
                order_id = ebay_result['order_id']
                
                # Mark shipping cost as matched (use order_id as reference)
                cursor.execute("""
                    UPDATE shipping_costs
                    SET matched = 1
                    WHERE tracking_number = ?
                """, (tracking,))
                
                # Count how many sales share this order (for multi-item orders)
                cursor.execute("""
                    SELECT COUNT(*) as count FROM sales
                    WHERE platform = 'eBay' AND order_number = ?
                """, (order_id,))
                count_result = cursor.fetchone()
                num_items = count_result['count'] if count_result else 1
                
                # Distribute shipping cost across items in order
                cost_per_item = cost / num_items if num_items > 0 else cost
                
                # Update sales.shipping_cost for this order
                cursor.execute("""
                    UPDATE sales
                    SET shipping_cost = ?,
                        net_profit = net_profit - ?
                    WHERE platform = 'eBay' 
                    AND order_number = ?
                    AND (shipping_cost = 0 OR shipping_cost IS NULL)
                """, (cost_per_item, cost_per_item, order_id))
                
                stats['sales_updated'] += cursor.rowcount
                stats['matched'] += 1
                stats['matched_ebay'] += 1
                matched = True
        
        # 3. Try to match to trades (Trade shipments)
        if not matched:
            cursor.execute("""
                SELECT trade_id FROM trades
                WHERE tracking_number = ?
            """, (tracking,))
            
            trade_result = cursor.fetchone()
            
            if trade_result:
                trade_id = trade_result['trade_id']
                
                # Mark shipping cost as matched
                cursor.execute("""
                    UPDATE shipping_costs
                    SET matched = 1
                    WHERE tracking_number = ?
                """, (tracking,))
                
                # Update trade with shipping cost
                cursor.execute("""
                    UPDATE trades SET shipping_cost = ?
                    WHERE trade_id = ?
                """, (cost, trade_id))
                
                # Get GIVE lines to allocate shipping proportionally
                cursor.execute("""
                    SELECT tl.line_id, tl.value, tl.sale_id
                    FROM trade_lines tl
                    WHERE tl.trade_id = ? AND tl.direction = 'GIVE' AND tl.line_type = 'inventory'
                """, (trade_id,))
                give_lines = cursor.fetchall()
                
                total_give_value = sum(line['value'] for line in give_lines)
                
                for line in give_lines:
                    if line['sale_id'] and total_give_value > 0:
                        line_shipping = (line['value'] / total_give_value) * cost
                        cursor.execute("""
                            UPDATE sales 
                            SET shipping_cost = ?,
                                net_profit = net_profit - ?
                            WHERE sale_id = ?
                            AND (shipping_cost = 0 OR shipping_cost IS NULL)
                        """, (line_shipping, line_shipping, line['sale_id']))
                        stats['sales_updated'] += cursor.rowcount
                
                stats['matched'] += 1
                stats['matched_trades'] += 1
                matched = True
        
        if not matched:
            stats['unmatched'] += 1
    
    conn.commit()
    conn.close()
    return stats

def manual_link_shipping_to_order(shipping_id, fulfillment_id):
    """
    Manually link an unmatched shipping cost to an order.
    Used for edge cases like split shipments (one order, multiple packages).
    
    This function:
    1. Links shipping_costs record to the order_fulfillment record
    2. Finds the corresponding sale and ADDS this shipping cost to existing
    3. Recalculates net_profit
    
    Args:
        shipping_id: ID from shipping_costs table
        fulfillment_id: ID from order_fulfillment table
    
    Returns:
        dict with success status and message
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get the shipping cost details
        cursor.execute("SELECT cost, tracking_number FROM shipping_costs WHERE shipping_id = ?", (shipping_id,))
        ship_result = cursor.fetchone()
        if not ship_result:
            conn.close()
            return {'success': False, 'message': 'Shipping record not found'}
        
        cost = ship_result['cost']
        
        # Get the order details
        cursor.execute("SELECT source_order_id, source FROM order_fulfillment WHERE fulfillment_id = ?", (fulfillment_id,))
        order_result = cursor.fetchone()
        if not order_result:
            conn.close()
            return {'success': False, 'message': 'Order not found'}
        
        source_order_id = order_result['source_order_id']
        source = order_result['source']
        
        # Mark shipping cost as matched and link to order
        cursor.execute("""
            UPDATE shipping_costs
            SET fulfillment_id = ?, matched = 1
            WHERE shipping_id = ?
        """, (fulfillment_id, shipping_id))
        
        # Update the sales record - ADD to existing shipping_cost (for split shipments)
        # Determine platform name for sales table
        platform = 'Stripe' if source == 'Stripe' else source
        
        cursor.execute("""
            UPDATE sales
            SET shipping_cost = COALESCE(shipping_cost, 0) + ?,
                net_profit = net_profit - ?
            WHERE platform = ? 
            AND order_number = ?
        """, (cost, cost, platform, source_order_id))
        
        sales_updated = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        return {
            'success': True, 
            'message': f'Linked ${cost:.2f} shipping to order. Sales updated: {sales_updated}',
            'sales_updated': sales_updated
        }
        
    except Exception as e:
        conn.close()
        return {'success': False, 'message': str(e)}

def manual_link_shipping_to_grading_batch(shipping_id, batch_id):
    """
    Manually link an unmatched shipping cost to a grading batch.
    Used for shipments to grading companies (TAG, PSA, etc.).
    
    This function:
    1. Marks shipping_costs record as matched
    2. ADDS this shipping cost to the batch's existing shipping_cost
    3. Recalculates batch total_cost and cost_per_card
    
    Args:
        shipping_id: ID from shipping_costs table
        batch_id: ID from grading_batches table
    
    Returns:
        dict with success status and message
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get the shipping cost details
        cursor.execute("SELECT cost, tracking_number FROM shipping_costs WHERE shipping_id = ?", (shipping_id,))
        ship_result = cursor.fetchone()
        if not ship_result:
            conn.close()
            return {'success': False, 'message': 'Shipping record not found'}
        
        cost = ship_result['cost']
        
        # Get the batch details
        cursor.execute("SELECT batch_name, shipping_cost, grading_fee, card_count FROM grading_batches WHERE batch_id = ?", (batch_id,))
        batch_result = cursor.fetchone()
        if not batch_result:
            conn.close()
            return {'success': False, 'message': 'Grading batch not found'}
        
        batch_name = batch_result['batch_name']
        current_shipping = batch_result['shipping_cost'] or 0
        grading_fee = batch_result['grading_fee'] or 0
        card_count = batch_result['card_count'] or 0
        
        # Calculate new totals
        new_shipping = current_shipping + cost
        new_total = grading_fee + new_shipping
        new_cost_per_card = new_total / card_count if card_count > 0 else 0
        
        # Mark shipping cost as matched
        cursor.execute("""
            UPDATE shipping_costs
            SET matched = 1
            WHERE shipping_id = ?
        """, (shipping_id,))
        
        # Update the grading batch
        cursor.execute("""
            UPDATE grading_batches
            SET shipping_cost = ?,
                total_cost = ?,
                cost_per_card = ?
            WHERE batch_id = ?
        """, (new_shipping, new_total, new_cost_per_card, batch_id))
        
        conn.commit()
        conn.close()
        
        return {
            'success': True, 
            'message': f'Linked ${cost:.2f} shipping to batch "{batch_name}". New total: ${new_total:.2f}'
        }
        
    except Exception as e:
        conn.close()
        return {'success': False, 'message': str(e)}

def get_grading_batches_for_linking():
    """
    Get list of grading batches for manual shipping cost linking dropdown.
    Returns recent batches with name and submission date for identification.
    """
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            batch_id,
            batch_name,
            grader,
            submission_date,
            status,
            shipping_cost,
            total_cost,
            card_count
        FROM grading_batches
        ORDER BY submission_date DESC
        LIMIT 30
    """)
    conn.close()
    return df

def get_shipped_orders_for_linking(source_filter=None):
    """
    Get list of shipped orders for manual shipping cost linking dropdown.
    Returns recent shipped orders with customer name and tracking for identification.
    
    Args:
        source_filter: Optional filter by source (e.g., 'Stripe', 'PayPal', 'Venmo')
                      If None or 'All', returns all sources
    """
    conn = get_connection()
    
    if source_filter and source_filter != 'All':
        df = _read_sql(conn, """
            SELECT 
                fulfillment_id,
                source,
                source_order_id,
                customer_name,
                tracking_number,
                ship_date,
                order_total,
                item_description
            FROM order_fulfillment
            WHERE fulfillment_status = 'Shipped'
            AND source = ?
            ORDER BY ship_date DESC
            LIMIT 50
        """,  params=(source_filter,))
    else:
        df = _read_sql(conn, """
            SELECT 
                fulfillment_id,
                source,
                source_order_id,
                customer_name,
                tracking_number,
                ship_date,
                order_total,
                item_description
            FROM order_fulfillment
            WHERE fulfillment_status = 'Shipped'
            ORDER BY ship_date DESC
            LIMIT 50
        """)
    
    conn.close()
    return df


def get_order_sources():
    """
    Get list of distinct sources from order_fulfillment table.
    Used to populate source filter dropdown.
    """
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT DISTINCT source 
        FROM order_fulfillment 
        WHERE fulfillment_status = 'Shipped'
        ORDER BY source
    """)
    conn.close()
    return df['source'].tolist()


def get_sales_platforms():
    """
    Get list of distinct platforms from sales table.
    Used to populate platform filter dropdown for shipping linking.
    """
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT DISTINCT platform 
        FROM sales 
        ORDER BY 
            CASE platform
                WHEN 'eBay' THEN 1
                WHEN 'Stripe' THEN 2
                WHEN 'PayPal' THEN 3
                WHEN 'Venmo' THEN 4
                ELSE 5
            END
    """)
    conn.close()
    return df['platform'].tolist()


def get_sales_orders_for_shipping_linking(platform_filter=None):
    """
    Get list of sales orders (grouped by order_number) for manual shipping cost linking.
    Returns orders with total value, item count, and customer info for identification.
    
    Args:
        platform_filter: Optional filter by platform (e.g., 'Stripe', 'PayPal', 'Venmo', 'eBay')
                        If None or 'All', returns all platforms
    
    Returns:
        DataFrame with columns: platform, order_number, sale_date, customer_name, 
                               order_total, item_count, current_shipping, item_titles
    """
    conn = get_connection()
    
    base_query = """
        SELECT 
            platform,
            order_number,
            MAX(sale_date) as sale_date,
            COALESCE(MAX(customer_name), MAX(item_title)) as customer_name,
            SUM(sale_price) as order_total,
            COUNT(*) as item_count,
            SUM(COALESCE(shipping_cost, 0)) as current_shipping,
            GROUP_CONCAT(item_title, ' | ') as item_titles
        FROM sales
        WHERE order_number IS NOT NULL AND order_number != ''
        {where_clause}
        GROUP BY platform, order_number
        ORDER BY sale_date DESC
        LIMIT 100
    """
    
    if platform_filter and platform_filter != 'All':
        query = base_query.format(where_clause="AND platform = ?")
        df = _read_sql(conn, query,  params=(platform_filter,))
    else:
        query = base_query.format(where_clause="")
        df = _read_sql(conn, query)
    
    conn.close()
    return df


def manual_link_shipping_to_sales_order(shipping_id, platform, order_number):
    """
    Manually link an unmatched shipping cost to a sales order.
    Distributes shipping cost proportionally across all line items in the order
    based on their sale_price.
    
    This function:
    1. Marks shipping_costs record as matched (sets matched=1, stores order ref in notes)
    2. Distributes shipping cost proportionally across all line items
    3. ADDS to existing shipping_cost (for split shipments/multiple packages)
    4. Recalculates net_profit for each line item
    
    Args:
        shipping_id: ID from shipping_costs table
        platform: Platform from sales table (e.g., 'PayPal', 'Venmo', 'Stripe')
        order_number: Order number from sales table
    
    Returns:
        dict with success status and message
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get the shipping cost details
        cursor.execute("SELECT cost, tracking_number FROM shipping_costs WHERE shipping_id = ?", (shipping_id,))
        ship_result = cursor.fetchone()
        if not ship_result:
            conn.close()
            return {'success': False, 'message': 'Shipping record not found'}
        
        cost = ship_result['cost']
        tracking = ship_result['tracking_number']
        
        # Get all line items for this order
        cursor.execute("""
            SELECT sale_id, sale_price, shipping_cost, net_profit
            FROM sales
            WHERE platform = ? AND order_number = ?
        """, (platform, order_number))
        
        line_items = cursor.fetchall()
        if not line_items:
            conn.close()
            return {'success': False, 'message': f'No sales found for {platform} order {order_number}'}
        
        # Calculate total order value for proportional distribution
        total_order_value = sum(item['sale_price'] or 0 for item in line_items)
        
        if total_order_value == 0:
            conn.close()
            return {'success': False, 'message': 'Order has zero total value, cannot distribute shipping'}
        
        # Distribute shipping cost proportionally and update each line item
        items_updated = 0
        for item in line_items:
            # Calculate this item's share of shipping
            proportion = (item['sale_price'] or 0) / total_order_value
            item_shipping = round(cost * proportion, 2)
            
            # Update the sale record - ADD to existing shipping_cost
            cursor.execute("""
                UPDATE sales
                SET shipping_cost = COALESCE(shipping_cost, 0) + ?,
                    net_profit = COALESCE(net_profit, 0) - ?
                WHERE sale_id = ?
            """, (item_shipping, item_shipping, item['sale_id']))
            
            items_updated += cursor.rowcount
        
        # Mark shipping cost as matched and store reference
        cursor.execute("""
            UPDATE shipping_costs
            SET matched = 1,
                recipient = COALESCE(recipient, '') || ' [Linked to ' || ? || ' #' || ? || ']'
            WHERE shipping_id = ?
        """, (platform, order_number, shipping_id))
        
        conn.commit()
        conn.close()
        
        return {
            'success': True,
            'message': f'Linked ${cost:.2f} shipping to {platform} #{order_number}. '
                      f'Distributed across {len(line_items)} item(s).',
            'items_updated': items_updated,
            'line_items': len(line_items)
        }
        
    except Exception as e:
        conn.close()
        return {'success': False, 'message': str(e)}


def get_shipping_cost_by_tracking(tracking_number):
    """Get shipping cost by tracking number"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM shipping_costs
        WHERE tracking_number = ?
    """, (tracking_number,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

def get_shipping_summary():
    """Get summary statistics for shipping costs"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            COUNT(*) as total_shipments,
            SUM(cost) as total_cost,
            AVG(cost) as avg_cost,
            SUM(CASE WHEN matched = 1 THEN 1 ELSE 0 END) as matched_count,
            SUM(CASE WHEN matched = 0 THEN 1 ELSE 0 END) as unmatched_count
        FROM shipping_costs
    """)
    
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None

# ============================================================================
# STRIPE LINE ITEMS & ORDER TOTALS
# ============================================================================

def add_stripe_line_item(line_item_data):
    """
    Add a Stripe line item from checkout session
    
    Args:
        line_item_data: Dict with line item fields
    
    Returns:
        True if inserted, False if duplicate or error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO stripe_line_items (
                line_item_id, payment_intent_id, checkout_session_id,
                stripe_product_id, product_name, description,
                quantity, unit_amount, amount_subtotal, amount_total,
                amount_discount, amount_tax, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(line_item_id) DO UPDATE SET
                amount_total = excluded.amount_total,
                amount_discount = excluded.amount_discount,
                amount_tax = excluded.amount_tax
        """, (
            line_item_data.get('line_item_id'),
            line_item_data.get('payment_intent_id'),
            line_item_data.get('checkout_session_id'),
            line_item_data.get('stripe_product_id'),
            line_item_data.get('product_name'),
            line_item_data.get('description'),
            line_item_data.get('quantity', 1),
            line_item_data.get('unit_amount', 0),
            line_item_data.get('amount_subtotal', 0),
            line_item_data.get('amount_total', 0),
            line_item_data.get('amount_discount', 0),
            line_item_data.get('amount_tax', 0),
            line_item_data.get('currency', 'usd')
        ))
        conn.commit()
        success = cursor.rowcount > 0
    except Exception as e:
        print(f"Error adding Stripe line item: {e}")
        success = False
    finally:
        conn.close()
    
    return success


def add_stripe_order_totals(order_data):
    """
    Add Stripe order totals for accounting
    
    Args:
        order_data: Dict with order total fields
    
    Returns:
        True if inserted, False if duplicate or error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO stripe_order_totals (
                payment_intent_id, checkout_session_id,
                amount_subtotal, amount_total, amount_discount,
                amount_shipping, amount_tax, stripe_fee, net_amount,
                currency, customer_name, customer_email,
                shipping_name, shipping_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(payment_intent_id) DO UPDATE SET
                amount_total = excluded.amount_total,
                amount_discount = excluded.amount_discount,
                amount_tax = excluded.amount_tax,
                stripe_fee = excluded.stripe_fee,
                net_amount = excluded.net_amount
        """, (
            order_data.get('payment_intent_id'),
            order_data.get('checkout_session_id'),
            order_data.get('amount_subtotal', 0),
            order_data.get('amount_total', 0),
            order_data.get('amount_discount', 0),
            order_data.get('amount_shipping', 0),
            order_data.get('amount_tax', 0),
            order_data.get('stripe_fee', 0),
            order_data.get('net_amount', 0),
            order_data.get('currency', 'usd'),
            order_data.get('customer_name'),
            order_data.get('customer_email'),
            order_data.get('shipping_name'),
            order_data.get('shipping_address')
        ))
        conn.commit()
        success = cursor.rowcount > 0
    except Exception as e:
        print(f"Error adding Stripe order totals: {e}")
        success = False
    finally:
        conn.close()
    
    return success


def get_stripe_line_items(payment_intent_id):
    """Get line items for a Stripe payment"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM stripe_line_items
        WHERE payment_intent_id = ?
        ORDER BY line_item_id
    """,  params=(payment_intent_id,))
    conn.close()
    return df


def get_stripe_order_totals(payment_intent_id):
    """Get order totals for a Stripe payment"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM stripe_order_totals
        WHERE payment_intent_id = ?
    """, (payment_intent_id,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None


def add_stripe_product_mapping(stripe_product_id, purchase_id, product_name=None, default_sku=None):
    """
    Map a Stripe product ID to a purchase ID
    
    Args:
        stripe_product_id: Stripe product ID (prod_xxx)
        purchase_id: Your internal purchase ID
        product_name: Product name for reference
        default_sku: Default SKU to use
    
    Returns:
        True if saved
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO stripe_product_mapping (
            stripe_product_id, purchase_id, product_name, default_sku, updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(stripe_product_id) DO UPDATE SET
            purchase_id = excluded.purchase_id,
            product_name = COALESCE(excluded.product_name, stripe_product_mapping.product_name),
            default_sku = COALESCE(excluded.default_sku, stripe_product_mapping.default_sku),
            updated_at = CURRENT_TIMESTAMP
    """, (stripe_product_id, purchase_id, product_name, default_sku))
    
    conn.commit()
    conn.close()
    return True


def get_stripe_product_mapping(stripe_product_id):
    """Get purchase ID mapping for a Stripe product"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM stripe_product_mapping
        WHERE stripe_product_id = ?
    """, (stripe_product_id,))
    result = cursor.fetchone()
    conn.close()
    return _row_to_dict(result, cursor) if result else None


def get_all_stripe_product_mappings():
    """Get all Stripe product mappings"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT spm.*, p.display_name, p.description as purchase_description
        FROM stripe_product_mapping spm
        LEFT JOIN purchases p ON spm.purchase_id = p.purchase_id
        ORDER BY spm.product_name
    """)
    conn.close()
    return df


def discover_stripe_products_from_line_items():
    """
    Discover Stripe products from existing line items and add to mapping table.
    Useful for populating mappings from already-imported data.
    
    Returns:
        Dict with discovery stats
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {
        'products_found': 0,
        'products_added': 0,
        'products_already_mapped': 0
    }
    
    try:
        # Get unique products from line items
        cursor.execute("""
            SELECT DISTINCT stripe_product_id, product_name
            FROM stripe_line_items
            WHERE stripe_product_id IS NOT NULL AND stripe_product_id != ''
        """)
        
        products = cursor.fetchall()
        stats['products_found'] = len(products)
        
        for product in products:
            product_id = product['stripe_product_id']
            product_name = product['product_name']
            
            # Check if already in mapping
            cursor.execute("""
                SELECT 1 FROM stripe_product_mapping
                WHERE stripe_product_id = ?
            """, (product_id,))
            
            if cursor.fetchone():
                stats['products_already_mapped'] += 1
            else:
                # Add to mapping with UNKNOWN purchase_id
                cursor.execute("""
                    INSERT INTO stripe_product_mapping (stripe_product_id, purchase_id, product_name)
                    VALUES (?, 'UNKNOWN', ?)
                """, (product_id, product_name))
                stats['products_added'] += 1
        
        conn.commit()
        
    except Exception as e:
        stats['error'] = str(e)
        conn.rollback()
    finally:
        conn.close()
    
    return stats


# ============================================================================
# PHASE 3A: EBAY API QUERIES
# ============================================================================

def add_ebay_order(order_data):
    """
    Add or update an eBay order from API
    
    Args:
        order_data: Dict with order fields from parse_order()
    
    Returns:
        True if inserted/updated, False if error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO ebay_orders (
                order_id, order_number, legacy_order_id, created_date,
                buyer_username, buyer_name, ship_to_name,
                ship_to_address1, ship_to_address2, ship_to_city,
                ship_to_state, ship_to_zip, ship_to_country,
                order_status, order_total, last_synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(order_id) DO UPDATE SET
                order_status = excluded.order_status,
                order_total = excluded.order_total,
                last_synced = CURRENT_TIMESTAMP
        """, (
            order_data.get('order_id'),
            order_data.get('order_number'),
            order_data.get('legacy_order_id'),
            order_data.get('created_date'),
            order_data.get('buyer_username'),
            order_data.get('buyer_name'),
            order_data.get('ship_to_name'),
            order_data.get('ship_to_address1'),
            order_data.get('ship_to_address2'),
            order_data.get('ship_to_city'),
            order_data.get('ship_to_state'),
            order_data.get('ship_to_zip'),
            order_data.get('ship_to_country'),
            order_data.get('order_status'),
            order_data.get('order_total')
        ))
        conn.commit()
        success = True
    except Exception as e:
        print(f"Error adding eBay order: {e}")
        success = False
    finally:
        conn.close()
    
    return success


def add_ebay_line_item(item_data):
    """
    Add or update an eBay line item
    
    Args:
        item_data: Dict with line item fields
    
    Returns:
        True if inserted/updated, False if error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO ebay_line_items (
                line_item_id, order_id, item_id, item_title,
                sku, quantity, unit_price, line_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(line_item_id) DO UPDATE SET
                item_title = excluded.item_title,
                sku = excluded.sku,
                quantity = excluded.quantity,
                unit_price = excluded.unit_price,
                line_total = excluded.line_total
        """, (
            item_data.get('line_item_id'),
            item_data.get('order_id'),
            item_data.get('item_id'),
            item_data.get('item_title'),
            item_data.get('sku'),
            item_data.get('quantity'),
            item_data.get('unit_price'),
            item_data.get('line_total')
        ))
        conn.commit()
        success = True
    except Exception as e:
        print(f"Error adding eBay line item: {e}")
        success = False
    finally:
        conn.close()
    
    return success


def add_ebay_fulfillment(fulfillment_data):
    """
    Add or update an eBay fulfillment (shipping/tracking)
    
    Args:
        fulfillment_data: Dict with fulfillment fields
    
    Returns:
        True if inserted/updated, False if error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO ebay_fulfillments (
                fulfillment_id, order_id, tracking_number, carrier, ship_date
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fulfillment_id) DO UPDATE SET
                tracking_number = excluded.tracking_number,
                carrier = excluded.carrier,
                ship_date = excluded.ship_date
        """, (
            fulfillment_data.get('fulfillment_id'),
            fulfillment_data.get('order_id'),
            fulfillment_data.get('tracking_number'),
            fulfillment_data.get('carrier'),
            fulfillment_data.get('ship_date')
        ))
        conn.commit()
        success = True
    except Exception as e:
        print(f"Error adding eBay fulfillment: {e}")
        success = False
    finally:
        conn.close()
    
    return success


def add_ebay_fee(fee_data):
    """
    Add an eBay fee transaction
    
    Args:
        fee_data: Dict with fee fields
    
    Returns:
        True if inserted, False if duplicate or error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO ebay_fees (
                order_id, transaction_id, fee_type, amount,
                transaction_date, payout_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                amount = excluded.amount
        """, (
            fee_data.get('order_id'),
            fee_data.get('transaction_id'),
            fee_data.get('fee_type'),
            fee_data.get('amount'),
            fee_data.get('transaction_date'),
            fee_data.get('payout_id')
        ))
        conn.commit()
        success = True
    except Exception as e:
        print(f"Error adding eBay fee: {e}")
        success = False
    finally:
        conn.close()
    
    return success




def add_ebay_transaction_from_api(transaction: dict) -> bool:
    """
    Add a transaction record from eBay Finances API to ebay_transactions table
    
    Args:
        transaction: Dict with transaction data from API
        
    Returns:
        bool: True if inserted, False if duplicate or error
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Check for duplicate (same transaction_id and type)
        cursor.execute("""
            SELECT id FROM ebay_transactions 
            WHERE transaction_id = ? AND type = ?
        """, (transaction.get('transaction_id'), transaction.get('type')))
        
        if cursor.fetchone():
            conn.close()
            return False  # Already exists
        
        # Insert transaction
        cursor.execute("""
            INSERT INTO ebay_transactions (
                transaction_date, payout_date, type, order_number, transaction_id,
                payout_id, gross_transaction_amount, net_amount, 
                transaction_currency, reference_id, description,
                import_date, import_batch, processed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            transaction.get('transaction_date'),
            transaction.get('payout_date'),
            transaction.get('type'),
            transaction.get('order_number'),
            transaction.get('transaction_id'),
            transaction.get('payout_id'),
            transaction.get('gross_transaction_amount'),
            transaction.get('net_amount'),
            transaction.get('transaction_currency'),
            transaction.get('reference_id'),
            transaction.get('description'),
            datetime.now().isoformat(),
            'API_SYNC',
            0  # Not processed yet
        ))
        
        conn.commit()
        conn.close()
        return True
        
    except Exception as e:
        print(f"Error adding transaction from API: {e}")
        conn.close()
        return False


def get_ebay_orders(limit=100, status=None):
    """
    Get recent eBay orders from API sync
    
    Args:
        limit: Maximum orders to return
        status: Filter by order status (optional)
    
    Returns:
        DataFrame with orders
    """
    conn = get_connection()
    
    if status:
        df = _read_sql(conn, """
            SELECT o.*,
                   (SELECT COUNT(*) FROM ebay_line_items WHERE order_id = o.order_id) as item_count,
                   (SELECT SUM(amount) FROM ebay_fees WHERE order_id = o.order_id) as total_fees
            FROM ebay_orders o
            WHERE o.order_status = ?
            ORDER BY o.created_date DESC
            LIMIT ?
        """,  params=(status, limit))
    else:
        df = _read_sql(conn, """
            SELECT o.*,
                   (SELECT COUNT(*) FROM ebay_line_items WHERE order_id = o.order_id) as item_count,
                   (SELECT SUM(amount) FROM ebay_fees WHERE order_id = o.order_id) as total_fees
            FROM ebay_orders o
            ORDER BY o.created_date DESC
            LIMIT ?
        """,  params=(limit,))
    
    conn.close()
    return df


def get_ebay_order_detail(order_id):
    """
    Get complete details for a single eBay order
    
    Args:
        order_id: eBay order ID
    
    Returns:
        Dict with order, line_items, fulfillments, and fees
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get order
    cursor.execute("SELECT * FROM ebay_orders WHERE order_id = ?", (order_id,))
    order_row = cursor.fetchone()
    
    if not order_row:
        conn.close()
        return None
    
    order = dict(order_row)
    
    # Get line items
    line_items_df = _read_sql(conn, """
        SELECT * FROM ebay_line_items WHERE order_id = ?
    """,  params=(order_id,))
    
    # Get fulfillments
    fulfillments_df = _read_sql(conn, """
        SELECT * FROM ebay_fulfillments WHERE order_id = ?
    """,  params=(order_id,))
    
    # Get fees
    fees_df = _read_sql(conn, """
        SELECT * FROM ebay_fees WHERE order_id = ?
    """,  params=(order_id,))
    
    conn.close()
    
    return {
        'order': order,
        'line_items': line_items_df.to_dict('records'),
        'fulfillments': fulfillments_df.to_dict('records'),
        'fees': fees_df.to_dict('records'),
        'total_fees': fees_df['amount'].sum() if not fees_df.empty else 0
    }


def get_ebay_line_items_by_order(order_id):
    """Get all line items for an order"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM ebay_line_items
        WHERE order_id = ?
        ORDER BY line_item_id
    """,  params=(order_id,))
    conn.close()
    return df


def get_ebay_fulfillments_by_order(order_id):
    """Get all fulfillments (tracking) for an order"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM ebay_fulfillments
        WHERE order_id = ?
        ORDER BY ship_date DESC
    """,  params=(order_id,))
    conn.close()
    return df


def get_ebay_fees_by_order(order_id):
    """Get all fees for an order"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM ebay_fees
        WHERE order_id = ?
        ORDER BY transaction_date DESC
    """,  params=(order_id,))
    conn.close()
    return df


def get_ebay_sync_status():
    """
    Get status information about eBay API sync
    
    Returns:
        Dict with sync statistics
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get last sync timestamp from settings
    last_sync = get_setting('ebay_last_sync')
    environment = get_setting('ebay_environment') or 'sandbox'
    
    # Count orders
    cursor.execute("SELECT COUNT(*) as count FROM ebay_orders")
    order_count = cursor.fetchone()[0]
    
    # Count line items
    cursor.execute("SELECT COUNT(*) as count FROM ebay_line_items")
    line_item_count = cursor.fetchone()[0]
    
    # Count fulfillments
    cursor.execute("SELECT COUNT(*) as count FROM ebay_fulfillments")
    fulfillment_count = cursor.fetchone()[0]
    
    # Count fees
    cursor.execute("SELECT COUNT(*) as count FROM ebay_fees")
    fee_count = cursor.fetchone()[0]
    
    # Get date range
    cursor.execute("""
        SELECT MIN(created_date) as earliest, MAX(created_date) as latest
        FROM ebay_orders
    """)
    date_range = cursor.fetchone()
    
    conn.close()
    
    return {
        'last_sync': last_sync,
        'environment': environment,
        'order_count': order_count,
        'line_item_count': line_item_count,
        'fulfillment_count': fulfillment_count,
        'fee_count': fee_count,
        'earliest_order': date_range['earliest'] if date_range else None,
        'latest_order': date_range['latest'] if date_range else None
    }


def get_ebay_orders_summary(days=30):
    """
    Get summary statistics for recent eBay orders
    
    Args:
        days: Number of days to look back
    
    Returns:
        Dict with summary stats
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            COUNT(*) as order_count,
            SUM(order_total) as total_revenue,
            AVG(order_total) as avg_order_value
        FROM ebay_orders
        WHERE created_date >= date('now', ?)
    """, (f'-{days} days',))
    
    summary = cursor.fetchone()
    
    # Get fees total
    cursor.execute("""
        SELECT SUM(f.amount) as total_fees
        FROM ebay_fees f
        JOIN ebay_orders o ON f.order_id = o.order_id
        WHERE o.created_date >= date('now', ?)
    """, (f'-{days} days',))
    
    fees = cursor.fetchone()
    
    conn.close()
    
    return {
        'order_count': summary['order_count'] or 0,
        'total_revenue': summary['total_revenue'] or 0,
        'avg_order_value': summary['avg_order_value'] or 0,
        'total_fees': fees['total_fees'] or 0
    }


def clear_ebay_api_data():
    """
    Clear all eBay API data (for re-sync or testing)
    
    Returns:
        Dict with counts of deleted records
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Count before deleting
    cursor.execute("SELECT COUNT(*) FROM ebay_orders")
    orders = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM ebay_line_items")
    items = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM ebay_fulfillments")
    fulfillments = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM ebay_fees")
    fees = cursor.fetchone()[0]
    
    # Delete in order (respecting foreign keys)
    cursor.execute("DELETE FROM ebay_fees")
    cursor.execute("DELETE FROM ebay_fulfillments")
    cursor.execute("DELETE FROM ebay_line_items")
    cursor.execute("DELETE FROM ebay_orders")
    
    conn.commit()
    conn.close()
    
    return {
        'orders_deleted': orders,
        'line_items_deleted': items,
        'fulfillments_deleted': fulfillments,
        'fees_deleted': fees
    }


def search_ebay_orders(search_term, limit=50):
    """
    Search eBay orders by buyer username, order number, or item title
    
    Args:
        search_term: Text to search for
        limit: Maximum results
    
    Returns:
        DataFrame with matching orders
    """
    conn = get_connection()
    
    search_pattern = f"%{search_term}%"
    
    df = _read_sql(conn, """
        SELECT DISTINCT o.*
        FROM ebay_orders o
        LEFT JOIN ebay_line_items li ON o.order_id = li.order_id
        WHERE o.buyer_username LIKE ?
           OR o.order_number LIKE ?
           OR o.order_id LIKE ?
           OR li.item_title LIKE ?
           OR li.sku LIKE ?
        ORDER BY o.created_date DESC
        LIMIT ?
    """,  params=(search_pattern, search_pattern, search_pattern, 
                       search_pattern, search_pattern, limit))
    
    conn.close()
    return df


# Initialize database when module is imported
if __name__ == "__main__":
    init_database()


def migrate_database():
    """
    Run database migrations to add new columns to existing tables.
    Safe to run multiple times - checks if columns exist first.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    migrations = []
    
    # Check and add columns to purchases table
    cursor.execute("PRAGMA table_info(purchases)")
    purchase_cols = [col[1] for col in cursor.fetchall()]
    
    if 'created_at' not in purchase_cols:
        migrations.append(("purchases", "created_at", "ALTER TABLE purchases ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
    if 'updated_at' not in purchase_cols:
        migrations.append(("purchases", "updated_at", "ALTER TABLE purchases ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
    
    # Check and add columns to sales table
    cursor.execute("PRAGMA table_info(sales)")
    sales_cols = [col[1] for col in cursor.fetchall()]
    
    if 'created_at' not in sales_cols:
        migrations.append(("sales", "created_at", "ALTER TABLE sales ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
    if 'updated_at' not in sales_cols:
        migrations.append(("sales", "updated_at", "ALTER TABLE sales ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
    
    # Check and add columns to chase_transactions table
    cursor.execute("PRAGMA table_info(chase_transactions)")
    chase_cols = [col[1] for col in cursor.fetchall()]
    
    if 'skip_reason' not in chase_cols:
        migrations.append(("chase_transactions", "skip_reason", "ALTER TABLE chase_transactions ADD COLUMN skip_reason TEXT"))
    if 'updated_at' not in chase_cols:
        migrations.append(("chase_transactions", "updated_at", "ALTER TABLE chase_transactions ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
    
    # Run migrations
    for table, column, sql in migrations:
        try:
            cursor.execute(sql)
            print(f"Added column '{column}' to '{table}'")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"Migration warning for {table}.{column}: {e}")
    
    conn.commit()
    conn.close()
    print("Database migrations complete")


def create_backup(backup_dir=None):
    """
    Create a timestamped backup of the database.
    Returns: Path to backup file, or None if failed
    """
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'backups')
    
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filename = f"taclaco_{timestamp}.db"
    backup_path = os.path.join(backup_dir, backup_filename)
    
    # Backup uses local SQLite directly. When running on Turso the local
    # DB_PATH file is the import copy used by run_monthly_import.py; the
    # authoritative data lives in Turso and does not need a local backup here.
    try:
        source_conn = sqlite3.connect(DB_PATH)
        dest_conn = sqlite3.connect(backup_path)
        source_conn.backup(dest_conn)
        source_conn.close()
        dest_conn.close()
        print(f"Backup created: {backup_path}")
        return backup_path
    except Exception as e:
        print(f"Backup failed: {e}")
        return None


def list_backups(backup_dir=None):
    """
    List all available database backups.
    Returns: List of (filename, size_kb, modified_date) tuples, sorted newest first
    """
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'backups')
    
    if not os.path.exists(backup_dir):
        return []
    
    backups = []
    for filename in os.listdir(backup_dir):
        if filename.endswith('.db') and filename.startswith('taclaco_'):
            filepath = os.path.join(backup_dir, filename)
            size_kb = os.path.getsize(filepath) / 1024
            modified = datetime.fromtimestamp(os.path.getmtime(filepath))
            backups.append((filename, size_kb, modified))
    
    backups.sort(key=lambda x: x[2], reverse=True)
    return backups


def validate_purchase_id(purchase_id):
    """
    Validate purchase ID format.
    Returns: (is_valid: bool, error_message: str or None)
    """
    if not purchase_id or not purchase_id.strip():
        return False, "Purchase ID cannot be empty"
    
    pid = purchase_id.strip().upper()
    
    if pid in ['PC', 'UNKNOWN', 'N/A']:
        return True, None
    
    if '-' in pid and len(pid) <= 10:
        return True, None
    
    if len(pid) == 6 and pid.isdigit():
        month = int(pid[2:4])
        day = int(pid[4:6])
        if month < 1 or month > 12:
            return False, f"Invalid month: {month} (must be 01-12)"
        if day < 1 or day > 31:
            return False, f"Invalid day: {day} (must be 01-31)"
        return True, None
    
    if len(pid) == 5 and pid[0:4].isdigit() and pid[4].isalpha():
        month = int(pid[2:4])
        if month < 1 or month > 12:
            return False, f"Invalid month: {month}"
        return True, None
    
    if len(pid) <= 4:
        return True, None
    
    return False, f"Unrecognized format: {purchase_id}"


def get_skipped_chase_transactions():
    """Get Chase transactions that were skipped (payments, returns, etc.)"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT * FROM chase_transactions
        WHERE status = 'Skipped'
        ORDER BY transaction_date DESC
    """)
    conn.close()
    return df


# ============================================================================
# TRADES QUERIES
# ============================================================================

def get_all_trades():
    """Get all trades with summary info"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            t.trade_id,
            t.trade_date,
            t.tracking_number,
            t.shipping_cost,
            t.notes,
            t.created_at,
            COALESCE(give.total_value, 0) as give_value,
            COALESCE(receive.total_value, 0) as receive_value,
            COALESCE(give.line_count, 0) as give_lines,
            COALESCE(receive.line_count, 0) as receive_lines
        FROM trades t
        LEFT JOIN (
            SELECT trade_id, SUM(value) as total_value, COUNT(*) as line_count
            FROM trade_lines WHERE direction = 'GIVE'
            GROUP BY trade_id
        ) give ON t.trade_id = give.trade_id
        LEFT JOIN (
            SELECT trade_id, SUM(value) as total_value, COUNT(*) as line_count
            FROM trade_lines WHERE direction = 'RECEIVE'
            GROUP BY trade_id
        ) receive ON t.trade_id = receive.trade_id
        ORDER BY t.trade_date DESC
    """)
    conn.close()
    return df


def get_trade_details(trade_id):
    """Get a single trade with all its lines"""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get trade header
    cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    trade = cursor.fetchone()
    
    if not trade:
        conn.close()
        return None
    
    # Get trade lines
    df_lines = _read_sql(conn, """
        SELECT 
            tl.*,
            p.display_name as purchase_name,
            gc.card_name as graded_card_name,
            gc.cert_number,
            gc.grade
        FROM trade_lines tl
        LEFT JOIN purchases p ON tl.purchase_id = p.purchase_id
        LEFT JOIN graded_cards gc ON tl.graded_card_id = gc.card_id
        WHERE tl.trade_id = ?
        ORDER BY tl.direction DESC, tl.line_id
    """,  params=(trade_id,))
    
    conn.close()
    
    return {
        'trade': dict(trade),
        'lines': df_lines
    }


def create_trade(trade_date, tracking_number=None, notes=None):
    """Create a new trade record and return its ID"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO trades (trade_date, tracking_number, notes)
        VALUES (?, ?, ?)
    """, (trade_date, tracking_number, notes))
    
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return trade_id


def add_trade_line(trade_id, direction, line_type, value, purchase_id=None, 
                   graded_card_id=None, payment_source=None, transaction_id=None):
    """Add a line to a trade"""
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO trade_lines 
        (trade_id, direction, line_type, purchase_id, value, graded_card_id, payment_source, transaction_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade_id, direction, line_type, purchase_id, value, graded_card_id, payment_source, transaction_id))
    
    line_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return line_id


def update_trade_line_sale_id(line_id, sale_id):
    """Update a trade line with the created sale_id"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE trade_lines SET sale_id = ? WHERE line_id = ?", (sale_id, line_id))
    conn.commit()
    conn.close()


def process_trade(trade_id):
    """
    Process a trade - creates sales for GIVE lines and updates purchase costs for RECEIVE lines.
    
    For GIVE inventory lines:
    - Creates a sale with platform='Trade'
    - If graded_card_id specified, links it and sets status to 'Traded'
    
    For RECEIVE inventory lines:
    - Adds the value to the purchase's total_cost
    
    Returns: dict with processing stats
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {
        'sales_created': 0,
        'purchases_updated': 0,
        'graded_cards_linked': 0,
        'errors': []
    }
    
    # Get trade info
    cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    trade = cursor.fetchone()
    
    if not trade:
        conn.close()
        return {'success': False, 'message': 'Trade not found', **stats}
    
    trade_date = trade['trade_date']
    tracking_number = trade['tracking_number']
    
    # Get all GIVE inventory lines for shipping cost allocation
    cursor.execute("""
        SELECT line_id, value FROM trade_lines 
        WHERE trade_id = ? AND direction = 'GIVE' AND line_type = 'inventory'
    """, (trade_id,))
    give_lines = cursor.fetchall()
    
    total_give_value = sum(line['value'] for line in give_lines)
    
    # Get shipping cost from shipping_costs table if tracking number provided
    shipping_cost = 0
    if tracking_number:
        cursor.execute("""
            SELECT cost FROM shipping_costs WHERE tracking_number = ?
        """, (tracking_number,))
        ship_result = cursor.fetchone()
        if ship_result:
            shipping_cost = ship_result['cost']
            # Mark as matched
            cursor.execute("""
                UPDATE shipping_costs SET matched = 1 WHERE tracking_number = ?
            """, (tracking_number,))
    
    # Update trade with shipping cost
    cursor.execute("UPDATE trades SET shipping_cost = ? WHERE trade_id = ?", (shipping_cost, trade_id))
    
    # Process GIVE lines
    cursor.execute("""
        SELECT * FROM trade_lines 
        WHERE trade_id = ? AND direction = 'GIVE'
    """, (trade_id,))
    give_lines_full = cursor.fetchall()
    
    for line in give_lines_full:
        if line['line_type'] == 'inventory':
            # Calculate proportional shipping cost
            line_shipping = 0
            if total_give_value > 0 and shipping_cost > 0:
                line_shipping = (line['value'] / total_give_value) * shipping_cost
            
            # Create sale record
            # Generate a unique order number for the trade
            order_number = f"TRADE-{trade_id}"
            
            # Get purchase display name for item title
            cursor.execute("SELECT display_name FROM purchases WHERE purchase_id = ?", (line['purchase_id'],))
            purchase_result = cursor.fetchone()
            purchase_name = purchase_result['display_name'] if purchase_result else line['purchase_id']
            
            # Build item title
            item_title = f"Trade: {purchase_name}"
            if line['graded_card_id']:
                cursor.execute("SELECT card_name, cert_number, grade FROM graded_cards WHERE card_id = ?", 
                             (line['graded_card_id'],))
                card = cursor.fetchone()
                if card:
                    item_title = f"Trade: {card['card_name']} [{card['grade']}] (Cert: {card['cert_number']})"
            
            # Calculate net profit (revenue - shipping)
            net_profit = line['value'] - line_shipping
            
            # Insert sale
            cursor.execute("""
                INSERT INTO sales (
                    purchase_id, platform, order_number, item_title, sale_date,
                    quantity, sale_price, shipping_cost, net_profit
                ) VALUES (?, 'Trade', ?, ?, ?, 1, ?, ?, ?)
            """, (line['purchase_id'], order_number, item_title, trade_date, 
                  line['value'], line_shipping, net_profit))
            
            sale_id = cursor.lastrowid
            stats['sales_created'] += 1
            
            # Update trade line with sale_id
            cursor.execute("UPDATE trade_lines SET sale_id = ? WHERE line_id = ?", 
                         (sale_id, line['line_id']))
            
            # If graded card, link it and update status
            if line['graded_card_id']:
                # Get allocated cost
                cursor.execute("SELECT allocated_cost FROM graded_cards WHERE card_id = ?", 
                             (line['graded_card_id'],))
                gc_result = cursor.fetchone()
                allocated_cost = gc_result['allocated_cost'] if gc_result else 0
                
                # Update graded card status
                cursor.execute("""
                    UPDATE graded_cards 
                    SET sale_id = ?, status = 'Traded'
                    WHERE card_id = ?
                """, (sale_id, line['graded_card_id']))
                
                # Update sale with grading fee
                cursor.execute("""
                    UPDATE sales 
                    SET grading_fee = ?, net_profit = net_profit - ?
                    WHERE sale_id = ?
                """, (allocated_cost, allocated_cost, sale_id))
                
                stats['graded_cards_linked'] += 1
    
    # Process RECEIVE lines
    cursor.execute("""
        SELECT * FROM trade_lines 
        WHERE trade_id = ? AND direction = 'RECEIVE'
    """, (trade_id,))
    receive_lines = cursor.fetchall()
    
    for line in receive_lines:
        if line['line_type'] == 'inventory':
            # Add value to purchase's total_cost
            cursor.execute("""
                UPDATE purchases 
                SET total_cost = COALESCE(total_cost, 0) + ?
                WHERE purchase_id = ?
            """, (line['value'], line['purchase_id']))
            
            if cursor.rowcount > 0:
                stats['purchases_updated'] += 1
    
    conn.commit()
    conn.close()
    
    return {'success': True, 'message': 'Trade processed successfully', **stats}


def delete_trade(trade_id):
    """
    Delete a trade and reverse its effects.
    
    - Deletes associated sales records
    - Reverses purchase cost increases
    - Resets graded card status back to 'Inventory'
    - Unmatches shipping cost
    
    Returns: dict with deletion stats
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    stats = {
        'sales_deleted': 0,
        'purchases_reversed': 0,
        'graded_cards_reset': 0
    }
    
    # Get trade info
    cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    trade = cursor.fetchone()
    
    if not trade:
        conn.close()
        return {'success': False, 'message': 'Trade not found'}
    
    # Get all lines
    cursor.execute("SELECT * FROM trade_lines WHERE trade_id = ?", (trade_id,))
    lines = cursor.fetchall()
    
    for line in lines:
        if line['direction'] == 'GIVE' and line['line_type'] == 'inventory':
            # Delete the sale
            if line['sale_id']:
                cursor.execute("DELETE FROM sales WHERE sale_id = ?", (line['sale_id'],))
                stats['sales_deleted'] += 1
            
            # Reset graded card status
            if line['graded_card_id']:
                cursor.execute("""
                    UPDATE graded_cards 
                    SET sale_id = NULL, status = 'Inventory'
                    WHERE card_id = ?
                """, (line['graded_card_id'],))
                stats['graded_cards_reset'] += 1
        
        elif line['direction'] == 'RECEIVE' and line['line_type'] == 'inventory':
            # Reverse the cost addition
            cursor.execute("""
                UPDATE purchases 
                SET total_cost = COALESCE(total_cost, 0) - ?
                WHERE purchase_id = ?
            """, (line['value'], line['purchase_id']))
            stats['purchases_reversed'] += 1
    
    # Unmatch shipping cost
    if trade['tracking_number']:
        cursor.execute("""
            UPDATE shipping_costs SET matched = 0 WHERE tracking_number = ?
        """, (trade['tracking_number'],))
    
    # Delete trade lines
    cursor.execute("DELETE FROM trade_lines WHERE trade_id = ?", (trade_id,))
    
    # Delete trade
    cursor.execute("DELETE FROM trades WHERE trade_id = ?", (trade_id,))
    
    conn.commit()
    conn.close()
    
    return {'success': True, 'message': 'Trade deleted and reversed', **stats}


def get_inventory_graded_cards(purchase_id=None):
    """Get graded cards with status='Inventory' for trade selection"""
    conn = get_connection()
    
    query = """
        SELECT 
            gc.card_id,
            gc.cert_number,
            gc.card_name,
            gc.grade,
            gc.purchase_id,
            gc.allocated_cost,
            p.display_name as purchase_name
        FROM graded_cards gc
        LEFT JOIN purchases p ON gc.purchase_id = p.purchase_id
        WHERE gc.status = 'Inventory'
    """
    
    if purchase_id:
        query += " AND gc.purchase_id = ?"
        df = _read_sql(conn, query + " ORDER BY gc.card_name",  params=(purchase_id,))
    else:
        df = _read_sql(conn, query + " ORDER BY gc.purchase_id, gc.card_name")
    
    conn.close()
    return df


def get_unmatched_cash_transactions(payment_sources=None):
    """Get unmatched transactions that could be cash in a trade"""
    conn = get_connection()
    
    # Default payment sources for trades
    if payment_sources is None:
        payment_sources = ['PayPal', 'Venmo']
    
    placeholders = ','.join(['?' for _ in payment_sources])
    
    df = _read_sql(conn, f"""
        SELECT 
            id,
            source,
            transaction_date,
            description,
            amount,
            category
        FROM transactions
        WHERE source IN ({placeholders})
        AND (category IS NULL OR category = '' OR category = 'Uncategorized')
        ORDER BY transaction_date DESC
    """,  params=payment_sources)
    
    conn.close()
    return df


def link_shipping_to_trade(tracking_number, trade_id):
    """
    Link a shipping cost to a trade by tracking number.
    Called when Pirate Ship import matches a tracking number.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get shipping cost
    cursor.execute("SELECT cost FROM shipping_costs WHERE tracking_number = ?", (tracking_number,))
    ship_result = cursor.fetchone()
    
    if not ship_result:
        conn.close()
        return {'success': False, 'message': 'Shipping cost not found'}
    
    shipping_cost = ship_result['cost']
    
    # Update trade
    cursor.execute("""
        UPDATE trades SET shipping_cost = ?, tracking_number = ?
        WHERE trade_id = ?
    """, (shipping_cost, tracking_number, trade_id))
    
    # Mark shipping as matched
    cursor.execute("UPDATE shipping_costs SET matched = 1 WHERE tracking_number = ?", (tracking_number,))
    
    # Reallocate shipping to GIVE sales proportionally
    cursor.execute("""
        SELECT tl.line_id, tl.value, tl.sale_id
        FROM trade_lines tl
        WHERE tl.trade_id = ? AND tl.direction = 'GIVE' AND tl.line_type = 'inventory'
    """, (trade_id,))
    give_lines = cursor.fetchall()
    
    total_give_value = sum(line['value'] for line in give_lines)
    
    for line in give_lines:
        if line['sale_id'] and total_give_value > 0:
            line_shipping = (line['value'] / total_give_value) * shipping_cost
            cursor.execute("""
                UPDATE sales 
                SET shipping_cost = ?,
                    net_profit = sale_price - COALESCE(platform_fees_fixed, 0) - COALESCE(platform_fees_variable, 0) 
                               - COALESCE(regulatory_fee, 0) - COALESCE(promoted_listing_fee, 0) 
                               - COALESCE(international_fee, 0) - COALESCE(supplies_estimate, 0) 
                               - COALESCE(grading_fee, 0) - ?
                WHERE sale_id = ?
            """, (line_shipping, line_shipping, line['sale_id']))
    
    conn.commit()
    conn.close()
    
    return {'success': True, 'message': f'Linked ${shipping_cost:.2f} shipping to trade'}


# ============================================================================
# PROFIT ALLOCATION QUERIES
# ============================================================================

def get_all_allocations():
    """Get all profit allocations"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            pa.allocation_id,
            pa.source_purchase_id,
            ps.display_name as source_name,
            pa.target_purchase_id,
            pt.display_name as target_name,
            pa.amount,
            pa.allocation_date,
            pa.notes,
            pa.created_at
        FROM profit_allocations pa
        LEFT JOIN purchases ps ON pa.source_purchase_id = ps.purchase_id
        LEFT JOIN purchases pt ON pa.target_purchase_id = pt.purchase_id
        ORDER BY pa.allocation_date DESC
    """)
    conn.close()
    return df


def get_allocations_for_purchase(purchase_id):
    """Get all allocations from a specific purchase"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            pa.allocation_id,
            pa.target_purchase_id,
            pt.display_name as target_name,
            pa.amount,
            pa.allocation_date,
            pa.notes
        FROM profit_allocations pa
        LEFT JOIN purchases pt ON pa.target_purchase_id = pt.purchase_id
        WHERE pa.source_purchase_id = ?
        ORDER BY pa.allocation_date DESC
    """,  params=(purchase_id,))
    conn.close()
    return df


def get_total_allocated_from_purchase(purchase_id):
    """Get total amount allocated from a purchase"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM profit_allocations
        WHERE source_purchase_id = ?
    """, (purchase_id,))
    result = cursor.fetchone()
    conn.close()
    return result['total'] if result else 0


def get_total_allocated_to_purchase(purchase_id):
    """Get total amount allocated to a purchase (e.g., LTH)"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) as total
        FROM profit_allocations
        WHERE target_purchase_id = ?
    """, (purchase_id,))
    result = cursor.fetchone()
    conn.close()
    return result['total'] if result else 0


def add_profit_allocation(source_purchase_id, amount, target_purchase_id='LTH', 
                         allocation_date=None, notes=None):
    """Add a new profit allocation"""
    conn = get_connection()
    cursor = conn.cursor()
    
    if allocation_date is None:
        allocation_date = datetime.now().strftime('%Y-%m-%d')
    
    cursor.execute("""
        INSERT INTO profit_allocations 
        (source_purchase_id, target_purchase_id, amount, allocation_date, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (source_purchase_id, target_purchase_id, amount, allocation_date, notes))
    
    allocation_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return allocation_id


def delete_profit_allocation(allocation_id):
    """Delete a profit allocation"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM profit_allocations WHERE allocation_id = ?", (allocation_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_allocation_summary():
    """Get summary of allocations by source purchase"""
    conn = get_connection()
    df = _read_sql(conn, """
        SELECT 
            pa.source_purchase_id,
            p.display_name as purchase_name,
            SUM(pa.amount) as total_allocated,
            COUNT(*) as allocation_count,
            MAX(pa.allocation_date) as last_allocation
        FROM profit_allocations pa
        LEFT JOIN purchases p ON pa.source_purchase_id = p.purchase_id
        GROUP BY pa.source_purchase_id
        ORDER BY total_allocated DESC
    """)
    conn.close()
    return df

