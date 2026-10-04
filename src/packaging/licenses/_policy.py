# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
"""Evaluate normalized License-Expressions against an explicit caller policy.

The evaluator never decides on its own whether a license is legally safe to
use. It only applies the rules handed to it by the caller: any license (or
``license WITH exception`` combination) that the policy does not name is
reported as :attr:`~LicenseDecision.UNKNOWN`, never implicitly allowed.

The possible outcomes of a License-Expression are enumerated as *plans*. A
plan is one complete selection of branches for every ``OR`` in the
expression. ``AND`` merges the selected branches and the union of their
obligation labels, while ``OR`` produces alternative plans. A ``WITH``
exception is part of the license it attaches to and is always looked up as
one atomic key; it is never split into two separate license decisions.

Enumeration is bounded by an explicit plan budget. When the budget is
exhausted the result is marked incomplete, so a report never claims that no
acceptable branch exists merely because enumeration was truncated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum


__all__ = [
    "LicenseChoice",
    "LicenseDecision",
    "LicenseEvaluation",
    "LicenseLeaf",
    "LicensePlan",
    "LicenseVerdict",
    "Policy",
    "PolicyRule",
    "evaluate_license_expression",
]


class LicenseDecision(Enum):
    """The decision a :class:`PolicyRule` records for a single license."""

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


class LicenseVerdict(Enum):
    """The overall verdict for an evaluated License-Expression."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PolicyRule:
    """A single caller-supplied policy rule.

    A rule binds one decision to a license or ``license WITH exception``
    combination and carries the obligation labels the caller wants reported on
    every plan that uses an allowed match. Obligation labels are opaque
    strings; the evaluator only unions and compares them.
    """

    decision: LicenseDecision
    obligations: frozenset[str] = frozenset()

    @classmethod
    def allow(cls, *obligations: str) -> PolicyRule:
        """Create an :attr:`~LicenseDecision.ALLOW` rule."""
        return cls(LicenseDecision.ALLOW, frozenset(obligations))

    @classmethod
    def deny(cls) -> PolicyRule:
        """Create a :attr:`~LicenseDecision.DENY` rule."""
        return cls(LicenseDecision.DENY)

    @classmethod
    def unknown(cls) -> PolicyRule:
        """Create an :attr:`~LicenseDecision.UNKNOWN` rule."""
        return cls(LicenseDecision.UNKNOWN)


@dataclass(frozen=True)
class LicenseLeaf:
    """One atomic license selection within a plan.

    ``license`` is the (canonical) license identifier, possibly carrying its
    SPDX ``+`` suffix. When ``exception`` is set, the pair was written as
    ``license WITH exception`` in the expression and is treated as a single,
    indivisible decision.
    """

    license: str
    exception: str | None = None

    @property
    def key(self) -> str:
        """The key this leaf is looked up under in a policy table."""
        if self.exception is None:
            return self.license
        return f"{self.license} WITH {self.exception}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class LicenseChoice:
    """The policy outcome of one selected :class:`LicenseLeaf`."""

    leaf: LicenseLeaf
    decision: LicenseDecision
    obligations: frozenset[str] = frozenset()

    def __str__(self) -> str:
        return self.leaf.key


@dataclass(frozen=True)
class LicensePlan:
    """One complete selection of OR-branches that satisfies the expression.

    ``choices`` are ordered as the licenses appear in the expression. The
    :meth:`branch` rendering records which OR-branch was taken so a report can
    state exactly what the plan relies on.
    """

    choices: tuple[LicenseChoice, ...]

    @property
    def obligations(self) -> frozenset[str]:
        """The union of every chosen branch's obligation labels."""
        merged: set[str] = set()
        for choice in self.choices:
            merged.update(choice.obligations)
        return frozenset(merged)

    @property
    def leaves(self) -> tuple[LicenseLeaf, ...]:
        """The licenses this plan selected."""
        return tuple(choice.leaf for choice in self.choices)

    def branch(self) -> str:
        """Render the selected branch as a normalized conjunction."""
        return " AND ".join(str(choice) for choice in self.choices)

    def __str__(self) -> str:
        return self.branch()


@dataclass(frozen=True)
class LicenseEvaluation:
    """The assembled outcome of one evaluation.

    ``plans_examined`` is the number of complete branch selections in which
    every chosen license was allowed, counted across the whole expression
    before dominance pruning (and even when the retained budget was hit);
    ``complete`` is ``False`` when enumeration stopped because that budget was
    exhausted.
    """

    verdict: LicenseVerdict
    allowed_plans: tuple[LicensePlan, ...] = ()
    denied: tuple[LicenseChoice, ...] = ()
    unknown: tuple[LicenseChoice, ...] = ()
    plans_examined: int = 0
    complete: bool = True

    @property
    def incomplete(self) -> bool:
        """Whether enumeration stopped because the plan budget was exhausted."""
        return not self.complete


