-- ============================================================================
--  Q06 :: Customer lifetime value -- distribution and top customers
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    How is lifetime value distributed across the customer base?  How few
--    customers produce most of the revenue, and who exactly are the top ones?
--
--  WHY IT MATTERS
--    This is the concentration-risk and account-management view. If the top
--    decile of customers is a large share of revenue, losing a handful of
--    accounts is an existential risk, and the named top accounts deserve
--    dedicated service. The decile curve also sets realistic CAC ceilings:
--    you cannot pay more to acquire a customer than their decile's LTV.
--
--  TECHNIQUE
--    Per-customer aggregation in a CTE, then four window functions in one pass:
--    NTILE(10) for the decile, RANK() for the leaderboard, SUM() OVER () for
--    the share of total revenue, and a running SUM() OVER (ORDER BY ltv DESC)
--    to give the cumulative revenue share -- the number that answers "how many
--    customers do we actually depend on?".
--
--  NOTE ON THE DATA
--    The file covers roughly one year, so this is observed 12-month value, not
--    a modelled multi-year LTV. It is a floor, not a forecast.
-- ============================================================================

WITH customer_ltv AS (
    SELECT
        o.customer_id,
        MAX(o.country)                            AS country,
        MIN(o.invoice_date)::date                 AS first_order_date,
        MAX(o.invoice_date)::date                 AS last_order_date,
        (MAX(o.invoice_date)::date
            - MIN(o.invoice_date)::date)          AS lifespan_days,
        COUNT(DISTINCT o.invoice_no)              AS orders,
        SUM(i.quantity)                           AS units,
        SUM(i.line_revenue)                       AS ltv
    FROM order_items i
    JOIN orders o ON o.invoice_no = i.invoice_no
    WHERE NOT o.is_cancelled
      AND o.customer_id IS NOT NULL
    GROUP BY o.customer_id
),
scored AS (
    SELECT
        *,
        RANK()  OVER (ORDER BY ltv DESC)              AS ltv_rank,
        NTILE(10) OVER (ORDER BY ltv DESC, customer_id) AS ltv_decile,
        ROUND(100.0 * ltv / NULLIF(SUM(ltv) OVER (), 0), 4) AS pct_of_total_revenue,
        ROUND(
            100.0 * SUM(ltv) OVER (ORDER BY ltv DESC, customer_id
                                   ROWS UNBOUNDED PRECEDING)
            / NULLIF(SUM(ltv) OVER (), 0)
        , 2)                                          AS cumulative_revenue_pct,
        ROUND(ltv / NULLIF(orders, 0), 2)             AS avg_order_value
    FROM customer_ltv
)
SELECT
    ltv_rank,
    customer_id,
    country,
    orders,
    units,
    first_order_date,
    last_order_date,
    lifespan_days,
    ROUND(ltv, 2)                    AS ltv,
    avg_order_value,
    ROUND(ltv / NULLIF(lifespan_days, 0) * 30, 2) AS ltv_per_month,
    ltv_decile,
    pct_of_total_revenue,
    cumulative_revenue_pct
FROM scored
ORDER BY ltv DESC;
