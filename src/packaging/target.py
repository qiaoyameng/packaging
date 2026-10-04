# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Offline description of an interpreter/platform target.

This module lets packaging tools decide which wheels are compatible with a
target environment that is *not* the running interpreter.  Unlike
:func:`packaging.tags.sys_tags`, the tags are derived solely from the
explicitly provided :class:`TargetProfile`; the host machine is never
inspected (no ``sysconfig``, ``platform``, ``sys`` or subprocess probes are
used to fill in missing details).

Typical usage::

    from packaging.target import ManylinuxProfile, TargetProfile, rank_wheels

    profile = TargetProfile(
        python_version=(3, 13),
        platform=ManylinuxProfile(arch="x86_64", glibc=(2, 28)),
    )
    ranking = rank_wheels(
        profile,
        ["foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"],
    )

The only supported interpreter in this version is CPython.  Targets for
Linux (manylinux/musllinux), Windows, and macOS are supported.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _manylinux, _musllinux
from .tags import Tag, compatible_tags, cpython_tags, mac_platforms
from .utils import BuildTag, parse_wheel_filename

__all__ = [
    "IncompleteProfileError",
    "InsufficientInformation",
    "MacOSProfile",
    "ManylinuxProfile",
    "Mismatch",
    "MusllinuxProfile",
    "PlatformProfile",
    "RankedWheel",
    "TargetProfile",
    "WheelMatch",
    "WheelRejection",
    "evaluate_wheel",
    "rank_wheels",
]


def __dir__() -> list[str]:
    return __all__


# Architectures accepted in ``linux_<arch>``/``manylinux``/``musllinux`` tags.
_LINUX_ARCHS = frozenset(
    {
        "x86_64",
        "i686",
        "aarch64",
        "armv8l",
        "armv7l",
        "ppc64",
        "ppc64le",
        "s390x",
        "loongarch64",
        "riscv64",
    }
)

# Windows architecture aliases and the platform tag they map to.
_WINDOWS_ARCHS = {
    "AMD64": "win_amd64",
    "x86": "win32",
    "x86_32": "win32",
    "ARM64": "win_arm64",
    "aarch64": "win_arm64",
}

_MACOS_ARCHS = frozenset(
    {
        "x86_64",
        "i386",
        "ppc",
        "ppc64",
        "arm64",
        "universal2",
        "universal",
        "intel",
    }
)


class IncompleteProfileError(ValueError):
    """Raised when tags are requested from an incomplete :class:`TargetProfile`.

    .. versionadded:: 26.4
    """


def _summarize(values: set[str], limit: int = 12) -> str:
    """Render a set of tag components compactly in an explanatory message."""
    ordered = sorted(values)
    if len(ordered) > limit:
        return f"{ordered[:limit]}, ... ({len(ordered)} total)"
    return f"{ordered}"


def _validate_libc_version(version: tuple[int, int], name: str) -> None:
    if (
        not isinstance(version, tuple)
        or len(version) != 2
        or not all(isinstance(part, int) and part >= 0 for part in version)
    ):
        raise ValueError(f"{name} must be a (major, minor) tuple of non-negative ints")
    if version[0] < 1:
        raise ValueError(f"{name} major version must be >= 1")


def _target_cpython_abis(
    python_version: tuple[int, int],
    *,
    threaded: bool,
    debug: bool,
    pymalloc: bool,
    wide_unicode: bool,
) -> list[str]:
    """Return the CPython ABI tags for an offline target.

    Mirrors :func:`packaging.tags._cpython_abis` without consulting
    ``sysconfig`` or the running interpreter.
    """
    version = f"{python_version[0]}{python_version[1]}"
    threading_flag = "t" if threaded else ""
    debug_flag = "d" if debug else ""
    pymalloc_flag = ""
    ucs4_flag = ""
    if python_version < (3, 8) and pymalloc:
        pymalloc_flag = "m"
    if python_version < (3, 3) and wide_unicode:
        ucs4_flag = "u"
    abis = [f"cp{version}{threading_flag}{debug_flag}{pymalloc_flag}{ucs4_flag}"]
    if python_version >= (3, 8) and debug:
        # Debug builds can also load "normal" extension modules.
        abis.append(f"cp{version}{threading_flag}")
    return abis


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """Base class for offline platform descriptions.

    Subclasses describe a concrete OS/architecture combination and provide the
    platform tags (the third component of a compatibility tag) supported by
    that target.

    .. versionadded:: 26.4
    """

    def platform_tags(self) -> list[str]:
        """Return the platform tags supported by this target, best first."""
        raise NotImplementedError

    def missing_information(self) -> list[str]:
        """Return human-readable descriptions of missing required parameters."""
        return []


