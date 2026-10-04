# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
from __future__ import annotations

import pytest

from packaging.merger import (
    MergeReport,
    Target,
    format_report,
    merge_requirements,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet

WINDOWS = Target(
    "windows",
    {
        "sys_platform": "win32",
        "platform_system": "Windows",
        "os_name": "nt",
    },
)
LINUX = Target(
    "linux",
    {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
    },
)
LINUX_2 = Target(
    "linux-2",
    {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
    },
)


def package(report: MergeReport, target_name: str, name: str):
    for group in report.groups:
        if target_name in group.targets:
            for merged in group.packages:
                if merged.name == name:
                    return merged
    raise KeyError((target_name, name))


def has_package(report: MergeReport, target_name: str, name: str) -> bool:
    try:
        package(report, target_name, name)
    except KeyError:
        return False
    return True


class TestMerging:
    def test_intersects_ranges_and_unions_extras(self):
        report = merge_requirements(
            {
                "plugin-a": ["Foo[bar]>=1.0"],
                "plugin-b": ["foo[baz]<3.0,>=1.5"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "ok"
        assert merged.extras == frozenset({"bar", "baz"})
        assert merged.simplified is not None
        # The simplification must accept exactly the intersection.
        assert SpecifierSet(merged.simplified).is_subset(SpecifierSet(">=1.5,<3.0"))
        assert SpecifierSet(">=1.5,<3.0").is_subset(SpecifierSet(merged.simplified))

    def test_redundant_requirement_detected_via_range_relation(self):
        report = merge_requirements(
            {"a": ["foo>=1.0"], "b": ["foo>=1.5"]},
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        # >=1.5 already implies >=1.0, so only the latter adds nothing.
        assert [sr.raw for sr in merged.redundant] == ["foo>=1.0"]

    def test_version_conflict_minimal_core(self):
        report = merge_requirements(
            {
                "a": ["foo>=2.0"],
                "b": ["foo<2.0"],
                "c": ["foo>=1.0"],  # implied by >=2.0, not part of the core
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "conflict"
        (conflict,) = merged.conflicts
        assert conflict.kind == "version"
        raws = sorted(sr.raw for sr in conflict.requirements)
        assert raws == ["foo<2.0", "foo>=2.0"]
        assert {sr.source for sr in conflict.requirements} == {"a", "b"}

    def test_core_is_deletion_minimal(self):
        report = merge_requirements(
            {
                "a": ["foo>=2.0", "foo!=2.5"],
                "b": ["foo<2.0"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        (conflict,) = merged.conflicts
        core = [sr.raw for sr in conflict.requirements]
        # Removing any single entry must make the conflict unprovable.
        for dropped in core:
            remaining = [r for r in core if r != dropped]
            report2 = merge_requirements({"x": remaining}, [LINUX])
            assert package(report2, "linux", "foo").status == "ok"


class TestMarkersAndTargets:
    def test_windows_only_conflict_does_not_pollute_other_targets(self):
        plugins = {
            "plugin-a": ["foo>=2.0; sys_platform == 'win32'"],
            "plugin-b": ["foo<2.0; sys_platform == 'win32'"],
        }
        report = merge_requirements(plugins, [WINDOWS, LINUX])
        windows = package(report, "windows", "foo")
        assert windows.status == "conflict"
        assert windows.conflicts[0].kind == "version"
        # Neither constraint is active on Linux: the package does not even
        # appear there, so the Windows conflict cannot pollute it.
        assert not has_package(report, "linux", "foo")

    def test_marker_selects_constraint_per_target(self):
        plugins = {"a": ["foo>=2.0; sys_platform == 'win32'", "foo<3.0"]}
        report = merge_requirements(plugins, [WINDOWS, LINUX])
        windows = package(report, "windows", "foo")
        linux = package(report, "linux", "foo")
        assert SpecifierSet(windows.simplified) == SpecifierSet(">=2.0,<3.0")
        assert linux.simplified == "<3.0"

    def test_extras_gate_marker_evaluation(self):
        plugins = {"a": ["foo>=2.0; extra == 'test'", "foo"]}
        without = merge_requirements(plugins, [LINUX])
        assert package(without, "linux", "foo").simplified == ""
        with_extra = merge_requirements(plugins, [LINUX], extras=["test"])
        assert package(with_extra, "linux", "foo").simplified == ">=2.0"

    def test_identical_targets_are_grouped(self):
        plugins = {"a": ["foo>=1.0; sys_platform == 'win32'", "bar"]}
        report = merge_requirements(plugins, [LINUX, LINUX_2, WINDOWS])
        groups = {group.targets: group for group in report.groups}
        assert ("linux", "linux-2") in groups
        assert ("windows",) in groups
        assert has_package(report, "windows", "foo")
        assert not has_package(report, "linux", "foo")


class TestDirectURL:
    def test_same_url_merges(self):
        report = merge_requirements(
            {
                "a": ["foo @ https://example.com/foo-1.0.tar.gz"],
                "b": ["foo @ https://example.com/foo-1.0.tar.gz"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "ok"
        assert merged.urls == ("https://example.com/foo-1.0.tar.gz",)
        assert merged.conflicts == ()

    def test_different_urls_conflict(self):
        report = merge_requirements(
            {
                "a": ["foo @ https://example.com/foo-1.0.tar.gz"],
                "b": ["foo @ https://other.example.com/foo-1.0.tar.gz"],
                "c": ["foo @ https://example.com/foo-1.0.tar.gz"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "conflict"
        (conflict,) = merged.conflicts
        assert conflict.kind == "url"
        # Minimal core: exactly two requirements with two distinct URLs.
        assert len(conflict.requirements) == 2
        urls = {sr.requirement.url for sr in conflict.requirements}
        assert len(urls) == 2
        assert {sr.source for sr in conflict.requirements} <= {"a", "b", "c"}

    def test_url_with_version_specifier_is_unknown(self):
        report = merge_requirements(
            {
                "a": ["foo @ https://example.com/foo-1.0.tar.gz"],
                "b": ["foo>=1.0"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "unknown"
        assert merged.conflicts == ()
        # Both constraints are kept.
        assert merged.urls == ("https://example.com/foo-1.0.tar.gz",)
        assert merged.specifier == ">=1.0"
        assert any("cannot be verified" in note for note in merged.notes)

    def test_url_with_bare_requirement_is_ok(self):
        report = merge_requirements(
            {
                "a": ["foo @ https://example.com/foo-1.0.tar.gz"],
                "b": ["foo"],
            },
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "ok"


class TestArbitraryEquality:
    def test_arbitrary_equality_kept_as_is(self):
        report = merge_requirements(
            {"a": ["foo===1.0"], "b": ["foo>=1.0"]},
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "ok"
        assert merged.simplified is None
        assert "===1.0" in merged.specifier
        assert any("kept as-is" in note for note in merged.notes)

    def test_arbitrary_equality_contradiction_detected(self):
        report = merge_requirements(
            {"a": ["foo===1.0"], "b": ["foo>2.0"]},
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.status == "conflict"
        assert merged.conflicts[0].kind == "version"

    def test_arbitrary_equality_relations_not_proven(self):
        report = merge_requirements(
            {"a": ["foo===1.0"], "b": ["foo>=1.0,<2.0"]},
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        # No redundancy claims are made when === is involved.
        assert merged.redundant == ()


class TestReport:
    def test_format_report_mentions_sources(self):
        report = merge_requirements(
            {
                "plugin-a": ["foo>=2.0; sys_platform == 'win32'"],
                "plugin-b": ["foo<2.0; sys_platform == 'win32'"],
            },
            [WINDOWS, LINUX],
        )
        text = format_report(report)
        assert "plugin-a" in text
        assert "plugin-b" in text
        assert "conflict (version)" in text
        assert "windows" in text and "linux" in text

    def test_report_conflicts_property(self):
        report = merge_requirements(
            {"a": ["foo>=2.0"], "b": ["foo<2.0"]},
            [LINUX],
        )
        assert len(report.conflicts) == 1

    def test_requirement_objects_accepted(self):
        report = merge_requirements(
            {"a": [Requirement("foo>=1.0")]},
            [LINUX],
        )
        merged = package(report, "linux", "foo")
        assert merged.simplified == ">=1.0"

    def test_invalid_requirement_raises(self):
        with pytest.raises(InvalidRequirement):
            merge_requirements({"a": ["foo>="]}, [LINUX])
