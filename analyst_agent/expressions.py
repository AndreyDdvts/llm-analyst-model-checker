"""DSL выражений: разбор, проверка типов, компиляция в TLA+, вычисление.

Грамматика (JSON):

    expr := literal                       -- "Created" | true | false | 42
          | {"var": "<name>"}
          | {"eq":  [expr, expr]}  | {"neq": [expr, expr]}
          | {"lt":  [expr, expr]}  | {"le":  [expr, expr]}
          | {"gt":  [expr, expr]}  | {"ge":  [expr, expr]}
          | {"and": [expr, ...]}   | {"or":  [expr, ...]}
          | {"not": expr}          | {"implies": [expr, expr]}
          | {"in":  [expr, [literal, ...]]}
          | {"add": [expr, expr]}  | {"sub": [expr, expr]}

Строка в JSON — всегда строковый литерал; ссылка на переменную только
через {"var": ...}. Это исключает неоднозначность «имя или значение».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

# ---------------------------------------------------------------- AST


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Lit:
    value: Union[bool, int, str]


@dataclass(frozen=True)
class Op:
    op: str
    args: tuple  # tuple[Node, ...]


@dataclass(frozen=True)
class InSet:
    item: "Node"
    options: tuple  # tuple[Lit, ...]


Node = Union[Var, Lit, Op, InSet]

COMPARE = {"eq": "=", "neq": "#", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
ARITH = {"add": "+", "sub": "-"}
LOGIC_NARY = {"and": "/\\", "or": "\\/"}
BINARY = set(COMPARE) | set(ARITH) | {"implies"}
ALL_OPS = set(COMPARE) | set(ARITH) | set(LOGIC_NARY) | {"not", "implies", "in", "var"}


class ExprError(ValueError):
    """Ошибка структуры или типов выражения. path — JSON-путь внутри выражения."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.path = path


# ---------------------------------------------------------------- parsing


def _is_literal(x: Any) -> bool:
    return isinstance(x, (bool, int, str)) and not isinstance(x, float)


def parse(raw: Any, path: str = "") -> Node:
    if _is_literal(raw):
        return Lit(raw)
    if isinstance(raw, float):
        raise ExprError("дробные числа не поддерживаются", path)
    if raw is None:
        raise ExprError("пустое выражение (null)", path)
    if not isinstance(raw, dict):
        raise ExprError(f"ожидался объект-оператор или литерал, получено {type(raw).__name__}", path)
    if len(raw) != 1:
        raise ExprError(f"объект-оператор должен содержать ровно один ключ, получено {sorted(raw)}", path)
    (op, arg), = raw.items()
    p = f"{path}.{op}" if path else op

    if op not in ALL_OPS:
        raise ExprError(f"неизвестный оператор '{op}'", p)
    if op == "var":
        if not isinstance(arg, str):
            raise ExprError("'var' должен содержать имя переменной (строку)", p)
        return Var(arg)
    if op == "not":
        return Op("not", (parse(arg, p),))
    if op == "in":
        if not (isinstance(arg, list) and len(arg) == 2 and isinstance(arg[1], list)):
            raise ExprError("'in' ожидает [expr, [литерал, ...]]", p)
        if not arg[1]:
            raise ExprError("'in': пустой список значений", p)
        opts = []
        for i, o in enumerate(arg[1]):
            if not _is_literal(o):
                raise ExprError("'in': элементы списка должны быть литералами", f"{p}[1][{i}]")
            opts.append(Lit(o))
        return InSet(parse(arg[0], f"{p}[0]"), tuple(opts))
    if not isinstance(arg, list):
        raise ExprError(f"'{op}' ожидает список аргументов", p)
    if op in LOGIC_NARY:
        if len(arg) < 1:
            raise ExprError(f"'{op}' ожидает хотя бы один аргумент", p)
    elif op in BINARY and len(arg) != 2:
        raise ExprError(f"'{op}' ожидает ровно 2 аргумента, получено {len(arg)}", p)
    return Op(op, tuple(parse(a, f"{p}[{i}]") for i, a in enumerate(arg)))


def variables_of(node: Node) -> set[str]:
    if isinstance(node, Var):
        return {node.name}
    if isinstance(node, Lit):
        return set()
    if isinstance(node, InSet):
        return variables_of(node.item)
    out: set[str] = set()
    for a in node.args:
        out |= variables_of(a)
    return out


# ---------------------------------------------------------------- typing
# Типы: ("bool",) ("int",) ("enum", frozenset) ("str", value) — строковый
# литерал, ещё не привязанный к конкретному enum.


def type_of_var(var) -> tuple:
    t = var.type.value if hasattr(var.type, "value") else var.type
    if t == "boolean":
        return ("bool",)
    if t == "integer":
        return ("int",)
    return ("enum", frozenset(var.values or []))


def _fmt(t: tuple) -> str:
    if t[0] == "enum":
        return "enum{" + ",".join(sorted(t[1])) + "}"
    if t[0] == "str":
        return f'строка "{t[1]}"'
    return {"bool": "boolean", "int": "integer"}[t[0]]


def _lit_type(v) -> tuple:
    if isinstance(v, bool):
        return ("bool",)
    if isinstance(v, int):
        return ("int",)
    return ("str", v)


