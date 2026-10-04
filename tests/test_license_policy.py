import pytest

from packaging.licenses import (
    InvalidLicenseExpression,
    LicenseDecision,
    LicenseVerdict,
    PolicyRule,
    evaluate_license_expression,
)

R = PolicyRule


def _plan_strings(evaluation):
    return [plan.branch() for plan in evaluation.allowed_plans]


def test_unknown_license_is_not_allowed_by_default() -> None:
    result = evaluate_license_expression(
        "LicenseRef-Proprietary", {"MIT": R.allow()}
    )
    assert result.verdict is LicenseVerdict.UNKNOWN
    assert result.allowed_plans == ()
    assert [str(choice) for choice in result.unknown] == [
        "LicenseRef-Proprietary"
    ]
    assert result.complete is True


def test_single_allowed_and_denied() -> None:
    allowed = evaluate_license_expression("MIT", {"MIT": R.allow("notice")})
    assert allowed.verdict is LicenseVerdict.ALLOWED
    assert allowed.allowed_plans[0].obligations == frozenset({"notice"})

    denied = evaluate_license_expression(
        "GPL-3.0-only", {"GPL-3.0-only": R.deny()}
    )
    assert denied.verdict is LicenseVerdict.DENIED
    assert [str(choice) for choice in denied.denied] == ["GPL-3.0-only"]

    explicit_unknown = evaluate_license_expression(
        "MIT", {"MIT": R.unknown()}
    )
    assert explicit_unknown.verdict is LicenseVerdict.UNKNOWN


