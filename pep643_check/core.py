"""PEP 643 comparison between sdist and wheel Core Metadata."""

from __future__ import annotations

from typing import Any, Iterable

from .loader import load_document
from .model import Change, ChangeKind, ComparisonReport, MetadataDocument
from .rules import NEVER_DYNAMIC, FieldKind, field_kind
from . import semantic as sem

MISSING = sem._MISSING

__all__ = [
    "Change",
    "ChangeKind",
    "ComparisonReport",
    "MetadataDocument",
    "compare_metadata",
    "load_metadata",
]


def load_metadata(source: Any) -> MetadataDocument:
    """Accept email text/bytes, RawMetadata-like mapping, or Metadata."""

    return load_document(source)


def _display(value: Any) -> object | None:
    if value is MISSING:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if value is None:
        return None
    return str(value)


def _get_value(document: MetadataDocument, field: str) -> Any:
    # Comparators consume original spellings; semantic parsing is done inside
    # them. This also makes raw and parsed inputs follow identical rules.
    if field in document.raw:
        return document.raw[field]
    if field in document.invalid:
        return document.invalid[field]
    return MISSING


def _is_present(value: Any) -> bool:
    if value is MISSING or value is None:
        return False
    if isinstance(value, str) and not value:
        return False
    if isinstance(value, (list, tuple, dict)) and not value:
        return False
    return True


def _change(
    field: str,
    old: Any,
    new: Any,
    kind: ChangeKind,
    rule: str,
    detail: str | None = None,
) -> Change:
    return Change(
        field=field,
        old=_display(old),
        new=_display(new),
        kind=kind,
        rule=rule,
        detail=detail,
    )


