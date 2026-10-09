# Retail SQL Analytics

**Six production-shaped analytical SQL queries — RFM, cohort retention, LTV, ABC, cancellation and
growth analysis — running against a real PostgreSQL 16 instance, with every number in this README
produced by an actual run of `python run_all.py`.**

No Docker required. No database server to install. `python run_all.py` starts its own PostgreSQL 16,
creates the schema, loads the data, runs all six queries, writes the report and shuts down again.

---

## 1. Business framing

The dataset is a UK-based online giftware wholesaler: roughly one year of transactions, ~4,300
customers and ~3,700 SKUs across 37 countries. It is a real business with real problems, and the six
queries here are the questions its commercial team would actually be asking:

| Question the business has | Query that answers it |
| --- | --- |
| Are we growing, and when does the year turn? | `01_monthly_revenue.sql` |
| Who are our best customers, and who is about to leave? | `02_rfm_segmentation.sql` |
| Do new customers come back, and is that improving? | `03_cohort_retention.sql` |
| What does each country actually buy? | `04_top_products_by_country.sql` |
| How much revenue do we hand back in refunds? | `05_cancellations.sql` |
| How dependent are we on a handful of accounts? | `06_customer_lifetime_value.sql` |

The analysis deliberately goes past `GROUP BY` + `SUM`: every query uses at least one window function
or a multi-step CTE, because that is the level at which retail analytics questions stop being
answerable with simple aggregation.

### What the run actually found

These are real results from the run documented in [`reports/sql_report.md`](reports/sql_report.md):

- **£8,887,208.89** of revenue across **392,692** cleaned line items, **18,532** invoices and
  **4,338** buying customers.
- Revenue is **extremely seasonal**: November 2011 peaks at **£1,156,205.61**, February 2011 troughs
  at **£446,084.92** — a 2.59× swing between the best and worst month.
- Revenue is **extremely concentrated**: the top **1%** of customers generate **31.8%** of all
  revenue; the top 10% generate **61.5%**. The single largest account is worth **£280,206.02**, or
  **3.15%** of the entire business.
- **942 customers (21.7% of the base) are "Champions"** and produce **64.5%** of revenue.
- **776 SKUs — 21.2% of the catalogue — carry 80% of revenue** (classic Pareto/ABC shape).
- Cancellations give back **£893,979.73**, about **10.1%** of gross sales.
- The UK is **82.0%** of revenue; the top 5 countries are **93.1%**.

---

## 2. The data

