"""Evaluate License-Expressions against a caller-supplied license policy.

This module executes *only* the policy it is given. It ships no license
data and encodes no legal conclusions: every license or
``license WITH exception`` combination is allow/deny/unknown purely based
on the caller's policy mapping, and any obligation labels attached to
policy rules are opaque strings that are merely propagated and merged.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, NamedTuple, Sequence

from . import InvalidLicenseExpression, canonicalize_license_expression

__all__ = [
    "Decision",
    "Evaluation",
    "EvaluationStatus",
    "Option",
    "PolicyRule",
    "evaluate_license_policy",
]

DEFAULT_MAX_OPTIONS = 256


class Decision(enum.Enum):
    """The verdict a policy assigns to a license or license+exception pair."""

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PolicyRule:
    """A single policy entry.

    :param decision: Whether the combination is allowed, denied, or unknown.
    :param obligations: Caller-defined obligation labels (e.g.
        ``"attribution"``) that adopting this combination incurs. Labels are
        opaque to the evaluator; they are only merged and compared.
    """

    decision: Decision
    obligations: frozenset[str] = frozenset()


class EvaluationStatus(enum.Enum):
    """The overall outcome of evaluating an expression against a policy."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Option:
    """One viable way to satisfy the expression.

    :param obligations: The merged obligation labels of the adopted branches.
    :param leaves: The policy keys of the adopted branches, in expression
        order. This is the provenance a report needs to point at which
        branch of each ``OR`` was adopted.
    """

    obligations: frozenset[str]
    leaves: tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    """The result of :func:`evaluate_license_policy`.

    :param status: ``ALLOWED`` if at least one fully-allowed combination
        exists, ``DENIED`` if evaluation completed and every combination
        contains a denied branch, otherwise ``UNKNOWN``.
    :param options: The non-dominated allowed options, empty unless the
        status is ``ALLOWED``.
    :param complete: ``False`` when the option budget was exhausted and the
        expansion was cut short. An incomplete evaluation that found no
        allowed option reports ``UNKNOWN`` -- never ``DENIED``.
    :param expression: The normalized expression that was evaluated.
    :param unknown_ids: License/exception combinations encountered that the
        policy does not cover (including ``LicenseRef-*`` ids, which are
        never assumed safe).
    :param denied_ids: Combinations encountered that the policy denies.
    """

    status: EvaluationStatus
    options: tuple[Option, ...]
    complete: bool
    expression: str
    unknown_ids: tuple[str, ...] = ()
    denied_ids: tuple[str, ...] = ()


class _Node(NamedTuple):
    kind: str  # "license" | "and" | "or"
    key: str  # set for "license"
    children: tuple[_Node, ...]  # set for "and"/"or"


@dataclass(frozen=True)
class _Conjunction:
    """A set of leaf judgments combined with AND."""

    obligations: frozenset[str]
    leaves: tuple[str, ...]
    unknowns: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()

    @property
    def viable(self) -> bool:
        return not self.denied and not self.unknowns


_EMPTY = _Conjunction(frozenset(), ())


class _State:
    def __init__(self, max_options: int) -> None:
        self.max_options = max_options
        self.complete = True
        self.unknown_ids: set[str] = set()
        self.denied_ids: set[str] = set()


def _parse(expression: str) -> _Node:
    """Parse a normalized License-Expression into an AND/OR/WITH tree."""
    tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
    position = 0

    def error() -> InvalidLicenseExpression:
        return InvalidLicenseExpression(
            f"Invalid license expression: {expression!r}"
        )

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def parse_or() -> _Node:
        nonlocal position
        children = [parse_and()]
        while peek() == "OR":
            position += 1
            children.append(parse_and())
        if len(children) == 1:
            return children[0]
        return _Node("or", "", tuple(children))

    def parse_and() -> _Node:
        nonlocal position
        children = [parse_with()]
        while peek() == "AND":
            position += 1
            children.append(parse_with())
        if len(children) == 1:
            return children[0]
        return _Node("and", "", tuple(children))

    def parse_with() -> _Node:
        nonlocal position
        token = peek()
        if token is None or token in {"AND", "OR", "WITH", ")"}:
            raise error()
        position += 1
        if token == "(":
            node = parse_or()
            if peek() != ")":
                raise error()
            position += 1
            return node
        key = token
        if peek() == "WITH":
            # WITH binds a single license to a single exception; the pair is
            # one policy key and is never split into two independent lookups.
            position += 1
            exception = peek()
            if exception is None or exception in {"AND", "OR", "WITH", "(", ")"}:
                raise error()
            position += 1
            key = f"{key} WITH {exception}"
        return _Node("license", key, ())

    node = parse_or()
    if position != len(tokens):
        raise error()
    return node


def _leaf(
    key: str, rules: Mapping[str, PolicyRule], state: _State
) -> _Conjunction:
    rule = rules.get(key.lower())
    if rule is None or rule.decision is Decision.UNKNOWN:
        state.unknown_ids.add(key)
        return _Conjunction(frozenset(), (key,), (key,))
    if rule.decision is Decision.DENY:
        state.denied_ids.add(key)
        return _Conjunction(frozenset(), (key,), (), (key,))
    return _Conjunction(frozenset(rule.obligations), (key,))


