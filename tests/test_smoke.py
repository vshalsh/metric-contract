"""Smoke tests for L-001: validate (good + broken packs), docs and dax determinism."""

import json

import pytest

from metric_contract.cli import main
from metric_contract.contract import load_pack
from metric_contract.engine import PASS, validate

from conftest import BROKEN, GOOD


def _by_name(results):
    return {r.metric: r for r in results}


def test_good_pack_is_green(retail_db):
    pack = load_pack([GOOD])
    assert pack.errors == []
    assert len(pack.metrics) >= 8
    results = validate(pack, retail_db)
    failing = [(r.metric, r.reasons) for r in results if r.status != PASS]
    assert failing == []


def test_good_pack_cli_exit_zero(retail_db, capsys):
    assert main(["validate", str(GOOD), "--db", str(retail_db)]) == 0
    out = capsys.readouterr().out
    assert "0 failed" in out
    assert "FAIL" not in out


def test_results_carry_provenance(retail_db, capsys):
    rc = main(["validate", str(GOOD), "--db", str(retail_db), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["failed"] == 0
    for r in payload["results"]:
        assert r["contract_file"].endswith("retail.yml")
        assert r["metric"]
        assert r["executed_sql"].startswith("SELECT ")
        assert r["value"] is not None


EXPECTED_BROKEN = {
    "missing_owner": "owner: missing required field",
    "bad_grain_column": "grain column 'order_week' does not exist",
    "bad_sql_column": "no such column: net_revenue",
    "negative_margin": "non_negative",
    "return_rate_out_of_bounds": "outside [0, 0.01]",
    "null_revenue": "not_null: value is NULL",
    "missing_table": "table 'refunds' does not exist",
}


def test_broken_pack_reports_every_failure(retail_db):
    results = _by_name(validate(load_pack([BROKEN]), retail_db))
    assert set(results) == set(EXPECTED_BROKEN)
    for name, needle in EXPECTED_BROKEN.items():
        r = results[name]
        assert r.status == "FAIL", name
        assert any(needle in reason for reason in r.reasons), (name, r.reasons)


def test_broken_pack_cli_exit_one_and_names_metrics(retail_db, capsys):
    assert main(["validate", str(BROKEN), "--db", str(retail_db)]) == 1
    out = capsys.readouterr().out
    for name in EXPECTED_BROKEN:
        assert f"FAIL  {name}" in out


def test_no_color_when_not_tty(retail_db, capsys):
    main(["validate", str(GOOD), "--db", str(retail_db)])
    assert "\033[" not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["docs", "dax"])
def test_generators_deterministic_and_complete(command, tmp_path, capsys):
    a, b = tmp_path / "a.out", tmp_path / "b.out"
    assert main([command, str(GOOD), "-o", str(a)]) == 0
    assert main([command, str(GOOD), "-o", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()
    text = a.read_text(encoding="utf-8")
    for m in load_pack([GOOD]).metrics:
        assert m.name in text
        assert m.label in text


def test_generators_refuse_broken_pack(tmp_path, capsys):
    out = tmp_path / "x.dax"
    assert main(["dax", str(BROKEN), "-o", str(out)]) == 1
    assert not out.exists()
    assert "missing_owner" in capsys.readouterr().err
