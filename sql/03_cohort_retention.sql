-- ============================================================================
--  Q03 :: Cohort retention by first-purchase month
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    Of the customers who bought for the first time in a given month, how many
--    came back in month 1, month 2, ... month 12?  Is retention getting better
--    or worse for newer cohorts?
--
--  WHY IT MATTERS
--    Acquisition spend is only worth it if cohorts keep buying. A cohort
--    triangle separates "we have a leaky bucket" from "we have a growth
--    problem", and comparing the 2010-12 cohort against the 2011-06 cohort
--    shows whether product or service changes actually improved repeat
--    behaviour. It also gives the retention curve used in LTV forecasting.
--
--  TECHNIQUE
--    CTEs identify each customer's cohort (month of first purchase) and every
--    month they were active, a self-join attaches the cohort to the activity,
--    and month_index is computed with integer year/month arithmetic so that
--    the join does not have to materialise a calendar table.
--
--  NOTE ON THE DATA
--    Cohorts near the end of the file can only be observed for a few months,
--    so their row count is naturally short. Compare cohorts down a column,
--    never across the last rows.
-- ============================================================================

WITH first_purchase AS (
    SELECT
        o.customer_id,
        DATE_TRUNC('month', MIN(o.invoice_date))::date AS cohort_month
    FROM orders o
    WHERE NOT o.is_cancelled
      AND o.customer_id IS NOT NULL
    GROUP BY o.customer_id
),
activity AS (
    SELECT DISTINCT
        o.customer_id,
        DATE_TRUNC('month', o.invoice_date)::date AS activity_month
    FROM orders o
    WHERE NOT o.is_cancelled
      AND o.customer_id IS NOT NULL
),
cohort_activity AS (
    SELECT
        f.cohort_month,
        a.activity_month,
        (
            (EXTRACT(YEAR  FROM a.activity_month) - EXTRACT(YEAR  FROM f.cohort_month)) * 12
          + (EXTRACT(MONTH FROM a.activity_month) - EXTRACT(MONTH FROM f.cohort_month))
        )::int AS month_index,
        a.customer_id
    FROM activity a
    JOIN first_purchase f ON f.customer_id = a.customer_id
),
cohort_size AS (
    SELECT cohort_month, COUNT(DISTINCT customer_id) AS cohort_size
    FROM cohort_activity
    WHERE month_index = 0
    GROUP BY cohort_month
)
SELECT
    TO_CHAR(ca.cohort_month, 'YYYY-MM')                       AS cohort_month,
    ca.month_index,
    cs.cohort_size,
    COUNT(DISTINCT ca.customer_id)                            AS active_customers,
    ROUND(100.0 * COUNT(DISTINCT ca.customer_id)
          / NULLIF(cs.cohort_size, 0), 1)                     AS retention_pct,
    ROUND(100.0 * COUNT(DISTINCT ca.customer_id)
          / NULLIF(MAX(cs.cohort_size) OVER (), 0), 1)        AS pct_of_largest_cohort
FROM cohort_activity ca
JOIN cohort_size cs ON cs.cohort_month = ca.cohort_month
GROUP BY ca.cohort_month, ca.month_index, cs.cohort_size
ORDER BY ca.cohort_month, ca.month_index;
