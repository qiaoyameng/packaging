# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Merge ``Requires-Dist`` requirements from multiple sources per target.

Given several plugins that each contribute a set of ``Requires-Dist``
requirement strings, and a finite matrix of target environments, compute the
final constraint each target imposes on every package.

The merger:

* groups requirements by normalized (canonical) package name,
* evaluates each requirement's marker against every target environment and
  every selected extra, so a constraint only affects the targets where its
  marker actually holds (targets never pollute each other),
* intersects the version specifiers of the surviving requirements and takes
  the union of their extras,
* reuses :class:`~packaging.specifiers.SpecifierSet` range relations
  (:meth:`~packaging.specifiers.SpecifierSet.is_subset`) and the
  :meth:`~packaging.specifiers.SpecifierSet.is_unsatisfiable` check instead of
  reimplementing any PEP 440 logic,
* keeps the original constraints and reports an *unknown* conclusion for
  ``===`` (arbitrary equality) specifiers or any other case where a canonical
  simplified form cannot be proven,
* reports conflicts as minimal sets of the original requirements (with their
  sources): removing any single entry from a reported conflict makes the
  conflict unprovable,
* groups targets whose merged results are identical.

Direct URL requirements follow explicit compatibility rules:

* requirements pointing at the *same* URL string are the same source and
  merge normally,
* requirements pointing at *different* URLs are never assumed to be the same
  source and are reported as a conflict,
* a direct URL combined with version specifiers (from any source) cannot be
  verified without fetching the URL, so both constraints are kept and the
  package is reported with an *unknown* status.

The merger works purely at the constraint level: it never resolves actual
package versions and never contacts a package index.

.. versionadded:: 26.4
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .requirements import Requirement
from .specifiers import SpecifierSet
from .utils import NormalizedName, canonicalize_name

__all__ = [
    "Conflict",
    "MergedPackage",
    "MergeReport",
    "SourcedRequirement",
    "Target",
    "TargetGroup",
    "format_report",
    "merge_requirements",
]


def __dir__() -> list[str]:
    return __all__


@dataclass(frozen=True)
class Target:
    """One entry of the target environment matrix.

    ``environment`` is a marker environment as accepted by
    :meth:`packaging.markers.Marker.evaluate`; missing keys fall back to the
    running interpreter's defaults.
    """

    name: str
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourcedRequirement:
    """A requirement together with the plugin that contributed it."""

    source: str
    raw: str
    requirement: Requirement


@dataclass(frozen=True)
class Conflict:
    """A proven conflict for one package on one target.

    ``requirements`` is a deletion-minimal core: removing any single entry
    makes the conflict unprovable with the remaining ones.
    """

    package: NormalizedName
    kind: str  # "version" or "url"
    detail: str
    requirements: tuple[SourcedRequirement, ...]


