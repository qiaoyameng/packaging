"""Core merging logic.

The merger never decides which concrete version of a project is installed.
It only states, per target and normalized distribution name, whether the
already-asserted requirements are provably compatible, provably conflicting,
or undecidable from the information given.

Verdicts
--------

``satisfied``
    Every active requirement is mutually compatible. ``merged`` describes
    the combined constraint.
``conflict``
    Compatibility is *proven* impossible. ``minimal_conflict`` is an
    inclusion-minimal subset: deleting any one of its members destroys the
    proof.
``unknown``
    Neither side can be proven (e.g. ``===`` arbitrary equality, a direct URL
    mixed with index constraints, or a marker that cannot be evaluated).
    Every original requirement is retained verbatim.

Direct URLs
-----------

A requirement pinned with ``name @ url`` selects a concrete, index-external
source. Two *different* URLs are always a conflict; the *same* repeated URL
merges like an ordinary duplicate. A direct URL next to a plain version
specifier cannot be judged from static metadata alone, so it stays
``unknown``.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from packaging.markers import (
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
    default_environment,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

Verdict = Literal["satisfied", "conflict", "unknown"]

# Marker evaluation can fail on missing environment variables or on
# comparisons that are invalid for the involved values (e.g. comparing
# python_version against a malformed operand).
_MARKER_ERRORS = (UndefinedComparison, UndefinedEnvironmentName)


@dataclass(frozen=True)
class ParsedRequirement:
    """A single ``Requires-Dist`` line bound to its plugin origin."""

    raw: str
    origin: str
    requirement: Requirement

    @property
    def key(self) -> str:
        return canonicalize_name(self.requirement.name)


@dataclass(frozen=True)
class ActiveRequirement:
    """A requirement that survived marker evaluation for one target."""

    parsed: ParsedRequirement
    marker_error: str | None = None


@dataclass(frozen=True)
class MarkerOutcome:
    """Result of evaluating one marker against one target."""

    active: bool | None
    reason: str | None = None


def _derived_python_version(python_full_version: str) -> str:
    """Return the ``python_version`` implied by a full version.

    ``python_version`` is the major.minor portion of the release segment
    (``3.10.4`` -> ``3.10``; ``3.10`` -> ``3.10``).
    """
    parts = python_full_version.split("+", 1)[0].split(".")
    return ".".join(parts[:2])


def build_environment(target: dict[str, object]) -> dict[str, str]:
    """Build the PEP 508 marker environment for one target descriptor.

    The running interpreter supplies defaults; keys present on the target win.
    When only one of ``python_version``/``python_full_version`` is supplied the
    other is derived so every standard marker variable is defined.
    """
    overrides = target.get("environment", {})
    if not isinstance(overrides, dict):
        msg = "target 'environment' must be an object"
        raise TypeError(msg)

    environment: dict[str, str] = {
        key: str(value) for key, value in default_environment().items()
    }
    for key, value in overrides.items():
        # An explicit null removes the variable from the environment, which
        # makes markers referencing it undecidable instead of failing loudly.
        if value is None:
            environment.pop(str(key), None)
        else:
            environment[str(key)] = str(value)

    if (
        "python_full_version" in environment
        and overrides.get("python_version") is None
    ):
        environment["python_version"] = _derived_python_version(
            environment["python_full_version"]
        )
    if overrides.get("python_version") is not None and overrides.get(
        "python_full_version"
    ) is None:
        environment["python_full_version"] = str(overrides["python_version"])
    return environment


def selected_extras(target: dict[str, object]) -> frozenset[str]:
    """Return the normalized set of project extras selected by a target."""
    raw_extras = target.get("extras", ())
    if isinstance(raw_extras, str):
        msg = "target 'extras' must be a list, not a string"
        raise TypeError(msg)
    return frozenset(canonicalize_name(str(extra)) for extra in raw_extras)


def resolve_marker(
    marker: Marker | None,
    environment: dict[str, str],
    extras: frozenset[str],
) -> MarkerOutcome:
    """Evaluate a marker in the *metadata* context.

    Core metadata evaluates a requirement once per requested extra (with
    ``extra`` bound to that extra) and once with no extra selected. The
    full selected set is also exposed as ``extras`` for set-form markers.
    The requirement is active if any evaluation is true. ``None`` means the
    requirement applies unconditionally.
    """
    if marker is None:
        return MarkerOutcome(True)
    extra_bindings = ["", *sorted(extras)]
    try:
        for extra in extra_bindings:
            if marker.evaluate(
                {**environment, "extra": extra, "extras": extras},
                context="metadata",
            ):
                return MarkerOutcome(True)
    except _MARKER_ERRORS as exc:
        return MarkerOutcome(None, f"marker-undecidable: {exc}")
    return MarkerOutcome(False)


def parse_plugins(
    plugins: list[dict[str, object]],
) -> tuple[list[ParsedRequirement], list[dict[str, str]]]:
    """Parse every ``Requires-Dist`` line of every plugin.

    Malformed lines never abort the run; they are returned separately with the
    plugin they came from.
    """
    parsed: list[ParsedRequirement] = []
    malformed: list[dict[str, str]] = []
    for plugin in plugins:
        name = str(plugin.get("name", "<unnamed-plugin>"))
        requires = plugin.get("requires_dist", [])
        if not isinstance(requires, list):
            msg = f"plugin {name!r}: 'requires_dist' must be a list"
            raise TypeError(msg)
        for line in requires:
            text = str(line)
            try:
                requirement = Requirement(text)
            except InvalidRequirement as exc:
                malformed.append({"origin": name, "raw": text, "error": str(exc)})
                continue
            parsed.append(ParsedRequirement(text, name, requirement))
    return parsed, malformed


def _combine_specifiers(
    entries: list[ActiveRequirement],
) -> tuple[SpecifierSet | None, str | None]:
    """Intersect specifiers using SpecifierSet's own algebra.

    Returns the combined set and an unknown reason. A reason is returned when
    any requirement uses ``===`` (arbitrary, literal-string equality outside
    PEP 440 version reasoning), when a specifier is unparseable, or when the
    sets disagree on prerelease handling.
    """
    combined: SpecifierSet | None = None
    for entry in entries:
        specifier = entry.parsed.requirement.specifier
        if str(specifier) == "":
            continue
        if any(spec.operator == "===" for spec in specifier):
            return None, "arbitrary-equality (===) is outside PEP 440 ranges"
        try:
            if combined is None:
                combined = SpecifierSet(str(specifier))
            else:
                combined = combined & SpecifierSet(str(specifier))
        except (InvalidSpecifier, ValueError) as exc:
            return None, f"specifiers-not-combinable: {exc}"
    return combined, None


def _proves_conflict(
    indices: frozenset[int], entries: list[ActiveRequirement]
) -> bool:
    """Whether the named subset alone proves incompatibility.

    Two distinct direct URLs always prove a conflict; a non-arbitrary
    SpecifierSet that is unsatisfiable proves a conflict.
    """
    subset = [entries[index] for index in indices]
    urls = {
        entry.parsed.requirement.url
        for entry in subset
        if entry.parsed.requirement.url is not None
    }
    if len(urls) >= 2:
        return True
    spec_entries = [
        entry for entry in subset if str(entry.parsed.requirement.specifier)
    ]
    if not spec_entries:
        return False
    if any(
        spec.operator == "==="
        for entry in spec_entries
        for spec in entry.parsed.requirement.specifier
    ):
        return False
    combined: SpecifierSet | None = None
    try:
        for entry in spec_entries:
            current = SpecifierSet(str(entry.parsed.requirement.specifier))
            combined = current if combined is None else combined & current
    except (InvalidSpecifier, ValueError):
        return False
    return bool(combined) and combined.is_unsatisfiable()


def _minimal_conflict(
    entries: list[ActiveRequirement],
) -> list[ActiveRequirement]:
    """Greedily shrink the active set to an inclusion-minimal conflict proof.

    Entries are removed one at a time in input order; an entry is kept only if
    its removal would make the remaining set unprovable. The result has the
    property that deleting any member breaks the proof.
    """
    surviving = {index for index, _ in enumerate(entries)}
    for index, _ in enumerate(entries):
        trial = surviving - {index}
        if _proves_conflict(frozenset(trial), entries):
            surviving = trial
    return [entries[index] for index in sorted(surviving)]


def _unknown_reason(entries: list[ActiveRequirement]) -> str | None:
    """Classify why a non-conflicting group still cannot be judged safe."""
    marker_errors = {
        entry.marker_error for entry in entries if entry.marker_error is not None
    }
    if marker_errors:
        return "; ".join(sorted(marker_errors))
    urls = {
        entry.parsed.requirement.url
        for entry in entries
        if entry.parsed.requirement.url is not None
    }
    has_spec = any(str(entry.parsed.requirement.specifier) for entry in entries)
    if urls and has_spec:
        return (
            "direct URL is mixed with an index version constraint; "
            "static metadata cannot prove compatibility"
        )
    if any(
        spec.operator == "==="
        for entry in entries
        for spec in entry.parsed.requirement.specifier
    ):
        return "arbitrary-equality (===) is outside PEP 440 ranges"
    return None


def _origin_record(entry: ActiveRequirement) -> dict[str, str]:
    return {"origin": entry.parsed.origin, "raw": entry.parsed.raw}


def _merged_view(entries: list[ActiveRequirement]) -> dict[str, object]:
    """Build the display view of a group of compatible requirements."""
    extras = {
        canonicalize_name(extra)
        for entry in entries
        for extra in entry.parsed.requirement.extras
    }
    urls = {
        entry.parsed.requirement.url
        for entry in entries
        if entry.parsed.requirement.url is not None
    }
    combined, _ = _combine_specifiers(entries)
    return {
        "specifier": str(combined) if combined is not None else "",
        "url": next(iter(urls)) if len(urls) == 1 else None,
        "extras": sorted(extras),
    }


def _resolve_group(entries: list[ActiveRequirement]) -> dict[str, object]:
    """Resolve one normalized distribution name for one target."""
    if _proves_conflict(frozenset(range(len(entries))), entries):
        proof = _minimal_conflict(entries)
        urls = {
            entry.parsed.requirement.url
            for entry in proof
            if entry.parsed.requirement.url is not None
        }
        reason = (
            "distinct direct URLs select different sources"
            if len(urls) >= 2
            else "intersected SpecifierSet is unsatisfiable"
        )
        return {
            "verdict": "conflict",
            "reason": reason,
            "minimal_conflict": [_origin_record(entry) for entry in proof],
            "requirements": [_origin_record(entry) for entry in entries],
        }

    reason = _unknown_reason(entries)
    if reason is not None:
        return {
            "verdict": "unknown",
            "reason": reason,
            "requirements": [_origin_record(entry) for entry in entries],
        }

    return {
        "verdict": "satisfied",
        "merged": _merged_view(entries),
        "requirements": [_origin_record(entry) for entry in entries],
    }


def _resolve_target(
    target_id: str,
    target: dict[str, object],
    parsed: list[ParsedRequirement],
) -> dict[str, object]:
    """Evaluate every requirement against one target and resolve groups."""
    environment = build_environment(target)
    extras = selected_extras(target)

    groups: dict[str, list[ActiveRequirement]] = {}
    order: list[str] = []

    for entry in parsed:
        outcome = resolve_marker(entry.requirement.marker, environment, extras)
        if outcome.active is None:
            active = ActiveRequirement(entry, outcome.reason)
        elif outcome.active:
            active = ActiveRequirement(entry)
        else:
            continue
        if entry.key not in groups:
            groups[entry.key] = []
            order.append(entry.key)
        groups[entry.key].append(active)

    packages: dict[str, object] = OrderedDict(
        (key, _resolve_group(groups[key])) for key in sorted(order)
    )
    return {"target": target_id, "packages": packages}


def _signature(result: dict[str, object]) -> str:
    """Stable signature of everything that distinguishes one target's result."""
    return json.dumps(result["packages"], sort_keys=True, separators=(",", ":"))


def merge(
    plugins: list[dict[str, object]],
    targets: list[dict[str, object]],
) -> dict[str, object]:
    """Merge plugin requirements across a finite target matrix.

    :param plugins: ``[{"name": str, "requires_dist": [PEP 508 strings]}]``.
    :param targets: ``[{"name": str, "extras": [str],
        "environment": {PEP 508 marker var: value}}]``.
    :returns: A JSON-serializable report. Targets with identical results are
        grouped together under ``groups``; the ungrouped per-target view is
        available under ``targets``.
    """
    parsed, malformed = parse_plugins(plugins)

    target_results: list[dict[str, object]] = [
        _resolve_target(str(target.get("name", "<unnamed-target>")), target, parsed)
        for target in targets
    ]

    grouped: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
    for result in target_results:
        grouped.setdefault(_signature(result), []).append(result)

    groups = [
        {
            "targets": [item["target"] for item in members],
            "packages": members[0]["packages"],
        }
        for members in grouped.values()
    ]

    return {
        "groups": groups,
        "targets": target_results,
        "malformed_requirements": malformed,
    }