@dataclass(frozen=True, slots=True)
class ManylinuxProfile(PlatformProfile):
    """A CPython-on-glibc Linux target (PEP 600 manylinux).

    :param str arch: The CPU architecture tag suffix, e.g. ``"x86_64"`` or
        ``"aarch64"``.  The same set of architectures used in
        ``linux_*``/``manylinux_*`` tags is accepted.
    :param tuple glibc: The target glibc version as a ``(major, minor)``
        tuple, e.g. ``(2, 28)``.  ``None`` means the glibc version is unknown.

    .. versionadded:: 26.4
    """

    arch: str | None = None
    glibc: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.arch is not None and self.arch not in _LINUX_ARCHS:
            raise ValueError(
                f"Unknown manylinux architecture: {self.arch!r}; expected one of "
                f"{sorted(_LINUX_ARCHS)}"
            )
        if self.glibc is not None:
            _validate_libc_version(self.glibc, "glibc")

    def platform_tags(self) -> list[str]:
        if self.arch is None or self.glibc is None:
            raise IncompleteProfileError(
                "Cannot generate manylinux tags: "
                + ", ".join(self.missing_information())
            )
        tags: list[str] = []
        archs = _linux_archs(self.arch)
        for arch in archs:
            tags.append(f"linux_{arch}")
        tags.extend(_manylinux.glibc_platform_tags(archs, self.glibc))
        return tags

    def missing_information(self) -> list[str]:
        missing = []
        if self.arch is None:
            missing.append("manylinux target architecture")
        if self.glibc is None:
            missing.append("manylinux target glibc version")
        return missing


@dataclass(frozen=True, slots=True)
class MusllinuxProfile(PlatformProfile):
    """A CPython-on-musl Linux target (PEP 656 musllinux).

    :param str arch: The CPU architecture tag suffix, e.g. ``"x86_64"`` or
        ``"aarch64"``.
    :param tuple musl: The target musl libc version as a ``(major, minor)``
        tuple, e.g. ``(1, 2)``.  ``None`` means the musl version is unknown.

    .. versionadded:: 26.4
    """

    arch: str | None = None
    musl: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.arch is not None and self.arch not in _LINUX_ARCHS:
            raise ValueError(
                f"Unknown musllinux architecture: {self.arch!r}; expected one of "
                f"{sorted(_LINUX_ARCHS)}"
            )
        if self.musl is not None:
            _validate_libc_version(self.musl, "musl")

    def platform_tags(self) -> list[str]:
        if self.arch is None or self.musl is None:
            raise IncompleteProfileError(
                "Cannot generate musllinux tags: "
                + ", ".join(self.missing_information())
            )
        archs = _linux_archs(self.arch)
        tags = [f"linux_{arch}" for arch in archs]
        tags.extend(_musllinux.musl_platform_tags(archs, self.musl))
        return tags

    def missing_information(self) -> list[str]:
        missing = []
        if self.arch is None:
            missing.append("musllinux target architecture")
        if self.musl is None:
            missing.append("musllinux target musl version")
        return missing


@dataclass(frozen=True, slots=True)
class WindowsProfile(PlatformProfile):
    """A CPython Windows target.

    :param str arch: The CPU architecture: one of ``"AMD64"``, ``"x86"`` and
        ``"ARM64"`` (common aliases such as ``"aarch64"`` are accepted).
        ``None`` means the architecture is unknown.

    .. versionadded:: 26.4
    """

    arch: str | None = None

    def __post_init__(self) -> None:
        if self.arch is not None and self.arch not in _WINDOWS_ARCHS:
            raise ValueError(
                f"Unknown Windows architecture: {self.arch!r}; expected one of "
                f"{sorted(_WINDOWS_ARCHS)}"
            )

    def platform_tags(self) -> list[str]:
        if self.arch is None:
            raise IncompleteProfileError(
                "Cannot generate Windows tags: Windows target architecture"
            )
        return [_WINDOWS_ARCHS[self.arch]]

    def missing_information(self) -> list[str]:
        if self.arch is None:
            return ["Windows target architecture"]
        return []


