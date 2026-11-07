"""The safe rule and formula language."""
from datetime import date
from decimal import Decimal

import pytest

from core.expressions import ExpressionError, check_condition, evaluate, validate, variables_used

VARS = {
    "employee": {"grade": "M1", "joined_on": "2025-01-15", "state": "KA", "age": 30, "tags": ["remote", "tech"]},
    "request": {"days": 4, "type": "casual", "amount": Decimal("52000.50")},
    "basic": 25000,
}
TODAY = date(2026, 11, 7)


def ev(src, **kw):
    return evaluate(src, VARS, today=TODAY, **kw)


@pytest.mark.parametrize("src,expected", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("10 / 4", Decimal("2.5")), ("10 % 4", 2), ("-3 + 5", 2),
    ("2 * -3", -6), ("round(basic * 0.4, 0)", 10000), ("round(2.5)", 3), ("round(1.005, 2)", Decimal("1.01")),
    ("floor(2.7) + ceil(2.1)", 5), ("abs(-4)", 4), ("min(3, 1, 2)", 1), ("max([3, 9], 2)", 9),
    ("0.1 + 0.2 == 0.3", True), ("1_00_000 / 2", 50000),
])
def test_arithmetic(src, expected):
    assert ev(src) == expected


@pytest.mark.parametrize("src,expected", [
    ("employee.grade in ['M1', 'M2'] and request.days > 3", True),
    ("employee.grade not in ['M1', 'M2']", False),
    ("request.days between 1 and 3", False),
    ("request.days between 1 and 5 and request.type == 'casual'", True),
    ("not request.days > 10", True),
    ("request.days > 10 or employee.state == 'KA'", True),
    ("request.amount >= 50000", True),
    ("'remote' in employee.tags", True),
    ("employee.age != 30", False),
    ("missing.field == null", True),
    ("missing.field > 3", False),
    ("employee.age == '30'", True),
    ("request.days > 3 && employee.age < 40", True),
    ("contains(employee.grade, 'm')", True),
])
def test_conditions(src, expected):
    assert ev(src) is expected


def test_dates():
    assert ev("days_between(employee.joined_on, today())") == 661
    assert ev("years_between(date('1990-06-01'), today())") == 36
    assert ev("months_between(employee.joined_on, today())") == 21
    assert ev("add_days(date(2026, 1, 31), 1)") == date(2026, 2, 1)
    assert ev("today() - date('2026-11-01')") == 6
    assert ev("today() > employee.joined_on") is True
    assert ev("year(today()) * 100 + month(today())") == 202611
    assert ev("weekday(date('2026-11-07'))") == 6


def test_if_is_lazy():
    assert ev("if(request.days > 3, 'long', 'short')") == "long"
    assert ev("if(basic > 0, basic * 2, 1 / 0)") == 50000  # the false branch never runs
    assert ev("coalesce(missing.x, employee.grade)") == "M1"


@pytest.mark.parametrize("src,message", [
    ("1 +", "ends too early"),
    ("basic = 3", "Use “==”"),
    ("foo(1)", "Unknown function"),
    ("round()", "takes 1 to 2"),
    ("(1 + 2", "Expected “)”"),
    ("'abc", "closing quote"),
    ("1 / 0", "Division by zero"),
    ("employee.grade * 2", "Expected a number"),
    ("request.days between 1, 3", "needs “and”"),
    ("1 2", "Is an operator missing"),
    ("", "empty"),
    ("a $ b", "Unexpected character"),
])
def test_errors_are_plain(src, message):
    with pytest.raises(ExpressionError) as err:
        ev(src)
    assert message in str(err.value)


def test_no_python_escape():
    class Secretive:
        public = 1
        _private = 2

        def method(self):
            return 3

    v = {"obj": Secretive()}
    assert evaluate("obj.public", v) == 1
    with pytest.raises(ExpressionError):
        evaluate("obj._private", v)
    assert evaluate("obj.method", v) is None  # methods are not reachable
    for attack in ("__import__('os')", "().__class__", "obj.__class__.__subclasses__"):
        with pytest.raises(ExpressionError):
            evaluate(attack, v, strict=True)


def test_strict_mode_and_validation():
    with pytest.raises(ExpressionError, match="Unknown field"):
        ev("employee.salary > 1", strict=True)
    assert variables_used("employee.grade in ['M1'] and request.days > basic") == ["employee.grade", "request.days", "basic"]
    validate("employee.grade == 'M1'", {"employee.grade", "request.days"})
    with pytest.raises(ExpressionError):
        validate("employee.salary > 1", {"employee.grade"})


def test_check_condition_empty_is_true():
    assert check_condition("", VARS) is True
    assert check_condition("request.days > 30", VARS, today=TODAY) is False


def test_nesting_limit():
    with pytest.raises(ExpressionError):
        ev("(" * 200 + "1" + ")" * 200)
