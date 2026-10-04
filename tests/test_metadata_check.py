from __future__ import annotations

import copy
import pathlib

import pytest

from packaging import metadata as metadata_module
from packaging.metadata_check import (
    ChangeKind,
    FieldChange,
    check_metadata_consistency,
)


FIXTURES = pathlib.Path(__file__).parent / "metadata_check"


def _find(changes: tuple[FieldChange, ...], field: str) -> list[FieldChange]:
    return [c for c in changes if c.field.lower() == field.lower()]


def _kinds(changes: tuple[FieldChange, ...], field: str) -> list[ChangeKind]:
    return [c.kind for c in _find(changes, field)]


def _ids(changes: tuple[FieldChange, ...], field: str) -> list[str]:
    return [c.rule.id for c in _find(changes, field)]


class TestFixturePairs:
    """The two contrasting examples required by the task."""

    def test_stolen_static_dependency_is_a_violation(self) -> None:
        sdist = (FIXTURES / "stolen-static-dependency.sdist.metadata").read_text()
        wheel = (FIXTURES / "stolen-static-dependency.wheel.metadata").read_text()
        result = check_metadata_consistency(sdist, wheel)

        assert not result.ok
        assert len(result.violations) == 1
        change = _find(result.changes, "Requires-Dist")[0]
        assert change.kind is ChangeKind.VIOLATION
        assert change.old == "requests==2.28.0"
        assert change.new == "requests==2.32.3"
        assert change.rule.id == "pep643-multi-static"
        assert len(_find(result.changes, "Requires-Dist")) == 1

    def test_legal_dynamic_dependency_change_is_allowed(self) -> None:
        sdist = (FIXTURES / "legal-dynamic-dependency.sdist.metadata").read_text()
        wheel = (FIXTURES / "legal-dynamic-dependency.wheel.metadata").read_text()
        result = check_metadata_consistency(sdist, wheel)

        assert result.ok
        assert result.violations == ()
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.ALLOWED
        assert changes[0].old is None
        assert changes[0].new == ("requests==2.32.3", "click>=8.0")
        assert changes[0].rule.id == "pep643-multi-dynamic"


HEADER = "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"


class TestStrictFields:
    def test_name_change_is_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER.replace("Name: demo", "Name: other")
        )
        assert _kinds(result.changes, "Name") == [ChangeKind.VIOLATION]
        assert _ids(result.changes, "Name") == ["pep643-name-static"]

    def test_name_normalization_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("Name: demo", "Name: My_Package"),
            HEADER.replace("Name: demo", "Name: my.package"),
        )
        assert _find(result.changes, "Name") == []

    def test_name_change_even_when_dynamic_listed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Name\n",
            HEADER.replace("Name: demo", "Name: other") + "Dynamic: Name\n",
        )
        assert _kinds(result.changes, "Name") == [ChangeKind.VIOLATION]

    def test_version_change_is_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER.replace("1.0", "2.0")
        )
        assert _kinds(result.changes, "Version") == [ChangeKind.VIOLATION]
        assert _ids(result.changes, "Version") == ["pep643-version-static"]

    def test_version_normalization_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER.replace("Version: 1.0", "Version: 1.0.0")
        )
        assert result.violations == ()
        assert _find(result.changes, "Version") == []

    @pytest.mark.parametrize("field", ["Name", "Version"])
    def test_missing_required_in_wheel(self, field: str) -> None:
        wheel = "Metadata-Version: 2.2\n"
        if field == "Version":
            wheel += "Name: demo\n"
        else:
            wheel += "Version: 1.0\n"
        result = check_metadata_consistency(HEADER, wheel)
        assert _kinds(result.changes, field) == [ChangeKind.VIOLATION]
        assert _ids(result.changes, field) == ["required-field-changed"]

    def test_duplicate_required_header_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Name: demo\n",
            HEADER.replace("Name: demo", "Name: demo\nName: other"),
        )
        assert _kinds(result.changes, "Name") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Name") == ["field-value-unparseable"]


class TestMetadataVersion:
    def test_newer_metadata_version_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "2.2"), HEADER.replace("2.2", "2.4")
        )
        assert _kinds(result.changes, "Metadata-Version") == [ChangeKind.ALLOWED]

    def test_downgrade_is_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "2.4"), HEADER
        )
        assert _kinds(result.changes, "Metadata-Version") == [
            ChangeKind.VIOLATION
        ]

    def test_same_version_no_change(self) -> None:
        result = check_metadata_consistency(HEADER, HEADER)
        assert _find(result.changes, "Metadata-Version") == []

    def test_unrecognized_version_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "9.9"), HEADER
        )
        assert _kinds(result.changes, "Metadata-Version") == [ChangeKind.UNKNOWN]


