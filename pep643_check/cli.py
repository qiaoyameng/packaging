"""Command line entry point for the PEP 643 metadata comparison."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .core import Change, ChangeKind, compare_metadata
from .loader import load_document

_EXIT_OK = 0
_EXIT_VIOLATION = 1
_EXIT_UNCERTAIN = 2

_KIND_HEADERS = {
    ChangeKind.VIOLATION: "Violations of the sdist's static metadata promise",
    ChangeKind.UNCERTAIN: "Differences that could not be proven equivalent",
    ChangeKind.ALLOWED: "Allowed differences (declared Dynamic / private extension)",
}
_KIND_ORDER = [ChangeKind.VIOLATION, ChangeKind.UNCERTAIN, ChangeKind.ALLOWED]


def _read(value: str) -> object:
    if value == "-":
        return sys.stdin.read()
    if value.endswith(".json"):
        with open(value, encoding="utf-8") as handle:
            return json.load(handle)
    with open(value, "rb") as handle:
        return handle.read()


def _value_text(value: object) -> str:
    if value is None:
        return "<absent>"
    if isinstance(value, list):
        return "\n".join(f"      - {item}" for item in value)
    if isinstance(value, dict):
        return "\n".join(f"      {key}: {item}" for key, item in value.items())
    text = str(value)
    if "\n" in text:
        return "\n      " + text.replace("\n", "\n      ")
    return text


def render_text(changes: list[Change]) -> str:
    visible = [change for change in changes if change.kind is not ChangeKind.EQUAL]
    if not visible:
        return "No differences found; wheel metadata keeps the sdist promises."

    blocks: list[str] = []
    for kind in _KIND_ORDER:
        group = [change for change in visible if change.kind is kind]
        if not group:
            continue
        blocks.append(f"{_KIND_HEADERS[kind]} ({len(group)})")
        for change in group:
            blocks.append(f"- field: {change.field}")
            blocks.append(f"  rule: {change.rule}")
            if change.detail:
                blocks.append(f"  why: {change.detail}")
            blocks.append(f"  sdist: {_value_text(change.old)}")
            blocks.append(f"  wheel: {_value_text(change.new)}")
    return "\n".join(blocks)


def change_to_json(change: Change) -> dict[str, object]:
    return {
        "field": change.field,
        "kind": change.kind.value,
        "rule": change.rule,
        "detail": change.detail,
        "old": change.old,
        "new": change.new,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pep643_check",
        description=(
            "Compare the Core Metadata promised by an sdist (PKG-INFO) with "
            "the Core Metadata emitted in a wheel (METADATA) using PEP 643 "
            "Dynamic rules. The inputs and build backend are never touched."
        ),
    )
    parser.add_argument(
        "sdist",
        help="sdist PKG-INFO file ('-' reads email text from standard input)",
    )
    parser.add_argument(
        "wheel",
        help="wheel METADATA file ('-' reads email text from standard input)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a text report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sdist == "-" and args.wheel == "-":
        raise SystemExit("only one input may be read from standard input")

    sdist = load_document(_read(args.sdist))
    wheel = load_document(_read(args.wheel))
    report = compare_metadata(sdist, wheel)

    if args.json:
        payload = {
            "ok": report.ok,
            "violations": [change_to_json(change) for change in report.violations],
            "uncertain": [change_to_json(change) for change in report.uncertain],
            "allowed": [change_to_json(change) for change in report.allowed],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(report.changes))

    if report.violations:
        return _EXIT_VIOLATION
    if report.uncertain:
        return _EXIT_UNCERTAIN
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
