"""Generate Power BI DAX measure stubs from metric contracts.

Translation is intentionally conservative. Supported SQL expression subset:

* aggregates ``SUM(col)``, ``AVG(col)``, ``MIN(col)``, ``MAX(col)``,
  ``COUNT(*)``, ``COUNT(col)``, ``COUNT(DISTINCT col)``
* numeric literals, ``+ - * /`` and parentheses (``a / b`` becomes ``DIVIDE(a, b)``)

Supported filter subset (become CALCULATE filter arguments):

* ``col <op> literal`` with ``= <> != < <= > >=``
* ``col IN ('a', 'b')`` / ``col NOT IN (...)``
* ``col IS NULL`` / ``col IS NOT NULL``

Anything else is emitted as a ``BLANK()`` stub (expression) or carried as a
``//`` comment (filter) so a human finishes it - never silently guessed.
Output is deterministic: same contracts in, byte-identical file out.
"""

from __future__ import annotations

import re

from .contract import Metric

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[()+\-*/,])"
    r"|(?P<other>>=|<=|<>|!=|=|<|>|'(?:[^']|'')*'))"
)

_AGG_MAP = {"SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT"}


class Untranslatable(Exception):
    pass


def _tokenize(text: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise Untranslatable(f"unsupported syntax near {text[pos:pos + 15]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _col(table: str, column: str) -> str:
    return f"'{table}'[{column}]"


class _Parser:
    def __init__(self, text: str, table: str) -> None:
        self.toks = _tokenize(text)
        self.i = 0
        self.table = table

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, value: str | None = None, kind: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (value is not None and tok[1].upper() != value) or (kind is not None and tok[0] != kind):
            raise Untranslatable(f"expected {value or kind}")
        self.i += 1
        return tok[1]

    def parse(self) -> str:
        out = self.expr()
        if self.peek() is not None:
            raise Untranslatable(f"unexpected token {self.peek()[1]!r}")
        return out

    def expr(self) -> str:
        left = self.term()
        while (tok := self.peek()) and tok[1] in "+-" and tok[0] == "op":
            self.i += 1
            left = f"{left} {tok[1]} {self.term()}"
        return left

    def term(self) -> str:
        left = self.factor()
        while (tok := self.peek()) and tok[1] in "*/" and tok[0] == "op":
            self.i += 1
            right = self.factor()
            left = f"DIVIDE({left}, {right})" if tok[1] == "/" else f"{left} * {right}"
        return left

    def factor(self) -> str:
        tok = self.peek()
        if tok is None:
            raise Untranslatable("unexpected end of expression")
        kind, val = tok
        if kind == "num":
            self.i += 1
            return val
        if kind == "op" and val == "(":
            self.i += 1
            inner = self.expr()
            self.take(")")
            return f"({inner})"
        if kind == "op" and val == "-":
            self.i += 1
            return f"-{self.factor()}"
        if kind == "ident" and val.upper() in _AGG_MAP:
            return self.aggregate()
        raise Untranslatable(f"unsupported token {val!r}")

    def aggregate(self) -> str:
        fn = self.take(kind="ident").upper()
        self.take("(")
        tok = self.peek()
        if fn == "COUNT" and tok == ("op", "*"):
            self.i += 1
            self.take(")")
            return f"COUNTROWS('{self.table}')"
        distinct = False
        if tok and tok[0] == "ident" and tok[1].upper() == "DISTINCT":
            if fn != "COUNT":
                raise Untranslatable(f"{fn}(DISTINCT ...) is not supported")
            self.i += 1
            distinct = True
        col = self.take(kind="ident")
        if col.upper() in _AGG_MAP or col.upper() in ("CASE", "CAST", "DISTINCT"):
            raise Untranslatable(f"unsupported argument {col!r}")
        self.take(")")
        if distinct:
            return f"DISTINCTCOUNT({_col(self.table, col)})"
        return f"{_AGG_MAP[fn]}({_col(self.table, col)})"


def translate_expression(sql: str, table: str) -> str:
    """Translate a SQL aggregate expression to DAX, or raise Untranslatable."""
    return _Parser(sql, table).parse()


_LIT = r"(?:'(?:[^']|'')*'|-?\d+(?:\.\d+)?)"
_CMP_RE = re.compile(rf"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=|<>|!=|<=|>=|<|>)\s*({_LIT})\s*$")
_IN_RE = re.compile(rf"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+(NOT\s+)?IN\s*\(\s*({_LIT}(?:\s*,\s*{_LIT})*)\s*\)\s*$", re.I)
_NULL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+(NOT\s+)?NULL\s*$", re.I)


def _dax_literal(lit: str) -> str:
    if lit.startswith("'"):
        inner = lit[1:-1].replace("''", "'").replace('"', '""')
        return f'"{inner}"'
    return lit


def translate_filter(flt: str, table: str) -> str | None:
    """Translate one SQL boolean filter to a DAX filter argument, or None."""
    m = _CMP_RE.match(flt)
    if m:
        col, op, lit = m.groups()
        op = "<>" if op == "!=" else op
        return f"{_col(table, col)} {op} {_dax_literal(lit)}"
    m = _IN_RE.match(flt)
    if m:
        col, neg, items = m.groups()
        lits = re.findall(_LIT, items)
        body = f"{_col(table, col)} IN {{ {', '.join(_dax_literal(x) for x in lits)} }}"
        return f"NOT ( {body} )" if neg else body
    m = _NULL_RE.match(flt)
    if m:
        col, neg = m.groups()
        body = f"ISBLANK({_col(table, col)})"
        return f"NOT {body}" if neg else body
    return None


def metric_to_dax(m: Metric) -> str:
    lines = [f"// ---- {m.name} ----"]
    lines += [f"// Label:       {m.label}", f"// Owner:       {m.owner}"]
    desc = m.description.strip().splitlines()
    lines.append(f"// Description: {desc[0]}".rstrip())
    lines += [f"//              {d}".rstrip() for d in desc[1:]]
    lines.append(f"// Grain:       {', '.join(m.grain) if m.grain else '(none declared)'}")
    lines.append(f"// Dimensions:  {', '.join(m.dimensions) if m.dimensions else '(none declared)'}")
    lines.append(f"// Source:      {m.source}")
    lines.append(f"// SQL:         {m.sql}")
    tests = m.tests.describe()
    lines.append(f"// Tests:       {', '.join(tests) if tests else '(none)'}")

    try:
        expr = translate_expression(m.sql, m.table)
        expr_note = None
    except Untranslatable as exc:
        expr = "BLANK()"
        expr_note = f"// TODO: SQL expression not auto-translatable ({exc}); translate by hand: {m.sql}"

    dax_filters, untranslated = [], []
    for f in m.filters:
        t = translate_filter(f, m.table)
        if t is None:
            untranslated.append(f)
        else:
            dax_filters.append(t)

    if expr_note:
        lines.append(expr_note)
    for f in untranslated:
        lines.append(f"// TODO: filter not auto-translatable, apply by hand: {f}")

    # Measure name is the business label, in Power BI "New measure" bar syntax.
    if dax_filters:
        lines.append(f"{m.label} =")
        lines.append("CALCULATE(")
        lines.append(f"    {expr},")
        for i, f in enumerate(dax_filters):
            sep = "," if i < len(dax_filters) - 1 else ""
            lines.append(f"    {f}{sep}")
        lines.append(")")
    else:
        lines.append(f"{m.label} = {expr}")
    return "\n".join(lines)


def render(metrics: list[Metric]) -> str:
    header = [
        "// Power BI DAX measure stubs generated by metric-contract (mc dax).",
        "// Do not edit by hand: change the YAML contract and regenerate.",
        f"// Metrics: {len(metrics)}",
        "",
    ]
    body = "\n\n".join(metric_to_dax(m) for m in metrics)
    return "\n".join(header) + "\n" + body + "\n"
