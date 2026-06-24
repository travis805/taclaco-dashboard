"""
Taclaco Monthly Import Runner
Runs all import sources in the correct sequence from the Import Files folder.
Called by the taclaco-monthly-import Cowork skill.

Usage:
    python run_monthly_import.py --dashboard-path /path/to/taclaco-dashboard

Expected file layout (YTD files — replace each month):
    Import Files/
        eBay/transaction_report_YTD.csv
        Shopify/orders_YTD.csv
        Shopify/payouts_YTD.csv
        Chase/chase_4433_YTD.csv
        Chase/chase_4051_YTD.csv
        Chase/chase_5742_YTD.csv
        Pirate Ship/pirateship_YTD.xlsx
        American Express/amex_YTD.csv

Non-standard download filenames are automatically renamed before the file check.
Chase files like Chase4433_Activity20260101_20260504.CSV, Pirate Ship files
like Shipments (2).xlsx, and AmEx files like activity.csv are detected and
renamed to the canonical names above.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def check(label, path):
    exists = path.exists()
    status = "✓ found" if exists else "✗ missing (skipping)"
    print(f"  {label:35s} {status}")
    return exists


def normalize_import_files(import_files: Path):
    """
    Rename files with non-standard download names to the fixed YTD convention
    the importer expects.  Prints a note for each file it renames.

    Chase downloads as:  Chase4433_Activity<date>_<date>.CSV  (case-insensitive)
    Pirate Ship downloads as: Shipments*.xlsx (any name ending in .xlsx)
    """
    chase_folder = import_files / "Chase"
    ps_folder    = import_files / "Pirate Ship"
    amex_folder  = import_files / "American Express"

    renamed = False

    # Chase: map last-4 digits to canonical filename
    chase_map = {
        "4433": chase_folder / "chase_4433_YTD.csv",
        "4051": chase_folder / "chase_4051_YTD.csv",
        "5742": chase_folder / "chase_5742_YTD.csv",
    }
    if chase_folder.exists():
        for f in list(chase_folder.iterdir()):
            for last4, canonical in chase_map.items():
                if f != canonical and last4 in f.stem and f.suffix.lower() == ".csv":
                    print(f"  ↳ Renamed {f.name} → {canonical.name}")
                    f.rename(canonical)
                    renamed = True
                    break

    # Pirate Ship: any .xlsx that isn't already pirateship_YTD.xlsx
    canonical_ps = ps_folder / "pirateship_YTD.xlsx"
    if ps_folder.exists() and not canonical_ps.exists():
        candidates = [f for f in ps_folder.iterdir()
                      if f.suffix.lower() == ".xlsx" and f != canonical_ps]
        if candidates:
            # prefer the most recently modified one if there are multiple
            candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            print(f"  ↳ Renamed {candidates[0].name} → {canonical_ps.name}")
            candidates[0].rename(canonical_ps)
            renamed = True

    # American Express: activity.csv (default AmEx download name) → amex_YTD.csv
    canonical_amex = amex_folder / "amex_YTD.csv"
    if amex_folder.exists() and not canonical_amex.exists():
        candidates = [f for f in amex_folder.iterdir()
                      if f.suffix.lower() == ".csv" and f != canonical_amex]
        if candidates:
            candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            print(f"  ↳ Renamed {candidates[0].name} → {canonical_amex.name}")
            candidates[0].rename(canonical_amex)
            renamed = True

    if not renamed:
        print("  No renames needed.")


def run(dashboard_path: Path):
    import_files = dashboard_path / "Import Files"

    # Normalize any non-standard download filenames before the file check
    section("NORMALIZING FILENAMES")
    normalize_import_files(import_files)

    # File paths (fixed convention)
    files = {
        "ebay":             import_files / "eBay"             / "transaction_report_YTD.csv",
        "shopify_orders":   import_files / "Shopify"          / "orders_YTD.csv",
        "shopify_payouts":  import_files / "Shopify"          / "payouts_YTD.csv",
        "chase_4433":       import_files / "Chase"            / "chase_4433_YTD.csv",
        "chase_4051":       import_files / "Chase"            / "chase_4051_YTD.csv",
        "chase_5742":       import_files / "Chase"            / "chase_5742_YTD.csv",
        "pirateship":       import_files / "Pirate Ship"      / "pirateship_YTD.xlsx",
        "amex":             import_files / "American Express" / "amex_YTD.csv",
    }

    section("FILE CHECK")
    present = {k: check(k, v) for k, v in files.items()}

    if not any(present.values()):
        print("\n⚠  No import files found. Drop your YTD exports into Import Files/ and try again.")
        sys.exit(1)

    # Single batch name for the whole run
    batch = datetime.now().strftime("monthly_%Y%m%d_%H%M%S")
    print(f"\n  Batch: {batch}")

    # Dual-mode: use Turso when creds are present, local copy otherwise.
    import shutil
    import tempfile
    source_db = dashboard_path / "taclaco.db"

    sys.path.insert(0, str(dashboard_path))
    import database

    _turso_url = os.environ.get("TURSO_DATABASE_URL")
    _turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    _use_turso = bool(_turso_url and _turso_token)

    if _use_turso:
        # Turso mode: imports write directly to the remote primary. The import
        # connection runs in autocommit mode (see database._open_connection) so
        # the importers' nested connections don't deadlock on the write lock.
        tmp_dir = None
        tmp_db = None
        print(f"  Mode: Turso remote ({_turso_url})")
    else:
        # Local SQLite mode: work on a temp copy to avoid iCloud write issues.
        tmp_dir = Path(tempfile.mkdtemp(prefix="taclaco_import_"))
        tmp_db = tmp_dir / "taclaco.db"
        print(f"  Working copy: {tmp_db}")
        shutil.copy2(source_db, tmp_db)
        database.DB_PATH = str(tmp_db)

    database.init_database()   # adds any new tables (idempotent)

    results = {}

    # ------------------------------------------------------------------
    # 1. eBay Transaction Report
    # ------------------------------------------------------------------
    if present["ebay"]:
        section("1 / 6  eBay Transaction Report")
        import import_ebay
        try:
            s = import_ebay.import_ebay_transaction_report(str(files["ebay"]), batch_name=batch)
            print(f"  Staged:  {s['orders']} orders | {s['shipping_labels']} shipping labels | "
                  f"{s['other_fees']} other fees | {s['claims']} claims | "
                  f"{s['subscription_fees']} subscription fees")
            if s["errors"]:
                print(f"  ⚠  {len(s['errors'])} staging errors")

            ps = import_ebay.process_orders_to_sales(batch)
            print(f"  Sales:   {ps['processed']} created | {ps['skipped_duplicates']} duplicates skipped")

            ss = import_ebay.link_shipping_costs(batch)
            print(f"  Shipping:{ss['linked']} labels linked | {ss['not_found']} unmatched")

            pf = import_ebay.link_promoted_listing_fees(batch)
            print(f"  Promo:   {pf['linked']} fees linked | {pf['not_found']} unmatched")

            results["ebay"] = {**s, "sales_created": ps["processed"],
                               "shipping_linked": ss["linked"], "promo_linked": pf["linked"]}
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["ebay"] = {"error": str(e)}

    # ------------------------------------------------------------------
    # 2. Shopify Orders
    # ------------------------------------------------------------------
    if present["shopify_orders"]:
        section("2 / 6  Shopify Orders")
        import import_shopify
        try:
            s = import_shopify.import_shopify_orders(str(files["shopify_orders"]), batch_name=batch)
            print(f"  Staged:  {s['imported']} line items | "
                  f"{s['codisto_skipped']} Codisto/eBay excluded | "
                  f"{s['status_skipped']} non-paid skipped | "
                  f"{s['duplicate_skipped']} duplicates skipped")
            if s["unknown_skus"]:
                print(f"  ⚠  {len(s['unknown_skus'])} UNKNOWN SKUs (review in dashboard)")
                for u in s["unknown_skus"]:
                    print(f"       {u}")

            p = import_shopify.process_shopify_to_sales(batch)
            print(f"  Sales:   {p['processed']} created | {p['skipped_duplicates']} duplicates skipped")

            results["shopify_orders"] = {**s, "sales_created": p["processed"]}
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["shopify_orders"] = {"error": str(e)}

    # ------------------------------------------------------------------
    # 3. Shopify Payouts
    # ------------------------------------------------------------------
    if present["shopify_payouts"]:
        section("3 / 6  Shopify Payouts")
        import import_shopify
        try:
            s = import_shopify.import_shopify_payouts(str(files["shopify_payouts"]), batch_name=batch)
            print(f"  Staged:  {s['imported']} payouts | {s['duplicate_skipped']} duplicates skipped")

            p = import_shopify.process_shopify_payouts_to_transactions(batch)
            print(f"  Fees:    {p['processed']} transactions created | "
                  f"{p['skipped_no_fees']} zero-fee payouts skipped")

            results["shopify_payouts"] = {**s, "transactions_created": p["processed"]}
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["shopify_payouts"] = {"error": str(e)}

    # ------------------------------------------------------------------
    # 4. Chase (all 3 cards)
    # ------------------------------------------------------------------
    chase_sources = {
        "chase_4433": ("Chase-4433", files["chase_4433"]),
        "chase_4051": ("Chase-4051", files["chase_4051"]),
        "chase_5742": ("Chase-5742", files["chase_5742"]),
    }

    any_chase = any(present[k] for k in chase_sources)
    if any_chase:
        section("4 / 6  Chase Credit Cards")
        import import_chase
        # Chase uses a two-step API: parse_chase_csv → import_chase_transactions
        for key, (source_name, filepath) in chase_sources.items():
            if not present[key]:
                print(f"  {source_name}: skipped (no file)")
                continue
            try:
                df = import_chase.parse_chase_csv(str(filepath))
                # import_chase_transactions expects an import_batch_id (integer or string)
                s = import_chase.import_chase_transactions(df, import_batch_id=batch)
                imported = s.get('imported_pending', 0) + s.get('auto_categorized', 0)
                skipped  = s.get('skipped', 0)
                print(f"  {source_name}: {imported} imported "
                      f"({s.get('auto_categorized', 0)} auto-categorized) | "
                      f"{skipped} skipped")
                results[key] = s
            except Exception as e:
                print(f"  {source_name}: ✗ ERROR: {e}")
                results[key] = {"error": str(e)}

    # ------------------------------------------------------------------
    # 5. Pirate Ship
    # ------------------------------------------------------------------
    if present["pirateship"]:
        section("5 / 6  Pirate Ship Shipping Costs")
        import import_pirateship
        try:
            s = import_pirateship.import_pirate_ship_costs(str(files["pirateship"]))
            imported   = s.get('imported', 0)
            duplicates = s.get('duplicates', 0)
            matched    = s.get('matched_to_orders', 0)
            print(f"  Staged:  {imported} labels | {duplicates} duplicates skipped | "
                  f"{matched} matched to orders")
            results["pirateship"] = s
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["pirateship"] = {"error": str(e)}

    # ------------------------------------------------------------------
    # 6. American Express
    # ------------------------------------------------------------------
    if present["amex"]:
        section("6 / 6  American Express")
        import import_amex
        try:
            df = import_amex.parse_amex_csv(str(files["amex"]))
            s = import_amex.import_amex_transactions(df, import_batch_id=batch)
            print(f"  Imported:{s['imported']} transactions "
                  f"({s['auto_categorized']} auto-categorized) | "
                  f"{s['skipped']} skipped | "
                  f"{s['duplicate_skipped']} duplicates skipped")
            results["amex"] = s
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            results["amex"] = {"error": str(e)}

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    section("SUMMARY")
    errors = [(k, v["error"]) for k, v in results.items() if "error" in v]
    if errors:
        print(f"  ⚠  {len(errors)} source(s) had errors:")
        for k, e in errors:
            print(f"     {k}: {e}")
    else:
        print("  ✓ All sources completed without errors")

    print(f"\n  Batch name: {batch}")
    print("  Review UNKNOWN SKUs in the dashboard → Sales → Review UNKNOWN")
    print("  Review AmEx Pending transactions → Financials → Wave Export")
    print("  Grading Batches import is manual — use Inventory → Grading in dashboard\n")

    # Save results back (local SQLite mode only; Turso writes happen in-flight).
    section("SAVING DATABASE")
    if _use_turso:
        print("  Turso mode: writes committed directly to the remote database.")
    else:
        backup_db = source_db.with_name(
            f"taclaco_pre_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
        shutil.copy2(source_db, backup_db)
        print(f"  Backup written: {backup_db.name}")
        shutil.copy2(tmp_db, source_db)
        print(f"  taclaco.db updated")
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Taclaco monthly import")
    parser.add_argument("--dashboard-path", required=True,
                        help="Path to the taclaco-dashboard directory")
    args = parser.parse_args()

    dashboard_path = Path(args.dashboard_path).expanduser().resolve()
    if not dashboard_path.exists():
        print(f"✗ Dashboard path not found: {dashboard_path}")
        sys.exit(1)

    run(dashboard_path)