class TestDynamicField:
    def test_dynamic_removed_is_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\n", HEADER
        )
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Dynamic") == ["pep643-dynamic-field-list"]

    def test_dynamic_case_insensitive_no_change(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\n", HEADER + "Dynamic: summary\n"
        )
        assert _find(result.changes, "Dynamic") == []

    def test_dynamic_on_old_metadata_is_unknown(self) -> None:
        old = HEADER.replace("2.2", "2.1") + "Dynamic: Summary\n"
        new = HEADER.replace("2.2", "2.1")
        result = check_metadata_consistency(old, new)
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Dynamic") == ["pre-2.2-dynamic-field"]

    def test_wheel_only_dynamic_does_not_grant_permission(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Summary: a\n",
            HEADER + "Summary: b\nDynamic: Summary\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.VIOLATION]


class TestScalarFields:
    @pytest.mark.parametrize(
        "header,old,new",
        [
            ("Summary", "old summary", "new summary"),
            ("Home-Page", "https://a.example", "https://b.example"),
            ("Author", "Alice", "Bob"),
            ("Maintainer", "Alice", "Bob"),
        ],
    )
    def test_static_scalar_change_violation(
        self, header: str, old: str, new: str
    ) -> None:
        if header == "Maintainer":
            prefix = HEADER.replace("2.2", "2.2")
        else:
            prefix = HEADER
        result = check_metadata_consistency(
            f"{prefix}{header}: {old}\n", f"{prefix}{header}: {new}\n"
        )
        assert _kinds(result.changes, header) == [ChangeKind.VIOLATION]

    def test_dynamic_scalar_change_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\nSummary: a\n",
            HEADER + "Summary: b\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Summary") == ["pep643-scalar-dynamic"]

    def test_wheel_adds_static_scalar_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER + "Summary: new\n"
        )
        change = _find(result.changes, "Summary")[0]
        assert change.kind is ChangeKind.VIOLATION
        assert change.old is None
        assert change.new == "new"

    def test_wheel_removes_static_scalar_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Summary: a\n", HEADER
        )
        change = _find(result.changes, "Summary")[0]
        assert change.kind is ChangeKind.VIOLATION
        assert change.old == "a"
        assert change.new is None

    def test_explicit_empty_equals_missing(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Summary: \n", HEADER
        )
        assert result.changes == ()

    def test_requires_python_normalized_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Python: >=3.8\n",
            HEADER + "Requires-Python: >=3.8.0\n",
        )
        assert _find(result.changes, "Requires-Python") == []

    def test_requires_python_changed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Python: >=3.8\n",
            HEADER + "Requires-Python: >=3.9\n",
        )
        assert _kinds(result.changes, "Requires-Python") == [ChangeKind.VIOLATION]

    def test_license_expression_spelling_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "2.4")
            + "License-Expression: mit and apache-2.0\n",
            HEADER.replace("2.2", "2.4")
            + "License-Expression: MIT AND Apache-2.0\n",
        )
        assert _find(result.changes, "License-Expression") == []

    def test_license_expression_changed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "2.4") + "License-Expression: MIT\n",
            HEADER.replace("2.2", "2.4") + "License-Expression: Apache-2.0\n",
        )
        assert _kinds(result.changes, "License-Expression") == [
            ChangeKind.VIOLATION
        ]

    def test_license_expression_unparseable_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "2.4") + "License-Expression: mit\n",
            HEADER.replace("2.2", "2.4")
            + "License-Expression: definitely-not-an-spdx-id\n",
        )
        assert _kinds(result.changes, "License-Expression") == [ChangeKind.UNKNOWN]


