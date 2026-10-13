"""Структурный разбор результата проверки (детерминированный, без LLM).

Готовит «опоры» для объяснения:
  * business_steps   — шаги контрпримера словами описаний переходов модели;
  * diagnostic_hints — структурные наблюдения: какое условие разрешило
                       последний переход, какой переход не сбросил признак;
  * hypotheses       — возможные причины (ошибка требований / интерпретации /
                       отсутствующее правило);
  * questions        — уточняющие вопросы пользователю.

Агент-аналитик (LLM) использует их, чтобы сформулировать вывод на языке
исходных требований. Модель этот модуль НЕ исправляет.
"""

from __future__ import annotations

from typing import Any, Optional

from . import expressions as ex
from .schema import (
    BehavioralModel,
    Property,
    PropertyType,
    ResultStatus,
    Trace,
    VerificationResult,
)


def _v(x: Any) -> str:
    return str(x).lower() if isinstance(x, bool) else str(x)


def _state_text(state: dict, only: Optional[set] = None) -> str:
    return ", ".join(f"{k}={_v(v)}" for k, v in state.items() if only is None or k in only)


def _guard_vars(model: BehavioralModel, tname: str) -> set[str]:
    t = next((t for t in model.transitions if t.name == tname), None)
    return ex.variables_of(ex.parse(t.guard)) if t else set()


def _relevant_vars(model: BehavioralModel, prop: Property, trace: Trace) -> set[str]:
    rel: set[str] = set()
    for e in (prop.expression, prop.terminal):
        if e is not None:
            rel |= ex.variables_of(ex.parse(e))
    for name in trace.transitions():
        rel |= _guard_vars(model, name)
    return rel


def business_steps(model: BehavioralModel, trace: Trace, relevant: Optional[set] = None) -> list[str]:
    tr = {t.name: t for t in model.transitions}
    var_desc = {v.name: v.description for v in model.variables}
    steps: list[str] = []
    prev: Optional[dict] = None
    for s in trace.steps:
        if s.transition is None:
            steps.append(f"Начальное состояние: {_state_text(s.state)}.")
        else:
            t = tr.get(s.transition)
            what = (t.description if t and t.description else s.transition)
            ch = "; ".join(f"{k}: {_v(a)} → {_v(b)}" for k, (a, b) in s.changes.items()) or "состояние не изменилось"
            line = f"{what} [{s.transition}]. Изменения: {ch}."
            if relevant and prev is not None:
                kept = [k for k in relevant if k in s.state and k not in s.changes]
                if kept:
                    kept_txt = ", ".join(
                        f"{k}={_v(s.state[k])}" + (f" ({var_desc[k]})" if var_desc.get(k) else "")
                        for k in sorted(kept, key=list(s.state).index))
                    line += f" Сохранились: {kept_txt}."
            steps.append(line)
        prev = s.state
    return [f"{i + 1}. {x}" for i, x in enumerate(steps)]


def _lit_key(v: Any):
    if isinstance(v, (bool, int, str)):
        return (type(v).__name__, v)
    return None


def _literals(node) -> set:
    if isinstance(node, ex.Lit):
        return {_lit_key(node.value)}
    if isinstance(node, ex.Var):
        return set()
    if isinstance(node, ex.InSet):
        return {_lit_key(o.value) for o in node.options}
    out: set = set()
    for a in node.args:
        out |= _literals(a)
    return out


def _unsatisfiable_comparisons(model: BehavioralModel, node) -> list[str]:
    """Сравнения integer-переменной с константой, невыполнимые в её домене."""
    ints = {v.name: v for v in model.variables if v.type.value == "integer"}
    found: list[str] = []

    def walk(n):
        if isinstance(n, ex.Op):
            if n.op in ("lt", "le", "gt", "ge", "eq") and len(n.args) == 2:
                a, b = n.args
                if isinstance(a, ex.Var) and a.name in ints and isinstance(b, ex.Lit) \
                        and isinstance(b.value, int) and not isinstance(b.value, bool):
                    v, c = ints[a.name], b.value
                    ok = any({"lt": x < c, "le": x <= c, "gt": x > c, "ge": x >= c, "eq": x == c}[n.op]
                             for x in (v.min, v.max)) if n.op != "eq" else v.min <= c <= v.max
                    if not ok:
                        found.append(f"«{ex.to_text(n)}» невыполнимо, т.к. {a.name} ∈ {v.min}..{v.max}")
            for x in n.args:
                walk(x)

    walk(node)
    return found


