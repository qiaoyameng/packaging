# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Compare sdist and wheel Core Metadata using the PEP 643 Dynamic rules.

Run it against the two fixture pairs shipped in ``tests/metadata_check/``::

    python examples/check_sdist_wheel_metadata.py

The first pair shows a wheel that silently rewrites a *static* dependency
(``requests==2.28.0`` -> ``requests==2.32.3``) without declaring
``Dynamic: Requires-Dist`` -- a violation. The second pair makes the very same
value change legitimately because the sdist lists ``Requires-Dist`` as dynamic.
"""

from __future__ import annotations

import pathlib

from packaging.metadata_check import ChangeKind, check_metadata_consistency


FIXTURES = pathlib.Path(__file__).parent.parent / "tests" / "metadata_check"


def _show(label: str, sdist_path: pathlib.Path, wheel_path: pathlib.Path) -> bool:
    result = check_metadata_consistency(
        sdist_path.read_text(encoding="utf-8"),
        wheel_path.read_text(encoding="utf-8"),
    )

    print(f"\n{label}")
    print("=" * len(label))
    print(f"consistent (no proven violations): {result.ok}")
    for change in result.changes:
        marker = {
            ChangeKind.VIOLATION: "VIOLATION",
            ChangeKind.ALLOWED: "  allowed",
            ChangeKind.UNKNOWN: "  unknown",
        }[change.kind]
        print(f"  [{marker}] {change.field}")
        print(f"      rule: {change.rule.id}")
        print(f"      sdist: {change.old!r}")
        print(f"      wheel: {change.new!r}")
        print(f"      {change.message}")
    return result.ok


def main() -> None:
    stolen_ok = _show(
        "1. Static dependency secretly changed",
        FIXTURES / "stolen-static-dependency.sdist.metadata",
        FIXTURES / "stolen-static-dependency.wheel.metadata",
    )
    dynamic_ok = _show(
        "2. Legitimate dynamic dependency change",
        FIXTURES / "legal-dynamic-dependency.sdist.metadata",
        FIXTURES / "legal-dynamic-dependency.wheel.metadata",
    )
    print()
    status_1 = "ok" if stolen_ok else "rejected!"
    status_2 = "ok" if dynamic_ok else "rejected!"
    print(f"case 1 (must fail): {status_1}")
    print(f"case 2 (must pass): {status_2}")


if __name__ == "__main__":
    main()