def _compare_known_field(
    field: str, sdist: MetadataDocument, wheel: MetadataDocument
) -> Change | None:
    kind = field_kind(field)
    old = _get_value(sdist, field)
    new = _get_value(wheel, field)
    email_field = sem.email_name(field)

    # PEP 643: only a field the sdist names in Dynamic may change in the wheel.
    if field == "dynamic":
        if sem.compare_multiset(old, new) == sem.EQUAL:
            return None
        if not (sdist.has_dynamic_semantics and wheel.has_dynamic_semantics):
            if (
                not sdist.has_dynamic_semantics
                and wheel.has_dynamic_semantics
                and not _is_present(old)
            ):
                rule = "dynamic-not-promised-by-sdist"
                detail = "the sdist made no Dynamic declaration; a wheel cannot add one"
            else:
                rule = "no-dynamic-semantics-before-metadata-2.2"
                detail = "Dynamic has no PEP 643 meaning in Metadata-Version < 2.2"
            return _change(field, old, new, ChangeKind.VIOLATION, rule, detail)
        if not _is_present(old):
            return _change(
                field,
                old,
                new,
                ChangeKind.VIOLATION,
                "dynamic-not-promised-by-sdist",
                "a wheel-only Dynamic declaration does not make the sdist field dynamic",
            )
        return _change(
            field,
            old,
            new,
            ChangeKind.ALLOWED,
            "dynamic-declaration-may-change",
            "Dynamic is itself metadata about which fields may change",
        )

    if email_field.lower() in sdist.dynamic:
        if not sdist.has_dynamic_semantics:
            # Pre-PEP-643 sdists have the header syntactically but it grants no
            # permission. An unchanged value remains equal, while any actual
            # difference is reported as a static violation with that reason.
            if sem.compare_scalar(old, new) != sem.EQUAL:
                return _change(
                    field,
                    old,
                    new,
                    ChangeKind.VIOLATION,
                    "pre-pep-643-dynamic-header-has-no-semantics",
                    "the sdist predates Metadata-Version 2.2, so Dynamic grants nothing",
                )
        elif sem.compare_scalar(old, new) == sem.EQUAL:
            return None
        else:
            never_dynamic = field in NEVER_DYNAMIC
            return _change(
                field,
                old,
                new,
                ChangeKind.VIOLATION if never_dynamic else ChangeKind.ALLOWED,
                "never-dynamic" if never_dynamic else "declared-dynamic-pep-643",
                "Name, Version and Metadata-Version cannot be Dynamic"
                if never_dynamic
                else f"{email_field} is declared Dynamic in the sdist",
            )

    if field == "license_expression":
        result, detail = sem.compare_license_expression(old, new)
        return _semantic_change(
            field, old, new, result, "static-license-expression-changed", detail
        )

    if kind is FieldKind.SCALAR:
        if sem.compare_scalar(old, new) == sem.EQUAL:
            return None
        if field == "metadata_version":
            return _compare_metadata_version(old, new)
        return _change(
            field,
            old,
            new,
            ChangeKind.VIOLATION,
            "static-scalar-changed",
        )

    if kind is FieldKind.NAME:
        result, detail = sem.compare_name(old, new)
        return _semantic_change(field, old, new, result, "static-name-changed", detail)

    if kind is FieldKind.VERSION:
        result, detail = sem.compare_version(old, new)
        return _semantic_change(field, old, new, result, "static-version-changed", detail)

    if kind is FieldKind.REQUIRES_PYTHON:
        result, detail = sem.compare_requires_python(old, new)
        return _semantic_change(
            field, old, new, result, "static-requires-python-changed", detail
        )

    if kind is FieldKind.REQUIRES_DIST:
        result, reasons = sem.compare_requires_dist(old, new)
        return _semantic_change(
            field,
            old,
            new,
            result,
            "static-requires-dist-changed",
            "; ".join(reason for reason in reasons if reason) or None,
        )

    if kind is FieldKind.KEYWORDS:
        result = sem.compare_keywords(old, new)
        return _semantic_change(field, old, new, result, "static-keywords-changed")

    if kind is FieldKind.PROJECT_URLS:
        result, detail = sem.compare_project_urls(
            sdist.parsed.get(field) if isinstance(sdist.parsed.get(field), dict) else None,
            wheel.parsed.get(field) if isinstance(wheel.parsed.get(field), dict) else None,
            old,
            new,
        )
        return _semantic_change(
            field, old, new, result, "static-project-urls-changed", detail
        )

    if kind is FieldKind.SET:
        comparator = {
            "classifiers": sem.compare_classifiers,
            "provides_extra": sem.compare_provides_extra,
        }[field]
        result = comparator(old, new)
        rule = {
            "classifiers": "static-classifiers-changed",
            "provides_extra": "static-provides-extra-changed",
        }[field]
        return _semantic_change(field, old, new, result, rule)

    if kind is FieldKind.MULTISET:
        result = sem.compare_multiset(old, new)
        return _semantic_change(field, old, new, result, "static-multiset-field-changed")

    # License-Expression is a scalar but needs its own normalization.
    if field == "license_expression":
        result, detail = sem.compare_license_expression(old, new)
        return _semantic_change(
            field, old, new, result, "static-license-expression-changed", detail
        )

    return _change(
        field, old, new, ChangeKind.UNCERTAIN, "no-comparator-for-field"
    )


def _semantic_change(
    field: str,
    old: Any,
    new: Any,
    result: str,
    violation_rule: str,
    detail: str | None = None,
) -> Change | None:
    if result == sem.EQUAL:
        return None
    kind = ChangeKind.UNCERTAIN if result == sem.UNCERTAIN else ChangeKind.VIOLATION
    rule = "equivalence-unproven" if kind is ChangeKind.UNCERTAIN else violation_rule
    return _change(field, old, new, kind, rule, detail)


