"""Semantic comparison primitives for individual Core Metadata fields."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from packaging import specifiers as spec_module
from packaging import version as version_module
from packaging._parser import (
    MarkerAtom,
    MarkerList,
    Op,
    Value,
    Variable,
    parse_marker,
)
from packaging._tokenizer import ParserSyntaxError
from packaging.licenses import (
    InvalidLicenseExpression,
    canonicalize_license_expression,
)
from packaging.markers import InvalidMarker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidName, canonicalize_name

from .rules import EMAIL_FIELDS

EQUAL = "equal"
DIFFERENT = "different"
UNCERTAIN = "uncertain"

_MISSING = object()


def email_name(field: str) -> str:
    """Return the email header spelling of a canonical underscore field."""

    for email_header, raw_field in EMAIL_FIELDS.items():
        if raw_field == field:
            return email_header
    return field.replace("_", "-")


def _iter_values(value: Any) -> list[Any]:
    if value is None or value is _MISSING:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.items())
    return [value]


def _multiset(value: Any) -> Counter[str]:
    return Counter(str(item) for item in _iter_values(value))


def compare_multiset(old: Any, new: Any) -> str:
    return EQUAL if _multiset(old) == _multiset(new) else DIFFERENT


def compare_classifiers(old: Any, new: Any) -> str:
    # Classifier list order is not meaningful; duplicate classifiers are not a
    # meaningful distinction either.
    return EQUAL if set(_multiset(old)) == set(_multiset(new)) else DIFFERENT


def compare_keywords(old: Any, new: Any) -> str:
    def tokens(values: Any) -> Counter[str]:
        result: Counter[str] = Counter()
        for header in _iter_values(values):
            for part in str(header).split(","):
                token = part.strip()
                if token:
                    result[token] += 1
        return result

    return EQUAL if tokens(old) == tokens(new) else DIFFERENT


def compare_name(old: Any, new: Any) -> tuple[str, str | None]:
    if old is _MISSING and new is _MISSING:
        return EQUAL, None
    if old is _MISSING or new is _MISSING:
        return DIFFERENT, None
    try:
        old_norm = canonicalize_name(str(old), validate=True)
        new_norm = canonicalize_name(str(new), validate=True)
    except InvalidName:
        return (
            (EQUAL, None)
            if str(old) == str(new)
            else (UNCERTAIN, "invalid distribution name; normalization unavailable")
        )
    return (
        (EQUAL, None)
        if old_norm == new_norm
        else (DIFFERENT, "different normalized distribution name")
    )


def compare_version(old: Any, new: Any) -> tuple[str, str | None]:
    old_present = old is not _MISSING and str(old) != ""
    new_present = new is not _MISSING and str(new) != ""
    if not old_present and not new_present:
        return EQUAL, None
    if not old_present or not new_present:
        return DIFFERENT, None
    try:
        old_version = version_module.parse(str(old))
        new_version = version_module.parse(str(new))
    except version_module.InvalidVersion:
        return (
            (EQUAL, None)
            if str(old) == str(new)
            else (UNCERTAIN, "invalid PEP 440 version; normalization unavailable")
        )
    return (
        (EQUAL, None)
        if old_version == new_version
        else (DIFFERENT, f"{old_version} != {new_version}")
    )


def compare_requires_python(old: Any, new: Any) -> tuple[str, str | None]:
    old_present = old is not _MISSING and str(old).strip()
    new_present = new is not _MISSING and str(new).strip()
    if not old_present and not new_present:
        return EQUAL, None
    if not old_present or not new_present:
        return DIFFERENT, None
    try:
        old_spec = spec_module.SpecifierSet(str(old))
        new_spec = spec_module.SpecifierSet(str(new))
    except spec_module.InvalidSpecifier:
        return (
            (EQUAL, None)
            if str(old) == str(new)
            else (UNCERTAIN, "invalid Requires-Python; normalization unavailable")
        )
    return (
        (EQUAL, None)
        if old_spec == new_spec
        else (DIFFERENT, f"{str(old_spec).strip()!r} != {str(new_spec).strip()!r}")
    )


def compare_license_expression(old: Any, new: Any) -> tuple[str, str | None]:
    old_present = old is not _MISSING and str(old).strip()
    new_present = new is not _MISSING and str(new).strip()
    if not old_present and not new_present:
        return EQUAL, None
    if not old_present or not new_present:
        return DIFFERENT, None
    try:
        old_norm = canonicalize_license_expression(str(old))
        new_norm = canonicalize_license_expression(str(new))
    except InvalidLicenseExpression:
        return (
            (EQUAL, None)
            if str(old) == str(new)
            else (UNCERTAIN, "invalid License-Expression; normalization unavailable")
        )
    return (EQUAL, None) if old_norm == new_norm else (DIFFERENT, None)


def compare_provides_extra(old: Any, new: Any) -> str:
    def normalized(values: Any) -> set[str]:
        result: set[str] = set()
        for item in _iter_values(values):
            try:
                result.add(canonicalize_name(str(item), validate=True))
            except InvalidName:
                result.add(f"<invalid:{item}>")
        return result

    return EQUAL if normalized(old) == normalized(new) else DIFFERENT


def compare_project_urls(old: Any, new: Any, old_raw: Any, new_raw: Any) -> tuple[str, str | None]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        result = compare_multiset(old_raw, new_raw)
        if result == DIFFERENT:
            return DIFFERENT, "Project-URL entries could not be fully parsed"
        return EQUAL, None
    old_labels = set(old)
    new_labels = set(new)
    if old_labels != new_labels:
        if {label.lower() for label in old_labels} == {
            label.lower() for label in new_labels
        }:
            return UNCERTAIN, "Project-URL labels differ only in case"
        return DIFFERENT, "Project-URL labels differ"
    for label in old_labels:
        if str(old[label]).strip() != str(new[label]).strip():
            return DIFFERENT, f"URL for {label!r} differs"
    return EQUAL, None


def compare_scalar(old: Any, new: Any) -> str:
    def normalize(value: Any) -> str | None:
        if value is _MISSING or value is None:
            return None
        text = str(value).strip("\r\n")
        return text or None

    return EQUAL if normalize(old) == normalize(new) else DIFFERENT


# --- Marker normalization ---------------------------------------------------

_EXTRA_VARIABLES = frozenset({"extra"})
_SET_VARIABLES = frozenset({"extras", "dependency_groups"})


@dataclass(frozen=True)
class _Atom:
    text: str


@dataclass(frozen=True)
class _Group:
    operator: str
    children: tuple["_Node", ...]


_Node = _Atom | _Group


def _atom_serialize(lhs: Node, operator: Op, rhs: Node) -> str:
    canonical_lhs, canonical_rhs = lhs, rhs
    if operator.value in {"==", "!="}:
        # Equality is commutative; do not punish operand ordering.
        if lhs.serialize() > rhs.serialize():
            canonical_lhs, canonical_rhs = rhs, lhs
    return (
        f"{canonical_lhs.serialize()} {operator.serialize()} "
        f"{canonical_rhs.serialize()}"
    )


def _normalize_atom(atom: MarkerAtom) -> _Atom:
    lhs, operator, rhs = atom
    assert isinstance(operator, Op)

    def canonical_literal(variable: Variable, literal: Value) -> Value:
        if isinstance(variable, Variable) and isinstance(literal, Value) and (
            variable.value in _EXTRA_VARIABLES
            or (
                variable.value in _SET_VARIABLES
                and operator.value in {"in", "not in"}
            )
        ):
            return Value(canonicalize_name(literal.value, validate=False))
        return literal

    if isinstance(lhs, Variable):
        rhs = canonical_literal(lhs, rhs)
    elif isinstance(rhs, Variable):
        lhs = canonical_literal(rhs, lhs)
    return _Atom(_atom_serialize(lhs, operator, rhs))


def _build_node(expression: MarkerList) -> _Node:
    if isinstance(expression, tuple):
        return _normalize_atom(expression)

    nodes: list[_Node] = []
    operators: list[str] = []
    for part in expression:
        if isinstance(part, str):
            operators.append(part)
        else:
            nodes.append(_build_node(part))
    if len(nodes) == 1:
        return nodes[0]
    # The parser guarantees alternating atoms and one boolean operator per
    # parenthesized marker level.
    return _Group(operators[0] if operators else "and", tuple(nodes))


def _flatten(node: _Node) -> _Node:
    if isinstance(node, _Atom):
        return node
    children: list[_Node] = []
    for child in node.children:
        flat = _flatten(child)
        if isinstance(flat, _Group) and flat.operator == node.operator:
            children.extend(flat.children)
        else:
            children.append(flat)
    return _Group(node.operator, tuple(children))


def _serialize(node: _Node, *, parent_operator: str | None = None) -> str:
    if isinstance(node, _Atom):
        return node.text
    inner = f" {node.operator} ".join(
        _serialize(child, parent_operator=node.operator) for child in node.children
    )
    if parent_operator is not None and (
        node.operator == "or"
        or (parent_operator == "or" and node.operator == "and")
    ):
        return f"({inner})"
    return inner


def _canonical(node: _Node) -> str:
    def visit(target: _Node) -> str:
        if isinstance(target, _Atom):
            return target.text
        parts = sorted(visit(child) for child in target.children)
        return target.operator + ":" + "|".join(parts)

    return visit(node)


def _canonical_from_text(raw: str) -> tuple[_Node | None, str | None]:
    try:
        parsed = parse_marker(raw)
    except (InvalidMarker, ParserSyntaxError, ValueError, TypeError) as exc:
        return None, f"unparsable marker: {exc}"
    return _flatten(_build_node(parsed)), None


def canonical_marker(raw: str | None) -> tuple[str | None, str | None]:
    """Return a stable semantic key for a marker, plus an uncertainty reason."""

    if raw is None or not raw.strip():
        return "", None
    original, error = _canonical_from_text(raw)
    if original is None:
        return None, error

    # Round-trip our spelling and require the same semantic key. This checks
    # serialization soundness without punishing the harmless operand/clause
    # reordering that the semantic key intentionally ignores.
    try:
        rebuilt_text = _serialize(original)
        rebuilt, error = _canonical_from_text(rebuilt_text)
    except (InvalidMarker, ParserSyntaxError, ValueError) as exc:
        return None, f"marker could not be re-serialized: {exc}"
    if rebuilt is None:
        return None, error
    if _canonical(original) != _canonical(rebuilt):
        return None, "marker parses but serialization is not provably equivalent"
    return _canonical(original), None


# --- Requires-Dist multiset comparison --------------------------------------


@dataclass(frozen=True)
class _Requirement:
    name: str
    extras: frozenset[str]
    url: str | None
    specifiers: str
    marker: str
    raw: str
    uncertain: str | None

    def key(self, *, strict: bool) -> tuple[Any, ...]:
        if self.uncertain is not None and self.name == "<unparsable>":
            # No structure can be recovered; identical raw text is the only
            # provable equality. Different unparsable lines stay uncertain.
            return ("__raw__", self.raw)
        marker = self.marker if strict else (self.marker if self.uncertain is None else None)
        return (
            self.name,
            self.extras,
            self.url,
            self.specifiers,
            marker,
            self.uncertain if strict else None,
        )


def _parse_requirement(raw_line: str) -> _Requirement:
    try:
        requirement = Requirement(raw_line)
    except (InvalidRequirement, ParserSyntaxError) as exc:
        return _Requirement(
            name="<unparsable>",
            extras=frozenset(),
            url=None,
            specifiers="",
            marker="",
            raw=raw_line.strip(),
            uncertain=f"unparsable requirement: {exc}",
        )

    marker_text = str(requirement.marker) if requirement.marker is not None else ""
    marker_key, marker_reason = canonical_marker(marker_text or None)
    return _Requirement(
        name=canonicalize_name(requirement.name, validate=False),
        extras=frozenset(
            canonicalize_name(extra, validate=False) for extra in requirement.extras
        ),
        url=requirement.url,
        specifiers=str(requirement.specifier),
        marker=marker_key if marker_key is not None else f"<uncertain:{marker_text}>",
        raw=raw_line.strip(),
        uncertain=marker_reason,
    )


def compare_requires_dist(old: Any, new: Any) -> tuple[str, list[str]]:
    old_requirements = [_parse_requirement(line) for line in _iter_values(old)]
    new_requirements = [_parse_requirement(line) for line in _iter_values(new)]

    unparsable_old = Counter(req.raw for req in old_requirements if req.uncertain and req.name == "<unparsable>")
    unparsable_new = Counter(req.raw for req in new_requirements if req.uncertain and req.name == "<unparsable>")
    if unparsable_old != unparsable_new:
        return UNCERTAIN, ["Requires-Dist contains an unparsable requirement"]

    old_known = [req for req in old_requirements if not (req.uncertain and req.name == "<unparsable>")]
    new_known = [req for req in new_requirements if not (req.uncertain and req.name == "<unparsable>")]

    # List order is not semantic; compare multiset signatures.
    old_strict = Counter(req.key(strict=True) for req in old_known)
    new_strict = Counter(req.key(strict=True) for req in new_known)
    if old_strict == new_strict:
        return EQUAL, []

    # Ignore only markers that parsed but could not be proven equivalent.
    old_loose = Counter(req.key(strict=False) for req in old_known)
    new_loose = Counter(req.key(strict=False) for req in new_known)
    if old_loose == new_loose:
        return UNCERTAIN, ["Requires-Dist marker equivalence could not be proven"]

    reasons = sorted({
        reason
        for requirement in [*old_known, *new_known]
        if requirement.uncertain is not None
        for reason in [requirement.uncertain]
    })
    return DIFFERENT, reasons
