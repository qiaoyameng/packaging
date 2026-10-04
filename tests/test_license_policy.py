from __future__ import annotations

import pytest

from packaging.licenses import (
    Decision,
    EvaluationStatus,
    InvalidLicenseExpression,
    PolicyRule,
    evaluate_license_policy,
)

ALLOW = PolicyRule(Decision.ALLOW)
DENY = PolicyRule(Decision.DENY)


def test_simple_allow_with_obligations() -> None:
    result = evaluate_license_policy(
        "mit", {"MIT": PolicyRule(Decision.ALLOW, frozenset({"attribution"}))}
    )
    assert result.status is EvaluationStatus.ALLOWED
    assert result.complete
    assert result.expression == "MIT"
    assert len(result.options) == 1
    assert result.options[0].obligations == frozenset({"attribution"})
    assert result.options[0].leaves == ("MIT",)


def test_and_merges_obligations() -> None:
    policy = {
        "MIT": PolicyRule(Decision.ALLOW, frozenset({"attribution"})),
        "Apache-2.0": PolicyRule(Decision.ALLOW, frozenset({"attribution", "patent-grant"})),
    }
    result = evaluate_license_policy("MIT AND Apache-2.0", policy)
    assert result.status is EvaluationStatus.ALLOWED
    assert len(result.options) == 1
    assert result.options[0].obligations == frozenset({"attribution", "patent-grant"})
    assert result.options[0].leaves == ("MIT", "Apache-2.0")


def test_or_produces_alternatives_and_prunes_dominated() -> None:
    policy = {
        "MIT": PolicyRule(Decision.ALLOW, frozenset({"attribution"})),
        "Apache-2.0": PolicyRule(
            Decision.ALLOW, frozenset({"attribution", "patent-grant"})
        ),
        "BSD-2-Clause": PolicyRule(Decision.ALLOW, frozenset({"notice"})),
    }
    result = evaluate_license_policy("MIT OR Apache-2.0 OR BSD-2-Clause", policy)
    assert result.status is EvaluationStatus.ALLOWED
    obligation_sets = {option.obligations for option in result.options}
    # Apache-2.0's obligations are a strict superset of MIT's: dominated.
    assert obligation_sets == {frozenset({"attribution"}), frozenset({"notice"})}


def test_or_keeps_equal_cost_distinct_branches() -> None:
    policy = {"MIT": ALLOW, "ISC": ALLOW}
    result = evaluate_license_policy("MIT OR ISC", policy)
    assert result.status is EvaluationStatus.ALLOWED
    assert {option.leaves for option in result.options} == {("MIT",), ("ISC",)}


def test_nested_and_or_reports_adopted_branch() -> None:
    policy = {
        "MIT": PolicyRule(Decision.ALLOW, frozenset({"attribution"})),
        "Apache-2.0": PolicyRule(
            Decision.ALLOW, frozenset({"attribution", "patent-grant"})
        ),
        "BSD-2-Clause": PolicyRule(Decision.ALLOW, frozenset({"notice"})),
        "GPL-3.0-only": DENY,
    }
    result = evaluate_license_policy(
        "(MIT OR Apache-2.0) AND (BSD-2-Clause OR GPL-3.0-only)", policy
    )
    assert result.status is EvaluationStatus.ALLOWED
    assert result.complete
    leaves = {option.leaves for option in result.options}
    # The GPL branch is denied, and MIT dominates Apache-2.0 on obligations.
    assert leaves == {("MIT", "BSD-2-Clause")}
    option = result.options[0]
    assert option.obligations == frozenset({"attribution", "notice"})
    assert result.denied_ids == ("GPL-3.0-only",)


def test_or_with_one_denied_branch_still_allowed() -> None:
    policy = {"MIT": ALLOW, "GPL-3.0-only": DENY}
    result = evaluate_license_policy("GPL-3.0-only OR MIT", policy)
    assert result.status is EvaluationStatus.ALLOWED
    assert [option.leaves for option in result.options] == [("MIT",)]


def test_all_denied_is_denied() -> None:
    policy = {"GPL-3.0-only": DENY, "AGPL-3.0-only": DENY}
    result = evaluate_license_policy("GPL-3.0-only AND AGPL-3.0-only", policy)
    assert result.status is EvaluationStatus.DENIED
    assert result.complete
    assert result.options == ()
    assert set(result.denied_ids) == {"GPL-3.0-only", "AGPL-3.0-only"}


def test_with_combination_is_a_single_policy_key() -> None:
    policy = {
        "Apache-2.0": DENY,
        "Apache-2.0 WITH LLVM-exception": ALLOW,
    }
    result = evaluate_license_policy("Apache-2.0 WITH LLVM-exception", policy)
    assert result.status is EvaluationStatus.ALLOWED
    assert result.options[0].leaves == ("Apache-2.0 WITH LLVM-exception",)