class TestRequiresDist:
    def test_reorder_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: alpha\nRequires-Dist: beta\n",
            HEADER + "Requires-Dist: beta\nRequires-Dist: alpha\n",
        )
        assert result.changes == ()

    def test_name_normalization_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo_bar[baz]==1.0\n",
            HEADER + "Requires-Dist: foo-bar[Baz]==1.0.0\n",
        )
        assert result.changes == ()

    def test_marker_reformat_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + 'Requires-Dist: foo;python_version>="3.8"\n',
            HEADER + 'Requires-Dist: foo ; python_version >= "3.8"\n',
        )
        assert result.changes == ()

    def test_specifier_normalization_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo>=1.0,!=2.0\n",
            HEADER + "Requires-Dist: foo!=2.0,>=1.0.0\n",
        )
        assert result.changes == ()

    def test_static_version_change_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: requests==2.28.0\n",
            HEADER + "Requires-Dist: requests==2.32.3\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION
        assert changes[0].old == "requests==2.28.0"
        assert changes[0].new == "requests==2.32.3"

    def test_static_added_dependency_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo\n",
            HEADER + "Requires-Dist: foo\nRequires-Dist: bar\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION
        assert changes[0].old is None
        assert changes[0].new == "bar"

    def test_static_removed_dependency_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo\nRequires-Dist: bar\n",
            HEADER + "Requires-Dist: foo\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION
        assert changes[0].old == "bar"
        assert changes[0].new is None

    def test_dynamic_dependency_change_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Requires-Dist\nRequires-Dist: foo==1\n",
            HEADER + "Requires-Dist: foo==2\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.ALLOWED]

    def test_duplicate_entries_are_a_multiset(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo==1\n",
            HEADER + "Requires-Dist: foo==1\nRequires-Dist: foo==1\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION

    def test_marker_change_not_proven_equivalent_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + 'Requires-Dist: foo;python_version>="3.8"\n',
            HEADER + 'Requires-Dist: foo;python_version>"3.7"\n',
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.UNKNOWN
        assert changes[0].rule.id == "requires-dist-equivalence-unproven"

    def test_url_change_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo @ https://e.example/f-1.tar.gz\n",
            HEADER + "Requires-Dist: foo @ https://e.example/f-2.tar.gz\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.UNKNOWN]

    def test_extras_normalized_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo[a_b]\n",
            HEADER + "Requires-Dist: foo[a-b]\n",
        )
        assert result.changes == ()

    def test_extras_changed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo[a]\n",
            HEADER + "Requires-Dist: foo[b]\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.VIOLATION]

    def test_unparsable_requirement_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo==1\n",
            HEADER + "Requires-Dist: !!not-valid!!\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Requires-Dist") == ["field-value-unparseable"]

    def test_unparsable_but_identical_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: !!bad!!\n",
            HEADER + "Requires-Dist: !!bad!!\n",
        )
        assert result.changes == ()


class TestOtherMultiFields:
    def test_classifiers_reorder_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Classifier: A :: B\nClassifier: C :: D\n",
            HEADER + "Classifier: C :: D\nClassifier: A :: B\n",
        )
        assert result.changes == ()

    def test_classifier_added_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Classifier: A\n", HEADER + "Classifier: A\nClassifier: B\n"
        )
        assert _kinds(result.changes, "Classifier") == [ChangeKind.VIOLATION]

    def test_keywords_reorder_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Keywords: a, b\n", HEADER + "Keywords: b, a\n"
        )
        assert result.changes == ()

    def test_keywords_changed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Keywords: a\n", HEADER + "Keywords: a, c\n"
        )
        assert _kinds(result.changes, "Keywords") == [ChangeKind.VIOLATION]

    def test_provides_extra_normalized_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Provides-Extra: My_Extra\n",
            HEADER + "Provides-Extra: my.extra\n",
        )
        assert result.changes == ()

    def test_provides_extra_added_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Provides-Extra: a\n",
            HEADER + "Provides-Extra: a\nProvides-Extra: b\n",
        )
        assert _kinds(result.changes, "Provides-Extra") == [ChangeKind.VIOLATION]

    def test_project_urls_equal_and_new_static_entry_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Project-URL: Home, https://a\n",
            HEADER
            + "Project-URL: Home, https://a\nProject-URL: Bugs, https://b\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.VIOLATION]

    def test_project_urls_static_value_changed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Project-URL: Home, https://a\n",
            HEADER + "Project-URL: Home, https://c\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.VIOLATION]


H26 = "Metadata-Version: 2.6\nName: demo\nVersion: 1.0\n"


