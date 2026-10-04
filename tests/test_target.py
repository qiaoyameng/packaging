# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import annotations

import sysconfig

import pytest

from packaging.target import (
    IncompleteProfileError,
    InsufficientInformation,
    MacOSProfile,
    ManylinuxProfile,
    MusllinuxProfile,
    TargetProfile,
    WheelMatch,
    WheelRejection,
    WindowsProfile,
    evaluate_wheel,
    rank_wheels,
)
from packaging.utils import InvalidWheelFilename


LINUX = ManylinuxProfile("x86_64", (2, 28))
MUSL = MusllinuxProfile("x86_64", (1, 2))
WIN = WindowsProfile("AMD64")
MAC = MacOSProfile((13, 0), "arm64")


class TestProfileValidation:
    def test_bad_python_version(self) -> None:
        with pytest.raises(ValueError, match="python_version"):
            TargetProfile((3,))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="python_version"):
            TargetProfile(("3", "13"))  # type: ignore[arg-type]

    def test_threaded_requires_313(self) -> None:
        with pytest.raises(ValueError, match="Free-threaded"):
            TargetProfile((3, 12), LINUX, threaded=True)
        with pytest.raises(ValueError, match="python_version"):
            TargetProfile(None, LINUX, threaded=True)

    def test_bad_libc_version(self) -> None:
        with pytest.raises(ValueError, match="glibc"):
            ManylinuxProfile("x86_64", (2,))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="musl"):
            MusllinuxProfile("x86_64", (0, 2))  # musl major 0 is invalid
        with pytest.raises(ValueError, match="musl"):
            MusllinuxProfile("x86_64", (1, -1))

    def test_bad_architectures(self) -> None:
        with pytest.raises(ValueError, match="architecture"):
            ManylinuxProfile("mips64", (2, 28))
        with pytest.raises(ValueError, match="architecture"):
            MusllinuxProfile("mips64", (1, 2))
        with pytest.raises(ValueError, match="Windows architecture"):
            WindowsProfile("itanium")
        with pytest.raises(ValueError, match="macOS architecture"):
            MacOSProfile((13, 0), "m68k")
        with pytest.raises(ValueError, match="macOS version"):
            MacOSProfile((13,), "arm64")  # type: ignore[arg-type]

    def test_windows_aliases(self) -> None:
        assert WindowsProfile("aarch64").platform_tags() == ["win_arm64"]
        assert WindowsProfile("x86").platform_tags() == ["win32"]
        assert WindowsProfile("AMD64").platform_tags() == ["win_amd64"]


class TestIncompleteProfiles:
    def test_completely_empty(self) -> None:
        profile = TargetProfile()
        assert not profile.is_complete
        missing = profile.missing_information()
        assert "CPython interpreter version" in missing
        assert "target platform (OS and architecture)" in missing
        with pytest.raises(IncompleteProfileError):
            profile.tags()

    def test_partial_platform(self) -> None:
        profile = TargetProfile((3, 13), ManylinuxProfile(arch=None, glibc=(2, 28)))
        assert not profile.is_complete
        assert profile.missing_information() == ["manylinux target architecture"]

        profile = TargetProfile((3, 13), ManylinuxProfile(arch="x86_64", glibc=None))
        assert profile.missing_information() == ["manylinux target glibc version"]

        profile = TargetProfile((3, 13), MusllinuxProfile("x86_64", None))
        assert profile.missing_information() == ["musllinux target musl version"]

        profile = TargetProfile((3, 13), WindowsProfile(None))
        assert profile.missing_information() == ["Windows target architecture"]

        profile = TargetProfile((3, 13), MacOSProfile(None, None))
        assert profile.missing_information() == [
            "macOS target version",
            "macOS target architecture",
        ]

    def test_evaluate_returns_insufficient(self) -> None:
        result = evaluate_wheel(TargetProfile(), "foo-1.0-py3-none-any.whl")
        assert isinstance(result, InsufficientInformation)
        assert result.missing
        assert result.filename.endswith(".whl")

    def test_never_guesses_platform(self) -> None:
        # Even with a fully known interpreter, an unknown platform means binary
        # wheels cannot be judged; no host platform is substituted.
        profile = TargetProfile((3, 13))
        assert not profile.is_complete
        result = evaluate_wheel(
            profile, "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"
        )
        assert isinstance(result, InsufficientInformation)
        assert any("platform" in item for item in result.missing)