def test_with_combination_is_not_split_into_license_and_exception() -> None:
    # The bare license is allowed, but the license+exception pair is not in
    # the policy: the pair must not inherit the bare license's verdict.
    policy = {"Apache-2.0": ALLOW}
    result = evaluate_license_policy("Apache-2.0 WITH LLVM-exception", policy)
    assert result.status is EvaluationStatus.UNKNOWN
    assert result.unknown_ids == ("Apache-2.0 WITH LLVM-exception",)


def test_with_combination_carries_its_own_obligations() -> None:
    policy = {
        "GPL-2.0-only WITH Classpath-exception-2.0": PolicyRule(
            Decision.ALLOW, frozenset({"copyleft", "exception-notice"})
        )
    }
    result = evaluate_license_policy(
        "GPL-2.0-only WITH Classpath-exception-2.0", policy
    )
    assert result.status is EvaluationStatus.ALLOWED
    assert result.options[0].obligations == frozenset(
        {"copyleft", "exception-notice"}
    )


def test_unknown_licenseref_is_not_assumed_safe() -> None:
    result = evaluate_license_policy("LicenseRef-Custom", {"MIT": ALLOW})
    assert result.status is EvaluationStatus.UNKNOWN
    assert result.options == ()
    assert result.unknown_ids == ("LicenseRef-Custom",)


def test_unknown_in_and_chain_poisons_the_combination() -> None:
    policy = {"MIT": ALLOW}
    result = evaluate_license_policy("MIT AND LicenseRef-Custom", policy)
    assert result.status is EvaluationStatus.UNKNOWN
    assert result.options == ()


def test_unknown_or_branch_falls_back_to_allowed_branch() -> None:
    policy = {"MIT": ALLOW}
    result = evaluate_license_policy("LicenseRef-Custom OR MIT", policy)
    assert result.status is EvaluationStatus.ALLOWED
    assert [option.leaves for option in result.options] == [("MIT",)]
    assert result.unknown_ids == ("LicenseRef-Custom",)


def test_explicit_unknown_decision() -> None:
    policy = {"MIT": PolicyRule(Decision.UNKNOWN)}
    result = evaluate_license_policy("MIT", policy)
    assert result.status is EvaluationStatus.UNKNOWN
    assert result.unknown_ids == ("MIT",)


def test_bare_decision_values_and_case_insensitive_keys() -> None:
    result = evaluate_license_policy("mit AND bsd-2-clause", {
        "mit": Decision.ALLOW,
        "BSD-2-Clause": Decision.ALLOW,
    })
    assert result.status is EvaluationStatus.ALLOWED
    assert result.expression == "MIT AND BSD-2-Clause"


def test_budget_marks_incomplete_and_caps_options() -> None:
    pairs = [
        ("MIT", "ISC"),
        ("BSD-2-Clause", "BSD-3-Clause"),
        ("Apache-2.0", "0BSD"),
        ("AFL-3.0", "APL-1.0"),
        ("Artistic-2.0", "Beerware"),
    ]
    policy = {
        license_id: PolicyRule(Decision.ALLOW, frozenset({f"ob-{license_id}"}))
        for pair in pairs
        for license_id in pair
    }
    expression = " AND ".join(f"({left} OR {right})" for left, right in pairs)
    result = evaluate_license_policy(expression, policy, max_options=8)
    # 2**5 = 32 combinations, but the budget caps the expansion.
    assert not result.complete
    assert len(result.options) <= 8
    # Allowed options already found remain valid.
    assert result.status is EvaluationStatus.ALLOWED


def test_budget_exhaustion_never_claims_denied() -> None:
    # Every leaf is unknown to the policy; with a tiny budget the expansion
    # is cut short, and the result must not claim there is no allowed option.
    pairs = [
        ("MIT", "ISC"),
        ("BSD-2-Clause", "BSD-3-Clause"),
        ("Apache-2.0", "0BSD"),
    ]
    expression = " AND ".join(f"({left} OR {right})" for left, right in pairs)
    result = evaluate_license_policy(expression, {}, max_options=2)
    assert not result.complete
    assert result.status is EvaluationStatus.UNKNOWN
    assert result.options == ()


def test_complete_evaluation_with_no_viable_option_is_denied() -> None:
    policy = {"GPL-3.0-only": DENY, "AGPL-3.0-only": DENY}
    result = evaluate_license_policy(
        "GPL-3.0-only OR AGPL-3.0-only", policy, max_options=1
    )
    assert result.complete
    assert result.status is EvaluationStatus.DENIED


def test_invalid_expression_raises() -> None:
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_policy("(MIT", {"MIT": ALLOW})
    with pytest.raises(InvalidLicenseExpression):
        evaluate_license_policy("Not-A-Real-License", {})


def test_max_options_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_options"):
        evaluate_license_policy("MIT", {"MIT": ALLOW}, max_options=0)
