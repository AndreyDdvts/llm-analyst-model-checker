"""Этап 1 Model Checker Tool: валидация BehavioralModel.

Полностью детерминированная проверка. Возвращает список ValidationIssue;
наличие хотя бы одной ошибки (severity="error") означает MODEL_INVALID.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Union

from pydantic import ValidationError

from . import expressions as ex
from .schema import BehavioralModel, PropertyType, ValidationIssue, VarType

IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# Значения enum попадают в TLA+ как строковые литералы
ENUM_VALUE_RE = re.compile(r'^[^"\\\n\r\t]+$')

MAX_INT_RANGE = 10_000          # ширина одного целочисленного домена
STATE_SPACE_WARN = 5_000_000    # оценка |S| = произведение доменов

TLA_RESERVED = {
    # ключевые слова TLA+
    "ASSUME", "ASSUMPTION", "AXIOM", "CASE", "CHOOSE", "CONSTANT", "CONSTANTS",
    "DOMAIN", "ELSE", "ENABLED", "EXCEPT", "EXTENDS", "IF", "IN", "INSTANCE",
    "LET", "LOCAL", "MODULE", "OTHER", "SUBSET", "THEN", "THEOREM", "UNCHANGED",
    "UNION", "VARIABLE", "VARIABLES", "WITH", "LAMBDA", "BOOLEAN", "TRUE",
    "FALSE", "STRING", "LEMMA", "PROOF", "PROPOSITION", "COROLLARY", "BY", "QED",
    "OBVIOUS", "OMITTED", "HAVE", "TAKE", "WITNESS", "PICK", "SUFFICES", "DEFINE",
    "DEF", "DEFS", "USE", "HIDE", "NEW", "ACTION", "STATE", "TEMPORAL", "RECURSIVE",
    "ONLY", "PROVE",
    # операторы стандартных модулей (Integers, TLC, Sequences, FiniteSets)
    "Nat", "Int", "Real", "Seq", "Len", "Append", "Head", "Tail", "SubSeq",
    "SelectSeq", "Cardinality", "IsFiniteSet", "Print", "PrintT", "Assert",
    "JavaTime", "TLCGet", "TLCSet", "Permutations", "SortSeq", "RandomElement",
    "Any", "ToString", "TLCEval",
    # имена, которые генерирует компилятор
    "Init", "Next", "Spec", "vars",
}
GENERATED_PREFIX = "MC_"


def _issue(code: str, path: str, msg: str, severity: str = "error") -> ValidationIssue:
    return ValidationIssue(code=code, path=path, message=msg, severity=severity)


def _check_ident(name: str, path: str, what: str, issues: list) -> bool:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        issues.append(_issue(
            "invalid_name", path,
            f"{what} '{name}': допустимы латинские буквы, цифры и '_', первым символом — буква",
        ))
        return False
    if name in TLA_RESERVED or name.startswith(GENERATED_PREFIX):
        issues.append(_issue("reserved_name", path, f"{what} '{name}' совпадает с зарезервированным именем TLA+"))
        return False
    return True


def _value_in_domain(var, value: Any) -> Optional[str]:
    """None если значение допустимо, иначе текст ошибки."""
    t = var.type
    if t == VarType.boolean:
        return None if isinstance(value, bool) else f"ожидалось boolean, получено {value!r}"
    if t == VarType.integer:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"ожидалось integer, получено {value!r}"
        if var.min is not None and var.max is not None and not (var.min <= value <= var.max):
            return f"значение {value} вне домена {var.min}..{var.max}"
        return None
    if not isinstance(value, str):
        return f"ожидалось значение enum, получено {value!r}"
    if value not in (var.values or []):
        return f"значение \"{value}\" не входит в домен {var.values}"
    return None


def parse_model(raw: Union[dict, BehavioralModel]) -> tuple[Optional[BehavioralModel], list[ValidationIssue]]:
    if isinstance(raw, BehavioralModel):
        return raw, []
    try:
        return BehavioralModel.model_validate(raw), []
    except ValidationError as e:
        issues = []
        for err in e.errors():
            path = ".".join(str(p) for p in err["loc"])
            issues.append(_issue("schema", path, err["msg"]))
        return None, issues


def validate(raw: Union[dict, BehavioralModel]) -> tuple[Optional[BehavioralModel], list[ValidationIssue]]:
    """Полная валидация. Возвращает (модель или None, список замечаний)."""
    model, issues = parse_model(raw)
    if model is None:
        return None, issues

    # ---------------------------------------------------- variables
    if not model.variables:
        issues.append(_issue("no_variables", "variables", "Модель не содержит ни одной переменной состояния"))
    var_by_name: dict[str, Any] = {}
    for i, v in enumerate(model.variables):
        p = f"variables[{i}]"
        _check_ident(v.name, f"{p}.name", "Имя переменной", issues)
        if v.name in var_by_name:
            issues.append(_issue("duplicate_variable", f"{p}.name", f"Повторяющееся имя переменной '{v.name}'"))
            continue
        var_by_name[v.name] = v
        if v.type == VarType.enum:
            if not v.values:
                issues.append(_issue("empty_domain", f"{p}.values", f"enum '{v.name}' должен иметь непустой список values"))
            else:
                if len(set(v.values)) != len(v.values):
                    issues.append(_issue("duplicate_value", f"{p}.values", f"В enum '{v.name}' есть повторяющиеся значения"))
                for j, val in enumerate(v.values):
                    if not isinstance(val, str) or not ENUM_VALUE_RE.match(val):
                        issues.append(_issue("invalid_value", f"{p}.values[{j}]", f"Недопустимое значение enum {val!r}"))
            if v.min is not None or v.max is not None:
                issues.append(_issue("type_mismatch", p, f"enum '{v.name}' не может иметь min/max"))
        elif v.type == VarType.integer:
            if v.min is None or v.max is None:
                issues.append(_issue(
                    "infinite_domain", p,
                    f"integer '{v.name}' должен иметь min и max: все домены обязаны быть конечными",
                ))
            elif v.min > v.max:
                issues.append(_issue("empty_domain", p, f"integer '{v.name}': min > max"))
            elif v.max - v.min + 1 > MAX_INT_RANGE:
                issues.append(_issue("domain_too_large", p, f"integer '{v.name}': домен шире {MAX_INT_RANGE} значений"))
            if v.values:
                issues.append(_issue("type_mismatch", p, f"integer '{v.name}' не может иметь values"))
        else:  # boolean
            if v.values or v.min is not None or v.max is not None:
                issues.append(_issue("type_mismatch", p, f"boolean '{v.name}' не может иметь values/min/max"))

    var_types = {n: ex.type_of_var(v) for n, v in var_by_name.items()}

    # ---------------------------------------------------- initial
    for name in var_by_name:
        if name not in model.initial:
            issues.append(_issue("missing_initial", f"initial.{name}", f"Нет начального значения для переменной '{name}'"))
    for name, value in model.initial.items():
        if name not in var_by_name:
            issues.append(_issue(
                "unknown_variable", f"initial.{name!r}",
                f"Начальное значение задано для несуществующей переменной {name!r}",
            ))
            continue
        err = _value_in_domain(var_by_name[name], value)
        if err:
            issues.append(_issue("initial_out_of_domain", f"initial.{name}", f"'{name}': {err}"))

    # ---------------------------------------------------- transitions
    if not model.transitions:
        issues.append(_issue("no_transitions", "transitions", "Модель не содержит ни одного перехода"))
    seen_tr: set[str] = set()
    modified_vars: set[str] = set()
    for i, t in enumerate(model.transitions):
        p = f"transitions[{i}]"
        _check_ident(t.name, f"{p}.name", "Имя перехода", issues)
        if t.name in seen_tr:
            issues.append(_issue("duplicate_transition", f"{p}.name", f"Повторяющееся имя перехода '{t.name}'"))
        if t.name in var_by_name:
            issues.append(_issue("name_clash", f"{p}.name", f"Имя перехода '{t.name}' совпадает с именем переменной"))
        seen_tr.add(t.name)

        _check_bool_expr(t.guard, f"{p}.guard", var_types, issues, "guard")

        if not t.effects:
            issues.append(_issue("no_effects", f"{p}.effects", f"Переход '{t.name}' ничего не меняет", "warning"))
        assigned: set[str] = set()
        for j, eff in enumerate(t.effects):
            ep = f"{p}.effects[{j}]"
            if set(eff) != {"set"} or not isinstance(eff["set"], list) or len(eff["set"]) != 2:
                issues.append(_issue("invalid_effect", ep, 'Эффект должен иметь вид {"set": ["<var>", <expression>]}'))
                continue
            target, value = eff["set"]
            if target not in var_by_name:
                issues.append(_issue("unknown_variable", ep, f"Эффект меняет несуществующую переменную {target!r}"))
                continue
            if target in assigned:
                issues.append(_issue("duplicate_assignment", ep, f"Переменная '{target}' присваивается дважды в '{t.name}'"))
            assigned.add(target)
            modified_vars.add(target)
            try:
                node = ex.parse(value)
                vt = ex.typecheck(node, var_types)
                ex.check_assignable(var_types[target], vt, "", target)
                if isinstance(node, ex.Lit):
                    err = _value_in_domain(var_by_name[target], node.value)
                    if err:
                        issues.append(_issue("effect_out_of_domain", ep, f"'{target}': {err}"))
            except ex.ExprError as e:
                issues.append(_issue("invalid_effect", ep + (f".{e.path}" if e.path else ""), str(e)))

    for name in var_by_name:
        if name not in modified_vars:
            issues.append(_issue(
                "constant_variable", f"variables.{name}",
                f"Переменная '{name}' не изменяется ни одним переходом", "warning",
            ))

    # ---------------------------------------------------- properties
    req_ids = {r.id for r in model.requirements}
    seen_p: set[str] = set()
    for i, prop in enumerate(model.properties):
        p = f"properties[{i}]"
        if not prop.id:
            issues.append(_issue("invalid_property", f"{p}.id", "Пустой id свойства"))
        if prop.id in seen_p:
            issues.append(_issue("duplicate_property", f"{p}.id", f"Повторяющийся id свойства '{prop.id}'"))
        seen_p.add(prop.id)
        if prop.type == PropertyType.deadlock_free:
            if prop.expression is not None:
                issues.append(_issue(
                    "invalid_property", f"{p}.expression",
                    "deadlock_free не использует expression; финальные состояния задаются в 'terminal'",
                ))
            if prop.terminal is not None:
                _check_bool_expr(prop.terminal, f"{p}.terminal", var_types, issues, "terminal")
        else:
            if prop.terminal is not None:
                issues.append(_issue("invalid_property", f"{p}.terminal", "'terminal' допустим только для deadlock_free"))
            if prop.expression is None:
                issues.append(_issue("invalid_property", f"{p}.expression", f"Свойство типа {prop.type.value} требует expression"))
            else:
                _check_bool_expr(prop.expression, f"{p}.expression", var_types, issues, "expression")

    # ---------------------------------------------------- source references
    def _refs(items, kind):
        for i, it in enumerate(items):
            for s in it.source:
                if s not in req_ids:
                    issues.append(_issue(
                        "unknown_requirement", f"{kind}[{i}].source",
                        f"Ссылка на несуществующее требование '{s}'", "warning",
                    ))
    _refs(model.variables, "variables")
    _refs(model.transitions, "transitions")
    _refs(model.properties, "properties")

    # ---------------------------------------------------- size estimate
    size = 1
    for v in var_by_name.values():
        if v.type == VarType.boolean:
            size *= 2
        elif v.type == VarType.enum:
            size *= max(1, len(v.values or []))
        elif v.min is not None and v.max is not None and v.max >= v.min:
            size *= v.max - v.min + 1
    if size > STATE_SPACE_WARN:
        issues.append(_issue(
            "large_state_space", "variables",
            f"Верхняя оценка пространства состояний {size:,}; проверка может упереться в лимиты", "warning",
        ))

    return model, issues


def _check_bool_expr(raw, path, var_types, issues, what) -> None:
    try:
        node = ex.parse(raw)
        t = ex.typecheck(node, var_types)
        if t[0] != "bool":
            issues.append(_issue("type_mismatch", path, f"{what} должен быть boolean-выражением"))
    except ex.ExprError as e:
        issues.append(_issue("invalid_expression", path + (f".{e.path}" if e.path else ""), str(e)))


def has_errors(issues: list[ValidationIssue]) -> bool:
    return any(i.severity == "error" for i in issues)
