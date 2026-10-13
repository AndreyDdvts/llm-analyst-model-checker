"""Офлайн-демонстрация основного сценария без доступа к LLM API.

Роль LLM здесь играет ScriptedLLM: формализация требований — это
записанный ответ модели (examples/order_buggy.json), а итоговое объяснение
собирается из ФАКТИЧЕСКОГО результата Model Checker (TLC запускается
по-настоящему). С реальным LLM тот же сценарий запускается командой
`python -m analyst_agent analyze ...`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .agent import AnalystAgent
from .llm import LLMResponse, ScriptedLLM, last_tool_result, tool_call
from .model_checker import ModelChecker

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

REQUIREMENTS_TEXT = (
    "Заявка после согласования может быть исполнена. "
    "Пользователь может отменить заявку до исполнения."
)
FOLLOW_UP_TEXT = (
    "Да, отменённая заявка исполняться не должна. Пусть отмена снимает признак согласования "
    "(approved := false). Примени это и перепроверь."
)

STATUS_RU = {
    "PROPERTY_HOLDS": "выполняется",
    "PROPERTY_VIOLATED": "НАРУШЕНО",
    "MODEL_INVALID": "модель некорректна",
    "TIMEOUT": "не проверено (таймаут)",
    "STATE_LIMIT_REACHED": "не проверено (лимит состояний)",
    "ENGINE_ERROR": "ошибка движка",
}


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def compose_answer(tool_result: dict, model: dict, previous: Optional[dict] = None) -> str:
    """Шаблон ответа аналитика по результату check_behavior."""
    props = {p["id"]: p for p in model.get("properties", [])}
    out = ["**Итог проверки (TLC):**"]
    for r in tool_result["results"]:
        p = props.get(r.get("property_id"), {})
        before = ""
        if previous:
            prev = next((x for x in previous["results"] if x.get("property_id") == r.get("property_id")), None)
            if prev and prev["status"] != r["status"]:
                before = f" (было: {STATUS_RU[prev['status']]})"
        out.append(f"- {r.get('property_id')} «{p.get('description', '')}» — {STATUS_RU[r['status']]}{before}")

    for r in tool_result["results"]:
        if r["status"] != "PROPERTY_VIOLATED" or "counterexample" not in r:
            continue
        aids = r.get("explanation_aids", {})
        p = props.get(r["property_id"], {})
        out.append(f"\n**Сценарий, нарушающий «{p.get('description', r['property_id'])}»:**")
        out += aids.get("business_steps", [])
        out.append("\n**Аналитический вывод:**")
        out += [f"- {h}" for h in aids.get("diagnostic_hints", [])]
        out.append("\n**Возможные причины:**")
        out += [f"- {h}" for h in aids.get("hypotheses", [])]
        reqs = r.get("related_requirements", [])
        if reqs:
            out.append("\n**Затронутые требования:** " + "; ".join(f"{q['id']} «{q['text']}»" for q in reqs))
        out.append("\n**Уточняющие вопросы:**")
        out += [f"- {q}" for q in aids.get("questions", [])]
        out.append("\nМодель я не менял: подтвердите, какое правило верно, и я перепроверю.")
    if all(r["status"] == "PROPERTY_HOLDS" for r in tool_result["results"]):
        out.append("\nВсе свойства выполняются в исследованной конечной модели "
                   "(это не доказательство корректности production-системы).")
    return "\n".join(out)


def scripted_llm() -> ScriptedLLM:
    buggy, fixed = load_example("order_buggy.json"), load_example("order_fixed.json")
    state: dict = {}

    def answer1(messages):
        state["r1"] = last_tool_result(messages)
        return LLMResponse(text=compose_answer(state["r1"], buggy))

    def answer2(messages):
        return LLMResponse(text=compose_answer(last_tool_result(messages), fixed, previous=state.get("r1")))

    return ScriptedLLM([
        tool_call("check_behavior", model=buggy),
        answer1,
        tool_call("check_behavior", model=fixed),
        answer2,
    ])


def run_demo(checker: Optional[ModelChecker] = None, printer=print) -> list:
    agent = AnalystAgent(scripted_llm(), checker=checker)
    turns = []
    for text in (REQUIREMENTS_TEXT, FOLLOW_UP_TEXT):
        printer(f"\n=== Пользователь ===\n{text}\n")
        turn = agent.ask(text)
        for e in turn.events:
            printer(f"[инструмент {e.name}] → " + ", ".join(
                f"{r.get('property_id')}: {r['status']}" for r in e.output.get("results", [])))
        printer(f"\n=== Агент-аналитик ===\n{turn.text}")
        turns.append(turn)
    return turns
