# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Check a wheel's Core Metadata against an sdist's static metadata promises.

`PEP 643 <https://peps.python.org/pep-0643/>`_ allows the metadata of a
:term:`source distribution` (sdist) to be partly generated at build time: a
field whose name appears in the sdist's :mailheader:`Dynamic` header is
permitted to change (or be filled in) when the sdist is built into a
:term:`wheel`. Every other value is a *static* promise that the wheel must
keep. :mailheader:`Name`, :mailheader:`Version`, and
:mailheader:`Metadata-Version` can never be dynamic.

The entry point is :func:`check_metadata_consistency`. It takes two pieces of
Core Metadata -- the sdist's (the promise) and the wheel's (the result) --
each either as raw email text/bytes, an already-parsed
:class:`~packaging.metadata.RawMetadata` mapping, or a validated
:class:`~packaging.metadata.Metadata`. The inputs are never modified and no
build backend is invoked.

Each reported :class:`FieldChange` carries the field name, the *old* (sdist)
and *new* (wheel) raw values, the :class:`Rule` that applies, and a
:class:`ChangeKind`:

* :attr:`ChangeKind.VIOLATION` -- the wheel broke a static promise;
* :attr:`ChangeKind.ALLOWED` -- the difference is permitted (typically because
  the field is dynamic, or the metadata version simply advanced);
* :attr:`ChangeKind.UNKNOWN` -- the raw values differ, but whether the meaning
  is the same could not be proven (e.g. an unrecognized extension field, an
  unparsable value, or a marker reformatting that cannot be proven equivalent).

Multi-valued fields are compared by their own semantics, not as raw text. In
particular, :mailheader:`Requires-Dist` is insensitive to entry ordering and
treats PEP 503 name normalization, PEP 440 specifier normalization, and
canonical marker spelling as equal; differences that cannot be proven
equivalent stay :attr:`ChangeKind.UNKNOWN`.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import typing
from typing import Any, Mapping, Union

from . import licenses, metadata as _metadata
from . import requirements as _requirements
from . import specifiers as _specifiers
from . import utils as _utils
from . import version as _version
from .licenses import InvalidLicenseExpression
from .requirements import InvalidRequirement
from .specifiers import InvalidSpecifier
from .utils import InvalidName
from .version import InvalidVersion

__all__ = [
    "ChangeKind",
    "FieldChange",
    "MetadataComparison",
    "Rule",
    "check_metadata_consistency",
]


def __dir__() -> list[str]:
    return __all__  # pragma: no cover


class ChangeKind(enum.Enum):
    """How a difference between the two metadata documents is classified.

    .. versionadded:: 26.4
    """

    VIOLATION = "violation"
    """The wheel changed a value the sdist promised statically."""

    ALLOWED = "allowed"
    """The difference is permitted by the dynamic rules or expected."""

    UNKNOWN = "unknown"
    """The raw values differ, but semantic equivalence could not be proven."""


@dataclasses.dataclass(frozen=True)
class Rule:
    """The rule that governs a particular :class:`FieldChange`.

    .. versionadded:: 26.4
    """

    id: str
    """A stable, machine-readable identifier for the rule."""

    title: str
    """A short human-readable description of the rule."""


@dataclasses.dataclass(frozen=True)
class FieldChange:
    """A single field difference found between sdist and wheel metadata.

    .. versionadded:: 26.4
    """

    field: str
    """The Core Metadata field name as it appears in the header (e.g.
    ``Requires-Dist``)."""

    kind: ChangeKind
    """Whether the change violates a promise, is allowed, or is uncertain."""

    rule: Rule
    """The rule under which *kind* was decided."""

    old: Any
    """The raw sdist value (``None`` when the field is absent)."""

    new: Any
    """The raw wheel value (``None`` when the field is absent)."""

    message: str
    """A short explanation of the finding."""

    def __str__(self) -> str:
        present = []
        if self.old is not None:
            present.append(f"sdist={self.old!r}")
        if self.new is not None:
            present.append(f"wheel={self.new!r}")
        suffix = f" ({'; '.join(present)})" if present else ""
        return f"{self.kind.value}: {self.field}: {self.message}{suffix}"


@dataclasses.dataclass(frozen=True)
class MetadataComparison:
    """The result of comparing sdist and wheel Core Metadata.

    .. versionadded:: 26.4
    """

    changes: tuple[FieldChange, ...]
    """All field differences, ordered by field name."""

    @property
    def violations(self) -> tuple[FieldChange, ...]:
        """Differences that break a static promise."""
        return tuple(c for c in self.changes if c.kind is ChangeKind.VIOLATION)

    @property
    def allowed(self) -> tuple[FieldChange, ...]:
        """Differences that are permitted."""
        return tuple(c for c in self.changes if c.kind is ChangeKind.ALLOWED)

    @property
    def unknown(self) -> tuple[FieldChange, ...]:
        """Differences whose semantic equivalence could not be proven."""
        return tuple(c for c in self.changes if c.kind is ChangeKind.UNKNOWN)

    @property
    def ok(self) -> bool:
        """True when there are no proven violations.

        :attr:`ChangeKind.UNKNOWN` findings do not make this false: the
        caller decides how conservative to be about unprovable differences.
        """
        return not self.violations


# ---------------------------------------------------------------------------
# Rule catalog
# ---------------------------------------------------------------------------

_RULES: dict[str, Rule] = {}


def _rule(rule_id: str, title: str) -> Rule:
    rule = Rule(rule_id, title)
    _RULES[rule_id] = rule
    return rule


# Special, always-static fields (PEP 643).
R_NAME_STATIC = _rule(
    "pep643-name-static",
    "Name is not permitted to be dynamic (PEP 643).",
)
R_VERSION_STATIC = _rule(
    "pep643-version-static",
    "Version is not permitted to be dynamic (PEP 643).",
)
R_METADATA_VERSION = _rule(
    "metadata-version-advanced",
    "The wheel's Metadata-Version may be newer than the sdist's.",
)

