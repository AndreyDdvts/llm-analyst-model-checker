"""Этап 2 Model Checker Tool: компиляция BehavioralModel → TLA+ (.tla + .cfg).

Поддерживается только DSL проекта. Для каждого проверяемого свойства
генерируется отдельная пара .tla/.cfg — так результат TLC однозначно
относится к одному property_id.

Семантика, которую фиксирует компилятор:
  * переменные, не упомянутые в effects перехода, остаются неизменными
    (UNCHANGED);
  * все правые части effects вычисляются в состоянии до перехода;
  * если эффект присваивает integer-переменной вычисляемое значение,
    переход разрешён только когда результат остаётся в [min, max]
    (домены конечны, выход за границы = переход недоступен);
  * invariant        → INVARIANT  expr
    forbidden_state  → INVARIANT ~(expr)
    reachable        → INVARIANT ~(expr)  (нарушение = найден путь-свидетель)
    deadlock_free    → встроенная проверка deadlock TLC; состояния terminal
                       получают «шаг на месте», поэтому не считаются deadlock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import expressions as ex
from .schema import BehavioralModel, Property, PropertyType, VarType

MODULE_NAME = "BehaviorModel"


@dataclass
class CompiledSpec:
    module_name: str
    tla: str
    cfg: str
    property_id: Optional[str]
    check_deadlock: bool
    state_limit: Optional[int]


def _domain_tla(v) -> str:
    if v.type == VarType.boolean:
        return "BOOLEAN"
    if v.type == VarType.integer:
        return f"{ex.tla_literal(v.min)}..{ex.tla_literal(v.max)}"
    return "{" + ", ".join(ex.tla_literal(x) for x in v.values) + "}"


def _comment(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(text.split()).replace("*)", "* )")


def compile_init(model: BehavioralModel) -> str:
    lines = ["Init =="]
    for v in model.variables:
        lines.append(f"    /\\ {v.name} = {ex.tla_literal(model.initial[v.name])}")
    return "\n".join(lines)


def compile_transition(model: BehavioralModel, t) -> str:
    var_by_name = {v.name: v for v in model.variables}
    head = []
    if t.description or t.source:
        src = f"[{', '.join(t.source)}] " if t.source else ""
        head.append(f"\\* {src}{_comment(t.description)}".rstrip())
    body = [f"{t.name} =="]
    guard = ex.parse(t.guard)
    if not (isinstance(guard, ex.Lit) and guard.value is True):
        body.append(f"    /\\ {ex.to_tla(guard)}")
    assigned = []
    domain_guards = []
    for eff in t.effects:
        target, value = eff["set"]
        node = ex.parse(value)
        body.append(f"    /\\ {target}' = {ex.to_tla(node)}")
        assigned.append(target)
        if var_by_name[target].type == VarType.integer and not isinstance(node, ex.Lit):
            domain_guards.append(f"    /\\ {target}' \\in MC_Domain_{target}")
    body.extend(domain_guards)
    unchanged = [v.name for v in model.variables if v.name not in assigned]
    if unchanged:
        body.append(f"    /\\ UNCHANGED <<{', '.join(unchanged)}>>")
    if len(body) == 1:  # нет ни guard, ни эффектов — допустимый «пустой» шаг
        body.append("    /\\ TRUE")
    return "\n".join(head + body)


def compile_property_expr(prop: Property) -> Optional[str]:
    if prop.type == PropertyType.deadlock_free:
        return None
    e = ex.to_tla(ex.parse(prop.expression))
    if prop.type == PropertyType.invariant:
        return e
    return f"~{e}" if e.startswith("(") else f"~({e})"


def compile_model(
    model: BehavioralModel,
    prop: Optional[Property] = None,
    state_limit: Optional[int] = None,
    module_name: str = MODULE_NAME,
) -> CompiledSpec:
    """Генерирует TLA+-модуль и конфигурацию TLC для одного свойства.

    Модель должна быть предварительно провалидирована (validator.validate).
    """
    names = [v.name for v in model.variables]
    out: list[str] = []
    out.append(f"---- MODULE {module_name} ----")
    out.append(f"\\* Сгенерировано из BehavioralModel \"{_comment(model.name)}\". Не редактировать вручную.")
    out.append("EXTENDS Integers, TLC")
    out.append("")
    out.append(f"VARIABLES {', '.join(names)}")
    out.append(f"vars == <<{', '.join(names)}>>")
    out.append("")
    for v in model.variables:
        out.append(f"MC_Domain_{v.name} == {_domain_tla(v)}")
    out.append("MC_TypeOK ==")
    for v in model.variables:
        out.append(f"    /\\ {v.name} \\in MC_Domain_{v.name}")
    out.append("")
    out.append(compile_init(model))
    out.append("")
    for t in model.transitions:
        out.append(compile_transition(model, t))
        out.append("")

    check_deadlock = prop is not None and prop.type == PropertyType.deadlock_free
    next_lines = ["Next =="] + [f"    \\/ {t.name}" for t in model.transitions]
    if check_deadlock and prop.terminal is not None:
        out.append("\\* Финальные состояния: остановка в них не считается deadlock")
        out.append(f"MC_Terminal == {ex.to_tla(ex.parse(prop.terminal))} /\\ UNCHANGED vars")
        out.append("")
        next_lines.append("    \\/ MC_Terminal")
    out.extend(next_lines)
    out.append("")
    out.append("Spec == Init /\\ [][Next]_vars")
    out.append("")

    cfg = ["SPECIFICATION Spec"]
    if prop is not None and not check_deadlock:
        desc = _comment(prop.description)
        out.append(f"\\* {prop.id} ({prop.type.value}){': ' + desc if desc else ''}")
        out.append(f"MC_Prop == {compile_property_expr(prop)}")
        out.append("")
        cfg.append("INVARIANT MC_Prop")
    cfg.append("INVARIANT MC_TypeOK")
    if state_limit is not None:
        # Состояния сверх лимита не раскрываются; превышение лимита
        # распознаётся по distinct_states > state_limit.
        out.append(f"MC_StateLimit == TLCGet(\"distinct\") <= {int(state_limit)}")
        out.append("")
        cfg.append("CONSTRAINT MC_StateLimit")
    out.append("====")

    return CompiledSpec(
        module_name=module_name,
        tla="\n".join(out) + "\n",
        cfg="\n".join(cfg) + "\n",
        property_id=prop.id if prop else None,
        check_deadlock=check_deadlock,
        state_limit=state_limit,
    )
