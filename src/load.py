"""Load the Online Retail dataset into PostgreSQL.

Pipeline
--------
    read_raw()      ->  raw DataFrame (541,909 rows on the full dataset)
    split_clean()   ->  cleaned sales rows  +  cancellation rows
    load_database() ->  COPY everything into the five tables from schema.sql

Cleaning rules (applied to build the *sales* fact table)
--------------------------------------------------------
  1. drop rows with a missing CustomerID
  2. drop cancellations (InvoiceNo starts with 'C')
  3. drop Quantity  <= 0
  4. drop UnitPrice <= 0
  5. drop exact duplicate rows
  6. revenue = Quantity * UnitPrice

Cancellation rows are NOT discarded.  They are kept as a separate dataset so
that query 05 can measure returns; see ``split_clean`` for the exact rule.
"""

from __future__ import annotations

import decimal
import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd

from db import PROJECT_ROOT

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
DATA_DIR = PROJECT_ROOT / "data"
SAMPLE_CSV = DATA_DIR / "sample" / "online_retail_sample.csv"
RAW_DIR = DATA_DIR / "raw"
RAW_XLSX = RAW_DIR / "Online Retail.xlsx"
RAW_ZIP = RAW_DIR / "online_retail.zip"
# Reading the .xlsx takes ~30s; cache the equivalent CSV next to it so repeated
# runs of run_all.py are fast.  data/raw/ is git-ignored.
RAW_CSV_CACHE = RAW_DIR / "online_retail.csv"

FULL_DATA_URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"

DTYPES = {
    "InvoiceNo": "string",
    "StockCode": "string",
    "Description": "string",
    "Quantity": "int64",
    "UnitPrice": "float64",
    "CustomerID": "float64",
    "Country": "string",
}

# Bulk COPY settings -- all constraint checking happens once at the end of the
# load instead of row-by-row, which turns minutes into seconds.
COPY_SETUP = [
    "SET synchronous_commit = off",
    "SET maintenance_work_mem = '256MB'",
    "SET work_mem = '64MB'",
]


# ==========================================================================
# Reading
# ==========================================================================
def find_full_dataset() -> Path | None:
    """Return the path of a usable full dataset, or None."""
    if RAW_XLSX.exists():
        return RAW_XLSX
    if RAW_CSV_CACHE.exists():
        return RAW_CSV_CACHE
    return None


def download_full_dataset(target_zip: Path | None = None) -> Path | None:
    """Try to fetch the full UCI dataset.  Returns the zip path or None.

    Never raises: the sample CSV is a perfectly valid fallback, so a network
    failure should only produce a warning.
    """
    target_zip = target_zip or RAW_ZIP
    if target_zip.exists() and target_zip.stat().st_size > 20_000_000:
        return target_zip
    try:
        import urllib.request

        target_zip.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading full dataset from {FULL_DATA_URL} ...")
        with urllib.request.urlopen(FULL_DATA_URL, timeout=120) as response:
            payload = response.read()
        target_zip.write_bytes(payload)
        print(f"  downloaded {len(payload) / 1e6:.1f} MB -> {target_zip}")
        return target_zip
    except Exception as exc:  # noqa: BLE001 - offline is an expected case
        print(f"  ! could not download full dataset ({exc.__class__.__name__}: {exc})",
              file=sys.stderr)
        return None


def read_raw(source: Path | None = None, allow_download: bool = True) -> tuple[pd.DataFrame, str]:
    """Read the full dataset if present, else the bundled sample.

    Returns ``(dataframe, source_label)``.  The label is what run_all.py prints
    and records in the report, so it must be unambiguous.
    """
    path = Path(source) if source else find_full_dataset()

    if path is None and allow_download:
        zip_path = download_full_dataset()
        if zip_path:
            extract_full_dataset(zip_path)
            path = find_full_dataset()

    if path is not None and path.suffix.lower() == ".zip":
        extract_full_dataset(path)
        path = find_full_dataset()

    if path is not None and path.suffix.lower() == ".xlsx":
        if RAW_CSV_CACHE.exists() and RAW_CSV_CACHE.stat().st_mtime >= path.stat().st_mtime:
            df = pd.read_csv(RAW_CSV_CACHE, dtype=DTYPES, parse_dates=["InvoiceDate"])
            return df, f"FULL dataset (cached CSV {RAW_CSV_CACHE.name})"
        print(f"  reading {path.name} (Excel, ~30s) ...")
        df = pd.read_excel(path)
        df.to_csv(RAW_CSV_CACHE, index=False)
        df = pd.read_csv(RAW_CSV_CACHE, dtype=DTYPES, parse_dates=["InvoiceDate"])
        return df, f"FULL dataset ({path.name})"

    if path is not None:
        df = pd.read_csv(path, dtype=DTYPES, parse_dates=["InvoiceDate"])
        if path.resolve() == SAMPLE_CSV.resolve():
            return df, f"SAMPLE ({SAMPLE_CSV.relative_to(PROJECT_ROOT).as_posix()})"
        return df, f"FULL dataset ({path.name})"

    if not SAMPLE_CSV.exists():
        raise FileNotFoundError(
            f"No dataset found.  Expected the bundled sample at {SAMPLE_CSV}."
        )
    df = pd.read_csv(SAMPLE_CSV, dtype=DTYPES, parse_dates=["InvoiceDate"])
    return df, f"SAMPLE ({SAMPLE_CSV.relative_to(PROJECT_ROOT).as_posix()})"


