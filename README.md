# metric-contract

**KPI-as-code.** Define every executive metric once, in YAML, and prove it by execution: `mc` validates each contract against a SQLite database, runs the metric's declared tests on the real value, and generates a measure dictionary and Power BI DAX measure stubs from the same source.

## Why

"Revenue" in the board deck, "revenue" in the finance Power BI model and "revenue" in the data team's warehouse view are usually three slightly different numbers. One excludes cancelled orders, one nets returns, one was fixed in a pull request last quarter that never reached the DAX. That is **metric drift**: BI developers and data engineers each own a copy of the definition, and nothing checks that the copies agree.

A metric contract is a single, reviewable definition with an accountable owner, a declared grain, the dimensions it may be sliced by, its filters and its sanity bounds. `mc` then:

1. **validates** the contract by actually running it, so a definition that references a renamed column or drifts outside its bounds fails in CI rather than in a meeting;
2. **documents** it as a deterministic measure dictionary for business users;
3. **generates** DAX measure stubs so the Power BI side starts from the contract rather than from memory.

As agents start generating pipelines and semantic models, human-owned metric definitions matter more, not less. The contract is the thing a person signs off.

## Install

Requires Python 3.10+. Dependencies: PyYAML (plus the standard-library `sqlite3`). No network access, no LLM.

```bash
git clone https://github.com/vshalsh/metric-contract.git
cd metric-contract
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -e '.[test]' to run the tests
mc --version                # mc 0.1.0
```

## Quick start

```bash
python examples/build_sample_db.py          # writes examples/retail.sqlite (deterministic, seeded)
mc validate examples/contracts/retail.yml --db examples/retail.sqlite
mc docs examples/contracts/retail.yml -o DICTIONARY.md
mc dax  examples/contracts/retail.yml -o measures.dax
```

The sample database is not committed; the build script regenerates the identical data on every run (fixed seed). It holds `customers` (200 rows), `products` (12), `orders` (600) and a `customer_order_stats` view.

## Contract format

A contract file is YAML. It holds either many metrics under `metrics:` (a list) or one under `metric:` (a mapping). A directory argument is searched recursively for `*.yml` / `*.yaml`.

```yaml
metrics:
  - name: total_revenue
    label: Total Revenue
    owner: Finance - Revenue Reporting
    description: Net sales value of all orders that were not cancelled.
    table: orders
    sql: SUM(revenue)
    grain: [order_date, order_month, customer_id]
    dimensions: [region, channel, category]
    filters:
      - status <> 'cancelled'
    tests:
      not_null: true
      non_negative: true
      between: [10000, 1000000]
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `name` | yes | string, snake_case | Stable metric id (`^[a-z][a-z0-9_]*$`). Must be unique across the whole pack. |
| `label` | yes | string | Business-facing name. Used as the DAX measure name. |
| `owner` | yes | string | Accountable team or person. A metric without an owner is rejected. |
| `description` | yes | string | Plain-language definition. Becomes the DAX comment and the dictionary text. |
| `table` | yes | identifier | Table or view the metric is computed over. Must exist in the database. |
| `sql` | yes | SQL expression | An aggregate expression over `table`, e.g. `SUM(revenue)`, `COUNT(DISTINCT customer_id)`, `(SUM(revenue) - SUM(cost)) / SUM(revenue)`. Not a full `SELECT`; no `;` and no SQL comments. Must return exactly one row. |
| `grain` | no | list of columns | Date/entity columns the metric is valid at. Each must exist in `table`. |
| `dimensions` | no | list of columns | Columns the metric may be sliced by. Each must exist in `table`. |
| `filters` | no | list of SQL boolean expressions | AND-joined into the `WHERE` clause, e.g. `status <> 'cancelled'`. |
| `tests.not_null` | no | bool | Fail if the value is NULL. |
| `tests.non_negative` | no | bool | Fail if the value is negative (or not a number). |
| `tests.between` | no | `[lo, hi]` numbers | Fail if the value is outside the inclusive range. `lo` must be <= `hi`. |

Unknown fields and unknown tests are errors, so a typo (`owners:`, `non_negitive:`) is caught rather than silently ignored. Duplicate keys at any level (for example a second `between:` or `sql:` on the same metric) are also errors, naming the file, the key and both line numbers: plain YAML loaders silently keep the last value, which could drop a guard or swap a metric's SQL unnoticed.

Each metric executes as:

```sql
SELECT <sql> AS value FROM "<table>" WHERE (<filter 1>) AND (<filter 2>) ...
```

The database is opened **read-only** (`mode=ro`) and a SQLite authorizer permits only `SELECT`/read/function operations while a metric runs.

## CLI

```
mc validate <contracts...> --db <sqlite file> [--format text|json] [--no-color]
mc docs     <contracts...> [-o DICTIONARY.md] [--no-color]
mc dax      <contracts...> [-o measures.dax]  [--no-color]
```

Exit codes: `0` all good, `1` any metric failed (or a contract has schema errors for `docs`/`dax`), `2` usage error such as a missing database or contract path.

### `mc validate`: green pack

```
$ mc validate examples/contracts/retail.yml --db examples/retail.sqlite
PASS  total_revenue              value=81999.05  [examples/contracts/retail.yml]
      sql: SELECT SUM(revenue) AS value FROM "orders" WHERE (status <> 'cancelled')
