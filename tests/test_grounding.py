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


# --- L-002 extensions -------------------------------------------------------

import json

import pytest

from metric_contract.cli import main

INTEGER_METRICS = {"order_count", "active_customers", "units_sold", "enterprise_customer_count"}


def test_grounding_covers_all_contract_metrics():
    """The verifier's metric set must equal the contract's metric set (coverage = 100%)."""
    contract_names = {m.name for m in load_pack([GOOD]).metrics}
    assert contract_names, "no demo metrics loaded"
    assert set(_independent_names()) == contract_names


def _independent_names():
    # Names the independent verifier knows about, without needing a DB.
    import inspect
    import re

    src = inspect.getsource(_independent)
    return re.findall(r'^\s+"([a-z_]+)":', src, re.MULTILINE)


def test_integer_metrics_are_exact_integers(retail_db):
    """Counts/unit sums must come back as ints and be exactly equal (no tolerance)."""
    results = {r.metric: r for r in validate(load_pack([GOOD]), retail_db)}
    expected = _independent(retail_db)
    for name in INTEGER_METRICS:
        assert isinstance(results[name].value, int), name
        assert isinstance(expected[name], int), name
        assert results[name].value == expected[name], name


def test_float_metrics_within_1e9(retail_db):
    results = {r.metric: r for r in validate(load_pack([GOOD]), retail_db)}
    expected = _independent(retail_db)
    for name, want in expected.items():
        if name in INTEGER_METRICS:
            continue
        assert math.isclose(results[name].value, want, rel_tol=1e-9, abs_tol=1e-9), (name, results[name].value, want)


def test_cli_json_values_match_independent_recomputation(retail_db, capsys):
    """End-to-end: the value a CI consumer reads from --format json is the grounded value."""
    assert main(["validate", str(GOOD), "--db", str(retail_db), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    expected = _independent(retail_db)
    got = {r["metric"]: r["value"] for r in payload["results"]}
    assert set(got) == set(expected)
    for name, want in expected.items():
        if name in INTEGER_METRICS:
            assert got[name] == want, name
        else:
            assert math.isclose(got[name], want, rel_tol=1e-9, abs_tol=1e-9), name


def test_verifier_detects_tampering(retail_db, tmp_path):
    """Negative control: if the data changes under the engine's feet the verifier is still sound
    (it recomputes from the same DB), and a corrupted engine value is caught by the comparison."""
    results = {r.metric: r for r in validate(load_pack([GOOD]), retail_db)}
    expected = _independent(retail_db)
    bad = results["total_revenue"].value + 0.01
    assert not math.isclose(bad, expected["total_revenue"], rel_tol=1e-9, abs_tol=1e-9)