# Dynamic bookkeeping.
R_DYNAMIC_LIST_CHANGED = _rule(
    "pep643-dynamic-field-list",
    "The Dynamic field itself may be changed by the build (PEP 643).",
)
R_DYNAMIC_OLD_METADATA = _rule(
    "pre-2.2-dynamic-field",
    "Dynamic has no meaning for Metadata-Version < 2.2.",
)

# Scalar fields.
R_SCALAR_STATIC = _rule(
    "pep643-scalar-static",
    "A single-valued field not listed in Dynamic must keep its value (PEP 643).",
)
R_SCALAR_DYNAMIC = _rule(
    "pep643-scalar-dynamic",
    "A single-valued field listed in Dynamic may be generated by the build "
    "(PEP 643).",
)
R_REQUIRED_FIELD = _rule(
    "required-field-changed",
    "A required field is missing, empty, or changed between sdist and wheel.",
)

# Multi-valued fields.
R_MULTI_STATIC = _rule(
    "pep643-multi-static",
    "A multi-valued field not listed in Dynamic must keep the same values "
    "(order is not significant; PEP 643).",
)
R_MULTI_DYNAMIC = _rule(
    "pep643-multi-dynamic",
    "A multi-valued field listed in Dynamic may be regenerated by the build "
    "(PEP 643).",
)
R_PEP808_EXTRA_VALUE = _rule(
    "pep808-dynamic-extra-value",
    "Under PEP 808 (Metadata-Version 2.6), a multi-valued field listed in "
    "Dynamic may gain additional values; its static values must be kept.",
)
R_MAPPING_STATIC = _rule(
    "pep643-project-urls-static",
    "Project-URL entries not listed in Dynamic must be kept (PEP 643).",
)
R_MAPPING_DYNAMIC = _rule(
    "pep643-project-urls-dynamic",
    "Project-URL listed in Dynamic may be regenerated by the build (PEP 643).",
)
R_PEP808_MAPPING_EXTRA = _rule(
    "pep808-project-urls-extra",
    "Under PEP 808, Project-URL listed in Dynamic may gain additional entries; "
    "its static entries must be kept.",
)

# Uncertainty.
R_REQUIRES_DIST_UNCERTAIN = _rule(
    "requires-dist-equivalence-unproven",
    "Requires-Dist values differ and semantic equivalence could not be proven "
    "(marker or direct-reference differences).",
)
R_VALUE_UNPARSEABLE = _rule(
    "field-value-unparseable",
    "The field value could not be parsed in one of the documents, so its "
    "semantics could not be compared.",
)
R_UNKNOWN_EXTENSION = _rule(
    "unknown-extension-field",
    "An unrecognized extension field changed; packaging does not know its "
    "semantics or whether it may be dynamic.",
)
R_NO_DYNAMIC_SEMANTICS = _rule(
    "pre-2.2-no-dynamic-semantics",
    "Metadata-Version < 2.2 has no Dynamic concept, so differences cannot be "
    "judged under PEP 643.",
)


# ---------------------------------------------------------------------------
# Internal normalized views of the two documents
# ---------------------------------------------------------------------------


class _Sem(enum.Enum):
    """A three-valued semantic comparison."""

    EQUAL = "equal"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class _View:
    raw: dict[str, Any]
    unparsed: dict[str, list[str]]
    # Lower-case Dynamic *header* names, only when the value parsed cleanly.
    dynamic: frozenset[str] | None
    # The normalized Dynamic values (original casing); empty list if absent.
    dynamic_values: tuple[str, ...]
    metadata_version: str | None


MetadataInput = Union[
    str,
    bytes,
    Mapping[str, Any],
    "_metadata.RawMetadata",
    "_metadata.Metadata",
]
"""A Core Metadata document in any of the forms :func:`parse_email` or
:class:`~packaging.metadata.Metadata` can consume, optionally paired with the
``unparsed`` mapping returned by :func:`~packaging.metadata.parse_email`.

.. versionadded:: 26.4
"""