PASS  order_count                value=555  [examples/contracts/retail.yml]
      sql: SELECT COUNT(*) AS value FROM "orders" WHERE (status <> 'cancelled')
PASS  aov                        value=147.746036  [examples/contracts/retail.yml]
      sql: SELECT SUM(revenue) / COUNT(*) AS value FROM "orders" WHERE (status <> 'cancelled')
PASS  active_customers           value=132  [examples/contracts/retail.yml]
      sql: SELECT COUNT(DISTINCT customer_id) AS value FROM "orders" WHERE (status = 'completed')
PASS  return_rate                value=0.079279  [examples/contracts/retail.yml]
      sql: SELECT AVG(is_returned) AS value FROM "orders" WHERE (status <> 'cancelled')
PASS  gross_margin_pct           value=0.536285  [examples/contracts/retail.yml]
      sql: SELECT (SUM(revenue) - SUM(cost)) / SUM(revenue) AS value FROM "orders" WHERE (status = 'completed')
PASS  revenue_per_customer       value=607.40037  [examples/contracts/retail.yml]
      sql: SELECT SUM(revenue) / COUNT(DISTINCT customer_id) AS value FROM "orders" WHERE (status <> 'cancelled')
PASS  repeat_purchase_rate       value=0.622222  [examples/contracts/retail.yml]
      sql: SELECT 1.0 * SUM(CASE WHEN order_count >= 2 THEN 1 ELSE 0 END) / COUNT(*) AS value FROM "customer_order_stats" WHERE (order_count >= 1)
PASS  units_sold                 value=880  [examples/contracts/retail.yml]
      sql: SELECT SUM(quantity) AS value FROM "orders" WHERE (status IN ('completed', 'returned'))
PASS  enterprise_customer_count  value=17  [examples/contracts/retail.yml]
      sql: SELECT COUNT(*) AS value FROM "customers" WHERE (segment = 'Enterprise')
PASS  10 metrics: 10 passed, 0 failed
```

Text output shows floats with at most 6 decimal places (trailing zeros dropped); values smaller than 1e-6 in magnitude are shown in scientific notation instead (e.g. `1.234e-07`) so they never print as `0`. `--format json` always carries the full value.

### `mc validate`: broken pack

`examples/contracts/broken.yml` is deliberately wrong in seven different ways:

```
$ mc validate examples/contracts/broken.yml --db examples/retail.sqlite
FAIL  missing_owner              value=-  [examples/contracts/broken.yml]
      reason: schema: owner: missing required field