# ---------------------------------------------------------------------------
# Parsing of a *normalized* expression into the simple grammar
#
#   expression := or-expression
#   or-expr    := and-expr ("OR" and-expr)*
#   and-expr   := with-expr ("AND" with-expr)*
#   with-expr  := atom ("WITH" exception-id)?
#   atom       := license-id | "(" expression ")"
#
# Inputs are expected to come from canonicalize_license_expression(); the
# parser still validates syntax rather than trusting that silently.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Leaf:
    leaf: LicenseLeaf


@dataclass(frozen=True)
class _AllOf:
    operands: tuple[_Node, ...]


@dataclass(frozen=True)
class _AnyOf:
    operands: tuple[_Node, ...]


_Node = _Leaf | _AllOf | _AnyOf


def _parse(tokens: Sequence[str], raw: str) -> _Node:
    def invalid() -> Exception:
        return ValueError(f"Invalid license expression: {raw!r}")

    index = 0

    def peek() -> str | None:
        return tokens[index] if index < len(tokens) else None

    def advance() -> str:
        nonlocal index
        token = tokens[index]
        index += 1
        return token

    def parse_expression() -> _Node:
        node = parse_or()
        if index != len(tokens):
            raise invalid()
        return node

    def parse_or() -> _Node:
        operands = [parse_and()]
        while peek() == "OR":
            advance()
            operands.append(parse_and())
        if len(operands) == 1:
            return operands[0]
        return _AnyOf(tuple(operands))

    def parse_and() -> _Node:
        operands = [parse_with()]
        while peek() == "AND":
            advance()
            operands.append(parse_with())
        if len(operands) == 1:
            return operands[0]
        return _AllOf(tuple(operands))

    def parse_with() -> _Node:
        node = parse_atom()
        if peek() != "WITH":
            return node
        advance()
        exception_token = peek()
        if (
            exception_token is None
            or exception_token in {"AND", "OR", "WITH", "(", ")"}
        ):
            raise invalid()
        advance()
        if not isinstance(node, _Leaf):
            raise invalid()
        return _Leaf(LicenseLeaf(node.leaf.license, exception_token))

    def parse_atom() -> _Node:
        token = peek()
        if token is None:
            raise invalid()
        if token == "(":
            advance()
            node = parse_or()
            if peek() != ")":
                raise invalid()
            advance()
            return node
        if token in {"AND", "OR", "WITH", ")"}:
            raise invalid()
        advance()
        return _Leaf(LicenseLeaf(token))

    return parse_expression()


def _parse_license_expression(expression: str) -> _Node:
    tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
    if not tokens:
        raise ValueError(f"Invalid license expression: {expression!r}")
    return _parse(tokens, expression)


# A term is one complete DNF selection: one tuple of atomic leaf choices.
# A term is adoptable only if every choice in it is ALLOW; the other terms are
# still enumerated so the report can explain why branches were rejected and,
# crucially, so the budget can tell whether branches were left unexamined.
_Term = tuple[LicenseChoice, ...]


@dataclass
class _Outcome:
    # Complete selections retained within the budget (all decisions mixed).
    terms: list[_Term] = field(default_factory=list)
    # How many of the *complete* selections were allowed, including ones beyond
    # the retained budget. This is what plans_examined reports.
    allowed_count: int = 0
    denied: list[LicenseChoice] = field(default_factory=list)
    unknown: list[LicenseChoice] = field(default_factory=list)
    complete: bool = True


def _append_unique(choices: list[LicenseChoice], choice: LicenseChoice) -> None:
    if all(existing.leaf.key != choice.leaf.key for existing in choices):
        choices.append(choice)


def _term_allowed(term: _Term) -> bool:
    return all(choice.decision is LicenseDecision.ALLOW for choice in term)


def _prune_dominated(plans: Sequence[LicensePlan]) -> list[LicensePlan]:
    """Drop a plan when another plan satisfies a strict subset of obligations.

    Plans with equal obligation sets are all kept: they select different
    branches and the report may need either as the basis for a decision.
    """
    kept: list[LicensePlan] = []
    for candidate in plans:
        candidate_obligations = candidate.obligations
        dominated = any(
            other.obligations < candidate_obligations for other in plans
        )
        if not dominated:
            kept.append(candidate)
    kept.sort(key=lambda plan: (sorted(plan.obligations), plan.branch()))
    return kept


def _leaf_outcome(leaf: LicenseLeaf, rules: dict[str, PolicyRule]) -> _Outcome:
    rule = rules.get(leaf.key)
    if rule is None:
        # No caller-supplied rule: never presume the license is safe.
        rule = PolicyRule(LicenseDecision.UNKNOWN)
    choice = LicenseChoice(leaf, rule.decision, frozenset(rule.obligations))
    outcome = _Outcome(
        terms=[(choice,)],
        allowed_count=1 if rule.decision is LicenseDecision.ALLOW else 0,
    )
    if rule.decision is LicenseDecision.DENY:
        outcome.denied = [choice]
    elif rule.decision is LicenseDecision.UNKNOWN:
        outcome.unknown = [choice]
    return outcome


