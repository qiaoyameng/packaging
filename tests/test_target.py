# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError as dataclasses_FrozenInstanceError

import pytest

from packaging import target
from packaging._manylinux import _GLibCVersion
from packaging.target import (
    ABIComponentMismatch,
    InsufficientTargetInformationError,
    InterpreterComponentMismatch,
    InvalidTargetProfile,
    PlatformComponentMismatch,
    PlatformOS,
    TargetProfile,
    WheelEvaluation,
)
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename
from packaging.version import Version


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


class TestProfileConstruction:
    def test_linux_glibc_basic(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        assert profile.python_version == (3, 12)
        assert profile.os is PlatformOS.LINUX
        assert profile.arch == "x86_64"
        assert profile.glibc_version == (2, 35)
        assert profile.musl_version is None
        assert not profile.debug
        assert not profile.free_threaded

    def test_linux_musl_basic(self) -> None:
        profile = TargetProfile.linux_musl((3, 13), "aarch64", (1, 2))
        assert profile.musl_version == (1, 2)
        assert profile.glibc_version is None
        assert profile.macos_version is None

    def test_windows_basic(self) -> None:
        profile = TargetProfile.windows((3, 12), "amd64")
        assert profile.os is PlatformOS.WINDOWS
        assert profile.arch == "amd64"

    @pytest.mark.parametrize("arch", ["x86_64", "arm64"])
    def test_macos_basic(self, arch: str) -> None:
        profile = TargetProfile.macos((3, 12), arch, (14, 0))
        assert profile.os is PlatformOS.MACOS
        assert profile.macos_version == (14, 0)

    def test_direct_constructor(self) -> None:
        profile = TargetProfile(
            (3, 13),
            PlatformOS.LINUX,
            "x86_64",
            free_threaded=True,
            glibc_version=(2, 39),
        )
        assert profile.free_threaded
        assert profile.abis == ("cp313t",)

    def test_linux_without_libc_is_insufficient(self) -> None:
        with pytest.raises(InsufficientTargetInformationError, match="libc"):
            TargetProfile((3, 12), PlatformOS.LINUX, "x86_64")

    def test_macos_without_version_is_insufficient(self) -> None:
        with pytest.raises(InsufficientTargetInformationError, match="macOS"):
            TargetProfile((3, 12), PlatformOS.MACOS, "arm64")

    def test_both_libcs_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="mutually exclusive"):
            TargetProfile(
                (3, 12),
                PlatformOS.LINUX,
                "x86_64",
                glibc_version=(2, 35),
                musl_version=(1, 2),
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"glibc_version": (2, 35)},
            {"musl_version": (1, 2)},
        ],
    )
    def test_libc_on_non_linux_rejected(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(InvalidTargetProfile, match="Linux"):
            TargetProfile(
                (3, 12),
                PlatformOS.WINDOWS,
                "x86_64",
                **kwargs,  # type: ignore[arg-type]
            )

    def test_macos_version_on_linux_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="macOS"):
            TargetProfile(
                (3, 12),
                PlatformOS.LINUX,
                "x86_64",
                glibc_version=(2, 35),
                macos_version=(14, 0),
            )

    def test_macos_version_on_windows_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="macOS"):
            TargetProfile(
                (3, 12),
                PlatformOS.WINDOWS,
                "x86_64",
                macos_version=(14, 0),
            )

    def test_free_threaded_below_313_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="free-threaded"):
            TargetProfile.linux_glibc(
                (3, 12), "x86_64", (2, 35), free_threaded=True
            )

    def test_free_threaded_on_313_ok(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 13), "x86_64", (2, 39), free_threaded=True
        )
        assert profile.abis == ("cp313t",)

    def test_pymalloc_on_modern_python_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="pymalloc"):
            TargetProfile.linux_glibc(
                (3, 12), "x86_64", (2, 35), pymalloc=True
            )

    def test_ucs4_on_modern_python_rejected(self) -> None:
        with pytest.raises(InvalidTargetProfile, match="ucs4"):
            TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35), ucs4=True)

    @pytest.mark.parametrize("arch", ["mips64", "weird", "x86"])
    def test_unsupported_linux_arch(self, arch: str) -> None:
        with pytest.raises(InvalidTargetProfile, match="Linux architecture"):
            TargetProfile.linux_glibc((3, 12), arch, (2, 35))

    @pytest.mark.parametrize("arch", ["mips64", "ppc64le"])
    def test_unsupported_windows_arch(self, arch: str) -> None:
        with pytest.raises(InvalidTargetProfile, match="Windows architecture"):
            TargetProfile.windows((3, 12), arch)

    @pytest.mark.parametrize("arch", ["x86", "i386", "universal2"])
    def test_unsupported_macos_arch(self, arch: str) -> None:
        with pytest.raises(InvalidTargetProfile, match="macOS architecture"):
            TargetProfile.macos((3, 12), arch, (14, 0))

    @pytest.mark.parametrize("bad_version", [(), (3,), (3, 12, 1), ("3", "12")])
    def test_bad_python_version(self, bad_version: object) -> None:
        with pytest.raises(InvalidTargetProfile):
            TargetProfile.linux_glibc(
                bad_version,  # type: ignore[arg-type]
                "x86_64",
                (2, 35),
            )

    @pytest.mark.parametrize(
        ("factory", "version_kwarg"),
        [
            (TargetProfile.linux_glibc, "glibc_version"),
            (TargetProfile.linux_musl, "musl_version"),
        ],
    )
    def test_bad_libc_version_pair(
        self,
        factory: Callable[..., TargetProfile],
        version_kwarg: str,
    ) -> None:
        with pytest.raises(InvalidTargetProfile):
            factory((3, 12), "x86_64", **{version_kwarg: (1,)})

    def test_bad_macos_version_pair(self) -> None:
        with pytest.raises(InvalidTargetProfile):
            TargetProfile.macos((3, 12), "arm64", (14,))

    def test_bool_not_accepted_as_version_part(self) -> None:
        with pytest.raises(InvalidTargetProfile):
            TargetProfile.linux_glibc(
                (True, 12),  # type: ignore[arg-type]
                "x86_64",
                (2, 35),
            )

    def test_repr_and_str(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        assert repr(profile) == (
            "TargetProfile(python_version=(3, 12), os=<PlatformOS.LINUX: 'linux'>, "
            "arch='x86_64')"
        )
        assert str(PlatformOS.LINUX) == "linux"

    def test_debug_abis_modern(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 12), "x86_64", (2, 35), debug=True
        )
        assert profile.abis == ("cp312d", "cp312")

    def test_debug_abis_free_threaded(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 13), "x86_64", (2, 39), debug=True, free_threaded=True
        )
        assert profile.abis == ("cp313td", "cp313t")

    def test_legacy_pymalloc_ucs4_abis(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 7), "x86_64", (2, 17), pymalloc=True, ucs4=None
        )
        assert profile.abis == ("cp37m",)

        profile2 = TargetProfile.linux_glibc(
            (3, 2), "x86_64", (2, 17), pymalloc=False, ucs4=False
        )
        assert profile2.abis == ("cp32",)


