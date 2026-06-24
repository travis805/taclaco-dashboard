"""
Taclaco Dashboard - Shopify Import
Handles Shopify Orders CSV and Payouts CSV imports.

Two data sources:
  - Orders export  (Admin → Orders → Export → All, CSV)
  - Payouts export (Admin → Finances → Payouts → Export)

They do NOT join at the row level; orders give per-order/line-item revenue
detail, payouts give aggregate platform fees per settlement.
"""

import pandas as pd
from datetime import datetime
from pathlib import Path
from database import get_connection, get_purchase_id_from_sku


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value, default=0.0):
    """Convert a value to float safely; return default on failure."""
    if pd.isna(value) or value in ("--", "", None):
        return default
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def _parse_shopify_date(value):
    """
    Parse a Shopify datetime string to YYYY-MM-DD.
    Shopify format: '2026-04-30 11:06:05 -0700'
    """
    if pd.isna(value) or not str(value).strip():
        return None
    raw = str(value).strip()
    # Try full datetime with timezone offset
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:len(fmt) + 5].strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Fallback: just take the date portion
    return raw[:10] if len(raw) >= 10 else None


def _normalize_sku(raw_sku):
    """
    Normalize a Shopify SKU to a clean purchase_id-extractable form.
    Handles trailing dashes, blank values, etc.
    """
    if pd.isna(raw_sku) or str(raw_sku).strip() in ("", "--", "nan"):
        return ""
    sku = str(raw_sku).strip().rstrip("-").strip()
    return sku


def _is_codisto_order(row):
    """
    Return True if this Shopify order row is a Codisto/eBay mirror that
    should be excluded from import to prevent double-counting.
    """
    source = str(row.get("Source", "")).strip().lower()
    email = str(row.get("Email", "")).strip().lower()
    notes = str(row.get("Notes", "")).strip()

    if source == "ebay":
        return True
    if "codisto.com" in email:
        return True
    if "eBay Order Id:" in notes:
        return True
    return False


# ---------------------------------------------------------------------------
# Orders import
# ---------------------------------------------------------------------------

