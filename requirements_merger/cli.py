"""Command line interface for the requirements merger.

Usage::

    python -m requirements_merger input.json
    cat input.json | python -m requirements_merger

Input shape::

    {
      "plugins": [{"name": "a", "requires_dist": ["requests>=2.0"]}],
      "targets": [{"name": "win", "extras": ["gui"],
                   "environment": {"sys_platform": "win32"}}]
    }

Exit status is ``1`` when at least one target has a proven conflict and ``0``
otherwise, so the tool is usable from checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from requirements_merger.core import merge


def _load(path: str | None) -> dict[str, Any]:
    if path is None or path == "-":
        payload = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    if not isinstance(payload, dict):
        msg = "input must be a JSON object with 'plugins' and 'targets'"
        raise SystemExit(msg)
    plugins = payload.get("plugins", [])
    targets = payload.get("targets", [])
    if not isinstance(plugins, list) or not isinstance(targets, list):
        msg = "'plugins' and 'targets' must be lists"
        raise SystemExit(msg)
    return {"plugins": plugins, "targets": targets}


def _has_conflict(report: dict[str, Any]) -> bool:
    return any(
        package["verdict"] == "conflict"
        for target in report["targets"]
        for package in target["packages"].values()
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="requirements_merger",
        description=(
            "Merge plugin Requires-Dist constraints over a finite target "
            "environment matrix without resolving package versions."
        ),
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="JSON input file (default: stdin)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation (default: 2, use 0 for compact output)",
    )
    args = parser.parse_args(argv)

    payload = _load(args.input)
    report = merge(payload["plugins"], payload["targets"])
    indent = args.indent or None
    sys.stdout.write(json.dumps(report, indent=indent, sort_keys=False) + "\n")
    return 1 if _has_conflict(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