FAIL  bad_grain_column           value=88174.4  [examples/contracts/broken.yml]
      sql: SELECT SUM(revenue) AS value FROM "orders"
      reason: grain column 'order_week' does not exist in table 'orders'
FAIL  bad_sql_column             value=-  [examples/contracts/broken.yml]
      sql: SELECT SUM(net_revenue) AS value FROM "orders"
      reason: SQL error: no such column: net_revenue
FAIL  negative_margin            value=-47323.4  [examples/contracts/broken.yml]
      sql: SELECT SUM(cost) - SUM(revenue) AS value FROM "orders"
      reason: non_negative: value -47323.4 is below 0
FAIL  return_rate_out_of_bounds  value=0.079279  [examples/contracts/broken.yml]
      sql: SELECT AVG(is_returned) AS value FROM "orders" WHERE (status <> 'cancelled')
      reason: between: value 0.079279 is outside [0, 0.01]
FAIL  null_revenue               value=NULL  [examples/contracts/broken.yml]
      sql: SELECT MAX(revenue) AS value FROM "orders" WHERE (status = 'no_such_status')
      reason: not_null: value is NULL
FAIL  missing_table              value=-  [examples/contracts/broken.yml]
      reason: table 'refunds' does not exist in the database
FAIL  7 metrics: 0 passed, 7 failed
$ echo $?
1
```

Schema errors name the file, the metric (or `<metric #N>` when the name itself is missing) and the field. One bad metric never hides the others: every metric is reported.

### `mc validate --format json`

Machine-readable, colour-free, one provenance record per metric (abridged to one result):

```json
{
  "db": "examples/retail.sqlite",
  "results": [
    {
      "contract_file": "examples/contracts/retail.yml",
      "executed_sql": "SELECT SUM(revenue) AS value FROM \"orders\" WHERE (status <> 'cancelled')",
      "metric": "total_revenue",
      "reasons": [],
      "status": "PASS",
      "tests": {
        "between": "PASS",
        "non_negative": "PASS",
        "not_null": "PASS"
      },
      "value": 81999.05
    }
  ],
  "summary": {"failed": 0, "passed": 10, "total": 10}
}
```

### `mc docs`

Writes a markdown measure dictionary: a summary table (name, label, owner, table, tests) followed by a detail section per metric with its description, owner, expression, grain, dimensions, filters, tests, source contract and the exact SQL that `validate` executes. `docs` and `dax` refuse to write anything if any contract has a schema error.

### `mc dax`

Writes one commented stub per metric. Excerpt from `mc dax examples/contracts/retail.yml`:

```dax
// ---- gross_margin_pct ----
// Label:       Gross Margin %
// Owner:       Finance - FP&A
// Description: (Revenue - cost of goods) / revenue on completed orders, as a fraction (0.55 = 55%).
// Grain:       order_date, order_month
// Dimensions:  region, channel, category
// Source:      examples/contracts/retail.yml
// SQL:         (SUM(revenue) - SUM(cost)) / SUM(revenue)
// Tests:       not_null, between [0, 1]
Gross Margin % =
CALCULATE(
    DIVIDE((SUM('orders'[revenue]) - SUM('orders'[cost])), SUM('orders'[revenue])),
    'orders'[status] = "completed"
)

// ---- units_sold ----
// Label:       Units Sold
// Owner:       Merchandising
// Description: Total item quantity on completed and returned orders (cancelled excluded).
// Grain:       order_date, order_month
// Dimensions:  region, channel, category
// Source:      examples/contracts/retail.yml
// SQL:         SUM(quantity)
// Tests:       not_null, non_negative
Units Sold =
CALCULATE(
    SUM('orders'[quantity]),
    'orders'[status] IN { "completed", "returned" }
)

// ---- repeat_purchase_rate ----
// Label:       Repeat Purchase Rate
// Owner:       Customer Analytics
// Description: Share of purchasing customers with two or more non-cancelled orders.
// Grain:       customer_id
// Dimensions:  region, segment
// Source:      examples/contracts/retail.yml
// SQL:         1.0 * SUM(CASE WHEN order_count >= 2 THEN 1 ELSE 0 END) / COUNT(*)
// Tests:       not_null, between [0, 1]
// TODO: SQL expression not auto-translatable (unsupported argument 'CASE'); translate by hand: 1.0 * SUM(CASE WHEN order_count >= 2 THEN 1 ELSE 0 END) / COUNT(*)
Repeat Purchase Rate =
CALCULATE(
    BLANK(),
    'customer_order_stats'[order_count] >= 1
)
```