def import_shopify_orders(filepath, batch_name=None):
    """
    Stage Shopify Orders CSV into shopify_orders table.

    Args:
        filepath:   Path to the Shopify orders export CSV
        batch_name: Optional label for this import batch

    Returns:
        dict with import statistics
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()

    if batch_name is None:
        batch_name = datetime.now().strftime("%Y%m%d_%H%M%S")

    conn = get_connection()
    cursor = conn.cursor()

    stats = {
        "total_rows": len(df),
        "imported": 0,
        "codisto_skipped": 0,
        "status_skipped": 0,
        "duplicate_skipped": 0,
        "unknown_skus": [],
        "errors": [],
        "batch_name": batch_name,
    }

    # Group rows by order Name so we can forward-fill order-level fields
    # and handle multi-line-item orders correctly.
    for order_name, group in df.groupby("Name", sort=False):
        try:
            first = group.iloc[0]

            # ---- Codisto / eBay mirror filter ----
            if _is_codisto_order(first):
                stats["codisto_skipped"] += len(group)
                continue

            # ---- Financial status filter ----
            financial_status = str(first.get("Financial Status", "")).strip().lower()
            if financial_status not in ("paid",):
                # Still import partially-refunded paid orders; skip the rest
                stats["status_skipped"] += len(group)
                continue

            refunded_amount = _safe_float(first.get("Refunded Amount", 0))

            # Order-level fields (from first row only)
            paid_at = _parse_shopify_date(first.get("Paid at"))
            payment_method = str(first.get("Payment Method", "")).strip() or None
            payment_reference = str(first.get("Payment Reference", "")).strip() or None
            shipping_total = _safe_float(first.get("Shipping", 0))
            discount_amount = _safe_float(first.get("Discount Amount", 0))
            discount_code = str(first.get("Discount Code", "")).strip() or None
            currency = str(first.get("Currency", "USD")).strip()
            email = str(first.get("Email", "")).strip() or None
            subtotal = _safe_float(first.get("Subtotal", 0))
            taxes = _safe_float(first.get("Taxes", 0))
            total = _safe_float(first.get("Total", 0))
            shopify_order_id = str(first.get("Id", "")).strip() or None
            source = str(first.get("Source", "")).strip() or None
            notes = str(first.get("Notes", "")).strip() or None
            vendor = str(first.get("Vendor", "")).strip() or None
            created_at_shopify = _parse_shopify_date(first.get("Created at"))

            # Count valid line items (all rows in the group)
            line_item_count = len(group)

            # Split order-level shipping evenly across line items
            shipping_per_item = (shipping_total / line_item_count) if line_item_count > 0 else 0

            # Pre-compute order gross for proportional discount allocation
            order_gross = sum(
                _safe_float(r.get("Lineitem price", 0)) * max(int(_safe_float(r.get("Lineitem quantity", 1))), 1)
                for _, r in group.iterrows()
            )

            for _, row in group.iterrows():
                try:
                    lineitem_quantity = max(int(_safe_float(row.get("Lineitem quantity", 1))), 1)
                    lineitem_price = _safe_float(row.get("Lineitem price", 0))
                    lineitem_name = str(row.get("Lineitem name", "")).strip() or None
                    raw_sku = row.get("Lineitem sku", "")
                    lineitem_sku = _normalize_sku(raw_sku)
                    lineitem_fulfillment_status = str(row.get("Lineitem fulfillment status", "")).strip() or None

                    # Proportional discount for this line item
                    if discount_amount > 0 and order_gross > 0:
                        line_value = lineitem_price * lineitem_quantity
                        allocated_discount = discount_amount * (line_value / order_gross)
                    else:
                        allocated_discount = 0.0

                    # Flag unknown SKUs for review
                    if not lineitem_sku:
                        stats["unknown_skus"].append(
                            f"{order_name} | {lineitem_name}"
                        )

                    # Duplicate check
                    cursor.execute("""
                        SELECT id FROM shopify_orders
                        WHERE order_name = ? AND lineitem_sku = ? AND lineitem_name = ? AND import_batch = ?
                    """, (order_name, lineitem_sku, lineitem_name, batch_name))
                    if cursor.fetchone():
                        stats["duplicate_skipped"] += 1
                        continue

                    cursor.execute("""
                        INSERT INTO shopify_orders (
                            order_name, email, financial_status, paid_at,
                            fulfillment_status, currency,
                            subtotal, shipping, taxes, total,
                            discount_code, discount_amount,
                            lineitem_quantity, lineitem_name, lineitem_price,
                            lineitem_sku, lineitem_fulfillment_status,
                            payment_method, payment_reference,
                            refunded_amount, vendor, shopify_order_id,
                            source, notes, created_at_shopify, import_batch
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        order_name,
                        email,
                        financial_status,
                        paid_at,
                        lineitem_fulfillment_status,
                        currency,
                        subtotal,
                        shipping_per_item,
                        taxes,
                        total,
                        discount_code,
                        allocated_discount,
                        lineitem_quantity,
                        lineitem_name,
                        lineitem_price,
                        lineitem_sku,
                        lineitem_fulfillment_status,
                        payment_method,
                        payment_reference,
                        refunded_amount,
                        vendor,
                        shopify_order_id,
                        source,
                        notes,
                        created_at_shopify,
                        batch_name,
                    ))
                    stats["imported"] += 1

                except Exception as e:
                    stats["errors"].append(f"{order_name} line item: {str(e)}")

        except Exception as e:
            stats["errors"].append(f"Order {order_name}: {str(e)}")

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Orders → Sales
# ---------------------------------------------------------------------------

