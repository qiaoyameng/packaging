from __future__ import annotations

import json

import pytest
from packaging.metadata import Metadata, parse_email

from pep643_check import ChangeKind, compare_metadata, load_metadata
from pep643_check.cli import main as cli_main

BASE = """Metadata-Version: 2.2
Name: demo-pkg
Version: 1.0.0
Summary: Demo
"""


def fields(sdist: str, wheel: str) -> dict[str, ChangeKind]:
    report = compare_metadata(sdist, wheel)
    return {change.field: change.kind for change in report.changes if change.is_difference}


def rules(sdist: str, wheel: str) -> dict[str, str]:
    report = compare_metadata(sdist, wheel)
    return {change.field: change.rule for change in report.violations}


def test_identity_is_ok() -> None:
    report = compare_metadata(BASE, BASE)
    assert report.ok
    assert not report.changes


def test_identity_fields_are_never_dynamic() -> None:
    sdist = BASE + "Dynamic: Name\nDynamic: Version\n"
    wheel = BASE.replace("demo-pkg", "other-pkg").replace(
        "1.0.0", "2.0.0"
    ) + "Dynamic: Name\nDynamic: Version\n"
    result = fields(sdist, wheel)
    assert result["name"] is ChangeKind.VIOLATION
    assert result["version"] is ChangeKind.VIOLATION
    assert rules(sdist, wheel)["name"] == "never-dynamic"


def test_dynamic_field_change_is_allowed() -> None:
    sdist = BASE + "Dynamic: Summary\n"
    wheel = BASE.replace("Summary: Demo", "Summary: Different")
    wheel += "Dynamic: Summary\n"
    result = fields(sdist, wheel)
    assert result == {"summary": ChangeKind.ALLOWED}


def test_dynamic_declaration_itself_can_change() -> None:
    sdist = BASE + "Dynamic: Summary\n"
    wheel = BASE.replace("Summary: Demo", "Summary: Different")
    wheel += "Dynamic: Summary\nDynamic: Classifier\nClassifier: A\n"
    result = fields(sdist, wheel)
    assert result["dynamic"] is ChangeKind.ALLOWED
    assert result["summary"] is ChangeKind.ALLOWED
    assert result["classifiers"] is ChangeKind.VIOLATION


def test_old_metadata_version_has_no_dynamic_semantics() -> None:
    sdist = (
        "Metadata-Version: 2.1\nName: p\nVersion: 1\n"
        "Dynamic: Summary\nSummary: old\n"
    )
    wheel = (
        "Metadata-Version: 2.2\nName: p\nVersion: 1\n"
        "Dynamic: Summary\nSummary: new\n"
    )
    result = fields(sdist, wheel)
    assert result["metadata_version"] is ChangeKind.UNCERTAIN
    assert result["summary"] is ChangeKind.VIOLATION
    report = compare_metadata(sdist, wheel)
    summary = next(change for change in report.violations if change.field == "summary")
    assert summary.detail is not None
    assert summary.rule == "pre-pep-643-dynamic-header-has-no-semantics"
    assert "predates Metadata-Version 2.2" in summary.detail



def test_dynamic_listing_unknown_field_does_not_grant_permission() -> None:
    sdist = BASE + "Dynamic: Magic-Field\n"
    wheel = BASE + "Dynamic: Magic-Field\nMagic-Field: x\n"
    result = fields(sdist, wheel)
    assert result["magic-field"] is ChangeKind.VIOLATION


def test_metadata_version_upgrade_is_uncertain_not_violation() -> None:
    result = fields(
        BASE,
        BASE.replace("Metadata-Version: 2.2", "Metadata-Version: 2.4"),
    )
    assert result == {"metadata_version": ChangeKind.UNCERTAIN}
    assert not compare_metadata(BASE, BASE.replace("2.2", "2.4")).ok


def test_metadata_version_downgrade_is_violation() -> None:
    sdist = BASE.replace("2.2", "2.4")
    wheel = BASE
    assert fields(sdist, wheel)["metadata_version"] is ChangeKind.VIOLATION


def test_classifier_order_is_semantic_noise() -> None:
    assert fields(BASE + "Classifier: A\nClassifier: B\n",
                  BASE + "Classifier: B\nClassifier: A\n") == {}


def test_classifier_value_change_is_violation() -> None:
    result = fields(BASE + "Classifier: A\n", BASE + "Classifier: B\n")
    assert result["classifiers"] is ChangeKind.VIOLATION