def extract_full_dataset(zip_path: Path) -> Path:
    """Extract the UCI zip (which contains Online Retail.xlsx)."""
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(zip_path.parent)
    return zip_path.parent / "Online Retail.xlsx"


# ==========================================================================
# Cleaning
# ==========================================================================
def split_clean(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Apply the six cleaning rules and split off cancellations.

    Returns ``(cleaned_sales, cancellations, stats)``.
    """
    stats: dict[str, int] = {"raw_rows": int(len(df))}

    df = df.copy()
    df["InvoiceNo"] = df["InvoiceNo"].astype("string").str.strip()
    df["StockCode"] = df["StockCode"].astype("string").str.strip()
    df["Country"] = df["Country"].fillna("Unknown").astype("string").str.strip()

    # -- Cancellations are pulled out first, before any other rule, because the
    #    rules below (Quantity <= 0) would otherwise delete every single one of
    #    them: a return is stored as a negative quantity.
    is_cancel = df["InvoiceNo"].str.upper().str.startswith("C", na=False)
    cancellations = df.loc[is_cancel].copy()
    stats["cancellation_rows_raw"] = int(len(cancellations))

    # Rule 2 on the sales side: cancellations leave the sales pipeline here.
    sales = df.loc[~is_cancel].copy()

    # Rule 1: a line without a CustomerID cannot be attributed to anyone.
    before = len(sales)
    sales = sales[sales["CustomerID"].notna()]
    stats["dropped_missing_customer"] = int(before - len(sales))

    # Rule 3: negative or zero quantity is not a sale.
    before = len(sales)
    sales = sales[sales["Quantity"] > 0]
    stats["dropped_nonpositive_quantity"] = int(before - len(sales))

    # Rule 4: a zero/negative price is an adjustment, not revenue.
    before = len(sales)
    sales = sales[sales["UnitPrice"] > 0]
    stats["dropped_nonpositive_price"] = int(before - len(sales))

    # Rule 5: exact duplicates (all eight source columns identical).
    before = len(sales)
    sales = sales.drop_duplicates()
    stats["dropped_duplicates"] = int(before - len(sales))

    # Rule 6.
    sales["revenue"] = (sales["Quantity"] * sales["UnitPrice"]).astype("float64")
    sales["CustomerID"] = sales["CustomerID"].astype("int64")
    sales["line_revenue"] = sales["revenue"]

    # ---- cancellations: keep real return lines, drop pure noise -------------
    # A cancellation row is meaningful when it references a known customer, a
    # known product and carries a positive unit price.  Rows without a customer
    # cannot be attributed; rows with a non-positive price carry no money.
    cancellations["InvoiceDate"] = pd.to_datetime(cancellations["InvoiceDate"])
    cancellations["CustomerID"] = cancellations["CustomerID"].astype("Int64")
    cancellations = cancellations[cancellations["UnitPrice"] > 0]
    cancellations = cancellations[cancellations["StockCode"].notna()]
    cancellations = cancellations.drop_duplicates()
    # cancelled_revenue is the positive magnitude of the money handed back.
    cancellations["cancelled_revenue"] = (
        cancellations["Quantity"].abs() * cancellations["UnitPrice"]
    ).astype("float64")
    stats["cancellation_rows_kept"] = int(len(cancellations))

    sales = sales.reset_index(drop=True)
    cancellations = cancellations.reset_index(drop=True)
    stats["cleaned_rows"] = int(len(sales))
    return sales, cancellations, stats


def build_dimensions(sales: pd.DataFrame, cancellations: pd.DataFrame,
                     raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the ``customers`` and ``products`` dimension frames."""
    # Customers: everyone who ever appears with an ID, including people whose
    # only interaction was a return.  Otherwise the cancellations FK would fail.
    known = raw.loc[raw["CustomerID"].notna(), ["CustomerID", "Country"]].copy()
    known["CustomerID"] = known["CustomerID"].astype("int64")
    # One row per customer: keep their most frequent country.
    customers = (known.groupby("CustomerID")["Country"]
                 .agg(lambda s: s.mode().iat[0] if not s.mode().empty else s.iat[0])
                 .reset_index()
                 .rename(columns={"CustomerID": "customer_id", "Country": "country"}))
    customers["country"] = customers["country"].fillna("Unknown")

    # Products: catalogue from the sales lines, with a median reference price.
    prod = sales[["StockCode", "Description", "UnitPrice"]].copy()
    agg = prod.groupby("StockCode").agg(
        median_price=("UnitPrice", "median"),
        description=("Description", lambda s: s.dropna().mode().iat[0]
                     if not s.dropna().empty else None),
    ).reset_index()
    agg = agg.rename(columns={"StockCode": "stock_code"})
    agg["description"] = agg["description"].fillna("UNKNOWN")

    # A cancellation can reference a stock code that never sold; the
    # cancellations table has no FK to products precisely to tolerate that.
    return customers, agg


# ==========================================================================
# Loading
# ==========================================================================
def _copy(conn, table: str, columns: list[str], frame: pd.DataFrame) -> int:
    """Bulk-load a frame with COPY ... FROM STDIN (CSV)."""
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, header=False, na_rep="")
    buffer.seek(0)
    col_list = ", ".join(columns)
    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv, NULL '')",
            buffer,
        )
    return len(frame)