Translation rules (deliberately conservative: anything unsupported is flagged, never guessed):

| SQL | DAX |
|---|---|
| `SUM(c)` / `AVG(c)` / `MIN(c)` / `MAX(c)` | `SUM('t'[c])` / `AVERAGE('t'[c])` / `MIN('t'[c])` / `MAX('t'[c])` |
| `COUNT(*)` | `COUNTROWS('t')` |
| `COUNT(c)` / `COUNT(DISTINCT c)` | `COUNT('t'[c])` / `DISTINCTCOUNT('t'[c])` |
| `a / b` | `DIVIDE(a, b)` |
| `+ - *`, numbers, parentheses | unchanged |
| filter `c <op> literal` (`= <> != < <= > >=`) | `CALCULATE` filter `'t'[c] <op> literal` (strings in double quotes) |
| filter `c [NOT] IN ('a', 'b')` | `'t'[c] IN { "a", "b" }` (wrapped in `NOT ( ... )` when negated) |
| filter `c IS [NOT] NULL` | `[NOT] ISBLANK('t'[c])` |
| anything else (`CASE`, functions, compound filters) | expression becomes `BLANK()` with a `// TODO` holding the SQL; filter is carried as a `// TODO` comment |

### Determinism

`mc docs` and `mc dax` are pure functions of the contracts: no timestamps, no environment data, fixed ordering (files sorted by path, metrics in file order), `\n` line endings on every OS. Same input gives byte-identical output, so the generated files diff cleanly in review. Source paths are written relative to the working directory (or as a bare file name if the contract lives outside it), so no machine-specific absolute paths leak into generated files.

### Colour

Colour is used only when stdout is a TTY, and never when `NO_COLOR` is set or `--no-color` is passed. `FAIL` is red, `PASS` green, detail (SQL, paths) dim. The status word is always printed, so colour is only ever a secondary cue. JSON output is never coloured.

## Grounding & traceability

`mc` produces numbers that people will put in front of executives, so every number must be traceable and independently checkable.

**Provenance on every result.** Each validation result records the contract file, the metric name, the exact SQL statement executed, the value returned, the per-test outcome and every failure reason (`Result.provenance()` in `metric_contract/engine.py`). Text output prints the same fields; `--format json` emits them as structured records for CI logs or audit storage. The dictionary also prints the executed SQL for every metric, so a reader can rerun it by hand.

**Independent recomputation verifier.** `tests/test_grounding.py::test_values_recompute` recomputes every demo metric from the raw rows of the sample database in plain Python (filters, distinct counts and ratios written by hand, using neither the contract's SQL nor the `mc` engine) and compares the results with what `mc validate` returned.

- Sample: all metrics in `examples/contracts/retail.yml`. The test fails if any metric lacks an independent recomputation.
- Threshold: **100% agreement**. Exact equality for integer counts and sums; 1e-9 relative/absolute tolerance for real-valued sums and ratios.
- A failure lists every mismatching metric with the engine value and the independent value.

The broken pack is the negative control: `tests/test_smoke.py` asserts that each of its seven metrics fails with the specific expected reason.

## Development

```bash
pip install -e '.[test]'
pytest
```

## Scope

In v0.1: SQLite engine, the contract format above, `validate` / `docs` / `dax`. Not yet: live warehouse connectors (Snowflake, BigQuery), dbt integration, DuckDB, a UI, metric lineage graphs.

## License

MIT, see [LICENSE](LICENSE).
