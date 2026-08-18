"""Evaluating user-written rules against a photograph's measurements.

Ratings are decided by expressions in the config file, so the tool ships with
opinions you can delete. That means executing strings a user wrote, which is
done here with a small AST interpreter rather than :func:`eval`.

This is not security theatre against a hostile config -- the config is yours. It
is about failing usefully: a whitelist can say "``subject_sharpnes`` is not a
known measurement, did you mean ``subject_acutance``?" at load time, where
``eval`` would raise ``NameError`` on photograph 400 of 800.

Two semantics worth knowing:

* an unknown name is an error when the rules are validated, not at match time
* comparing a missing measurement (``None``) against a number is ``False``, not
  a crash. Half the interesting metrics are absent when no subject was found,
  and every rule mentioning them would otherwise need a guard clause.
"""

from __future__ import annotations

import ast
from typing import Any, Mapping, Sequence

from .errors import ExpressionError
from .models import PhotoReport

# Functions a rule may call. Deliberately tiny: enough to write a clamp or a
# tolerance, not enough to reach the filesystem.
_FUNCTIONS: Mapping[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "len": len,
}

_CONSTANTS: Mapping[str, Any] = {"True": True, "False": False, "None": None}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.List,
    ast.Tuple,
)

_ORDERED = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class Expression:
    """A compiled, validated rule expression."""

    __slots__ = ("source", "_tree")

    def __init__(self, source: str, known_names: Sequence[str]) -> None:
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"cannot parse rule {source!r}: {exc.msg}") from exc

        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ExpressionError(
                    f"rule {source!r} uses {type(node).__name__}, which is not allowed here"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                    raise ExpressionError(
                        f"rule {source!r} may only call: {', '.join(sorted(_FUNCTIONS))}"
                    )
            if isinstance(node, ast.Name) and node.id not in _CONSTANTS and node.id not in _FUNCTIONS:
                if node.id not in known_names:
                    suggestion = _closest(node.id, known_names)
                    hint = f"; did you mean '{suggestion}'?" if suggestion else ""
                    raise ExpressionError(f"rule {source!r} refers to unknown measurement '{node.id}'{hint}")
        self._tree = tree.body

    def evaluate(self, values: Mapping[str, Any]) -> Any:
        return self._eval(self._tree, values)

    def matches(self, values: Mapping[str, Any]) -> bool:
        return bool(self.evaluate(values))

    def _eval(self, node: ast.AST, values: Mapping[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _CONSTANTS:
                return _CONSTANTS[node.id]
            return values.get(node.id)
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, values)
            if isinstance(node.op, ast.Not):
                return not operand
            if operand is None:
                return None
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BoolOp):
            results = (self._eval(v, values) for v in node.values)
            if isinstance(node.op, ast.And):
                return all(results)
            return any(results)
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left, values), self._eval(node.right, values)
            if left is None or right is None:
                return None
            return self._binary(node.op, left, right)
        if isinstance(node, ast.Compare):
            return self._compare(node, values)
        if isinstance(node, ast.IfExp):
            branch = node.body if self._eval(node.test, values) else node.orelse
            return self._eval(branch, values)
        if isinstance(node, ast.Call):
            args = [self._eval(arg, values) for arg in node.args]
            if any(arg is None for arg in args):
                return None
            return _FUNCTIONS[node.func.id](*args)  # type: ignore[attr-defined]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(item, values) for item in node.elts]
        raise ExpressionError(f"unsupported expression node {type(node).__name__}")

    @staticmethod
    def _binary(op: ast.operator, left: Any, right: Any) -> Any:
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right if right else None
        if isinstance(op, ast.Mod):
            return left % right if right else None
        if isinstance(op, ast.Pow):
            return left**right
        raise ExpressionError(f"unsupported operator {type(op).__name__}")

    def _compare(self, node: ast.Compare, values: Mapping[str, Any]) -> bool:
        left = self._eval(node.left, values)
        for operator, comparator in zip(node.ops, node.comparators):
            right = self._eval(comparator, values)
            if isinstance(operator, ast.Is):
                result = left is right
            elif isinstance(operator, ast.IsNot):
                result = left is not right
            elif isinstance(operator, ast.Eq):
                result = left == right
            elif isinstance(operator, ast.NotEq):
                result = left != right
            elif isinstance(operator, ast.In):
                result = right is not None and left in right
            elif isinstance(operator, ast.NotIn):
                result = right is not None and left not in right
            elif isinstance(operator, _ORDERED):
                # A missing measurement compares false rather than exploding, so
                # rules stay readable instead of being padded with None guards.
                if left is None or right is None:
                    return False
                result = self._ordered(operator, left, right)
            else:
                raise ExpressionError(f"unsupported comparison {type(operator).__name__}")

            if not result:
                return False
            left = right
        return True

    @staticmethod
    def _ordered(operator: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        return left >= right


def _closest(name: str, candidates: Sequence[str]) -> str | None:
    """Cheap nearest-name suggestion for typo'd measurements."""
    import difflib

    matches = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


class Rater:
    """Applies an ordered rule set to reports. First matching rule wins."""

    def __init__(self, rules: Sequence[Any], known_names: Sequence[str]) -> None:
        self._rules = list(rules)
        self._expressions = [Expression(rule.when, known_names) for rule in self._rules]

    def apply(self, report: PhotoReport) -> PhotoReport:
        """Return a copy of ``report`` with rating, label and reason filled in."""
        from dataclasses import replace

        values = report.flat_metrics()
        for rule, expression in zip(self._rules, self._expressions):
            if expression.matches(values):
                reason = rule.reason or rule.when
                return replace(
                    report,
                    rating=rule.stars,
                    label=rule.label,
                    reasons=(*report.reasons, reason),
                )
        return report
