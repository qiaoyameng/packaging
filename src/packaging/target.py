"""Offline description of a target interpreter environment.

This module provides :class:`TargetProfile`, an explicit, offline description
of the environment wheels are selected for. Unlike
:func:`packaging.tags.sys_tags`, a target profile never consults the running
interpreter, :mod:`sysconfig`, or the host operating system to fill in
missing details: every value required to decide compatibility must be
provided. When a profile does not carry enough information to judge a wheel,
:class:`InsufficientTargetInformationError` is raised rather than guessing a
platform.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Iterable, Sequence
from enum import Enum

from . import _manylinux, _musllinux
from .tags import Tag, compatible_tags, cpython_tags, mac_platforms
from .utils import BuildTag, parse_wheel_filename
from .version import Version

__all__ = [
    "ABIComponentMismatch",
    "ComponentMismatch",
    "InsufficientTargetInformationError",
    "InterpreterComponentMismatch",
    "InvalidTargetProfile",
    "PlatformComponentMismatch",
    "PlatformOS",
    "TargetProfile",
    "WheelEvaluation",
]


def __dir__() -> list[str]:
    return __all__


class PlatformOS(Enum):
    """The operating system family of a target environment."""

    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"

    def __str__(self) -> str:
        return self.value


class InvalidTargetProfile(ValueError):
    """Raised when a :class:`TargetProfile` describes an unsupported target.

    This covers inconsistent values (e.g. a libc version on a non-Linux
    target), values outside the supported range (e.g. a free-threaded build
    on Python older than 3.13), and architectures that have no
    corresponding platform tags.
    """


class InsufficientTargetInformationError(ValueError):
    """Raised when a target lacks information needed to judge compatibility.

    A profile never falls back to :mod:`sysconfig` or host introspection. If
    a decision cannot be made from the explicitly provided values (e.g. a
    Linux profile with no libc family or libc version), this error is raised
    instead of assuming a platform.
    """


@dataclasses.dataclass(frozen=True)
class InterpreterComponentMismatch:
    """The interpreter component of a tag is not supported by the target.

    :ivar value: The interpreter component that did not match, e.g.
        ``"pp310"``.
    :ivar reason: A human-readable explanation of the mismatch.
    """

    value: str
    reason: str

    def __str__(self) -> str:
        return f"interpreter {self.value!r}: {self.reason}"


@dataclasses.dataclass(frozen=True)
class ABIComponentMismatch:
    """The ABI component of a tag is not supported by the target.

    :ivar value: The ABI component that did not match, e.g. ``"cp312d"``.
    :ivar reason: A human-readable explanation of the mismatch.
    """

    value: str
    reason: str

    def __str__(self) -> str:
        return f"ABI {self.value!r}: {self.reason}"


@dataclasses.dataclass(frozen=True)
class PlatformComponentMismatch:
    """The platform component of a tag is not supported by the target.

    :ivar value: The platform component that did not match, e.g.
        ``"manylinux_2_28_x86_64"``.
    :ivar reason: A human-readable explanation of the mismatch.
    """

    value: str
    reason: str

    def __str__(self) -> str:
        return f"platform {self.value!r}: {self.reason}"


ComponentMismatch = (
    InterpreterComponentMismatch | ABIComponentMismatch | PlatformComponentMismatch
)
"""Union of the three possible per-component incompatibility explanations."""


@dataclasses.dataclass(frozen=True)
class WheelEvaluation:
    """The result of comparing one wheel filename against a target profile.

    For a compatible wheel, :attr:`compatible` is true, :attr:`best_tag` is
    the highest-priority supported tag of the wheel, and :attr:`mismatches`
    is empty. For an incompatible wheel, :attr:`compatible` is false,
    :attr:`best_tag` is ``None``, and :attr:`mismatches` explains why every
    tag of the wheel was rejected (one :class:`ComponentMismatch` per
    distinct failing component).

    :ivar filename: The wheel filename that was evaluated.
    :ivar name: The normalized project name parsed from the filename.
    :ivar version: The project version parsed from the filename.
    :ivar build: The build tag parsed from the filename; ``()`` if absent.
    :ivar compatible: Whether the wheel can be installed on the target.
    :ivar best_tag: The best matching supported tag, or ``None``.
    :ivar mismatches: Distinct component-level incompatibility explanations.
    """

    filename: str
    name: str
    version: Version
    build: BuildTag
    compatible: bool
    best_tag: Tag | None
    mismatches: tuple[ComponentMismatch, ...]

    @property
    def explanation(self) -> str:
        """A one-line human-readable summary of the evaluation."""
        if self.compatible:
            assert self.best_tag is not None
            return f"compatible with the target; best matching tag {self.best_tag}"
        if not self.mismatches:
            return "not compatible with the target"
        return "not compatible: " + "; ".join(map(str, self.mismatches))


# ---------------------------------------------------------------------------
# Architecture definitions
# ---------------------------------------------------------------------------

# Architectures that have well-defined manylinux/musllinux platform tags.
_LINUX_ARCHS: frozenset[str] = frozenset(
    {
        "x86_64",
        "i686",
        "aarch64",
        "armv7l",
        "ppc64",
        "ppc64le",
        "s390x",
        "riscv64",
        "loongarch64",
    }
)

# Accepted Windows architecture aliases, mapped to the platform tags they
# imply (in priority order).
_WINDOWS_ARCHS: dict[str, tuple[str, ...]] = {
    "x86_64": ("win_amd64",),
    "amd64": ("win_amd64",),
    "x86": ("win32",),
    "i386": ("win32",),
    "i686": ("win32",),
    "arm64": ("win_arm64",),
    "aarch64": ("win_arm64",),
}

_MACOS_ARCHS: frozenset[str] = frozenset({"x86_64", "arm64"})

# Legacy manylinux aliases and the glibc versions they pin (PEP 513/571/599).
_LEGACY_MANYLINUX: dict[str, tuple[int, int]] = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def _linux_archs(arch: str) -> tuple[str, ...]:
    # armv8l (a 32-bit Python on a 64-bit ARM kernel) also runs armv7l
    # wheels.
    if arch == "armv8l":
        return ("armv8l", "armv7l")
    if arch not in _LINUX_ARCHS:
        raise InvalidTargetProfile(f"unsupported Linux architecture: {arch!r}")
    return (arch,)


def _as_version_pair(
    value: Sequence[int] | None, field: str
) -> tuple[int, int] | None:
    if value is None:
        return None
    pair = tuple(value)
    if len(pair) != 2:
        raise InvalidTargetProfile(f"{field} must be a (major, minor) pair")
    first, second = pair
    for part in pair:
        if not isinstance(part, int) or isinstance(part, bool) or part < 0:
            raise InvalidTargetProfile(
                f"{field} must be a (major, minor) pair of non-negative ints"
            )
    return first, second


def _cpython_abis(
    python_version: tuple[int, int],
    *,
    debug: bool,
    free_threaded: bool,
    pymalloc: bool | None,
    ucs4: bool | None,
) -> tuple[str, ...]:
    """Build the CPython ABI tags from explicit feature flags.

    Mirrors ``packaging.tags._cpython_abis`` but takes every flag as an
    argument instead of reading :mod:`sysconfig`.
    """
    major, minor = python_version
    version = f"{major}{minor}"
    threading = "t" if free_threaded else ""
    suffix = ""
    if debug:
        suffix += "d"
    if (major, minor) < (3, 8):
        if pymalloc is None or pymalloc:
            suffix += "m"
        if (major, minor) < (3, 3) and (ucs4 is None or ucs4):
            suffix += "u"
    abis = [f"cp{version}{threading}{suffix}"]
    if (major, minor) >= (3, 8) and debug:
        # Debug builds can also load normal extension modules.
        abis.append(f"cp{version}{threading}")
    return tuple(abis)


def _build_key(build: BuildTag) -> tuple[int, int, str]:
    # Wheels with a build tag are ordered ahead of wheels without one; within
    # each group, leading digits compare numerically and the remainder
    # lexicographically (PEP 427).
    if build:
        return (0, build[0], build[1])
    return (1, 0, "")


# ---------------------------------------------------------------------------
# Platform-tag parsing (for mismatch explanations)
# ---------------------------------------------------------------------------


def _parse_macos_platform(platform: str) -> tuple[tuple[int, int], str] | None:
    # e.g. macosx_14_0_arm64, macosx_10_9_universal2
    match = re.fullmatch(r"macosx_(\d+)_(\d+)_(.+)", platform)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2))), match.group(3)


def _parse_glibc_platform(platform: str) -> tuple[tuple[int, int], str] | None:
    match = re.fullmatch(r"manylinux_(\d+)_(\d+)_(.+)", platform)
    if match is not None:
        return (int(match.group(1)), int(match.group(2))), match.group(3)
    for legacy, version in _LEGACY_MANYLINUX.items():
        if platform.startswith(f"{legacy}_"):
            return version, platform[len(legacy) + 1 :]
    return None


def _parse_musl_platform(platform: str) -> tuple[tuple[int, int], str] | None:
    match = re.fullmatch(r"musllinux_(\d+)_(\d+)_(.+)", platform)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2))), match.group(3)


class _Compatibility:
    """Precomputed supported-tag sets and per-component explanations."""

    def __init__(self, profile: TargetProfile) -> None:
        self.profile = profile
        tags = profile.tags
        self.supported = frozenset(tags)
        self.rank = {tag: index for index, tag in enumerate(tags)}
        self.interpreters = frozenset(tag.interpreter for tag in tags)
        self.abis = frozenset(tag.abi for tag in tags)
        self.platforms = frozenset(tag.platform for tag in tags)

    # -- interpreter -------------------------------------------------------

    def explain_interpreter(self, value: str) -> str | None:
        if value in self.interpreters:
            return None
        target = self.profile.python_version
        if value == "py3":
            return None
        versioned_generic = re.fullmatch(r"py(\d)(\d+)", value)
        if versioned_generic is not None:
            generic_major, generic_minor = (
                int(versioned_generic.group(1)),
                int(versioned_generic.group(2)),
            )
            if generic_major == target[0] and generic_minor <= target[1]:
                return None
            return (
                f"requires Python {generic_major}.{generic_minor}, target is "
                f"Python {target[0]}.{target[1]}"
            )
        cp_match = re.fullmatch(r"cp(\d)(\d+)", value)
        if cp_match is not None:
            # A cp nodot tag is "cp" + one major digit + the minor digits,
            # e.g. cp313 -> (3, 13).
            cp_major, cp_minor = int(cp_match.group(1)), int(cp_match.group(2))
            if cp_major == target[0] and cp_minor == target[1]:
                return None
            if cp_major < target[0] or (
                cp_major == target[0] and cp_minor < target[1]
            ):
                # Older-version cp tags are supported only together with the
                # stable abi3/abi3t ABI, which is checked separately.
                return (
                    f"binary built for CPython {cp_major}.{cp_minor}; "
                    f"target is CPython {target[0]}.{target[1]} and the "
                    f"wheel does not use the stable ABI"
                )
            return (
                f"requires CPython {cp_major}.{cp_minor} or newer, target is "
                f"CPython {target[0]}.{target[1]}"
            )
        if value.startswith(("pp", "jy", "ip", "graalpy")):
            return (
                f"target is CPython {target[0]}.{target[1]}, but the wheel "
                f"is for another interpreter"
            )
        return f"interpreter {value!r} is not supported by the target"

    # -- ABI ---------------------------------------------------------------

    def explain_abi(self, value: str) -> str | None:
        if value in self.abis or value == "none":
            return None
        target = self.profile.python_version
        if value == "abi3":
            if self.profile.free_threaded:
                return "free-threaded targets cannot use abi3 wheels"
            return None
        if value == "abi3t":
            if not self.profile.free_threaded:
                return "abi3t wheels require a free-threaded interpreter"
            return None
        match = re.fullmatch(r"cp(\d)(\d+)([tdmu]*)", value)
        if match is not None:
            abi_major, abi_minor = int(match.group(1)), int(match.group(2))
            flags = match.group(3)
            details: list[str] = []
            if "t" in flags and not self.profile.free_threaded:
                details.append("wheel requires a free-threaded interpreter")
            if "d" in flags and not self.profile.debug:
                details.append("wheel requires a debug interpreter")
            if "t" not in flags and self.profile.free_threaded:
                details.append(
                    "target interpreter is free-threaded but the wheel is "
                    "not built for it"
                )
            if details:
                return "; ".join(details)
            if (abi_major, abi_minor) != target:
                return (
                    f"binary built for CPython {abi_major}.{abi_minor}; target "
                    f"is CPython {target[0]}.{target[1]} and its feature flags"
                )
            return None
        return f"ABI {value!r} is not supported by the target"

    # -- platform ----------------------------------------------------------

    def explain_platform(self, value: str) -> str | None:
        if value in self.platforms or value == "any":
            return None
        profile = self.profile

        if profile.os is PlatformOS.LINUX:
            return self._explain_linux_platform(value)
        if profile.os is PlatformOS.WINDOWS:
            return self._explain_windows_platform(value)
        if profile.os is not PlatformOS.MACOS:  # pragma: no cover
            return f"platform {value!r} is not supported by the target"
        return self._explain_macos_platform(value)

    def _explain_linux_platform(self, value: str) -> str | None:
        profile = self.profile
        target_archs = set(profile._linux_archs)

        parsed_glibc = _parse_glibc_platform(value)
        if parsed_glibc is not None:
            if profile.glibc_version is None:
                return f"target uses musl; {value} requires a glibc system"
            required, arch = parsed_glibc
            if arch not in target_archs:
                return f"wheel architecture {arch!r} does not match the target"
            if required > profile.glibc_version:
                return (
                    f"wheel requires glibc {required[0]}.{required[1]}, target "
                    f"provides {profile.glibc_version[0]}."
                    f"{profile.glibc_version[1]}"
                )
            return None

        parsed_musl = _parse_musl_platform(value)
        if parsed_musl is not None:
            if profile.musl_version is None:
                return f"target uses glibc; {value} requires a musl system"
            required, arch = parsed_musl
            if arch not in target_archs:
                return f"wheel architecture {arch!r} does not match the target"
            if required > profile.musl_version:
                return (
                    f"wheel requires musl {required[0]}.{required[1]}, target "
                    f"provides {profile.musl_version[0]}.{profile.musl_version[1]}"
                )
            return None

        if value.startswith("linux_"):
            arch = value.split("_", 1)[1]
            if arch not in target_archs:
                return f"wheel architecture {arch!r} does not match the target"
            return None

        if value.startswith(("macosx_", "win")):
            return f"platform {value!r} is not a Linux platform"
        return None

    def _explain_windows_platform(self, value: str) -> str | None:
        profile = self.profile
        target_tags = _WINDOWS_ARCHS[profile.arch]
        if value.startswith("win"):
            if value not in target_tags:
                return f"wheel is for {value!r}, target is {target_tags[0]!r}"
            return None
        return f"platform {value!r} is not a Windows platform"

    def _explain_macos_platform(self, value: str) -> str | None:
        profile = self.profile
        parsed = _parse_macos_platform(value)
        if parsed is None:
            if value.startswith("macosx_"):
                return f"unrecognized macOS platform tag {value!r}"
            return f"platform {value!r} is not a macOS platform"
        required, arch = parsed
        assert profile.macos_version is not None
        if arch != "universal2" and arch != profile.arch:
            return (
                f"wheel architecture {arch!r} does not match target "
                f"architecture {profile.arch!r}"
            )
        if required > profile.macos_version:
            return (
                f"wheel requires macOS {required[0]}.{required[1]}, target is "
                f"macOS {profile.macos_version[0]}.{profile.macos_version[1]}"
            )
        return None

    # -- whole tag ---------------------------------------------------------

    def explain_tag(self, tag: Tag) -> ComponentMismatch:
        # Callers only invoke this for tags outside the supported set.
        assert tag not in self.supported
        interpreter_reason = self.explain_interpreter(tag.interpreter)
        platform_reason = self.explain_platform(tag.platform)
        abi_reason = self.explain_abi(tag.abi)

        # An older-version cpX tag is usable through the stable ABI. Only
        # treat it as an interpreter mismatch if neither the platform nor the
        # ABI is the actual blocker.
        older_cp = False
        cp_match = re.fullmatch(r"cp(\d)(\d+)", tag.interpreter)
        if cp_match is not None:
            older_cp = (
                int(cp_match.group(1)),
                int(cp_match.group(2)),
            ) < self.profile.python_version

        if interpreter_reason is not None and (
            not older_cp or (platform_reason is None and abi_reason is None)
        ):
            return InterpreterComponentMismatch(tag.interpreter, interpreter_reason)
        if platform_reason is not None:
            return PlatformComponentMismatch(tag.platform, platform_reason)
        if abi_reason is not None:
            return ABIComponentMismatch(tag.abi, abi_reason)
        # The platform is not among the target's platform tags and is not
        # explained above (e.g. an exotic platform family).
        if platform_reason is None:
            platform_reason = (
                f"platform {tag.platform!r} is not supported by the target"
            )
        return PlatformComponentMismatch(tag.platform, platform_reason)


class TargetProfile:
    """An explicit, offline description of a target interpreter environment.

    A profile contains everything needed to generate the target's
    compatibility tags and to explain why individual wheels are or are not
    usable:

    - the interpreter version (``python_version``) and implementation
      (only CPython is supported in the first version);
    - the ABI features of the interpreter (debug and free-threaded builds,
      plus the legacy ``pymalloc``/``ucs4`` features for Python < 3.8);
    - the operating system (``os``) and CPU architecture (``arch``);
    - for Linux, the libc family (``glibc_version`` or ``musl_version``);
      for macOS, the system version.

    Nothing is ever inferred from the host: constructing a profile whose
    information is contradictory or outside the supported range raises
    :class:`InvalidTargetProfile`, while a Linux profile that does not
    specify its libc raises :class:`InsufficientTargetInformationError`.

    Convenience constructors :meth:`linux_glibc`, :meth:`linux_musl`,
    :meth:`windows`, and :meth:`macos` cover the supported targets::

        >>> profile = TargetProfile.linux_glibc(
        ...     python_version=(3, 12), arch="x86_64", glibc_version=(2, 35)
        ... )
        >>> str(profile.tags[0])
        'cp312-cp312-linux_x86_64'
    """

    def __init__(
        self,
        python_version: Sequence[int],
        os: PlatformOS,
        arch: str,
        *,
        debug: bool = False,
        free_threaded: bool = False,
        pymalloc: bool | None = None,
        ucs4: bool | None = None,
        glibc_version: Sequence[int] | None = None,
        musl_version: Sequence[int] | None = None,
        macos_version: Sequence[int] | None = None,
    ) -> None:
        pair = _as_version_pair(python_version, "python_version")
        if pair is None:
            raise InvalidTargetProfile(
                "python_version must be a (major, minor) pair"
            )
        self._python_version: tuple[int, int] = pair
        self._os = os
        self._arch = arch
        self._debug = debug
        self._free_threaded = free_threaded
        self._pymalloc = pymalloc
        self._ucs4 = ucs4

        glibc = _as_version_pair(glibc_version, "glibc_version")
        musl = _as_version_pair(musl_version, "musl_version")
        macos = _as_version_pair(macos_version, "macos_version")

        if free_threaded and pair < (3, 13):
            raise InvalidTargetProfile(
                "free-threaded builds are only supported on Python 3.13 or newer"
            )
        if pymalloc is not None and pair >= (3, 8):
            raise InvalidTargetProfile(
                "pymalloc is not part of the ABI tag on Python 3.8 or newer"
            )
        if ucs4 is not None and pair >= (3, 3):
            raise InvalidTargetProfile(
                "ucs4 is not part of the ABI tag on Python 3.3 or newer"
            )

        self._linux_archs: tuple[str, ...] = ()
        if os is PlatformOS.LINUX:
            if macos is not None:
                raise InvalidTargetProfile(
                    "macos_version is only valid for macOS targets"
                )
            if glibc is not None and musl is not None:
                raise InvalidTargetProfile(
                    "glibc_version and musl_version are mutually exclusive"
                )
            if glibc is None and musl is None:
                raise InsufficientTargetInformationError(
                    "a Linux target profile requires either glibc_version or "
                    "musl_version; refusing to guess the libc from the host"
                )
            self._linux_archs = _linux_archs(arch)
        else:
            if glibc is not None or musl is not None:
                raise InvalidTargetProfile(
                    "glibc_version and musl_version are only valid for Linux "
                    "targets"
                )
            if os is PlatformOS.WINDOWS:
                if macos is not None:
                    raise InvalidTargetProfile(
                        "macos_version is only valid for macOS targets"
                    )
                if arch not in _WINDOWS_ARCHS:
                    raise InvalidTargetProfile(
                        f"unsupported Windows architecture: {arch!r}"
                    )
            elif os is PlatformOS.MACOS:
                if macos is None:
                    raise InsufficientTargetInformationError(
                        "a macOS target profile requires macos_version"
                    )
                if arch not in _MACOS_ARCHS:
                    raise InvalidTargetProfile(
                        f"unsupported macOS architecture: {arch!r}"
                    )
            else:  # pragma: no cover - all PlatformOS members handled above
                raise InvalidTargetProfile(
                    f"unsupported operating system: {os!r}"
                )

        self._glibc_version = glibc
        self._musl_version = musl
        self._macos_version = macos

    @classmethod
    def linux_glibc(
        cls,
        python_version: Sequence[int],
        arch: str,
        glibc_version: Sequence[int],
        *,
        debug: bool = False,
        free_threaded: bool = False,
        pymalloc: bool | None = None,
        ucs4: bool | None = None,
    ) -> TargetProfile:
        """Create a profile for a glibc-based (manylinux) Linux target."""
        return cls(
            python_version,
            PlatformOS.LINUX,
            arch,
            debug=debug,
            free_threaded=free_threaded,
            pymalloc=pymalloc,
            ucs4=ucs4,
            glibc_version=glibc_version,
        )

    @classmethod
    def linux_musl(
        cls,
        python_version: Sequence[int],
        arch: str,
        musl_version: Sequence[int],
        *,
        debug: bool = False,
        free_threaded: bool = False,
        pymalloc: bool | None = None,
        ucs4: bool | None = None,
    ) -> TargetProfile:
        """Create a profile for a musl-based (musllinux) Linux target."""
        return cls(
            python_version,
            PlatformOS.LINUX,
            arch,
            debug=debug,
            free_threaded=free_threaded,
            pymalloc=pymalloc,
            ucs4=ucs4,
            musl_version=musl_version,
        )

    @classmethod
    def windows(
        cls,
        python_version: Sequence[int],
        arch: str,
        *,
        debug: bool = False,
        free_threaded: bool = False,
    ) -> TargetProfile:
        """Create a profile for a Windows target.

        ``arch`` is one of ``"x86_64"``/``"amd64"``, ``"x86"``/``"i386"``, or
        ``"arm64"``/``"aarch64"``.
        """
        return cls(
            python_version,
            PlatformOS.WINDOWS,
            arch,
            debug=debug,
            free_threaded=free_threaded,
        )

    @classmethod
    def macos(
        cls,
        python_version: Sequence[int],
        arch: str,
        macos_version: Sequence[int],
        *,
        debug: bool = False,
        free_threaded: bool = False,
    ) -> TargetProfile:
        """Create a profile for a macOS target.

        ``arch`` is ``"x86_64"`` or ``"arm64"``; ``universal2`` binaries are
        generated as additional platform tags for both architectures.
        """
        return cls(
            python_version,
            PlatformOS.MACOS,
            arch,
            debug=debug,
            free_threaded=free_threaded,
            macos_version=macos_version,
        )

    @property
    def python_version(self) -> tuple[int, int]:
        """The target interpreter's ``(major, minor)`` version."""
        return self._python_version

    @property
    def os(self) -> PlatformOS:
        """The target operating system family."""
        return self._os

    @property
    def arch(self) -> str:
        """The target CPU architecture in platform-tag spelling."""
        return self._arch

    @property
    def debug(self) -> bool:
        """Whether the target interpreter is a debug build."""
        return self._debug

    @property
    def free_threaded(self) -> bool:
        """Whether the target interpreter is a free-threaded build."""
        return self._free_threaded

    @property
    def glibc_version(self) -> tuple[int, int] | None:
        """The target glibc version, or ``None`` for non-glibc targets."""
        return self._glibc_version

    @property
    def musl_version(self) -> tuple[int, int] | None:
        """The target musl version, or ``None`` for non-musl targets."""
        return self._musl_version

    @property
    def macos_version(self) -> tuple[int, int] | None:
        """The target macOS version, or ``None`` for non-macOS targets."""
        return self._macos_version

    @functools.cached_property
    def abis(self) -> tuple[str, ...]:
        """The explicit CPython ABI tags of the target interpreter."""
        return _cpython_abis(
            self._python_version,
            debug=self._debug,
            free_threaded=self._free_threaded,
            pymalloc=self._pymalloc,
            ucs4=self._ucs4,
        )

    def _platform_tags(self) -> tuple[str, ...]:
        if self._os is PlatformOS.LINUX:
            archs = self._linux_archs
            tags = [f"linux_{arch}" for arch in archs]
            if self._glibc_version is not None:
                tags.extend(
                    _manylinux.platform_tags_for_glibc(
                        archs, self._glibc_version
                    )
                )
            else:
                assert self._musl_version is not None
                tags.extend(
                    _musllinux.platform_tags_for_musl(archs, self._musl_version)
                )
            return tuple(tags)

        if self._os is PlatformOS.WINDOWS:
            return _WINDOWS_ARCHS[self._arch]

        if self._os is not PlatformOS.MACOS:  # pragma: no cover
            raise InvalidTargetProfile(
                f"unsupported operating system: {self._os!r}"
            )
        assert self._macos_version is not None
        tags = tuple(
            mac_platforms(version=self._macos_version, arch=self._arch)
        )
        if not tags:
            raise InvalidTargetProfile(
                f"no macOS platform tags for version {self._macos_version} "
                f"and architecture {self._arch!r}"
            )
        return tags

    @functools.cached_property
    def tags(self) -> tuple[Tag, ...]:
        """The target's compatibility tags, highest priority first.

        Generated exclusively from the explicit profile values; the host
        interpreter, :mod:`sysconfig`, and the running system are never
        consulted. Duplicate tags (possible for compressed tag sets) are
        removed while preserving first-seen order.
        """
        platforms = self._platform_tags()
        tags: list[Tag] = list(
            cpython_tags(
                self._python_version, abis=self.abis, platforms=platforms
            )
        )
        interpreter = f"cp{self._python_version[0]}{self._python_version[1]}"
        tags.extend(
            compatible_tags(
                self._python_version,
                interpreter=interpreter,
                platforms=platforms,
            )
        )
        seen: set[Tag] = set()
        ordered: list[Tag] = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                ordered.append(tag)
        return tuple(ordered)

    def evaluate_wheel(
        self,
        filename: str,
        *,
        validate_order: bool = False,
        limit: int | None = None,
    ) -> WheelEvaluation:
        """Evaluate a wheel filename against this target profile.

        Only the filename is inspected; nothing is downloaded. The wheel tag
        set is parsed with :func:`packaging.utils.parse_wheel_filename`, so
        compressed tag sets are expanded and :class:`InvalidWheelFilename`
        propagates for malformed names.

        :param filename: The wheel filename, e.g.
            ``"foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl"``.
        :param validate_order: Validate compressed tag set ordering per
            PEP 425.
        :param limit: Maximum number of tags the wheel's compressed tag set
            may expand to.
        """
        name, version, build, wheel_tags = parse_wheel_filename(
            filename, validate_order=validate_order, limit=limit
        )
        compatibility = _Compatibility(self)

        matches = [tag for tag in wheel_tags if tag in compatibility.supported]
        if matches:
            best_tag = min(matches, key=lambda tag: compatibility.rank[tag])
            return WheelEvaluation(
                filename=filename,
                name=name,
                version=version,
                build=build,
                compatible=True,
                best_tag=best_tag,
                mismatches=(),
            )

        mismatches: dict[tuple[str, str, str], ComponentMismatch] = {}
        for tag in sorted(wheel_tags, key=lambda tag: str(tag)):
            mismatch = compatibility.explain_tag(tag)
            key = (type(mismatch).__name__, mismatch.value, mismatch.reason)
            mismatches.setdefault(key, mismatch)
        return WheelEvaluation(
            filename=filename,
            name=name,
            version=version,
            build=build,
            compatible=False,
            best_tag=None,
            mismatches=tuple(mismatches.values()),
        )

    def select_wheels(
        self,
        filenames: Iterable[str],
        *,
        validate_order: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        """Select and rank compatible wheel filenames for the target.

        Compatible wheels come first, ordered by their best matching tag
        (in :attr:`tags` priority), then by build tag (per PEP 427: wheels
        with a build tag precede wheels without, then numeric build number,
        then the remaining build string), then by filename. Incompatible
        filenames follow in their original input order, so the overall
        ordering is stable.

        Malformed filenames raise :class:`InvalidWheelFilename`.
        """
        compatibility = _Compatibility(self)
        compatible: list[tuple[int, tuple[int, int, str], str]] = []
        incompatible: list[str] = []
        for filename in filenames:
            evaluation = self.evaluate_wheel(
                filename, validate_order=validate_order, limit=limit
            )
            if evaluation.compatible:
                assert evaluation.best_tag is not None
                compatible.append(
                    (
                        compatibility.rank[evaluation.best_tag],
                        _build_key(evaluation.build),
                        filename,
                    )
                )
            else:
                incompatible.append(filename)
        compatible.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[2] for item in compatible] + incompatible

    def __repr__(self) -> str:
        return (
            f"TargetProfile(python_version={self._python_version!r}, "
            f"os={self._os!r}, arch={self._arch!r})"
        )
