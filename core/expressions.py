"""A small, safe expression language for conditions and formulas.

    employee.grade in ['M1', 'M2'] and request.days > 3
    round(basic * 0.4, 0)
    if(days_between(employee.joined_on, today()) >= 240, 1.75, 0)

Text is tokenised and parsed into a tree by a precedence-climbing (Pratt) parser, then
the tree is evaluated against a dict of variables. Nothing is ever passed to Python's
eval/exec, attribute access is limited to dict keys (and plain attributes of simple
objects, never names starting with "_"), and only the functions listed in FUNCTIONS
can be called. Numbers are Decimal, so money stays exact.

Errors are `ExpressionError` with a plain message and the character position.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable

MAX_LENGTH = 4000
MAX_DEPTH = 60


class ExpressionError(ValueError):
    def __init__(self, message: str, position: int | None = None):
        super().__init__(message if position is None else f"{message} (at character {position + 1})")
        self.message = message
        self.position = position


# ── Tokens ──────────────────────────────────────────────────────────

@dataclass
class Token:
    kind: str  # num, str, name, op, eof
    value: Any
    pos: int


KEYWORDS = {"and", "or", "not", "in", "between", "true", "false", "null", "none"}
TWO_CHAR = {"==", "!=", "<=", ">=", "&&", "||"}
ONE_CHAR = set("+-*/%()[],.<>!=")


def tokenize(src: str) -> list[Token]:
    if len(src) > MAX_LENGTH:
        raise ExpressionError(f"The expression is too long (over {MAX_LENGTH} characters).")
    out: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (src[j].isdigit() or (src[j] == "." and not seen_dot and j + 1 < n and src[j + 1].isdigit())):
                seen_dot = seen_dot or src[j] == "."
                j += 1
            while j < n and src[j] == "_" and j + 1 < n and src[j + 1].isdigit():  # 1_00_000
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            out.append(Token("num", Decimal(src[i:j].replace("_", "")), i))
            i = j
            continue
        if c in ("'", '"'):
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(src[j])
                j += 1
            if j >= n:
                raise ExpressionError("A text value is missing its closing quote.", i)
            out.append(Token("str", "".join(buf), i))
            i = j + 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            out.append(Token("name", word, i))
            i = j
            continue
        if src[i:i + 2] in TWO_CHAR:
            op = {"&&": "and", "||": "or"}.get(src[i:i + 2], src[i:i + 2])
            out.append(Token("name" if op in ("and", "or") else "op", op, i))
            i += 2
            continue
        if c in ONE_CHAR:
            out.append(Token("op", c, i))
            i += 1
            continue
        raise ExpressionError(f"Unexpected character “{c}”.", i)
    out.append(Token("eof", None, n))
    return out


# ── Tree ────────────────────────────────────────────────────────────

@dataclass
class Node:
    kind: str  # lit, var, list, unary, binary, call, between, in
    pos: int
    value: Any = None
    args: list["Node"] = field(default_factory=list)


BINARY_PRECEDENCE = {
    "or": 1, "and": 2,
    "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4, "not in": 4, "between": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.tokens = tokenize(src)
        self.i = 0
        self.depth = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_op(self, op: str) -> Token:
        tok = self.next()
        if tok.kind != "op" or tok.value != op:
            found = "the end" if tok.kind == "eof" else f"“{tok.value}”"
            raise ExpressionError(f"Expected “{op}” but found {found}.", tok.pos)
        return tok

    def parse(self) -> Node:
        if self.peek().kind == "eof":
            raise ExpressionError("The expression is empty.", 0)
        node = self.expression(0)
        tok = self.peek()
        if tok.kind != "eof":
            raise ExpressionError(f"Unexpected “{tok.value}”. Is an operator missing?", tok.pos)
        return node

    def _binary_op(self) -> str | None:
        tok = self.peek()
        if tok.kind == "op" and tok.value in BINARY_PRECEDENCE:
            return tok.value
        if tok.kind == "op" and tok.value == "=":
            raise ExpressionError("Use “==” to compare.", tok.pos)
        if tok.kind == "name":
            word = tok.value.lower()
            if word in ("and", "or", "in", "between"):
                return word
            if word == "not" and self.peek(1).kind == "name" and self.peek(1).value.lower() == "in":
                return "not in"
        return None

    def expression(self, min_prec: int) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("The expression is nested too deeply.", self.peek().pos)
        left = self.unary()
        while True:
            op = self._binary_op()
            if op is None or BINARY_PRECEDENCE[op] < min_prec:
                break
            tok = self.next()
            if op == "not in":
                self.next()
            prec = BINARY_PRECEDENCE[op]
            if op == "between":
                low = self.expression(prec + 1)
                and_tok = self.next()
                if and_tok.kind != "name" or and_tok.value.lower() != "and":
                    raise ExpressionError("“between” needs “and”, e.g. x between 1 and 5.", and_tok.pos)
                high = self.expression(prec + 1)
                left = Node("between", tok.pos, args=[left, low, high])
                continue
            right = self.expression(prec + 1)
            left = Node("binary", tok.pos, op, [left, right])
        self.depth -= 1
        return left

    def unary(self) -> Node:
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("-", "+"):
            self.next()
            return Node("unary", tok.pos, tok.value, [self.unary()])
        if (tok.kind == "op" and tok.value == "!") or (tok.kind == "name" and tok.value.lower() == "not"):
            self.next()
            return Node("unary", tok.pos, "not", [self.expression(3)])
        return self.postfix(self.primary())

    def postfix(self, node: Node) -> Node:
        while self.peek().kind == "op" and self.peek().value == "." and node.kind == "var":
            self.next()
            name = self.next()
            if name.kind != "name":
                raise ExpressionError("Expected a field name after “.”.", name.pos)
            node = Node("var", node.pos, node.value + [name.value])
        return node

    def primary(self) -> Node:
        tok = self.next()
        if tok.kind == "num":
            return Node("lit", tok.pos, tok.value)
        if tok.kind == "str":
            return Node("lit", tok.pos, tok.value)
        if tok.kind == "op" and tok.value == "(":
            node = self.expression(0)
            self.expect_op(")")
            return node
        if tok.kind == "op" and tok.value == "[":
            items = []
            if not (self.peek().kind == "op" and self.peek().value == "]"):
                while True:
                    items.append(self.expression(0))
                    if self.peek().kind == "op" and self.peek().value == ",":
                        self.next()
                        continue
                    break
            self.expect_op("]")
            return Node("list", tok.pos, args=items)
        if tok.kind == "name":
            word = tok.value
            low = word.lower()
            if low == "true":
                return Node("lit", tok.pos, True)
            if low == "false":
                return Node("lit", tok.pos, False)
            if low in ("null", "none"):
                return Node("lit", tok.pos, None)
            if low in ("and", "or", "in", "between", "not"):
                raise ExpressionError(f"“{word}” can't be used here.", tok.pos)
            if self.peek().kind == "op" and self.peek().value == "(":
                self.next()
                if low not in FUNCTIONS:
                    raise ExpressionError(f"Unknown function “{word}”. Available: {', '.join(sorted(FUNCTIONS))}.", tok.pos)
                args = []
                if not (self.peek().kind == "op" and self.peek().value == ")"):
                    while True:
                        args.append(self.expression(0))
                        if self.peek().kind == "op" and self.peek().value == ",":
                            self.next()
                            continue
                        break
                self.expect_op(")")
                spec = FUNCTIONS[low]
                if not (spec.min_args <= len(args) <= spec.max_args):
                    want = str(spec.min_args) if spec.min_args == spec.max_args else f"{spec.min_args} to {spec.max_args}"
                    raise ExpressionError(f"{low}() takes {want} value(s), not {len(args)}.", tok.pos)
                return Node("call", tok.pos, low, args)
            return Node("var", tok.pos, [word])
        if tok.kind == "eof":
            raise ExpressionError("The expression ends too early.", tok.pos)
        raise ExpressionError(f"Unexpected “{tok.value}”.", tok.pos)


# ── Values ──────────────────────────────────────────────────────────

def _num(v, pos, what="a number"):
    if isinstance(v, bool) or v is None:
        raise ExpressionError(f"Expected {what} but got {_describe(v)}.", pos)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, str):
        try:
            return Decimal(v.strip())
        except InvalidOperation:
            pass
    raise ExpressionError(f"Expected {what} but got {_describe(v)}.", pos)


def _date(v, pos):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            pass
    raise ExpressionError(f"Expected a date but got {_describe(v)}.", pos)


def _describe(v) -> str:
    if v is None:
        return "nothing (empty)"
    if isinstance(v, bool):
        return "yes/no"
    if isinstance(v, (int, float, Decimal)):
        return f"the number {v}"
    if isinstance(v, str):
        return f"the text '{v}'"
    if isinstance(v, (date, datetime)):
        return f"the date {v.isoformat()}"
    if isinstance(v, (list, tuple)):
        return "a list"
    return "a value"


def _normalise(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, datetime):
        return v
    if isinstance(v, (list, tuple, set)):
        return [_normalise(x) for x in v]
    return v


def _truthy(v) -> bool:
    if isinstance(v, Decimal):
        return v != 0
    return bool(v)


def _compare(op, a, b, pos):
    if op in ("==", "!="):
        if isinstance(a, (int, float, Decimal)) and not isinstance(a, bool) and isinstance(b, str):
            b = _num(b, pos) if _looks_numeric(b) else b
        if isinstance(b, (int, float, Decimal)) and not isinstance(b, bool) and isinstance(a, str):
            a = _num(a, pos) if _looks_numeric(a) else a
        if isinstance(a, date) and isinstance(b, str):
            b = _date(b, pos)
        if isinstance(b, date) and isinstance(a, str):
            a = _date(a, pos)
        eq = a == b
        return eq if op == "==" else not eq
    if a is None or b is None:
        return False
    if isinstance(a, (date, datetime)) or isinstance(b, (date, datetime)):
        a, b = _date(a, pos), _date(b, pos)
    elif isinstance(a, str) and isinstance(b, str):
        pass
    else:
        a, b = _num(a, pos), _num(b, pos)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _looks_numeric(s: str) -> bool:
    try:
        Decimal(s)
        return True
    except InvalidOperation:
        return False


# ── Functions ───────────────────────────────────────────────────────

@dataclass
class Fn:
    impl: Callable
    min_args: int
    max_args: int
    help: str
    lazy: bool = False  # receives unevaluated nodes (for if)


def _round(x, digits=Decimal(0)):
    d = int(digits)
    q = Decimal(1).scaleb(-d)
    return x.quantize(q, rounding=ROUND_HALF_UP)


def _today(ctx):
    return ctx.today


FUNCTIONS: dict[str, Fn] = {}


def _reg(name, min_args, max_args, help, lazy=False):
    def deco(fn):
        FUNCTIONS[name] = Fn(fn, min_args, max_args, help, lazy)
        return fn
    return deco


@_reg("min", 1, 50, "Smallest of the values: min(a, b, …)")
def _f_min(ctx, pos, *a):
    vals = _flatten(a)
    if not vals:
        raise ExpressionError("min() needs at least one value.", pos)
    return min(_num(v, pos) for v in vals)


@_reg("max", 1, 50, "Largest of the values: max(a, b, …)")
def _f_max(ctx, pos, *a):
    vals = _flatten(a)
    if not vals:
        raise ExpressionError("max() needs at least one value.", pos)
    return max(_num(v, pos) for v in vals)


@_reg("round", 1, 2, "Round half up: round(x) or round(x, 2)")
def _f_round(ctx, pos, x, digits=Decimal(0)):
    return _round(_num(x, pos), _num(digits, pos))


@_reg("floor", 1, 1, "Round down to a whole number")
def _f_floor(ctx, pos, x):
    return Decimal(math.floor(_num(x, pos)))


@_reg("ceil", 1, 1, "Round up to a whole number")
def _f_ceil(ctx, pos, x):
    return Decimal(math.ceil(_num(x, pos)))


@_reg("abs", 1, 1, "Value without its sign")
def _f_abs(ctx, pos, x):
    return abs(_num(x, pos))


@_reg("if", 3, 3, "if(condition, value when true, value when false)", lazy=True)
def _f_if(ctx, pos, cond, a, b):
    return ctx.eval(a) if _truthy(ctx.eval(cond)) else ctx.eval(b)


@_reg("coalesce", 1, 20, "First value that is not empty")
def _f_coalesce(ctx, pos, *a):
    return next((v for v in a if v not in (None, "")), None)


@_reg("today", 0, 0, "Today's date in the company's timezone")
def _f_today(ctx, pos):
    return ctx.today


@_reg("date", 1, 3, "date('2026-04-01') or date(2026, 4, 1)")
def _f_date(ctx, pos, *a):
    if len(a) == 1:
        return _date(a[0], pos)
    if len(a) == 3:
        try:
            return date(int(_num(a[0], pos)), int(_num(a[1], pos)), int(_num(a[2], pos)))
        except ValueError:
            raise ExpressionError("That date does not exist.", pos)
    raise ExpressionError("date() takes a text date or year, month, day.", pos)


@_reg("days_between", 2, 2, "Days from the first date to the second")
def _f_days_between(ctx, pos, a, b):
    return Decimal((_date(b, pos) - _date(a, pos)).days)


@_reg("months_between", 2, 2, "Whole months from the first date to the second")
def _f_months_between(ctx, pos, a, b):
    a, b = _date(a, pos), _date(b, pos)
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day < a.day:
        months -= 1
    return Decimal(months)


@_reg("years_between", 2, 2, "Whole years from the first date to the second (e.g. age, service)")
def _f_years_between(ctx, pos, a, b):
    return Decimal(int(_f_months_between(ctx, pos, a, b) // 12))


@_reg("add_days", 2, 2, "Date plus a number of days")
def _f_add_days(ctx, pos, d, n):
    return _date(d, pos) + timedelta(days=int(_num(n, pos)))


@_reg("year", 1, 1, "Year of a date")
def _f_year(ctx, pos, d):
    return Decimal(_date(d, pos).year)


@_reg("month", 1, 1, "Month (1–12) of a date")
def _f_month(ctx, pos, d):
    return Decimal(_date(d, pos).month)


@_reg("day", 1, 1, "Day of the month of a date")
def _f_day(ctx, pos, d):
    return Decimal(_date(d, pos).day)


@_reg("weekday", 1, 1, "Day of the week, 1 = Monday … 7 = Sunday")
def _f_weekday(ctx, pos, d):
    return Decimal(_date(d, pos).isoweekday())


@_reg("len", 1, 1, "Number of characters in a text, or items in a list")
def _f_len(ctx, pos, v):
    if v is None:
        return Decimal(0)
    if isinstance(v, (str, list)):
        return Decimal(len(v))
    raise ExpressionError(f"len() needs a text or a list, not {_describe(v)}.", pos)


@_reg("lower", 1, 1, "Text in lower case")
def _f_lower(ctx, pos, v):
    return "" if v is None else str(v).lower()


@_reg("upper", 1, 1, "Text in upper case")
def _f_upper(ctx, pos, v):
    return "" if v is None else str(v).upper()


@_reg("contains", 2, 2, "contains(text_or_list, value)")
def _f_contains(ctx, pos, hay, needle):
    if hay is None:
        return False
    if isinstance(hay, list):
        return needle in hay
    return str(needle).lower() in str(hay).lower()


def _flatten(values):
    out = []
    for v in values:
        out.extend(v if isinstance(v, list) else [v])
    return out


# ── Evaluation ──────────────────────────────────────────────────────

class _Context:
    def __init__(self, variables: dict, today: date, strict: bool):
        self.variables = variables or {}
        self.today = today
        self.strict = strict

    def lookup(self, path: list[str], pos: int):
        cur: Any = self.variables
        for i, part in enumerate(path):
            if part.startswith("_"):
                raise ExpressionError(f"“{part}” is not allowed.", pos)
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                    continue
            elif cur is not None and not isinstance(cur, (str, int, float, Decimal, bool, list, date)) \
                    and hasattr(cur, part) and not callable(getattr(cur, part)):
                cur = getattr(cur, part)
                continue
            if self.strict:
                raise ExpressionError(f"Unknown field “{'.'.join(path[:i + 1])}”.", pos)
            return None
        return _normalise(cur)

    def eval(self, node: Node):
        k = node.kind
        if k == "lit":
            return node.value
        if k == "var":
            return self.lookup(node.value, node.pos)
        if k == "list":
            return [self.eval(a) for a in node.args]
        if k == "unary":
            v = self.eval(node.args[0])
            if node.value == "not":
                return not _truthy(v)
            n = _num(v, node.pos)
            return -n if node.value == "-" else n
        if k == "between":
            v, lo, hi = (self.eval(a) for a in node.args)
            return _compare(">=", v, lo, node.pos) and _compare("<=", v, hi, node.pos)
        if k == "call":
            spec = FUNCTIONS[node.value]
            if spec.lazy:
                return spec.impl(self, node.pos, *node.args)
            return spec.impl(self, node.pos, *[self.eval(a) for a in node.args])
        if k == "binary":
            op = node.value
            if op == "and":
                return _truthy(self.eval(node.args[0])) and _truthy(self.eval(node.args[1]))
            if op == "or":
                return _truthy(self.eval(node.args[0])) or _truthy(self.eval(node.args[1]))
            a, b = self.eval(node.args[0]), self.eval(node.args[1])
            if op in ("in", "not in"):
                if b is None:
                    found = False
                elif isinstance(b, list):
                    found = any(_compare("==", a, x, node.pos) for x in b)
                elif isinstance(b, str):
                    found = a is not None and str(a) in b
                else:
                    raise ExpressionError(f"“in” needs a list or a text on the right, not {_describe(b)}.", node.pos)
                return found if op == "in" else not found
            if op in ("==", "!=", "<", "<=", ">", ">="):
                return _compare(op, a, b, node.pos)
            if op == "+":
                if isinstance(a, str) and isinstance(b, str):
                    return a + b
                if isinstance(a, date) and not isinstance(b, date):
                    return a + timedelta(days=int(_num(b, node.pos)))
                return _num(a, node.pos) + _num(b, node.pos)
            if op == "-":
                if isinstance(a, date) and isinstance(b, date):
                    return Decimal((a - b).days)
                if isinstance(a, date):
                    return a - timedelta(days=int(_num(b, node.pos)))
                return _num(a, node.pos) - _num(b, node.pos)
            x, y = _num(a, node.pos), _num(b, node.pos)
            if op == "*":
                return x * y
            if y == 0:
                raise ExpressionError("Division by zero.", node.pos)
            return x / y if op == "/" else x % y
        raise ExpressionError("Unsupported expression.", node.pos)


_CACHE: dict[str, Node] = {}


def parse(src: str) -> Node:
    """Parse text into a tree (cached), raising ExpressionError on mistakes."""
    src = (src or "").strip()
    node = _CACHE.get(src)
    if node is None:
        node = Parser(src).parse()
        if len(_CACHE) > 2000:
            _CACHE.clear()
        _CACHE[src] = node
    return node


def evaluate(expression: str | Node, variables: dict | None = None, *, today: date | None = None,
             strict: bool = False):
    """Evaluate a condition or formula. With strict=True an unknown field is an error;
    otherwise it is empty (null). Numbers come back as Decimal."""
    node = parse(expression) if isinstance(expression, str) else expression
    return _Context(variables or {}, today or date.today(), strict).eval(node)


def check_condition(expression: str | None, variables: dict | None = None, *, today: date | None = None) -> bool:
    """True when the condition holds; an empty condition always holds."""
    if not expression or not expression.strip():
        return True
    return _truthy(evaluate(expression, variables, today=today))


def variables_used(expression: str) -> list[str]:
    """Dotted field names used in the expression, e.g. ['employee.grade', 'request.days']."""
    out: list[str] = []

    def walk(n: Node):
        if n.kind == "var":
            name = ".".join(n.value)
            if name not in out:
                out.append(name)
        for a in n.args:
            if isinstance(a, Node):
                walk(a)
    walk(parse(expression))
    return out


def validate(expression: str, allowed: set[str] | None = None) -> list[str]:
    """Parse and, when `allowed` is given, check that only known fields (or their
    prefixes) are used. Returns the fields used."""
    used = variables_used(expression)
    if allowed is not None:
        for name in used:
            if name not in allowed and not any(a.startswith(name + ".") or name.startswith(a + ".") for a in allowed):
                raise ExpressionError(f"Unknown field “{name}”.")
    return used


def to_json(value):
    """Result as plain JSON (Decimal -> number, dates -> ISO)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 10**15 else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [to_json(v) for v in value]
    return value