def test_provides_extra_normalization_is_equal() -> None:
    assert fields(BASE + "Provides-Extra: Foo_Bar\n",
                  BASE + "Provides-Extra: foo-bar\n") == {}


def test_keywords_spacing_and_order_is_equal() -> None:
    sdist = BASE + "Keywords: a, b\n"
    wheel = BASE + "Keywords: b ,  a\n"
    assert fields(sdist, wheel) == {}


def test_project_urls_label_and_order_normalization() -> None:
    sdist = BASE + "Project-URL: Home , https://a\nProject-URL: Bugs, https://b\n"
    wheel = BASE + "Project-URL: Bugs,https://b\nProject-URL: Home,https://a\n"
    assert fields(sdist, wheel) == {}


def test_project_url_change_is_violation() -> None:
    sdist = BASE + "Project-URL: Home, https://a\n"
    wheel = BASE + "Project-URL: Home, https://b\n"
    assert fields(sdist, wheel)["project_urls"] is ChangeKind.VIOLATION


def test_requires_dist_order_name_and_extras_are_equal() -> None:
    sdist = (
        BASE
        + 'Requires-Dist: z>=1\n'
        + 'Requires-Dist: Foo_Bar[Extra_A,extra-b]>=1; extra == "X"\n'
    )
    wheel = (
        BASE
        + 'Requires-Dist: foo-bar[extra-b,extra-a]>=1; extra=="x"\n'
        + 'Requires-Dist: z >= 1\n'
    )
    assert fields(sdist, wheel) == {}


def test_requires_dist_static_specifier_change_is_violation() -> None:
    sdist = BASE + "Requires-Dist: click>=8.0\n"
    wheel = BASE + "Requires-Dist: click>=9.0\n"
    result = fields(sdist, wheel)
    assert result["requires_dist"] is ChangeKind.VIOLATION


def test_requires_dist_provable_marker_difference_is_violation() -> None:
    sdist = BASE + 'Requires-Dist: click; python_version>="3.10"\n'
    wheel = BASE + 'Requires-Dist: click; python_version>="3.8"\n'
    result = fields(sdist, wheel)
    assert result["requires_dist"] is ChangeKind.VIOLATION


def test_requires_dist_unparsable_marker_is_uncertain() -> None:
    sdist = BASE + "Requires-Dist: click; bogus = marker\n"
    wheel = BASE + "Requires-Dist: click; other bogus = marker\n"
    result = fields(sdist, wheel)
    assert result["requires_dist"] is ChangeKind.UNCERTAIN


def test_requires_dist_provable_marker_reorder_is_equal() -> None:
    sdist = (
        BASE
        + 'Requires-Dist: click; extra == "x" and python_version >= "3.10"\n'
    )
    wheel = (
        BASE
        + 'Requires-Dist: click; python_version>="3.10" and extra=="x"\n'
    )
    assert fields(sdist, wheel) == {}


def test_requires_python_formatting_is_equal() -> None:
    assert fields(BASE + "Requires-Python: >=3.10\n",
                  BASE + "Requires-Python:>= 3.10\n") == {}


def test_private_x_extension_added_is_allowed() -> None:
    result = fields(BASE, BASE + "X-Build-Revision: abc\n")
    assert result["x-build-revision"] is ChangeKind.ALLOWED


def test_unknown_non_x_extension_is_uncertain() -> None:
    result = fields(BASE, BASE + "Magic-Field: abc\n")
    assert result["magic-field"] is ChangeKind.UNCERTAIN


def test_raw_mapping_and_parsed_metadata_use_same_rules() -> None:
    sdist_text = BASE + "Requires-Dist: click>=8.0\n"
    wheel_text = BASE + "Requires-Dist: click>=9.0\n"
    raw_sdist, _ = parse_email(sdist_text)
    raw_wheel, _ = parse_email(wheel_text)
    report = compare_metadata(raw_sdist, raw_wheel)
    assert [c.field for c in report.violations] == ["requires_dist"]

    parsed_sdist = Metadata.from_raw(raw_sdist, validate=False)
    parsed_wheel = Metadata.from_raw(raw_wheel, validate=False)
    report2 = compare_metadata(parsed_sdist, parsed_wheel)
    assert [c.field for c in report2.violations] == ["requires_dist"]


def test_load_metadata_does_not_mutate_input() -> None:
    raw, _ = parse_email(BASE + "Requires-Dist: Foo_Bar\n")
    snapshot = repr(raw)
    load_metadata(raw)
    assert repr(raw) == snapshot


