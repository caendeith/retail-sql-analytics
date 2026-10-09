-- ============================================================================
--  Q05 :: Cancellations and returns -- how much revenue is handed back?
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    How much revenue is lost to cancellations each month, what share of gross
--    sales does that represent, and is the return rate getting better or worse?
--
--  WHY IT MATTERS
--    Gross revenue flatters the business; net revenue pays the bills. A rising
--    cancellation ratio is the signature of a fulfilment problem, a product
--    quality problem, or a pricing/promo mistake, and it also feeds the
--    provision for refunds in the accounts. This query is the monthly control
--    chart for that metric.
--
--  TECHNIQUE
--    Two independent monthly aggregates (sales and cancellations) are FULL
--    OUTER JOINed on the month so that a month with cancellations but no sales
--    -- or the reverse -- still appears. Percentages are guarded with NULLIF.
--
--  WHY A SEPARATE cancellations TABLE
--    Returns are stored in the source data as negative quantities on invoices
--    prefixed with 'C'. The cleaning rules that make the sales table trustworthy
--    (drop Quantity <= 0) would delete every single return. So cancellations are
--    loaded into their own table with the negative quantity preserved and a
--    positive cancelled_revenue column, which keeps the revenue fact table clean
--    while making returns first-class and measurable. The invoices themselves
--    still exist in orders with is_cancelled = TRUE, so both tables join.
--
--  NOTE ON THE DATA
--    A cancellation is booked in the month the refund was processed, which can
--    be a later month than the original sale. lost_revenue_pct is therefore a
--    cancellation-intensity ratio for the month, not a matched reversal of that
--    month's sales.
-- ============================================================================

WITH sales_month AS (
    SELECT
        DATE_TRUNC('month', o.invoice_date)::date AS month_start,
        SUM(i.line_revenue)                       AS sales_revenue,
        COUNT(DISTINCT i.invoice_no)              AS sales_invoices,
        COUNT(DISTINCT o.customer_id)             AS buying_customers
    FROM order_items i
    JOIN orders o ON o.invoice_no = i.invoice_no
    WHERE NOT o.is_cancelled
    GROUP BY 1
),
cancel_month AS (
    SELECT
        DATE_TRUNC('month', c.invoice_date)::date AS month_start,
        SUM(c.cancelled_revenue)                  AS cancelled_revenue,
        COUNT(DISTINCT c.invoice_no)              AS cancelled_invoices,
        COUNT(*)                                  AS cancelled_lines,
        COUNT(DISTINCT c.customer_id)             AS cancelling_customers
    FROM cancellations c
    GROUP BY 1
),
combined AS (
    SELECT
        COALESCE(s.month_start, c.month_start)                  AS month_start,
        COALESCE(s.sales_revenue, 0)                            AS sales_revenue,
        COALESCE(s.sales_invoices, 0)                           AS sales_invoices,
        COALESCE(s.buying_customers, 0)                         AS buying_customers,
        COALESCE(c.cancelled_revenue, 0)                        AS cancelled_revenue,
        COALESCE(c.cancelled_invoices, 0)                       AS cancelled_invoices,
        COALESCE(c.cancelled_lines, 0)                          AS cancelled_lines,
        COALESCE(c.cancelling_customers, 0)                     AS cancelling_customers
    FROM sales_month s
    FULL OUTER JOIN cancel_month c ON c.month_start = s.month_start
)
SELECT
    TO_CHAR(month_start, 'YYYY-MM')                             AS month,
    ROUND(sales_revenue, 2)                                     AS sales_revenue,
    ROUND(cancelled_revenue, 2)                                 AS cancelled_revenue,
    ROUND(sales_revenue - cancelled_revenue, 2)                 AS net_revenue,
    sales_invoices,
    cancelled_invoices,
    cancelled_lines,
    cancelling_customers,
    ROUND(100.0 * cancelled_revenue
          / NULLIF(sales_revenue, 0), 2)                        AS lost_revenue_pct,
    ROUND(100.0 * cancelled_invoices
          / NULLIF(sales_invoices, 0), 2)                       AS cancellation_rate_pct,
    ROUND(100.0 * cancelled_revenue
          / NULLIF(SUM(cancelled_revenue) OVER (), 0), 2)       AS pct_of_all_cancellations,
    ROUND(cancelled_revenue
          / NULLIF(cancelled_lines, 0), 2)                      AS avg_cancelled_line_value
FROM combined
ORDER BY month_start;