# ---------------------------------------------------------------------------
# Tag generation
# ---------------------------------------------------------------------------


@pytest.fixture
def glibc_profile() -> TargetProfile:
    return TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))


class TestTagGeneration:
    def test_tags_are_tuple_and_cached(self, glibc_profile: TargetProfile) -> None:
        assert isinstance(glibc_profile.tags, tuple)
        assert glibc_profile.tags is glibc_profile.tags

    def test_linux_glibc_tag_order(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 17))
        tags = [str(tag) for tag in profile.tags]
        assert tags[0] == "cp312-cp312-linux_x86_64"
        assert tags[1] == "cp312-cp312-manylinux_2_17_x86_64"
        # Legacy aliases follow the canonical tag they pin.
        assert "cp312-cp312-manylinux2014_x86_64" in tags
        assert tags.index(
            "cp312-cp312-manylinux2014_x86_64"
        ) == tags.index("cp312-cp312-manylinux_2_17_x86_64") + 1
        # Newer-than-target glibc tags are absent.
        assert "cp312-cp312-manylinux_2_18_x86_64" not in tags
        # abi3 and none sections are present after the exact ABI.
        assert "cp312-abi3-manylinux_2_17_x86_64" in tags
        assert "cp312-none-manylinux_2_17_x86_64" in tags
        # Older-version abi3 tags descend to 3.2.
        assert "cp32-abi3-manylinux_2_17_x86_64" in tags
        assert "cp31-abi3-manylinux_2_17_x86_64" not in tags

    def test_linux_glibc_x86_includes_manylinux1(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 12))
        tags = {str(tag) for tag in profile.tags}
        assert "cp312-cp312-manylinux1_x86_64" in tags
        assert "cp312-cp312-manylinux2010_x86_64" in tags

    def test_linux_glibc_aarch64_floor_is_2_17(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "aarch64", (2, 31))
        tags = {str(tag) for tag in profile.tags}
        assert "cp312-cp312-manylinux2014_aarch64" in tags
        assert not any("manylinux_2_16_" in str(tag) for tag in tags)
        assert not any("manylinux1_" in str(tag) for tag in tags)

    def test_linux_musl_tag_order(self) -> None:
        profile = TargetProfile.linux_musl((3, 12), "aarch64", (1, 2))
        platforms = [
            tag.platform
            for tag in profile.tags
            if tag.interpreter == "cp312" and tag.abi == "cp312"
        ]
        assert platforms == [
            "linux_aarch64",
            "musllinux_1_2_aarch64",
            "musllinux_1_1_aarch64",
            "musllinux_1_0_aarch64",
        ]
        # A musl target never produces manylinux tags.
        assert not any(
            tag.platform.startswith("manylinux") for tag in profile.tags
        )

    def test_glibc_target_never_produces_musllinux(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        assert not any(
            tag.platform.startswith("musllinux") for tag in profile.tags
        )

    def test_armv8l_implies_armv7l(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "armv8l", (2, 35))
        platforms = [
            tag.platform
            for tag in profile.tags
            if tag.interpreter == "cp312" and tag.abi == "cp312"
        ]
        assert platforms[:2] == ["linux_armv8l", "linux_armv7l"]

    def test_windows_tags(self) -> None:
        profile = TargetProfile.windows((3, 12), "x86_64")
        assert Tag("cp312", "cp312", "win_amd64") in profile.tags
        assert Tag("cp312", "cp312", "win32") not in profile.tags
        x86 = TargetProfile.windows((3, 12), "x86")
        assert Tag("cp312", "cp312", "win32") in x86.tags
        arm = TargetProfile.windows((3, 12), "arm64")
        assert Tag("cp312", "cp312", "win_arm64") in arm.tags

    def test_macos_tags_include_universal2(self) -> None:
        profile = TargetProfile.macos((3, 12), "arm64", (14, 0))
        platforms = {
            tag.platform
            for tag in profile.tags
            if tag.interpreter == "cp312" and tag.abi == "cp312"
        }
        assert "macosx_14_0_arm64" in platforms
        assert "macosx_10_9_universal2" in platforms
        assert not any(p.endswith("x86_64") for p in platforms)

    def test_macos_x86_64_descends_to_10_4(self) -> None:
        profile = TargetProfile.macos((3, 12), "x86_64", (10, 15))
        platforms = {
            tag.platform
            for tag in profile.tags
            if tag.interpreter == "cp312" and tag.abi == "cp312"
        }
        assert "macosx_10_15_x86_64" in platforms
        assert "macosx_10_9_universal2" in platforms
        assert "macosx_11_0_x86_64" not in platforms

    def test_pure_python_tags_present(self, glibc_profile: TargetProfile) -> None:
        assert Tag("py312", "none", "any") in glibc_profile.tags
        assert Tag("py3", "none", "any") in glibc_profile.tags
        assert Tag("cp312", "none", "any") in glibc_profile.tags
        # And the platform-bearing generic tags.
        assert Tag("py312", "none", "linux_x86_64") in glibc_profile.tags

    def test_free_threaded_uses_abi3t_not_abi3(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 13), "x86_64", (2, 39), free_threaded=True
        )
        tags = set(profile.tags)
        assert Tag("cp313", "cp313t", "linux_x86_64") in tags
        assert Tag("cp313", "abi3t", "linux_x86_64") in tags
        assert Tag("cp313", "abi3", "linux_x86_64") not in tags
        assert Tag("cp312", "abi3t", "linux_x86_64") in tags
        assert Tag("cp312", "abi3", "linux_x86_64") not in tags

    def test_gil_abi3_range(self, glibc_profile: TargetProfile) -> None:
        tags = set(glibc_profile.tags)
        assert Tag("cp311", "abi3", "linux_x86_64") in tags
        assert Tag("cp313", "abi3t", "linux_x86_64") not in tags

    def test_tags_do_not_read_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*args: object, **kwargs: object) -> object:
            raise AssertionError("profile must not read host state")

        from packaging import tags as tags_module

        monkeypatch.setattr(tags_module.sysconfig, "get_platform", fail)
        monkeypatch.setattr(tags_module.sysconfig, "get_config_var", fail)
        monkeypatch.setattr(tags_module.platform, "system", fail)
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        assert profile.tags[0] == Tag("cp312", "cp312", "linux_x86_64")


