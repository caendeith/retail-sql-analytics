-- ============================================================================
--  Q02 :: RFM segmentation -- one row per customer
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    Who are our best customers, who is slipping away, and how much revenue
--    sits in each behavioural segment?
--
--  WHY IT MATTERS
--    RFM turns a flat customer list into an actionable targeting list. The
--    marketing budget should not be spent evenly: Champions deserve loyalty
--    rewards and early access, "At Risk / high value" deserve a win-back
--    campaign before they are gone, and Hibernating customers should be
--    suppressed from expensive channels.
--
--  TECHNIQUE
--    CTEs build a per-customer RFM frame against a fixed snapshot date, then
--    NTILE(5) scores each dimension 1-5. Scores are oriented so that 5 is
--    always the best (most recent / most frequent / highest spend). The three
--    scores are concatenated into a classic RFM cell and mapped to a named
--    segment.
--
--  NOTE ON THE DATA
--    "Recency" is measured from the last invoice date in the file (2011-12-09)
--    rather than from today, so the segmentation is reproducible forever.
-- ============================================================================

WITH snapshot AS (
    SELECT MAX(invoice_date)::date AS as_of_date
    FROM orders
    WHERE NOT is_cancelled
),
customer_rfm AS (
    SELECT
        o.customer_id,
        MAX(o.invoice_date)::date                  AS last_order_date,
        (SELECT as_of_date FROM snapshot)
            - MAX(o.invoice_date)::date            AS recency_days,
        COUNT(DISTINCT o.invoice_no)               AS frequency,
        SUM(i.line_revenue)                        AS monetary
    FROM order_items i
    JOIN orders o ON o.invoice_no = i.invoice_no
    WHERE NOT o.is_cancelled
      AND o.customer_id IS NOT NULL
    GROUP BY o.customer_id
),
scored AS (
    SELECT
        *,
        -- ORDER BY recency_days DESC puts the least recent customers in tile 1,
        -- so tile 5 is always the most recent cohort.
        NTILE(5) OVER (ORDER BY recency_days DESC, customer_id) AS r_score,
        NTILE(5) OVER (ORDER BY frequency ASC, customer_id)     AS f_score,
        NTILE(5) OVER (ORDER BY monetary ASC, customer_id)      AS m_score
    FROM customer_rfm
),
segmented AS (
    SELECT
        *,
        CONCAT(r_score, f_score, m_score) AS rfm_cell,
        (r_score + f_score + m_score)     AS rfm_total,
        CASE
            WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champions'
            WHEN r_score <= 2 AND f_score >= 4 AND m_score >= 4 THEN 'At Risk (high value)'
            WHEN r_score <= 2 AND f_score <= 2 AND m_score <= 2 THEN 'Hibernating'
            WHEN r_score >= 4 AND f_score <= 2                  THEN 'New / Promising'
            WHEN r_score >= 3 AND f_score >= 3                  THEN 'Loyal'
            WHEN r_score <= 2                                   THEN 'At Risk'
            ELSE 'Needs Attention'
        END AS segment
    FROM scored
)
SELECT
    customer_id,
    segment,
    rfm_cell,
    r_score,
    f_score,
    m_score,
    rfm_total,
    recency_days,
    last_order_date,
    frequency,
    ROUND(monetary, 2)                                  AS monetary,
    ROUND(monetary / NULLIF(frequency, 0), 2)           AS avg_order_value,
    ROUND(100.0 * monetary / SUM(monetary) OVER (), 3)  AS pct_of_revenue,
    RANK() OVER (ORDER BY monetary DESC)                AS revenue_rank
FROM segmented
ORDER BY monetary DESC;