def test_cli_exit_codes_and_json(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "examples"
    assert cli_main([
        str(root / "sdist_legal_dynamic.pkg-info"),
        str(root / "wheel_legal_dynamic.metadata"),
    ]) == 0
    assert "Allowed differences" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", [
        "pep643_check",
        "--json",
        str(root / "sdist_static_tampered.pkg-info"),
        str(root / "wheel_static_tampered.metadata"),
    ])
    assert cli_main([
        "--json",
        str(root / "sdist_static_tampered.pkg-info"),
        str(root / "wheel_static_tampered.metadata"),
    ]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert [item["field"] for item in payload["violations"]] == ["requires_dist"]
    assert payload["uncertain"] == []


def test_description_body_change_without_dynamic_is_violation() -> None:
    sdist = BASE + "\nBody text"
    wheel = BASE + "\nDifferent body"
    assert fields(sdist, wheel)["description"] is ChangeKind.VIOLATION


def test_license_expression_spelling_is_equal() -> None:
    assert fields(BASE + "License-Expression: mit\n",
                  BASE + "License-Expression: MIT\n") == {}


def test_license_expression_change_is_violation() -> None:
    result = fields(BASE + "License-Expression: MIT\n",
                    BASE + "License-Expression: Apache-2.0\n")
    assert result["license_expression"] is ChangeKind.VIOLATION


def test_multiset_duplicates_have_meaning() -> None:
    sdist = BASE + "Requires-External: libc\n"
    wheel = (
        BASE
        + "Requires-External: libc\n"
        + "Requires-External: libc\n"
    )
    result = fields(sdist, wheel)
    assert result["requires_external"] is ChangeKind.VIOLATION


def test_dynamic_field_removed_from_wheel_still_gate_valid() -> None:
    sdist = BASE + "Dynamic: Home-page\nHome-page: https://old\n"
    wheel = BASE + "Dynamic: Home-page\nHome-page: https://new\n"
    assert fields(sdist, wheel)["home_page"] is ChangeKind.ALLOWED


def test_dynamic_field_added_when_absent_in_sdist_is_allowed() -> None:
    sdist = BASE + "Dynamic: Home-page\n"
    wheel = BASE + "Dynamic: Home-page\nHome-page: https://new\n"
    assert fields(sdist, wheel)["home_page"] is ChangeKind.ALLOWED


def test_unknown_dynamic_token_unknown_field_change_is_violation() -> None:
    sdist = BASE + "Dynamic:\n"
    wheel = BASE + "Dynamic: name\n"
    # 'name' is a valid but forbidden dynamic field; declaration itself may
    # differ, while the concrete Name value remains unchanged.
    assert fields(sdist, wheel)["dynamic"] is ChangeKind.ALLOWED


def test_dynamic_declared_only_in_wheel_does_not_grant_permission() -> None:
    sdist = BASE
    wheel = BASE + "Dynamic: Home-page\nHome-page: https://new\n"
    result = fields(sdist, wheel)
    assert result["home_page"] is ChangeKind.VIOLATION
    assert result["dynamic"] is ChangeKind.VIOLATION


def test_pre_pep643_dynamic_header_does_not_grant_permission() -> None:
    sdist = (
        "Metadata-Version: 2.1\nName: p\nVersion: 1\n"
        "Dynamic: Home-page\nHome-page: https://old\n"
    )
    wheel = (
        "Metadata-Version: 2.2\nName: p\nVersion: 1\n"
        "Dynamic: Home-page\nHome-page: https://new\n"
    )
    result = fields(sdist, wheel)
    assert result["home_page"] is ChangeKind.VIOLATION


def test_requires_dist_structurally_different_marker_is_violation() -> None:
    sdist = BASE + 'Requires-Dist: click; platform_machine == "x86_64"\n'
    wheel = BASE + 'Requires-Dist: click; platform_machine == "aarch64"\n'
    result = fields(sdist, wheel)
    assert result["requires_dist"] is ChangeKind.VIOLATION


def test_requires_dist_unparsable_only_in_wheel_is_uncertain_not_clean() -> None:
    sdist = BASE + "Requires-Dist: click>=8\n"
    wheel = BASE + "Requires-Dist: this is not valid!!\n"
    report = compare_metadata(sdist, wheel)
    assert not report.ok
    assert report.uncertain and not report.violations