class TestTagGeneration:
    def test_manylinux_order_and_legacy_tags(self) -> None:
        profile = TargetProfile((3, 12), ManylinuxProfile("x86_64", (2, 20)))
        tags = list(map(str, profile.tags()))
        # Native tag first, then manylinux from the target glibc downward.
        assert tags[0] == "cp312-cp312-linux_x86_64"
        assert "cp312-cp312-manylinux_2_20_x86_64" in tags
        assert "cp312-cp312-manylinux1_x86_64" in tags
        assert "cp312-cp312-manylinux2010_x86_64" in tags
        assert "cp312-cp312-manylinux2014_x86_64" in tags
        # Tags newer than the target glibc are absent.
        assert "cp312-cp312-manylinux_2_21_x86_64" not in tags

    def test_manylinux_aarch64_floor(self) -> None:
        profile = TargetProfile((3, 12), ManylinuxProfile("aarch64", (2, 17)))
        tags = set(map(str, profile.tags()))
        assert "cp312-cp312-manylinux_2_17_aarch64" in tags
        assert "cp312-cp312-manylinux2014_aarch64" in tags
        assert "cp312-cp312-manylinux_2_16_aarch64" not in tags

    def test_musllinux_tags(self) -> None:
        profile = TargetProfile((3, 12), MusllinuxProfile("x86_64", (1, 2)))
        tags = list(map(str, profile.tags()))
        assert tags[:4] == [
            "cp312-cp312-linux_x86_64",
            "cp312-cp312-musllinux_1_2_x86_64",
            "cp312-cp312-musllinux_1_1_x86_64",
            "cp312-cp312-musllinux_1_0_x86_64",
        ]
        assert not any("manylinux" in tag for tag in tags)

    def test_windows_tags(self) -> None:
        tags = list(map(str, TargetProfile((3, 11), WIN).tags()))
        assert tags[0] == "cp311-cp311-win_amd64"
        assert "cp311-abi3-win_amd64" in tags
        assert "cp311-none-win_amd64" in tags
        assert "cp310-abi3-win_amd64" in tags
        assert "py3-none-any" in tags

    def test_macos_universal2_arm64(self) -> None:
        profile = TargetProfile((3, 13), MacOSProfile((14, 0), "arm64"))
        tags = set(map(str, profile.tags()))
        assert "cp313-cp313-macosx_14_0_arm64" in tags
        assert "cp313-cp313-macosx_14_0_universal2" in tags
        # arm64 cannot use pre-11.0 arch-specific binaries, only universal2.
        assert "cp313-cp313-macosx_10_9_arm64" not in tags
        assert "cp313-cp313-macosx_10_9_universal2" in tags
        assert "cp313-cp313-macosx_10_9_x86_64" not in tags

    def test_macos_x86_64_back_compat(self) -> None:
        profile = TargetProfile((3, 11), MacOSProfile((12, 0), "x86_64"))
        tags = set(map(str, profile.tags()))
        assert "cp311-cp311-macosx_12_0_x86_64" in tags
        assert "cp311-cp311-macosx_10_9_x86_64" in tags
        assert "cp311-cp311-macosx_10_9_universal2" in tags
        assert "cp311-cp311-macosx_10_9_intel" in tags

    def test_free_threaded_abis(self) -> None:
        profile = TargetProfile((3, 13), LINUX, threaded=True)
        tags = profile.tags()
        assert tags[0].interpreter == "cp313"
        assert tags[0].abi == "cp313t"
        assert "abi3t" in {tag.abi for tag in tags}
        # Free-threaded builds do not support abi3.
        assert "abi3" not in {tag.abi for tag in tags}

    def test_abi3_back_version_range(self) -> None:
        tags = set(map(str, TargetProfile((3, 9), LINUX).tags()))
        assert "cp39-abi3-manylinux_2_28_x86_64" in tags
        assert "cp38-abi3-manylinux_2_28_x86_64" in tags
        assert "cp32-abi3-manylinux_2_28_x86_64" in tags
        assert "cp31-abi3-manylinux_2_28_x86_64" not in tags

    def test_legacy_pymalloc_and_ucs4(self) -> None:
        profile = TargetProfile(
            (3, 7), LINUX, pymalloc=True, wide_unicode=True
        )
        assert profile.tags()[0].abi == "cp37m"

        profile = TargetProfile((3, 2), LINUX, wide_unicode=True)
        assert profile.tags()[0].abi == "cp32mu"

        profile = TargetProfile((3, 7), LINUX, pymalloc=False)
        assert profile.tags()[0].abi == "cp37"

    def test_debug_build_abis(self) -> None:
        tags = TargetProfile((3, 13), LINUX, debug=True).tags()
        abis = []
        for tag in tags:
            if tag.abi not in abis:
                abis.append(tag.abi)
        assert abis[:2] == ["cp313d", "cp313"]

    def test_armv8l_uses_armv7l_fallback(self) -> None:
        profile = TargetProfile((3, 12), ManylinuxProfile("armv8l", (2, 27)))
        tags = set(map(str, profile.tags()))
        assert "cp312-cp312-linux_armv8l" in tags
        assert "cp312-cp312-linux_armv7l" in tags
        assert "cp312-cp312-manylinux_2_17_armv7l" in tags