# ---------------------------------------------------------------------------
# Wheel evaluation
# ---------------------------------------------------------------------------


class TestWheelEvaluation:
    def test_compatible_exact_wheel(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl"
        )
        assert result.compatible
        assert result.best_tag == Tag("cp312", "cp312", "manylinux_2_28_x86_64")
        assert result.mismatches == ()
        assert result.name == "foo"
        assert result.version == Version("1.0")
        assert result.build == ()
        assert result.explanation.startswith("compatible")

    def test_best_tag_uses_priority(self, glibc_profile: TargetProfile) -> None:
        # native linux_* outranks manylinux
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-linux_x86_64.whl"
        )
        assert result.best_tag == Tag("cp312", "cp312", "linux_x86_64")

        pure = glibc_profile.evaluate_wheel("foo-1.0-py3-none-any.whl")
        assert pure.compatible
        # Generic py3 outranks versioned py tags in sys_tags order, and
        # cp312-none-any comes later still.
        assert pure.best_tag == Tag("py3", "none", "any")

    def test_compressed_tag_set(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312.cp311-abi3-manylinux_2_17_x86_64.whl"
        )
        assert result.compatible
        assert result.best_tag == Tag("cp312", "abi3", "manylinux_2_17_x86_64")

        # If the set expands to only unsupported tags, the wheel is
        # rejected.
        result2 = glibc_profile.evaluate_wheel(
            "foo-1.0-cp310.cp313-cp310.cp313-manylinux_2_17_x86_64.whl"
        )
        assert not result2.compatible

    def test_compressed_tag_limit(self, glibc_profile: TargetProfile) -> None:
        with pytest.raises(InvalidWheelFilename, match="more than"):
            glibc_profile.evaluate_wheel(
                "foo-1.0-cp310.cp313-cp310.cp313-manylinux_2_17_x86_64.whl",
                limit=3,
            )

    def test_newer_interpreter(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"
        )
        assert not result.compatible
        assert result.best_tag is None
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, InterpreterComponentMismatch)
        assert mismatch.value == "cp313"
        assert "3.13" in mismatch.reason

    def test_older_cp_without_stable_abi(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp310-cp310-manylinux_2_28_x86_64.whl"
        )
        assert not result.compatible
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, ABIComponentMismatch)
        assert "CPython 3.10" in mismatch.reason

    def test_glibc_too_new(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-manylinux_2_39_x86_64.whl"
        )
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, PlatformComponentMismatch)
        assert "2.39" in mismatch.reason
        assert "2.35" in mismatch.reason

    def test_musl_wheel_on_glibc_target(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-musllinux_1_2_x86_64.whl"
        )
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, PlatformComponentMismatch)
        assert "musl" in mismatch.reason

    def test_glibc_wheel_on_musl_target(self) -> None:
        profile = TargetProfile.linux_musl((3, 12), "x86_64", (1, 2))
        result = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], PlatformComponentMismatch)
        assert "glibc" in result.mismatches[0].reason

    def test_musl_too_new(self) -> None:
        profile = TargetProfile.linux_musl((3, 12), "x86_64", (1, 2))
        result = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-musllinux_1_3_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], PlatformComponentMismatch)
        assert "musl 1.3" in result.mismatches[0].reason

    def test_architecture_mismatch(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-manylinux_2_17_aarch64.whl"
        )
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, PlatformComponentMismatch)
        assert "aarch64" in mismatch.reason

    def test_wrong_os(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-win_amd64.whl"
        )
        assert isinstance(result.mismatches[0], PlatformComponentMismatch)
        assert "Linux" in result.mismatches[0].reason

    def test_abi3_on_free_threaded_target(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 13), "x86_64", (2, 39), free_threaded=True
        )
        result = profile.evaluate_wheel(
            "foo-1.0-cp313-abi3-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], ABIComponentMismatch)
        assert "abi3" in result.mismatches[0].reason

    def test_abi3t_on_gil_target(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-abi3t-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], ABIComponentMismatch)
        assert "free-threaded" in result.mismatches[0].reason

    def test_abi3_compatible(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp37-abi3-manylinux_2_17_x86_64.whl"
        )
        assert result.compatible
        assert result.best_tag == Tag("cp37", "abi3", "manylinux_2_17_x86_64")

    def test_abi3t_compatible(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 15), "x86_64", (2, 39), free_threaded=True
        )
        result = profile.evaluate_wheel(
            "foo-1.0-cp310-abi3t-manylinux_2_17_x86_64.whl"
        )
        assert result.compatible
        assert result.best_tag == Tag(
            "cp310", "abi3t", "manylinux_2_17_x86_64"
        )

    def test_plain_cp_on_free_threaded_target(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 13), "x86_64", (2, 39), free_threaded=True
        )
        result = profile.evaluate_wheel(
            "foo-1.0-cp313-cp313-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], ABIComponentMismatch)
        assert "free-threaded" in result.mismatches[0].reason

    def test_free_threaded_wheel_on_gil_target(self) -> None:
        profile = TargetProfile.linux_glibc((3, 13), "x86_64", (2, 39))
        result = profile.evaluate_wheel(
            "foo-1.0-cp313-cp313t-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], ABIComponentMismatch)
        assert "free-threaded interpreter" in result.mismatches[0].reason

    def test_debug_wheel_on_release_target(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp312-cp312d-manylinux_2_17_x86_64.whl"
        )
        assert isinstance(result.mismatches[0], ABIComponentMismatch)
        assert "debug" in result.mismatches[0].reason

    def test_release_wheel_on_debug_target(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 12), "x86_64", (2, 35), debug=True
        )
        result = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        )
        assert result.compatible  # debug interpreters load normal modules

    def test_pypy_wheel_rejected(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-pp310-pypy310_pp73-manylinux_2_17_x86_64.whl"
        )
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, InterpreterComponentMismatch)
        assert "another interpreter" in mismatch.reason

    def test_python2_wheel_rejected(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel("foo-1.0-py2-none-any.whl")
        assert not result.compatible

    def test_windows_evaluation(self) -> None:
        profile = TargetProfile.windows((3, 12), "x86_64")
        assert profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-win_amd64.whl"
        ).compatible
        result = profile.evaluate_wheel("foo-1.0-cp312-cp312-win32.whl")
        assert not result.compatible
        assert isinstance(result.mismatches[0], PlatformComponentMismatch)

        arm = TargetProfile.windows((3, 12), "arm64")
        assert arm.evaluate_wheel(
            "foo-1.0-cp312-cp312-win_arm64.whl"
        ).compatible

    def test_macos_evaluation(self) -> None:
        profile = TargetProfile.macos((3, 12), "arm64", (14, 0))
        assert profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_14_0_arm64.whl"
        ).compatible
        universal = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_10_9_universal2.whl"
        )
        assert universal.compatible
        assert universal.best_tag == Tag(
            "cp312", "cp312", "macosx_10_9_universal2"
        )
        newer = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_15_0_arm64.whl"
        )
        assert not newer.compatible
        assert "macOS 15.0" in newer.mismatches[0].reason
        x86 = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_14_0_x86_64.whl"
        )
        assert not x86.compatible
        assert "architecture" in x86.mismatches[0].reason

    def test_macos_x86_64_universal2(self) -> None:
        profile = TargetProfile.macos((3, 12), "x86_64", (12, 6))
        result = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_10_9_universal2.whl"
        )
        assert result.compatible

    def test_mismatches_deduplicated(self, glibc_profile: TargetProfile) -> None:
        # A compressed set that expands into several tags failing for the
        # same component reason should not repeat explanations.
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp313.cp314-cp313.cp314-manylinux_2_17_x86_64.whl"
        )
        assert not result.compatible
        reasons = [m.reason for m in result.mismatches]
        assert len(reasons) == len(set(reasons))

    def test_evaluation_is_immutable_dataclass(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.evaluate_wheel("foo-1.0-py3-none-any.whl")
        assert isinstance(result, WheelEvaluation)
        with pytest.raises(dataclasses_FrozenInstanceError):
            result.compatible = False  # type: ignore[misc]

    def test_invalid_filename(self, glibc_profile: TargetProfile) -> None:
        with pytest.raises(InvalidWheelFilename):
            glibc_profile.evaluate_wheel("not-a-wheel.whl")

    def test_mismatch_strings(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel(
            "foo-1.0-cp313-cp313-manylinux_2_39_x86_64.whl"
        )
        text = result.explanation
        assert text.startswith("not compatible")
        assert "cp313" in text


# ---------------------------------------------------------------------------
# Wheel selection / ordering
# ---------------------------------------------------------------------------


class TestSelectWheels:
    def test_compatible_ordered_by_tag_priority(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.select_wheels(
            [
                "foo-1.0-py3-none-any.whl",
                "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl",
                "foo-1.0-cp312-cp312-linux_x86_64.whl",
            ]
        )
        assert result == [
            "foo-1.0-cp312-cp312-linux_x86_64.whl",
            "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl",
            "foo-1.0-py3-none-any.whl",
        ]

    def test_incompatible_follow_in_input_order(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        bad_a = "foo-1.0-cp312-cp312-win_amd64.whl"
        bad_b = "foo-1.0-cp313-cp313-manylinux_2_17_x86_64.whl"
        good = "foo-1.0-cp312-cp312-linux_x86_64.whl"
        result = profile.select_wheels([bad_a, good, bad_b])
        assert result == [good, bad_a, bad_b]

    def test_build_tag_ordering(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        # All share the same best tag; build tags sort per PEP 427.
        names = [
            "foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-2-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-10-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-1-cp312-cp312-manylinux_2_17_x86_64.whl",
        ]
        result = profile.select_wheels(names)
        assert result == [
            "foo-1.0-1-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-2-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-10-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        ]

    def test_build_tag_numeric_then_string(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        names = [
            "foo-1.0-1b-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-1a-cp312-cp312-manylinux_2_17_x86_64.whl",
        ]
        result = profile.select_wheels(names)
        assert result == [
            "foo-1.0-1a-cp312-cp312-manylinux_2_17_x86_64.whl",
            "foo-1.0-1b-cp312-cp312-manylinux_2_17_x86_64.whl",
        ]

    def test_stable_for_identical_rank(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        # Different project names, identical best tag rank; filename breaks
        # ties deterministically.
        names = [
            "zoo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
            "aaa-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        ]
        result = profile.select_wheels(names)
        assert result[0].startswith("aaa")
        assert result[1].startswith("zoo")

    def test_empty_input(self, glibc_profile: TargetProfile) -> None:
        assert glibc_profile.select_wheels(()) == []

    def test_returns_list(self, glibc_profile: TargetProfile) -> None:
        assert isinstance(
            glibc_profile.select_wheels(["foo-1.0-py3-none-any.whl"]),
            list,
        )


# ---------------------------------------------------------------------------
# Pure libc platform-tag helpers
# ---------------------------------------------------------------------------


class TestPureLibcHelpers:
    def test_manylinux_pure_glibc(self) -> None:
        tags = list(
            target._manylinux.platform_tags_for_glibc(  # type: ignore[attr-defined]
                ["x86_64"], (2, 17)
            )
        )
        assert tags[0] == "manylinux_2_17_x86_64"
        assert tags[1] == "manylinux2014_x86_64"
        assert "manylinux1_x86_64" in tags
        assert "manylinux_2_18_x86_64" not in tags

    def test_manylinux_pure_floor_non_x86(self) -> None:
        tags = list(
            target._manylinux.platform_tags_for_glibc(  # type: ignore[attr-defined]
                ["aarch64"], (2, 31)
            )
        )
        assert tags[0] == "manylinux_2_31_aarch64"
        assert tags[-1] == "manylinux2014_aarch64"
        assert "manylinux1_aarch64" not in tags

    def test_manylinux_pure_custom_compatible(self) -> None:
        # Only 2.17 is "compatible" per the predicate, like a manylinux2014
        # container.
        tags = list(
            target._manylinux.platform_tags_for_glibc(  # type: ignore[attr-defined]
                ["x86_64"],
                (2, 20),
                compatible=lambda _arch, v: v == _GLibCVersion(2, 17),
            )
        )
        assert tags == ["manylinux_2_17_x86_64", "manylinux2014_x86_64"]

    def test_musl_pure(self) -> None:
        tags = list(
            target._musllinux.platform_tags_for_musl(  # type: ignore[attr-defined]
                ["x86_64"], (1, 2)
            )
        )
        assert tags == [
            "musllinux_1_2_x86_64",
            "musllinux_1_1_x86_64",
            "musllinux_1_0_x86_64",
        ]

    def test_musl_pure_multiple_archs(self) -> None:
        tags = list(
            target._musllinux.platform_tags_for_musl(  # type: ignore[attr-defined]
                ["armv8l", "armv7l"], (1, 1)
            )
        )
        assert tags == [
            "musllinux_1_1_armv8l",
            "musllinux_1_0_armv8l",
            "musllinux_1_1_armv7l",
            "musllinux_1_0_armv7l",
        ]


# ---------------------------------------------------------------------------
# Edge cases for full branch coverage
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_pymalloc_false_and_ucs4_true_python32(self) -> None:
        # ucs4=True is explicitly allowed on 3.2; pymalloc=False drops "m".
        profile = TargetProfile.linux_glibc(
            (3, 2), "x86_64", (2, 17), pymalloc=False, ucs4=True
        )
        assert profile.abis == ("cp32u",)

    def test_pymalloc_none_ucs4_none_python32_defaults(self) -> None:
        profile = TargetProfile.linux_glibc((3, 2), "x86_64", (2, 17))
        assert profile.abis == ("cp32mu",)

    def test_debug_legacy_includes_pymalloc(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 7), "x86_64", (2, 17), debug=True
        )
        assert profile.abis == ("cp37dm",)

    def test_debug_python38_emits_plain_abi(self) -> None:
        profile = TargetProfile.linux_glibc(
            (3, 8), "x86_64", (2, 17), debug=True
        )
        assert profile.abis == ("cp38d", "cp38")

    def test_python31_has_no_abi3(self) -> None:
        profile = TargetProfile.linux_glibc((3, 1), "x86_64", (2, 17))
        tags = set(profile.tags)
        assert not any(tag.abi in {"abi3", "abi3t"} for tag in tags)

    def test_universal2_wheel_newer_macos_version(self) -> None:
        # Platform-version mismatch reported through the universal2 path.
        profile = TargetProfile.macos((3, 12), "arm64", (10, 15))
        result = profile.evaluate_wheel(
            "foo-1.0-cp312-cp312-macosx_11_0_universal2.whl"
        )
        assert not result.compatible
        assert isinstance(result.mismatches[0], PlatformComponentMismatch)
        assert "macOS 11.0" in result.mismatches[0].reason

    def test_malformed_macos_tag(self) -> None:
        profile = TargetProfile.macos((3, 12), "arm64", (14, 0))
        result = profile.evaluate_wheel(
            "foo-1.0-py3-none-macosx_bogus_tag.whl"
        )
        assert not result.compatible
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, PlatformComponentMismatch)
        assert "unrecognized" in mismatch.reason

    def test_exotic_platform_falls_back(self) -> None:
        # An unknown platform family: on Linux the per-family explainer has
        # nothing specific to say, so the generic fallback is used; on
        # Windows it is reported as the wrong family.
        linux = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = linux.evaluate_wheel("foo-1.0-py3-none-haiku1.whl")
        assert not result.compatible
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, PlatformComponentMismatch)
        assert mismatch.value == "haiku1"
        assert "not supported" in mismatch.reason

        windows = TargetProfile.windows((3, 12), "x86_64")
        result_win = windows.evaluate_wheel("foo-1.0-py3-none-haiku1.whl")
        assert "not a Windows platform" in result_win.mismatches[0].reason

    def test_native_linux_arch_mismatch(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.evaluate_wheel("foo-1.0-py3-none-linux_aarch64.whl")
        assert not result.compatible
        assert "aarch64" in result.mismatches[0].reason

    def test_abi3t_returned_none_when_platform_blocks(self) -> None:
        # abi3t itself is acceptable to a free-threaded target, but the
        # platform (Windows) blocks a Linux wheel; ensure no ABI mismatch is
        # emitted.
        profile = TargetProfile.windows((3, 14), "x86_64", free_threaded=True)
        result = profile.evaluate_wheel(
            "foo-1.0-cp313-abi3t-manylinux_2_17_x86_64.whl"
        )
        assert all(
            isinstance(m, PlatformComponentMismatch) for m in result.mismatches
        )

    def test_abi_flags_m_and_u_on_legacy_wheel(self) -> None:
        profile = TargetProfile.linux_glibc((3, 7), "x86_64", (2, 17))
        assert profile.evaluate_wheel(
            "foo-1.0-cp37-cp37m-manylinux_2_17_x86_64.whl"
        ).compatible
        result = profile.evaluate_wheel(
            "foo-1.0-cp37-cp37mu-manylinux_2_17_x86_64.whl"
        )
        assert not result.compatible  # ucs4 flag not present on a 3.7 build

    def test_unknown_interpreter_token(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.evaluate_wheel("foo-1.0-ruby3-none-any.whl")
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, InterpreterComponentMismatch)
        assert "ruby3" in mismatch.reason

    @pytest.mark.parametrize(
        "interpreter",
        ["cp3", "cp2", "py4", "py"],
    )
    def test_partial_versioned_interpreters(self, interpreter: str) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.evaluate_wheel(
            f"foo-1.0-{interpreter}-{interpreter}-any.whl"
        )
        assert not result.compatible
        mismatch = result.mismatches[0]
        assert isinstance(mismatch, InterpreterComponentMismatch)

    def test_python2_versioned_tag_rejected(
        self, glibc_profile: TargetProfile
    ) -> None:
        result = glibc_profile.evaluate_wheel("foo-1.0-py27-none-any.whl")
        assert not result.compatible
        assert isinstance(result.mismatches[0], InterpreterComponentMismatch)
        assert "Python 2.7" in result.mismatches[0].reason

    def test_jython_tag_rejected(self, glibc_profile: TargetProfile) -> None:
        result = glibc_profile.evaluate_wheel("foo-1.0-jy312-jy312-any.whl")
        assert not result.compatible
        assert isinstance(result.mismatches[0], InterpreterComponentMismatch)
        assert "another interpreter" in result.mismatches[0].reason

    def test_empty_mismatches_explanation_unreachable_in_practice(
        self, glibc_profile: TargetProfile
    ) -> None:
        evaluation = WheelEvaluation(
            filename="x.whl",
            name="x",
            version=Version("1"),
            build=(),
            compatible=False,
            best_tag=None,
            mismatches=(),
        )
        assert evaluation.explanation == "not compatible with the target"

    def test_dir_exports(self) -> None:
        assert "TargetProfile" in target.__dir__()
        assert set(target.__all__).issubset(set(dir(target)))

    def test_musl_arch_mismatch_native_tag(self) -> None:
        profile = TargetProfile.linux_musl((3, 12), "x86_64", (1, 2))
        result = profile.evaluate_wheel("foo-1.0-py3-none-linux_aarch64.whl")
        assert not result.compatible

    def test_manylinux_2_x_parsed_unknown_arch(self) -> None:
        profile = TargetProfile.linux_glibc((3, 12), "x86_64", (2, 35))
        result = profile.evaluate_wheel(
            "foo-1.0-py3-none-manylinux_2_17_mips64.whl"
        )
        assert not result.compatible
        assert "mips64" in result.mismatches[0].reason

    def test_musllinux_arch_mismatch(self) -> None:
        profile = TargetProfile.linux_musl((3, 12), "x86_64", (1, 2))
        result = profile.evaluate_wheel(
            "foo-1.0-py3-none-musllinux_1_1_aarch64.whl"
        )
        assert not result.compatible
        assert "aarch64" in result.mismatches[0].reason

    def test_properties_for_all_os(self) -> None:
        mac = TargetProfile.macos((3, 12), "arm64", (14, 0))
        assert mac.glibc_version is None
        assert mac.musl_version is None
        assert mac.macos_version == (14, 0)
        win = TargetProfile.windows((3, 12), "x86_64", debug=True)
        assert win.debug
        assert win.macos_version is None

    def test_validate_order_propagates(self, glibc_profile: TargetProfile) -> None:
        with pytest.raises(InvalidWheelFilename):
            glibc_profile.evaluate_wheel(
                "foo-1.0-py312.py3-none-any.whl", validate_order=True
            )