def process_shopify_to_sales(batch_name=None):
    """
    Convert staged shopify_orders rows into the sales table.

    Args:
        batch_name: Only process rows from this batch (None = all unprocessed)

    Returns:
        dict with processing statistics
    """
    conn = get_connection()
    cursor = conn.cursor()

    where = "WHERE 1=1"
    params = []
    if batch_name:
        where += " AND import_batch = ?"
        params.append(batch_name)

    cursor.execute(f"SELECT * FROM shopify_orders {where}", params)
    rows = cursor.fetchall()

    stats = {
        "processed": 0,
        "skipped_duplicates": 0,
        "unknown_skus": [],
        "errors": [],
    }

    for row in rows:
        try:
            raw_sku = row["lineitem_sku"] or ""
            purchase_id = get_purchase_id_from_sku(raw_sku, row["lineitem_name"])

            if purchase_id == "UNKNOWN":
                stats["unknown_skus"].append(
                    f"{row['order_name']} | {row['lineitem_name']} | SKU: {raw_sku!r}"
                )

            # Duplicate guard: (platform, order_number, transaction_id, custom_label)
            cursor.execute("""
                SELECT sale_id FROM sales
                WHERE platform = 'Shopify'
                  AND order_number = ?
                  AND (transaction_id = ? OR (transaction_id IS NULL AND ? IS NULL))
                  AND (custom_label = ? OR (custom_label IS NULL AND ? IS NULL))
            """, (
                row["order_name"],
                row["payment_reference"], row["payment_reference"],
                raw_sku or None, raw_sku or None,
            ))
            if cursor.fetchone():
                stats["skipped_duplicates"] += 1
                continue

            quantity = row["lineitem_quantity"] or 1
            sale_price = row["lineitem_price"] or 0.0
            shipping_charged = row["shipping"] or 0.0
            discount_allocated = row["discount_amount"] or 0.0

            net_profit = (sale_price * quantity) + shipping_charged - discount_allocated

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
                "Shopify",
                row["order_name"],
                row["payment_reference"] or None,
                row["lineitem_name"],
                raw_sku or None,
                row["paid_at"],
                quantity,
                sale_price,
                shipping_charged,
                0,    # shipping_cost — Shopify labels come through Chase; matched separately
                0,    # platform_fees_fixed — recorded at payout level
                0,    # platform_fees_variable — same
                0,    # regulatory_fee
                0,    # promoted_listing_fee
                0,    # international_fee
                0,    # supplies_estimate
                net_profit,
            ))
            stats["processed"] += 1

        except Exception as e:
            stats["errors"].append(
                f"shopify_orders row {row['id']}: {str(e)}"
            )

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Payouts import
# ---------------------------------------------------------------------------

def import_shopify_payouts(filepath, batch_name=None):
    """
    Stage Shopify Payouts CSV into shopify_payouts table.

    Args:
        filepath:   Path to the Shopify payouts export CSV
        batch_name: Optional label for this import batch

    Returns:
        dict with import statistics
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()

    if batch_name is None:
        batch_name = datetime.now().strftime("%Y%m%d_%H%M%S")

    conn = get_connection()
    cursor = conn.cursor()

    stats = {
        "total_rows": len(df),
        "imported": 0,
        "duplicate_skipped": 0,
        "errors": [],
        "batch_name": batch_name,
    }

    for idx, row in df.iterrows():
        try:
            payout_date = str(row.get("Payout Date", "")).strip()
            status = str(row.get("Status", "")).strip()
            charges = _safe_float(row.get("Charges", 0))
            refunds = _safe_float(row.get("Refunds", 0))
            adjustments = _safe_float(row.get("Adjustments", 0))
            marketplace_sales_tax = _safe_float(row.get("Marketplace Sales Tax", 0))
            fees = _safe_float(row.get("Fees", 0))
            total = _safe_float(row.get("Total", 0))
            currency = str(row.get("Currency", "USD")).strip()
            bank_reference = str(row.get("Bank Reference", "")).strip() or None

            if not payout_date:
                stats["errors"].append(f"Row {idx}: missing Payout Date")
                continue

            # Duplicate check: same bank_reference (batch-agnostic so re-importing
            # a YTD file across multiple runs doesn't create duplicate records).
            # Fall back to payout_date when bank_reference is absent.
            if bank_reference:
                cursor.execute("""
                    SELECT id FROM shopify_payouts
                    WHERE bank_reference = ?
                """, (bank_reference,))
            else:
                cursor.execute("""
                    SELECT id FROM shopify_payouts
                    WHERE payout_date = ? AND bank_reference IS NULL
                """, (payout_date,))
            if cursor.fetchone():
                stats["duplicate_skipped"] += 1
                continue

            cursor.execute("""
                INSERT INTO shopify_payouts (
                    payout_date, status, charges, refunds, adjustments,
                    marketplace_sales_tax, fees, total, currency,
                    bank_reference, import_batch, processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                payout_date,
                status,
                charges,
                refunds,
                adjustments,
                marketplace_sales_tax,
                fees,
                total,
                currency,
                bank_reference,
                batch_name,
            ))
            stats["imported"] += 1

        except Exception as e:
            stats["errors"].append(f"Row {idx}: {str(e)}")

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# Payouts → Transactions
# ---------------------------------------------------------------------------