def _from_metadata(
    obj: _metadata.Metadata,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Read raw values from a ``Metadata`` object without mutating the input.

    A validated/accessed object has already converted and deleted some
    ``_raw`` keys (the enriched values live in ``__dict__``), so both sources
    are consulted.
    """
    clone = copy.deepcopy(obj)
    raw: dict[str, Any] = {}
    unparsed: dict[str, list[str]] = {}
    names = set(_metadata._STRING_FIELDS) | set(_metadata._LIST_FIELDS)
    names |= set(_metadata._DICT_FIELDS)
    names.add("dynamic")
    for raw_name in sorted(names):
        validator = _metadata.Metadata.__dict__.get(raw_name)
        if validator is None:
            continue  # pragma: no cover
        original = clone._raw.get(raw_name)
        enriched_cached = clone.__dict__.get(raw_name)
        if original is None and enriched_cached is None:
            continue  # pragma: no cover
        if original is not None:
            try:
                getattr(clone, raw_name)
            except Exception:
                # A raw value the object cannot convert; report it as an
                # unparsed header like parse_email does.
                header = _metadata._RAW_TO_EMAIL_MAPPING[raw_name]
                if isinstance(original, (list, tuple)):
                    unparsed[header] = [str(v) for v in original]
                else:
                    # Scalar (a mapping already has no raising validator).
                    unparsed[header] = [str(original)]
                continue
            value = original
        else:
            # Only the enriched (validated) form is available; use its stable
            # string representation.
            value = enriched_cached
        if value is None:
            continue  # pragma: no cover
        if isinstance(value, (list, tuple)):
            raw[raw_name] = [str(v) for v in value]
        elif isinstance(value, Mapping):
            raw[raw_name] = {str(k): str(v) for k, v in value.items()}
        else:
            raw[raw_name] = str(value)
    return raw, unparsed


def _build_view(
    data: MetadataInput | tuple[Mapping[str, Any], Mapping[str, list[str]]]
) -> _View:
    unparsed_in: Mapping[str, list[str]] = {}
    if isinstance(data, (bytes, str)):
        raw_mapping, unparsed_in = _metadata.parse_email(data)
        raw: dict[str, Any] = dict(raw_mapping)
    elif isinstance(data, _metadata.Metadata):
        raw, unparsed_in = _from_metadata(data)
    elif isinstance(data, tuple) and len(data) == 2:
        raw = dict(data[0])
        unparsed_in = data[1]
    elif isinstance(data, Mapping):
        raw = dict(data)
    else:
        raise TypeError(
            "metadata must be raw email text/bytes, a RawMetadata mapping, a "
            "Metadata object, or a (RawMetadata, unparsed) tuple"
        )

    # parse_email keys unparsed headers by their lower-case name; normalize
    # any other input source the same way.
    unparsed = {str(k).lower(): list(v) for k, v in unparsed_in.items()}

    # Dynamic: use the raw values, normalized to lower-case header names.
    dynamic_raw = raw.get("dynamic")
    dynamic: frozenset[str] | None = None
    dynamic_values: tuple[str, ...] = ()
    if dynamic_raw not in (None, [], ""):
        if isinstance(dynamic_raw, (list, tuple)):
            values: list[str] = [str(v) for v in dynamic_raw]
        else:
            values = [str(dynamic_raw)]
        values = [v for v in values if v.strip()]
        if values:
            dynamic = frozenset(v.lower() for v in values)
            dynamic_values = tuple(values)
            raw["dynamic"] = values

    mv = raw.get("metadata_version")
    metadata_version = str(mv) if mv is not None else None
    return _View(dict(raw), unparsed, dynamic, dynamic_values, metadata_version)


# ---------------------------------------------------------------------------
# Semantic comparators
# ---------------------------------------------------------------------------


def _name_sem(old: str, new: str) -> _Sem:
    try:
        if _utils.canonicalize_name(
            old, validate=True
        ) == _utils.canonicalize_name(new, validate=True):
            return _Sem.EQUAL
        return _Sem.DIFFERENT
    except InvalidName:
        return _Sem.UNKNOWN


def _version_sem(old: str, new: str) -> _Sem:
    try:
        if _version.parse(old) == _version.parse(new):
            return _Sem.EQUAL
        return _Sem.DIFFERENT
    except InvalidVersion:
        return _Sem.UNKNOWN


def _requires_python_sem(old: str, new: str) -> _Sem:
    try:
        old_set = _specifiers.SpecifierSet(old)
        new_set = _specifiers.SpecifierSet(new)
    except InvalidSpecifier:
        return _Sem.UNKNOWN
    if old_set == new_set:
        return _Sem.EQUAL
    # Canonical spelling differs; only call them equal when the accepted
    # version sets can be proven identical.
    if old_set.to_range() == new_set.to_range():
        return _Sem.EQUAL
    return _Sem.DIFFERENT


def _license_expression_sem(old: str, new: str) -> _Sem:
    try:
        if (
            licenses.canonicalize_license_expression(old)
            == licenses.canonicalize_license_expression(new)
        ):
            return _Sem.EQUAL
        return _Sem.DIFFERENT
    except InvalidLicenseExpression:
        return _Sem.UNKNOWN


_SCALAR_SEMANTICS: dict[str, typing.Callable[[str, str], _Sem]] = {
    "name": _name_sem,
    "version": _version_sem,
    "requires_python": _requires_python_sem,
    "license_expression": _license_expression_sem,
}


def _worst(*sems: _Sem) -> _Sem:
    """Combine verdicts: a proven difference beats uncertainty beats equality."""
    if any(s is _Sem.DIFFERENT for s in sems):
        return _Sem.DIFFERENT
    if any(s is _Sem.UNKNOWN for s in sems):
        return _Sem.UNKNOWN
    return _Sem.EQUAL


# -- Order-insensitive multiset comparison ----------------------------------

_PARSE = typing.TypeVar("_PARSE")


@dataclasses.dataclass(frozen=True)
class _ElemChange:
    old: str | None
    new: str | None
    verdict: _Sem
    reason: str = ""


def _multiset_compare(
    old_items: tuple[str, ...],
    new_items: tuple[str, ...],
    *,
    parse: typing.Callable[[str], _PARSE],
    exact: typing.Callable[[_PARSE, _PARSE], bool],
    key: typing.Callable[[_PARSE], str | None],
    pair: typing.Callable[[_PARSE, _PARSE], _Sem] | None = None,
    raw_of: typing.Callable[[_PARSE], str] | None = None,
) -> tuple[_Sem, tuple[_ElemChange, ...]]:
    """Compare two order-insensitive multisets of values.

    Pairing is done in phases: exact semantic matches first (this absorbs
    reordering and normalization), then, if *pair* is given, unmatched values
    sharing the same *key* are judged by *pair*. Anything left over is a
    genuine addition or removal.
    """
    if raw_of is None:
        raw_of = str

    old_objs = [parse(v) for v in old_items]
    new_objs = [parse(v) for v in new_items]

    if any(key(o) is None for o in old_objs) or any(
        key(n) is None for n in new_objs
    ):
        # An unparsable value on either side cannot be compared.
        return _Sem.UNKNOWN, ()

    changes: list[_ElemChange] = []

    # Phase 1: exact semantic equality.
    new_left = list(new_objs)
    old_left: list[_PARSE] = []
    for o in old_objs:
        for i, n in enumerate(new_left):
            if exact(o, n):
                del new_left[i]
                break
        else:
            old_left.append(o)

    # Phase 2: same key, compared pairwise.
    if pair is not None:
        pending = list(old_left)
        old_left = []
        for o in pending:
            for i, n in enumerate(new_left):
                if key(o) == key(n):
                    changes.append(
                        _ElemChange(raw_of(o), raw_of(n), pair(o, n))
                    )
                    del new_left[i]
                    break
            else:
                old_left.append(o)

    # Phase 3: leftovers are removals and additions.
    for o in old_left:
        changes.append(
            _ElemChange(raw_of(o), None, _Sem.DIFFERENT, "removed")
        )
    for n in new_left:
        changes.append(
            _ElemChange(None, raw_of(n), _Sem.DIFFERENT, "added")
        )

    if not changes:
        return _Sem.EQUAL, ()
    return _worst(*(c.verdict for c in changes)), tuple(changes)


# -- Requires-Dist -----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Req:
    raw: str
    req: _requirements.Requirement | None


def _parse_req(raw: str) -> _Req:
    try:
        return _Req(raw, _requirements.Requirement(raw))
    except InvalidRequirement:
        return _Req(raw, None)


def _req_raw(obj: _Req) -> str:
    return obj.raw


def _req_verdict(
    old_req: _requirements.Requirement, new_req: _requirements.Requirement
) -> _Sem:
    """Compare two parsed requirements sharing a canonical name."""
    old_extras = frozenset(_utils.canonicalize_name(e) for e in old_req.extras)
    new_extras = frozenset(_utils.canonicalize_name(e) for e in new_req.extras)
    if old_extras != new_extras:
        return _Sem.DIFFERENT

    if (old_req.url or None) != (new_req.url or None):
        # Different direct-reference URLs *might* name the same artifact, but
        # that cannot be proven from metadata alone.
        return _Sem.UNKNOWN

    if old_req.specifier == new_req.specifier:
        spec_sem = _Sem.EQUAL
    elif old_req.specifier.to_range() == new_req.specifier.to_range():
        spec_sem = _Sem.EQUAL
    else:
        spec_sem = _Sem.DIFFERENT

    if (old_req.marker is None) != (new_req.marker is None):
        marker_sem = _Sem.DIFFERENT
    elif old_req.marker is None and new_req.marker is None:
        marker_sem = _Sem.EQUAL
    else:
        # The pairwise matcher only reaches here when the full requirements
        # are not equal, so the canonical marker forms genuinely differ.
        # Proving boolean equivalence for arbitrary marker expressions is out
        # of scope; stay uncertain.
        marker_sem = _Sem.UNKNOWN

    return _worst(spec_sem, marker_sem)


def _requirement_pair(old: _Req, new: _Req) -> _Sem:
    assert old.req is not None and new.req is not None
    return _req_verdict(old.req, new.req)


def _requirement_exact(old: _Req, new: _Req) -> bool:
    return old.req is not None and new.req is not None and old.req == new.req


def _requirement_key(obj: _Req) -> str | None:
    return _utils.canonicalize_name(obj.req.name) if obj.req is not None else None


# -- Provides-Extra ----------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Name:
    raw: str
    norm: str | None


def _parse_name(raw: str) -> _Name:
    try:
        return _Name(raw, _utils.canonicalize_name(raw, validate=True))
    except InvalidName:
        return _Name(raw, None)


def _name_exact(old: _Name, new: _Name) -> bool:
    return old.norm is not None and new.norm is not None and old.norm == new.norm


def _name_key(obj: _Name) -> str | None:
    return obj.norm


def _name_raw(obj: _Name) -> str:
    return obj.raw


# ---------------------------------------------------------------------------
# Field classification
# ---------------------------------------------------------------------------


# Multi-valued fields that, under PEP 808 (Metadata-Version 2.6), may carry
# static values while also being listed in Dynamic (only *extra* values may be
# produced by the build). Project-URL is a mapping but follows the same rule.
_PEP808_PARTIAL_FIELDS = frozenset(
    {
        "classifiers",
        "requires_dist",
        "license_files",
        "provides_extra",
        "project_urls",
        "import_names",
        "import_namespaces",
    }
)

# Multi-valued fields with PEP 508 requirement semantics.
_REQUIREMENT_LIST_FIELDS = frozenset(
    {"requires_dist", "provides_dist", "obsoletes_dist"}
)

_VALID_VERSIONS = _metadata._VALID_METADATA_VERSIONS


class _Mode(enum.Enum):
    """How the sdist permits the field to change."""

    STATIC = "static"
    FULL = "full"
    PARTIAL = "partial"
    LEGACY = "legacy"


@dataclasses.dataclass(frozen=True)
class _FieldSpec:
    raw_key: str
    header: str
    category: str  # "scalar", "list", "dict"
    # Comparator kind for non-exact semantics.
    semantics: str = "plain"  # "plain", "name", "version", "requires_python",
    #   "license_expression", "requirements", "extras"

    @property
    def static_rule(self) -> Rule:
        if self.category == "dict":
            return R_MAPPING_STATIC
        if self.category == "list":
            return R_MULTI_STATIC
        if self.raw_key == "name":
            return R_NAME_STATIC
        if self.raw_key == "version":
            return R_VERSION_STATIC
        return R_SCALAR_STATIC

    @property
    def dynamic_rule(self) -> Rule:
        if self.category == "dict":
            return R_MAPPING_DYNAMIC
        if self.category == "list":
            return R_MULTI_DYNAMIC
        return R_SCALAR_DYNAMIC

    @property
    def extra_rule(self) -> Rule:
        return (
            R_PEP808_MAPPING_EXTRA
            if self.category == "dict"
            else R_PEP808_EXTRA_VALUE
        )


def _build_field_specs() -> "dict[str, _FieldSpec]":
    specs: dict[str, _FieldSpec] = {}
    for raw_key in _metadata._STRING_FIELDS:
        header = _metadata._RAW_TO_EMAIL_MAPPING[raw_key]
        semantics = "plain"
        if raw_key == "name":
            semantics = "name"
        elif raw_key == "version":
            semantics = "version"
        elif raw_key == "requires_python":
            semantics = "requires_python"
        elif raw_key == "license_expression":
            semantics = "license_expression"
        specs[raw_key] = _FieldSpec(raw_key, header, "scalar", semantics)
    for raw_key in _metadata._LIST_FIELDS:
        header = _metadata._RAW_TO_EMAIL_MAPPING[raw_key]
        semantics = "requirements" if raw_key in _REQUIREMENT_LIST_FIELDS else "plain"
        if raw_key == "provides_extra":
            semantics = "extras"
        specs[raw_key] = _FieldSpec(raw_key, header, "list", semantics)
    for raw_key in _metadata._DICT_FIELDS:
        header = _metadata._RAW_TO_EMAIL_MAPPING[raw_key]
        specs[raw_key] = _FieldSpec(raw_key, header, "dict", "plain")
    # Keywords is a single Core Metadata header holding a comma-separated
    # list, which parse_email exposes as a list; order is not significant.
    specs["keywords"] = _FieldSpec(
        "keywords", _metadata._RAW_TO_EMAIL_MAPPING["keywords"], "list", "plain"
    )
    return specs


_FIELD_SPECS = _build_field_specs()

# Fields handled outside the generic loop.
_SPECIAL_RAW_KEYS = frozenset({"metadata_version", "dynamic"})
_REQUIRED_RAW_KEYS = frozenset({"name", "version"})


def _metadata_age(view: _View) -> int | None:
    if view.metadata_version is None:
        return None
    try:
        return _VALID_VERSIONS.index(view.metadata_version)
    except ValueError:
        return None


def _field_mode(spec: _FieldSpec, *, sdist: _View, age: int | None) -> _Mode:
    if age is None or age < _VALID_VERSIONS.index("2.2"):
        return _Mode.LEGACY
    is_dynamic = bool(sdist.dynamic and spec.header.lower() in sdist.dynamic)
    if not is_dynamic:
        return _Mode.STATIC
    if (
        age >= _VALID_VERSIONS.index("2.6")
        and spec.raw_key in _PEP808_PARTIAL_FIELDS
    ):
        return _Mode.PARTIAL
    return _Mode.FULL


def _side_status(
    view: _View, raw_key: str, header: str
) -> tuple[str, Any]:
    """Return ("absent" | "raw" | "unparsed", value).

    An explicit empty value is treated as absent, as an empty optional field
    makes no static promise.
    """
    if raw_key in view.raw:
        value = view.raw[raw_key]
        if value is None or value == "" or value == [] or value == {}:
            return ("absent", None)
        return ("raw", value)
    if header.lower() in view.unparsed and view.unparsed[header.lower()] != []:
        return ("unparsed", list(view.unparsed[header.lower()]))
    return ("absent", None)


# ---------------------------------------------------------------------------
# Semantic list comparison dispatch
# ---------------------------------------------------------------------------


def _plain_list_compare(
    old_items: tuple[str, ...], new_items: tuple[str, ...]
) -> tuple[_Sem, tuple[_ElemChange, ...]]:
    return _multiset_compare(
        old_items,
        new_items,
        parse=lambda v: v,
        exact=lambda a, b: a == b,
        key=lambda v: v,
    )


def _requirement_list_compare(
    old_items: tuple[str, ...], new_items: tuple[str, ...]
) -> tuple[_Sem, tuple[_ElemChange, ...]]:
    return _multiset_compare(
        old_items,
        new_items,
        parse=_parse_req,
        exact=_requirement_exact,
        key=_requirement_key,
        pair=_requirement_pair,
        raw_of=_req_raw,
    )


def _extra_list_compare(
    old_items: tuple[str, ...], new_items: tuple[str, ...]
) -> tuple[_Sem, tuple[_ElemChange, ...]]:
    return _multiset_compare(
        old_items,
        new_items,
        parse=_parse_name,
        exact=_name_exact,
        key=_name_key,
        raw_of=_name_raw,
    )


def _compare_list(
    spec: _FieldSpec, old: tuple[str, ...], new: tuple[str, ...]
) -> tuple[_Sem, tuple[_ElemChange, ...]]:
    if spec.semantics == "requirements":
        return _requirement_list_compare(old, new)
    if spec.semantics == "extras":
        return _extra_list_compare(old, new)
    return _plain_list_compare(old, new)


# ---------------------------------------------------------------------------
# Finding helpers
# ---------------------------------------------------------------------------


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _addition_kind(mode: _Mode, spec: _FieldSpec) -> tuple[ChangeKind, Rule]:
    if mode is _Mode.FULL:
        return ChangeKind.ALLOWED, spec.dynamic_rule
    if mode is _Mode.PARTIAL:
        return ChangeKind.ALLOWED, spec.extra_rule
    if mode is _Mode.LEGACY:
        return ChangeKind.UNKNOWN, R_NO_DYNAMIC_SEMANTICS
    return ChangeKind.VIOLATION, spec.static_rule


def _removal_kind(mode: _Mode, spec: _FieldSpec) -> tuple[ChangeKind, Rule]:
    if mode is _Mode.FULL:
        return ChangeKind.ALLOWED, spec.dynamic_rule
    if mode is _Mode.LEGACY:
        return ChangeKind.UNKNOWN, R_NO_DYNAMIC_SEMANTICS
    # STATIC and PARTIAL: dropping a promised value is a violation.
    return ChangeKind.VIOLATION, spec.static_rule


# ---------------------------------------------------------------------------
# Special fields
# ---------------------------------------------------------------------------


def _compare_required(
    spec: _FieldSpec,
    sdist_status: tuple[str, Any],
    wheel_status: tuple[str, Any],
) -> list[FieldChange]:
    _, old = sdist_status
    _, new = wheel_status
    old_present = sdist_status[0] != "absent"
    new_present = wheel_status[0] != "absent"

    changes: list[FieldChange] = []
    if old_present != new_present:
        # A value present but unparsable is still "present" for the strict
        # presence check; its contents are compared below if both sides have
        # something to look at.
        changes.append(
            FieldChange(
                spec.header,
                ChangeKind.VIOLATION,
                R_REQUIRED_FIELD,
                _display(old),
                _display(new),
                f"{spec.header} is required and must not be removed or added "
                "with a different presence",
            )
        )
        return changes
    if not old_present and not new_present:
        return changes
    if sdist_status[0] == "unparsed" or wheel_status[0] == "unparsed":
        # Name/Version are strictly checked, but an unparsable value cannot be
        # proven equal or different.
        if old == new:
            return []  # pragma: no cover
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                _display(old),
                _display(new),
                f"{spec.header} could not be parsed, so the strict check is "
                "inconclusive",
            )
        ]
    return _compare_present_scalar(spec, sdist_status, wheel_status, _Mode.STATIC)


def _display(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _compare_metadata_version(
    sdist: _View, wheel: _View
) -> list[FieldChange]:
    old = sdist.metadata_version
    new = wheel.metadata_version
    old_age = _metadata_age(sdist)
    new_age = _metadata_age(wheel)

    old_in = "metadata-version" in sdist.unparsed or (
        sdist.raw.get("metadata_version") is not None
    )
    new_in = "metadata-version" in wheel.unparsed or (
        wheel.raw.get("metadata_version") is not None
    )

    if not old_in and not new_in:
        return []
    if not old_in or not new_in:
        return [
            FieldChange(
                "Metadata-Version",
                ChangeKind.VIOLATION,
                R_REQUIRED_FIELD,
                old,
                new,
                "Metadata-Version must be present in both documents",
            )
        ]
    if sdist.unparsed.get("metadata-version") or wheel.unparsed.get(
        "metadata-version"
    ):
        return [
            FieldChange(
                "Metadata-Version",
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                old
                if old is not None
                else tuple(sdist.unparsed.get("metadata-version", [])),
                new
                if new is not None
                else tuple(wheel.unparsed.get("metadata-version", [])),
                "Metadata-Version could not be compared",
            )
        ]
    if old == new:
        return []
    if old_age is None or new_age is None:
        return [
            FieldChange(
                "Metadata-Version",
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                old,
                new,
                "An unrecognized Metadata-Version could not be ordered",
            )
        ]
    kind = ChangeKind.ALLOWED if new_age >= old_age else ChangeKind.VIOLATION
    return [
        FieldChange(
            "Metadata-Version",
            kind,
            R_METADATA_VERSION,
            old,
            new,
            "the wheel may emit a newer Metadata-Version"
            if kind is ChangeKind.ALLOWED
            else "the wheel must not downgrade the Metadata-Version",
        )
    ]


def _compare_dynamic(sdist: _View, wheel: _View, age: int | None) -> list[FieldChange]:
    old_unparsed = sdist.unparsed.get("dynamic")
    new_unparsed = wheel.unparsed.get("dynamic")

    # Unparseable on either side.
    if old_unparsed is not None or new_unparsed is not None:
        old_val = (
            tuple(old_unparsed)
            if old_unparsed is not None
            else tuple(sdist.dynamic_values)
        )
        new_val = (
            tuple(new_unparsed)
            if new_unparsed is not None
            else tuple(wheel.dynamic_values)
        )
        if old_val == new_val:
            return []
        if age is None or age < _VALID_VERSIONS.index("2.2"):
            kind = ChangeKind.UNKNOWN
            rule = R_DYNAMIC_OLD_METADATA
            msg = "Dynamic has no meaning for Metadata-Version < 2.2"
        else:
            kind = ChangeKind.ALLOWED
            rule = R_DYNAMIC_LIST_CHANGED
            msg = "the Dynamic field may be rewritten by the build"
        return [FieldChange("Dynamic", kind, rule, old_val, new_val, msg)]

    old_set = frozenset(v.lower() for v in sdist.dynamic_values)
    new_set = frozenset(v.lower() for v in wheel.dynamic_values)
    old_present = bool(sdist.dynamic_values)
    new_present = bool(wheel.dynamic_values)

    if old_set == new_set and old_present == new_present:
        return []
    if not old_present and not new_present:
        return []  # pragma: no cover
    old_val = tuple(sdist.dynamic_values) if old_present else None
    new_val = tuple(wheel.dynamic_values) if new_present else None
    if age is None or age < _VALID_VERSIONS.index("2.2"):
        return [
            FieldChange(
                "Dynamic",
                ChangeKind.UNKNOWN,
                R_DYNAMIC_OLD_METADATA,
                old_val,
                new_val,
                "Dynamic has no meaning for Metadata-Version < 2.2",
            )
        ]
    return [
        FieldChange(
            "Dynamic",
            ChangeKind.ALLOWED,
            R_DYNAMIC_LIST_CHANGED,
            old_val,
            new_val,
            "the build may update the Dynamic field itself; only the sdist's "
            "list grants permission to change other fields",
        )
    ]


# ---------------------------------------------------------------------------
# Generic field comparison
# ---------------------------------------------------------------------------


def _compare_present_scalar(
    spec: _FieldSpec,
    sdist_status: tuple[str, Any],
    wheel_status: tuple[str, Any],
    mode: _Mode,
) -> list[FieldChange]:
    _, old = sdist_status
    _, new = wheel_status
    old_str = str(old)
    new_str = str(new)
    if old_str == new_str:
        return []

    comparator = _SCALAR_SEMANTICS.get(spec.raw_key)
    verdict = comparator(old_str, new_str) if comparator is not None else _Sem.DIFFERENT

    if verdict is _Sem.EQUAL:
        return []
    if mode is _Mode.FULL:
        return [
            FieldChange(
                spec.header,
                ChangeKind.ALLOWED,
                spec.dynamic_rule,
                old_str,
                new_str,
                "dynamic field regenerated by the build",
            )
        ]
    if mode is _Mode.LEGACY:
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_NO_DYNAMIC_SEMANTICS,
                old_str,
                new_str,
                "Metadata-Version < 2.2 has no Dynamic semantics",
            )
        ]
    if verdict is _Sem.UNKNOWN:
        rule = R_VALUE_UNPARSEABLE
        kind = ChangeKind.UNKNOWN
        msg = "values differ and semantic equivalence could not be proven"
    else:
        rule = spec.static_rule
        kind = ChangeKind.VIOLATION
        msg = "static scalar value changed"
    return [FieldChange(spec.header, kind, rule, old_str, new_str, msg)]


def _element_field_changes(
    spec: _FieldSpec,
    mode: _Mode,
    verdict: _Sem,
    elements: tuple[_ElemChange, ...],
    old: tuple[str, ...],
    new: tuple[str, ...],
) -> list[FieldChange]:
    if mode is _Mode.FULL:
        return [
            FieldChange(
                spec.header,
                ChangeKind.ALLOWED,
                spec.dynamic_rule,
                old,
                new,
                "dynamic multi-valued field regenerated by the build",
            )
        ]
    if mode is _Mode.LEGACY:
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_NO_DYNAMIC_SEMANTICS,
                old,
                new,
                "Metadata-Version < 2.2 has no Dynamic semantics",
            )
        ]

    # STATIC or PARTIAL: report per element so original/new values are clear.
    changes: list[FieldChange] = []
    for elem in elements:
        reason = elem.reason or "changed"
        if elem.old is not None and elem.new is not None:
            if mode is _Mode.PARTIAL:
                # A matching key whose value changed is a promised value.
                kind = (
                    ChangeKind.VIOLATION
                    if elem.verdict is _Sem.DIFFERENT
                    else ChangeKind.UNKNOWN
                )
                rule = (
                    spec.static_rule
                    if kind is ChangeKind.VIOLATION
                    else R_REQUIRES_DIST_UNCERTAIN
                    if spec.raw_key == "requires_dist"
                    else R_VALUE_UNPARSEABLE
                )
                msg = (
                    "promised value changed; PEP 808 allows only additional "
                    "values in a dynamic multi-valued field"
                )
            else:
                kind = (
                    ChangeKind.VIOLATION
                    if elem.verdict is _Sem.DIFFERENT
                    else ChangeKind.UNKNOWN
                )
                if kind is ChangeKind.UNKNOWN:
                    rule = (
                        R_REQUIRES_DIST_UNCERTAIN
                        if spec.raw_key == "requires_dist"
                        else R_VALUE_UNPARSEABLE
                    )
                else:
                    rule = spec.static_rule
                msg = f"static {reason} value changed"
            changes.append(
                FieldChange(spec.header, kind, rule, elem.old, elem.new, msg)
            )
        elif elem.new is not None:
            # Addition.
            if mode is _Mode.PARTIAL:
                changes.append(
                    FieldChange(
                        spec.header,
                        ChangeKind.ALLOWED,
                        spec.extra_rule,
                        None,
                        elem.new,
                        "additional dynamic value allowed by PEP 808",
                    )
                )
            else:
                changes.append(
                    FieldChange(
                        spec.header,
                        ChangeKind.VIOLATION,
                        spec.static_rule,
                        None,
                        elem.new,
                        "wheel added a value to a static field",
                    )
                )
        else:
            # Removal of a promised value (STATIC and PARTIAL alike).
            changes.append(
                FieldChange(
                    spec.header,
                    ChangeKind.VIOLATION,
                    spec.static_rule,
                    elem.old,
                    None,
                    "wheel removed a value the sdist promised statically",
                )
            )
    if not changes and verdict is _Sem.UNKNOWN:
        # Unparsable elements with no pairwise details.
        changes.append(
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                old,
                new,
                "an unparsable value prevented comparison",
            )
        )
    return changes


def _compare_present_list(
    spec: _FieldSpec,
    old: tuple[str, ...],
    new: tuple[str, ...],
    mode: _Mode,
) -> list[FieldChange]:
    if old == new:
        return []
    verdict, elements = _compare_list(spec, old, new)
    if verdict is _Sem.EQUAL:
        return []
    return _element_field_changes(spec, mode, verdict, elements, old, new)


def _compare_present_dict(
    spec: _FieldSpec,
    old: Mapping[str, str],
    new: Mapping[str, str],
    mode: _Mode,
) -> list[FieldChange]:
    if dict(old) == dict(new):
        return []
    if mode is _Mode.FULL:
        return [
            FieldChange(
                spec.header,
                ChangeKind.ALLOWED,
                spec.dynamic_rule,
                dict(old),
                dict(new),
                "dynamic mapping regenerated by the build",
            )
        ]
    if mode is _Mode.LEGACY:
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_NO_DYNAMIC_SEMANTICS,
                dict(old),
                dict(new),
                "Metadata-Version < 2.2 has no Dynamic semantics",
            )
        ]

    changes: list[FieldChange] = []
    for key in sorted(frozenset(old) | frozenset(new)):
        if key in old and key in new:
            if old[key] == new[key]:
                continue
            changes.append(
                FieldChange(
                    spec.header,
                    ChangeKind.VIOLATION,
                    spec.static_rule,
                    {key: old[key]},
                    {key: new[key]},
                    "promised mapping entry changed",
                )
            )
        elif key in new:
            if mode is _Mode.PARTIAL:
                changes.append(
                    FieldChange(
                        spec.header,
                        ChangeKind.ALLOWED,
                        spec.extra_rule,
                        None,
                        {key: new[key]},
                        "additional dynamic entry allowed by PEP 808",
                    )
                )
            else:
                changes.append(
                    FieldChange(
                        spec.header,
                        ChangeKind.VIOLATION,
                        spec.static_rule,
                        None,
                        {key: new[key]},
                        "wheel added an entry to a static mapping",
                    )
                )
        else:
            changes.append(
                FieldChange(
                    spec.header,
                    ChangeKind.VIOLATION,
                    spec.static_rule,
                    {key: old[key]},
                    None,
                    "wheel removed an entry the sdist promised statically",
                )
            )
    return changes


def _compare_known_field(
    spec: _FieldSpec, sdist: _View, wheel: _View, age: int | None
) -> list[FieldChange]:
    mode = _field_mode(spec, sdist=sdist, age=age)
    s_status = _side_status(sdist, spec.raw_key, spec.header)
    w_status = _side_status(wheel, spec.raw_key, spec.header)
    s_kind, old = s_status
    w_kind, new = w_status

    # Required fields have stricter presence rules and are always static.
    if spec.raw_key in _REQUIRED_RAW_KEYS:
        return _compare_required(spec, s_status, w_status)

    if s_kind == "absent" and w_kind == "absent":
        return []

    if s_kind == "absent" and w_kind == "unparsed":
        # The wheel added a value the sdist did not promise, and that value
        # cannot even be parsed: report both findings, uncertainty first.
        kind, rule = _addition_kind(mode, spec)
        return [
            FieldChange(
                spec.header,
                kind,
                rule,
                None,
                _display(new),
                "wheel added an unparsable value the sdist did not promise "
                "statically",
            )
        ]

    if s_kind == "unparsed" and w_kind == "absent":
        if mode is _Mode.FULL:
            return [
                FieldChange(
                    spec.header,
                    ChangeKind.ALLOWED,
                    spec.dynamic_rule,
                    _display(old),
                    None,
                    "dynamic field omitted by the build",
                )
            ]
        if mode is _Mode.LEGACY:
            return [
                FieldChange(
                    spec.header,
                    ChangeKind.UNKNOWN,
                    R_NO_DYNAMIC_SEMANTICS,
                    _display(old),
                    None,
                    "Metadata-Version < 2.2 has no Dynamic semantics",
                )
            ]
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                _display(old),
                None,
                "the sdist value could not be parsed, so its removal cannot "
                "be judged",
            )
        ]

    if s_kind == "absent":
        kind, rule = _addition_kind(mode, spec)
        msg = (
            "wheel filled in a dynamic field"
            if kind is ChangeKind.ALLOWED
            else "wheel added a value the sdist did not promise statically"
            if kind is ChangeKind.VIOLATION
            else "addition cannot be judged without Dynamic semantics"
        )
        return [
            FieldChange(
                spec.header, kind, rule, None, _display(new), msg
            )
        ]
    if w_kind == "absent":
        kind, rule = _removal_kind(mode, spec)
        msg = (
            "dynamic field omitted by the build"
            if kind is ChangeKind.ALLOWED
            else "wheel removed a value the sdist promised statically"
            if kind is ChangeKind.VIOLATION
            else "removal cannot be judged without Dynamic semantics"
        )
        return [
            FieldChange(
                spec.header, kind, rule, _display(old), None, msg
            )
        ]

    # Both present.
    if s_kind == "unparsed" or w_kind == "unparsed":
        if old == new:
            return []
        if mode is _Mode.FULL:
            return [
                FieldChange(
                    spec.header,
                    ChangeKind.ALLOWED,
                    spec.dynamic_rule,
                    _display(old),
                    _display(new),
                    "dynamic field changed",
                )
            ]
        if mode is _Mode.LEGACY:
            return [
                FieldChange(
                    spec.header,
                    ChangeKind.UNKNOWN,
                    R_NO_DYNAMIC_SEMANTICS,
                    _display(old),
                    _display(new),
                    "Metadata-Version < 2.2 has no Dynamic semantics",
                )
            ]
        return [
            FieldChange(
                spec.header,
                ChangeKind.UNKNOWN,
                R_VALUE_UNPARSEABLE,
                _display(old),
                _display(new),
                "the field value could not be parsed in one of the documents",
            )
        ]

    if spec.category == "scalar":
        return _compare_present_scalar(spec, s_status, w_status, mode)
    if spec.category == "dict":
        assert isinstance(old, Mapping) and isinstance(new, Mapping)
        return _compare_present_dict(spec, old, new, mode)
    return _compare_present_list(spec, _as_tuple(old), _as_tuple(new), mode)


def _all_header_keys(sdist: _View, wheel: _View) -> tuple[str, ...]:
    keys = set(sdist.unparsed) | set(wheel.unparsed)
    keys = {
        header
        for header in keys
        if _metadata._EMAIL_TO_RAW_MAPPING.get(header) is None
    }
    return tuple(sorted(keys))


def _compare_unknown_header(
    header: str, sdist: _View, wheel: _View
) -> list[FieldChange]:
    if _metadata._EMAIL_TO_RAW_MAPPING.get(header) is not None:
        return []  # pragma: no cover
    old = tuple(sdist.unparsed.get(header, ()))
    new = tuple(wheel.unparsed.get(header, ()))
    old_present = bool(old)
    new_present = bool(new)
    if old_present == new_present and old == new:
        return []
    # Unknown extensions carry no declared semantics or dynamic permission.
    return [
        FieldChange(
            header,
            ChangeKind.UNKNOWN,
            R_UNKNOWN_EXTENSION,
            old if old_present else None,
            new if new_present else None,
            "unrecognized extension field; unknown whether the change is "
            "allowed or semantically equivalent",
        )
    ]


def _sort_key(change: FieldChange) -> tuple[str, str, str]:
    return (
        change.field.lower(),
        str(change.old),
        str(change.new),
    )


def check_metadata_consistency(
    sdist: MetadataInput | tuple[Mapping[str, Any], Mapping[str, list[str]]],
    wheel: MetadataInput | tuple[Mapping[str, Any], Mapping[str, list[str]]],
) -> MetadataComparison:
    """Check wheel Core Metadata against an sdist's static metadata promises.

    *sdist* is the source distribution metadata (the promise); *wheel* is the
    built distribution metadata (the result). Each may be raw email text or
    bytes, a :class:`~packaging.metadata.RawMetadata` mapping, a
    :class:`~packaging.metadata.Metadata` object, or a
    ``(RawMetadata, unparsed)`` tuple as returned by
    :func:`~packaging.metadata.parse_email`.

    Returns a :class:`MetadataComparison`. No build backend is run and the
    inputs are left unmodified.

    .. versionadded:: 26.4
    """
    sdist_view = _build_view(sdist)
    wheel_view = _build_view(wheel)
    age = _metadata_age(sdist_view)

    changes: list[FieldChange] = []
    changes.extend(_compare_metadata_version(sdist_view, wheel_view))
    changes.extend(_compare_dynamic(sdist_view, wheel_view, age))

    for raw_key, spec in _FIELD_SPECS.items():
        if raw_key in _SPECIAL_RAW_KEYS:
            continue
        changes.extend(_compare_known_field(spec, sdist_view, wheel_view, age))

    for header in _all_header_keys(sdist_view, wheel_view):
        changes.extend(_compare_unknown_header(header, sdist_view, wheel_view))

    changes.sort(key=_sort_key)
    return MetadataComparison(tuple(changes))