class TestPep808PartialDynamic:
    def test_extra_dependency_added_is_allowed(self) -> None:
        result = check_metadata_consistency(
            H26 + "Dynamic: Requires-Dist\nRequires-Dist: requests==2.28.0\n",
            H26
            + "Dynamic: Requires-Dist\nRequires-Dist: requests==2.28.0\n"
            + "Requires-Dist: tomli;python_version<'3.11'\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert [c.kind for c in changes] == [ChangeKind.ALLOWED]
        assert changes[0].rule.id == "pep808-dynamic-extra-value"

    def test_static_dependency_changed_is_violation(self) -> None:
        result = check_metadata_consistency(
            H26 + "Dynamic: Requires-Dist\nRequires-Dist: requests==2.28.0\n",
            H26 + "Dynamic: Requires-Dist\nRequires-Dist: requests==2.32.3\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.VIOLATION]
        assert _ids(result.changes, "Requires-Dist") == ["pep643-multi-static"]

    def test_static_dependency_removed_is_violation(self) -> None:
        result = check_metadata_consistency(
            H26 + "Dynamic: Requires-Dist\nRequires-Dist: requests==2.28.0\n",
            H26 + "Dynamic: Requires-Dist\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.VIOLATION]

    def test_extra_project_url_allowed_static_changed_violation(self) -> None:
        result = check_metadata_consistency(
            H26 + "Dynamic: Project-URL\nProject-URL: Home, https://a\n",
            H26
            + "Dynamic: Project-URL\nProject-URL: Home, https://a\n"
            + "Project-URL: Bugs, https://b\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.ALLOWED]

        result2 = check_metadata_consistency(
            H26 + "Dynamic: Project-URL\nProject-URL: Home, https://a\n",
            H26 + "Dynamic: Project-URL\nProject-URL: Home, https://c\n",
        )
        assert _kinds(result2.changes, "Project-URL") == [ChangeKind.VIOLATION]

    def test_scalar_dynamic_still_fully_dynamic(self) -> None:
        result = check_metadata_consistency(
            H26 + "Dynamic: Summary\nSummary: a\n",
            H26 + "Summary: b\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]


class TestLegacyMetadata:
    LEGACY = "Metadata-Version: 1.2\nName: demo\nVersion: 1.0\n"

    def test_changed_dependency_is_unknown_not_violation(self) -> None:
        result = check_metadata_consistency(
            self.LEGACY + "Requires-Dist: foo==1\n",
            "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nRequires-Dist: foo==2\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Requires-Dist") == [
            "pre-2.2-no-dynamic-semantics"
        ]

    def test_identical_legacy_metadata_is_clean(self) -> None:
        result = check_metadata_consistency(
            self.LEGACY + "Requires-Dist: foo\n",
            self.LEGACY + "Requires-Dist: foo\n",
        )
        assert result.changes == ()

    def test_name_still_strictly_checked(self) -> None:
        result = check_metadata_consistency(
            self.LEGACY, self.LEGACY.replace("demo", "other")
        )
        assert _kinds(result.changes, "Name") == [ChangeKind.VIOLATION]


class TestUnknownFields:
    def test_new_extension_field_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER + "X-Build-Hash: abc\n"
        )
        changes = _find(result.changes, "x-build-hash")
        assert [c.kind for c in changes] == [ChangeKind.UNKNOWN]
        assert changes[0].rule.id == "unknown-extension-field"
        assert changes[0].old is None
        assert changes[0].new == ("abc",)

    def test_changed_extension_field_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "X-Build-Hash: abc\n",
            HEADER + "X-Build-Hash: def\n",
        )
        assert _kinds(result.changes, "x-build-hash") == [ChangeKind.UNKNOWN]

    def test_identical_extension_field_is_clean(self) -> None:
        result = check_metadata_consistency(
            HEADER + "X-Build-Hash: abc\n",
            HEADER + "X-Build-Hash: abc\n",
        )
        assert result.changes == ()

    def test_removed_extension_field_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "X-Build-Hash: abc\n", HEADER
        )
        assert _kinds(result.changes, "x-build-hash") == [ChangeKind.UNKNOWN]


class TestInputForms:
    def test_bytes_input(self) -> None:
        result = check_metadata_consistency(
            HEADER.encode(), (HEADER + "Summary: x\n").encode()
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.VIOLATION]

    def test_raw_mapping_input(self) -> None:
        sdist = {
            "metadata_version": "2.2",
            "name": "demo",
            "version": "1.0",
            "requires_dist": ["foo==1"],
        }
        wheel = dict(sdist, requires_dist=["foo==2"])
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.VIOLATION]

    def test_raw_and_unparsed_tuple_input(self) -> None:
        s_raw, s_un = metadata_module.parse_email(
            HEADER + "Requires-Dist: foo\n"
        )
        w_raw, w_un = metadata_module.parse_email(
            HEADER + "Requires-Dist: bar\n"
        )
        result = check_metadata_consistency((s_raw, s_un), (w_raw, w_un))
        kinds = _kinds(result.changes, "Requires-Dist")
        assert set(kinds) == {ChangeKind.VIOLATION}
        assert len(kinds) == 2  # an unmatched removal and addition

    def test_metadata_object_input(self) -> None:
        sdist = metadata_module.Metadata.from_email(
            HEADER + "Requires-Python: >=3.8\n"
        )
        wheel = metadata_module.Metadata.from_email(
            HEADER + "Requires-Python: >=3.8.0\n"
        )
        result = check_metadata_consistency(sdist, wheel)
        assert result.changes == ()

    def test_invalid_input_type_raises(self) -> None:
        with pytest.raises(TypeError):
            check_metadata_consistency(123, HEADER)  # type: ignore[arg-type]

    def test_inputs_are_not_mutated(self) -> None:
        text = HEADER + "Requires-Dist: foo==1.0\nDynamic: Summary\n"
        s_meta = metadata_module.Metadata.from_email(text, validate=False)
        w_meta = metadata_module.Metadata.from_email(text, validate=False)
        snapshot = copy.deepcopy(s_meta._raw)
        check_metadata_consistency(s_meta, w_meta)
        assert s_meta._raw == snapshot

        s_raw, s_un = metadata_module.parse_email(text)
        w_raw, w_un = metadata_module.parse_email(text)
        raw_snap = copy.deepcopy(s_raw)
        un_snap = copy.deepcopy(s_un)
        check_metadata_consistency((s_raw, s_un), (w_raw, w_un))
        assert s_raw == raw_snap
        assert s_un == un_snap


class TestResultShape:
    def test_grouping_properties(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo\nX-S: a\n",
            HEADER
            + "Requires-Dist: bar\nX-S: b\nDynamic: Summary\nSummary: z\n",
        )
        assert (
            len(result.changes)
            == len(result.violations)
            + len(result.allowed)
            + len(result.unknown)
        )
        assert not result.ok
        # Changes are ordered by field name.
        names = [c.field.lower() for c in result.changes]
        assert names == sorted(names)

    def test_str_contains_field_and_kind(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Summary: a\n", HEADER + "Summary: b\n"
        )
        text = str(result.violations[0])
        assert "summary" in text
        assert "violation" in text

    def test_clean_comparison(self) -> None:
        result = check_metadata_consistency(HEADER, HEADER)
        assert result.ok
        assert result.changes == ()
        assert result.violations == result.allowed == result.unknown == ()


class TestUnparseableValues:
    def test_invalid_name_scalar_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("Name: demo", "Name: !!bad!!"), HEADER
        )
        assert _kinds(result.changes, "Name") == [ChangeKind.UNKNOWN]

    def test_invalid_version_scalar_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("Version: 1.0", "Version: not a version"), HEADER
        )
        assert _kinds(result.changes, "Version") == [ChangeKind.UNKNOWN]

    def test_invalid_requires_python_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Python: ~~garbage~~\n",
            HEADER + "Requires-Python: >=3.8\n",
        )
        assert _kinds(result.changes, "Requires-Python") == [ChangeKind.UNKNOWN]

    def test_invalid_provides_extra_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Provides-Extra: NOT VALID!!\n",
            HEADER + "Provides-Extra: alsobad!!\n",
        )
        assert _kinds(result.changes, "Provides-Extra") == [ChangeKind.UNKNOWN]

    def test_sdist_unparsed_wheel_absent_static_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Summary: a\nSummary: b\n", HEADER
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]

    def test_wheel_adds_unparsable_static_is_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER, HEADER + "Summary: a\nSummary: b\n"
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.VIOLATION]

    def test_unparsable_field_dynamic_is_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\nSummary: a\nSummary: b\n",
            HEADER + "Summary: c\nSummary: d\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]

    def test_unparsable_field_legacy_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2") + "Summary: a\nSummary: b\n",
            HEADER.replace("2.2", "2.2") + "Summary: c\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]


