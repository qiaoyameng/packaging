"""Result types for the PEP 643 metadata comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ChangeKind(str, Enum):
    """Outcome of comparing one field between sdist and wheel metadata.

    The four values intentionally distinguish "proven equal", "proven
    allowed", "proven a violation" and "cannot be proven equivalent".
    """

    EQUAL = "equal"
    """The two sides are semantically identical (no real difference)."""

    ALLOWED = "allowed"
    """The field is Dynamic (PEP 643), so the difference is permitted."""

    VIOLATION = "violation"
    """The field is a static promise and the wheel changed it."""

    UNCERTAIN = "uncertain"
    """Both values parse, but equivalence could neither be proven nor refuted."""


@dataclass(frozen=True)
class Change:
    """One field-level finding.

    ``old`` is the sdist (promise) value and ``new`` the wheel value. Both
    keep original spellings: canonicalization is only used internally.
    """

    field: str
    old: object | None
    new: object | None
    kind: ChangeKind
    rule: str
    detail: str | None = None

    @property
    def is_difference(self) -> bool:
        return self.kind is not ChangeKind.EQUAL


@dataclass
class MetadataDocument:
    """One side of the comparison.

    ``parsed`` holds successfully recognized core metadata; ``raw`` holds the
    original text values before validation so invalid data can still be shown.
    ``unknown`` holds unrecognized email headers (case-insensitive).
    """

    raw: dict[str, list[str] | str]
    parsed: dict[str, object]
    unknown: dict[str, list[str]]
    invalid: dict[str, str]
    metadata_version: str | None
    dynamic: frozenset[str]
    has_dynamic_semantics: bool


@dataclass
class ComparisonReport:
    """Aggregate result of :func:`pep643_check.core.compare_metadata`."""

    changes: list[Change] = field(default_factory=list)

    @property
    def violations(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.VIOLATION]

    @property
    def uncertain(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.UNCERTAIN]

    @property
    def allowed(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.ALLOWED]

    @property
    def ok(self) -> bool:
        """True only when no static promise was broken.

        Uncertain findings make this False: this checker must not certify
        metadata it cannot reason about.
        """

        return not self.violations and not self.uncertain