def process_shopify_payouts_to_transactions(batch_name=None):
    """
    Export unprocessed Shopify payout fees to the transactions table.

    Marketplace Sales Tax is a pass-through (Shopify collected & remitted);
    it is NOT recorded as revenue or expense — informational only.

    Net fee = Fees - Adjustments (Adjustments are Shopify fee credits).

    Args:
        batch_name: Only process rows from this batch (None = all unprocessed)

    Returns:
        dict with processing statistics
    """
    conn = get_connection()
    cursor = conn.cursor()

    where = "WHERE processed = 0"
    params = []
    if batch_name:
        where += " AND import_batch = ?"
        params.append(batch_name)

    cursor.execute(f"SELECT * FROM shopify_payouts {where}", params)
    payouts = cursor.fetchall()

    stats = {
        "processed": 0,
        "skipped_no_fees": 0,
        "errors": [],
    }

    for payout in payouts:
        try:
            fees = payout["fees"] or 0.0
            adjustments = payout["adjustments"] or 0.0
            net_fees = fees - adjustments  # positive = gross expense before credits

            # Refund payout: total is negative, fees are zero
            total = payout["total"] or 0.0
            charges = payout["charges"] or 0.0
            refunds = payout["refunds"] or 0.0

            payout_date = payout["payout_date"]
            bank_ref = payout["bank_reference"] or "N/A"

            if total < 0 or refunds < 0:
                # Refund payout — money pulled back from Travis's account
                amount = total  # already negative
                category = "Returns & Refunds - Shopify"
                description = f"Shopify refund payout – {payout_date}"
            elif net_fees <= 0 and total >= 0:
                # Payout with no fee component (e.g., pure deposit, no fees)
                stats["skipped_no_fees"] += 1
                cursor.execute(
                    "UPDATE shopify_payouts SET processed = 1 WHERE id = ?",
                    (payout["id"],)
                )
                continue
            else:
                amount = -net_fees  # store as negative (expense)
                category = "Platform Fees - Shopify"
                description = f"Shopify Payments fees – payout {payout_date}"

            notes = (
                f"Bank ref: {bank_ref} | "
                f"Gross charges: {charges:.2f} | "
                f"Fees: {fees:.2f} | "
                f"Adjustments: {adjustments:.2f} | "
                f"Net payout: {total:.2f}"
            )

            cursor.execute("""
                INSERT INTO transactions (
                    source, transaction_date, merchant_name, description,
                    amount, category, status, notes, import_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "Shopify",
                payout_date,
                "Shopify Payments",
                description,
                amount,
                category,
                "Categorized",
                notes,
                f"shopify_payouts:{payout['import_batch']}",
            ))

            # Mark as processed to prevent re-export on re-run
            cursor.execute(
                "UPDATE shopify_payouts SET processed = 1 WHERE id = ?",
                (payout["id"],)
            )
            stats["processed"] += 1

        except Exception as e:
            stats["errors"].append(f"Payout {payout['id']} ({payout['payout_date']}): {str(e)}")

    conn.commit()
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python import_shopify.py orders <orders_csv>")
        print("       python import_shopify.py payouts <payouts_csv>")
        sys.exit(1)

    mode = sys.argv[1].lower()
    path = sys.argv[2]

    if mode == "orders":
        print(f"Importing Shopify orders from: {path}")
        s = import_shopify_orders(path)
        print(f"Import stats: {s}")
        print("Processing orders to sales...")
        p = process_shopify_to_sales(s["batch_name"])
        print(f"Process stats: {p}")

    elif mode == "payouts":
        print(f"Importing Shopify payouts from: {path}")
        s = import_shopify_payouts(path)
        print(f"Import stats: {s}")
        print("Exporting payout fees to transactions...")
        p = process_shopify_payouts_to_transactions(s["batch_name"])
        print(f"Process stats: {p}")

    else:
        print(f"Unknown mode: {mode}. Use 'orders' or 'payouts'.")
        sys.exit(1)