class TestMetadataObjectInvalid:
    def test_invalid_name_in_metadata_object_is_unknown(self) -> None:
        sdist = metadata_module.Metadata.from_raw(
            {
                "metadata_version": "2.2",
                "name": "!!bad!!",
                "version": "1.0",
            },
            validate=False,
        )
        wheel = metadata_module.Metadata.from_email(HEADER)
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Name") == [ChangeKind.UNKNOWN]

    def test_unparsable_project_urls_from_email_is_unknown(self) -> None:
        # A duplicated label fails Project-URL parsing into a mapping.
        sdist = (
            HEADER
            + "Project-URL: dup, https://a\nProject-URL: dup, https://b\n"
        )
        wheel = HEADER + "Project-URL: Home, https://h\n"
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Project-URL") == ["field-value-unparseable"]

class TestMetadataObjectMalformedBranches:
    def test_invalid_requires_dist_object_is_unknown(self) -> None:
        sdist = metadata_module.Metadata.from_raw(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "requires_dist": ["!!not a requirement!!"],
            },
            validate=False,
        )
        wheel = metadata_module.Metadata.from_email(
            HEADER + "Requires-Dist: foo==1\n"
        )
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.UNKNOWN]

    def test_invalid_version_object_is_unknown(self) -> None:
        sdist = metadata_module.Metadata.from_raw(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "not a version",
            },
            validate=False,
        )
        wheel = metadata_module.Metadata.from_email(HEADER)
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Version") == [ChangeKind.UNKNOWN]


