"""Grounding verifier: independently recompute every demo metric.

Values are recomputed from raw rows in Python (not with the contract's SQL) and
compared with what `mc validate` returned. Threshold: 100% of demo metrics
must match - exact for counts and sums of integers, 1e-9 relative for
real-valued sums and ratios.
"""

import math
import sqlite3

from metric_contract.contract import load_pack
from metric_contract.engine import validate

from conftest import GOOD


def _independent(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    orders = [dict(r) for r in conn.execute("SELECT * FROM orders")]
    customers = [dict(r) for r in conn.execute("SELECT * FROM customers")]
    conn.close()

    live = [o for o in orders if o["status"] != "cancelled"]
    done = [o for o in orders if o["status"] == "completed"]
    rev = math.fsum(o["revenue"] for o in live)
    per_cust = {}
    for o in live:
        per_cust[o["customer_id"]] = per_cust.get(o["customer_id"], 0) + 1
    done_rev = math.fsum(o["revenue"] for o in done)
    return {
        "total_revenue": rev,
        "order_count": len(live),
        "aov": rev / len(live),
        "active_customers": len({o["customer_id"] for o in done}),
        "return_rate": sum(o["is_returned"] for o in live) / len(live),
        "gross_margin_pct": (done_rev - math.fsum(o["cost"] for o in done)) / done_rev,
        "revenue_per_customer": rev / len({o["customer_id"] for o in live}),
        "repeat_purchase_rate": sum(1 for n in per_cust.values() if n >= 2) / len(per_cust),
        "units_sold": sum(o["quantity"] for o in orders if o["status"] in ("completed", "returned")),
        "enterprise_customer_count": sum(1 for c in customers if c["segment"] == "Enterprise"),
    }


def test_values_recompute(retail_db):
    results = {r.metric: r for r in validate(load_pack([GOOD]), retail_db)}
    expected = _independent(retail_db)
    assert set(results) == set(expected), "every demo metric needs an independent recomputation"
    mismatches = []
    for name, want in expected.items():
        got = results[name].value
        if isinstance(want, int) and isinstance(got, int):
            ok = got == want
        else:
            ok = math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)
        if not ok:
            mismatches.append((name, got, want))
    assert mismatches == [], f"grounding threshold is 100%; mismatches: {mismatches}"
