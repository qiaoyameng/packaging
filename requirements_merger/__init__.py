"""Merge plugin ``Requires-Dist`` lists across a finite target matrix.

Requirements are grouped by PEP 503/PEP 685 normalized distribution name.
For every target environment (with its selected extras) each requirement's
marker is evaluated, then the surviving version ranges are combined with
:class:`packaging.specifiers.SpecifierSet`. No package version resolution is
attempted and no index is accessed.
"""

from __future__ import annotations

from requirements_merger.core import (
    merge,
    resolve_marker,
)

__all__ = ["merge", "resolve_marker"]
