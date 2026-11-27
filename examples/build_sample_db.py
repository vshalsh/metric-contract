"""Build the deterministic sample retail SQLite database used by the examples and tests.

Usage:
    python examples/build_sample_db.py              # writes examples/retail.sqlite
    python examples/build_sample_db.py out.sqlite   # writes to a custom path

Same seed -> same rows, every run. Stdlib only.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 20261127
N_CUSTOMERS = 200
N_ORDERS = 600

REGIONS = ["North", "South", "East", "West"]
SEGMENTS = ["Consumer", "Small Business", "Enterprise"]
CHANNELS = ["Online", "Store", "Marketplace"]
PRODUCTS = [
    # name, category, unit_price, unit_cost
    ("Trail Running Shoe", "Footwear", 129.00, 61.00),
    ("City Sneaker", "Footwear", 89.00, 38.50),
    ("Waterproof Jacket", "Apparel", 179.00, 82.00),
    ("Merino Base Layer", "Apparel", 65.00, 24.00),
    ("Daypack 25L", "Bags", 95.00, 41.00),
    ("Duffel 60L", "Bags", 140.00, 63.00),
    ("Insulated Bottle", "Accessories", 32.00, 9.50),
    ("Headlamp", "Accessories", 45.00, 17.00),
    ("Trekking Poles", "Equipment", 110.00, 52.00),
    ("Camp Stove", "Equipment", 75.00, 33.00),
    ("Sleeping Pad", "Equipment", 99.00, 44.00),
    ("Wool Socks (3-pack)", "Apparel", 28.00, 8.00),
]

SCHEMA = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    region TEXT NOT NULL,
    segment TEXT NOT NULL,
    signup_date TEXT NOT NULL
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost REAL NOT NULL
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    order_date TEXT NOT NULL,
    order_month TEXT NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    region TEXT NOT NULL,
    channel TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    revenue REAL NOT NULL,
    cost REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'returned', 'cancelled')),
    is_returned INTEGER NOT NULL
);
-- One row per customer with their (non-cancelled) order count: lets
-- customer-level metrics such as repeat purchase rate stay single-table.
CREATE VIEW customer_order_stats AS
SELECT c.customer_id,
       c.region,
       c.segment,
       COUNT(o.order_id) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id AND o.status <> 'cancelled'
GROUP BY c.customer_id, c.region, c.segment;
"""


def build(path: str | Path) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    rng = random.Random(SEED)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)

        start = date(2025, 1, 1)
        customers = []
        for cid in range(1, N_CUSTOMERS + 1):
            customers.append(
                (
                    cid,
                    f"Customer {cid:04d}",
                    rng.choice(REGIONS),
                    rng.choices(SEGMENTS, weights=[6, 3, 1])[0],
                    (start + timedelta(days=rng.randrange(0, 365))).isoformat(),
                )
            )
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

        conn.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
            [(i, *p) for i, p in enumerate(PRODUCTS, start=1)],
        )

        # Skewed purchase frequency: a few loyal customers, a long tail of one-off buyers.
        weights = [rng.paretovariate(1.3) for _ in customers]
        orders = []
        for oid in range(1, N_ORDERS + 1):
            cust = rng.choices(customers, weights=weights)[0]
            pid = rng.randrange(len(PRODUCTS)) + 1
            _, category, price, unit_cost = PRODUCTS[pid - 1]
            qty = rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5])[0]
            discount = rng.choice([0.0, 0.0, 0.0, 0.05, 0.10, 0.15])
            revenue = round(price * qty * (1 - discount), 2)
            cost = round(unit_cost * qty, 2)
            status = rng.choices(["completed", "returned", "cancelled"], weights=[86, 8, 6])[0]
            d = start + timedelta(days=rng.randrange(0, 365 + 300))
            orders.append(
                (
                    oid,
                    d.isoformat(),
                    d.strftime("%Y-%m"),
                    cust[0],
                    pid,
                    cust[2],
                    rng.choices(CHANNELS, weights=[5, 3, 2])[0],
                    category,
                    qty,
                    revenue,
                    cost,
                    status,
                    1 if status == "returned" else 0,
                )
            )
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", orders)
        conn.commit()
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("retail.sqlite")
    p = build(out)
    print(f"wrote {p}")
