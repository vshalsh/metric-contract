"""L-004 regressions: duplicate YAML keys, binary contract files, Ctrl-C handling."""

import json

import pytest

import metric_contract.cli as cli
from metric_contract.cli import main
from metric_contract.contract import load_pack

from conftest import GOOD

METRIC_HEAD = """\
metrics:
  - name: total_revenue
    label: Total Revenue
    owner: Finance
    description: Revenue.
    table: orders
"""


def assert_duplicate_rejected(path, key, retail_db, capsys):
    rc = main(["validate", str(path), "--db", str(retail_db), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["summary"]["passed"] == 0
    reasons = " ".join(r for res in payload["results"] for r in res["reasons"])
    files = {res["contract_file"] for res in payload["results"]}
    assert f"duplicate key '{key}'" in reasons
    assert any(f.endswith(path.name) for f in files)

    # Text mode names file + key too.
    assert main(["validate", str(path), "--db", str(retail_db)]) == 1
    out = capsys.readouterr().out
    assert path.name in out and f"duplicate key '{key}'" in out

    # docs and dax refuse to generate anything from the ambiguous contract.
    for cmd in ("docs", "dax"):
        out_file = path.with_suffix("." + cmd)
        assert main([cmd, str(path), "-o", str(out_file)]) == 1
        assert not out_file.exists()
        err = capsys.readouterr().err
        assert path.name in err and f"duplicate key '{key}'" in err


def test_duplicate_between_rejected(tmp_path, retail_db, capsys):
    # The customer-reported shape: two `between` keys in one tests mapping.
    p = tmp_path / "c.yml"
    p.write_text(
        METRIC_HEAD
        + "    sql: SUM(revenue)\n"
        + "    tests:\n"
        + "      between: [1000000, 2000000]\n"
        + "      between: [0, 1000000000]\n",
        encoding="utf-8",
    )
    assert_duplicate_rejected(p, "between", retail_db, capsys)


def test_duplicate_sql_rejected(tmp_path, retail_db, capsys):
    p = tmp_path / "c.yml"
    p.write_text(METRIC_HEAD + "    sql: SUM(revenue)\n    sql: COUNT(*)\n", encoding="utf-8")
    assert_duplicate_rejected(p, "sql", retail_db, capsys)


def test_duplicate_nested_deep_rejected(tmp_path, retail_db, capsys):
    p = tmp_path / "c.yml"
    p.write_text(
        METRIC_HEAD + "    sql: SUM(revenue)\n    tests:\n      not_null: true\n      non_negative: true\n"
        "      not_null: false\n",
        encoding="utf-8",
    )
    assert_duplicate_rejected(p, "not_null", retail_db, capsys)


def test_duplicate_top_level_rejected(tmp_path, retail_db, capsys):
    p = tmp_path / "c.yml"
    p.write_text(METRIC_HEAD + "    sql: SUM(revenue)\nmetrics: []\n", encoding="utf-8")
    assert_duplicate_rejected(p, "metrics", retail_db, capsys)


def test_duplicate_error_names_lines(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text(METRIC_HEAD + "    sql: SUM(revenue)\n    sql: COUNT(*)\n", encoding="utf-8")
    pack = load_pack([p])
    assert pack.metrics == []
    (err,) = pack.errors
    assert err.field == "sql"
    assert "line 8" in err.message and "first defined on line 7" in err.message


def test_merge_keys_still_allowed(tmp_path, retail_db):
    p = tmp_path / "c.yml"
    p.write_text(
        "base: &base\n  owner: Finance\n  table: orders\n  description: Revenue.\n"
        "metrics:\n  - <<: *base\n    name: total_revenue\n    label: Total Revenue\n    sql: SUM(revenue)\n",
        encoding="utf-8",
    )
    pack = load_pack([p])
    # 'base' is an unknown top-level key but load_file only reads 'metrics'; the merge must not error.
    assert [m.name for m in pack.metrics] == ["total_revenue"]
    assert pack.errors == []


def test_good_pack_unaffected():
    pack = load_pack([GOOD])
    assert pack.errors == [] and len(pack.metrics) == 10


def test_sqlite_file_as_contract_is_usage_error(retail_db, capsys):
    for cmd in (["validate", str(retail_db), "--db", str(retail_db)], ["docs", str(retail_db)], ["dax", str(retail_db)]):
        assert main(cmd) == 2
        err = capsys.readouterr().err
        assert "not a contract file: looks like a SQLite database?" in err
        assert "Traceback" not in err


def test_binary_file_as_contract_is_usage_error(tmp_path, retail_db, capsys):
    p = tmp_path / "blob.yml"
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert main(["validate", str(p), "--db", str(retail_db)]) == 2
    assert "not a contract file" in capsys.readouterr().err


def test_keyboard_interrupt_exit_130(monkeypatch, retail_db, capsys):
    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "validate", boom)
    assert main(["validate", str(GOOD), "--db", str(retail_db)]) == 130
    captured = capsys.readouterr()
    assert "interrupted" in captured.err
    assert "Traceback" not in captured.err