class TestDynamicUnparsed:
    def test_duplicate_dynamic_modern_differs_is_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: A\nDynamic: B\n", HEADER + "Dynamic: A\n"
        )
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Dynamic") == ["pep643-dynamic-field-list"]

    def test_duplicate_dynamic_equal_both_sides_is_clean(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: A\nDynamic: B\n",
            HEADER + "Dynamic: A\nDynamic: B\n",
        )
        assert _find(result.changes, "Dynamic") == []

    def test_duplicate_dynamic_legacy_is_unknown(self) -> None:
        legacy = HEADER.replace("2.2", "2.1")
        result = check_metadata_consistency(
            legacy + "Dynamic: A\nDynamic: B\n", legacy + "Dynamic: A\n"
        )
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Dynamic") == ["pre-2.2-dynamic-field"]


class TestRequiresDistBranches:
    def test_range_equal_despite_canonical_spelling_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Requires-Dist: foo>=1.4.5,==1.4.*\n",
            HEADER + "Requires-Dist: foo~=1.4.5\n",
        )
        assert result.changes == ()

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("foo;python_version>='3.8'", "foo"),
            ("foo", "foo;python_version>='3.8'"),
        ],
    )
    def test_marker_presence_difference_is_violation(
        self, old: str, new: str
    ) -> None:
        result = check_metadata_consistency(
            HEADER + f"Requires-Dist: {old}\n",
            HEADER + f"Requires-Dist: {new}\n",
        )
        assert _kinds(result.changes, "Requires-Dist") == [ChangeKind.VIOLATION]

    def test_canonical_marker_form_is_equal(self) -> None:
        result = check_metadata_consistency(
            HEADER + 'Requires-Dist: foo;extra=="a_b"\n',
            HEADER + 'Requires-Dist: foo;extra=="a-b"\n',
        )
        assert result.changes == ()