**Source:** [UCI Machine Learning Repository — Online Retail (ID 352)](https://archive.ics.uci.edu/dataset/352/online+retail)

| Property | Value |
| --- | --- |
| Raw rows | 541,909 |
| Cleaned rows loaded into `order_items` | 392,692 |
| Period covered | 2010-12-01 → 2011-12-09 |
| Countries | 37 |
| Distinct SKUs | 3,665 |
| Buying customers | 4,338 |
| Invoices | 18,532 |
| Gross revenue | £8,887,208.89 |

Columns: `InvoiceNo, StockCode, Description, Quantity, InvoiceDate, UnitPrice, CustomerID, Country`.

### Cleaning rules

Applied in `src/load.py` while loading, in this order:

1. Drop rows with a missing `CustomerID` — an unattributable line cannot support customer analytics.
2. Drop cancellations (`InvoiceNo` starts with `C`).
3. Drop `Quantity <= 0`.
4. Drop `UnitPrice <= 0` — price adjustments, not sales.
5. Drop exact duplicate rows.
6. `revenue = Quantity * UnitPrice`.

The measured effect of each rule on the full dataset:

| Step | Rows |
| --- | ---: |
| Raw rows read | 541,909 |
| — of which cancellation rows | 9,288 |
| Dropped: missing `CustomerID` | −134,697 |
| Dropped: `Quantity <= 0` | −0 |
| Dropped: `UnitPrice <= 0` | −40 |
| Dropped: exact duplicates | −5,192 |
| **Cleaned rows loaded** | **392,692** |

> `Quantity <= 0` removes **zero** rows on the sales side precisely because cancellations were
> already split off in step 2 — every negative quantity in this dataset is a return. That ordering
> matters, and it is why cancellations must be handled deliberately rather than deleted.

### Why cancellations get their own table

This is the one genuine design decision in the schema, so it is worth being explicit.

Returns in this dataset are stored as **negative quantities on invoices prefixed `C`**. That puts the
cleaning rules in direct conflict: rule 3 (`Quantity <= 0`) would delete **all 9,288** cancellation
rows, and rule 2 exists specifically to remove them. If cancellations were simply dropped, query 05
would have nothing to analyse — the returns would be invisible.

The resolution is to **load both**, along three deliberate choices:

1. **`cancellations` is a separate table** holding line-level return detail, with the negative
   `quantity` preserved exactly as received and a positive `cancelled_revenue` column so "lost money"
   can be summed directly without sign gymnastics. `order_items` stays a pure revenue fact table.
2. **`orders` contains *both* kinds of invoice**, distinguished by `is_cancelled`. So
   `orders` = 18,532 sales invoices + 3,836 cancellation invoices = **22,368** rows, and query 05 can
   join returns to a real order header. The `is_cancelled` flag lets every revenue query exclude
   returns with one predicate.
3. **`cancellations` has no foreign key to `products`.** Cancellation lines occasionally reference
   stock codes that never appear in a sale (postage, manual adjustments, damaged stock). Enforcing
   that FK would reject legitimate return data, so the constraint is intentionally omitted.

Cancellation rows are kept when they carry a positive unit price and a stock code; rows without a
`CustomerID` are retained (182 cancellation invoices have none), which is why
`orders.customer_id` and `cancellations.customer_id` are nullable.

---

## 3. Schema

```
                        ┌──────────────────────┐
                        │      customers       │
                        │──────────────────────│
                        │ customer_id  INTEGER │ PK
                        │ country      TEXT    │
                        └──────────┬───────────┘
                                   │ 1
                                   │
                                   │ N
   ┌───────────────────────┐   ┌───┴──────────────────────┐
   │       products        │   │         orders           │
   │───────────────────────│   │──────────────────────────│
   │ stock_code   TEXT   PK│   │ invoice_no    TEXT     PK│
   │ description  TEXT     │   │ customer_id   INTEGER  FK│→ customers (NULLable)
   │ median_price NUMERIC  │   │ invoice_date  TIMESTAMP  │
   └──────────┬────────────┘   │ is_cancelled  BOOLEAN    │
              │ 1              │ country       TEXT       │
              │                └───┬──────────────────┬───┘
              │ N                  │ 1                │ 1
   ┌──────────┴────────────┐   ┌───┴──────────────────┴───┐
   │      order_items      │   │      cancellations       │
   │───────────────────────│   │──────────────────────────│
   │ order_item_id BIGSERIAL│PK │ id            BIGSERIAL PK│
   │ invoice_no    TEXT   FK│   │ invoice_no    TEXT     FK│→ orders
   │ stock_code    TEXT   FK│   │ stock_code    TEXT       │  (no FK: see §2)
   │ quantity      INTEGER  │   │ description   TEXT       │
   │ unit_price    NUMERIC  │   │ customer_id   INTEGER  FK│→ customers (NULLable)
   │ line_revenue  NUMERIC  │   │ invoice_date  TIMESTAMP  │
   └────────────────────────┘   │ quantity      INTEGER    │  ← negative, as received
                                │ unit_price    NUMERIC    │
   revenue fact table           │ cancelled_revenue NUMERIC│  ← positive magnitude
   (cleaned sales only)         └──────────────────────────┘
                                return detail (never in order_items)
```

**Table notes**

- **`customers`** — 4,372 rows, built from *every* raw row carrying a `CustomerID`, not just cleaned
  sales. That is 34 more than the 4,338 customers who actually bought: those 34 appear only on a
  cancellation or on a row dropped by the cleaning rules. Including them keeps every foreign key
  valid. `country` is the customer's most frequent country.
- **`products`** — 3,665 rows. `median_price` is the **median**, not the mean, so bulk/wholesale
  lines cannot skew the reference price.
- **`orders`** — 22,368 rows, one per invoice, both sales and cancellations.
- **`order_items`** — 392,692 rows. Uses a surrogate `BIGSERIAL` key rather than
  `(invoice_no, stock_code)`: the same product can legitimately appear on two lines of one invoice
  when it was re-priced.
- **`cancellations`** — 9,251 rows of return detail.

**Indexes:** `orders(invoice_date)`, `orders(customer_id)`, `orders(is_cancelled)`,
`order_items(invoice_no)`, `order_items(stock_code)`, `cancellations(invoice_date)`,
`cancellations(customer_id)`. All five tables are `ANALYZE`d after the bulk load so the planner has
fresh statistics.

---

## 4. How to run

### Prerequisites

Python 3.11+ and the pinned dependencies:

```bash
pip install -r requirements.txt
```

### Primary path — no Docker, no database server (`pgserver`)

```bash
python run_all.py
```

That single command:

1. reads the full dataset if present, else the bundled sample (and prints which it used),
2. applies the six cleaning rules,
3. starts a **real PostgreSQL 16.2** server via `pgserver`, with its data directory in `.pgdata/`
   (git-ignored),
4. creates the schema from `src/schema.sql` and bulk-loads all five tables with `COPY`,
5. runs all six queries, saving each result to `reports/query_results/NN_*.csv`,
6. writes `reports/sql_report.md` from the real query output,
7. shuts the server down and prints a verification table.

Expected output (this is the actual tail of a real run):

```
[3/6] Starting PostgreSQL ...
  PostgreSQL 16.2  (embedded pgserver (D:\dev ent\caendeith_readme\retail-sql-analytics\.pgdata))
  target: postgres @ 127.0.0.1:61605

[4/6] Creating schema and loading ...
  loaded in 15.7s
    customers          4,372 rows
    products           3,665 rows
    orders            22,368 rows
    order_items      392,692 rows
    cancellations      9,251 rows

[5/6] Running queries ...

-- running 6 queries --
  ok   01_monthly_revenue               13 rows x  9 cols   0.76s  -> 01_monthly_revenue.csv
  ok   02_rfm_segmentation           4,338 rows x 14 cols   0.66s  -> 02_rfm_segmentation.csv
  ok   03_cohort_retention              91 rows x  6 cols   0.12s  -> 03_cohort_retention.csv
  ok   04_top_products_by_country      197 rows x 10 cols   2.88s  -> 04_top_products_by_country.csv
  ok   05_cancellations                 13 rows x 12 cols   0.49s  -> 05_cancellations.csv
  ok   06_customer_lifetime_value    4,338 rows x 14 cols   0.66s  -> 06_customer_lifetime_value.csv

[6/6] Writing report ...
  wrote reports/sql_report.md (22,980 bytes)

-- verification --
  ok  01_monthly_revenue.csv                 13 rows       987 bytes
  ok  02_rfm_segmentation.csv             4,338 rows   307,364 bytes
  ok  03_cohort_retention.csv                91 rows     2,651 bytes
  ok  04_top_products_by_country.csv        197 rows    14,360 bytes
  ok  05_cancellations.csv                   13 rows     1,218 bytes
  ok  06_customer_lifetime_value.csv      4,338 rows   394,998 bytes

==========================================================================
  DONE in 38.5s - 6 queries, revenue GBP 8,887,208.89
==========================================================================
```

Runtime is roughly **25–40 seconds** end to end on the full dataset. The first run additionally
spends ~30s converting the 23 MB `Online Retail.xlsx` to a cached CSV; later runs reuse the cache.

### Useful flags

```bash
python run_all.py --sample        # use the bundled 55,177-row sample instead
python run_all.py --no-download   # never touch the network
python run_all.py --keep-server   # leave PostgreSQL running (debugging)
python run_queries.py             # re-run only the SQL against the loaded database
python scripts/verify_cleaning.py # print cleaning + headline numbers, no database at all
```

### Alternative path — Docker

For users who already have Docker, a PostgreSQL 16 service is provided. **This is not the primary
path**; the `pgserver` route above needs no Docker at all.

```bash
docker compose up -d
# wait for "database system is ready to accept connections"

# Linux/macOS
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/retail
# Windows (cmd)
set DATABASE_URL=postgresql://postgres:postgres@localhost:5432/retail

python run_all.py
```

`src/db.py` reads `DATABASE_URL` first and skips `pgserver` entirely when it is set, so both paths
run the identical pipeline. If port 5432 is already taken, change the mapping in
`docker-compose.yml` (e.g. `"5433:5432"`) and update `DATABASE_URL` to match.

### Optional — `make`

`make` is **not** required and not installed on the machine this was built on. A `Makefile` is
included purely as a convenience wrapper for those who have it:

```bash
make all       # == python run_all.py
make sample    # == python run_all.py --sample --no-download
make verify    # == python scripts/verify_cleaning.py
make clean     # remove caches and .pgdata
```

---

## 5. The six queries

| # | File | What it shows | Why the business cares | SQL techniques |
| --- | --- | --- | --- | --- |
| 01 | [`01_monthly_revenue.sql`](sql/01_monthly_revenue.sql) | Monthly revenue, cumulative running total, month-over-month growth %, invoices, active customers, units, average invoice value | The running total says whether the year is on pace; the MoM swing is the earliest warning that a season, category or market is turning. This is the exec dashboard shape. | `SUM() OVER (ORDER BY … ROWS UNBOUNDED PRECEDING)`, `LAG()`, CTE |
| 02 | [`02_rfm_segmentation.sql`](sql/02_rfm_segmentation.sql) | Every customer scored 1–5 on Recency, Frequency and Monetary value, combined into a named segment with revenue share | Turns a flat customer list into a targeting list. Champions get loyalty treatment, high-value-but-lapsed customers get a win-back campaign before they are gone, and Hibernating customers get suppressed from expensive channels. | `NTILE(5)`, CTEs, `RANK()`, `SUM() OVER ()` |
| 03 | [`03_cohort_retention.sql`](sql/03_cohort_retention.sql) | Retention triangle: for each first-purchase month, how many of that cohort returned in month 0, 1, 2 … 12 | Separates "leaky bucket" from "growth problem". Comparing the 2010-12 cohort against 2011-06 shows whether product or service changes actually improved repeat behaviour. Feeds LTV forecasting. | CTEs, `DATE_TRUNC`, self-join, integer year/month arithmetic |
| 04 | [`04_top_products_by_country.sql`](sql/04_top_products_by_country.sql) | Top 5 products in every country, with each product's share of that country's revenue | A global top-10 is dominated by the UK and hides what local markets want. This tells merchandising what to stock per market, marketing which hero product to lead with, and flags markets dependent on a single SKU. | `RANK() OVER (PARTITION BY country …)`, `SUM() OVER (PARTITION BY …)` |
| 05 | [`05_cancellations.sql`](sql/05_cancellations.sql) | Monthly cancelled revenue, net revenue after returns, cancellation rate, share of all cancellations, average cancelled line value | Gross revenue flatters the business; net revenue pays the bills. A rising cancellation ratio signals a fulfilment, quality or pricing problem, and feeds the refund provision in the accounts. | `FULL OUTER JOIN`, aggregates, `NULLIF` guards, `SUM() OVER ()` |
| 06 | [`06_customer_lifetime_value.sql`](sql/06_customer_lifetime_value.sql) | Per-customer LTV, decile, revenue share, cumulative revenue share, order count, lifespan, LTV/month | The concentration-risk view. If the top decile is most of revenue, losing a few accounts is existential. The decile curve also sets realistic CAC ceilings — you cannot pay more to acquire a customer than their decile's LTV. | `NTILE(10)`, `RANK()`, `SUM() OVER ()`, running `SUM() OVER (ORDER BY …)` |

---

## 6. Real example output

All tables below are copied verbatim from the generated
[`reports/sql_report.md`](reports/sql_report.md) — actual rows returned by the queries.

### Query 01 — monthly revenue, running total and growth

| month | revenue | invoices | active_customers | units_sold | running_total | prev_month_revenue | mom_growth_pct | avg_invoice_value |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2010-12 | 570,422.73 | 1,400 | 885 | 311,048 | 570,422.73 |  |  | 407.44 |
| 2011-01 | 568,101.31 | 987 | 741 | 348,473 | 1,138,524.04 | 570,422.73 | -0.40 | 575.58 |
| 2011-02 | 446,084.92 | 997 | 758 | 265,027 | 1,584,608.96 | 568,101.31 | -21.50 | 447.43 |
| 2011-03 | 594,081.76 | 1,321 | 974 | 347,582 | 2,178,690.72 | 446,084.92 | 33.20 | 449.72 |
| 2011-04 | 468,374.33 | 1,149 | 856 | 291,366 | 2,647,065.05 | 594,081.76 | -21.20 | 407.64 |
| 2011-05 | 677,355.15 | 1,555 | 1,056 | 372,864 | 3,324,420.20 | 468,374.33 | 44.60 | 435.60 |
| 2011-06 | 660,046.05 | 1,393 | 991 | 363,014 | 3,984,466.25 | 677,355.15 | -2.60 | 473.83 |
| 2011-07 | 598,962.90 | 1,331 | 949 | 367,360 | 4,583,429.15 | 660,046.05 | -9.30 | 450.01 |
| 2011-08 | 644,051.04 | 1,280 | 935 | 397,373 | 5,227,480.19 | 598,962.90 | 7.50 | 503.16 |
| 2011-09 | 950,690.20 | 1,755 | 1,266 | 543,652 | 6,178,170.39 | 644,051.04 | 47.60 | 541.70 |
| 2011-10 | 1,035,642.45 | 1,929 | 1,364 | 591,543 | 7,213,812.84 | 950,690.20 | 8.90 | 536.88 |
| 2011-11 | 1,156,205.61 | 2,657 | 1,664 | 665,923 | 8,370,018.45 | 1,035,642.45 | 11.60 | 435.15 |
| 2011-12 | 517,190.44 | 778 | 615 | 286,777 | 8,887,208.89 | 1,156,205.61 | -55.30 | 664.77 |

> 13 rows returned. Read the November build-up and the January/February trough — that is the
> Christmas shape of a giftware wholesaler.

### Query 02 — RFM segments (roll-up of the 4,338 per-customer rows)

| segment | customers | avg_recency_days | avg_frequency | avg_ltv | total_revenue | pct_of_revenue | pct_of_customers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Champions | 942 | 12 | 11.20 | 6,082.25 | 5,729,483.37 | 64.50 | 21.70 |
| Loyal | 1,001 | 33 | 3.80 | 1,511.56 | 1,513,070.63 | 17.00 | 23.10 |
| At Risk | 735 | 167 | 2.20 | 1,047.37 | 769,817.21 | 8.70 | 16.90 |
| At Risk (high value) | 172 | 124 | 5.50 | 2,237.13 | 384,786.46 | 4.30 | 4.00 |
| Hibernating | 829 | 226 | 1.00 | 228.92 | 189,776.40 | 2.10 | 19.10 |
| Needs Attention | 350 | 52 | 1.20 | 467.55 | 163,641.50 | 1.80 | 8.10 |
| New / Promising | 309 | 18 | 1.20 | 442.18 | 136,633.32 | 1.50 | 7.10 |

Top customers by spend, exactly as returned:

| customer_id | segment | rfm_cell | recency_days | frequency | monetary | avg_order_value | pct_of_revenue | revenue_rank |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 14,646 | Champions | 555 | 1 | 73 | 280,206.02 | 3,838.44 | 3.15 | 1 |
| 18,102 | Champions | 555 | 0 | 60 | 259,657.30 | 4,327.62 | 2.92 | 2 |
| 17,450 | Champions | 555 | 8 | 46 | 194,390.79 | 4,225.89 | 2.19 | 3 |
| 16,446 | Loyal | 535 | 0 | 2 | 168,472.50 | 84,236.25 | 1.90 | 4 |
| 14,911 | Champions | 555 | 1 | 201 | 143,711.17 | 714.98 | 1.62 | 5 |

### Query 03 — cohort retention (first rows of the 91-row triangle)

| cohort_month | month_index | cohort_size | active_customers | retention_pct | pct_of_largest_cohort |
| --- | --- | --- | --- | --- | --- |
| 2010-12 | 0 | 885 | 885 | 100.00 | 100.00 |
| 2010-12 | 1 | 885 | 324 | 36.60 | 36.60 |
| 2010-12 | 2 | 885 | 286 | 32.30 | 32.30 |
| 2010-12 | 3 | 885 | 340 | 38.40 | 38.40 |
| 2010-12 | 4 | 885 | 321 | 36.30 | 36.30 |
| 2011-01 | 0 | 417 | 417 | 100.00 | 47.10 |
| 2011-01 | 1 | 417 | 92 | 22.10 | 10.40 |
| 2011-02 | 0 | 380 | 380 | 100.00 | 42.90 |
| 2011-02 | 1 | 380 | 71 | 18.70 | 8.00 |

> The 2010-12 cohort holds ~33–40% month after month, while the 2011-01 and 2011-02 cohorts retain
> only ~19–22% in month 1. Retention of newer cohorts is meaningfully weaker — the kind of finding
> that is invisible without a cohort view.

### Query 04 — top products per country (first rows of 197)

| country | product_rank | stock_code | description | revenue | units_sold | invoices | pct_of_country | country_revenue | distinct_skus |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| United Kingdom | 1 | 23843 | PAPER CRAFT , LITTLE BIRDIE | 168,469.60 | 80,995 | 1 | 2.31 | 7,285,024.64 | 3,645 |
| United Kingdom | 2 | 22423 | REGENCY CAKESTAND 3 TIER | 110,713.00 | 9,617 | 1,410 | 1.52 | 7,285,024.64 | 3,645 |
| United Kingdom | 3 | 85123A | WHITE HANGING HEART T-LIGHT HOLDER | 94,960.85 | 34,687 | 1,891 | 1.30 | 7,285,024.64 | 3,645 |
| United Kingdom | 4 | 23166 | MEDIUM CERAMIC TOP STORAGE JAR | 80,291.44 | 76,919 | 173 | 1.10 | 7,285,024.64 | 3,645 |
| United Kingdom | 5 | 85099B | JUMBO BAG RED RETROSPOT | 77,191.33 | 41,878 | 1,447 | 1.06 | 7,285,024.64 | 3,645 |
| Netherlands | 1 | 23084 | RABBIT NIGHT LIGHT | 9,568.48 | 4,801 | 7 | 3.35 | 285,446.34 | 782 |
| Netherlands | 2 | 22326 | ROUND SNACK BOXES SET OF4 WOODLAND | 7,991.40 | 3,132 | 25 | 2.80 | 285,446.34 | 782 |
| EIRE | 1 | 22423 | REGENCY CAKESTAND 3 TIER | 7,337.55 | 641 | 62 | 2.77 | 265,262.46 | 1,943 |
| EIRE | 2 | C2 | CARRIAGE | 4,875.00 | 98 | 98 | 1.84 | 265,262.46 | 1,943 |
| Germany | 1 | POST | POSTAGE | 21,001.00 | 1,114 | 374 | 9.18 | 228,678.40 | 1,664 |

> Note the two data-quality artefacts visible here, both of which are real and both of which the
> README flags rather than hides: `23843` is the UK's top product on the strength of a **single**
> 80,995-unit invoice (a one-off bulk order, not sustained demand), and non-product codes such as
> `C2` (carriage) and `POST` (postage) rank inside EIRE's and Germany's top 5.

### Query 05 — cancellations and lost revenue

| month | sales_revenue | cancelled_revenue | net_revenue | sales_invoices | cancelled_invoices | lost_revenue_pct | cancellation_rate_pct | pct_of_all_cancellations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2010-12 | 570,422.73 | 74,729.12 | 495,693.61 | 1,400 | 326 | 13.10 | 23.29 | 8.36 |
| 2011-01 | 568,101.31 | 131,363.05 | 436,738.26 | 987 | 260 | 23.12 | 26.34 | 14.69 |
| 2011-02 | 446,084.92 | 25,519.15 | 420,565.77 | 997 | 219 | 5.72 | 21.97 | 2.85 |
| … | | | | | | | | |
| 2011-09 | 950,690.20 | 38,838.51 | 911,851.69 | 1,755 | 333 | 4.09 | 18.97 | 4.34 |
| 2011-10 | 1,035,642.45 | 81,895.50 | 953,746.95 | 1,929 | 362 | 7.91 | 18.77 | 9.16 |
| 2011-11 | 1,156,205.61 | 47,720.98 | 1,108,484.63 | 2,657 | 441 | 4.13 | 16.60 | 5.34 |
| 2011-12 | 517,190.44 | 205,089.27 | 312,101.17 | 778 | 146 | 39.65 | 18.77 | 22.94 |

> January 2011 is the worst real month (23.12% of sales returned — the post-Christmas returns wave).
> December 2011's 39.65% is a **truncation artefact**: the file stops on 2011-12-09, so nine days of
> sales carry a full month's refunds. Full 13-row output is in the report and CSV.

### Query 06 — LTV deciles (roll-up of the 4,338 per-customer rows)

| ltv_decile | customers | revenue | avg_ltv | avg_orders | min_ltv | max_ltv | pct_of_revenue | cumulative_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 434 | 5,461,374.89 | 12,583.81 | 16.60 | 3,641.24 | 280,206.02 | 61.45 | 61.45 |
| 2 | 434 | 1,175,925.93 | 2,709.51 | 6.90 | 2,055.51 | 3,640.67 | 13.23 | 74.68 |
| 3 | 434 | 725,127.48 | 1,670.80 | 4.80 | 1,346.97 | 2,054.36 | 8.16 | 82.84 |
| 4 | 434 | 489,266.69 | 1,127.34 | 3.70 | 933.26 | 1,345.62 | 5.51 | 88.35 |
| 5 | 434 | 342,309.30 | 788.73 | 2.90 | 668.56 | 932.97 | 3.85 | 92.20 |
| 6 | 434 | 251,250.14 | 578.92 | 2.30 | 487.02 | 668.43 | 2.83 | 95.03 |
| 7 | 434 | 178,812.85 | 412.01 | 1.70 | 349.55 | 486.82 | 2.01 | 97.04 |
| 8 | 434 | 131,201.05 | 302.31 | 1.50 | 248.61 | 349.27 | 1.48 | 98.52 |
| 9 | 433 | 86,192.65 | 199.06 | 1.30 | 155.17 | 248.10 | 0.97 | 99.49 |
| 10 | 433 | 45,747.91 | 105.65 | 1.10 | 3.75 | 155.05 | 0.51 | 100.00 |

> The top decile is **61.45%** of revenue. The bottom decile's entire lifetime value (avg £105.65) is
> less than the top decile's average order value — the acquisition budget cannot be the same for
> both.

---

## 7. Project structure

```
retail-sql-analytics/
├── run_all.py                        # end-to-end pipeline (the primary entry point)
├── run_queries.py                    # execute sql/*.sql → reports/query_results/*.csv
├── requirements.txt                  # pinned dependencies
├── docker-compose.yml                # optional PostgreSQL 16 (not the primary path)
├── Makefile                          # optional convenience wrapper (not the primary path)
├── LICENSE                           # MIT
├── .gitignore                        # ignores data/raw/, .pgdata/, __pycache__/
├── src/
│   ├── schema.sql                    # DDL: 5 tables, FKs, indexes
│   ├── db.py                         # connection layer (pgserver or DATABASE_URL)
│   └── load.py                       # read → clean → COPY into PostgreSQL
├── sql/
│   ├── 01_monthly_revenue.sql        # window functions: SUM() OVER, LAG()
│   ├── 02_rfm_segmentation.sql       # NTILE(5), CTEs
│   ├── 03_cohort_retention.sql       # CTEs, date_trunc, self-join
│   ├── 04_top_products_by_country.sql# RANK() OVER (PARTITION BY …)
│   ├── 05_cancellations.sql          # aggregates, FULL OUTER JOIN
│   └── 06_customer_lifetime_value.sql# NTILE(10), RANK(), running SUM()
├── scripts/
│   └── verify_cleaning.py            # cleaning check, no database needed
├── data/
│   ├── sample/online_retail_sample.csv   # 55,177 rows, committed (offline fallback)
│   └── raw/                              # git-ignored; full dataset lives here
└── reports/
    ├── sql_report.md                 # generated report with real query output
    └── query_results/                # generated CSVs, one per query
        ├── 01_monthly_revenue.csv
        ├── 02_rfm_segmentation.csv
        ├── 03_cohort_retention.csv
        ├── 04_top_products_by_country.csv
        ├── 05_cancellations.csv
        └── 06_customer_lifetime_value.csv
```

---

## 8. Tech stack

| Component | Choice | Why |
| --- | --- | --- |
| Database | **PostgreSQL 16.2** | Real window functions, CTEs, `date_trunc`, `FILTER`, `NTILE` — the queries are written for a genuine analytical engine, not SQLite. |
| Local database | **`pgserver` 0.1.4** | Ships an actual PostgreSQL binary as a Python package. `python run_all.py` works on a clean machine with no Docker, no installer and no service — which matters for a reviewer who just wants to clone and run. |
| Driver | **`psycopg2-binary` 2.9.13** | `COPY … FROM STDIN` for the bulk load (392k rows in ~13s) plus `RealDictCursor` for named-column results. |
| Data handling | **pandas 3.0.6**, **numpy 2.1.0** | Cleaning and the report's roll-up tables. |
| Excel reading | **openpyxl** | The UCI source ships as `.xlsx`; results are cached to CSV so only the first run pays the cost. |
| ORM | **SQLAlchemy 2.0.35** | Declared as a dependency for anyone who wants an engine on top of the same database; the shipped pipeline uses psycopg2 directly to keep `COPY` simple. |

---

## 9. What could be improved

**Data quality**

1. **Separate true merchandise from adjustment codes.** `POST`, `M`, `C2`, `D`, `BANK CHARGES` and
   similar currently rank as "products" (visible in query 04 for Germany and EIRE). A
   `product_type` column with `merchandise` / `adjustment` / `postage` would let every query filter
   them consistently in one place instead of per-query.
2. **Handle the one-off bulk order.** UK's #1 product by revenue is a single 80,995-unit invoice.
   Flagging outlier invoices (or reporting median alongside sum) would stop a one-off order from
   reading as sustained demand.
3. **Investigate negative-priced and zero-quantity rows properly.** They are dropped here, but they
   may encode stock write-offs and adjustments that deserve their own table rather than deletion.
4. **De-duplicate more carefully.** `drop_duplicates()` on all eight columns is conservative; genuine
   repeat line items (same customer buying the same thing twice in one order) are indistinguishable
   from data errors. A composite-key + timestamp-tolerance rule would be more principled.

**Modelling**

5. **Match cancellations to their original sale.** Returns are currently booked in the month the
   refund was processed, which inflates the final partial month. Joining each `C` invoice back to the
   original purchase — by customer + stock code + approximate date — would give true net revenue by
   *sale* month and a real return-rate cohort.
6. **Make recency genuinely time-relative.** RFM uses the last invoice date in the file so the
   analysis is reproducible, but a real deployment should use `CURRENT_DATE` with a configurable
   snapshot.
7. **Add a calendar/dimension table.** Month-index arithmetic is done inline in query 03; a proper
   `dim_date` with fiscal periods would simplify cohort SQL and support fiscal-year reporting.
8. **Model LTV, don't just observe it.** Query 06 reports observed 12-month value on a 12-month
   dataset. BG/NBD or a survival model would forecast beyond the observation window and give a real
   CAC ceiling per decile.

**Engineering**

9. **Incremental loading.** The pipeline is a full reload. A `loaded_at` column plus an upsert on
   `invoice_no` would make daily incremental loads possible.
10. **Materialise the expensive steps.** Query 04 takes 2.5s because it aggregates the full fact table
    per country/product. A materialised view refreshed after load would make dashboards responsive.
11. **Partition `order_items` by month.** At 392k rows this is unnecessary; at 100M it is essential.
12. **Add tests.** There is no automated test suite — correctness is currently evidenced by the
    verification step in `run_all.py` and the checked-in report. `pytest` assertions on row counts,
    revenue totals and cleaning-funnel numbers would catch regressions.
13. **Pin the PostgreSQL version.** `pgserver` currently resolves to 16.2; pinning it explicitly
    would guarantee identical planner behaviour across machines.

---

## 10. License and attribution

Code released under the [MIT License](LICENSE) © Damir Ziatdinov.

Dataset: *Online Retail*, UCI Machine Learning Repository
([dataset 352](https://archive.ics.uci.edu/dataset/352/online+retail)), used under CC BY 4.0.
The bundled `data/sample/online_retail_sample.csv` is a subset of that dataset, committed so the
project runs fully offline.