def _compare_metadata_version(old: Any, new: Any) -> Change:
    old_present = _is_present(old)
    new_present = _is_present(new)
    if not old_present and not new_present:
        return _change(
            "metadata_version",
            old,
            new,
            ChangeKind.EQUAL,
            "metadata-version-equal",
        )
    if not old_present or not new_present:
        return _change(
            "metadata_version",
            old,
            new,
            ChangeKind.VIOLATION,
            "static-metadata-version-removed-or-added",
        )

    old_text = str(old).strip()
    new_text = str(new).strip()
    if old_text == new_text:
        return _change(
            "metadata_version", old, new, ChangeKind.EQUAL, "metadata-version-equal"
        )

    try:
        old_tuple = tuple(int(part) for part in old_text.split("."))
        new_tuple = tuple(int(part) for part in new_text.split("."))
    except ValueError:
        old_tuple = new_tuple = None

    # A build tool may emit a newer Core Metadata revision than the sdist
    # supplied; that is not evidence of a changed semantic promise, but it is
    # not provably equal either. Downgrades and unrelated values are violations.
    if (
        old_tuple is not None
        and new_tuple is not None
        and len(old_tuple) >= 2
        and len(new_tuple) >= 2
        and old_tuple < new_tuple
    ):
        return _change(
            "metadata_version",
            old,
            new,
            ChangeKind.UNCERTAIN,
            "metadata-version-upgrade",
            "wheel emits a newer Core Metadata revision; semantic effect not proven equal",
        )
    return _change(
        "metadata_version",
        old,
        new,
        ChangeKind.VIOLATION,
        "static-metadata-version-downgraded-or-changed",
    )


def _compare_unknown(sdist: MetadataDocument, wheel: MetadataDocument) -> Iterable[Change]:
    headers = set(sdist.unknown) | set(wheel.unknown)
    for header in sorted(headers, key=str.lower):
        old = sdist.unknown.get(header, MISSING)
        new = wheel.unknown.get(header, MISSING)
        if sem.compare_multiset(old, new) == sem.EQUAL:
            continue
        # X-* headers are the registered extension escape hatch; tooling is
        # allowed to add its own private headers to a wheel.
        if header.lower().startswith("x-"):
            yield _change(
                header,
                old,
                new,
                ChangeKind.ALLOWED,
                "private-x-extension-ignored",
                "X-* extension headers are outside Core Metadata promises",
            )
        elif header.lower() in sdist.dynamic and sdist.has_dynamic_semantics:
            # The token is not a recognized Core field (a recognized field
            # would take the typed comparison path), so the Dynamic promise is
            # malformed and grants no PEP 643 permission.
            yield _change(
                header,
                old,
                new,
                ChangeKind.VIOLATION,
                "dynamic-reference-is-unrecognized",
                "Dynamic lists a field name that is not a Core Metadata field",
            )
        else:
            # Do not accuse a tool of changing a Core field we do not know, but
            # do not silently certify an unrecognized field either.
            yield _change(
                header,
                old,
                new,
                ChangeKind.UNCERTAIN,
                "unknown-extension-field",
                "field is not a recognized Core Metadata field",
            )


def compare_metadata(sdist: Any, wheel: Any) -> ComparisonReport:
    """Compare sdist promises against the metadata actually written in a wheel.

    *sdist* and *wheel* may each be raw email text/bytes, a
    :class:`packaging.metadata.RawMetadata`-like mapping, or a parsed
    :class:`packaging.metadata.Metadata`.
    """

    sdist_doc = load_metadata(sdist)
    wheel_doc = load_metadata(wheel)

    fields = set(sdist_doc.raw) | set(wheel_doc.raw)
    changes: list[Change] = []
    for field in sorted(fields, key=lambda name: sem.email_name(name).lower()):
        change = _compare_known_field(field, sdist_doc, wheel_doc)
        if change is not None:
            changes.append(change)
    changes.extend(_compare_unknown(sdist_doc, wheel_doc))

    # Stable, human-oriented ordering: proven violations first, then anything
    # that could not be proven equivalent, then permitted dynamic differences.
    rank = {
        ChangeKind.VIOLATION: 0,
        ChangeKind.UNCERTAIN: 1,
        ChangeKind.ALLOWED: 2,
        ChangeKind.EQUAL: 3,
    }
    changes.sort(key=lambda change: (rank[change.kind], change.field.lower()))
    return ComparisonReport(changes=changes)
