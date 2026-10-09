-- ============================================================================
--  Q01 :: Monthly revenue, running total and month-over-month growth
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    How is revenue trending month by month, how much has the business booked
--    cumulatively, and which months grew or shrank versus the month before?
--
--  WHY IT MATTERS
--    The running total shows whether the year is on pace against target; the
--    month-over-month percentage is the earliest warning signal that a
--    category, a season or a market is turning. Finance, merchandising and
--    the exec dashboard all consume this shape.
--
--  TECHNIQUE
--    CTE to aggregate at month grain, then SUM() OVER (ORDER BY ... ROWS
--    UNBOUNDED PRECEDING) for the running total and LAG() for the prior month.
--
--  NOTE ON THE DATA
--    The source file ends on 2011-12-09, so the final month (2011-12) is a
--    partial month and its negative growth is an artefact of truncation, not a
--    real collapse in demand. 2010-12 is likewise the first month on file.
-- ============================================================================

WITH monthly AS (
    SELECT
        DATE_TRUNC('month', o.invoice_date)::date      AS month_start,
        TO_CHAR(DATE_TRUNC('month', o.invoice_date), 'YYYY-MM') AS month,
        SUM(i.line_revenue)                            AS revenue,
        COUNT(DISTINCT i.invoice_no)                   AS invoices,
        COUNT(DISTINCT o.customer_id)                  AS active_customers,
        SUM(i.quantity)                                AS units_sold
    FROM order_items i
    JOIN orders o ON o.invoice_no = i.invoice_no
    WHERE NOT o.is_cancelled
    GROUP BY 1, 2
)
SELECT
    month,
    ROUND(revenue, 2)                                   AS revenue,
    invoices,
    active_customers,
    units_sold,
    ROUND(SUM(revenue) OVER (ORDER BY month_start
                             ROWS UNBOUNDED PRECEDING), 2) AS running_total,
    ROUND(LAG(revenue) OVER (ORDER BY month_start), 2)  AS prev_month_revenue,
    ROUND(
        100.0 * (revenue - LAG(revenue) OVER (ORDER BY month_start))
        / NULLIF(LAG(revenue) OVER (ORDER BY month_start), 0)
    , 1)                                                AS mom_growth_pct,
    ROUND(revenue / NULLIF(invoices, 0), 2)             AS avg_invoice_value
FROM monthly
ORDER BY month_start;
