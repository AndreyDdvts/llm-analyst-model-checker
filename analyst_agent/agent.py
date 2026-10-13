"""Analyst Agent: LLM + инструменты check_behavior / validate_model.

    User → Analyst Agent (LLM) → BehavioralModel → Model Checker Tool
         → VerificationResult → Analyst Agent → объяснение
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import validator
from .explainer import analyze
from .llm import LLMClient
from .model_checker import CheckReport, ModelChecker
from .prompts import SYSTEM_PROMPT, TOOLS
from .schema import BehavioralModel, ResultStatus


@dataclass
class ToolEvent:
    name: str
    input: dict
    output: Any
    blocked: bool = False


@dataclass
class AgentTurn:
    text: str
    events: list[ToolEvent] = field(default_factory=list)
    model: Optional[dict] = None          # последняя переданная на проверку модель
    report: Optional[CheckReport] = None  # последний результат check_behavior

    @property
    def checks(self) -> list[ToolEvent]:
        return [e for e in self.events if e.name == "check_behavior" and not e.blocked]


def logic_fingerprint(model: dict) -> str:
    """Отпечаток логики модели (без описаний и ссылок на требования)."""
    def strip(items, keys):
        return [{k: it.get(k) for k in keys} for it in (items or []) if isinstance(it, dict)]
    core = {
        "variables": strip(model.get("variables"), ["name", "type", "values", "min", "max"]),
        "initial": model.get("initial"),
        "transitions": strip(model.get("transitions"), ["name", "guard", "effects"]),
        "properties": strip(model.get("properties"), ["id", "type", "expression", "terminal"]),
    }
    return json.dumps(core, sort_keys=True, ensure_ascii=False)


class AnalystAgent:
    def __init__(
        self,
        llm: LLMClient,
        checker: Optional[ModelChecker] = None,
        max_steps: int = 10,
        system_prompt: str = SYSTEM_PROMPT,
        enforce_no_autofix: bool = True,
    ):
        self.llm = llm
        self.checker = checker or ModelChecker()
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.enforce_no_autofix = enforce_no_autofix
        self.messages: list[dict] = []
        self.last_model: Optional[dict] = None
        self.last_report: Optional[CheckReport] = None

    # ------------------------------------------------------------------ tools
    def _tool_validate(self, inp: dict) -> dict:
        model, issues = validator.validate(inp.get("model") or {})
        return {
            "valid": model is not None and not validator.has_errors(issues),
            "issues": [i.model_dump() for i in issues],
        }

    def _tool_check(self, inp: dict, turn_state: dict) -> tuple[dict, bool]:
        raw = inp.get("model") or {}
        fp = logic_fingerprint(raw)
        violated_fp = turn_state.get("violated_fp")
        if self.enforce_no_autofix and violated_fp is not None and fp != violated_fp:
            return {
                "error": "POLICY_NO_AUTOFIX",
                "message": (
                    "В этом ходе уже найден контрпример. Изменённую модель нельзя перепроверять без "
                    "решения пользователя: представьте контрпример, гипотезы и уточняющие вопросы. "
                    "Если пользователь подтвердит исправление, его можно будет проверить в следующем ходе."
                ),
            }, True

        report = self.checker.check(raw, inp.get("property_ids") or None)
        self.last_model, self.last_report = raw, report

        model_obj: Optional[BehavioralModel] = None
        if not any(r.status == ResultStatus.MODEL_INVALID for r in report.results):
            model_obj = BehavioralModel.model_validate(raw)
        results = []
        for r in report.results:
            d = r.model_dump(mode="json", exclude_none=True)
            if model_obj is not None and r.status in (ResultStatus.PROPERTY_VIOLATED, ResultStatus.PROPERTY_HOLDS):
                d["explanation_aids"] = analyze(model_obj, r)
            results.append(d)
            if r.status == ResultStatus.PROPERTY_VIOLATED:
                turn_state["violated_fp"] = fp
        return {
            "model_name": report.model_name,
            "warnings": [i.model_dump() for i in report.issues if i.severity == "warning"],
            "results": results,
        }, False

    # ------------------------------------------------------------------ loop
    def ask(self, user_text: str) -> AgentTurn:
        self.messages.append({"role": "user", "content": user_text})
        turn = AgentTurn(text="")
        turn_state: dict = {}  # политика «без автоисправлений» действует в пределах хода

        for _ in range(self.max_steps):
            resp = self.llm.complete(self.system_prompt, self.messages, TOOLS)
            self.messages.append({"role": "assistant", "content": resp.content_blocks()})
            if not resp.tool_calls:
                turn.text = resp.text
                break
            results_blocks = []
            for call in resp.tool_calls:
                blocked = False
                try:
                    if call.name == "check_behavior":
                        out, blocked = self._tool_check(call.input, turn_state)
                        if not blocked:
                            turn.model, turn.report = self.last_model, self.last_report
                    elif call.name == "validate_model":
                        out = self._tool_validate(call.input)
                    else:
                        out = {"error": f"Неизвестный инструмент {call.name}"}
                except Exception as e:  # инструмент не должен ронять диалог
                    out = {"error": "TOOL_FAILURE", "message": f"{type(e).__name__}: {e}"}
                turn.events.append(ToolEvent(call.name, call.input, out, blocked))
                results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(out, ensure_ascii=False),
                    **({"is_error": True} if blocked or "error" in out else {}),
                })
            self.messages.append({"role": "user", "content": results_blocks})
        else:
            turn.text = "Достигнут лимит шагов агента; ответ не сформирован."
        return turn