class TestAdditionalBranches:
    def test_dynamic_scalar_string_form_from_raw_mapping(self) -> None:
        # A RawMapping (not email) may provide dynamic as a plain string.
        result = check_metadata_consistency(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "dynamic": "Summary",
                "summary": "a",
            },
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "summary": "b",
            },
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]

    def test_requires_python_range_equality_proven(self) -> None:
        # Different canonical spelling but identical accepted set.
        result = check_metadata_consistency(
            HEADER + "Requires-Python: >=1.4.5,==1.4.*\n",
            HEADER + "Requires-Python: ~=1.4.5\n",
        )
        assert _find(result.changes, "Requires-Python") == []

    def test_invalid_extras_identical_is_equal(self) -> None:
        # Unparsable entries that are textually identical are equal.
        result = check_metadata_consistency(
            HEADER + "Provides-Extra: NOT VALID!!\n",
            HEADER + "Provides-Extra: NOT VALID!!\n",
        )
        assert result.changes == ()

    def test_dynamic_project_urls_full_regeneration_allowed(self) -> None:
        # 2.2 full dynamic: the whole mapping may be regenerated.
        result = check_metadata_consistency(
            HEADER
            + "Dynamic: Project-URL\nProject-URL: A, https://a\n"
            + "Project-URL: B, https://b\n",
            HEADER + "Project-URL: C, https://c\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Project-URL") == [
            "pep643-project-urls-dynamic"
        ]

    def test_legacy_project_urls_change_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2") + "Project-URL: A, https://a\n",
            HEADER + "Project-URL: A, https://b\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.UNKNOWN]

    def test_no_metadata_version_in_either_is_clean_for_fields(self) -> None:
        result = check_metadata_consistency(
            "Name: demo\nVersion: 1.0\nSummary: a\n",
            "Name: demo\nVersion: 1.0\nSummary: a\n",
        )
        assert _find(result.changes, "Metadata-Version") == []
        assert result.ok

    def test_metadata_version_present_only_in_wheel_is_violation(self) -> None:
        result = check_metadata_consistency(
            "Name: demo\nVersion: 1.0\n", HEADER
        )
        assert _kinds(result.changes, "Metadata-Version") == [
            ChangeKind.VIOLATION
        ]

    def test_legacy_scalar_change_unknown_not_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2") + "Summary: a\n",
            HEADER.replace("2.2", "1.2") + "Summary: b\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]

    def test_legacy_addition_of_optional_field_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2"),
            HEADER.replace("2.2", "1.2") + "Summary: x\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]

    def test_legacy_removal_of_optional_field_is_unknown(self) -> None:
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2") + "Summary: x\n",
            HEADER.replace("2.2", "1.2"),
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]

    def test_static_element_equal_skipped_in_partial(self) -> None:
        # One static dep kept, one static dep changed: only the changed one is
        # reported under PEP 808 partial semantics.
        result = check_metadata_consistency(
            "Metadata-Version: 2.6\nName: demo\nVersion: 1.0\n"
            "Dynamic: Requires-Dist\nRequires-Dist: keep==1\n"
            "Requires-Dist: change==1\n",
            "Metadata-Version: 2.6\nName: demo\nVersion: 1.0\n"
            "Dynamic: Requires-Dist\nRequires-Dist: keep==1\n"
            "Requires-Dist: change==2\n",
        )
        changes = _find(result.changes, "Requires-Dist")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION
        assert changes[0].old == "change==1"
        assert changes[0].new == "change==2"

    def test_dynamic_field_omitted_legacy_is_unknown(self) -> None:
        # sdist malformed Summary listed in dynamic under legacy metadata.
        result = check_metadata_consistency(
            HEADER.replace("2.2", "1.2")
            + "Dynamic: Summary\nSummary: a\nSummary: b\n",
            HEADER.replace("2.2", "1.2"),
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.UNKNOWN]

    def test_dynamic_field_omitted_full_is_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\nSummary: a\nSummary: b\n",
            HEADER,
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]


class TestUnparsedDynamicViaTuple:
    @staticmethod
    def _doc(dynamic_value: list[str] | None):
        raw = {"metadata_version": "2.2", "name": "demo", "version": "1.0"}
        unparsed = {}
        if dynamic_value is not None:
            unparsed["dynamic"] = dynamic_value
        return raw, unparsed

    def test_unparsed_dynamic_modern_differs_allowed(self) -> None:
        result = check_metadata_consistency(
            self._doc(["!!bad!!"]), self._doc(None)
        )
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Dynamic") == ["pep643-dynamic-field-list"]

    def test_unparsed_dynamic_equal_is_clean(self) -> None:
        result = check_metadata_consistency(
            self._doc(["!!bad!!"]), self._doc(["!!bad!!"])
        )
        assert _find(result.changes, "Dynamic") == []

    def test_unparsed_dynamic_legacy_is_unknown(self) -> None:
        sdist = (
            {"metadata_version": "2.1", "name": "demo", "version": "1.0"},
            {"dynamic": ["!!bad!!"]},
        )
        wheel = (
            {"metadata_version": "2.1", "name": "demo", "version": "1.0"},
            {},
        )
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Dynamic") == [ChangeKind.UNKNOWN]


class TestFinalBranches:
    def test_duplicate_metadata_version_is_unknown(self) -> None:
        result = check_metadata_consistency(
            "Metadata-Version: 2.2\nMetadata-Version: 2.3\n"
            "Name: demo\nVersion: 1.0\n",
            HEADER,
        )
        assert _kinds(result.changes, "Metadata-Version") == [ChangeKind.UNKNOWN]
        assert _ids(result.changes, "Metadata-Version") == [
            "field-value-unparseable"
        ]

    def test_full_dynamic_mapping_removal_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER
            + "Dynamic: Project-URL\nProject-URL: A, https://a\n"
            + "Project-URL: B, https://b\n",
            HEADER + "Dynamic: Project-URL\nProject-URL: A, https://a\n",
        )
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.ALLOWED]


