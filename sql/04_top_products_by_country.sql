-- ============================================================================
--  Q04 :: Top 5 products in every country
-- ----------------------------------------------------------------------------
--  BUSINESS QUESTION
--    What does each market actually buy?  Which SKUs carry a country, and how
--    concentrated is that country's revenue in its single best seller?
--
--  WHY IT MATTERS
--    A global top-10 list is dominated by the UK and hides what local markets
--    want. Per-country ranking tells the merchandising team what to keep in
--    stock for each market, tells marketing which hero product to lead with in
--    each country, and flags markets that depend on one SKU (fragile) versus
--    markets with a broad base (resilient).
--
--  TECHNIQUE
--    RANK() OVER (PARTITION BY country ORDER BY revenue DESC) gives a rank
--    that resets per country; a second window (SUM() OVER (PARTITION BY
--    country)) supplies the denominator for the share-of-country column
--    without a correlated subquery.
-- ============================================================================

WITH country_product AS (
    SELECT
        o.country,
        i.stock_code,
        p.description,
        SUM(i.line_revenue)            AS revenue,
        SUM(i.quantity)                AS units_sold,
        COUNT(DISTINCT i.invoice_no)   AS invoices
    FROM order_items i
    JOIN orders   o ON o.invoice_no  = i.invoice_no
    JOIN products p ON p.stock_code  = i.stock_code
    WHERE NOT o.is_cancelled
    GROUP BY o.country, i.stock_code, p.description
),
ranked AS (
    SELECT
        *,
        RANK() OVER (PARTITION BY country ORDER BY revenue DESC)  AS product_rank,
        SUM(revenue) OVER (PARTITION BY country)                  AS country_revenue,
        COUNT(*)     OVER (PARTITION BY country)                  AS distinct_skus
    FROM country_product
)
SELECT
    country,
    product_rank,
    stock_code,
    description,
    ROUND(revenue, 2)                                        AS revenue,
    units_sold,
    invoices,
    ROUND(100.0 * revenue / NULLIF(country_revenue, 0), 2)   AS pct_of_country,
    ROUND(country_revenue, 2)                                AS country_revenue,
    distinct_skus
FROM ranked
WHERE product_rank <= 5
ORDER BY country_revenue DESC, country, product_rank;
