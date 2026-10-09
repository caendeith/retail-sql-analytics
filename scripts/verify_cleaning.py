"""Standalone sanity check for the cleaning rules -- no database required.

Run:  python scripts/verify_cleaning.py

It prints the headline numbers so they can be compared against the values quoted
in the README.  This is a development helper; run_all.py does not need it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import load  # noqa: E402


def main() -> int:
    raw, label = load.read_raw(allow_download=False)
    print(f"source        : {label}")
    print(f"raw rows      : {len(raw):,}")

    sales, cancels, stats = load.split_clean(raw)
    print("\n-- cleaning funnel --")
    print(f"raw rows                     : {stats['raw_rows']:,}")
    print(f"  cancellations split off    : {stats['cancellation_rows_raw']:,}")
    print(f"  dropped missing CustomerID : {stats['dropped_missing_customer']:,}")
    print(f"  dropped Quantity <= 0      : {stats['dropped_nonpositive_quantity']:,}")
    print(f"  dropped UnitPrice <= 0     : {stats['dropped_nonpositive_price']:,}")
    print(f"  dropped exact duplicates   : {stats['dropped_duplicates']:,}")
    print(f"cleaned rows                 : {stats['cleaned_rows']:,}")
    print(f"cancellation lines kept      : {stats['cancellation_rows_kept']:,}")

    print("\n-- headline facts (expect full-data values) --")
    print(f"revenue                      : {sales['revenue'].sum():,.2f}")
    print(f"customers                    : {sales['CustomerID'].nunique():,}")
    print(f"countries                    : {sales['Country'].nunique():,}")
    print(f"SKUs                         : {sales['StockCode'].nunique():,}")
    print(f"invoices                     : {sales['InvoiceNo'].nunique():,}")

    by_country = sales.groupby("Country")["revenue"].sum().sort_values(ascending=False)
    total = by_country.sum()
    print(f"UK share of revenue          : {100 * by_country.iloc[0] / total:.1f}%")
    print(f"top-5 countries share        : {100 * by_country.head(5).sum() / total:.1f}%")

    monthly = (sales.assign(month=sales["InvoiceDate"].dt.to_period("M"))
               .groupby("month")["revenue"].sum().sort_values(ascending=False))
    print(f"best month                   : {monthly.index[0]} "
          f"({monthly.iloc[0]:,.0f})")
    print(f"worst month                  : {monthly.index[-1]} "
          f"({monthly.iloc[-1]:,.0f})")

    sku = sales.groupby("StockCode")["revenue"].sum().sort_values(ascending=False)
    cum = sku.cumsum() / sku.sum()
    n80 = int((cum <= 0.80).sum()) + 1
    print(f"SKUs for 80% of revenue      : {n80} "
          f"({100 * n80 / len(sku):.1f}% of catalogue)")

    print(f"cancelled revenue (lines)    : {cancels['cancelled_revenue'].sum():,.2f}")
    print(f"cancellation invoices        : {cancels['InvoiceNo'].nunique():,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
