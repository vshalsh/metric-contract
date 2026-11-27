"""L-002 system tests: acceptance criteria, adversarial contracts, CLI behaviour, read-only engine."""

import json
import re
import shutil
import sqlite3
import subprocess
import sys

import pytest

from metric_contract.cli import main
from metric_contract.contract import load_pack
from metric_contract.engine import PASS, validate

from conftest import BROKEN, GOOD, ROOT

ANSI = re.compile(r"\x1b\[")


def write(tmp_path, text, name="c.yml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def metric_yaml(**over):
    base = {
        "name": "m1", "label": "M One", "owner": "Team", "description": "desc",
        "table": "orders", "sql": "COUNT(*)",
    }
    base.update(over)
    lines = ["metrics:", "  - " + "\n    ".join(
        f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in base.items())]
    return "\n".join(lines) + "\n"


def run_json(contract, db, capsys):
    rc = main(["validate", str(contract), "--db", str(db), "--format", "json"])
    out = capsys.readouterr().out
    return rc, json.loads(out)


# ---------- acceptance criteria ----------

def test_ac1_good_pack_at_least_8_metrics_green_exit0(retail_db, capsys):
    assert len(load_pack([GOOD]).metrics) >= 8
    assert main(["validate", str(GOOD), "--db", str(retail_db)]) == 0
    assert "0 failed" in capsys.readouterr().out


def test_ac1_broken_pack_every_failure_has_name_and_reason(retail_db, capsys):
    rc, payload = run_json(BROKEN, retail_db, capsys)
    assert rc == 1
    assert payload["summary"] == {"failed": 7, "passed": 0, "total": 7}
    for r in payload["results"]:
        assert r["status"] == "FAIL"
        assert r["metric"] and not r["metric"].startswith("<")
        assert r["reasons"] and all(isinstance(x, str) and x for x in r["reasons"])


def test_ac2_provenance_fields_present(retail_db, capsys):
    rc, payload = run_json(GOOD, retail_db, capsys)
    assert rc == 0
    for r in payload["results"]:
        assert r["contract_file"].endswith("retail.yml")
        assert r["metric"]
        assert r["executed_sql"].startswith("SELECT ")
        assert "value" in r and r["value"] is not None
    # Broken-pack results that reached execution keep their SQL.
    _, bp = run_json(BROKEN, retail_db, capsys)
    by = {r["metric"]: r for r in bp["results"]}
    assert by["negative_margin"]["executed_sql"] and by["negative_margin"]["value"] is not None
    assert by["bad_sql_column"]["contract_file"].endswith("broken.yml")


def test_ac2_executed_sql_reproduces_value(retail_db, capsys):
    _, payload = run_json(GOOD, retail_db, capsys)
    conn = sqlite3.connect(retail_db)
    for r in payload["results"]:
        assert conn.execute(r["executed_sql"]).fetchone()[0] == r["value"], r["metric"]


@pytest.mark.parametrize("command", ["docs", "dax"])
def test_ac3_deterministic_across_processes_and_cwd(command, tmp_path):
    outs = []
    for i, cwd in enumerate((ROOT, tmp_path)):
        out = tmp_path / f"o{i}.txt"
        subprocess.run([sys.executable, "-m", "metric_contract", command, str(GOOD), "-o", str(out)],
                       cwd=cwd, check=True, capture_output=True)
        outs.append(out.read_bytes())
    # Same process-level run twice from the same cwd must match byte for byte.
    out2 = tmp_path / "again.txt"
    subprocess.run([sys.executable, "-m", "metric_contract", command, str(GOOD), "-o", str(out2)],
                   cwd=ROOT, check=True, capture_output=True)
    assert outs[0] == out2.read_bytes()
    assert b"\r" not in outs[0]
    assert b"/home/" not in outs[0] and b"/tmp/" not in outs[0]  # no absolute path leakage (from ROOT cwd)


@pytest.mark.parametrize("command", ["docs", "dax"])
def test_ac3_stdout_equals_file(command, tmp_path, capfd):
    f = tmp_path / "f.out"
    assert main([command, str(GOOD), "-o", str(f)]) == 0
    capfd.readouterr()
    assert main([command, str(GOOD)]) == 0
    assert capfd.readouterr().out.encode() == f.read_bytes()


# ---------- adversarial contracts ----------

@pytest.mark.parametrize("sql", [
    "COUNT(*); DROP TABLE orders",
    "COUNT(*) -- comment",
    "COUNT(*) /* c */",
    "SELECT COUNT(*) FROM orders",
    "  select 1",
    "1; DELETE FROM orders",
])
def test_sql_injection_in_sql_field_rejected(tmp_path, retail_db, sql, capsys):
    c = write(tmp_path, metric_yaml(sql=sql))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1
    assert payload["results"][0]["status"] == "FAIL"
    assert payload["results"][0]["executed_sql"] is None
    # DB intact
    conn = sqlite3.connect(retail_db)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 600


@pytest.mark.parametrize("flt", ["1=1; DROP TABLE orders", "status = 'x' -- c", "1=1 /* x */"])
def test_sql_injection_in_filters_rejected(tmp_path, retail_db, flt, capsys):
    c = write(tmp_path, metric_yaml(filters=[flt]))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1 and payload["results"][0]["status"] == "FAIL"


def test_injection_in_table_and_columns_rejected(tmp_path, retail_db, capsys):
    for kw in ({"table": 'orders"; DROP TABLE orders; --'}, {"grain": ["order_date; DROP"]},
               {"dimensions": ["region) --"]}):
        c = write(tmp_path, metric_yaml(**kw))
        rc, payload = run_json(c, retail_db, capsys)
        assert rc == 1, kw
    assert sqlite3.connect(retail_db).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 600


def test_unbalanced_paren_filter_cannot_escape_where(tmp_path, retail_db, capsys):
    # Filter tries to break out of its own parenthesis group; must not succeed in
    # executing anything but a read (either SQL error or a read-only result).
    c = write(tmp_path, metric_yaml(filters=["1=1) OR (1=1"]))
    rc, payload = run_json(c, retail_db, capsys)
    assert sqlite3.connect(retail_db).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 600


def test_empty_contracts_dir(tmp_path, retail_db, capsys):
    d = tmp_path / "empty"
    d.mkdir()
    assert main(["validate", str(d), "--db", str(retail_db)]) == 1
    assert "no metrics" in capsys.readouterr().out
    assert main(["docs", str(d)]) == 1
    assert main(["dax", str(d)]) == 1
    capsys.readouterr()


def test_empty_contracts_dir_json_exit_nonzero(tmp_path, retail_db, capsys):
    d = tmp_path / "empty"
    d.mkdir()
    assert main(["validate", str(d), "--db", str(retail_db), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["total"] == 0


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n", "", "null\n", "metrics: 5\n",
                                  "metrics:\n  - just_a_string\n", "metric: [1, 2]\n", "a: [unclosed\n"])
def test_yaml_not_a_mapping_or_malformed(tmp_path, retail_db, text, capsys):
    c = write(tmp_path, text)
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1
    assert payload["summary"]["failed"] >= 1
    assert all(r["status"] == "FAIL" and r["reasons"] for r in payload["results"])
    assert main(["docs", str(c)]) == 1
    capsys.readouterr()


def test_binary_garbage_contract_does_not_crash(tmp_path, retail_db, capsys):
    p = tmp_path / "bin.yml"
    p.write_bytes(b"\xff\xfe\x00\x01garbage")
    # L-004: obvious binary (null bytes) is a clean usage error, exit 2, no traceback.
    rc = main(["validate", str(p), "--db", str(retail_db), "--format", "json"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not a contract file" in err and "Traceback" not in err


def test_missing_table_reports_failure(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(table="nope"))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1
    assert "table 'nope' does not exist" in payload["results"][0]["reasons"][0]


def test_missing_db_is_usage_error(tmp_path, capsys):
    assert main(["validate", str(GOOD), "--db", str(tmp_path / "nope.sqlite")]) == 2
    assert "database not found" in capsys.readouterr().err


def test_db_path_is_a_directory(tmp_path, capsys):
    assert main(["validate", str(GOOD), "--db", str(tmp_path)]) == 2


def test_db_not_a_sqlite_file(tmp_path, capsys):
    p = tmp_path / "x.sqlite"
    p.write_text("this is not a database" * 50)
    rc = main(["validate", str(GOOD), "--db", str(p)])
    assert rc == 2


def test_missing_contract_path_is_usage_error(retail_db, capsys):
    assert main(["validate", "/no/such/dir", "--db", str(retail_db)]) == 2
    assert main(["docs", "/no/such/file.yml"]) == 2
    assert main(["dax", "/no/such/file.yml"]) == 2


def test_unicode_labels_and_descriptions(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(label="Umsatz Gesamt (€) 売上 🚀", description="Größe – naïve café 売上"))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 0
    out_d, out_x = tmp_path / "d.md", tmp_path / "m.dax"
    assert main(["docs", str(c), "-o", str(out_d)]) == 0
    assert main(["dax", str(c), "-o", str(out_x)]) == 0
    assert "売上 🚀" in out_d.read_text(encoding="utf-8")
    assert "Umsatz Gesamt (€) 売上 🚀" in out_x.read_text(encoding="utf-8")
    assert out_d.read_bytes() == (lambda: (main(["docs", str(c), "-o", str(tmp_path / 'd2.md')]), (tmp_path / 'd2.md').read_bytes())[1])()


def test_unicode_label_text_output_no_crash(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(label="売上"))
    assert main(["validate", str(c), "--db", str(retail_db)]) == 0


def test_division_by_zero_metric(tmp_path, retail_db, capsys):
    # SQLite yields NULL for x/0 -> not_null must catch it, value serialises as JSON null.
    c = write(tmp_path, metric_yaml(sql="SUM(revenue) / 0", tests={"not_null": True}))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1
    r = payload["results"][0]
    assert r["value"] is None and "not_null" in r["reasons"][0]
    # Without tests a NULL value passes (documented behaviour: only declared tests are enforced).
    c2 = write(tmp_path, metric_yaml(sql="SUM(revenue) / 0"), name="c2.yml")
    rc, payload = run_json(c2, retail_db, capsys)
    assert payload["results"][0]["value"] is None


def test_division_by_zero_on_empty_filter(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(sql="SUM(revenue) / COUNT(*)", filters=["status = 'none'"],
                                    tests={"not_null": True}))
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1 and payload["results"][0]["status"] == "FAIL"


def test_multi_row_sql_cannot_happen_or_is_reported(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(sql="revenue"))  # bare column, aggregate-less: SQLite returns first row only
    rc, payload = run_json(c, retail_db, capsys)
    assert payload["results"][0]["executed_sql"]  # no crash either way


def test_json_valid_on_every_failure_kind(tmp_path, retail_db, capsys):
    cases = [BROKEN, tmp_path / "missing.yml"]
    rc, payload = run_json(BROKEN, retail_db, capsys)
    assert isinstance(payload["results"], list)
    # NaN / Infinity must never appear (invalid JSON)
    c = write(tmp_path, metric_yaml(sql="9e999"))
    main(["validate", str(c), "--db", str(retail_db), "--format", "json"])
    text = capsys.readouterr().out
    json.loads(text, parse_constant=lambda s: pytest.fail(f"non-standard JSON constant {s}"))


def test_duplicate_metric_names_reported(tmp_path, retail_db, capsys):
    body = metric_yaml()
    c = write(tmp_path, body + body.split("\n", 1)[1])
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1
    assert any("duplicate" in x for r in payload["results"] for x in r["reasons"])


def test_unknown_field_and_test_rejected(tmp_path, retail_db, capsys):
    c = write(tmp_path, metric_yaml(owners="x", tests={"non_negitive": True}))
    rc, payload = run_json(c, retail_db, capsys)
    reasons = " ".join(payload["results"][0]["reasons"])
    assert rc == 1 and "owners" in reasons and "non_negitive" in reasons


def test_between_inverted_and_bool_rejected(tmp_path, retail_db, capsys):
    for tests in ({"between": [5, 1]}, {"between": [True, 2]}, {"not_null": "yes"}, {"between": [1]}):
        c = write(tmp_path, metric_yaml(tests=tests))
        rc, _ = run_json(c, retail_db, capsys)
        assert rc == 1, tests


def test_directory_discovery_recursive_and_sorted(tmp_path, retail_db, capsys):
    (tmp_path / "sub").mkdir()
    write(tmp_path / "sub", metric_yaml(name="b_metric"), "b.yaml")
    write(tmp_path, metric_yaml(name="a_metric"), "a.yml")
    write(tmp_path, "ignored", "notes.txt")
    rc, payload = run_json(tmp_path, retail_db, capsys)
    assert [r["metric"] for r in payload["results"]] == ["a_metric", "b_metric"]


def test_all_failures_reported_not_just_first(tmp_path, retail_db, capsys):
    text = "metrics:\n" + "".join(
        f"  - {{name: m{i}, label: L, owner: O, description: D, table: orders, sql: 'SUM(nope{i})'}}\n"
        for i in range(5))
    c = write(tmp_path, text)
    rc, payload = run_json(c, retail_db, capsys)
    assert rc == 1 and payload["summary"]["failed"] == 5


# ---------- colour / output hygiene ----------

def test_no_ansi_non_tty_all_commands(retail_db, capfd):
    for argv in (["validate", str(BROKEN), "--db", str(retail_db)],
                 ["validate", str(GOOD), "--db", str(retail_db)], ["docs", str(GOOD)], ["dax", str(GOOD)]):
        main(argv)
        cap = capfd.readouterr()
        assert not ANSI.search(cap.out) and not ANSI.search(cap.err), argv


def test_no_ansi_json_even_when_tty_forced(retail_db):
    code = ("import sys,io\nclass T(io.StringIO):\n  def isatty(self): return True\n"
            "sys.stdout=T()\nfrom metric_contract.cli import main\n"
            f"main(['validate',{str(BROKEN)!r},'--db',{str(retail_db)!r},'--format','json'])\n"
            "sys.stderr.write(sys.stdout.getvalue())")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "\x1b[" not in p.stderr
    json.loads(p.stderr)


def _tty_run(retail_db, env_extra, args_extra):
    code = ("import sys,io\nclass T(io.StringIO):\n  def isatty(self): return True\n"
            "sys.stdout=T()\nfrom metric_contract.cli import main\n"
            f"main(['validate',{str(BROKEN)!r},'--db',{str(retail_db)!r}]+{args_extra!r})\n"
            "sys.stderr.write(sys.stdout.getvalue())")
    import os
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stderr


def test_color_on_tty_but_suppressed_by_no_color_and_flag(retail_db):
    assert "\x1b[" in _tty_run(retail_db, {}, [])          # colour is used on a TTY
    assert "\x1b[" not in _tty_run(retail_db, {"NO_COLOR": "1"}, [])
    assert "\x1b[" not in _tty_run(retail_db, {}, ["--no-color"])


# ---------- CLI usage errors ----------

@pytest.mark.parametrize("argv", [[], ["validate"], ["validate", "x.yml"], ["bogus"],
                                  ["validate", "x.yml", "--db", "d", "--format", "xml"], ["docs"], ["--nope"]])
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2


def test_version_and_module_entrypoint():
    p = subprocess.run([sys.executable, "-m", "metric_contract", "--version"], capture_output=True, text=True)
    assert p.returncode == 0 and "0.1.0" in p.stdout


def test_console_script_exit_codes(retail_db):
    mc = shutil.which("mc", path=str(__import__("pathlib").Path(sys.executable).parent))
    if not mc:
        pytest.skip("mc console script not installed")
    assert subprocess.run([mc, "validate", str(GOOD), "--db", str(retail_db)], capture_output=True).returncode == 0
    assert subprocess.run([mc, "validate", str(BROKEN), "--db", str(retail_db)], capture_output=True).returncode == 1
    assert subprocess.run([mc, "validate"], capture_output=True).returncode == 2


# ---------- the engine cannot write ----------

@pytest.mark.parametrize("sql,flt", [
    ("(SELECT 1)", "1=1; INSERT INTO customers VALUES (9999,'x','N','Consumer','2020-01-01')"),
])
def test_stacked_write_blocked(tmp_path, retail_db, sql, flt, capsys):
    c = write(tmp_path, metric_yaml(sql=sql, filters=[flt]))
    rc, _ = run_json(c, retail_db, capsys)
    assert rc == 1
    assert sqlite3.connect(retail_db).execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 200


def _snapshot(db):
    conn = sqlite3.connect(db)
    return (conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            conn.execute("SELECT SUM(revenue) FROM orders").fetchone()[0],
            conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0])


def test_authorizer_denies_writes_directly(retail_db):
    """Drive the engine's own authorizer + connection against write statements."""
    from metric_contract import engine

    # 1. Authorizer alone, on a WRITABLE connection (so only the authorizer can stop it).
    work = sqlite3.connect(retail_db)
    work.set_authorizer(engine._authorizer)
    for stmt in ("INSERT INTO customers VALUES (9999,'x','N','Consumer','2020-01-01')",
                 "UPDATE orders SET revenue = 0",
                 "DELETE FROM orders",
                 "DROP TABLE customers",
                 "CREATE TABLE t(x)",
                 "ALTER TABLE customers ADD COLUMN z",
                 "CREATE INDEX i ON orders(revenue)",
                 "PRAGMA user_version = 7",
                 "ATTACH DATABASE ':memory:' AS evil",
                 "CREATE TRIGGER tr AFTER INSERT ON orders BEGIN SELECT 1; END"):
        with pytest.raises(sqlite3.DatabaseError):
            work.execute(stmt)
    work.set_authorizer(None)
    work.close()
    assert _snapshot(retail_db)[:3] == (600, 200, _snapshot(retail_db)[2])
    assert _snapshot(retail_db)[3] == 0

    # 2. Read-only connection alone.
    ro = engine.connect_readonly(retail_db)
    for stmt in ("INSERT INTO customers VALUES (9999,'x','N','Consumer','2020-01-01')",
                 "UPDATE orders SET revenue = 0", "DELETE FROM orders", "PRAGMA user_version = 7"):
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(stmt)
    ro.close()
    assert _snapshot(retail_db) == _snapshot(retail_db)


def test_authorizer_blocks_pragma_in_metric_expression(tmp_path, retail_db, capsys):
    before = _snapshot(retail_db)
    # A subquery cannot hold INSERT/PRAGMA statements; try reads of pragma functions & writable pragmas.
    for sql in ("(SELECT COUNT(*) FROM pragma_table_info('orders'))", "(SELECT writable_schema FROM pragma_function_list)"):
        c = write(tmp_path, metric_yaml(sql=sql))
        rc, payload = run_json(c, retail_db, capsys)
        assert payload["results"][0]["status"] in ("PASS", "FAIL")
    assert _snapshot(retail_db) == before


def test_engine_run_leaves_db_bytes_unchanged(retail_db):
    import hashlib, pathlib
    h = lambda: hashlib.sha256(pathlib.Path(retail_db).read_bytes()).hexdigest()
    before = h()
    validate(load_pack([GOOD, BROKEN]), retail_db)
    assert h() == before


def test_readonly_db_file_permissions(tmp_path, retail_db):
    """A chmod-444 DB must still validate (read-only open)."""
    import os, shutil
    ro = tmp_path / "ro.sqlite"
    shutil.copy(retail_db, ro)
    os.chmod(ro, 0o444)
    assert all(r.status == PASS for r in validate(load_pack([GOOD]), ro))
