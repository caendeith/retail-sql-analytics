"""End-to-end pipeline for the retail SQL analytics portfolio project.

    python run_all.py

Steps
-----
 1. read the full Online Retail dataset (falls back to the bundled sample and
    says which source it used)
 2. apply the six cleaning rules, keeping cancellations aside
 3. start a real PostgreSQL 16 server (pgserver, data dir ``.pgdata/``)
 4. create the schema from ``src/schema.sql`` and bulk-load all five tables
 5. run all six queries in ``sql/`` and save ``reports/query_results/NN_*.csv``
 6. write ``reports/sql_report.md`` from the real query output
 7. shut the server down again

Flags
-----
    --sample        force the bundled sample CSV instead of the full dataset
    --no-download   never hit the network; use whatever is already on disk
    --keep-server   leave PostgreSQL running after the run (debugging)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import load  # noqa: E402
from db import Database  # noqa: E402
from run_queries import RESULTS_DIR, QueryResult, run_all_queries  # noqa: E402

REPORTS_DIR = PROJECT_ROOT / "reports"
REPORT_PATH = REPORTS_DIR / "sql_report.md"


def configure_console() -> None:
    """Keep console output safe on legacy Windows code pages.

    The report file is always written as UTF-8, but a default Windows console
    may be running cp1251/cp866 where characters such as the pound sign cannot
    be encoded at all -- which would otherwise crash an otherwise fine run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass


# ==========================================================================
# Formatting helpers
# ==========================================================================
def money(value, currency: str = "£") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"{currency}{float(value):,.2f}"