def _last_setter(trace: Trace, var: str, before_idx: int) -> Optional[int]:
    for i in range(before_idx, 0, -1):
        if var in trace.steps[i].changes:
            return i
    return None


def analyze(model: BehavioralModel, result: VerificationResult) -> dict[str, list[str]]:
    """Структурные подсказки для объяснения результата."""
    out: dict[str, list[str]] = {"business_steps": [], "diagnostic_hints": [], "hypotheses": [], "questions": []}
    prop = next((p for p in model.properties if p.id == result.property_id), None)
    if prop is None:
        return out
    tr = {t.name: t for t in model.transitions}
    pdesc = prop.description or prop.id

    trace = result.counterexample or result.witness
    if trace:
        out["business_steps"] = business_steps(model, trace, _relevant_vars(model, prop, trace))

    if result.status != ResultStatus.PROPERTY_VIOLATED:
        return out

    # ---------------------------------------------------- deadlock
    if prop.type == PropertyType.deadlock_free and trace:
        last = trace.steps[-1].state
        disabled = []
        for t in model.transitions:
            g = ex.parse(t.guard)
            disabled.append(f"{t.name} (условие {ex.to_text(g)} ложно)")
        out["diagnostic_hints"].append(
            f"В состоянии {_state_text(last)} не разрешён ни один переход: " + "; ".join(disabled) + ".")
        out["hypotheses"] += [
            "Это состояние на самом деле финальное — его нужно явно указать в terminal.",
            "В требованиях не описано, что происходит с объектом в этом состоянии (пропущен переход).",
        ]
        out["questions"].append(f"Что должно происходить, когда {_state_text(last)}? Это конечное состояние процесса?")
        return out

    # ---------------------------------------------------- unreachable
    if prop.type == PropertyType.reachable:
        target = ex.parse(prop.expression)
        target_vars = ex.variables_of(target)
        target_lits = _literals(target)
        # переходы, которые присваивают целевой переменной значение из цели
        leading = [t for t in model.transitions
                   if any(e["set"][0] in target_vars and _lit_key(e["set"][1]) in target_lits
                          for e in t.effects)]
        hint = f"Состояние «{pdesc}» ({ex.to_text(target)}) недостижимо ни при какой допустимой последовательности событий."
        if leading:
            hint += " Ведут в него переходы: " + "; ".join(
                f"{t.name} (условие «{ex.to_text(ex.parse(t.guard))}»)" for t in leading
            ) + " — ни в одном достижимом состоянии их условия не выполняются."
        else:
            hint += " Ни один переход не устанавливает целевые значения."
        out["diagnostic_hints"].append(hint)
        for t in leading:
            for msg in _unsatisfiable_comparisons(model, ex.parse(t.guard)):
                out["diagnostic_hints"].append(f"В условии перехода {t.name}: {msg}.")
        out["hypotheses"].append(
            "Условия (guards) переходов, ведущих в цель, слишком строгие или противоречат ограничениям "
            "других требований; либо в модели не хватает перехода.")
        out["questions"].append(f"Каким сценарием по требованиям должно достигаться состояние «{pdesc}»?")
        return out

    # ---------------------------------------------------- invariant / forbidden_state
    if not trace or len(trace.steps) < 2:
        out["diagnostic_hints"].append(f"Свойство «{pdesc}» нарушено уже в начальном состоянии.")
        out["hypotheses"].append("Начальное состояние модели противоречит свойству — проверьте initial.")
        return out

    last_step = trace.steps[-1]
    tname = last_step.transition
    t = tr.get(tname)
    prop_vars = ex.variables_of(ex.parse(prop.expression))
    gvars = _guard_vars(model, tname)
    if t is not None:
        gtext = ex.to_text(ex.parse(t.guard))
        prev_state = trace.steps[-2].state
        unchecked = sorted(v for v in prop_vars if v not in gvars)
        hint = (f"Последний переход {tname} разрешён условием «{gtext}», которое в состоянии "
                f"{_state_text(prev_state)} истинно.")
        if unchecked:
            hint += f" Это условие не проверяет {', '.join(unchecked)}."
        out["diagnostic_hints"].append(hint)
        if unchecked:
            out["hypotheses"].append(
                f"В условии перехода {tname} отсутствует проверка, исключающая этот сценарий "
                f"(например, по {', '.join(unchecked)}).")

        # «залипший» признак: переменная из guard была установлена раньше и
        # не была сброшена промежуточным переходом, изменившим состояние объекта
        n = len(trace.steps) - 1
        for g in sorted(gvars):
            src = _last_setter(trace, g, n - 1)
            if src is None:
                continue
            for k in range(src + 1, n):
                st = trace.steps[k]
                if st.changes and g not in st.changes and (set(st.changes) & (prop_vars | gvars)):
                    setter = trace.steps[src].transition
                    mid = st.transition
                    out["diagnostic_hints"].append(
                        f"Переход {mid} меняет {', '.join(st.changes)}, но не изменяет {g}; "
                        f"значение {g}={_v(trace.steps[k].state[g])}, установленное переходом {setter}, "
                        f"сохраняется и продолжает разрешать {tname}.")
                    out["hypotheses"].append(f"Переход {mid} должен сбрасывать признак {g}.")
                    mdesc = tr[mid].description if mid in tr and tr[mid].description else mid
                    gd = next((v.description for v in model.variables if v.name == g and v.description), g)
                    gd = gd[:1].lower() + gd[1:]
                    out["questions"].append(
                        f"Должен ли после события «{mdesc}» сохраняться {gd} ({g}={_v(trace.steps[src].state[g])})? "
                        f"Можно ли после этого выполнять {tname}?")
                    break

    out["hypotheses"].append(
        f"Требования допускают этот сценарий, и свойство «{pdesc}» сформулировано строже, чем задумано.")
    out["hypotheses"].append("Модель неверно интерпретирует требования (ошибка формализации).")
    if not out["questions"]:
        out["questions"].append(f"Допустим ли по бизнес-логике сценарий {' → '.join(trace.transitions())}?")
    return out