@dataclass(frozen=True, slots=True)
class MacOSProfile(PlatformProfile):
    """A CPython macOS target.

    :param tuple version: The target macOS version as a ``(major, minor)``
        tuple, e.g. ``(13, 0)``.  ``None`` means the version is unknown.
    :param str arch: The CPU architecture, e.g. ``"x86_64"`` or ``"arm64"``.
        ``"universal2"`` is also accepted.  ``None`` means the architecture is
        unknown.

    .. versionadded:: 26.4
    """

    version: tuple[int, int] | None = None
    arch: str | None = None

    def __post_init__(self) -> None:
        if self.version is not None:
            if (
                not isinstance(self.version, tuple)
                or len(self.version) != 2
                or not all(
                    isinstance(part, int) and part >= 0 for part in self.version
                )
            ):
                raise ValueError(
                    "macOS version must be a (major, minor) tuple of non-negative ints"
                )
        if self.arch is not None and self.arch not in _MACOS_ARCHS:
            raise ValueError(
                f"Unknown macOS architecture: {self.arch!r}; expected one of "
                f"{sorted(_MACOS_ARCHS)}"
            )

    def platform_tags(self) -> list[str]:
        if self.version is None or self.arch is None:
            raise IncompleteProfileError(
                "Cannot generate macOS tags: "
                + ", ".join(self.missing_information())
            )
        return list(mac_platforms(self.version, self.arch))

    def missing_information(self) -> list[str]:
        missing = []
        if self.version is None:
            missing.append("macOS target version")
        if self.arch is None:
            missing.append("macOS target architecture")
        return missing


def _linux_archs(arch: str) -> list[str]:
    # A 32-bit interpreter on a 64-bit ARM kernel uses armv8l as its native
    # tag and can also use armv7l wheels.
    if arch == "armv8l":
        return ["armv8l", "armv7l"]
    return [arch]