class TestEvaluateWheel:
    def test_exact_match(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX),
            "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelMatch)
        assert str(result.best_tag) == "cp313-cp313-manylinux_2_28_x86_64"

    def test_native_tag_preferred_over_manylinux(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX),
            "foo-1.0-cp313-cp313-linux_x86_64.whl",
        )
        assert isinstance(result, WheelMatch)
        assert result.best_tag.platform == "linux_x86_64"
        assert result.rank == 0

    def test_older_glibc_wheel_compatible(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), ManylinuxProfile("x86_64", (2, 31))),
            "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelMatch)

    def test_newer_glibc_wheel_rejected_with_platform_reason(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), ManylinuxProfile("x86_64", (2, 28))),
            "foo-1.0-cp313-cp313-manylinux_2_31_x86_64.whl",
        )
        assert isinstance(result, WheelRejection)
        components = {m.component for m in result.mismatches}
        assert components == {"platform"}
        assert "manylinux_2_31_x86_64" in result.detail

    def test_wrong_interpreter_version(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX),
            "foo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelRejection)
        # cp312 remains a supported interpreter via the back-version abi3 tag
        # range, so only the ABI itself is incompatible.
        assert [m.component for m in result.mismatches] == ["abi"]
        assert "cp312" in result.detail

    def test_musl_wheel_on_glibc_target(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 12), LINUX),
            "foo-1.0-cp312-cp312-musllinux_1_2_x86_64.whl",
        )
        assert isinstance(result, WheelRejection)
        assert result.mismatches[0].component == "platform"

    def test_glibc_wheel_on_musl_target(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 12), MUSL),
            "foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl",
        )
        assert isinstance(result, WheelRejection)
        assert result.mismatches[0].component == "platform"

    def test_abi3_wheel(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX),
            "foo-1.0-cp313-abi3-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelMatch)
        assert result.best_tag.abi == "abi3"

    def test_abi3t_wheel_only_on_threaded(self) -> None:
        profile_threaded = TargetProfile((3, 13), LINUX, threaded=True)
        result = evaluate_wheel(
            profile_threaded,
            "foo-1.0-cp313-abi3t-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelMatch)

        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX),
            "foo-1.0-cp313-abi3t-manylinux_2_28_x86_64.whl",
        )
        assert isinstance(result, WheelRejection)
        assert any(m.component == "abi" for m in result.mismatches)

    def test_pure_python_wheel(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), LINUX), "foo-1.0-py3-none-any.whl"
        )
        assert isinstance(result, WheelMatch)
        assert str(result.best_tag) == "py3-none-any"

        result = evaluate_wheel(
            TargetProfile((2, 7), LINUX), "foo-1.0-py3-none-any.whl"
        )
        assert isinstance(result, WheelRejection)
        assert result.mismatches[0].component == "interpreter"

    def test_compressed_tag_set(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), WIN),
            "foo-1.0-cp312.cp313-cp312.cp313-win_amd64.whl",
        )
        assert isinstance(result, WheelMatch)
        assert result.best_tag.interpreter == "cp313"

    def test_windows_mismatch(self) -> None:
        result = evaluate_wheel(
            TargetProfile((3, 13), WIN),
            "foo-1.0-cp313-cp313-win32.whl",
        )
        assert isinstance(result, WheelRejection)
        assert result.mismatches[0].component == "platform"

    def test_invalid_filename_raises(self) -> None:
        with pytest.raises(InvalidWheelFilename):
            evaluate_wheel(TargetProfile((3, 13), LINUX), "not-a-wheel.whl")