def render_explanation(model: BehavioralModel, result: VerificationResult) -> str:
    """Текстовое объяснение без LLM (fallback и офлайн-демо)."""
    prop = next((p for p in model.properties if p.id == result.property_id), None)
    title = f"{result.property_id}" + (f" — «{prop.description}»" if prop and prop.description else "")
    a = analyze(model, result)
    lines = [f"### {title}: {result.status.value}"]
    if result.status == ResultStatus.PROPERTY_HOLDS:
        if result.witness:
            lines.append("Целевое состояние достижимо, например так:")
            lines += a["business_steps"]
        else:
            lines.append(result.message or "")
    elif result.status == ResultStatus.PROPERTY_VIOLATED:
        if result.counterexample:
            lines.append("Найден допустимый по модели сценарий, нарушающий свойство:")
            lines += a["business_steps"]
        if a["diagnostic_hints"]:
            lines.append("\nЧто происходит:")
            lines += [f"- {h}" for h in a["diagnostic_hints"]]
        if a["hypotheses"]:
            lines.append("\nВозможные причины:")
            lines += [f"- {h}" for h in a["hypotheses"]]
        if a["questions"]:
            lines.append("\nВопросы для уточнения:")
            lines += [f"- {q}" for q in a["questions"]]
    else:
        lines.append(result.message or "")
        for i in result.issues:
            lines.append(f"- [{i.code}] {i.path}: {i.message}")
    if result.related_requirements:
        lines.append("\nСвязанные требования: " + "; ".join(f"{r.id}: «{r.text}»" for r in result.related_requirements))
    return "\n".join(lines)