def load_database(db, sales: pd.DataFrame, cancellations: pd.DataFrame,
                  customers: pd.DataFrame, products: pd.DataFrame,
                  verbose: bool = True) -> dict[str, int]:
    """Create the schema and COPY all five tables.  Returns row counts."""
    started = time.time()
    db.create_schema()

    conn = db.connect()
    conn.autocommit = True
    counts: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for statement in COPY_SETUP:
                cur.execute(statement)

        # ---- customers -------------------------------------------------
        counts["customers"] = _copy(
            conn, "customers", ["customer_id", "country"], customers)

        # ---- products --------------------------------------------------
        products_out = products[["stock_code", "description", "median_price"]].copy()
        counts["products"] = _copy(
            conn, "products", ["stock_code", "description", "median_price"],
            products_out)

        # ---- orders ----------------------------------------------------
        # One header row per invoice, from BOTH cleaned sales and cancellations,
        # so that query 05 can join cancellations to a real order record.
        sales_orders = (sales.groupby("InvoiceNo")
                        .agg(invoice_date=("InvoiceDate", "min"),
                             customer_id=("CustomerID", "first"),
                             country=("Country", "first"))
                        .reset_index()
                        .rename(columns={"InvoiceNo": "invoice_no"}))
        sales_orders["is_cancelled"] = False

        cancel_orders = (cancellations.groupby("InvoiceNo")
                         .agg(invoice_date=("InvoiceDate", "min"),
                              country=("Country", "first"))
                         .reset_index()
                         .rename(columns={"InvoiceNo": "invoice_no"}))
        cancel_customers = (cancellations.dropna(subset=["CustomerID"])
                            .groupby("InvoiceNo")["CustomerID"].first())
        cancel_orders["customer_id"] = (cancel_orders["invoice_no"]
                                        .map(cancel_customers)
                                        .astype("Int64"))
        cancel_orders["is_cancelled"] = True

        orders = pd.concat([sales_orders, cancel_orders], ignore_index=True)
        orders = orders[["invoice_no", "customer_id", "invoice_date",
                         "is_cancelled", "country"]]
        counts["orders"] = _copy(
            conn, "orders",
            ["invoice_no", "customer_id", "invoice_date", "is_cancelled", "country"],
            orders)

        # ---- order_items (the revenue fact table) ----------------------
        items = sales[["InvoiceNo", "StockCode", "Quantity", "UnitPrice",
                       "line_revenue"]].rename(columns={
            "InvoiceNo": "invoice_no", "StockCode": "stock_code",
            "Quantity": "quantity", "UnitPrice": "unit_price"})
        counts["order_items"] = _copy(
            conn, "order_items",
            ["invoice_no", "stock_code", "quantity", "unit_price", "line_revenue"],
            items)

        # ---- cancellations (line level) --------------------------------
        canc = cancellations[[
            "InvoiceNo", "StockCode", "Description", "CustomerID",
            "InvoiceDate", "Quantity", "UnitPrice", "cancelled_revenue"]].rename(
            columns={
                "InvoiceNo": "invoice_no", "StockCode": "stock_code",
                "Description": "description", "CustomerID": "customer_id",
                "InvoiceDate": "invoice_date", "Quantity": "quantity",
                "UnitPrice": "unit_price"})
        counts["cancellations"] = _copy(
            conn, "cancellations",
            ["invoice_no", "stock_code", "description", "customer_id",
             "invoice_date", "quantity", "unit_price", "cancelled_revenue"],
            canc)

        # ---- refresh planner statistics, in one pass -------------------
        with conn.cursor() as cur:
            for table in ("customers", "products", "orders", "order_items",
                          "cancellations"):
                cur.execute(f"ANALYZE {table}")
    finally:
        conn.close()

    if verbose:
        elapsed = time.time() - started
        print(f"  loaded in {elapsed:.1f}s")
        for table, n in counts.items():
            print(f"    {table:<14} {n:>9,} rows")
    return counts


