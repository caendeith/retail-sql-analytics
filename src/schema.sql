-- ============================================================================
--  retail-sql-analytics :: PostgreSQL schema
-- ----------------------------------------------------------------------------
--  Designed for analytical SQL: every table exists so that one of the six
--  business queries in ../sql/ can answer its question without touching raw
--  CSVs again.
--
--  Load strategy (see src/load.py and the README):
--    * customers    <- every raw row that carries a CustomerID
--    * products     <- every row that survives the cleaning rules
--    * orders       <- one row per invoice: cleaned invoices (is_cancelled =
--                      FALSE) AND cancellation invoices (is_cancelled = TRUE)
--    * order_items  <- cleaned, revenue-bearing lines only
--    * cancellations<- line-level detail of the 'C%' invoices, kept separately
--                      so that returns are measurable without polluting the
--                      revenue fact table
-- ============================================================================

DROP TABLE IF EXISTS cancellations CASCADE;
DROP TABLE IF EXISTS order_items   CASCADE;
DROP TABLE IF EXISTS orders        CASCADE;
DROP TABLE IF EXISTS products      CASCADE;
DROP TABLE IF EXISTS customers     CASCADE;

-- ----------------------------------------------------------------------------
-- customers: one row per known buyer
-- ----------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id  INTEGER      PRIMARY KEY,
    country      TEXT         NOT NULL
);

-- ----------------------------------------------------------------------------
-- products: catalogue with a robust central price (median, not mean, so that
-- a handful of bulk/wholesale lines cannot skew the reference price)
-- ----------------------------------------------------------------------------
CREATE TABLE products (
    stock_code   TEXT         PRIMARY KEY,
    description  TEXT,
    median_price NUMERIC(12,4)
);

-- ----------------------------------------------------------------------------
-- orders: invoice header. customer_id is NULLable because a small number of
-- cancellation invoices have no CustomerID in the source data; PostgreSQL
-- foreign keys permit NULL, and this keeps those returns countable.
-- ----------------------------------------------------------------------------
CREATE TABLE orders (
    invoice_no   TEXT         PRIMARY KEY,
    customer_id  INTEGER      REFERENCES customers (customer_id),
    invoice_date TIMESTAMP    NOT NULL,
    is_cancelled BOOLEAN      NOT NULL DEFAULT FALSE,
    country      TEXT         NOT NULL
);

-- ----------------------------------------------------------------------------
-- order_items: the revenue fact table (grain = one invoice line).
-- A surrogate key is used because the same (invoice_no, stock_code) pair can
-- legitimately appear twice when a line was re-priced within one invoice.
-- ----------------------------------------------------------------------------
CREATE TABLE order_items (
    order_item_id BIGSERIAL   PRIMARY KEY,
    invoice_no    TEXT        NOT NULL REFERENCES orders (invoice_no),
    stock_code    TEXT        NOT NULL REFERENCES products (stock_code),
    quantity      INTEGER     NOT NULL,
    unit_price    NUMERIC(12,4) NOT NULL,
    line_revenue  NUMERIC(14,4) NOT NULL
);

-- ----------------------------------------------------------------------------
-- cancellations: line-level detail of returned / cancelled invoices.
-- quantity is stored negative exactly as received; cancelled_revenue is the
-- positive magnitude so that "lost revenue" can be summed directly.
-- No FK to products: cancellation lines occasionally reference stock codes
-- (postage, manual adjustments, damaged stock) that never appear in a sale.
-- ----------------------------------------------------------------------------
CREATE TABLE cancellations (
    id                BIGSERIAL   PRIMARY KEY,
    invoice_no        TEXT        NOT NULL REFERENCES orders (invoice_no),
    stock_code        TEXT,
    description       TEXT,
    customer_id       INTEGER     REFERENCES customers (customer_id),
    invoice_date      TIMESTAMP   NOT NULL,
    quantity          INTEGER     NOT NULL,
    unit_price        NUMERIC(12,4) NOT NULL,
    cancelled_revenue NUMERIC(14,4) NOT NULL
);

-- ----------------------------------------------------------------------------
-- Indexes for the access patterns the six queries actually use
-- ----------------------------------------------------------------------------
CREATE INDEX idx_orders_invoice_date       ON orders (invoice_date);
CREATE INDEX idx_orders_customer_id        ON orders (customer_id);
CREATE INDEX idx_orders_is_cancelled       ON orders (is_cancelled);
CREATE INDEX idx_order_items_invoice_no    ON order_items (invoice_no);
CREATE INDEX idx_order_items_stock_code    ON order_items (stock_code);
CREATE INDEX idx_cancellations_invoice_date ON cancellations (invoice_date);
CREATE INDEX idx_cancellations_customer_id  ON cancellations (customer_id);

-- Refresh planner statistics right after the bulk load.
ANALYZE customers;
ANALYZE products;
ANALYZE orders;
ANALYZE order_items;
ANALYZE cancellations;