def test_bare_decision_policy_values() -> None:
    result = evaluate_license_expression(
        "MIT OR GPL-3.0-only",
        {
            "MIT": LicenseDecision.ALLOW,
            "GPL-3.0-only": LicenseDecision.DENY,
        },
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    assert _plan_strings(result) == ["MIT"]


def test_and_merges_obligations() -> None:
    result = evaluate_license_expression(
        "MIT AND Apache-2.0",
        {
            "MIT": R.allow("notice"),
            "Apache-2.0": R.allow("notice", "license-file"),
        },
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    plan = result.allowed_plans[0]
    assert plan.obligations == frozenset({"notice", "license-file"})
    assert plan.branch() == "MIT AND Apache-2.0"


def test_and_is_denied_if_any_branch_denied() -> None:
    result = evaluate_license_expression(
        "MIT AND GPL-3.0-only",
        {"MIT": R.allow(), "GPL-3.0-only": R.deny()},
    )
    assert result.verdict is LicenseVerdict.DENIED
    assert result.allowed_plans == ()
    assert [str(choice) for choice in result.denied] == ["GPL-3.0-only"]


def test_and_with_unknown_branch_is_unknown() -> None:
    result = evaluate_license_expression(
        "MIT AND LicenseRef-Internal", {"MIT": R.allow()}
    )
    assert result.verdict is LicenseVerdict.UNKNOWN
    assert [str(choice) for choice in result.unknown] == [
        "LicenseRef-Internal"
    ]


def test_or_produces_alternative_plans() -> None:
    result = evaluate_license_expression(
        "GPL-3.0-only OR MIT OR LicenseRef-X",
        {"GPL-3.0-only": R.deny(), "MIT": R.allow("notice")},
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    assert _plan_strings(result) == ["MIT"]
    assert [str(choice) for choice in result.denied] == ["GPL-3.0-only"]
    assert [str(choice) for choice in result.unknown] == ["LicenseRef-X"]


def test_nested_and_or_distributes_into_plans() -> None:
    policy = {
        "MIT": R.allow("notice"),
        "Apache-2.0": R.allow("notice", "license-file"),
        "BSD-2-Clause": R.allow("notice"),
        "ISC": R.allow("notice"),
        "GPL-3.0-only": R.deny(),
    }
    result = evaluate_license_expression(
        "(MIT OR GPL-3.0-only) AND (Apache-2.0 OR BSD-2-Clause)", policy
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    # MIT+Apache carries {notice, license-file} and is dominated by
    # MIT+BSD which carries the strict subset {notice}.
    assert _plan_strings(result) == ["MIT AND BSD-2-Clause"]
    assert result.allowed_plans[0].obligations == frozenset({"notice"})
    assert result.plans_examined == 2

    # With incomparable obligations both complete selections survive.
    incomparable = evaluate_license_expression(
        "(MIT OR ISC) AND Apache-2.0", policy
    )
    assert _plan_strings(incomparable) == [
        "ISC AND Apache-2.0",
        "MIT AND Apache-2.0",
    ]


def test_dominated_plan_is_removed_but_equal_plans_kept() -> None:
    policy = {
        "MIT": R.allow("notice"),
        "Apache-2.0": R.allow("notice", "state-changes"),
        "BSD-2-Clause": R.allow("notice"),
    }
    result = evaluate_license_expression(
        "Apache-2.0 OR (MIT AND BSD-2-Clause)", policy
    )
    # Apache-2.0 carries {notice, state-changes}; the AND branch carries the
    # strict subset {notice}, so Apache-2.0 is dominated and removed.
    assert _plan_strings(result) == ["MIT AND BSD-2-Clause"]

    # Equal obligation sets are *not* dominance: both branches survive so a
    # report can state which one was adopted.
    equal = evaluate_license_expression(
        "MIT OR BSD-2-Clause", policy
    )
    assert _plan_strings(equal) == ["BSD-2-Clause", "MIT"]


def test_with_exception_is_one_atomic_decision() -> None:
    policy = {"MIT": R.allow("notice")}
    # The bare-MIT rule does not cover the WITH combination.
    result = evaluate_license_expression(
        "MIT WITH Classpath-exception-2.0", policy
    )
    assert result.verdict is LicenseVerdict.UNKNOWN
    assert [str(choice) for choice in result.unknown] == [
        "MIT WITH Classpath-exception-2.0"
    ]

    policy["MIT WITH Classpath-exception-2.0"] = R.allow(
        "notice", "cp-exception"
    )
    explicit = evaluate_license_expression(
        "MIT WITH Classpath-exception-2.0", policy
    )
    assert explicit.verdict is LicenseVerdict.ALLOWED
    from packaging.licenses import LicenseLeaf

    plan = explicit.allowed_plans[0]
    assert plan.leaves == (LicenseLeaf("MIT", "Classpath-exception-2.0"),)
    assert plan.obligations == frozenset({"notice", "cp-exception"})


def test_with_inside_or_and_and() -> None:
    policy = {
        "MIT": R.allow("notice"),
        "MIT WITH Classpath-exception-2.0": R.allow("notice", "cp"),
        "Apache-2.0": R.allow("notice", "license-file"),
    }
    result = evaluate_license_expression(
        "(MIT WITH Classpath-exception-2.0 OR MIT) AND Apache-2.0", policy
    )
    # The WITH branch adds "cp" on top of the bare branch's obligations, so
    # it is dominated and removed; the bare-MIT branch is retained.
    assert _plan_strings(result) == ["MIT AND Apache-2.0"]

    # The WITH combination survives alone when the bare license is denied.
    policy["MIT"] = R.deny()
    only_with = evaluate_license_expression(
        "(MIT WITH Classpath-exception-2.0 OR MIT) AND Apache-2.0", policy
    )
    assert _plan_strings(only_with) == [
        "MIT WITH Classpath-exception-2.0 AND Apache-2.0"
    ]
    assert only_with.allowed_plans[0].obligations == frozenset(
        {"cp", "license-file", "notice"}
    )


def test_nested_unknown_reference_never_defaults_safe() -> None:
    result = evaluate_license_expression(
        "(MIT OR Apache-2.0) AND LicenseRef-Mystery",
        {"MIT": R.allow(), "Apache-2.0": R.allow()},
    )
    assert result.verdict is LicenseVerdict.UNKNOWN
    assert result.allowed_plans == ()
    assert [str(choice) for choice in result.unknown] == [
        "LicenseRef-Mystery"
    ]


def test_duplicate_or_branches_deduplicate() -> None:
    result = evaluate_license_expression(
        "MIT OR MIT OR (MIT)", {"MIT": R.allow("notice")}
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    assert _plan_strings(result) == ["MIT"]


def test_and_with_duplicated_or_branch_deduplicates() -> None:
    result = evaluate_license_expression(
        "(MIT OR MIT) AND Apache-2.0",
        {"MIT": R.allow(), "Apache-2.0": R.allow()},
    )
    assert _plan_strings(result) == ["MIT AND Apache-2.0"]


def test_budget_marks_result_incomplete_without_claiming_denied() -> None:
    licenses = [f"LicenseRef-Allowed-{i}" for i in range(8)]
    policy = {lic: R.allow() for lic in licenses}
    expression = " OR ".join(licenses)

    result = evaluate_license_expression(expression, policy, max_plans=3)
    assert result.verdict is LicenseVerdict.INCOMPLETE
    assert result.complete is False
    assert len(result.allowed_plans) == 3
    assert _plan_strings(result) == licenses[:3]

    # Even when no allowed branch fits in the budget, truncation is reported
    # as incomplete rather than denied.
    denied_policy = {
        lic: LicenseDecision.DENY for lic in licenses[:3]
    }
    denied_result = evaluate_license_expression(
        expression, denied_policy, max_plans=3
    )
    assert denied_result.verdict is LicenseVerdict.INCOMPLETE
    assert denied_result.allowed_plans == ()
    assert denied_result.complete is False


def test_budget_for_and_cross_product() -> None:
    left = [f"LicenseRef-A-{i}" for i in range(4)]
    right = [f"LicenseRef-B-{i}" for i in range(4)]
    policy = {lic: R.allow() for lic in left + right}
    expression = f"({' OR '.join(left)}) AND ({' OR '.join(right)})"

    result = evaluate_license_expression(expression, policy, max_plans=5)
    assert result.verdict is LicenseVerdict.INCOMPLETE
    assert len(result.allowed_plans) == 5
    # Sixteen allowed selections exist; only the budget truncated enumeration.
    assert result.plans_examined == 16

    full = evaluate_license_expression(expression, policy)
    assert full.verdict is LicenseVerdict.ALLOWED
    assert full.complete is True
    assert len(full.allowed_plans) == 16


def test_policy_keys_are_canonicalized() -> None:
    result = evaluate_license_expression(
        "mit with classpath-exception-2.0 or apache-2.0",
        {
            "mit with classpath-exception-2.0": R.allow("cp"),
            "apache-2.0": R.deny(),
        },
    )
    assert result.verdict is LicenseVerdict.ALLOWED
    assert _plan_strings(result) == [
        "MIT WITH Classpath-exception-2.0"
    ]


def test_invalid_expression_is_rejected() -> None:
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_expression("(MIT OR Apache-2.0", {"MIT": R.allow()})
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_expression("MIT WITH", {"MIT": R.allow()})
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_expression(
            "(MIT OR Apache-2.0) WITH Classpath-exception-2.0",
            {"MIT": R.allow()},
        )
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_expression("", {"MIT": R.allow()})


def test_invalid_policy_construction() -> None:
    with pytest.raises(TypeError):
        evaluate_license_expression("MIT", {"MIT": "allowed"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="max_plans"):
        evaluate_license_expression("MIT", {"MIT": R.allow()}, max_plans=0)
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_license_expression(
            "MIT", {"mit": R.allow(), "MIT": R.allow()}
        )
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_expression("MIT", {"bogus-license": R.allow()})