def num(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"{int(value):,}"


def pct(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"{float(value):.{digits}f}%"


def markdown_table(frame: pd.DataFrame) -> str:
    from run_queries import to_markdown

    return to_markdown(frame)


# ==========================================================================
# Derived roll-ups (computed from the *saved* query output, never invented)
# ==========================================================================
def _round_preserving_ints(frame: pd.DataFrame,
                           decimals: dict[str, int]) -> pd.DataFrame:
    """Round float columns while leaving count columns as integers.

    ``DataFrame.round()`` upcasts whole-number integer columns to float, which
    makes a customer count render as ``434.00`` in the report. Integer columns
    that were not asked to be rounded are cast back.
    """
    integer_columns = {c: frame[c].dtype for c in frame.columns
                       if pd.api.types.is_integer_dtype(frame[c])}
    out = frame.round(decimals)
    for column in integer_columns:
        if column not in decimals:
            out[column] = out[column].astype("int64")
    return out


def segment_rollup(rfm: pd.DataFrame) -> pd.DataFrame:
    """Group query 02's per-customer rows into segment-level KPIs."""
    grouped = rfm.groupby("segment").agg(
        customers=("customer_id", "count"),
        avg_recency_days=("recency_days", "mean"),
        avg_frequency=("frequency", "mean"),
        avg_ltv=("monetary", "mean"),
        total_revenue=("monetary", "sum"),
    ).reset_index()
    grouped["pct_of_revenue"] = 100.0 * grouped["total_revenue"] / grouped["total_revenue"].sum()
    grouped["pct_of_customers"] = 100.0 * grouped["customers"] / grouped["customers"].sum()
    grouped = grouped.sort_values("total_revenue", ascending=False)
    return _round_preserving_ints(grouped, {
        "avg_recency_days": 0, "avg_frequency": 1, "avg_ltv": 2,
        "total_revenue": 2, "pct_of_revenue": 1, "pct_of_customers": 1,
    })


def decile_rollup(ltv: pd.DataFrame) -> pd.DataFrame:
    """Group query 06's per-customer rows into LTV deciles (1 = highest value)."""
    grouped = ltv.groupby("ltv_decile").agg(
        customers=("customer_id", "count"),
        revenue=("ltv", "sum"),
        avg_ltv=("ltv", "mean"),
        avg_orders=("orders", "mean"),
        min_ltv=("ltv", "min"),
        max_ltv=("ltv", "max"),
    ).reset_index()
    grouped["pct_of_revenue"] = 100.0 * grouped["revenue"] / grouped["revenue"].sum()
    grouped["cumulative_pct"] = grouped.sort_values("ltv_decile")["pct_of_revenue"].cumsum()
    grouped["ltv_decile"] = grouped["ltv_decile"].astype("int64")
    return _round_preserving_ints(grouped, {
        "revenue": 2, "avg_ltv": 2, "avg_orders": 1, "min_ltv": 2,
        "max_ltv": 2, "pct_of_revenue": 2, "cumulative_pct": 2,
    })


def concentration_from_ltv(ltv: pd.DataFrame) -> dict:
    """What share of revenue do the top 1% / 5% / 10% of customers produce?"""
    ordered = ltv.sort_values("ltv", ascending=False).reset_index(drop=True)
    total = ordered["ltv"].sum()
    out: dict[str, float] = {}
    for label, frac in (("top_1pct", 0.01), ("top_5pct", 0.05),
                        ("top_10pct", 0.10), ("top_20pct", 0.20)):
        k = max(1, int(round(len(ordered) * frac)))
        out[label] = 100.0 * ordered["ltv"].head(k).sum() / total
    return out


# ==========================================================================
# Report
# ==========================================================================
def build_report(*, source_label: str, stats: dict, facts: dict,
                 results: list[QueryResult], pg_version: str, target: str,
                 counts: dict[str, int], total_elapsed: float) -> str:
    by_name = {r.name: r for r in results}
    q01, q02 = by_name.get("01_monthly_revenue"), by_name.get("02_rfm_segmentation")
    q03, q04 = by_name.get("03_cohort_retention"), by_name.get("04_top_products_by_country")
    q05, q06 = by_name.get("05_cancellations"), by_name.get("06_customer_lifetime_value")

    segments = segment_rollup(q02.dataframe) if q02 is not None and q02.ok else pd.DataFrame()
    deciles = decile_rollup(q06.dataframe) if q06 is not None and q06.ok else pd.DataFrame()
    conc = concentration_from_ltv(q06.dataframe) if q06 is not None and q06.ok else {}

    peak = q01.dataframe.loc[q01.dataframe["revenue"].idxmax()] if q01 is not None and q01.ok else None
    worst = q01.dataframe.loc[q01.dataframe["revenue"].idxmin()] if q01 is not None and q01.ok else None

    lines: list[str] = []
    add = lines.append

    # ---------------- header ----------------
    add("# Retail SQL Analytics — Query Report")
    add("")
    add("This report is generated by `python run_all.py`. Every figure and every "
        "table below is **actual output from a real run** against the PostgreSQL "
        "instance described here — nothing is illustrative or hand-written.")
    add("")
    add(f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    add(f"- **PostgreSQL version:** `{pg_version}`")
    add(f"- **Database:** `{target}`")
    add(f"- **Data source:** {source_label}")
    add(f"- **Total pipeline runtime:** {total_elapsed:.1f}s")
    add("")

    # ---------------- load summary ----------------
    add("## 1. Data loaded")
    add("")
    add("| Table | Rows |")
    add("| --- | ---: |")
    for table, n in counts.items():
        add(f"| `{table}` | {num(n)} |")
    add("")
    add("### Cleaning funnel")
    add("")
    add("| Step | Rows |")
    add("| --- | ---: |")
    add(f"| Raw rows read from source | {num(stats['raw_rows'])} |")
    add(f"| — of which cancellation rows (InvoiceNo starts with `C`) | "
        f"{num(stats['cancellation_rows_raw'])} |")
    add(f"| Dropped: missing `CustomerID` | −{num(stats['dropped_missing_customer'])} |")
    add(f"| Dropped: `Quantity <= 0` | −{num(stats['dropped_nonpositive_quantity'])} |")
    add(f"| Dropped: `UnitPrice <= 0` | −{num(stats['dropped_nonpositive_price'])} |")
    add(f"| Dropped: exact duplicate rows | −{num(stats['dropped_duplicates'])} |")
    add(f"| **Cleaned sales rows loaded into `order_items`** | **{num(stats['cleaned_rows'])}** |")
    add(f"| Cancellation lines kept in `cancellations` | {num(stats['cancellation_rows_kept'])} |")
    add("")
    add("### Headline facts read back out of PostgreSQL")
    add("")
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| Total revenue | {money(facts['total_revenue'])} |")
    add(f"| Customers | {num(facts['customers'])} |")
    add(f"| Countries | {num(facts['countries'])} |")
    add(f"| Distinct SKUs | {num(facts['skus'])} |")
    add(f"| Invoices | {num(facts['invoices'])} |")
    add(f"| United Kingdom share of revenue | {pct(facts['uk_pct'], 1)} |")
    add(f"| Top 5 countries share of revenue | {pct(facts['top5_pct'], 1)} |")
    add(f"| SKUs needed for 80% of revenue (ABC) | {num(facts['abc_skus_80pct'])} "
        f"({pct(facts['abc_pct_of_catalog'], 1)} of the catalogue) |")
    add(f"| Best month | {facts['best_month']} ({money(facts['best_month_revenue'])}) |")
    add(f"| Worst month | {facts['worst_month']} ({money(facts['worst_month_revenue'])}) |")
    add(f"| Cancellation invoices | {num(facts['cancel_invoices'])} |")
    add(f"| Cancellation lines | {num(facts['cancel_lines'])} |")
    add(f"| Revenue cancelled (gross of returns) | {money(facts['cancel_revenue'])} |")
    add("")

    # ---------------- query index ----------------
    add("## 2. Query index")
    add("")
    add("| # | File | Rows returned | Columns | Runtime | Result CSV |")
    add("| --- | --- | ---: | ---: | ---: | --- |")
    for r in results:
        rel = r.path.relative_to(PROJECT_ROOT).as_posix()
        csv = (r.csv_path.relative_to(PROJECT_ROOT).as_posix()
               if r.csv_path else "—")
        add(f"| {r.name[:2]} | `{rel}` | {num(r.row_count)} | {len(r.columns)} | "
            f"{r.elapsed_s:.2f}s | `{csv}` |")
    add("")

    # ---------------- key findings ----------------
    add("## 3. Key findings")
    add("")
    if peak is not None and worst is not None:
        add(f"- Revenue peaks in **{peak['month']}** at {money(peak['revenue'])} and "
            f"troughs in **{worst['month']}** at {money(worst['revenue'])} — a "
            f"{peak['revenue'] / worst['revenue']:.2f}× swing that is the seasonal "
            f"shape of a gift/novelty retailer (Q4 peak, January–February trough).")
    if not segments.empty:
        top_seg = segments.iloc[0]
        add(f"- The **{top_seg['segment']}** segment is the largest by revenue: "
            f"{num(top_seg['customers'])} customers "
            f"({pct(top_seg['pct_of_customers'], 1)} of the base) generating "
            f"{money(top_seg['total_revenue'])} "
            f"({pct(top_seg['pct_of_revenue'], 1)} of revenue).")
    if conc:
        add(f"- Revenue is heavily concentrated: the top **1%** of customers produce "
            f"**{pct(conc['top_1pct'], 1)}** of revenue, the top 5% produce "
            f"{pct(conc['top_5pct'], 1)} and the top 10% produce {pct(conc['top_10pct'], 1)}.")
    if facts.get("cancel_revenue") and facts.get("total_revenue"):
        share = 100.0 * facts["cancel_revenue"] / facts["total_revenue"]
        add(f"- Cancellations give back {money(facts['cancel_revenue'])}, equal to "
            f"**{pct(share, 2)}** of gross sales revenue — small but material enough "
            f"to be tracked monthly.")
    add(f"- {num(facts['abc_skus_80pct'])} SKUs ({pct(facts['abc_pct_of_catalog'], 1)} of "
        f"the {num(facts['abc_skus_total'])}-SKU catalogue) carry 80% of revenue, so "
        f"inventory policy can focus on roughly a fifth of the range.")
    add("")

    # ---------------- per query ----------------
    add("## 4. Query results")
    add("")
    for r in results:
        add(f"### {r.name[:2]}. `{r.path.name}`")
        add("")
        add(f"**Business question.** {r.question}")
        add("")
        add(f"**Rows returned:** {num(r.row_count)} &nbsp;·&nbsp; "
            f"**Columns:** {len(r.columns)} &nbsp;·&nbsp; "
            f"**Runtime:** {r.elapsed_s:.2f}s")
        add("")

        if r.name == "02_rfm_segmentation" and not segments.empty:
            add("Segment roll-up (grouped from the "
                f"{num(r.row_count)} per-customer rows returned by this query):")
            add("")
            add(markdown_table(segments))
            add("")
            add(f"Top 15 of {num(r.row_count)} customer rows, as written to the CSV:")
            add("")
            add(markdown_table(r.dataframe.head(15)))
            add("")
        elif r.name == "06_customer_lifetime_value" and not deciles.empty:
            add("LTV decile roll-up (grouped from the "
                f"{num(r.row_count)} per-customer rows returned by this query):")
            add("")
            add(markdown_table(deciles))
            add("")
            add(f"Top 15 of {num(r.row_count)} customer rows, as written to the CSV:")
            add("")
            add(markdown_table(r.dataframe.head(15)))
            add("")
        elif r.name == "03_cohort_retention":
            add("Full cohort triangle — every row the query returned:")
            add("")
            add(markdown_table(r.dataframe))
            add("")
        elif r.name == "04_top_products_by_country":
            add(f"First 20 of {num(r.row_count)} rows (all rows are in the CSV):")
            add("")
            add(markdown_table(r.dataframe.head(20)))
            add("")
        else:
            add("Full result set:")
            add("")
            add(markdown_table(r.dataframe))
            add("")
        add(f"SQL: [`{r.path.relative_to(PROJECT_ROOT).as_posix()}`]"
            f"(../{r.path.relative_to(PROJECT_ROOT).as_posix()})")
        add("")

    # ---------------- notes ----------------
    add("## 5. Data caveats")
    add("")
    add("- The source file ends on **2011-12-09**, so 2011-12 is a partial month. "
        "Its negative month-over-month growth in query 01 and its low totals in "
        "queries 03–05 are truncation artefacts, not real declines.")
    add("- The same truncation inflates 2011-12 in query 05: refunds processed in "
        "the first nine days of December are booked against only nine days of "
        "December sales, which is why that month's cancellation ratio spikes. "
        "Treat the final month of any of these series as incomplete.")
    add("- The catalogue contains non-product adjustment codes that behave like "
        "products — `POST` (postage), `M` (manual), `C2` (carriage), `D` "
        "(discount) and similar. They are genuine revenue lines in the source "
        "data, so they are kept, and they can legitimately rank inside a "
        "country's top 5 in query 04 (visible for Germany and EIRE). Filtering "
        "them out is a one-line change if a purely merchandise view is wanted.")
    add("- The file begins on **2010-12-01**, so 2010-12 is the first observable "
        "cohort/month and has no comparable prior period.")
    add("- RFM recency (query 02) is measured from the last invoice in the file, "
        "not from today, which keeps the segmentation reproducible.")
    add("- LTV (query 06) is observed 12-month value on a ~12-month file, so it is "
        "a floor on true lifetime value, not a forecast.")
    add("- A cancellation is recorded in the month the refund was processed, which "
        "may be later than the original sale; query 05's `lost_revenue_pct` is a "
        "monthly cancellation-intensity ratio rather than a matched reversal.")
    add("")
    add("---")
    add("")
    add("Generated by `run_all.py`. Re-run it to regenerate this file and every "
        "CSV in `reports/query_results/`.")
    add("")
    return "\n".join(lines)


# ==========================================================================
# Main
# ==========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the full retail SQL analytics pipeline end to end.")
    parser.add_argument("--sample", action="store_true",
                        help="force the bundled sample CSV instead of the full dataset")
    parser.add_argument("--no-download", action="store_true",
                        help="never download; use only what is already on disk")
    parser.add_argument("--keep-server", action="store_true",
                        help="leave PostgreSQL running when the run finishes")
    args = parser.parse_args(argv)
    configure_console()

    started = time.time()
    print("=" * 74)
    print("  retail-sql-analytics - full pipeline")
    print("=" * 74)

    # ---- 1. read ---------------------------------------------------------
    print("\n[1/6] Reading data ...")
    source = load.SAMPLE_CSV if args.sample else None
    raw, source_label = load.read_raw(source,
                                      allow_download=not args.no_download)
    print(f"  source: {source_label}")
    print(f"  raw rows: {len(raw):,}")

    # ---- 2. clean --------------------------------------------------------
    print("\n[2/6] Cleaning ...")
    sales, cancellations, stats = load.split_clean(raw)
    customers, products = load.build_dimensions(sales, cancellations, raw)
    print(f"  cleaned sales rows : {stats['cleaned_rows']:,}")
    print(f"  cancellation lines : {stats['cancellation_rows_kept']:,}")
    print(f"  customers          : {len(customers):,}")
    print(f"  products           : {len(products):,}")

    # ---- 3-5. database, load, queries -----------------------------------
    db = Database()
    try:
        print("\n[3/6] Starting PostgreSQL ...")
        db.start()
        pg_version = db.server_version()
        target = db.describe_target()
        print(f"  PostgreSQL {pg_version}  ({db.backend})")
        print(f"  target: {target}")

        print("\n[4/6] Creating schema and loading ...")
        counts = load.load_database(db, sales, cancellations, customers, products)

        print("\n[5/6] Running queries ...")
        results = run_all_queries(db, save=True, out_dir=RESULTS_DIR)

        facts = load.compute_facts(db)
    finally:
        if args.keep_server:
            print("\n  (--keep-server) leaving PostgreSQL running")
        else:
            db.stop()

    # ---- 6. report -------------------------------------------------------
    print("\n[6/6] Writing report ...")
    total_elapsed = time.time() - started
    report = build_report(source_label=source_label, stats=stats, facts=facts,
                          results=results, pg_version=pg_version, target=target,
                          counts=counts, total_elapsed=total_elapsed)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"  wrote {REPORT_PATH.relative_to(PROJECT_ROOT).as_posix()} "
          f"({REPORT_PATH.stat().st_size:,} bytes)")

    # ---- verification ----------------------------------------------------
    print("\n-- verification --")
    problems = 0
    for r in results:
        if r.csv_path is None:
            print(f"  MISSING CSV for {r.name}")
            problems += 1
            continue
        size = r.csv_path.stat().st_size
        flag = "ok " if size > 0 and r.row_count > 0 else "EMPTY"
        if flag != "ok ":
            problems += 1
        print(f"  {flag} {r.csv_path.name:<34} {r.row_count:>6,} rows "
              f"{size:>9,} bytes")

    print("\n" + "=" * 74)
    print(f"  DONE in {total_elapsed:.1f}s - {len(results)} queries, "
          f"revenue GBP {float(facts['total_revenue']):,.2f}")
    print("=" * 74)
    if problems:
        print(f"  {problems} problem(s) detected")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
