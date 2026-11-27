"""Loading and schema validation of metric contracts.

A contract file is YAML holding either a single metric::

    metric:
      name: total_revenue
      ...

or many::

    metrics:
      - name: total_revenue
        ...

Schema validation is pure (no database). Column/table existence is checked by
the engine, which has the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REQUIRED_STR_FIELDS = ("name", "label", "owner", "description", "table", "sql")
OPTIONAL_LIST_FIELDS = ("grain", "dimensions", "filters")
KNOWN_FIELDS = set(REQUIRED_STR_FIELDS) | set(OPTIONAL_LIST_FIELDS) | {"tests"}
KNOWN_TESTS = {"not_null", "non_negative", "between"}


@dataclass(frozen=True)
class Tests:
    not_null: bool = False
    non_negative: bool = False
    between: tuple[float, float] | None = None

    def describe(self) -> list[str]:
        """Human-readable list of the declared tests, in a fixed order."""
        out = []
        if self.not_null:
            out.append("not_null")
        if self.non_negative:
            out.append("non_negative")
        if self.between is not None:
            out.append(f"between [{_num(self.between[0])}, {_num(self.between[1])}]")
        return out


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    owner: str
    description: str
    table: str
    sql: str
    source: str  # contract file the metric came from (display path)
    grain: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    tests: Tests = field(default_factory=Tests)


@dataclass(frozen=True)
class SchemaError:
    """A contract-level problem: file + metric + field + message."""

    source: str
    metric: str  # metric name, or "<metric #N>" when the name itself is missing
    field: str | None
    message: str

    def __str__(self) -> str:
        where = f"{self.source}: metric '{self.metric}'"
        if self.field:
            where += f", field '{self.field}'"
        return f"{where}: {self.message}"


@dataclass
class ContractPack:
    metrics: list[Metric]
    errors: list[SchemaError]
    # metric id -> list of errors, used to report schema failures per metric
    failed: dict[tuple[str, str], list[SchemaError]] = field(default_factory=dict)


def _num(x: float) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def discover(paths: list[str | Path]) -> list[Path]:
    """Expand files/directories into a sorted, de-duplicated list of YAML files."""
    found: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found.extend(sorted(q for q in p.rglob("*") if q.suffix in (".yml", ".yaml") and q.is_file()))
        elif p.is_file():
            found.append(p)
        else:
            raise FileNotFoundError(f"contract path not found: {p}")
    seen: set[Path] = set()
    out = []
    for f in found:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _check_sql_fragment(value: str) -> str | None:
    if ";" in value:
        return "must be a single SQL expression (';' is not allowed)"
    if "--" in value or "/*" in value:
        return "SQL comments are not allowed"
    return None


def _validate_metric(raw: Any, source: str, index: int) -> tuple[Metric | None, list[SchemaError]]:
    errors: list[SchemaError] = []
    if not isinstance(raw, dict):
        return None, [SchemaError(source, f"<metric #{index}>", None, "metric entry must be a mapping")]

    name = raw.get("name")
    ident = name if isinstance(name, str) and name else f"<metric #{index}>"

    def err(fld: str | None, msg: str) -> None:
        errors.append(SchemaError(source, ident, fld, msg))

    for key in sorted(k for k in raw if k not in KNOWN_FIELDS):
        err(str(key), f"unknown field (allowed: {', '.join(sorted(KNOWN_FIELDS))})")

    values: dict[str, Any] = {}
    for fld in REQUIRED_STR_FIELDS:
        if fld not in raw or raw[fld] is None:
            err(fld, "missing required field")
            continue
        v = raw[fld]
        if not isinstance(v, str) or not v.strip():
            err(fld, "must be a non-empty string")
            continue
        values[fld] = v.strip()

    if "name" in values and not NAME_RE.match(values["name"]):
        err("name", "must be snake_case (lowercase letters, digits, underscores; starts with a letter)")
    if "table" in values and not IDENT_RE.match(values["table"]):
        err("table", "must be a plain table or view identifier")
    if "sql" in values:
        problem = _check_sql_fragment(values["sql"])
        if problem:
            err("sql", problem)
        elif re.match(r"^\s*select\b", values["sql"], re.IGNORECASE):
            err("sql", "give the aggregate expression only (e.g. SUM(revenue)), not a full SELECT")

    lists: dict[str, tuple[str, ...]] = {}
    for fld in OPTIONAL_LIST_FIELDS:
        v = raw.get(fld, [])
        if v is None:
            v = []
        if not isinstance(v, list) or not all(isinstance(i, str) and i.strip() for i in v):
            err(fld, "must be a list of non-empty strings")
            continue
        items = tuple(i.strip() for i in v)
        if fld in ("grain", "dimensions"):
            bad = [i for i in items if not IDENT_RE.match(i)]
            if bad:
                err(fld, f"not a column identifier: {', '.join(bad)}")
                continue
        if fld == "filters":
            for i in items:
                problem = _check_sql_fragment(i)
                if problem:
                    err(fld, f"{problem}: {i!r}")
        lists[fld] = items

    tests = Tests()
    raw_tests = raw.get("tests", {})
    if raw_tests is None:
        raw_tests = {}
    if not isinstance(raw_tests, dict):
        err("tests", "must be a mapping of test name to setting")
    else:
        for key in sorted(str(k) for k in raw_tests if k not in KNOWN_TESTS):
            err(f"tests.{key}", f"unknown test (allowed: {', '.join(sorted(KNOWN_TESTS))})")
        nn = raw_tests.get("not_null", False)
        ng = raw_tests.get("non_negative", False)
        bt = raw_tests.get("between")
        ok = True
        if not isinstance(nn, bool):
            err("tests.not_null", "must be true or false")
            ok = False
        if not isinstance(ng, bool):
            err("tests.non_negative", "must be true or false")
            ok = False
        between = None
        if bt is not None:
            if not (isinstance(bt, list) and len(bt) == 2 and all(_is_number(x) for x in bt)):
                err("tests.between", "must be a two-element list of numbers [lo, hi]")
                ok = False
            elif bt[0] > bt[1]:
                err("tests.between", f"lower bound {bt[0]} is greater than upper bound {bt[1]}")
                ok = False
            else:
                between = (bt[0], bt[1])
        if ok:
            tests = Tests(not_null=nn, non_negative=ng, between=between)

    if errors:
        return None, errors
    return (
        Metric(
            name=values["name"],
            label=values["label"],
            owner=values["owner"],
            description=values["description"],
            table=values["table"],
            sql=values["sql"],
            source=source,
            grain=lists["grain"],
            dimensions=lists["dimensions"],
            filters=lists["filters"],
            tests=tests,
        ),
        [],
    )


def display_path(path: Path) -> str:
    """Stable, machine-independent path for provenance and generated output."""
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


class NotAContractError(ValueError):
    """The path is clearly not a YAML contract (e.g. a binary or SQLite file)."""


class DuplicateKeyError(yaml.constructor.ConstructorError):
    def __init__(self, key: Any, first: yaml.Mark, dup: yaml.Mark) -> None:
        self.key = key
        self.first_line = first.line + 1
        self.dup_line = dup.line + 1
        super().__init__(
            None, None, f"duplicate key '{key}' on line {self.dup_line} (first defined on line {self.first_line})"
        )


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys at any nesting level.

    Plain PyYAML keeps the last value silently, which can drop a guard such as
    a second ``between:`` or swap a metric's ``sql:`` without any warning.
    Merge keys (``<<: *anchor``) keep their normal YAML override semantics.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            seen: dict[Any, yaml.Mark] = {}
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    hash(key)
                except TypeError:
                    continue  # unhashable keys are reported by the base constructor
                if key in seen:
                    raise DuplicateKeyError(key, seen[key], key_node.start_mark)
                seen[key] = key_node.start_mark
        return super().construct_mapping(node, deep=deep)


def parse_yaml(text: str) -> Any:
    return yaml.load(text, Loader=UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _sniff_binary(path: Path, data: bytes) -> None:
    if data.startswith(_SQLITE_MAGIC):
        raise NotAContractError(f"not a contract file: looks like a SQLite database? {path}")
    if b"\x00" in data[:8192]:
        raise NotAContractError(f"not a contract file: binary content in {path}")


def load_file(path: Path) -> tuple[list[Metric], list[SchemaError]]:
    source = display_path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [], [SchemaError(source, "<file>", None, f"cannot read file: {exc}")]
    _sniff_binary(path, data)
    try:
        doc = parse_yaml(data.decode("utf-8"))
    except DuplicateKeyError as exc:
        return [], [SchemaError(source, "<file>", str(exc.key), exc.problem)]
    except yaml.YAMLError as exc:
        return [], [SchemaError(source, "<file>", None, "invalid YAML: " + " ".join(str(exc).split()))]
    except (OSError, UnicodeDecodeError) as exc:
        return [], [SchemaError(source, "<file>", None, f"cannot read file: {exc}")]

    if isinstance(doc, dict) and "metrics" in doc:
        entries = doc["metrics"]
        if not isinstance(entries, list):
            return [], [SchemaError(source, "<file>", "metrics", "must be a list of metrics")]
    elif isinstance(doc, dict) and "metric" in doc:
        entries = [doc["metric"]]
    else:
        return [], [SchemaError(source, "<file>", None, "expected a top-level 'metrics:' list or 'metric:' mapping")]

    metrics, errors = [], []
    for i, raw in enumerate(entries, start=1):
        m, errs = _validate_metric(raw, source, i)
        errors.extend(errs)
        if m is not None:
            metrics.append(m)
    return metrics, errors


def load_pack(paths: list[str | Path]) -> ContractPack:
    """Load and schema-validate every contract under ``paths``.

    Returns the valid metrics plus every schema error. Duplicate metric names
    across the pack are an error on the second and later occurrences.
    """
    metrics: list[Metric] = []
    errors: list[SchemaError] = []
    for f in discover(paths):
        ms, errs = load_file(f)
        metrics.extend(ms)
        errors.extend(errs)

    seen: dict[str, str] = {}
    unique: list[Metric] = []
    for m in metrics:
        if m.name in seen:
            errors.append(SchemaError(m.source, m.name, "name", f"duplicate metric name (first defined in {seen[m.name]})"))
        else:
            seen[m.name] = m.source
            unique.append(m)

    failed: dict[tuple[str, str], list[SchemaError]] = {}
    for e in errors:
        failed.setdefault((e.source, e.metric), []).append(e)
    return ContractPack(metrics=unique, errors=errors, failed=failed)