def _compatible(a: tuple, b: tuple, path: str, ctx: str) -> None:
    """Проверка, что значения типов a и b можно сравнивать / присваивать."""
    ka, kb = a[0], b[0]
    if ka in ("enum", "str") and kb in ("enum", "str"):
        if ka == "enum" and kb == "str":
            a, b = b, a
            ka, kb = kb, ka
        if ka == "str" and kb == "enum":
            if a[1] not in b[1]:
                raise ExprError(
                    f"{ctx}: значение \"{a[1]}\" не входит в домен {_fmt(b)}", path
                )
            return
        if ka == "enum" and kb == "enum":
            if not (a[1] & b[1]):
                raise ExprError(f"{ctx}: домены {_fmt(a)} и {_fmt(b)} не пересекаются", path)
        return
    if ka != kb:
        raise ExprError(f"{ctx}: несовместимые типы {_fmt(a)} и {_fmt(b)}", path)


def typecheck(node: Node, var_types: dict[str, tuple], path: str = "") -> tuple:
    """Возвращает тип выражения или бросает ExprError."""
    if isinstance(node, Lit):
        return _lit_type(node.value)
    if isinstance(node, Var):
        if node.name not in var_types:
            raise ExprError(f"ссылка на несуществующую переменную '{node.name}'", path)
        return var_types[node.name]
    if isinstance(node, InSet):
        t = typecheck(node.item, var_types, path)
        if t[0] == "bool":
            raise ExprError("'in' не применяется к boolean", path)
        for o in node.options:
            _compatible(t, _lit_type(o.value), path, "'in'")
        return ("bool",)
    op, args = node.op, node.args
    types = [typecheck(a, var_types, f"{path}.{op}[{i}]") for i, a in enumerate(args)]
    if op in LOGIC_NARY or op in ("not", "implies"):
        for i, t in enumerate(types):
            if t[0] != "bool":
                raise ExprError(f"'{op}': аргумент {i} должен быть boolean, а не {_fmt(t)}", path)
        return ("bool",)
    if op in ("eq", "neq"):
        _compatible(types[0], types[1], path, f"'{op}'")
        return ("bool",)
    if op in ("lt", "le", "gt", "ge") or op in ARITH:
        for i, t in enumerate(types):
            if t[0] != "int":
                raise ExprError(f"'{op}': аргумент {i} должен быть integer, а не {_fmt(t)}", path)
        return ("bool",) if op not in ARITH else ("int",)
    raise ExprError(f"неизвестный оператор '{op}'", path)


def check_assignable(target: tuple, value_type: tuple, path: str, var_name: str) -> None:
    _compatible(target, value_type, path, f"присваивание '{var_name}'")


# ---------------------------------------------------------------- TLA+


def tla_literal(v: Union[bool, int, str]) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v) if v >= 0 else f"({v})"
    return '"' + v + '"'


def to_tla(node: Node) -> str:
    if isinstance(node, Lit):
        return tla_literal(node.value)
    if isinstance(node, Var):
        return node.name
    if isinstance(node, InSet):
        opts = ", ".join(tla_literal(o.value) for o in node.options)
        return f"({to_tla(node.item)} \\in {{{opts}}})"
    op, args = node.op, node.args
    if op == "not":
        return f"(~{to_tla(args[0])})"
    if op == "implies":
        return f"({to_tla(args[0])} => {to_tla(args[1])})"
    if op in LOGIC_NARY:
        if len(args) == 1:
            return to_tla(args[0])
        return "(" + f" {LOGIC_NARY[op]} ".join(to_tla(a) for a in args) + ")"
    sym = COMPARE.get(op) or ARITH[op]
    return f"({to_tla(args[0])} {sym} {to_tla(args[1])})"


# ---------------------------------------------------------------- evaluation
# Используется только для объяснений (подсветить, какое условие guard
# выполнилось в шаге контрпримера). Перебор состояний здесь НЕ делается.


def evaluate(node: Node, state: dict[str, Any]) -> Any:
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, Var):
        return state[node.name]
    if isinstance(node, InSet):
        v = evaluate(node.item, state)
        return any(v == o.value for o in node.options)
    op, a = node.op, node.args
    if op == "and":
        return all(evaluate(x, state) for x in a)
    if op == "or":
        return any(evaluate(x, state) for x in a)
    if op == "not":
        return not evaluate(a[0], state)
    if op == "implies":
        return (not evaluate(a[0], state)) or evaluate(a[1], state)
    x, y = evaluate(a[0], state), evaluate(a[1], state)
    return {
        "eq": lambda: x == y, "neq": lambda: x != y,
        "lt": lambda: x < y, "le": lambda: x <= y,
        "gt": lambda: x > y, "ge": lambda: x >= y,
        "add": lambda: x + y, "sub": lambda: x - y,
    }[op]()


def to_text(node: Node) -> str:
    """Человекочитаемая запись выражения (для объяснений)."""
    if isinstance(node, Lit):
        return str(node.value).lower() if isinstance(node.value, bool) else str(node.value)
    if isinstance(node, Var):
        return node.name
    if isinstance(node, InSet):
        return f"{to_text(node.item)} ∈ {{{', '.join(to_text(o) for o in node.options)}}}"
    op, a = node.op, node.args
    if op == "not":
        return f"НЕ ({to_text(a[0])})"
    if op == "and":
        return " И ".join(to_text(x) for x in a)
    if op == "or":
        return "(" + " ИЛИ ".join(to_text(x) for x in a) + ")"
    if op == "implies":
        return f"({to_text(a[0])} ⇒ {to_text(a[1])})"
    sym = {"eq": "=", "neq": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥", "add": "+", "sub": "-"}[op]
    return f"{to_text(a[0])} {sym} {to_text(a[1])}"
