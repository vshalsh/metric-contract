"""`mc` command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, dax, docs
from .contract import ContractPack, load_pack
from .engine import PASS, InvalidDatabaseError, format_value, validate
from .style import Style, color_enabled

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def _load(paths: list[str]) -> ContractPack | None:
    try:
        return load_pack(paths)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_validate(args: argparse.Namespace) -> int:
    pack = _load(args.contracts)
    if pack is None:
        return EXIT_USAGE
    try:
        results = validate(pack, args.db)
    except (FileNotFoundError, InvalidDatabaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    failed = sum(r.status != PASS for r in results)
    if args.format == "json":
        payload = {
            "db": Path(args.db).as_posix(),
            "summary": {"total": len(results), "passed": len(results) - failed, "failed": failed},
            "results": [r.provenance() for r in results],
        }
        if not results:
            payload["error"] = "no metrics found"
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_FAIL if failed or not results else EXIT_OK

    st = Style(color_enabled(sys.stdout, args.no_color))
    if not results:
        print("no metrics found")
        return EXIT_FAIL
    width = max(len(r.metric) for r in results)
    for r in results:
        value = format_value(r.value) if r.returned else "-"
        print(f"{st.status(r.status)}  {r.metric.ljust(width)}  value={value}  {st.dim('[' + r.source + ']')}")
        if r.executed_sql:
            print(st.dim(f"      sql: {r.executed_sql}"))
        for reason in r.reasons:
            print(f"      reason: {reason}")
    summary = f"{len(results)} metrics: {len(results) - failed} passed, {failed} failed"
    print(st.status("FAIL" if failed else "PASS") + "  " + summary)
    return EXIT_FAIL if failed else EXIT_OK


def _report_schema_errors(pack: ContractPack, st: Style) -> None:
    for e in pack.errors:
        print(f"{st.status('FAIL')}  {e}", file=sys.stderr)


def _write(text: str, output: str | None) -> None:
    if output:
        # newline="" keeps "\n" line endings on every OS, so output is byte-identical.
        with open(output, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


def _generate(args: argparse.Namespace, render) -> int:
    pack = _load(args.contracts)
    if pack is None:
        return EXIT_USAGE
    if pack.errors:
        _report_schema_errors(pack, Style(color_enabled(sys.stderr, args.no_color)))
        print(f"error: {len(pack.errors)} contract error(s); nothing written", file=sys.stderr)
        return EXIT_FAIL
    if not pack.metrics:
        print("error: no metrics found", file=sys.stderr)
        return EXIT_FAIL
    _write(render(pack.metrics), args.output)
    if args.output:
        print(f"wrote {len(pack.metrics)} metrics to {args.output}", file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mc", description="metric-contract: KPI-as-code for BI and data teams.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("contracts", nargs="+", help="contract YAML file(s) or directories")
    common.add_argument("--no-color", action="store_true", help="disable colour (NO_COLOR is also honoured)")

    v = sub.add_parser("validate", parents=[common], help="schema-check and execute contracts against SQLite")
    v.add_argument("--db", required=True, help="path to a SQLite database file")
    v.add_argument("--format", choices=["text", "json"], default="text", help="output format (json is colour-free)")
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("docs", parents=[common], help="generate a markdown measure dictionary")
    d.add_argument("-o", "--output", help="output file (default: stdout)")
    d.set_defaults(func=lambda a: _generate(a, docs.render))

    x = sub.add_parser("dax", parents=[common], help="generate Power BI DAX measure stubs")
    x.add_argument("-o", "--output", help="output file (default: stdout)")
    x.set_defaults(func=lambda a: _generate(a, dax.render))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