def _evaluate_node(
    node: _Node, rules: dict[str, PolicyRule], max_plans: int
) -> _Outcome:
    if isinstance(node, _Leaf):
        return _leaf_outcome(node.leaf, rules)

    children = [
        _evaluate_node(operand, rules, max_plans)
        for operand in node.operands
    ]
    outcome = _Outcome(complete=all(child.complete for child in children))

    for child in children:
        for choice in child.denied:
            _append_unique(outcome.denied, choice)
        for choice in child.unknown:
            _append_unique(outcome.unknown, choice)

    if isinstance(node, _AnyOf):
        seen: set[tuple[LicenseLeaf, ...]] = set()
        for child in children:
            outcome.allowed_count += child.allowed_count
            for term in child.terms:
                signature = tuple(choice.leaf for choice in term)
                if signature in seen:
                    continue
                if len(outcome.terms) >= max_plans:
                    outcome.complete = False
                    break
                seen.add(signature)
                outcome.terms.append(term)
            if not outcome.complete:
                break
        return outcome

    # _AllOf: every child contributes one selected branch to each term.
    terms: list[_Term] = [()]
    for child in children:
        combined: list[_Term] = []
        for base in terms:
            for child_term in child.terms:
                if len(combined) >= max_plans:
                    break
                combined.append(base + child_term)
            if len(combined) >= max_plans:
                break
        terms = combined

    # Count allowed selections without the retained-budget cap.
    allowed_factor = 1
    total_factor = 1
    for child in children:
        allowed_factor *= child.allowed_count
        total_factor *= len(child.terms)
    outcome.allowed_count = allowed_factor
    if total_factor > max_plans:
        outcome.complete = False
    unique: list[_Term] = []
    and_seen: set[tuple[LicenseLeaf, ...]] = set()
    for term in terms:
        signature = tuple(choice.leaf for choice in term)
        if signature not in and_seen:
            and_seen.add(signature)
            unique.append(term)
    outcome.terms = unique
    return outcome


# Policy construction accepts either a ready rule or a plain decision.
Policy = dict[str, PolicyRule | LicenseDecision]


def _compile_rule(value: PolicyRule | LicenseDecision) -> PolicyRule:
    if isinstance(value, PolicyRule):
        return value
    if isinstance(value, LicenseDecision):
        return PolicyRule(value)
    raise TypeError(
        "policy values must be PolicyRule or LicenseDecision, "
        f"not {type(value).__name__}"
    )


def _compile_policy(policy: Policy) -> dict[str, PolicyRule]:
    from . import canonicalize_license_expression  # noqa: PLC0415

    compiled: dict[str, PolicyRule] = {}
    for raw_key, value in policy.items():
        key = canonicalize_license_expression(raw_key)
        rule = _compile_rule(value)
        if key in compiled:
            raise ValueError(f"Duplicate policy entry: {key}")
        compiled[key] = rule
    return compiled


def evaluate_license_expression(
    expression: str,
    policy: Policy,
    *,
    max_plans: int = 256,
) -> LicenseEvaluation:
    """Evaluate a normalized License-Expression against an explicit policy.

    :param expression: A License-Expression, as produced (or accepted) by
        :func:`canonicalize_license_expression`.
    :param policy: A mapping from a license or ``license WITH exception`` key
        to a :class:`PolicyRule` (or a bare :class:`LicenseDecision`). Keys are
        canonicalized the same way expressions are. Any license the policy
        does not name is :attr:`~LicenseDecision.UNKNOWN`; nothing is allowed
        by default.
    :param max_plans: Budget on how many complete branch selections to
        enumerate. When the expression offers more plans than this, results
        are returned with :attr:`~LicenseEvaluation.complete` set to
        ``False`` instead of silently treating the uncounted plans as absent.
    :raises InvalidLicenseExpression: If the expression is syntactically
        invalid.
    """
    if max_plans < 1:
        raise ValueError("max_plans must be at least 1")

    from . import canonicalize_license_expression  # noqa: PLC0415

    normalized = canonicalize_license_expression(expression)
    root = _parse_license_expression(normalized)
    rules = _compile_policy(policy)
    outcome = _evaluate_node(root, rules, max_plans)

    plans = [
        LicensePlan(term) for term in outcome.terms if _term_allowed(term)
    ]
    allowed_plans = tuple(_prune_dominated(plans))

    if allowed_plans:
        verdict = (
            LicenseVerdict.INCOMPLETE
            if not outcome.complete
            else LicenseVerdict.ALLOWED
        )
    elif not outcome.complete:
        verdict = LicenseVerdict.INCOMPLETE
    elif outcome.unknown:
        verdict = LicenseVerdict.UNKNOWN
    else:
        verdict = LicenseVerdict.DENIED

    return LicenseEvaluation(
        verdict=verdict,
        allowed_plans=allowed_plans,
        denied=tuple(outcome.denied),
        unknown=tuple(outcome.unknown),
        plans_examined=outcome.allowed_count,
        complete=outcome.complete,
    )
