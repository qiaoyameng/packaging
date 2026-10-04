from __future__ import annotations


from requirements_merger.core import (
    build_environment,
    merge,
    resolve_marker,
)


def _plugins(*specs: str, name: str = "p") -> list[dict[str, object]]:
    return [{"name": name, "requires_dist": list(specs)}]


def _target(
    name: str = "t",
    *,
    extras: list[str] | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    return {"name": name, "extras": extras or [], "environment": environment or {}}


def _group(report: dict[str, object], target: str, package: str) -> dict[str, object]:
    result = next(item for item in report["targets"] if item["target"] == target)
    return result["packages"][package]


def test_names_are_normalized_when_grouped() -> None:
    report = merge(
        _plugins("Foo.Bar>=1", "foo_bar<2", "foo-bar!=1.5"),
        [_target()],
    )
    package = _group(report, "t", "foo-bar")
    assert package["verdict"] == "satisfied"
    assert package["merged"]["specifier"] == "!=1.5,<2,>=1"
    assert len(package["requirements"]) == 3


def test_satisfiable_range_intersection_merges_extras() -> None:
    report = merge(
        _plugins("requests[Security]>=2.28", "requests[SOCKS]!=2.30.*"),
        [_target()],
    )
    package = _group(report, "t", "requests")
    assert package["verdict"] == "satisfied"
    assert package["merged"]["extras"] == ["security", "socks"]
    assert package["merged"]["specifier"] == "!=2.30.*,>=2.28"


def test_unsatisfiable_specifiers_conflict_with_minimal_proof() -> None:
    report = merge(
        _plugins(
            "flask>=2.0",
            "flask<2",
            # Irrelevant constraint: removing either of the two above destroys
            # the proof, and this one must not appear in the minimal set.
            "flask<3",
        ),
        [_target()],
    )
    package = _group(report, "t", "flask")
    assert package["verdict"] == "conflict"
    assert package["reason"] == "intersected SpecifierSet is unsatisfiable"
    assert [req["raw"] for req in package["minimal_conflict"]] == [
        "flask>=2.0",
        "flask<2",
    ]


def test_minimal_conflict_is_deletion_minimal() -> None:
    specs = [
        "pkg>=2",
        "pkg<1",
        "pkg>0",
        "pkg!=3",
    ]
    report = merge(_plugins(*specs), [_target()])
    package = _group(report, "t", "pkg")
    assert package["verdict"] == "conflict"
    minimal = {req["raw"] for req in package["minimal_conflict"]}
    assert minimal == {"pkg>=2", "pkg<1"}


def test_distinct_direct_urls_conflict_and_same_url_merges() -> None:
    report = merge(
        _plugins(
            "a @ https://example.invalid/a-1.tar.gz",
            "a @ https://example.invalid/a-1.tar.gz",
            "a @ https://mirror.invalid/a-2.zip",
        ),
        [_target()],
    )
    package = _group(report, "t", "a")
    assert package["verdict"] == "conflict"
    assert package["reason"] == "distinct direct URLs select different sources"
    assert len(package["minimal_conflict"]) == 2
    assert {req["raw"] for req in package["minimal_conflict"]} == {
        "a @ https://example.invalid/a-1.tar.gz",
        "a @ https://mirror.invalid/a-2.zip",
    }


def test_repeated_identical_url_without_other_constraint_is_satisfied() -> None:
    report = merge(
        _plugins(
            "a @ https://example.invalid/a-1.tar.gz",
            "a @ https://example.invalid/a-1.tar.gz",
        ),
        [_target()],
    )
    package = _group(report, "t", "a")
    assert package["verdict"] == "satisfied"
    assert package["merged"]["url"] == "https://example.invalid/a-1.tar.gz"


def test_url_mixed_with_version_constraint_is_unknown() -> None:
    report = merge(
        _plugins(
            "a @ https://example.invalid/a-1.tar.gz",
            "a>=1",
        ),
        [_target()],
    )
    package = _group(report, "t", "a")
    assert package["verdict"] == "unknown"
    assert "direct URL is mixed" in package["reason"]
    assert len(package["requirements"]) == 2
    assert "merged" not in package


def test_arbitrary_equality_is_unknown_and_retains_requirements() -> None:
    report = merge(
        _plugins(
            "numpy===1.26-windows-tag",
            "numpy>=1",
        ),
        [_target()],
    )
    package = _group(report, "t", "numpy")
    assert package["verdict"] == "unknown"
    assert "===" in package["reason"]
    assert len(package["requirements"]) == 2


def test_windows_only_constraints_do_not_pollute_other_targets() -> None:
    report = merge(
        _plugins(
            "flask>=2.0",
            "flask<2 ; sys_platform == 'win32'",
            "libx @ https://example.invalid/a.tar.gz ; sys_platform != 'win32'",
            "libx @ https://example.invalid/b.tar.gz ; sys_platform != 'win32'",
        ),
        [
            _target(
                "win",
                environment={"sys_platform": "win32", "os_name": "nt"},
            ),
            _target(
                "linux",
                environment={"sys_platform": "linux", "os_name": "posix"},
            ),
        ],
    )
    win = _group(report, "win", "flask")
    linux = _group(report, "linux", "flask")
    assert win["verdict"] == "conflict"
    assert linux["verdict"] == "satisfied"
    assert "libx" not in report["targets"][0]["packages"]
    assert report["targets"][1]["packages"]["libx"]["verdict"] == "conflict"


def test_marker_is_evaluated_per_selected_extra() -> None:
    report = merge(
        _plugins(
            "orjson>=3 ; extra == 'speed'",
            "tomli ; extra == 'speed' or extra == 'dev'",
        ),
        [
            _target("speed", extras=["Speed"]),
            _target("bare"),
        ],
    )
    speed_packages = report["targets"][0]["packages"]
    bare_packages = report["targets"][1]["packages"]
    assert set(speed_packages) == {"orjson", "tomli"}
    assert bare_packages == {}


def test_identical_results_are_grouped_and_others_separated() -> None:
    report = merge(
        _plugins("flask>=2.0", "flask<2 ; sys_platform == 'win32'"),
        [
            _target("win-a", environment={"sys_platform": "win32"}),
            _target("win-b", environment={"sys_platform": "win32"}),
            _target("linux", environment={"sys_platform": "linux"}),
        ],
    )
    groups = report["groups"]
    assert len(groups) == 2
    members = {tuple(group["targets"]) for group in groups}
    assert ("win-a", "win-b") in members
    assert ("linux",) in members


def test_python_full_version_implies_python_version() -> None:
    environment = build_environment(
        {"environment": {"python_full_version": "3.11.9"}}
    )
    assert environment["python_version"] == "3.11"
    from packaging.markers import Marker

    outcome = resolve_marker(
        Marker("python_version == '3.11'"), environment, frozenset()
    )
    assert outcome.active is True


def test_malformed_requirement_is_reported_not_raised() -> None:
    report = merge(
        _plugins("good>=1", "this is not valid!!!"),
        [_target()],
    )
    malformed = report["malformed_requirements"]
    assert len(malformed) == 1
    assert malformed[0]["origin"] == "p"
    assert malformed[0]["raw"] == "this is not valid!!!"
    assert "Expected" in malformed[0]["error"]
    assert _group(report, "t", "good")["verdict"] == "satisfied"


def test_undecidable_marker_keeps_requirement_as_unknown() -> None:
    report = merge(
        _plugins(
            "mystery>=1 ; extras == 'x'",
        ),
        [_target(environment={"sys_platform": "linux"})],
    )
    package = _group(report, "t", "mystery")
    assert package["verdict"] == "unknown"
    assert "marker-undecidable" in package["reason"]
    assert package["requirements"][0]["raw"].startswith("mystery>=1")


def test_url_conflict_wins_over_arbitrary_equality_unknown() -> None:
    report = merge(
        _plugins(
            "a @ https://example.invalid/1",
            "a @ https://example.invalid/2",
            "a===v",
        ),
        [_target()],
    )
    package = _group(report, "t", "a")
    assert package["verdict"] == "conflict"
    assert len(package["minimal_conflict"]) == 2