def compute_facts(db) -> dict:
    """Pull the headline numbers straight from the database (never hard-coded)."""
    facts: dict[str, object] = {}
    with db.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                   AS cleaned_rows,
                ROUND(SUM(line_revenue), 2)                AS total_revenue,
                COUNT(DISTINCT o.customer_id)              AS customers,
                COUNT(DISTINCT o.country)                  AS countries,
                COUNT(DISTINCT i.stock_code)               AS skus,
                COUNT(DISTINCT i.invoice_no)               AS invoices
            FROM order_items i
            JOIN orders o ON o.invoice_no = i.invoice_no
        """)
        row = cur.fetchone()
        (facts["cleaned_rows"], facts["total_revenue"], facts["customers"],
         facts["countries"], facts["skus"], facts["invoices"]) = row

        cur.execute("""
            SELECT o.country,
                   ROUND(SUM(i.line_revenue), 2) AS revenue,
                   ROUND(100.0 * SUM(i.line_revenue)
                         / (SELECT SUM(line_revenue) FROM order_items), 2) AS pct
            FROM order_items i JOIN orders o ON o.invoice_no = i.invoice_no
            GROUP BY o.country ORDER BY revenue DESC
        """)
        country_rows = cur.fetchall()
        facts["top_countries"] = country_rows
        facts["uk_pct"] = next((r[2] for r in country_rows if r[0] == "United Kingdom"), 0.0)
        facts["top5_pct"] = round(sum(r[2] for r in country_rows[:5]), 2)

        cur.execute("""
            WITH sku AS (
                SELECT stock_code, SUM(line_revenue) AS rev
                FROM order_items GROUP BY stock_code
            ), ranked AS (
                SELECT stock_code, rev,
                       SUM(rev) OVER (ORDER BY rev DESC, stock_code
                                      ROWS UNBOUNDED PRECEDING) AS running,
                       SUM(rev) OVER () AS total
                FROM sku
            )
            SELECT COUNT(*) FILTER (WHERE running <= 0.80 * total) + 1 AS skus_for_80pct,
                   (SELECT COUNT(*) FROM sku) AS total_skus
            FROM ranked
        """)
        abc_skus, total_skus = cur.fetchone()
        facts["abc_skus_80pct"] = abc_skus
        facts["abc_skus_total"] = total_skus
        facts["abc_pct_of_catalog"] = round(100.0 * abc_skus / total_skus, 1)

        cur.execute("""
            SELECT TO_CHAR(DATE_TRUNC('month', o.invoice_date), 'YYYY-MM') AS month,
                   ROUND(SUM(i.line_revenue), 2) AS revenue
            FROM order_items i JOIN orders o ON o.invoice_no = i.invoice_no
            GROUP BY 1 ORDER BY revenue DESC LIMIT 1
        """)
        facts["best_month"], facts["best_month_revenue"] = cur.fetchone()

        cur.execute("""
            SELECT TO_CHAR(DATE_TRUNC('month', o.invoice_date), 'YYYY-MM') AS month,
                   ROUND(SUM(i.line_revenue), 2) AS revenue
            FROM order_items i JOIN orders o ON o.invoice_no = i.invoice_no
            GROUP BY 1 ORDER BY revenue ASC LIMIT 1
        """)
        facts["worst_month"], facts["worst_month_revenue"] = cur.fetchone()

        cur.execute("SELECT COUNT(*), ROUND(SUM(cancelled_revenue), 2) FROM cancellations")
        facts["cancel_lines"], facts["cancel_revenue"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM orders WHERE is_cancelled")
        facts["cancel_invoices"] = cur.fetchone()[0]

    # NUMERIC columns arrive as Decimal; normalise so the report layer can do
    # plain float arithmetic with them.
    for key, value in list(facts.items()):
        if isinstance(value, decimal.Decimal):
            facts[key] = float(value)
        elif isinstance(value, list):
            facts[key] = [tuple(float(v) if isinstance(v, decimal.Decimal) else v
                                for v in row) for row in value]
    return facts