@dataclass(frozen=True, slots=True)
class TargetProfile:
    """An explicit, offline description of a CPython target environment.

    All values needed to generate compatibility tags must be provided here;
    this class never inspects the running interpreter or host system to fill
    in defaults.

    :param tuple python_version: A ``(major, minor)`` tuple describing the
        targeted CPython version, e.g. ``(3, 13)``.  ``None`` means the
        interpreter version is unknown.
    :param PlatformProfile platform: The target platform.  ``None`` means no
        platform was described.
    :param bool threaded: Whether the target is a free-threaded
        (``--disable-gil``) build (Python 3.13+).
    :param bool debug: Whether the target is a debug build.
    :param bool pymalloc: Whether the target uses pymalloc (only relevant for
        CPython older than 3.8).
    :param bool wide_unicode: Whether the target uses UCS-4 Unicode (only
        relevant for CPython older than 3.3).

    .. versionadded:: 26.4
    """

    python_version: tuple[int, int] | None = None
    platform: PlatformProfile | None = None
    threaded: bool = False
    debug: bool = False
    pymalloc: bool = True
    wide_unicode: bool = True

    def __post_init__(self) -> None:
        if self.python_version is not None:
            if (
                not isinstance(self.python_version, tuple)
                or len(self.python_version) != 2
                or not all(
                    isinstance(part, int) and part >= 0 for part in self.python_version
                )
            ):
                raise ValueError(
                    "python_version must be a (major, minor) tuple of non-negative ints"
                )
            if self.threaded and self.python_version < (3, 13):
                raise ValueError(
                    "Free-threaded (--disable-gil) builds require Python 3.13 or newer"
                )
        elif self.threaded:
            raise ValueError(
                "python_version is required to describe a free-threaded build"
            )

    def missing_information(self) -> list[str]:
        """Return descriptions of the parameters required but not provided.

        The returned list is empty exactly when :meth:`tags` can be computed.
        """
        missing = []
        if self.python_version is None:
            missing.append("CPython interpreter version")
        if self.platform is None:
            missing.append("target platform (OS and architecture)")
        else:
            missing.extend(self.platform.missing_information())
        return missing

    @property
    def is_complete(self) -> bool:
        """Whether the profile carries everything needed to generate tags."""
        return not self.missing_information()

    def tags(self) -> list[Tag]:
        """Return the ordered list of tags supported by this target.

        Tags are ordered best-match first, reusing the same algorithms as
        :func:`packaging.tags.sys_tags`.

        :raises IncompleteProfileError: If required parameters are missing.
        """
        missing = self.missing_information()
        if missing:
            raise IncompleteProfileError(
                "Cannot generate tags without: " + ", ".join(missing)
            )
        assert self.python_version is not None
        assert self.platform is not None
        platforms = self.platform.platform_tags()
        abis = _target_cpython_abis(
            self.python_version,
            threaded=self.threaded,
            debug=self.debug,
            pymalloc=self.pymalloc,
            wide_unicode=self.wide_unicode,
        )
        tags: list[Tag] = []
        seen: set[Tag] = set()
        for source in (
            cpython_tags(self.python_version, abis, platforms),
            compatible_tags(
                self.python_version,
                f"cp{self.python_version[0]}{self.python_version[1]}",
                platforms,
            ),
        ):
            for tag in source:
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
        return tags


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One interpreter/ABI/platform component that does not match.

    :ivar str component: One of ``"interpreter"``, ``"abi"`` or ``"platform"``.
    :ivar str reason: A human-readable explanation of the mismatch.

    .. versionadded:: 26.4
    """

    component: str
    reason: str


@dataclass(frozen=True, slots=True)
class WheelMatch:
    """A wheel compatible with the target.

    :ivar str filename: The wheel filename that was evaluated.
    :ivar Tag best_tag: The highest-priority supported tag of the wheel.
    :ivar int rank: The zero-based rank of :attr:`best_tag` in the target's
        tag sequence; lower ranks are better matches.
    :ivar BuildTag build_tag: The parsed build tag (``()`` when absent).

    .. versionadded:: 26.4
    """

    filename: str
    best_tag: Tag
    rank: int
    build_tag: BuildTag


@dataclass(frozen=True, slots=True)
class WheelRejection:
    """A wheel not compatible with the target.

    :ivar str filename: The wheel filename that was evaluated.
    :ivar tuple mismatches: The component-level reasons for the rejection.
    :ivar str detail: All mismatch reasons joined into one sentence.

    .. versionadded:: 26.4
    """

    filename: str
    mismatches: tuple[Mismatch, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class InsufficientInformation:
    """The target profile lacks information needed to judge a wheel.

    :ivar str filename: The wheel filename that could not be judged.
    :ivar tuple missing: Human-readable descriptions of what is missing.
    :ivar str detail: All missing items joined into one sentence.

    .. versionadded:: 26.4
    """

    filename: str
    missing: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class RankedWheel:
    """One entry of the result of :func:`rank_wheels`.

    Exactly one of :attr:`match`, :attr:`rejection` and :attr:`insufficient`
    is not ``None``.

    :ivar int index: The position of the filename in the input, used as a
        stable tie-breaker.

    .. versionadded:: 26.4
    """

    index: int
    match: WheelMatch | None = None
    rejection: WheelRejection | None = None
    insufficient: InsufficientInformation | None = None

    @property
    def available(self) -> bool:
        """Whether the wheel can be used on the target."""
        return self.match is not None


def _mismatches(
    wheel_tags: frozenset[Tag], supported_tags: tuple[Tag, ...]
) -> tuple[Mismatch, ...]:
    wheel_interpreters = {tag.interpreter for tag in wheel_tags}
    wheel_abis = {tag.abi for tag in wheel_tags}
    wheel_platforms = {tag.platform for tag in wheel_tags}

    supported_interpreters = {tag.interpreter for tag in supported_tags}
    supported_abis = {tag.abi for tag in supported_tags}
    supported_platforms = {tag.platform for tag in supported_tags}

    mismatches: list[Mismatch] = []

    if not wheel_interpreters & supported_interpreters:
        mismatches.append(
            Mismatch(
                "interpreter",
                "wheel supports interpreter tag(s) "
                f"{_summarize(wheel_interpreters)}, but the target supports "
                f"{_summarize(supported_interpreters)}",
            )
        )
    if not wheel_abis & supported_abis:
        mismatches.append(
            Mismatch(
                "abi",
                f"wheel requires ABI tag(s) {_summarize(wheel_abis)}, but the "
                f"target supports {_summarize(supported_abis)}",
            )
        )
    if not wheel_platforms & supported_platforms:
        mismatches.append(
            Mismatch(
                "platform",
                f"wheel supports platform tag(s) {_summarize(wheel_platforms)}, but "
                f"the target supports {_summarize(supported_platforms)}",
            )
        )
    if not mismatches:
        # Every component intersects individually, but no complete triple is
        # supported together (e.g. incompatible interpreter/abi pairing).
        mismatches.append(
            Mismatch(
                "tag",
                "none of the wheel's complete tag triples are supported by the "
                f"target: wheel {sorted(map(str, wheel_tags))}",
            )
        )
    return tuple(mismatches)


def evaluate_wheel(
    profile: TargetProfile, filename: str
) -> WheelMatch | WheelRejection | InsufficientInformation:
    """Explain whether one wheel is usable on the described target.

    A wheel is compatible if at least one of its (possibly compressed) tag
    triples appears in the target's tag sequence.  Compressed tag sets are
    expanded via :func:`packaging.utils.parse_wheel_filename`.

    :param TargetProfile profile: The offline target description.
    :param str filename: The wheel filename, e.g.
        ``"foo-1.0-1-cp313-cp313-manylinux_2_28_x86_64.whl"``.
    :raises packaging.utils.InvalidWheelFilename: If the filename is not a
        valid wheel filename.
    """
    missing = profile.missing_information()
    if missing:
        return InsufficientInformation(
            filename, tuple(missing), "missing: " + ", ".join(missing)
        )

    _, _, build, wheel_tags = parse_wheel_filename(filename)
    supported_tags = tuple(profile.tags())
    tag_ranks = {tag: rank for rank, tag in enumerate(supported_tags)}

    matching = [(tag_ranks[tag], tag) for tag in wheel_tags if tag in tag_ranks]
    if matching:
        rank, best_tag = min(matching, key=lambda item: item[0])
        return WheelMatch(filename, best_tag, rank, build)

    mismatches = _mismatches(wheel_tags, supported_tags)
    detail = "; ".join(f"{item.component}: {item.reason}" for item in mismatches)
    return WheelRejection(filename, mismatches, detail)


def _build_sort_key(build: BuildTag) -> tuple[bool, int, str]:
    # Wheels with a build tag sort before otherwise identical wheels without
    # one (PEP 427); within build tags, the leading integer then the suffix
    # sort in ascending order, so newer builds compare greater.
    if not build:
        return (False, 0, "")
    return (True, build[0], build[1])


def rank_wheels(
    profile: TargetProfile, filenames: list[str] | tuple[str, ...]
) -> list[RankedWheel]:
    """Evaluate several wheels and order the compatible ones best first.

    Compatible wheels are ordered by:

    1. the rank of their best matching tag (better supported tags first),
    2. the wheel build tag per PEP 427 (higher build number first),
    3. the original input position (stable tie-breaking).

    Rejected wheels and wheels that cannot be judged because the profile is
    incomplete follow in their original input order.

    :param TargetProfile profile: The offline target description.
    :param filenames: The candidate wheel filenames.
    :raises packaging.utils.InvalidWheelFilename: If a filename is not a valid
        wheel filename.

    .. versionadded:: 26.4
    """
    results = [
        (index, evaluate_wheel(profile, filename))
        for index, filename in enumerate(filenames)
    ]

    matches = [
        (index, result)
        for index, result in results
        if isinstance(result, WheelMatch)
    ]
    # ``results`` is already in ascending input order, and Python's sort is
    # stable.  Sorting by build tag first and then (stably) by rank yields
    # rank ascending, with the build tag only breaking ties *within* one
    # rank and the input position remaining the final tie-breaker.
    matches.sort(key=lambda item: _build_sort_key(item[1].build_tag), reverse=True)
    matches.sort(key=lambda item: item[1].rank)

    match_indices = {index for index, _ in matches}
    ordered: list[tuple[int, WheelMatch | WheelRejection | InsufficientInformation]] = [
        *matches,
        *(
            (index, result)
            for index, result in results
            if index not in match_indices
        ),
    ]

    ranked: list[RankedWheel] = []
    for index, result in ordered:
        if isinstance(result, WheelMatch):
            ranked.append(RankedWheel(index, match=result))
        elif isinstance(result, WheelRejection):
            ranked.append(RankedWheel(index, rejection=result))
        else:
            ranked.append(RankedWheel(index, insufficient=result))
    return ranked