class TestRankWheels:
    def test_tag_rank_ordering(self) -> None:
        profile = TargetProfile((3, 13), LINUX)
        filenames = [
            "foo-1.0-py3-none-any.whl",
            "foo-1.0-cp313-abi3-manylinux_2_28_x86_64.whl",
            "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
            "foo-1.0-cp313-cp313-linux_x86_64.whl",
        ]
        ranking = rank_wheels(profile, filenames)
        available = [r for r in ranking if r.available]
        assert [r.match.best_tag.platform for r in available] == [
            "linux_x86_64",
            "manylinux_2_28_x86_64",
            "manylinux_2_28_x86_64",
            "any",
        ]
        assert [r.match.best_tag.abi for r in available] == [
            "cp313",
            "cp313",
            "abi3",
            "none",
        ]

    def test_build_tag_breaks_ties(self) -> None:
        profile = TargetProfile((3, 13), LINUX)
        plain = "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"
        build1 = "foo-1.0-1-cp313-cp313-manylinux_2_28_x86_64.whl"
        build2 = "foo-1.0-2-cp313-cp313-manylinux_2_28_x86_64.whl"
        build2a = "foo-1.0-2a-cp313-cp313-manylinux_2_28_x86_64.whl"

        ranking = rank_wheels(profile, [plain, build1, build2, build2a])
        chosen = [r.match.filename for r in ranking if r.available]
        # Higher build number wins; suffix sorts ascending so "2" < "2a" means
        # "2a" is the newer build; plain tag sorts last among the tag-equal set.
        assert chosen == [build2a, build2, build1, plain]

    def test_stable_tie_breaking(self) -> None:
        profile = TargetProfile((3, 13), LINUX)
        filenames = [
            "a-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
            "b-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
            "c-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        ]
        ranking = rank_wheels(profile, filenames)
        assert [r.index for r in ranking] == [0, 1, 2]
        # Equal tags tie-break on input position, not the project name.
        ranking = rank_wheels(profile, list(reversed(filenames)))
        assert [r.index for r in ranking] == [0, 1, 2]

    def test_build_tag_does_not_cross_tag_rank(self) -> None:
        profile = TargetProfile((3, 13), LINUX)
        built_native = "foo-1.0-9-cp313-cp313-linux_x86_64.whl"
        plain_manylinux = "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl"
        # A higher build tag on a worse-ranked platform must not win.
        ranking = rank_wheels(profile, [built_native, plain_manylinux])
        assert [r.match.best_tag.platform for r in ranking] == [
            "linux_x86_64",
            "manylinux_2_28_x86_64",
        ]

    def test_rejections_keep_input_order_after_matches(self) -> None:
        profile = TargetProfile((3, 13), LINUX)
        filenames = [
            "bad1-1.0-cp310-cp310-win_amd64.whl",
            "good-1.0-cp313-cp313-linux_x86_64.whl",
            "bad2-1.0-cp39-cp39-manylinux_2_28_x86_64.whl",
        ]
        ranking = rank_wheels(profile, filenames)
        assert ranking[0].index == 1
        assert ranking[0].available
        assert [r.index for r in ranking[1:]] == [0, 2]
        assert all(isinstance(r.rejection, WheelRejection) for r in ranking[1:])

    def test_insufficient_profile_ranking(self) -> None:
        filenames = [
            "a-1.0-py3-none-any.whl",
            "b-1.0-cp313-cp313-linux_x86_64.whl",
        ]
        ranking = rank_wheels(TargetProfile(), filenames)
        assert [r.index for r in ranking] == [0, 1]
        assert all(isinstance(r.insufficient, InsufficientInformation) for r in ranking)

    def test_best_manylinux_version_prefers_newest(self) -> None:
        profile = TargetProfile((3, 13), ManylinuxProfile("x86_64", (2, 35)))
        filenames = [
            "foo-1.0-cp313-cp313-manylinux_2_17_x86_64.whl",
            "foo-1.0-cp313-cp313-manylinux_2_28_x86_64.whl",
        ]
        ranking = rank_wheels(profile, filenames)
        best = ranking[0]
        assert best.match is not None
        assert best.match.best_tag.platform == "manylinux_2_28_x86_64"


class TestNoHostInspection:
    def test_tags_never_read_sysconfig(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(name: str) -> object:  # noqa: ARG001
            raise AssertionError("TargetProfile must not consult sysconfig")

        monkeypatch.setattr(sysconfig, "get_config_var", fail)
        monkeypatch.setattr(sysconfig, "get_platform", fail)

        profiles = [
            TargetProfile((3, 13), ManylinuxProfile("x86_64", (2, 28))),
            TargetProfile((3, 12), MusllinuxProfile("aarch64", (1, 2))),
            TargetProfile((3, 11), WindowsProfile("AMD64")),
            TargetProfile((3, 13), MacOSProfile((14, 0), "arm64")),
            TargetProfile((3, 13), ManylinuxProfile("x86_64", (2, 28)), threaded=True),
        ]
        for profile in profiles:
            assert profile.tags()