def _merge(left: _Conjunction, right: _Conjunction) -> _Conjunction:
    return _Conjunction(
        left.obligations | right.obligations,
        left.leaves + right.leaves,
        left.unknowns + right.unknowns,
        left.denied + right.denied,
    )


def _dominance_prune(options: Sequence[_Conjunction]) -> list[_Conjunction]:
    """Drop options whose obligations are a strict superset of another's.

    Options with identical obligations and identical adopted branches are
    duplicates; only the first is kept. Options with equal obligations but
    different branches are all kept so every distinct choice remains
    reportable.
    """
    kept: list[_Conjunction] = []
    seen: set[tuple[frozenset[str], frozenset[str]]] = set()
    for option in options:
        identity = (option.obligations, frozenset(option.leaves))
        if identity in seen:
            continue
        if any(other.obligations < option.obligations for other in options):
            continue
        seen.add(identity)
        kept.append(option)
    return kept


def _prune(options: list[_Conjunction]) -> list[_Conjunction]:
    viable = [option for option in options if option.viable]
    unknown = [
        option for option in options if not option.viable and not option.denied
    ]
    pruned = _dominance_prune(viable) + _dominance_prune(unknown)
    denied = [option for option in options if option.denied]
    if denied:
        # A denied conjunction only poisons its own AND-combination, so a
        # single representative is enough to preserve "denied exists".
        pruned.append(denied[0])
    return pruned


def _limit(options: list[_Conjunction], state: _State) -> None:
    if len(options) > state.max_options:
        state.complete = False
        del options[state.max_options :]


def _evaluate(
    node: _Node, rules: Mapping[str, PolicyRule], state: _State
) -> list[_Conjunction]:
    if node.kind == "license":
        return [_leaf(node.key, rules, state)]
    if node.kind == "or":
        options: list[_Conjunction] = []
        for child in node.children:
            options.extend(_evaluate(child, rules, state))
            options = _prune(options)
            _limit(options, state)
        return options
    # "and"
    combined = [_EMPTY]
    for child in node.children:
        branches = _evaluate(child, rules, state)
        combined = _prune(
            [_merge(acc, branch) for acc in combined for branch in branches]
        )
        _limit(combined, state)
    return combined


def evaluate_license_policy(
    expression: str,
    policy: Mapping[str, PolicyRule | Decision],
    *,
    max_options: int = DEFAULT_MAX_OPTIONS,
) -> Evaluation:
    """Evaluate a License-Expression against a caller-supplied policy.

    :param expression: The License-Expression to evaluate. It is normalized
        with :func:`canonicalize_license_expression` first.
    :param policy: Maps a license id (``"MIT"``) or a license+exception
        combination (``"Apache-2.0 WITH LLVM-exception"``) to a
        :class:`PolicyRule` (or a bare :class:`Decision`, meaning no
        obligations). Keys match case-insensitively. A ``WITH`` pair is
        looked up as a single key and is never decomposed into separate
        license and exception judgments. Anything not present in the policy
        -- including every uncovered ``LicenseRef-*`` -- is unknown, never
        implicitly allowed.
    :param max_options: Budget for the number of candidate options kept
        while expanding ``AND``/``OR`` combinations. If the expansion would
        exceed the budget, it is cut short and the result is marked
        incomplete; an incomplete result that found no allowed option
        reports ``UNKNOWN`` rather than ``DENIED``.
    :raises InvalidLicenseExpression: If the expression is invalid.
    :raises ValueError: If ``max_options`` is less than 1.
    """
    if max_options < 1:
        raise ValueError(f"max_options must be at least 1, got {max_options!r}")

    normalized = canonicalize_license_expression(expression)
    rules: dict[str, PolicyRule] = {}
    for key, value in policy.items():
        rule = value if isinstance(value, PolicyRule) else PolicyRule(value)
        rules[key.lower()] = rule

    state = _State(max_options)
    conjunctions = _evaluate(_parse(normalized), rules, state)

    viable = [option for option in conjunctions if option.viable]
    unknown = [
        option for option in conjunctions if not option.viable and not option.denied
    ]
    if viable:
        status = EvaluationStatus.ALLOWED
    elif unknown or not state.complete:
        status = EvaluationStatus.UNKNOWN
    else:
        status = EvaluationStatus.DENIED

    options = tuple(
        sorted(
            (Option(option.obligations, option.leaves) for option in viable),
            key=lambda option: (
                len(option.obligations),
                sorted(option.obligations),
                option.leaves,
            ),
        )
    )
    return Evaluation(
        status=status,
        options=options,
        complete=state.complete,
        expression=normalized,
        unknown_ids=tuple(sorted(state.unknown_ids)),
        denied_ids=tuple(sorted(state.denied_ids)),
    )