class TestDynamicPresenceBranches:
    def test_full_dynamic_scalar_added_when_sdist_absent(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\n",
            HEADER + "Dynamic: Summary\nSummary: x\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Summary") == ["pep643-scalar-dynamic"]

    def test_full_dynamic_scalar_removed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\nSummary: a\n",
            HEADER + "Dynamic: Summary\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]

    def test_full_dynamic_list_removed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Classifier\nClassifier: A\n",
            HEADER + "Dynamic: Classifier\n",
        )
        assert _kinds(result.changes, "Classifier") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Classifier") == ["pep643-multi-dynamic"]

    def test_partial_dynamic_list_added_when_sdist_absent(self) -> None:
        base = "Metadata-Version: 2.6\nName: demo\nVersion: 1.0\n"
        result = check_metadata_consistency(
            base + "Dynamic: Classifier\n",
            base + "Dynamic: Classifier\nClassifier: A\n",
        )
        assert _kinds(result.changes, "Classifier") == [ChangeKind.ALLOWED]
        assert _ids(result.changes, "Classifier") == [
            "pep808-dynamic-extra-value"
        ]

    def test_full_dynamic_unparsable_added_is_allowed(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Dynamic: Summary\n",
            HEADER + "Dynamic: Summary\nSummary: a\nSummary: b\n",
        )
        assert _kinds(result.changes, "Summary") == [ChangeKind.ALLOWED]


class TestEqualityShortCircuits:
    def test_equal_project_urls_is_clean(self) -> None:
        body = "Project-URL: Home, https://a\nProject-URL: Bugs, https://b\n"
        result = check_metadata_consistency(HEADER + body, HEADER + body)
        assert result.changes == ()

    def test_equal_unparsed_optional_field_is_clean(self) -> None:
        # A field that fails parsing identically on both sides is no change.
        body = "Summary: a\nSummary: b\n"
        result = check_metadata_consistency(HEADER + body, HEADER + body)
        assert result.changes == ()

    def test_equal_scalar_value_is_clean(self) -> None:
        body = "Summary: identical\n"
        result = check_metadata_consistency(HEADER + body, HEADER + body)
        assert result.changes == ()


class TestRawAndObjectEdgeCases:
    def test_list_field_given_as_plain_string_in_raw_mapping(self) -> None:
        # Defensive: a RawMapping may (incorrectly) carry a list field as a
        # scalar; the comparison still handles it as a single value.
        result = check_metadata_consistency(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "classifiers": "A",
            },
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "classifiers": "B",
            },
        )
        # The single value is treated as a one-element list; A -> B is a
        # removal plus an addition, both violations.
        assert _kinds(result.changes, "Classifier") == [
            ChangeKind.VIOLATION,
            ChangeKind.VIOLATION,
        ]

    def test_enriched_metadata_object_mapping_compared(self) -> None:
        sdist = metadata_module.Metadata.from_raw(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "project_urls": {"Home": "https://a"},
            },
            validate=True,
        )
        wheel = metadata_module.Metadata.from_raw(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "project_urls": {"Home": "https://b"},
            },
            validate=True,
        )
        result = check_metadata_consistency(sdist, wheel)
        assert _kinds(result.changes, "Project-URL") == [ChangeKind.VIOLATION]

    def test_required_fields_absent_in_both_is_clean_for_presence(self) -> None:
        result = check_metadata_consistency(
            "Metadata-Version: 2.2\n", "Metadata-Version: 2.2\n"
        )
        assert _find(result.changes, "Name") == []
        assert _find(result.changes, "Version") == []

    def test_empty_dynamic_list_in_both_is_clean(self) -> None:
        result = check_metadata_consistency(
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "dynamic": [],
            },
            {
                "metadata_version": "2.2",
                "name": "demo",
                "version": "1.0",
                "dynamic": [],
            },
        )
        assert result.changes == ()


class TestStaticMappingRemoval:
    def test_static_project_url_entry_removed_violation(self) -> None:
        result = check_metadata_consistency(
            HEADER + "Project-URL: Home, https://a\nProject-URL: Bugs, https://b\n",
            HEADER + "Project-URL: Home, https://a\n",
        )
        changes = _find(result.changes, "Project-URL")
        assert len(changes) == 1
        assert changes[0].kind is ChangeKind.VIOLATION
        assert changes[0].old == {"Bugs": "https://b"}
        assert changes[0].new is None