@dataclass(frozen=True)
class MergedPackage:
    """The merged constraint one target imposes on one package."""

    name: NormalizedName
    status: str  # "ok", "unknown" or "conflict"
    extras: frozenset[str]
    specifier: str
    simplified: str | None
    urls: tuple[str, ...]
    requirements: tuple[SourcedRequirement, ...]
    redundant: tuple[SourcedRequirement, ...]
    conflicts: tuple[Conflict, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class TargetGroup:
    """Targets that produced identical merged results."""

    targets: tuple[str, ...]
    packages: tuple[MergedPackage, ...]


@dataclass(frozen=True)
class MergeReport:
    """The outcome of a merge, grouped by identical target results."""

    extras: tuple[str, ...]
    groups: tuple[TargetGroup, ...]

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        """All conflicts across all target groups."""
        return tuple(
            conflict
            for group in self.groups
            for package in group.packages
            for conflict in package.conflicts
        )


def _active_extras(
    requirement: Requirement, environment: Mapping[str, str], extras: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the selected extras (plus ``""``) under which a requirement holds."""
    if requirement.marker is None:
        return ("", *extras)
    return tuple(
        extra
        for extra in ("", *extras)
        if requirement.marker.evaluate(
            {**environment, "extra": extra}, context="metadata"
        )
    )


def _combined_specifier(requirements: Iterable[SourcedRequirement]) -> SpecifierSet:
    combined = SpecifierSet()
    for sourced in requirements:
        combined &= sourced.requirement.specifier
    return combined


def _minimal_core(
    requirements: list[SourcedRequirement],
    still_conflicting: Callable[[list[SourcedRequirement]], bool],
) -> tuple[SourcedRequirement, ...]:
    """Shrink a conflicting set so removing any entry lifts the conflict."""
    core = list(requirements)
    for sourced in list(core):
        trial = [entry for entry in core if entry is not sourced]
        if trial and still_conflicting(trial):
            core = trial
    return tuple(core)


def _merge_package(
    name: NormalizedName, requirements: list[SourcedRequirement]
) -> MergedPackage:
    extras = frozenset(
        canonicalize_name(extra)
        for sourced in requirements
        for extra in sourced.requirement.extras
    )
    url_requirements = [sr for sr in requirements if sr.requirement.url]
    urls = tuple(
        sorted({sr.requirement.url for sr in url_requirements if sr.requirement.url})
    )
    conflicts: list[Conflict] = []
    notes: list[str] = []

    if len(urls) > 1:
        core = _minimal_core(
            url_requirements,
            lambda trial: len({sr.requirement.url for sr in trial}) > 1,
        )
        conflicts.append(
            Conflict(
                package=name,
                kind="url",
                detail=(
                    f"required from {len(urls)} distinct direct URLs; "
                    "different URLs are not assumed to be the same source"
                ),
                requirements=core,
            )
        )

    combined = _combined_specifier(requirements)
    has_arbitrary = any(spec.operator == "===" for spec in combined)

    if combined.is_unsatisfiable():
        core = _minimal_core(
            requirements,
            lambda trial: _combined_specifier(trial).is_unsatisfiable(),
        )
        conflicts.append(
            Conflict(
                package=name,
                kind="version",
                detail=f"merged specifier {combined!s} is unsatisfiable",
                requirements=core,
            )
        )

    simplified: str | None = None
    if has_arbitrary:
        notes.append(
            "=== (arbitrary equality) constraint kept as-is; "
            "canonical simplification not computed"
        )
    elif not conflicts:
        simplified_set = combined.to_range().to_specifier_set()
        if simplified_set is not None:
            simplified = str(simplified_set)
        else:
            notes.append(
                "no equivalent SpecifierSet exists; original constraints kept"
            )

    redundant: list[SourcedRequirement] = []
    if not conflicts and not has_arbitrary:
        for sourced in requirements:
            specifier = sourced.requirement.specifier
            if not specifier or any(s.operator == "===" for s in specifier):
                continue
            rest = _combined_specifier(
                entry for entry in requirements if entry is not sourced
            )
            try:
                if rest.is_subset(specifier):
                    redundant.append(sourced)
            except ValueError:
                notes.append(
                    f"could not prove whether {sourced.raw!r} is redundant; kept"
                )

    unknown = False
    if urls and any(sourced.requirement.specifier for sourced in requirements):
        unknown = True
        notes.append(
            "direct URL combined with version specifiers; compatibility "
            "cannot be verified without fetching the URL, both kept"
        )

    if conflicts:
        status = "conflict"
    elif unknown:
        status = "unknown"
    else:
        status = "ok"

    return MergedPackage(
        name=name,
        status=status,
        extras=extras,
        specifier=str(combined),
        simplified=simplified,
        urls=urls,
        requirements=tuple(requirements),
        redundant=tuple(redundant),
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )


def _merge_target(
    sourced: list[SourcedRequirement], target: Target, extras: tuple[str, ...]
) -> tuple[MergedPackage, ...]:
    by_name: dict[NormalizedName, list[SourcedRequirement]] = {}
    for entry in sourced:
        if _active_extras(entry.requirement, target.environment, extras):
            by_name.setdefault(canonicalize_name(entry.requirement.name), []).append(
                entry
            )
    return tuple(
        _merge_package(name, entries) for name, entries in sorted(by_name.items())
    )


def merge_requirements(
    plugins: Mapping[str, Iterable[str | Requirement]],
    targets: Iterable[Target],
    extras: Iterable[str] = (),
) -> MergeReport:
    """Merge ``Requires-Dist`` requirements from plugins over a target matrix.

    :param plugins: Maps each plugin (source) name to its requirement strings
        or :class:`~packaging.requirements.Requirement` objects.
    :param targets: The finite target environment matrix.
    :param extras: Extras selected for the merge; a requirement whose marker
        references ``extra`` only applies where the marker holds for the
        target environment and one of the selected extras.
    :returns: A :class:`MergeReport` grouping targets with identical results.
    """
    selected = tuple(sorted({canonicalize_name(extra) for extra in extras}))
    sourced = [
        SourcedRequirement(
            source=source,
            raw=str(raw),
            requirement=raw if isinstance(raw, Requirement) else Requirement(raw),
        )
        for source, requirements in plugins.items()
        for raw in requirements
    ]
    grouped: dict[tuple[MergedPackage, ...], list[str]] = {}
    for target in targets:
        packages = _merge_target(sourced, target, selected)
        grouped.setdefault(packages, []).append(target.name)
    return MergeReport(
        extras=selected,
        groups=tuple(
            TargetGroup(targets=tuple(sorted(names)), packages=packages)
            for packages, names in grouped.items()
        ),
    )


def format_report(report: MergeReport) -> str:
    """Render a :class:`MergeReport` as human-readable text."""
    lines = []
    selected = ", ".join(report.extras) or "(none)"
    lines.append(f"selected extras: {selected}")
    for group in report.groups:
        lines.append(f"targets: {', '.join(group.targets)}")
        for package in group.packages:
            extras = f"[{','.join(sorted(package.extras))}]" if package.extras else ""
            specifier = package.simplified
            if specifier is None:
                specifier = f"{package.specifier or '(any)'} (not simplified)"
            elif specifier != package.specifier:
                specifier = f"{specifier} (from {package.specifier or '(any)'})"
            lines.append(
                f"  {package.name}{extras}: {specifier} [{package.status}]"
            )
            for url in package.urls:
                lines.append(f"    url: {url}")
            for note in package.notes:
                lines.append(f"    note: {note}")
            for conflict in package.conflicts:
                lines.append(f"    conflict ({conflict.kind}): {conflict.detail}")
                for sourced in conflict.requirements:
                    lines.append(f"      {sourced.raw}  (from {sourced.source})")
    return "\n".join(lines)
